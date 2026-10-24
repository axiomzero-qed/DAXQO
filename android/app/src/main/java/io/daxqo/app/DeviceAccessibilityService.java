package io.daxqo.app;

import android.accessibilityservice.AccessibilityService;
import android.accessibilityservice.AccessibilityServiceInfo;
import android.accessibilityservice.GestureDescription;
import android.accessibilityservice.InputMethod;
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
import android.view.inputmethod.EditorInfo;
import android.text.InputType;
import org.json.JSONArray;
import org.json.JSONObject;
import java.io.ByteArrayOutputStream;
import java.util.Locale;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Map;
import java.util.HashMap;
import java.security.MessageDigest;
import java.nio.charset.StandardCharsets;
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
    private NodeSnapshot nodeSnapshot;
    private long snapshotSequence;
    volatile String lastAction = "";
    volatile String lastError = "";

    @Override protected void onServiceConnected() {
        super.onServiceConnected();
        if (Build.VERSION.SDK_INT >= 33) {
            AccessibilityServiceInfo info = getServiceInfo();
            info.flags |= AccessibilityServiceInfo.FLAG_INPUT_METHOD_EDITOR;
            setServiceInfo(info);
        }
        DeviceState.approvals.revoke();
        current = this;
        bridge = new DeviceHttpBridge(this);
        bridge.start();
        refreshNotification();
    }

    @Override public void onAccessibilityEvent(AccessibilityEvent event) {
        // Browsing Settings or the notification shade is a user's ordinary
        // interaction, not an agent action. Passive events must not revoke or
        // restore control. Active operations enforce their own authorization.
    }
    @Override public void onInterrupt() {
        // This callback interrupts accessibility feedback. We provide no such
        // feedback, and it is not a user request to change control permission.
    }
    @Override public void onDestroy() {
        if (current == this) current = null;
        DeviceState.approvals.revoke();
        WakeActivity.finishActive();
        if (bridge != null) bridge.close();
        screenshots.shutdownNow();
        main.removeCallbacksAndMessages(null);
        ((NotificationManager) getSystemService(NOTIFICATION_SERVICE)).cancel(NOTICE_ID);
        super.onDestroy();
    }
    boolean bridgeListening() { return bridge != null && bridge.isListening(); }
    boolean cursorInputMethodSupported() { return Build.VERSION.SDK_INT >= 33 && getInputMethod() != null; }

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
        DeviceState.LockState currentLock = DeviceState.lockState(this);
        if (!currentLock.known || currentLock.locked) { callback.done(lockedFailure(id, currentLock)); return; }
        PowerManager power = (PowerManager) getSystemService(POWER_SERVICE);
        if (power == null || !power.isInteractive()) { callback.done(failure(id, "screen_off", "屏幕已关闭，请先唤醒手机")); return; }
        if (!DeviceState.enabled(this) || !DeviceState.needsUser(this).isEmpty()) { callback.done(paused(id)); return; }
        String expected = args.optString("expectedPackage", "");
        String actual = foregroundPackage();
        if (!expected.isEmpty() && !expected.equals(actual)) { callback.done(failure(id, "foreground_changed", "前台应用已变化，请重新读取屏幕")); return; }
        boolean readOnly = DeviceState.isReadOnly(action);
        if (!readOnly && ControlActivity.visible) { callback.done(failure(id, "control_ui_local_only", "本机控制页只接受你亲自操作，请返回目标应用后重试")); return; }
        if (!readOnly && ownerConfirmationUi()) { callback.done(failure(id, "owner_confirmation_ui", "当前是你确认操作的界面，手机工具不能自行点击批准；请由你确认后继续")); return; }
        if (!readOnly && ("tap".equals(action) || "long_press".equals(action) || "swipe".equals(action) || "back".equals(action) || "home".equals(action))) {
            JSONObject blocked = authorizeMutation(id, action, args, sensitiveReason());
            if (blocked != null) { callback.done(blocked); return; }
        }
        switch (action) {
            case "tree": callback.done(success(id, tree())); return;
            case "screenshot": screenshot(id, callback); return;
            case "tap": case "long_press": case "swipe": gesture(id, action, args, callback); return;
            case "tap_node": tapNode(id, args, callback); return;
            case "input": input(id, args, callback); return;
            case "back": case "home":
                boolean performed = performGlobalAction("back".equals(action) ? GLOBAL_ACTION_BACK : GLOBAL_ACTION_HOME);
                callback.done(performed ? success(id, new JSONObject().put("performed", true)) : failure(id, "action_failed", "系统未执行此操作"));
                return;
            case "open_app":
                String packageName = args.optString("packageName", "");
                if (!packageName.matches("[A-Za-z0-9_]+(\\.[A-Za-z0-9_]+)+") || packageName.length() > 200) { callback.done(failure(id, "invalid_args", "请提供有效应用包名")); return; }
                Intent launch = getPackageManager().getLaunchIntentForPackage(packageName);
                if (launch == null) { callback.done(failure(id, "app_not_found", "应用未安装或没有可用的桌面入口")); return; }
                String reason = sensitivePackage(packageName) ? "打开系统授权或设置应用" : sensitiveReason();
                JSONObject blocked = authorizeMutation(id, action, args, reason);
                if (blocked != null) { callback.done(blocked); return; }
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

    JSONObject decideApproval(String approvalId, String decision) {
        CountDownLatch finished = new CountDownLatch(1);
        AtomicBoolean cancelled = new AtomicBoolean(false);
        JSONObject[] result = new JSONObject[1];
        main.post(() -> {
            if (cancelled.get()) return;
            result[0] = decideApprovalMain(approvalId, decision);
            finished.countDown();
        });
        try { if (finished.await(3, TimeUnit.SECONDS)) return result[0]; }
        catch (InterruptedException ignored) { Thread.currentThread().interrupt(); }
        cancelled.set(true);
        return failure(approvalId, "timeout", "确认响应超时，请重新核对审批状态");
    }

    // Both GUI decisions and the authenticated human-confirmation endpoint use
    // this main-thread path. A decision never starts or resumes phone control.
    JSONObject decideApprovalMain(String approvalId, String decision) {
        if (!"accept".equals(decision) && !"decline".equals(decision)) return failure(approvalId, "invalid_args", "请选择批准或拒绝");
        if (current != this) return failure(approvalId, "accessibility_unavailable", "无障碍服务未连接");
        if (!DeviceState.mayRecordApproval(DeviceState.enabled(this), !DeviceState.needsUser(this).isEmpty())) return paused(approvalId);
        // A remote owner may consent while the phone sleeps or is locked. No
        // operation runs here; executeMain/authorizeMutation check the actual
        // lock again before consuming the grant and performing the operation.
        synchronized (DeviceState.approvals) {
            ApprovalLedger.Result result = DeviceState.approvals.decide(approvalId, "accept".equals(decision), android.os.SystemClock.elapsedRealtime());
            if (result.newlyApproved) MainActivity.returnToTargetAfterApproval();
            return approvalResult(approvalId, result, "确认已失效，请等待新的操作确认");
        }
    }

    private JSONObject approvalResult(String requestId, ApprovalLedger.Result result, String message) {
        try {
            if (result.code.isEmpty()) return DeviceState.approvalJson(result.entry);
            JSONObject response = failure(requestId, result.code, message);
            if (result.entry != null) response.getJSONObject("error").put("approval", DeviceState.approvalJson(result.entry));
            return response;
        } catch (Exception ignored) { return failure(requestId, "action_failed", "无法读取操作确认状态"); }
    }

    // A null result authorizes exactly the imminent operation. Full mode skips
    // only DAXQO's sensitivity gate, never Android's actual security checks.
    private JSONObject authorizeMutation(String id, String action, JSONObject args, String reason) throws Exception {
        if (!DeviceState.enabled(this) || !DeviceState.needsUser(this).isEmpty()) return paused(id);
        DeviceState.LockState lock = DeviceState.lockState(this);
        if (!lock.known || lock.locked) return lockedFailure(id, lock);
        if (ControlActivity.visible) return failure(id, "control_ui_local_only", "本机控制页只接受你亲自操作，请返回目标应用后重试");
        if (ownerConfirmationUi()) return failure(id, "owner_confirmation_ui", "当前是你确认操作的界面，手机工具不能自行批准");
        if (!DeviceState.requiresApproval(DeviceState.policyMode(this), action, reason)) return null;
        String page = pageFingerprint("input".equals(action));
        if (page == null) return failure(id, "no_window", "当前没有可绑定的应用界面，请重新读取屏幕");
        Map<String, String> fields = new HashMap<>();
        java.util.Iterator<String> names = args.keys();
        while (names.hasNext()) { String key = names.next(); fields.put(key, canonical(args.get(key))); }
        // Input reaches this method only after its current snapshot/node has
        // been validated. The snapshot is an observation handle, not consent.
        String operation = digest(operationIdentity(action, fields,
                ("input".equals(action) || "tap_node".equals(action)) && !args.optString("nodeId", "").isEmpty()));
        synchronized (DeviceState.approvals) {
            ApprovalLedger.Result result = DeviceState.approvals.check(operation, page, action, foregroundPackage(),
                    actionSummary(action) + "：" + reason + approvalContext(), android.os.SystemClock.elapsedRealtime());
            if (result.code.isEmpty()) return null;
            String message = "action_denied".equals(result.code) ? "你已拒绝这项操作，达序不会执行"
                    : "stale_approval".equals(result.code) ? "操作确认因过期、控制变更或页面变化失效，请重新确认"
                    : "请确认这项操作；批准后达序会重试并自行执行";
            return approvalResult(id, result, message);
        }
    }
    private static String actionSummary(String action) {
        switch (action) {
            case "input": return "输入文本";
            case "tap": return "点击";
            case "tap_node": return "点击选中的界面元素";
            case "swipe": return "滑动";
            case "long_press": return "长按";
            case "open_app": return "打开应用";
            case "back": return "返回";
            case "home": return "返回桌面";
            default: return "手机操作";
        }
    }
    private JSONObject lockedFailure(String id, DeviceState.LockState lock) {
        return failure(id, "locked", !lock.known ? "无法确认锁屏安全状态，请先在手机上核对"
                : lock.secureLock || lock.deviceSecure ? "手机设有安全锁或 SIM 保护，请亲自解锁；达序不会绕过锁屏"
                : "手机仍在无密码滑动锁屏，请先调用 android_action 的 wake 再重试");
    }
    static String operationIdentity(String action, Map<String, String> canonicalFields, boolean validatedNodeInput) {
        ArrayList<String> keys = new ArrayList<>(canonicalFields.keySet());
        Collections.sort(keys);
        StringBuilder identity = new StringBuilder(action).append('\n');
        for (String key : keys) {
            if (("input".equals(action) || "tap_node".equals(action)) && validatedNodeInput && "snapshotId".equals(key)) continue;
            String field = canonicalFields.get(key);
            identity.append(key.length()).append(':').append(key).append(field.length()).append(':').append(field);
        }
        return identity.toString();
    }
    private boolean ownerConfirmationUi() {
        AccessibilityNodeInfo root = getRootInActiveWindow();
        if (root == null) return false;
        try {
            return getPackageName().equals(value(root.getPackageName(), 200)) && containsOwnerConfirmation(root, new int[] {0}, 0);
        } finally { root.recycle(); }
    }
    private boolean containsOwnerConfirmation(AccessibilityNodeInfo node, int[] count, int depth) {
        if (++count[0] > 400 || depth > 30 || !node.isVisibleToUser()) return false;
        if (!node.isPassword() && (node.isClickable() || value(node.getClassName(), 200).endsWith("Button"))
                && ("批准一次".equals(value(node.getText(), 100).trim()) || "批准一次".equals(value(node.getContentDescription(), 100).trim()))) return true;
        for (int i = 0; i < Math.min(node.getChildCount(), 200); i++) {
            AccessibilityNodeInfo child = node.getChild(i);
            if (child == null) continue;
            boolean found;
            try { found = containsOwnerConfirmation(child, count, depth + 1); } finally { child.recycle(); }
            if (found) return true;
        }
        return false;
    }
    private static String canonical(Object value) throws Exception {
        if (value instanceof JSONObject) {
            JSONObject object = (JSONObject) value;
            ArrayList<String> keys = new ArrayList<>();
            java.util.Iterator<String> iterator = object.keys();
            while (iterator.hasNext()) keys.add(iterator.next());
            Collections.sort(keys);
            StringBuilder text = new StringBuilder("{");
            for (String key : keys) text.append(JSONObject.quote(key)).append(':').append(canonical(object.get(key))).append(',');
            return text.append('}').toString();
        }
        if (value instanceof JSONArray) {
            JSONArray array = (JSONArray) value;
            StringBuilder text = new StringBuilder("[");
            for (int i = 0; i < array.length(); i++) text.append(canonical(array.get(i))).append(',');
            return text.append(']').toString();
        }
        return value instanceof String ? JSONObject.quote((String) value) : String.valueOf(value);
    }
    private static String digest(String text) throws Exception {
        return Base64.encodeToString(MessageDigest.getInstance("SHA-256").digest(text.getBytes(StandardCharsets.UTF_8)), Base64.NO_WRAP);
    }
    private String pageFingerprint(boolean bindInputFocus) throws Exception {
        AccessibilityNodeInfo root = getRootInActiveWindow();
        if (root == null) return null;
        try {
            JSONArray nodes = new JSONArray();
            fingerprintNode(root, "0", nodes, new int[] {0}, 0, bindInputFocus);
            if (bindInputFocus && Build.VERSION.SDK_INT >= 33 && getInputMethod() != null) {
                EditorInfo editor = getInputMethod().getCurrentInputEditorInfo();
                if (editor != null) nodes.put(new JSONObject().put("editorPackage", editor.packageName)
                        .put("editorField", editor.fieldId).put("editorType", editor.inputType));
            }
            // Android may recreate the same app window after keyguard/wake.
            // Its runtime window id is not a page identity; retain all of the
            // package, visible content, controls and state binding instead.
            return digest(pageIdentity(value(root.getPackageName(), 200), nodes.toString()));
        } finally { root.recycle(); }
    }
    static String pageIdentity(String packageName, String visibleContent) { return packageName + "\n" + visibleContent; }
    private void fingerprintNode(AccessibilityNodeInfo node, String path, JSONArray nodes, int[] count, int depth, boolean bindInputFocus) throws Exception {
        if (++count[0] > 400 || depth > 30 || !node.isVisibleToUser()) return;
        String viewId = value(node.getViewIdResourceName(), 250);
        // System clocks and countdowns must not invalidate an otherwise stable
        // consent. Other visible labels and control state remain bound.
        if (viewId.toLowerCase(Locale.ROOT).matches(".*/(clock|date|time|timer|countdown)(?:_.*)?")) return;
        Rect bounds = new Rect(); node.getBoundsInScreen(bounds);
        String text = node.isPassword() ? "[password]" : value(node.getText(), 500);
        String description = node.isPassword() ? "[password]" : value(node.getContentDescription(), 500);
        JSONObject item = new JSONObject().put("path", path).put("id", viewId).put("class", value(node.getClassName(), 200))
                .put("bounds", bounds.flattenToString()).put("text", stableLabel(text)).put("description", stableLabel(description))
                .put("clickable", node.isClickable()).put("editable", node.isEditable()).put("enabled", node.isEnabled())
                .put("checked", node.isChecked()).put("selected", node.isSelected()).put("password", node.isPassword());
        if (bindInputFocus) item.put("inputFocused", node.isFocused())
                .put("selectionStart", node.getTextSelectionStart()).put("selectionEnd", node.getTextSelectionEnd());
        nodes.put(item);
        for (int i = 0; i < Math.min(node.getChildCount(), 200) && count[0] < 400; i++) {
            AccessibilityNodeInfo child = node.getChild(i);
            if (child == null) continue;
            try { fingerprintNode(child, path + "." + i, nodes, count, depth + 1, bindInputFocus); } finally { child.recycle(); }
        }
    }
    static String stableLabel(String label) {
        return label.replaceAll("(?<![0-9])[0-9]{1,2}:[0-9]{2}(?::[0-9]{2})?(?![0-9])", "[clock]")
                .replaceAll("(?i)(?<![0-9])[0-9]+\\s*(秒后|秒剩余|seconds? remaining|s remaining)", "[countdown]");
    }
    private String approvalContext() {
        AccessibilityNodeInfo root = getRootInActiveWindow();
        if (root == null) return "";
        try {
            String name = value(root.getPackageName(), 200);
            if (!(name.contains("packageinstaller") || name.contains("permissioncontroller"))) return "";
            return findAppLabel(root, new int[] {0}, 0);
        } finally { root.recycle(); }
    }
    private String findAppLabel(AccessibilityNodeInfo node, int[] count, int depth) {
        if (++count[0] > 400 || depth > 30) return "";
        String id = value(node.getViewIdResourceName(), 250);
        if (!node.isPassword() && node.isVisibleToUser() && (id.endsWith("/app_name") || id.endsWith("/app_label"))) {
            String label = value(node.getText(), 60).replace('\n', ' ').trim();
            if (!label.isEmpty()) return "（应用：" + label + "）";
        }
        for (int i = 0; i < Math.min(node.getChildCount(), 200); i++) {
            AccessibilityNodeInfo child = node.getChild(i);
            if (child == null) continue;
            String label;
            try { label = findAppLabel(child, count, depth + 1); } finally { child.recycle(); }
            if (!label.isEmpty()) return label;
        }
        return "";
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

    @android.annotation.SuppressLint("NewApi") // ime is non-null only on API 33+ below.
    private void input(String id, JSONObject args, Completion callback) throws Exception {
        Object raw = args.opt("text");
        Object rawMode = args.has("mode") ? args.opt("mode") : "insert";
        if (!(raw instanceof String) || !TextInputEdit.validText((String) raw)
                || !(rawMode instanceof String) || !("insert".equals(rawMode) || "replace".equals(rawMode))) {
            callback.done(failure(id, "invalid_args", "请输入有效Unicode文本（最多8000个UTF-16单元），mode须为insert或replace")); return;
        }
        String mode = (String) rawMode;
        args.put("mode", mode);
        String nodeId = args.optString("nodeId", "");
        if ((args.has("nodeId") || args.has("snapshotId")) && (!(args.opt("nodeId") instanceof String)
                || nodeId.isEmpty() || nodeId.length() > 255 || !(args.opt("snapshotId") instanceof String)
                || args.optString("snapshotId").isEmpty() || args.optString("snapshotId").length() > 255)) {
            callback.done(failure(id, "invalid_args", "指定输入节点时需要有效nodeId和snapshotId")); return;
        }
        AccessibilityNodeInfo root = getRootInActiveWindow();
        if (root == null) { callback.done(failure(id, "no_window", "当前应用没有可用界面")); return; }
        AccessibilityNodeInfo node = null;
        try {
            if (nodeId.isEmpty()) node = root.findFocus(AccessibilityNodeInfo.FOCUS_INPUT);
            else {
                if (!snapshotId.equals(args.optString("snapshotId", "")) || !snapshotPackage.equals(foregroundPackage())) { callback.done(failure(id, "stale_snapshot", "界面快照已失效，请重新读取UI树")); return; }
                node = nodeAt(root, nodeId);
                if (nodeSnapshot == null || node == null || verifiedTarget(node, nodeId, args.optString("snapshotId"),
                        foregroundPackage(), screenSize()) == null) {
                    callback.done(failure(id, "stale_snapshot", "输入目标已变化，请重新读取UI树")); return;
                }
            }
            String packageName = value(root.getPackageName(), 200);
            if (node != null && (!node.isEnabled() || !node.isVisibleToUser())) {
                callback.done(failure(id, "no_input", "目标输入框不可用，请重新观察")); return;
            }
            InputMethod ime = Build.VERSION.SDK_INT >= 33 ? getInputMethod() : null;
            EditorInfo editor = ime == null ? null : ime.getCurrentInputEditorInfo();
            InputMethod.AccessibilityInputConnection connection = ime == null ? null : ime.getCurrentInputConnection();
            // A virtual editor may expose a valid input connection without an
            // editable accessibility node (e.g. WebView/contenteditable).
            boolean direct = "insert".equals(mode) && ime != null && ime.getCurrentInputStarted()
                    && connection != null && editor != null && packageName.equals(editor.packageName)
                    && editor.inputType != InputType.TYPE_NULL && (node == null ? nodeId.isEmpty() : node.isFocused());
            if (!direct && (node == null || !node.isEditable() || !node.isEnabled() || !node.isVisibleToUser())) {
                callback.done(failure(id, "no_input", "未找到可写入的编辑器，请先点击目标输入框并重新观察；不要逐键拼音输入")); return;
            }
            JSONObject blocked = authorizeMutation(id, "input", args,
                    (node != null && node.isPassword()) || editorPassword(editor) ? "向密码输入框填入文本" : sensitiveReason());
            if (blocked != null) { callback.done(blocked); return; }
            if (!packageName.equals(foregroundPackage())) { callback.done(failure(id, "foreground_changed", "输入时前台应用已变化，请重新观察")); return; }
            if (direct) {
                if (ime.getCurrentInputEditorInfo() != editor || !ime.getCurrentInputStarted()
                        || (node != null && (!node.refresh() || !node.isFocused()))) {
                    callback.done(failure(id, "input_focus_changed", "输入焦点已变化，未提交文本；请重新观察")); return;
                }
                // commitText replaces an active composing span/selection and
                // accepts full Unicode without switching IME or a clipboard.
                // This API is asynchronous: never fall back after dispatch,
                // which could type the same text twice. Observe the result.
                connection.commitText((String) raw, 1, null);
                invalidateSnapshot();
                callback.done(success(id, new JSONObject().put("submitted", true).put("mode", mode)
                        .put("method", "input_connection").put("verificationRequired", true)));
                return;
            }
            if ("insert".equals(mode) && node.isPassword()) {
                callback.done(failure(id, "input_unavailable", "当前密码框无法安全保留原文插入，请使用支持光标输入的系统或明确选择整框替换")); return;
            }
            TextInputEdit edit;
            try {
                edit = TextInputEdit.plan(node.getText() == null ? null : node.getText().toString(),
                        node.getTextSelectionStart(), node.getTextSelectionEnd(), (String) raw, mode);
            } catch (IllegalArgumentException unavailable) {
                callback.done(failure(id, "selection_unavailable", "无法确认当前光标或选区，未改写原文；请先点击输入框并重新观察")); return;
            }
            Bundle value = new Bundle(); value.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, edit.text);
            if (!node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, value)) {
                callback.done(failure(id, "input_unavailable", "应用未接受直接文本输入，请重新观察目标；不要逐键点按键盘")); return;
            }
            Bundle selection = new Bundle();
            selection.putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_START_INT, edit.cursor);
            selection.putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_END_INT, edit.cursor);
            boolean cursorSet = node.performAction(AccessibilityNodeInfo.ACTION_SET_SELECTION, selection);
            invalidateSnapshot();
            callback.done(success(id, new JSONObject().put("performed", true).put("mode", mode)
                    .put("method", "accessibility_set_text").put("cursorSet", cursorSet).put("verificationRequired", true)));
        } finally { if (node != null) node.recycle(); root.recycle(); }
    }
    private void invalidateSnapshot() { nodeSnapshot = null; snapshotId = ""; snapshotPackage = ""; }
    private static boolean editorPassword(EditorInfo editor) {
        if (editor == null) return false;
        int type = editor.inputType & InputType.TYPE_MASK_CLASS, variation = editor.inputType & InputType.TYPE_MASK_VARIATION;
        return type == InputType.TYPE_CLASS_NUMBER && variation == InputType.TYPE_NUMBER_VARIATION_PASSWORD
                || type == InputType.TYPE_CLASS_TEXT && (variation == InputType.TYPE_TEXT_VARIATION_PASSWORD
                || variation == InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD || variation == InputType.TYPE_TEXT_VARIATION_WEB_PASSWORD);
    }
    private NodeSnapshot captureNodes(String ident, String packageName, JSONArray nodes) {
        ArrayList<NodeSnapshot.Node> items = new ArrayList<>();
        for (int i = 0; i < nodes.length(); i++) {
            JSONObject n = nodes.optJSONObject(i);
            if (n == null) continue;
            JSONArray b = n.optJSONArray("bounds");
            if (b == null || b.length() != 4) continue;
            items.add(new NodeSnapshot.Node(n.optString("id"), n.optString("text"), n.optString("description"),
                    n.optString("viewId"), n.optString("className"), b.optInt(0), b.optInt(1), b.optInt(2), b.optInt(3),
                    n.optBoolean("visible"), n.optBoolean("enabled"), n.optBoolean("password")));
        }
        return new NodeSnapshot(ident, packageName, items);
    }
    private NodeSnapshot.Node verifiedTarget(AccessibilityNodeInfo node, String nodeId, String observed,
                                            String actual, int[] size) throws Exception {
        AccessibilityNodeInfo anchor = AccessibilityNodeInfo.obtain(node);
        String path = nodeId;
        try {
            for (int level = 0; level < 2 && path.contains("."); level++) {
                AccessibilityNodeInfo parent = anchor.getParent();
                if (parent == null) return null;
                anchor.recycle(); anchor = parent;
                path = path.substring(0, path.lastIndexOf('.'));
            }
            JSONArray fresh = new JSONArray();
            appendNode(anchor, path, "", fresh, 0, new boolean[] {false});
            return nodeSnapshot.match(observed, nodeId, captureNodes("", actual, fresh), size[0], size[1]);
        } finally { anchor.recycle(); }
    }
    private void tapNode(String id, JSONObject args, Completion callback) throws Exception {
        String nodeId = args.optString("nodeId", ""), observed = args.optString("snapshotId", "");
        if (nodeId.isEmpty() || observed.isEmpty() || nodeId.length() > 255 || observed.length() > 255
                || args.has("x") || args.has("y") || args.has("x1") || args.has("y1") || args.has("x2") || args.has("y2")) {
            callback.done(failure(id, "invalid_args", "元素点击需要nodeId和snapshotId，不接受坐标")); return;
        }
        if (nodeSnapshot == null || !snapshotId.equals(observed)) {
            callback.done(failure(id, "stale_snapshot", "界面快照已失效，请重新读取后选择元素")); return;
        }
        AccessibilityNodeInfo root = getRootInActiveWindow();
        AccessibilityNodeInfo targetNode = null, click = null;
        if (root == null) { callback.done(failure(id, "no_window", "当前应用没有可用界面")); return; }
        try {
            String actual = value(root.getPackageName(), 200);
            int[] size = screenSize();
            targetNode = nodeAt(root, nodeId);
            NodeSnapshot.Node target = targetNode == null ? null : verifiedTarget(targetNode, nodeId, observed, actual, size);
            if (target == null) {
                callback.done(failure(id, "stale_snapshot", "目标内容或所在列表已变化，请重新读取界面；未点击")); return;
            }
            JSONObject blocked = authorizeMutation(id, "tap_node", args, sensitiveReason());
            if (blocked != null) { callback.done(blocked); return; }
            // Consent checks can query another window. Recheck this same live
            // widget and its row before either dispatch method; never resolve a
            // reused child path and silently accept its new occupant.
            target = verifiedTarget(targetNode, nodeId, observed, foregroundPackage(), size);
            if (target == null) {
                callback.done(failure(id, "stale_snapshot", "目标在检查期间已变化，请重新读取界面；未点击")); return;
            }
            double x = (Math.max(0, target.left) + Math.min(size[0], target.right)) / 2.0;
            double y = (Math.max(0, target.top) + Math.min(size[1], target.bottom)) / 2.0;
            click = AccessibilityNodeInfo.obtain(targetNode);
            for (int level = 0; click != null && level < 3; level++) {
                if (click.isClickable() && click.isVisibleToUser() && click.isEnabled()) {
                    if (click.performAction(AccessibilityNodeInfo.ACTION_CLICK)) {
                        nodeSnapshot = null; snapshotId = ""; snapshotPackage = "";
                        callback.done(success(id, new JSONObject().put("nodeId", nodeId).put("snapshotId", observed)
                                .put("method", "accessibility").put("x", x).put("y", y)));
                    } else callback.done(failure(id, "action_failed", "应用未接受元素点击，请重新读取界面"));
                    return;
                }
                AccessibilityNodeInfo parent = click.getParent();
                click.recycle(); click = parent;
            }
            gesture(id, "tap", new JSONObject().put("x", x).put("y", y), response -> {
                if (response.optBoolean("ok")) {
                    try { response.getJSONObject("result").put("nodeId", nodeId).put("snapshotId", observed)
                            .put("method", "verified_bounds").put("x", x).put("y", y); } catch (Exception ignored) { }
                    nodeSnapshot = null; snapshotId = ""; snapshotPackage = "";
                }
                callback.done(response);
            });
        } finally {
            if (click != null) click.recycle();
            if (targetNode != null) targetNode.recycle();
            root.recycle();
        }
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
        nodeSnapshot = captureNodes(snapshotId, snapshotPackage, nodes);
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
        item.put("focused", node.isFocused());
        if (!password && node.isEditable()) item.put("selectionStart", node.getTextSelectionStart()).put("selectionEnd", node.getTextSelectionEnd());
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
            if (sensitivePackage(packageName)) return "操作系统授权或安全界面";
            int[] inspected = {0};
            return inspectSensitive(root, inspected, 0);
        } finally { root.recycle(); }
    }
    private String inspectSensitive(AccessibilityNodeInfo node, int[] inspected, int depth) {
        if (++inspected[0] > 400 || depth > 30 || !node.isVisibleToUser()) return "";
        if (node.isPassword()) return "操作密码或身份认证界面";
        String text = (value(node.getText(), 500) + " " + value(node.getContentDescription(), 500)).toLowerCase(Locale.ROOT);
        if (text.matches("(?s).*(确认支付|确认付款|立即支付|立即付款|支付密码|付款码|收付款|指纹验证|人脸验证|身份验证|身份认证|验证码|安全验证|确认授权|登录密码|confirm payment|pay now|payment confirmation|authenticate|biometric|verification code|sign in|log in).*")) return "操作支付或身份认证界面";
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
