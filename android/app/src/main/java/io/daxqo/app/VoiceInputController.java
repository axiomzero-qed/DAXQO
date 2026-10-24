package io.daxqo.app;

import android.Manifest;
import android.content.Context;
import android.content.ComponentName;
import android.content.Intent;
import android.content.pm.ApplicationInfo;
import android.content.pm.PackageManager;
import android.content.pm.ResolveInfo;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.speech.RecognitionListener;
import android.speech.RecognizerIntent;
import android.speech.RecognitionService;
import android.speech.SpeechRecognizer;
import android.provider.Settings;

import java.util.ArrayList;
import java.util.List;

/** One-shot speech-to-text helper. All public methods must be called on the main thread. */
public final class VoiceInputController {
    private static final String RECOGNITION_SETTING = "voice_recognition_service";
    private static final long MAX_SESSION_MS = 45_000L;
    public enum Availability { ON_DEVICE, SYSTEM_SERVICE, NO_SERVICE }

    public interface Callback {
        /** Status suitable for a small UI hint, such as listening or processing. */
        void onStatus(String message);
        /** Called once for a successful final transcript. Never submits it. */
        void onFinalText(String text);
        /** Called when startup or recognition fails. */
        void onError(String message);
    }

    private final Context appContext;
    private final Callback callback;
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private Runnable timeout;
    private SpeechRecognizer recognizer;
    private boolean active;
    private boolean destroyed;
    private long session;

    public VoiceInputController(Context context, Callback callback) {
        this.appContext = context.getApplicationContext();
        this.callback = callback;
    }

    /** Reports which route start() will use; callers can disclose system recognition first. */
    public static Availability getAvailability(Context context) {
        try {
            if (Build.VERSION.SDK_INT >= 31
                    && SpeechRecognizer.isOnDeviceRecognitionAvailable(context)) {
                return Availability.ON_DEVICE;
            }
            return recognitionService(context) != null ? Availability.SYSTEM_SERVICE : Availability.NO_SERVICE;
        } catch (RuntimeException e) {
            return Availability.NO_SERVICE;
        }
    }

    public static boolean requiresSystemRecognition(Context context) {
        return getAvailability(context) == Availability.SYSTEM_SERVICE;
    }

    /** Some OEMs ship a recognizer but leave VOICE_RECOGNITION_SERVICE unset. */
    private static ServiceInfo recognitionService(Context context) {
        List<ResolveInfo> choices = context.getPackageManager().queryIntentServices(
                new Intent(RecognitionService.SERVICE_INTERFACE), 0);
        String selected = Settings.Secure.getString(context.getContentResolver(),
                RECOGNITION_SETTING);
        ServiceInfo best = null;
        for (ResolveInfo choice : choices) {
            ServiceInfo service = choice.serviceInfo;
            if (service == null || !service.enabled || !service.exported
                    || service.applicationInfo == null || !service.applicationInfo.enabled) continue;
            // Legacy OEM services may have no bind permission. Modern services
            // use the system broker's BIND_VOICE_INTERACTION permission.
            if (service.permission != null && !service.permission.isEmpty()
                    && !Manifest.permission.BIND_VOICE_INTERACTION.equals(service.permission)) continue;
            String component = new ComponentName(service.packageName, service.name).flattenToString();
            if (component.equals(selected) || new ComponentName(service.packageName, service.name).flattenToShortString().equals(selected)) return service;
            boolean system = (service.applicationInfo.flags & ApplicationInfo.FLAG_SYSTEM) != 0;
            boolean bestSystem = best != null && (best.applicationInfo.flags & ApplicationInfo.FLAG_SYSTEM) != 0;
            if (best == null || (system && !bestSystem)
                    || (system == bestSystem && component.compareTo(new ComponentName(best.packageName, best.name).flattenToString()) < 0)) best = service;
        }
        return best;
    }

    public static String systemServiceLabel(Context context) {
        try {
            ServiceInfo service = recognitionService(context);
            return service == null ? "系统服务" : service.loadLabel(context.getPackageManager()).toString();
        } catch (RuntimeException ignored) { return "系统服务"; }
    }

    public boolean isListening() { return active; }

    /** Starts one recognition session. Runtime RECORD_AUDIO permission is owned by the caller. */
    public void start() {
        if (destroyed) {
            callback.onError("语音输入已关闭");
            return;
        }
        if (active) return; // Ignore rapid repeat taps.
        if (appContext.checkSelfPermission(Manifest.permission.RECORD_AUDIO)
                != PackageManager.PERMISSION_GRANTED) {
            callback.onError("请先授予麦克风权限");
            return;
        }

        Availability availability = getAvailability(appContext);
        if (availability == Availability.NO_SERVICE) {
            callback.onError("此设备没有可用的语音识别服务");
            return;
        }
        active = true;
        final long currentSession = ++session;
        try {
            if (Build.VERSION.SDK_INT >= 31 && availability == Availability.ON_DEVICE) {
                recognizer = SpeechRecognizer.createOnDeviceSpeechRecognizer(appContext);
            } else {
                ServiceInfo service = recognitionService(appContext);
                if (service == null) { finishError(currentSession, "此设备没有可用的语音识别服务"); return; }
                recognizer = SpeechRecognizer.createSpeechRecognizer(appContext,
                        new ComponentName(service.packageName, service.name));
            }
            recognizer.setRecognitionListener(new Listener(currentSession));
            Intent intent = new Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH);
            intent.putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL,
                    RecognizerIntent.LANGUAGE_MODEL_FREE_FORM);
            intent.putExtra(RecognizerIntent.EXTRA_LANGUAGE, "zh-CN");
            intent.putExtra(RecognizerIntent.EXTRA_LANGUAGE_PREFERENCE, "zh-CN");
            intent.putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, false);
            callback.onStatus("正在聆听…");
            timeout = () -> finishError(currentSession, "语音输入超时，请重试");
            mainHandler.postDelayed(timeout, MAX_SESSION_MS);
            recognizer.startListening(intent);
        } catch (SecurityException e) {
            finishError(currentSession, "无法访问麦克风，请检查权限");
        } catch (RuntimeException e) {
            finishError(currentSession, "无法启动语音识别");
        }
    }

    /** Requests end of speech and final recognition. */
    public void stop() {
        if (!active || recognizer == null) return;
        callback.onStatus("正在识别…");
        try { recognizer.stopListening(); }
        catch (RuntimeException e) { finishError(session, "语音识别未能完成"); }
    }

    /** Cancels silently and invalidates any callbacks already queued by the service. */
    public void cancel() {
        if (!active) return;
        active = false;
        ++session;
        clearTimeout();
        if (recognizer != null) {
            try { recognizer.cancel(); } catch (RuntimeException ignored) { }
            try { recognizer.destroy(); } catch (RuntimeException ignored) { }
            recognizer = null;
        }
    }

    /** Releases resources; safe to call more than once. */
    public void destroy() {
        if (destroyed) return;
        cancel();
        destroyed = true;
    }

    private void finishError(long expected, String message) {
        if (!isCurrent(expected)) return;
        releaseSession();
        callback.onError(message);
    }

    private boolean isCurrent(long expected) {
        return !destroyed && active && session == expected;
    }

    private void releaseSession() {
        active = false;
        clearTimeout();
        if (recognizer != null) {
            try { recognizer.destroy(); } catch (RuntimeException ignored) { }
            recognizer = null;
        }
    }

    private void clearTimeout() {
        if (timeout != null) {
            mainHandler.removeCallbacks(timeout);
            timeout = null;
        }
    }

    private final class Listener implements RecognitionListener {
        private final long expected;
        Listener(long expected) { this.expected = expected; }
        @Override public void onReadyForSpeech(Bundle params) {
            if (isCurrent(expected)) callback.onStatus("请开始说话");
        }
        @Override public void onBeginningOfSpeech() {
            if (isCurrent(expected)) callback.onStatus("正在聆听…");
        }
        @Override public void onRmsChanged(float rmsdB) { }
        @Override public void onBufferReceived(byte[] buffer) { }
        @Override public void onEndOfSpeech() {
            if (isCurrent(expected)) callback.onStatus("正在识别…");
        }
        @Override public void onError(int error) {
            if (!isCurrent(expected)) return;
            if (error == SpeechRecognizer.ERROR_NO_MATCH || error == SpeechRecognizer.ERROR_SPEECH_TIMEOUT) {
                finishError(expected, "没有识别到语音，请重试");
            } else if (error == SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS) {
                boolean appHasMicPermission = appContext.checkSelfPermission(Manifest.permission.RECORD_AUDIO)
                        == PackageManager.PERMISSION_GRANTED;
                finishError(expected, appHasMicPermission
                        ? "系统语音识别服务报告权限不足；达氙已获麦克风权限，请检查系统麦克风开关或更换识别服务。"
                        : "请先授予达氙麦克风权限后重试");
            } else if (error == SpeechRecognizer.ERROR_LANGUAGE_NOT_SUPPORTED
                    || error == SpeechRecognizer.ERROR_LANGUAGE_UNAVAILABLE) {
                finishError(expected, "中文语音资源暂不可用，请检查系统语音识别设置");
            } else {
                finishError(expected, "语音识别失败，请重试");
            }
        }
        @Override public void onResults(Bundle results) {
            if (!isCurrent(expected)) return;
            ArrayList<String> matches = results == null ? null
                    : results.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION);
            releaseSession();
            String transcript = matches == null || matches.isEmpty() ? null : matches.get(0);
            if (transcript == null || transcript.trim().isEmpty()) {
                callback.onError("没有识别到语音，请重试");
            } else {
                callback.onFinalText(transcript.trim());
            }
        }
        @Override public void onPartialResults(Bundle partialResults) { }
        @Override public void onEvent(int eventType, Bundle params) { }
    }
}
