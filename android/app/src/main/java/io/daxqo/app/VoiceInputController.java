package io.daxqo.app;

import android.Manifest;
import android.content.Context;
import android.content.pm.PackageManager;
import android.media.AudioFormat;
import android.media.AudioRecord;
import android.media.MediaRecorder;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;

import com.k2fsa.sherpa.onnx.FeatureConfig;
import com.k2fsa.sherpa.onnx.OnlineModelConfig;
import com.k2fsa.sherpa.onnx.OnlineRecognizer;
import com.k2fsa.sherpa.onnx.OnlineRecognizerConfig;
import com.k2fsa.sherpa.onnx.OnlineRecognizerResult;
import com.k2fsa.sherpa.onnx.OnlineStream;
import com.k2fsa.sherpa.onnx.OnlineZipformer2CtcModelConfig;

import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** Local-only streaming Chinese ASR. Public methods must be called on the main thread. */
public final class VoiceInputController {
    private static final int SAMPLE_RATE = 16000;
    private static final int CHANNEL = AudioFormat.CHANNEL_IN_MONO;
    private static final int PCM = AudioFormat.ENCODING_PCM_16BIT;
    private static final long MAX_SESSION_MS = 60_000L;
    private static final String TAG = "DAXQO-Voice";
    private final Context appContext;
    private final Callback callback;
    private final Handler main = new Handler(Looper.getMainLooper());
    private final ExecutorService worker = Executors.newSingleThreadExecutor(r -> {
        Thread t = new Thread(r, "daxqo-local-asr");
        t.setDaemon(true);
        return t;
    });
    private final Object admissionLock = new Object();
    private volatile Session active;
    private volatile long generation;
    private volatile boolean destroyed;

    public interface Callback {
        void onStatus(String message);
        /** Full cumulative hypothesis; later partials may revise it. */
        void onPartialText(String fullHypothesis);
        void onFinalText(String text);
        void onError(String message);
        /** Used when release happens before recording has started (for example during model load). */
        void onCancelled();
    }

    private static final class Session {
        final long generation;
        volatile boolean stopRequested;
        volatile boolean cancelled;
        volatile boolean timedOut;
        volatile AudioRecord recorder;
        final VoiceTranscriptAccumulator transcript = new VoiceTranscriptAccumulator();
        Runnable timeout;
        boolean capturedAudio;
        String lastPartial;
        Session(long generation) { this.generation = generation; }
    }

    public VoiceInputController(Context context, Callback callback) {
        this.appContext = context.getApplicationContext();
        this.callback = callback;
    }

    /** This implementation is bundled-model-only; availability does not depend on system ASR. */
    public static boolean isAvailable(Context context) {
        return context != null && context.getPackageManager().hasSystemFeature(
                PackageManager.FEATURE_MICROPHONE);
    }

    public boolean isListening() { return active != null; }

    public void start() {
        if (destroyed || active != null) return;
        if (appContext.checkSelfPermission(Manifest.permission.RECORD_AUDIO)
                != PackageManager.PERMISSION_GRANTED) {
            callback.onError("请先授予达氙麦克风权限");
            return;
        }
        Session session = new Session(++generation);
        active = session;
        callback.onStatus("正在加载本机语音模型");
        session.timeout = () -> {
            if (active == session && !session.stopRequested && !session.cancelled) {
                session.timedOut = true;
                requestStop(session);
                callback.onStatus("已到单次时长上限，正在整理文字");
            }
        };
        main.postDelayed(session.timeout, MAX_SESSION_MS);
        worker.execute(() -> recognize(session));
    }

    /** Stops capture promptly and lets the worker flush the final hypothesis. */
    public void stop() {
        Session session = active;
        if (session == null || session.stopRequested || session.cancelled) return;
        boolean wasCapturing = requestStop(session);
        if (!wasCapturing) {
            session.cancelled = true;
            finishCancelled(session);
        } else {
            callback.onStatus("正在整理文字");
        }
    }

    /** Invalidates queued callbacks and stops capture without returning text. */
    public void cancel() {
        Session session;
        synchronized (admissionLock) {
            session = active;
            if (session == null) return;
            ++generation;
            active = null;
            session.cancelled = true;
            session.stopRequested = true;
            stopRecorderLocked(session);
        }
        clearTimeout(session);
    }

    public void destroy() {
        if (destroyed) return;
        cancel();
        destroyed = true;
        worker.shutdownNow();
    }

    private void recognize(Session s) {
        OnlineRecognizer recognizer = null;
        OnlineStream stream = null;
        AudioRecord record = null;
        try {
            if (!isCurrent(s) || s.stopRequested) { finishCancelled(s); return; }
            recognizer = createRecognizer();
            if (!isCurrent(s) || s.stopRequested) { finishCancelled(s); return; }
            stream = recognizer.createStream("");
            int minBuffer = AudioRecord.getMinBufferSize(SAMPLE_RATE, CHANNEL, PCM);
            if (minBuffer <= 0) throw new IllegalStateException("当前设备不支持本机录音");
            int bufferBytes = Math.max(minBuffer * 2, 4096);
            record = new AudioRecord(MediaRecorder.AudioSource.MIC, SAMPLE_RATE, CHANNEL, PCM, bufferBytes);
            if (record.getState() != AudioRecord.STATE_INITIALIZED) {
                throw new IllegalStateException("无法初始化麦克风");
            }
            s.recorder = record;
            if (!isCurrent(s) || s.stopRequested) { finishCancelled(s); return; }
            if (!startRecordingIfAdmitted(s, record)) {
                finishCancelled(s);
                return;
            }
            if (record.getRecordingState() != AudioRecord.RECORDSTATE_RECORDING) {
                throw new IllegalStateException("无法开始麦克风录音");
            }
            postStatus(s, "正在聆听");
            short[] pcm = new short[1024];
            while (isCurrent(s) && !s.cancelled && !s.stopRequested) {
                int count = record.read(pcm, 0, pcm.length, AudioRecord.READ_BLOCKING);
                if (count < 0) {
                    if (s.stopRequested || s.cancelled) break;
                    throw new IllegalStateException("读取麦克风失败");
                }
                if (count == 0) continue;
                s.capturedAudio = true;
                float[] samples = new float[count];
                for (int i = 0; i < count; i++) samples[i] = pcm[i] / 32768.0f;
                stream.acceptWaveform(samples, SAMPLE_RATE);
                decodeReady(recognizer, stream);
                String current = resultText(recognizer, stream);
                if (recognizer.isEndpoint(stream)) {
                    String full = s.transcript.commitEndpoint(current);
                    if (!full.isEmpty()) postPartial(s, full);
                    recognizer.reset(stream);
                } else if (!current.isEmpty()) {
                    postPartial(s, s.transcript.hypothesis(current));
                }
            }
            if (s.cancelled || !isCurrent(s)) { finishCancelled(s); return; }
            stopAndReleaseRecorder(s, record);
            record = null;
            stream.inputFinished();
            decodeReady(recognizer, stream);
            String finalText = s.transcript.finish(resultText(recognizer, stream));
            if (s.timedOut) postStatus(s, "正在整理文字");
            if (!s.capturedAudio || finalText.isEmpty()) {
                finishError(s, s.capturedAudio ? "没有识别到语音，请重试" : "没有收到语音，请按住说话后再松开");
            } else {
                finishText(s, finalText);
            }
        } catch (SecurityException e) {
            logFailure(e);
            finishError(s, "无法访问麦克风，请检查达氙的麦克风权限");
        } catch (LinkageError e) {
            logFailure(e);
            finishError(s, "本机语音组件与设备不兼容");
        } catch (Exception e) {
            logFailure(e);
            finishError(s, "本机语音模型不可用或识别失败，请检查模型文件后重试");
        } finally {
            s.recorder = null;
            if (record != null) stopAndReleaseRecorder(s, record);
            if (stream != null) try { stream.release(); } catch (RuntimeException ignored) { }
            if (recognizer != null) try { recognizer.release(); } catch (RuntimeException ignored) { }
        }
    }

    private OnlineRecognizer createRecognizer() {
        FeatureConfig feature = new FeatureConfig(SAMPLE_RATE, 80, 0.0f);
        OnlineModelConfig model = new OnlineModelConfig();
        model.setZipformer2Ctc(new OnlineZipformer2CtcModelConfig("voice/model.int8.onnx"));
        model.setTokens("voice/tokens.txt");
        model.setNumThreads(2);
        model.setProvider("cpu");
        model.setModelingUnit("cjkchar");
        OnlineRecognizerConfig config = new OnlineRecognizerConfig();
        config.setFeatConfig(feature);
        config.setModelConfig(model);
        return new OnlineRecognizer(appContext.getAssets(), config);
    }

    private static void decodeReady(OnlineRecognizer recognizer, OnlineStream stream) {
        while (recognizer.isReady(stream)) recognizer.decode(stream);
    }

    private static String resultText(OnlineRecognizer recognizer, OnlineStream stream) {
        OnlineRecognizerResult result = recognizer.getResult(stream);
        return result == null || result.getText() == null ? "" : result.getText();
    }

    /** Linearizes admission with release/cancel; lock is never held during read or decode. */
    private boolean startRecordingIfAdmitted(Session s, AudioRecord r) {
        synchronized (admissionLock) {
            if (!isCurrent(s) || s.stopRequested || s.cancelled) return false;
            r.startRecording();
            return r.getRecordingState() == AudioRecord.RECORDSTATE_RECORDING;
        }
    }

    private boolean requestStop(Session s) {
        synchronized (admissionLock) {
            if (active != s || s.cancelled) return false;
            AudioRecord r = s.recorder;
            boolean wasCapturing = false;
            if (r != null) {
                try { wasCapturing = r.getRecordingState() == AudioRecord.RECORDSTATE_RECORDING; }
                catch (RuntimeException ignored) { }
            }
            s.stopRequested = true;
            stopRecorderLocked(s);
            return wasCapturing;
        }
    }

    private static void stopRecorderLocked(Session s) {
        AudioRecord r = s.recorder;
        if (r != null) {
            try {
                if (r.getRecordingState() == AudioRecord.RECORDSTATE_RECORDING) r.stop();
            } catch (RuntimeException ignored) { }
        }
    }

    private static void logFailure(Throwable error) {
        Log.w(TAG, "local ASR failure: " + error.getClass().getSimpleName());
    }

    private static void stopAndReleaseRecorder(Session s, AudioRecord r) {
        try {
            if (r.getRecordingState() == AudioRecord.RECORDSTATE_RECORDING) r.stop();
        } catch (RuntimeException ignored) { }
        try { r.release(); } catch (RuntimeException ignored) { }
        if (s.recorder == r) s.recorder = null;
    }

    private boolean isCurrent(Session s) {
        return !destroyed && active == s && generation == s.generation;
    }

    private void postStatus(Session s, String message) {
        main.post(() -> { if (isCurrent(s)) callback.onStatus(message); });
    }

    private void postPartial(Session s, String text) {
        if (text.equals(s.lastPartial)) return;
        s.lastPartial = text;
        main.post(() -> { if (isCurrent(s)) callback.onPartialText(text); });
    }

    private void finishText(Session s, String text) {
        main.post(() -> {
            if (!isCurrent(s)) return;
            clearTimeout(s);
            active = null;
            callback.onFinalText(text);
        });
    }

    private void finishError(Session s, String message) {
        main.post(() -> {
            if (!isCurrent(s)) return;
            clearTimeout(s);
            active = null;
            callback.onError(message);
        });
    }

    private void finishCancelled(Session s) {
        main.post(() -> {
            if (!isCurrent(s)) return;
            clearTimeout(s);
            active = null;
            callback.onCancelled();
        });
    }

    private void clearTimeout(Session s) {
        if (s.timeout != null) {
            main.removeCallbacks(s.timeout);
            s.timeout = null;
        }
    }
}
