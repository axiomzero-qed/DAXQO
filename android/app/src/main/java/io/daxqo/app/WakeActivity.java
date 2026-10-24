package io.daxqo.app;

import android.app.Activity;
import android.app.KeyguardManager;
import android.content.Context;
import android.graphics.Color;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.PowerManager;
import android.os.SystemClock;
import android.view.Gravity;
import android.view.WindowManager;
import android.util.Log;
import android.widget.TextView;

// Only a positively identified, non-secure swipe screen may be dismissed.
public final class WakeActivity extends Activity {
    private static final long TIMEOUT_MS = 2800;
    static final String EXTRA_REQUEST_ID = "io.daxqo.app.WAKE_REQUEST_ID";
    private static WakeActivity active;
    private static String pendingRequest = "", startedRequest = "";
    private static long requestDeadline;
    private final Handler handler = new Handler(Looper.getMainLooper());
    private TextView label;
    private boolean dismissRequested;
    private boolean resumed;
    private String requestId = "";

    static boolean isActive() { return active != null; }
    static boolean hasStarted(String id) { return id.equals(startedRequest); }
    static void beginRequest(String id, long deadline) {
        finishActive();
        pendingRequest = id; startedRequest = ""; requestDeadline = deadline;
    }
    static void finishActive() {
        pendingRequest = ""; startedRequest = "";
        if (active != null) active.finishWake("service_end");
    }
    // Diagnostic stages and system booleans only: no request ids, UI content,
    // credentials, intent payloads or exception messages enter logcat.
    static void trace(Context context, String stage) {
        try {
            DeviceState.LockState lock = DeviceState.lockState(context);
            PowerManager power = (PowerManager) context.getSystemService(POWER_SERVICE);
            WakeActivity activity = context instanceof WakeActivity ? (WakeActivity) context : active;
            Log.d("DAXQO-Wake", stage + " screen=" + (power != null && power.isInteractive())
                    + " known=" + lock.known + " locked=" + lock.locked + " secure=" + lock.secureLock + " deviceSecure=" + lock.deviceSecure
                    + " authorized=" + (DeviceState.enabled(context) && DeviceState.needsUser(context).isEmpty())
                    + " active=" + (active != null) + " resumed=" + (activity != null && activity.resumed)
                    + " focus=" + (activity != null && activity.hasWindowFocus())
                    + " finishing=" + (activity != null && activity.isFinishing())
                    + " remainingMs=" + Math.max(0, requestDeadline - SystemClock.elapsedRealtime()));
        } catch (Exception ignored) { Log.d("DAXQO-Wake", stage + " state_unavailable"); }
    }
    private boolean requestValid() {
        return !requestId.isEmpty() && requestId.equals(pendingRequest) && SystemClock.elapsedRealtime() < requestDeadline;
    }

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        requestId = getIntent().getStringExtra(EXTRA_REQUEST_ID);
        if (requestId == null) requestId = "";
        trace(this, "onCreate");
        if (!requestValid() || DeviceAccessibilityService.current == null
                || !DeviceState.enabled(this) || !DeviceState.needsUser(this).isEmpty()) { trace(this, "create_rejected"); finish(); return; }
        startedRequest = requestId;
        WakeActivity previous = active;
        active = this;
        if (previous != null) previous.finishWake("replaced");
        if (Build.VERSION.SDK_INT >= 27) { setTurnScreenOn(true); setShowWhenLocked(true); }
        else getWindow().addFlags(WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON | WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        label = new TextView(this);
        label.setGravity(Gravity.CENTER);
        label.setTextSize(20);
        label.setTextColor(Color.rgb(35, 48, 64));
        label.setBackgroundColor(Color.WHITE);
        label.setText("达氙正在唤醒手机…");
        setContentView(label);
        trace(this, "window_configured");
        handler.postDelayed(() -> finishWake("activity_deadline"), TIMEOUT_MS);
    }

    @Override protected void onStart() { super.onStart(); trace(this, "onStart"); }

    @Override protected void onResume() {
        super.onResume();
        resumed = true;
        trace(this, "onResume");
        dismissIfAllowed();
    }

    @Override public void onWindowFocusChanged(boolean focus) {
        super.onWindowFocusChanged(focus);
        trace(this, focus ? "window_focus_gained" : "window_focus_lost");
        if (focus && resumed) handler.post(this::dismissIfAllowed);
    }

    private void dismissIfAllowed() {
        if (isFinishing() || isDestroyed()) return;
        if (!requestValid() || DeviceAccessibilityService.current == null) { finishWake("request_invalid"); return; }
        if (!DeviceState.enabled(this) || !DeviceState.needsUser(this).isEmpty()) { finishWake("authorization_stopped"); return; }
        DeviceState.LockState state = DeviceState.lockState(this);
        if (!state.known || state.secureLock || state.deviceSecure) {
            label.setText(state.known ? "手机设有安全锁或 SIM 保护\n请亲自解锁后继续" : "无法确认锁屏安全状态\n请亲自解锁后继续");
            trace(this, "secure_or_unknown");
            handler.postDelayed(() -> finishWake("manual_unlock_required"), 1000);
            return;
        }
        if (!state.locked) {
            PowerManager power = (PowerManager) getSystemService(POWER_SERVICE);
            if (power != null && power.isInteractive()) finishWake("already_awake_unlocked");
            else handler.postDelayed(this::dismissIfAllowed, 100);
            return;
        }
        if (dismissRequested || !DeviceState.mayDismissKeyguard(this, state)) return;
        // Re-query immediately before the system call; never request a secure
        // dismissal, even when Android currently regards the device as trusted.
        state = DeviceState.lockState(this);
        if (!DeviceState.mayDismissKeyguard(this, state)) { finishWake("dismiss_gate_changed"); return; }
        KeyguardManager manager = (KeyguardManager) getSystemService(KEYGUARD_SERVICE);
        if (manager == null) { finishWake("keyguard_unavailable"); return; }
        dismissRequested = true;
        try {
            trace(this, "request_dismiss");
            manager.requestDismissKeyguard(this, new KeyguardManager.KeyguardDismissCallback() {
                @Override public void onDismissSucceeded() { trace(WakeActivity.this, "dismiss_succeeded"); handler.post(() -> finishWake("dismiss_succeeded")); }
                @Override public void onDismissError() { trace(WakeActivity.this, "dismiss_error"); handler.post(() -> finishWake("dismiss_error")); }
                @Override public void onDismissCancelled() { trace(WakeActivity.this, "dismiss_cancelled"); handler.post(() -> finishWake("dismiss_cancelled")); }
            });
        } catch (Exception ignored) { finishWake("dismiss_exception"); }
    }

    private void finishWake(String reason) {
        trace(this, "finish_" + reason);
        handler.removeCallbacksAndMessages(null);
        getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        if (!isFinishing()) finish();
    }

    @Override protected void onPause() {
        resumed = false;
        trace(this, "onPause");
        super.onPause();
        // Screen-on/keyguard transitions may pause the activity before its first
        // frame. Keep the bounded lifetime; Android validates the requesting
        // activity's visibility, and the original deadline closes the window.
    }

    @Override protected void onStop() { trace(this, "onStop"); super.onStop(); }

    @Override protected void onDestroy() {
        trace(this, "onDestroy");
        handler.removeCallbacksAndMessages(null);
        if (active == this) active = null;
        super.onDestroy();
    }
}
