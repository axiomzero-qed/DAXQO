package com.tencent.mm.daxqofixture;

import android.app.Activity;
import android.os.Bundle;
import android.view.Gravity;
import android.view.Window;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;

/** Local-only dialog window used to exercise cross-window targeting policies. */
public final class OverlayActivity extends Activity {
    @Override protected void onCreate(Bundle state) {
        super.onCreate(state);
        Window window = getWindow();
        window.addFlags(WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL);
        window.clearFlags(WindowManager.LayoutParams.FLAG_DIM_BEHIND);

        LinearLayout content = new LinearLayout(this);
        content.setOrientation(LinearLayout.VERTICAL);
        content.setGravity(Gravity.CENTER);
        int padding = dp(16);
        content.setPadding(padding, padding, padding, padding);
        TextView title = new TextView(this);
        title.setText("跨窗口策略测试");
        content.addView(title);
        Button button = new Button(this);
        button.setText("浮窗内普通按钮");
        content.addView(button);
        setContentView(content);

        window.setLayout(dp(300), dp(200));
    }

    private int dp(int value) {
        return (int) (value * getResources().getDisplayMetrics().density + 0.5f);
    }
}
