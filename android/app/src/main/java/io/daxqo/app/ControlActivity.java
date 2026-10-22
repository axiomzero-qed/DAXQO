package io.daxqo.app;

import android.app.Activity;
import android.app.AlertDialog;
import android.app.NotificationManager;
import android.content.Intent;
import android.content.pm.ShortcutInfo;
import android.content.pm.ShortcutManager;
import android.graphics.Color;
import android.graphics.drawable.Icon;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.view.View;
import android.view.WindowInsets;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;
import org.json.JSONObject;

public final class ControlActivity extends Activity {
    static volatile boolean visible;
    static final String ACTION_PAUSE = "io.daxqo.app.PAUSE_CONTROL";
    static final String ACTION_CONTROL = "io.daxqo.app.CONTROL";
    private final Handler main = new Handler(Looper.getMainLooper());
    private TextView status;
    private TextView activity;
    private Button toggle;
    private TextView policy;
    private LinearLayout approvalBox;
    private String shownApprovals = "";
    private final Runnable refresh = new Runnable() {
        @Override public void run() { updateStatus(); main.postDelayed(this, 1000); }
    };

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        getWindow().setStatusBarColor(Color.WHITE);
        getWindow().setNavigationBarColor(Color.WHITE);
        getWindow().getDecorView().setSystemUiVisibility(View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR | View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR);
        if (ACTION_PAUSE.equals(getIntent().getAction())) DeviceState.setEnabled(this, false);
        ScrollView scroll = new ScrollView(this);
        scroll.setFillViewport(true);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(dp(22), dp(18), dp(22), dp(28));
        root.setBackgroundColor(Color.WHITE);
        scroll.addView(root);
        TextView title = text("手机控制", 25, Color.rgb(22, 35, 50));
        root.addView(title);
        root.addView(text("让达序在本机任务中查看和操作手机。你可以随时暂停。", 15, Color.rgb(80, 90, 105)));
        status = text("正在检查…", 15, Color.rgb(37, 99, 235));
        status.setPadding(0, dp(20), 0, dp(10));
        root.addView(status);
        toggle = button(root, "开始手机控制", v -> {
            if (DeviceAccessibilityService.current == null) {
                Toast.makeText(this, "请先在系统设置中启用达序手机控制", Toast.LENGTH_LONG).show();
                openSettings(Settings.ACTION_ACCESSIBILITY_SETTINGS);
            } else if (DeviceState.enabled(this)) DeviceState.setEnabled(this, false);
            else DeviceState.setEnabled(this, true);
            updateStatus();
        });
        policy = text("", 15, Color.rgb(37, 99, 235));
        root.addView(policy);
        root.addView(text("逐项确认：读取界面和普通操作可直接执行；支付、密码和系统授权等敏感操作先等你确认，批准后由达序完成。完整授权：在控制开启期间直接尝试这些操作，系统限制仍然有效。模式只能在本机此页切换。", 14, Color.rgb(80, 90, 105)));
        button(root, "切换授权模式", v -> {
            boolean full = "full".equals(DeviceState.policyMode(this));
            new AlertDialog.Builder(this).setTitle(full ? "切换为逐项确认" : "开启完整授权")
                    .setMessage(full ? "之后的敏感操作会等待你逐项确认。已有确认将失效。" : "控制开启时，达序可直接尝试支付、密码输入和系统授权界面操作。系统实际拒绝仍会返回失败；安全锁屏不会被绕过。你可以随时暂停或切回逐项确认。")
                    .setNegativeButton("取消", null).setPositiveButton("确认切换", (dialog, which) -> {
                        DeviceState.setPolicyMode(this, full ? "confirm" : "full"); updateStatus();
                    }).show();
        });
        approvalBox = new LinearLayout(this);
        approvalBox.setOrientation(LinearLayout.VERTICAL);
        root.addView(approvalBox);
        button(root, "1  开启无障碍服务", v -> openSettings(Settings.ACTION_ACCESSIBILITY_SETTINGS));
        if (Build.VERSION.SDK_INT >= 33) root.addView(text("如系统提示“受限设置”，请在达序应用信息菜单中允许后，再手动开启无障碍服务。", 13, Color.rgb(100, 110, 125)));
        button(root, "2  允许控制状态通知", v -> {
            if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS) != android.content.pm.PackageManager.PERMISSION_GRANTED) requestPermissions(new String[] {android.Manifest.permission.POST_NOTIFICATIONS}, 4002);
            else {
                Intent intent = new Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS).putExtra(Settings.EXTRA_APP_PACKAGE, getPackageName());
                try { startActivity(intent); } catch (Exception ignored) { openAppInfo(); }
            }
        });
        button(root, "3  检查电池与后台设置", v -> openSettings(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS));
        button(root, "添加桌面控制入口", v -> pinShortcut());
        root.addView(text("手机未设置安全密码且没有 SIM 锁时，远程唤醒会尝试通过系统关闭滑动锁屏。安全密码、SIM 保护或无法确认的锁屏状态需要你亲自解锁。审批不会重启已暂停的控制，也不会替代系统要求的授权。", 14, Color.rgb(80, 90, 105)));
        root.addView(text("后台运行受手机系统和电池策略影响。请按需在系统应用信息中允许后台运行，并保持已有 Linux 环境在线。达序不会更改厂商容器或绕过系统权限。", 14, Color.rgb(80, 90, 105)));
        activity = text("", 13, Color.rgb(100, 110, 125));
        activity.setPadding(0, dp(16), 0, dp(8));
        root.addView(activity);
        button(root, "返回任务界面", v -> {
            startActivity(new Intent(this, MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP | Intent.FLAG_ACTIVITY_SINGLE_TOP));
            finish();
        });
        if (Build.VERSION.SDK_INT >= 35) scroll.setOnApplyWindowInsetsListener((view, insets) -> {
            android.graphics.Insets safe = insets.getInsets(WindowInsets.Type.systemBars() | WindowInsets.Type.displayCutout());
            view.setPadding(safe.left, safe.top, safe.right, safe.bottom);
            return insets;
        });
        setContentView(scroll);
    }
    @Override protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        if (ACTION_PAUSE.equals(intent.getAction())) DeviceState.setEnabled(this, false);
        updateStatus();
    }
    @Override protected void onResume() { super.onResume(); main.removeCallbacks(refresh); main.post(refresh); }
    @Override protected void onPause() { main.removeCallbacks(refresh); super.onPause(); }
    @Override protected void onStart() { super.onStart(); visible = true; }
    @Override protected void onStop() { visible = false; super.onStop(); }
    private int dp(int value) { return Math.round(value * getResources().getDisplayMetrics().density); }
    private TextView text(String value, int size, int color) {
        TextView view = new TextView(this); view.setText(value); view.setTextSize(size); view.setTextColor(color);
        view.setPadding(0, dp(6), 0, dp(6)); view.setLineSpacing(dp(3), 1);
        return view;
    }
    private Button button(LinearLayout root, String label, View.OnClickListener listener) {
        Button button = new Button(this); button.setText(label); button.setAllCaps(false); button.setFilterTouchesWhenObscured(true); button.setOnClickListener(listener);
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(-1, -2); params.topMargin = dp(6); params.bottomMargin = dp(4);
        root.addView(button, params); return button;
    }
    private void updateStatus() {
        if (status == null) return;
        JSONObject state = DeviceState.status(this);
        policy.setText("授权模式：" + ("full".equals(state.optString("policyMode")) ? "完整授权" : "逐项确认（默认）"));
        updateApprovals(state.optJSONArray("approvals"));
        String reason = state.optString("needsUser");
        boolean connected = state.optBoolean("accessibilityConnected");
        boolean enabled = state.optBoolean("controlEnabled");
        String accessibility = connected ? "无障碍服务：已连接" : "无障碍服务：未连接，请到系统设置开启";
        String control = !reason.isEmpty() ? "手机控制：保护暂停\n" + reason + (connected ? "\n无障碍授权仍有效，处理后可在此继续。" : "")
                : enabled ? "手机控制：已开启" : "手机控制：已暂停";
        String pairing = state.optBoolean("paired") ? "" : "\n本机服务：待配对，请先返回任务界面连接";
        String notify = ((NotificationManager) getSystemService(NOTIFICATION_SERVICE)).areNotificationsEnabled() ? "已允许" : "未允许";
        String battery = state.optBoolean("batteryOptimizationsIgnored") ? "不受电池优化限制" : "仍受电池优化限制";
        boolean lockKnown = state.optBoolean("lockStateKnown", false);
        boolean secure = state.optBoolean("secureLock", true) || state.optBoolean("deviceSecure", true);
        String lock = !lockKnown ? "锁屏：状态未知，需亲自处理" : secure ? "锁屏：安全锁或 SIM 保护，需亲自解锁" : "锁屏：无安全密码，可尝试远程关闭滑动锁屏";
        status.setText(accessibility + "\n" + control + pairing + "\n" + lock + "\n通知：" + notify + "\n后台：" + battery);
        toggle.setText(!connected ? "开启无障碍服务" : enabled ? "暂停手机控制" : reason.isEmpty() ? "开始手机控制" : "我已处理，继续控制");
        String last = state.optString("lastAction");
        String error = state.optString("lastError");
        activity.setText((last.isEmpty() ? "暂无手机操作" : "最近操作：" + last) + (error.isEmpty() ? "" : "\n" + error) + "\n暂停后不再接受新操作；当前手势最多2秒结束。");
    }
    private void updateApprovals(org.json.JSONArray approvals) {
        String serialized = approvals == null ? "[]" : approvals.toString();
        if (serialized.equals(shownApprovals)) return;
        shownApprovals = serialized;
        approvalBox.removeAllViews();
        if (approvals == null) return;
        for (int i = approvals.length() - 1; i >= 0; i--) {
            JSONObject item = approvals.optJSONObject(i);
            if (item == null || !("pending".equals(item.optString("status")) || "approved".equals(item.optString("status")))) continue;
            approvalBox.addView(text(("pending".equals(item.optString("status")) ? "等待你确认：" : "已批准，等待任务重试：") + item.optString("summary") + "\n应用：" + item.optString("packageName") + "\n确认限本次操作和当前页面，10 分钟内有效。", 14, Color.rgb(80, 90, 105)));
            if ("pending".equals(item.optString("status"))) {
                button(approvalBox, "批准这一次并返回原应用", v -> decide(item.optString("id"), "accept"));
                button(approvalBox, "拒绝这一次", v -> decide(item.optString("id"), "decline"));
            }
            break;
        }
    }
    private void decide(String id, String decision) {
        DeviceAccessibilityService service = DeviceAccessibilityService.current;
        JSONObject result = service == null ? null : service.decideApprovalMain(id, decision);
        if (result != null && ("approved".equals(result.optString("status")) || "declined".equals(result.optString("status")))) {
            Toast.makeText(this, "accept".equals(decision) ? "已批准，达序将重试并执行" : "已拒绝该操作", Toast.LENGTH_SHORT).show();
            if ("accept".equals(decision)) { moveTaskToBack(true); finish(); }
        } else Toast.makeText(this, result == null ? "无障碍服务未连接" : result.optJSONObject("error") == null ? "确认未生效" : result.optJSONObject("error").optString("message"), Toast.LENGTH_LONG).show();
        updateStatus();
    }
    private void openSettings(String action) {
        try { startActivity(new Intent(action)); }
        catch (Exception ignored) { openAppInfo(); }
    }
    private void openAppInfo() {
        try { startActivity(new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, android.net.Uri.parse("package:" + getPackageName()))); }
        catch (Exception ignored) { Toast.makeText(this, "请在系统设置中打开达序应用信息", Toast.LENGTH_LONG).show(); }
    }
    private void pinShortcut() {
        ShortcutManager manager = getSystemService(ShortcutManager.class);
        if (manager == null || !manager.isRequestPinShortcutSupported()) { Toast.makeText(this, "当前桌面不支持添加快捷入口，可长按达序图标使用手机控制", Toast.LENGTH_LONG).show(); return; }
        ShortcutInfo shortcut = new ShortcutInfo.Builder(this, "phone_control")
                .setShortLabel("手机控制")
                .setIcon(Icon.createWithResource(this, R.drawable.ic_launcher_foreground))
                .setIntent(new Intent(this, MainActivity.class).setAction(ACTION_CONTROL))
                .build();
        manager.requestPinShortcut(shortcut, null);
    }
}
