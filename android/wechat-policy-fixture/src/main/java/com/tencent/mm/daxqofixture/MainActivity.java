package com.tencent.mm.daxqofixture;

import android.app.Activity;
import android.os.Bundle;
import android.text.Editable;
import android.text.TextWatcher;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

public final class MainActivity extends Activity {
    private android.content.SharedPreferences proof;
    private TextView status;
    private int clicks, edits, scrolls, leaves, wechatClicks;

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        proof = getSharedPreferences("proof", MODE_PRIVATE);
        clicks = proof.getInt("clicks", 0);
        edits = proof.getInt("edits", 0);
        scrolls = proof.getInt("scrolls", 0);
        leaves = proof.getInt("leaves", 0);
        wechatClicks = proof.getInt("wechat_clicks", 0);

        LinearLayout content = new LinearLayout(this);
        content.setOrientation(LinearLayout.VERTICAL);
        status = new TextView(this);
        content.addView(status);
        Button click = new Button(this);
        click.setText("测试点击");
        click.setOnClickListener(v -> { clicks++; save(); });
        content.addView(click);
        Button wechat = new Button(this);
        wechat.setText("微信测试入口");
        wechat.setOnClickListener(v -> { wechatClicks++; save(); });
        content.addView(wechat);
        LinearLayout entryRow = new LinearLayout(this);
        entryRow.setId(R.id.wechat_entry_row);
        entryRow.setOrientation(LinearLayout.HORIZONTAL);
        entryRow.setGravity(android.view.Gravity.CENTER_VERTICAL);
        entryRow.setClickable(true);
        entryRow.setFocusable(true);
        entryRow.setBackgroundResource(android.R.drawable.list_selector_background);
        entryRow.setOnClickListener(v -> { wechatClicks++; save(); });
        ImageView entryIcon = new ImageView(this);
        entryIcon.setId(R.id.wechat_entry_icon);
        entryIcon.setImageResource(android.R.drawable.ic_menu_view);
        entryIcon.setContentDescription(null);
        entryIcon.setClickable(false);
        entryIcon.setImportantForAccessibility(View.IMPORTANT_FOR_ACCESSIBILITY_YES);
        entryRow.addView(entryIcon, new LinearLayout.LayoutParams(dp(120), dp(60)));
        TextView entryLabel = new TextView(this);
        entryLabel.setText("微信模拟入口");
        entryRow.addView(entryLabel, new LinearLayout.LayoutParams(0, dp(60), 1));
        content.addView(entryRow, new LinearLayout.LayoutParams(-1, dp(60)));
        EditText input = new EditText(this);
        input.setId(0x1234);
        input.setHint("固定测试文本");
        content.addView(input);
        input.addTextChangedListener(new TextWatcher() {
            @Override public void beforeTextChanged(CharSequence s, int start, int count, int after) {}
            @Override public void onTextChanged(CharSequence s, int start, int before, int count) {
                edits++;
                proof.edit().putInt("edits", edits).putString("text", s.toString()).commit();
                update();
            }
            @Override public void afterTextChanged(Editable s) {}
        });
        ScrollView scroll = new ScrollView(this) {
            @Override protected void onScrollChanged(int x, int y, int oldX, int oldY) {
                super.onScrollChanged(x, y, oldX, oldY);
                if (y != oldY) { scrolls++; save(); }
            }
        };
        LinearLayout rows = new LinearLayout(this);
        rows.setOrientation(LinearLayout.VERTICAL);
        for (int i = 1; i <= 40; i++) {
            TextView row = new TextView(this);
            row.setText("滚动测试行 " + i);
            row.setPadding(16, 20, 16, 20);
            rows.addView(row);
        }
        scroll.addView(rows);
        content.addView(scroll, new LinearLayout.LayoutParams(-1, 0, 1));
        setContentView(content);
        update();
    }

    private void save() {
        proof.edit().putInt("clicks", clicks).putInt("edits", edits)
            .putInt("scrolls", scrolls).putInt("leaves", leaves)
            .putInt("wechat_clicks", wechatClicks).commit();
        update();
    }

    private void update() {
        if (status != null) status.setText("clicks=" + clicks + " edits=" + edits
            + " scrolls=" + scrolls + " leaves=" + leaves + " wechat_clicks=" + wechatClicks);
    }

    private int dp(int value) {
        return (int) (value * getResources().getDisplayMetrics().density + 0.5f);
    }

    @Override protected void onUserLeaveHint() {
        super.onUserLeaveHint();
        leaves++;
        save();
    }

    @Override protected void onStop() {
        leaves++;
        save();
        super.onStop();
    }
}
