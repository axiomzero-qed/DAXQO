package io.daxqo.app;

import android.accessibilityservice.AccessibilityService;
import android.accessibilityservice.GestureDescription;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Intent;
import android.graphics.Bitmap;
import android.graphics.Path;
import android.graphics.Rect;
import android.hardware.HardwareBuffer;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.PowerManager;
import android.util.Base64;
import android.view.Display;
import android.view.accessibility.AccessibilityEvent;
import android.view.accessibility.AccessibilityNodeInfo;
import org.json.JSONArray;
import org.json.JSONObject;
import java.io.ByteArrayOutputStream;
import java.util.Locale;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;

public final class DeviceAccessibilityService extends AccessibilityService {
    static volatile DeviceAccessibilityService current;
    private static final int NOTICE_ID = 4001;
    private static final String CHANNEL = "phone_control";
    private final Handler main = new Handler(Looper.getMainLooper());
    private final ExecutorService screenshots = Executors.newSingleThreadExecutor();
    private DeviceHttpBridge bridge;
    private String snapshotId = "";
    private String snapshotPackage = "";
    private long snapshotSequence;
    volatile String lastAction = "";
    volatile String lastError = "";

    @Override protected void onServiceConnected() {
        super.onServiceConnected();
        current = this;
        bridge = new DeviceHttpBridge(this);
        bridge.start();
        refreshNotification();
    }

    @Override public void onAccessibilityEvent(AccessibilityEvent event) {
        // Browsing Settings or the notification shade is a user's ordinary
        // interaction, not an agent action. Passive events must not revoke or
        // restore control. executeMain and screenshot/input callbacks enforce
        // the sensitive-screen gates when a tool actually requests access.
    }
    @Override public void onInterrupt() {
        // This callback interrupts accessibility feedback. We provide no such
        // feedback, and it is not a user request to change control permission.
    }
    @Override public void onDestroy() {
        if (current == this) current = null;
        WakeActivity.finishActive();
        if (bridge != null) bridge.close();
        screenshots.shutdownNow();
        main.removeCallbacksAndMessages(null);
        ((NotificationManager) getSystemService(NOTIFICATION_SERVICE)).cancel(NOTICE_ID);
        super.onDestroy();
    }
    boolean bridgeListening() { return bridge != null && bridge.isListening(); }

    void refreshNotification() {
        NotificationManager manager = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        manager.createNotificationChannel(new NotificationChannel(CHANNEL, "手机控制", NotificationManager.IMPORTANCE_LOW));
        Intent open = new Intent(this, ControlActivity.class);
        PendingIntent content = PendingIntent.getActivity(this, 1, open, PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Intent pause = new Intent(this, ControlActivity.class).setAction(ControlActivity.ACTION_PAUSE);
        PendingIntent pauseIntent = PendingIntent.getActivity(this, 2, pause, PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        String reason = DeviceState.needsUser(this);
        String text = DeviceState.enabled(this) ? "已允许本机任务操作手机 · 点此管理" : reason.isEmpty() ? "手机操作已暂停 · 点此管理" : "需要你处理：" + reason;
        Notification notice = new Notification.Builder(this, CHANNEL)
                .setSmallIcon(R.drawable.ic_launcher_foreground)
                .setContentTitle("达序手机控制")
                .setContentText(text)
                .setContentIntent(content)
                .setOngoing(true)
                .addAction(new Notification.Action.Builder(null, "暂停控制", pauseIntent).build())
                .build();
        try { manager.notify(NOTICE_ID, notice); } catch (SecurityException ignored) { }
    }

    String foregroundPackage() {
        AccessibilityNodeInfo root = getRootInActiveWindow();
        if (root == null) return "";
        try { return value(root.getPackageName(), 200); } finally { root.recycle(); }
    }

    static JSONObject failure(String id, String code, String message) {
        JSONObject response = new JSONObject();
        try { response.put("id", id); response.put("ok", false); response.put("error", new JSONObject().put("code", code).put("message", message)); }
        catch (Exception ignored) { }
        return response;
    }
    private static JSONObject success(String id, JSONObject result) {
        JSONObject response = new JSONObject();
        try { response.put("id", id); response.put("ok", true); response.put("result", result); } catch (Exception ignored) { }
        return response;
    }

    // Invoked on a bridge worker. All Android UI operations are serialized on the main thread.
    JSONObject execute(JSONObject request) {
        String id = request.optString("id", "");
        String action = request.optString("action", "");
        JSONObject args = request.optJSONObject("args");
        if (args == null) args = new JSONObject();
        if (id.length() > 128 || action.length() > 40) return failure(id, "invalid_args", "请求标识过长");
        if ("status".equals(action)) return success(id, DeviceState.status(this));
        final JSONObject actionArgs = args;
        CountDownLatch finished = new CountDownLatch(1);
        JSONObject[] result = new JSONObject[1];
        AtomicBoolean cancelled = new AtomicBoolean(false);
        main.post(() -> {
            if (cancelled.get()) return;
            try { executeMain(id, action, actionArgs, response -> { result[0] = response; finished.countDown(); }); }
            catch (Exception ignored) { result[0] = failure(id, "action_failed", "手机操作失败"); finished.countDown(); }
        });
        try {
            if (!finished.await(10, TimeUnit.SECONDS)) {
                cancelled.set(true);
                DeviceState.requireUser(this, "操作响应超时，请检查手机后继续");
                return failure(id, "timeout", "操作响应超时，已暂停控制；请检查实际结果，不要盲目重试");
            }
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            cancelled.set(true);
            return failure(id, "timeout", "操作已中断，请先核对手机实际状态");
        }
        boolean ok = result[0] != null && result[0].optBoolean("ok");
        lastAction = action + (ok ? " · 完成" : " · 未完成");
        lastError = ok ? "" : result[0] == null ? "操作无响应" : result[0].optJSONObject("error").optString("message");
        return result[0] == null ? failure(id, "action_failed", "操作无响应") : result[0];
    }

    private interface Completion { void done(JSONObject response); }
    private void executeMain(String id, String action, JSONObject args, Completion callback) throws Exception {
        if (current != this) { callback.done(failure(id, "accessibility_unavailable", "请在系统设置中手动启用达序手机控制")); return; }
        // Wake may dismiss only a non-secure swipe screen through Android's
        // official API. Secure/SIM/unknown states always remain user-controlled.
        if ("wake".equals(action)) {
            if (!DeviceState.enabled(this) || !DeviceState.needsUser(this).isEmpty()) { callback.done(paused(id)); return; }
            PowerManager power = (PowerManager) getSystemService(POWER_SERVICE);
            DeviceState.LockState lock = DeviceState.lockState(this);
            long deadline = android.os.SystemClock.elapsedRealtime() + 3800;
            boolean launch = power == null || !power.isInteractive() || lock.locked;
            if (launch) {
                WakeActivity.beginRequest(id, deadline);
                WakeActivity.trace(this, "service_launch");
                try {
                    startActivity(new Intent(this, WakeActivity.class).putExtra(WakeActivity.EXTRA_REQUEST_ID, id)
                            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
                } catch (Exception ignored) {
                    WakeActivity.trace(this, "service_launch_failed");
                    WakeActivity.finishActive();
                    callback.done(failure(id, "wake_unavailable", "系统未允许打开唤醒界面，请在手机上检查后重试"));
                    return;
                }
            }
            pollWake(id, deadline, launch, callback);
            return;
        }
        if (DeviceState.locked(this)) { callback.done(failure(id, "locked", "手机已锁定，请亲自解锁；达序不会绕过锁屏")); return; }
        PowerManager power = (PowerManager) getSystemService(POWER_SERVICE);
        if (power == null || !power.isInteractive()) { callback.done(failure(id, "screen_off", "屏幕已关闭，请先唤醒手机")); return; }
        if (!DeviceState.enabled(this)) { callback.done(paused(id)); return; }
        String expected = args.optString("expectedPackage", "");
        String actual = foregroundPackage();
        if (!expected.isEmpty() && !expected.equals(actual)) { callback.done(failure(id, "foreground_changed", "前台应用已变化，请重新读取屏幕")); return; }
        String sensitive = sensitiveReason();
        if (!sensitive.isEmpty()) {
            DeviceState.requireUser(this, sensitive);
            callback.done(failure(id, "needs_user", sensitive + "；请亲自完成，再到手机控制页继续"));
            return;
        }
        switch (action) {
            case "tree": callback.done(success(id, tree())); return;
            case "screenshot": screenshot(id, callback); return;
            case "tap": case "long_press": case "swipe": gesture(id, action, args, callback); return;
            case "input": input(id, args, callback); return;
            case "back": case "home":
                boolean performed = performGlobalAction("back".equals(action) ? GLOBAL_ACTION_BACK : GLOBAL_ACTION_HOME);
                callback.done(performed ? success(id, new JSONObject().put("performed", true)) : failure(id, "action_failed", "系统未执行此操作"));
                return;
            case "open_app":
                String packageName = args.optString("packageName", "");
                if (!packageName.matches("[A-Za-z0-9_]+(\\.[A-Za-z0-9_]+)+") || packageName.length() > 200) { callback.done(failure(id, "invalid_args", "请提供有效应用包名")); return; }
                // Authentication and system grant interfaces must be entered by the user.
                if (sensitivePackage(packageName)) { callback.done(failure(id, "needs_user", "此系统授权应用需要你手动打开")); return; }
                Intent launch = getPackageManager().getLaunchIntentForPackage(packageName);
                if (launch == null) { callback.done(failure(id, "app_not_found", "应用未安装或没有可用的桌面入口")); return; }
                launch.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                startActivity(launch);
                callback.done(success(id, new JSONObject().put("launched", true).put("packageName", packageName)));
                return;
            default: callback.done(failure(id, "unsupported", "不支持的手机操作"));
        }
    }
    private void pollWake(String id, long deadline, boolean launchRequested, Completion callback) {
        if (current != this) { WakeActivity.finishActive(); callback.done(failure(id, "accessibility_unavailable", "无障碍服务已断开，请检查手机")); return; }
        if (!DeviceState.enabled(this) || !DeviceState.needsUser(this).isEmpty()) { WakeActivity.finishActive(); callback.done(paused(id)); return; }
        DeviceState.LockState lock = DeviceState.lockState(this);
        PowerManager power = (PowerManager) getSystemService(POWER_SERVICE);
        boolean screenOn = power != null && power.isInteractive();
        // The callback is not proof of dismissal. Require the real keyguard to
        // be gone, the display awake, and the temporary activity fully closed.
        if (lock.known && !lock.locked && screenOn && (!launchRequested || WakeActivity.hasStarted(id)) && !WakeActivity.isActive()) {
            WakeActivity.trace(this, "service_ready");
            WakeActivity.finishActive();
            callback.done(success(id, DeviceState.status(this)));
            return;
        }
        if (android.os.SystemClock.elapsedRealtime() >= deadline) {
            WakeActivity.trace(this, "service_deadline");
            WakeActivity.finishActive();
            if (!lock.known) callback.done(failure(id, "locked", "无法确认锁屏安全状态，请亲自亮屏解锁"));
            else if (lock.locked) callback.done(failure(id, "locked", lock.secureLock || lock.deviceSecure
                    ? "手机设有安全锁或 SIM 保护，请亲自解锁；达序不会自动确认" : "系统未关闭滑动锁屏，请在手机上检查后重试"));
            else callback.done(failure(id, "wake_unavailable", "系统尚未完成唤醒，请在手机上检查后重试"));
            return;
        }
        main.postDelayed(() -> pollWake(id, deadline, launchRequested, callback), 100);
    }
    private JSONObject paused(String id) {
        String reason = DeviceState.needsUser(this);
        return failure(id, reason.isEmpty() ? "paused" : "needs_user", reason.isEmpty() ? "手机控制已暂停，请在手机控制页手动开启" : reason + "；请亲自处理后在手机控制页继续");
    }

    private int[] screenSize() {
        android.util.DisplayMetrics metrics = new android.util.DisplayMetrics();
        getSystemService(android.view.WindowManager.class).getDefaultDisplay().getRealMetrics(metrics);
        return new int[] {metrics.widthPixels, metrics.heightPixels};
    }
    private boolean coordinate(JSONObject args, String name, int limit) {
        Object value = args.opt(name);
        return value instanceof Number && Double.isFinite(((Number) value).doubleValue()) && ((Number) value).doubleValue() >= 0 && ((Number) value).doubleValue() < limit;
    }
    private void gesture(String id, String action, JSONObject args, Completion callback) throws Exception {
        int[] size = screenSize();
        boolean swipe = "swipe".equals(action);
        String x = swipe ? "x1" : "x", y = swipe ? "y1" : "y";
        if (!coordinate(args, x, size[0]) || !coordinate(args, y, size[1]) || (swipe && (!coordinate(args, "x2", size[0]) || !coordinate(args, "y2", size[1])))) { callback.done(failure(id, "invalid_args", "坐标必须位于当前屏幕内")); return; }
        long duration = swipe ? args.optLong("durationMs", 350) : "long_press".equals(action) ? args.optLong("durationMs", 700) : 60;
        if (duration < (swipe ? 50 : "long_press".equals(action) ? 500 : 1) || duration > 2000) { callback.done(failure(id, "invalid_args", "手势时长不在允许范围内")); return; }
        Path path = new Path();
        path.moveTo((float) args.getDouble(x), (float) args.getDouble(y));
        if (swipe) path.lineTo((float) args.getDouble("x2"), (float) args.getDouble("y2"));
        GestureDescription gesture = new GestureDescription.Builder().addStroke(new GestureDescription.StrokeDescription(path, 0, duration)).build();
        boolean accepted = dispatchGesture(gesture, new GestureResultCallback() {
            @Override public void onCompleted(GestureDescription description) { callback.done(success(id, new JSONObject())); }
            @Override public void onCancelled(GestureDescription description) { callback.done(failure(id, "gesture_cancelled", "手势被系统或用户中断，请重新读取屏幕")); }
        }, main);
        if (!accepted) callback.done(failure(id, "action_failed", "系统拒绝执行手势"));
    }

    private void input(String id, JSONObject args, Completion callback) throws Exception {
        Object raw = args.opt("text");
        if (!(raw instanceof String) || ((String) raw).length() > 8000) { callback.done(failure(id, "invalid_args", "输入文本无效或超过8000字符")); return; }
        AccessibilityNodeInfo root = getRootInActiveWindow();
        if (root == null) { callback.done(failure(id, "no_window", "当前应用没有可用界面")); return; }
        AccessibilityNodeInfo node = null;
        try {
            String nodeId = args.optString("nodeId", "");
            if (nodeId.isEmpty()) node = root.findFocus(AccessibilityNodeInfo.FOCUS_INPUT);
            else {
                if (!snapshotId.equals(args.optString("snapshotId", "")) || !snapshotPackage.equals(foregroundPackage())) { callback.done(failure(id, "stale_snapshot", "界面快照已失效，请重新读取UI树")); return; }
                node = nodeAt(root, nodeId);
            }
            if (node == null || !node.isEditable()) { callback.done(failure(id, "no_input", "请先点击可编辑输入框")); return; }
            if (node.isPassword()) { DeviceState.requireUser(this, "密码输入需要你亲自完成"); callback.done(failure(id, "needs_user", "密码输入需要你亲自完成")); return; }
            Bundle value = new Bundle(); value.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, (String) raw);
            boolean ok = node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, value);
            callback.done(ok ? success(id, new JSONObject().put("performed", true)) : failure(id, "action_failed", "应用未接受文本输入"));
        } finally { if (node != null) node.recycle(); root.recycle(); }
    }
    private AccessibilityNodeInfo nodeAt(AccessibilityNodeInfo root, String path) {
        if (!path.matches("0(\\.[0-9]{1,3}){0,30}")) return null;
        AccessibilityNodeInfo node = AccessibilityNodeInfo.obtain(root);
        String[] indices = path.split("\\.");
        for (int i = 1; i < indices.length; i++) {
            int child = Integer.parseInt(indices[i]);
            AccessibilityNodeInfo next = child < node.getChildCount() ? node.getChild(child) : null;
            node.recycle(); node = next;
            if (node == null) return null;
        }
        return node;
    }

    private JSONObject tree() throws Exception {
        int[] size = screenSize();
        AccessibilityNodeInfo root = getRootInActiveWindow();
        JSONArray nodes = new JSONArray();
        boolean[] truncated = {false};
        snapshotId = Long.toString(++snapshotSequence) + "-" + android.os.SystemClock.elapsedRealtime();
        snapshotPackage = root == null ? "" : value(root.getPackageName(), 200);
        if (root != null) { try { appendNode(root, "0", "", nodes, 0, truncated); } finally { root.recycle(); } }
        return new JSONObject().put("snapshotId", snapshotId).put("packageName", snapshotPackage).put("width", size[0]).put("height", size[1]).put("nodes", nodes).put("truncated", truncated[0]);
    }
    private void appendNode(AccessibilityNodeInfo node, String path, String parent, JSONArray nodes, int depth, boolean[] truncated) throws Exception {
        if (depth > 30 || nodes.length() >= 400) { truncated[0] = true; return; }
        Rect bounds = new Rect(); node.getBoundsInScreen(bounds);
        boolean password = node.isPassword();
        JSONObject item = new JSONObject().put("id", path).put("parentId", parent)
                .put("text", password ? "[已隐藏]" : value(node.getText(), 500))
                .put("description", password ? "[已隐藏]" : value(node.getContentDescription(), 500))
                .put("className", value(node.getClassName(), 200)).put("viewId", value(node.getViewIdResourceName(), 250))
                .put("bounds", new JSONArray().put(bounds.left).put(bounds.top).put(bounds.right).put(bounds.bottom))
                .put("clickable", node.isClickable()).put("editable", node.isEditable()).put("scrollable", node.isScrollable())
                .put("enabled", node.isEnabled()).put("visible", node.isVisibleToUser()).put("password", password);
        nodes.put(item);
        for (int i = 0; i < Math.min(node.getChildCount(), 200); i++) {
            AccessibilityNodeInfo child = node.getChild(i);
            if (child == null) continue;
            try { appendNode(child, path + "." + i, path, nodes, depth + 1, truncated); } finally { child.recycle(); }
            if (nodes.length() >= 400) { truncated[0] = true; break; }
        }
    }
    private static String value(CharSequence value, int limit) {
        if (value == null) return "";
        String text = value.toString(); return text.length() > limit ? text.substring(0, limit) : text;
    }
    private boolean sensitivePackage(String name) {
        return name.contains("permissioncontroller") || name.contains("packageinstaller") || name.equals("com.android.settings") || name.equals("com.android.systemui");
    }
    private String sensitiveReason() {
        AccessibilityNodeInfo root = getRootInActiveWindow();
        if (root == null) return "";
        try {
            String packageName = value(root.getPackageName(), 200);
            if (getPackageName().equals(packageName)) return "";
            if (sensitivePackage(packageName)) return "系统授权或安全界面需要你亲自处理";
            int[] inspected = {0};
            return inspectSensitive(root, inspected, 0);
        } finally { root.recycle(); }
    }
    private String inspectSensitive(AccessibilityNodeInfo node, int[] inspected, int depth) {
        if (++inspected[0] > 400 || depth > 30 || !node.isVisibleToUser()) return "";
        if (node.isPassword()) return "密码或身份认证需要你亲自完成";
        String text = (value(node.getText(), 500) + " " + value(node.getContentDescription(), 500)).toLowerCase(Locale.ROOT);
        if (text.matches("(?s).*(确认支付|确认付款|立即支付|立即付款|支付密码|付款码|收付款|指纹验证|人脸验证|身份验证|身份认证|验证码|安全验证|确认授权|登录密码|confirm payment|pay now|payment confirmation|authenticate|biometric|verification code|sign in|log in).*")) return "支付或身份认证需要你亲自确认";
        for (int i = 0; i < Math.min(node.getChildCount(), 200); i++) {
            AccessibilityNodeInfo child = node.getChild(i);
            if (child == null) continue;
            String found;
            try { found = inspectSensitive(child, inspected, depth + 1); } finally { child.recycle(); }
            if (!found.isEmpty()) return found;
        }
        return "";
    }

    private void screenshot(String id, Completion callback) {
        if (Build.VERSION.SDK_INT < 30) { callback.done(failure(id, "unsupported", "截图需要Android 11或更高版本")); return; }
        String capturePackage = foregroundPackage();
        takeScreenshot(Display.DEFAULT_DISPLAY, screenshots, new TakeScreenshotCallback() {
            @Override public void onSuccess(ScreenshotResult result) {
                HardwareBuffer buffer = result.getHardwareBuffer();
                Bitmap bitmap = null;
                Bitmap copy = null;
                try {
                    if (DeviceState.locked(DeviceAccessibilityService.this) || !DeviceState.enabled(DeviceAccessibilityService.this)) { callback.done(paused(id)); return; }
                    if (!capturePackage.equals(foregroundPackage())) { callback.done(failure(id, "foreground_changed", "截图时前台应用已变化，请重新读取屏幕")); return; }
                    String sensitive = sensitiveReason();
                    if (!sensitive.isEmpty()) { DeviceState.requireUser(DeviceAccessibilityService.this, sensitive); callback.done(failure(id, "needs_user", sensitive)); return; }
                    bitmap = Bitmap.wrapHardwareBuffer(buffer, result.getColorSpace());
                    if (bitmap == null) { callback.done(failure(id, "screenshot_failed", "无法读取系统截图")); return; }
                    copy = bitmap.copy(Bitmap.Config.ARGB_8888, false);
                    ByteArrayOutputStream png = new ByteArrayOutputStream();
                    copy.compress(Bitmap.CompressFormat.PNG, 100, png);
                    if (png.size() > 12 * 1024 * 1024) { callback.done(failure(id, "screenshot_too_large", "屏幕截图超过12MB限制")); return; }
                    JSONObject image = new JSONObject().put("mimeType", "image/png").put("data", Base64.encodeToString(png.toByteArray(), Base64.NO_WRAP)).put("width", copy.getWidth()).put("height", copy.getHeight());
                    if (DeviceState.locked(DeviceAccessibilityService.this) || !DeviceState.enabled(DeviceAccessibilityService.this)) { callback.done(paused(id)); return; }
                    callback.done(success(id, new JSONObject().put("image", image).put("packageName", capturePackage)));
                } catch (Exception ignored) { callback.done(failure(id, "screenshot_failed", "截图处理失败")); }
                finally { if (copy != null) copy.recycle(); if (bitmap != null) bitmap.recycle(); buffer.close(); }
            }
            @Override public void onFailure(int code) {
                callback.done(failure(id, code == ERROR_TAKE_SCREENSHOT_SECURE_WINDOW ? "secure_window" : "screenshot_failed", code == ERROR_TAKE_SCREENSHOT_SECURE_WINDOW ? "当前应用禁止截图，请亲自查看；达序不会绕过保护" : "系统未提供截图（错误" + code + "），请稍后重新读取屏幕"));
            }
        });
    }
}
