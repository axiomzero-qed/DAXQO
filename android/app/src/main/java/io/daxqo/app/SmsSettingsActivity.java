package io.daxqo.app;

import android.Manifest;
import android.app.Activity;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.os.Bundle;
import android.view.View;
import android.view.WindowInsets;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import org.json.JSONArray;
import org.json.JSONObject;

/** Local, explicit permission and domestic-SIM selection screen. */
public final class SmsSettingsActivity extends Activity {
    private LinearLayout rows;
    private TextView summary;
    private SmsController sms;
    private int dp(int value) { return Math.round(value * getResources().getDisplayMetrics().density); }
    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        sms = new SmsController(this);
        ScrollView scroll = new ScrollView(this);
        rows = new LinearLayout(this); rows.setOrientation(LinearLayout.VERTICAL); rows.setPadding(dp(22), dp(18), dp(22), dp(28));
        rows.setBackgroundColor(Color.WHITE); scroll.addView(rows);
        addText("短信与 SIM 卡", 24);
        addText("仅使用明确选中的国内卡。英国卡和无法确认国家的卡不参与短信读取、发送或任务接入。", 15);
        summary = addText("", 15);
        button("授予短信与电话状态权限", () -> requestPermissions(new String[] {
                Manifest.permission.READ_PHONE_STATE, Manifest.permission.READ_SMS, Manifest.permission.SEND_SMS}, 4070));
        button("暂停短信访问", () -> {
            try { sms.configure(new JSONObject().put("enabled", false)); } catch (Exception ignored) { }
            refresh();
        });
        button("返回", this::finish);
        if (android.os.Build.VERSION.SDK_INT >= 35) scroll.setOnApplyWindowInsetsListener((view, insets) -> {
            android.graphics.Insets safe = insets.getInsets(WindowInsets.Type.systemBars() | WindowInsets.Type.displayCutout());
            view.setPadding(safe.left, safe.top, safe.right, safe.bottom); return insets;
        });
        setContentView(scroll); refresh();
    }
    private TextView addText(String text, int size) {
        TextView view = new TextView(this); view.setText(text); view.setTextSize(size); view.setTextColor(Color.rgb(30, 45, 60));
        view.setPadding(0, dp(10), 0, dp(10)); rows.addView(view); return view;
    }
    private void button(String text, Runnable action) {
        Button button = new Button(this); button.setText(text); button.setAllCaps(false); button.setFilterTouchesWhenObscured(true);
        button.setOnClickListener(v -> action.run()); rows.addView(button, new LinearLayout.LayoutParams(-1, -2));
    }
    private void refresh() {
        try {
            JSONObject state = sms.status();
            StringBuilder text = new StringBuilder(state.optBoolean("ready") ? "短信访问已就绪" : "短信访问尚未就绪");
            JSONObject permissions = state.getJSONObject("permissions");
            text.append("\n读取短信：").append(permissions.optBoolean("readSms") ? "已允许" : "未允许")
                    .append("\n发送短信：").append(permissions.optBoolean("sendSms") ? "已允许" : "未允许");
            JSONArray sims = state.getJSONArray("subscriptions");
            for (int i = 0; i < sims.length(); i++) {
                JSONObject sim = sims.getJSONObject(i);
                text.append("\n插槽").append(sim.optInt("slotIndex") + 1).append(" · ").append(sim.optString("label"))
                        .append(sim.optBoolean("eligible") ? "（可用于国内短信）" : "（已排除）");
            }
            text.append("\n所选国内卡编号：").append(state.optInt("selectedSubscriptionId", -1));
            if (!permissions.optBoolean("readSms")) text.append("\n若系统不提供授权，请检查应用安装时是否允许了受限短信权限；不会自动更改默认短信应用。");
            summary.setText(text);
            // Keep card buttons separate from the fixed permission controls.
            while (rows.getChildCount() > 6) rows.removeViewAt(6);
            for (int i = 0; i < sims.length(); i++) {
                JSONObject sim = sims.getJSONObject(i);
                if (!sim.optBoolean("eligible")) continue;
                final int id = sim.getInt("subscriptionId");
                button("使用插槽" + (sim.getInt("slotIndex") + 1) + "的国内卡", () -> {
                    try { sms.configure(new JSONObject().put("enabled", true).put("subscriptionId", id)); }
                    catch (Exception error) { summary.setText(error.getMessage()); return; }
                    refresh();
                });
            }
        } catch (Exception error) { summary.setText("无法读取 SIM 状态，请先授予电话状态权限"); }
    }
    @Override public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] results) {
        super.onRequestPermissionsResult(requestCode, permissions, results);
        if (requestCode == 4070) {
            try {
                JSONArray sims = sms.status().getJSONArray("subscriptions");
                int chosen = -1, count = 0;
                for (int i = 0; i < sims.length(); i++) if (sims.getJSONObject(i).optBoolean("eligible")) { chosen = sims.getJSONObject(i).getInt("subscriptionId"); count++; }
                if (count == 1 && checkSelfPermission(Manifest.permission.READ_SMS) == PackageManager.PERMISSION_GRANTED)
                    sms.configure(new JSONObject().put("enabled", true).put("subscriptionId", chosen));
            } catch (Exception ignored) { }
            refresh();
        }
    }
}
