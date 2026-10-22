package io.daxqo.app;

import android.app.Activity;
import android.app.KeyguardManager;
import android.graphics.Color;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.PowerManager;
import android.os.SystemClock;
import android.view.Gravity;
import android.view.WindowManager;
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
    private String requestId = "";

    static boolean isActive() { return active != null; }
    static boolean hasStarted(String id) { return id.equals(startedRequest); }
    static void beginRequest(String id, long deadline) {
        finishActive();
        pendingRequest = id; startedRequest = ""; requestDeadline = deadline;
    }
    static void finishActive() {
        pendingRequest = ""; startedRequest = "";
        if (active != null) active.finishWake();
    }
    private boolean requestValid() {
        return !requestId.isEmpty() && requestId.equals(pendingRequest) && SystemClock.elapsedRealtime() < requestDeadline;
    }

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        requestId = getIntent().getStringExtra(EXTRA_REQUEST_ID);
        if (requestId == null) requestId = "";
        if (!requestValid() || DeviceAccessibilityService.current == null
                || !DeviceState.enabled(this) || !DeviceState.needsUser(this).isEmpty()) { finish(); return; }
        startedRequest = requestId;
        WakeActivity previous = active;
        active = this;
        if (previous != null) previous.finishWake();
        if (Build.VERSION.SDK_INT >= 27) { setTurnScreenOn(true); setShowWhenLocked(true); }
        else getWindow().addFlags(WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON | WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        label = new TextView(this);
        label.setGravity(Gravity.CENTER);
        label.setTextSize(20);
        label.setTextColor(Color.rgb(35, 48, 64));
        label.setBackgroundColor(Color.WHITE);
        label.setText("达序正在唤醒手机…");
        setContentView(label);
        handler.postDelayed(this::finishWake, TIMEOUT_MS);
    }

    @Override protected void onResume() {
        super.onResume();
        getWindow().getDecorView().post(this::dismissIfAllowed);
    }

    private void dismissIfAllowed() {
        if (isFinishing() || isDestroyed()) return;
        if (!requestValid() || DeviceAccessibilityService.current == null) { finishWake(); return; }
        if (!DeviceState.enabled(this) || !DeviceState.needsUser(this).isEmpty()) { finishWake(); return; }
        DeviceState.LockState state = DeviceState.lockState(this);
        if (!state.known || state.secureLock || state.deviceSecure) {
            label.setText(state.known ? "手机设有安全锁或 SIM 保护\n请亲自解锁后继续" : "无法确认锁屏安全状态\n请亲自解锁后继续");
            handler.postDelayed(this::finishWake, 1000);
            return;
        }
        if (!state.locked) {
            PowerManager power = (PowerManager) getSystemService(POWER_SERVICE);
            if (power != null && power.isInteractive()) finishWake();
            else handler.postDelayed(this::dismissIfAllowed, 100);
            return;
        }
        if (dismissRequested || !DeviceState.mayDismissKeyguard(this, state)) return;
        // Re-query immediately before the system call; never request a secure
        // dismissal, even when Android currently regards the device as trusted.
        state = DeviceState.lockState(this);
        if (!DeviceState.mayDismissKeyguard(this, state)) { finishWake(); return; }
        KeyguardManager manager = (KeyguardManager) getSystemService(KEYGUARD_SERVICE);
        if (manager == null) { finishWake(); return; }
        dismissRequested = true;
        try {
            manager.requestDismissKeyguard(this, new KeyguardManager.KeyguardDismissCallback() {
                @Override public void onDismissSucceeded() { handler.post(WakeActivity.this::finishWake); }
                @Override public void onDismissError() { handler.post(WakeActivity.this::finishWake); }
                @Override public void onDismissCancelled() { handler.post(WakeActivity.this::finishWake); }
            });
        } catch (Exception ignored) { finishWake(); }
    }

    private void finishWake() {
        handler.removeCallbacksAndMessages(null);
        getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        if (!isFinishing()) finish();
    }

    @Override protected void onPause() {
        super.onPause();
        finishWake();
    }

    @Override protected void onDestroy() {
        handler.removeCallbacksAndMessages(null);
        if (active == this) active = null;
        super.onDestroy();
    }
}
