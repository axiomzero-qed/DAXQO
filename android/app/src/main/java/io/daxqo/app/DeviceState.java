package io.daxqo.app;

import android.app.KeyguardManager;
import android.content.Context;
import android.content.SharedPreferences;
import android.os.PowerManager;
import org.json.JSONObject;

final class DeviceState {
    static final String PREFS = "device_control_private";
    private DeviceState() {}
    static SharedPreferences prefs(Context context) { return context.getSharedPreferences(PREFS, Context.MODE_PRIVATE); }
    static boolean enabled(Context context) { return prefs(context).getBoolean("enabled", false); }
    static String needsUser(Context context) { return prefs(context).getString("needs_user", ""); }
    static void setEnabled(Context context, boolean enabled) {
        SharedPreferences.Editor edit = prefs(context).edit().putBoolean("enabled", enabled);
        if (enabled) edit.remove("needs_user");
        edit.apply();
        DeviceAccessibilityService service = DeviceAccessibilityService.current;
        if (service != null) service.refreshNotification();
    }
    static void requireUser(Context context, String reason) {
        prefs(context).edit().putBoolean("enabled", false).putString("needs_user", reason).apply();
        DeviceAccessibilityService service = DeviceAccessibilityService.current;
        if (service != null) service.refreshNotification();
    }
    static boolean locked(Context context) {
        KeyguardManager manager = (KeyguardManager) context.getSystemService(Context.KEYGUARD_SERVICE);
        return manager == null || manager.isKeyguardLocked();
    }
    static JSONObject status(Context context) {
        JSONObject result = new JSONObject();
        try {
            PowerManager power = (PowerManager) context.getSystemService(Context.POWER_SERVICE);
            DeviceAccessibilityService service = DeviceAccessibilityService.current;
            result.put("version", "0.4.0");
            result.put("paired", context.getSharedPreferences("pairing_private", Context.MODE_PRIVATE).getString("capability", "").length() >= 32);
            result.put("accessibilityConnected", service != null);
            result.put("controlEnabled", enabled(context));
            result.put("needsUser", needsUser(context));
            result.put("locked", locked(context));
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
