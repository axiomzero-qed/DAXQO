package io.daxqo.app;

import android.app.KeyguardManager;
import android.content.Context;
import android.content.SharedPreferences;
import android.os.PowerManager;
import org.json.JSONObject;
import org.json.JSONArray;

final class DeviceState {
    static final String PREFS = "device_control_private";
    static final ApprovalLedger approvals = new ApprovalLedger();
    private DeviceState() {}
    static SharedPreferences prefs(Context context) { return context.getSharedPreferences(PREFS, Context.MODE_PRIVATE); }
    static boolean enabled(Context context) { return prefs(context).getBoolean("enabled", false); }
    static String needsUser(Context context) { return prefs(context).getString("needs_user", ""); }
    static String policyMode(Context context) { return "full".equals(prefs(context).getString("policy_mode", "confirm")) ? "full" : "confirm"; }
    static boolean isReadOnly(String action) { return "status".equals(action) || "tree".equals(action) || "screenshot".equals(action); }
    static boolean requiresApproval(String mode, String action, String reason) {
        return !"full".equals(mode) && !isReadOnly(action) && !reason.isEmpty();
    }
    // Consent records permission, not an attempt to unlock or perform an action.
    static boolean mayRecordApproval(boolean controlEnabled, boolean needsUser) { return controlEnabled && !needsUser; }
    // Called only by the native control screen, never by a bridge operation.
    static void setPolicyMode(Context context, String mode) {
        if (!"confirm".equals(mode) && !"full".equals(mode)) return;
        approvals.revoke();
        prefs(context).edit().putString("policy_mode", mode).apply();
    }
    static JSONObject approvalJson(ApprovalLedger.Entry entry) throws Exception {
        return new JSONObject().put("id", entry.id).put("action", entry.action).put("packageName", entry.packageName)
                .put("summary", entry.summary).put("status", entry.status).put("expiresAt", entry.expiresAt);
    }
    static JSONArray approvalList() {
        JSONArray array = new JSONArray();
        synchronized (approvals) {
            for (ApprovalLedger.Entry entry : approvals.list(android.os.SystemClock.elapsedRealtime())) try { array.put(approvalJson(entry)); } catch (Exception ignored) { }
        }
        return array;
    }
    static void setEnabled(Context context, boolean enabled) {
        approvals.revoke();
        SharedPreferences.Editor edit = prefs(context).edit().putBoolean("enabled", enabled);
        if (enabled) edit.remove("needs_user");
        edit.apply();
        DeviceAccessibilityService service = DeviceAccessibilityService.current;
        if (service != null) service.refreshNotification();
    }
    static void requireUser(Context context, String reason) {
        approvals.revoke();
        prefs(context).edit().putBoolean("enabled", false).putString("needs_user", reason).apply();
        DeviceAccessibilityService service = DeviceAccessibilityService.current;
        if (service != null) service.refreshNotification();
    }
    static boolean locked(Context context) {
        return lockState(context).locked;
    }
    static final class LockState {
        final boolean known, locked, secureLock, deviceSecure;
        LockState(boolean known, boolean locked, boolean secureLock, boolean deviceSecure) {
            this.known = known; this.locked = locked; this.secureLock = secureLock; this.deviceSecure = deviceSecure;
        }
    }
    static LockState lockState(Context context) {
        try {
            KeyguardManager manager = (KeyguardManager) context.getSystemService(Context.KEYGUARD_SERVICE);
            if (manager != null) return new LockState(true, manager.isKeyguardLocked(), manager.isKeyguardSecure(), manager.isDeviceSecure());
        } catch (Exception ignored) { }
        // An unavailable or failing system query must never authorize dismissal.
        return new LockState(false, true, true, true);
    }
    static boolean mayDismissKeyguard(boolean controlEnabled, boolean needsUser, boolean known, boolean locked, boolean secureLock, boolean deviceSecure) {
        return controlEnabled && !needsUser && known && locked && !secureLock && !deviceSecure;
    }
    static boolean mayDismissKeyguard(Context context, LockState state) {
        return mayDismissKeyguard(enabled(context), !needsUser(context).isEmpty(), state.known, state.locked, state.secureLock, state.deviceSecure);
    }
    static JSONObject status(Context context) {
        JSONObject result = new JSONObject();
        try {
            PowerManager power = (PowerManager) context.getSystemService(Context.POWER_SERVICE);
            DeviceAccessibilityService service = DeviceAccessibilityService.current;
            LockState lock = lockState(context);
            result.put("version", "0.4.0");
            result.put("device", new JSONObject().put("manufacturer", android.os.Build.MANUFACTURER)
                    .put("model", android.os.Build.MODEL).put("androidVersion", android.os.Build.VERSION.RELEASE)
                    .put("sdkInt", android.os.Build.VERSION.SDK_INT));
            JSONArray stores = new JSONArray();
            for (String name : new String[] {"com.xiaomi.market", "com.bbk.appstore", "com.vivo.appstore",
                    "com.heytap.market", "com.oppo.market", "com.huawei.appmarket",
                    "com.sec.android.app.samsungapps", "com.android.vending"}) {
                if (context.getPackageManager().getLaunchIntentForPackage(name) != null) stores.put(name);
            }
            result.put("appStores", stores);
            result.put("controlPageVisible", ControlActivity.visible);
            result.put("paired", context.getSharedPreferences("pairing_private", Context.MODE_PRIVATE).getString("capability", "").length() >= 32);
            result.put("accessibilityConnected", service != null);
            result.put("controlEnabled", enabled(context));
            result.put("policyMode", policyMode(context));
            result.put("approvals", approvalList());
            result.put("needsUser", needsUser(context));
            result.put("locked", lock.locked);
            result.put("lockStateKnown", lock.known);
            result.put("secureLock", lock.secureLock);
            result.put("deviceSecure", lock.deviceSecure);
            result.put("screenOn", power != null && power.isInteractive());
            result.put("batteryOptimizationsIgnored", power != null && power.isIgnoringBatteryOptimizations(context.getPackageName()));
            result.put("screenshotSupported", android.os.Build.VERSION.SDK_INT >= 30);
            result.put("bridgeListening", service != null && service.bridgeListening());
            result.put("packageName", service == null ? "" : service.foregroundPackage());
            result.put("lastAction", service == null ? "" : service.lastAction);
            result.put("lastError", service == null ? "" : service.lastError);
        } catch (Exception ignored) { }
        return result;
    }
}
