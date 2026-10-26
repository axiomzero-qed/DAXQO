package io.daxqo.app;

import android.app.Activity;
import android.app.Instrumentation;
import android.content.Context;
import android.content.Intent;
import android.os.Bundle;
import android.os.SystemClock;
import android.view.View;
import android.view.ViewGroup;
import android.view.accessibility.AccessibilityNodeInfo;
import android.widget.Switch;
import org.json.JSONArray;
import org.json.JSONObject;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.UUID;

/** Instrumentation only: test fixtures have no account and never launch WeChat. */
final class WechatGateAcceptance {
    private static final String PROTECTED = "com.tencent.mm.daxqofixture";
    private static final String PLAIN = "io.daxqo.wechatentryfixture";
    private final Instrumentation test;
    private final Context target;
    private final JSONArray checks = new JSONArray();
    private String token;

    private WechatGateAcceptance(Instrumentation test) { this.test = test; target = test.getTargetContext(); }

    static void run(Instrumentation test) {
        Bundle result = new Bundle();
        WechatGateAcceptance suite = new WechatGateAcceptance(test);
        boolean enabled = DeviceState.enabled(suite.target);
        String mode = DeviceState.policyMode(suite.target);
        try {
            suite.exercise();
            result.putBoolean("success", true);
        } catch (Throwable error) {
            result.putBoolean("success", false);
            result.putString("error", error.getClass().getSimpleName() + ": " + error.getMessage());
        } finally {
            test.runOnMainSync(() -> {
                DeviceState.setWechatAllowed(suite.target, false);
                DeviceState.setPolicyMode(suite.target, mode);
                DeviceState.setEnabled(suite.target, enabled);
            });
            suite.token = null;
            suite.open(targetIntent(suite.target, MainActivity.class));
            result.putBoolean("wechatPermissionLeftOff", !DeviceState.wechatAllowed(suite.target));
            result.putString("checks", suite.checks.toString());
            result.putInt("checkCount", suite.checks.length());
            result.putBoolean("realWechatAccountActions", false);
            test.finish(result.getBoolean("success") ? Activity.RESULT_OK : Activity.RESULT_CANCELED, result);
        }
    }

    private void exercise() throws Exception {
        open(targetIntent(target, MainActivity.class));
        for (int i = 0; i < 100 && DeviceAccessibilityService.current == null; i++) SystemClock.sleep(100);
        check("accessibility_connected", DeviceAccessibilityService.current != null);
        token = target.getSharedPreferences("pairing_private", 0).getString("capability", "");
        check("default_off_after_upgrade", !DeviceState.wechatAllowed(target));
        test.runOnMainSync(() -> { DeviceState.setEnabled(target, true); DeviceState.setPolicyMode(target, "full"); });
        denied("direct_open_real_package_off", action("open_app", new JSONObject().put("packageName", "com.tencent.mm")));
        denied("forged_consent_arguments", action("open_app", new JSONObject().put("packageName", PROTECTED).put("wechatAllowed", true).put("policyMode", "full")));

        openPackage(PROTECTED);
        String[] actions = {"tree", "screenshot", "tap", "long_press", "swipe", "tap_node", "scroll_node", "input", "back", "home", "wake", "open_app"};
        for (String name : actions) denied("protected_full_" + name, action(name, new JSONObject().put("x", 180).put("y", 240)
                .put("x1", 180).put("y1", 700).put("x2", 180).put("y2", 400).put("durationMs", 100)
                .put("nodeId", "0.1").put("snapshotId", "forged").put("text", "MUST_NOT_APPEAR")
                .put("packageName", PLAIN).put("expectedPackage", "forged.package")));
        denied("protected_motor_lease", request("/api/motor/lease", new JSONObject().put("owner", "policy-test").put("expected_package", PROTECTED)));
        check("protected_foreground_unchanged", PROTECTED.equals(DeviceAccessibilityService.current.foregroundPackage()));
        check("remote_setter_absent", request("/api/device/wechat", new JSONObject().put("wechatAllowed", true)).optJSONObject("error") != null);
        check("request_cannot_enable_permission", !DeviceState.wechatAllowed(target));

        // A corrupted preference must fail closed, rather than crash open.
        test.runOnMainSync(() -> DeviceState.prefs(target).edit().putString("wechat_allowed", "true").commit());
        denied("corrupt_preference_closed", action("tree", new JSONObject()));
        test.runOnMainSync(() -> DeviceState.setWechatAllowed(target, false));

        // Only the account-free fixture is operated while permission is on.
        test.runOnMainSync(() -> DeviceState.setWechatAllowed(target, true));
        JSONObject tree = tree();
        JSONObject button = find(tree, "text", "测试点击");
        String replayId = UUID.randomUUID().toString();
        JSONObject replayArgs = new JSONObject().put("nodeId", button.getString("id")).put("snapshotId", tree.getString("snapshotId"));
        check("enabled_fixture_click", call(replayId, "tap_node", replayArgs).optBoolean("ok"));
        tree = tree();
        JSONObject editor = editable(tree);
        check("enabled_fixture_focus", action("tap_node", new JSONObject().put("nodeId", editor.getString("id")).put("snapshotId", tree.getString("snapshotId"))).optBoolean("ok"));
        check("enabled_fixture_input", action("input", new JSONObject().put("text", "DAXQO_FIXTURE_ONLY").put("mode", "replace")).optBoolean("ok"));
        JSONObject lease = request("/api/motor/lease", new JSONObject().put("owner", "policy-test").put("expected_package", PROTECTED));
        check("enabled_fixture_motor_lease", lease.optBoolean("lease_active"));
        test.runOnMainSync(() -> DeviceState.setWechatAllowed(target, false));
        denied("cached_write_replay_after_revoke", call(replayId, "tap_node", replayArgs));
        check("lease_revoked_immediately", !request("/api/motor/status", null).optBoolean("lease_active"));
        denied("old_motor_renewal_denied", request("/api/motor/lease", new JSONObject().put("owner", "policy-test").put("expected_package", PROTECTED)
                .put("lease_id", lease.optString("lease_id")).put("generation", lease.optLong("generation"))));

        openPackage(PLAIN);
        tree = tree();
        JSONObject icon = findSuffix(tree, "viewId", ":id/wechat_entry_icon");
        JSONObject parent = findSuffix(tree, "viewId", ":id/wechat_entry_row");
        denied("plain_parent_label_node", action("tap_node", new JSONObject().put("nodeId", icon.getString("id")).put("snapshotId", tree.getString("snapshotId"))));
        denied("plain_parent_label_coordinate", action("tap", point(icon)));
        JSONObject space = point(parent); space.put("x", parent.getJSONArray("bounds").getInt(2) - 5);
        denied("plain_parent_row_blank_coordinate", action("tap", space));
        button = find(tree, "text", "测试点击");
        check("non_wechat_click_still_works", action("tap_node", new JSONObject().put("nodeId", button.getString("id")).put("snapshotId", tree.getString("snapshotId"))).optBoolean("ok"));

        // Two owner-screen instances must not clear each other's visibility.
        Activity ownerA = test.startActivitySync(targetIntent(target, ControlActivity.class));
        Activity ownerB = test.startActivitySync(targetIntent(target, ControlActivity.class).addFlags(Intent.FLAG_ACTIVITY_MULTIPLE_TASK));
        test.waitForIdleSync();
        check("two_owner_instances_still_protected", ControlActivity.visible && ownerA != ownerB);
        JSONObject remote = action("tap", new JSONObject().put("x", 900).put("y", 600));
        check("owner_ui_remote_write_denied", !remote.optBoolean("ok"));
        final Switch[] setting = new Switch[1];
        test.runOnMainSync(() -> { setting[0] = findSwitch(ownerB.getWindow().getDecorView()); setting[0].performClick(); });
        SystemClock.sleep(400);
        AccessibilityNodeInfo dialog = test.getUiAutomation().getRootInActiveWindow();
        check("ban_warning_visible", hasText(dialog, "严肃提醒：可能导致微信封号"));
        AccessibilityNodeInfo yes = byId(dialog, "android:id/button1");
        check("enable_requires_acknowledgement", yes != null && !yes.isEnabled());
        if (yes != null) yes.recycle();
        dialog.recycle();
        remote = action("tap", new JSONObject().put("x", 900).put("y", 1800));
        check("risk_dialog_remote_write_denied", !remote.optBoolean("ok") && !DeviceState.wechatAllowed(target));
        dialog = test.getUiAutomation().getRootInActiveWindow();
        AccessibilityNodeInfo cancel = byId(dialog, "android:id/button2");
        check("cancel_warning", cancel != null && cancel.performAction(AccessibilityNodeInfo.ACTION_CLICK));
        if (cancel != null) cancel.recycle(); dialog.recycle();
        check("cancel_keeps_off", !DeviceState.wechatAllowed(target));
        test.runOnMainSync(() -> { ownerB.finish(); ownerA.finish(); });
        open(targetIntent(target, MainActivity.class));
        check("final_off_status", !request("/api/device/status", null).optBoolean("wechatAllowed", true));
    }

    private static Intent targetIntent(Context context, Class<?> activity) { return new Intent(context, activity).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK); }
    private void open(Intent intent) { target.startActivity(intent); SystemClock.sleep(600); }
    private void openPackage(String pkg) throws Exception {
        Intent intent = target.getPackageManager().getLaunchIntentForPackage(pkg);
        if (intent == null) throw new AssertionError("fixture_not_installed: " + pkg);
        open(intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
        for (int i = 0; i < 40; i++) {
            if (pkg.equals(DeviceAccessibilityService.current.foregroundPackage())) { SystemClock.sleep(400); return; }
            SystemClock.sleep(100);
        }
        throw new AssertionError("fixture_not_foreground");
    }
    private JSONObject action(String name, JSONObject args) throws Exception { return call(UUID.randomUUID().toString(), name, args); }
    private JSONObject call(String id, String name, JSONObject args) throws Exception { return request("/api/device/execute", new JSONObject().put("id", id).put("action", name).put("args", args)); }
    private JSONObject tree() throws Exception {
        JSONObject result = action("tree", new JSONObject());
        if (!result.optBoolean("ok")) throw new AssertionError("tree: " + result.optJSONObject("error"));
        return result.getJSONObject("result");
    }
    private JSONObject request(String path, JSONObject body) throws Exception {
        HttpURLConnection connection = (HttpURLConnection) new URL("http://127.0.0.1:18883" + path).openConnection();
        connection.setConnectTimeout(5000); connection.setReadTimeout(15000);
        connection.setRequestProperty("Authorization", "Bearer " + token);
        if (body != null) {
            connection.setRequestMethod("POST"); connection.setDoOutput(true); connection.setRequestProperty("Content-Type", "application/json");
            byte[] bytes = body.toString().getBytes(StandardCharsets.UTF_8); connection.setFixedLengthStreamingMode(bytes.length);
            try (java.io.OutputStream out = connection.getOutputStream()) { out.write(bytes); }
        }
        try (InputStream input = connection.getResponseCode() < 400 ? connection.getInputStream() : connection.getErrorStream(); ByteArrayOutputStream out = new ByteArrayOutputStream()) {
            byte[] bytes = new byte[4096]; int count;
            while ((count = input.read(bytes)) >= 0) { out.write(bytes, 0, count); if (out.size() > 16 * 1024 * 1024) throw new AssertionError("response_budget"); }
            return new JSONObject(out.toString("UTF-8"));
        } finally { connection.disconnect(); }
    }
    private void check(String name, boolean ok) { if (!ok) throw new AssertionError(name); checks.put(name); }
    private void denied(String name, JSONObject response) { check(name, !response.optBoolean("ok") && response.optJSONObject("error") != null && WechatPolicy.BLOCKED.equals(response.optJSONObject("error").optString("code"))); }
    private static JSONObject point(JSONObject node) throws Exception {
        JSONArray b = node.getJSONArray("bounds"); return new JSONObject().put("x", (b.getInt(0) + b.getInt(2)) / 2).put("y", (b.getInt(1) + b.getInt(3)) / 2);
    }
    private static JSONObject find(JSONObject tree, String key, String text) throws Exception {
        JSONArray nodes = tree.getJSONArray("nodes");
        for (int i = 0; i < nodes.length(); i++) if (text.equals(nodes.getJSONObject(i).optString(key))) return nodes.getJSONObject(i);
        throw new AssertionError("fixture_node_missing: " + text);
    }
    private static JSONObject findSuffix(JSONObject tree, String key, String suffix) throws Exception {
        JSONArray nodes = tree.getJSONArray("nodes");
        for (int i = 0; i < nodes.length(); i++) if (nodes.getJSONObject(i).optString(key).endsWith(suffix)) return nodes.getJSONObject(i);
        throw new AssertionError("fixture_resource_missing: " + suffix);
    }
    private static JSONObject editable(JSONObject tree) throws Exception {
        JSONArray nodes = tree.getJSONArray("nodes");
        for (int i = 0; i < nodes.length(); i++) if (nodes.getJSONObject(i).optBoolean("editable")) return nodes.getJSONObject(i);
        throw new AssertionError("fixture_editor_missing");
    }
    private static Switch findSwitch(View view) {
        if (view instanceof Switch) return (Switch) view;
        if (view instanceof ViewGroup) for (int i = 0; i < ((ViewGroup) view).getChildCount(); i++) {
            Switch match = findSwitch(((ViewGroup) view).getChildAt(i)); if (match != null) return match;
        }
        return null;
    }
    private static boolean hasText(AccessibilityNodeInfo root, String text) { List<AccessibilityNodeInfo> nodes = root.findAccessibilityNodeInfosByText(text); boolean found = !nodes.isEmpty(); for (AccessibilityNodeInfo node : nodes) node.recycle(); return found; }
    private static AccessibilityNodeInfo byId(AccessibilityNodeInfo root, String id) { List<AccessibilityNodeInfo> nodes = root.findAccessibilityNodeInfosByViewId(id); if (nodes.isEmpty()) return null; AccessibilityNodeInfo first = nodes.remove(0); for (AccessibilityNodeInfo node : nodes) node.recycle(); return first; }
}
