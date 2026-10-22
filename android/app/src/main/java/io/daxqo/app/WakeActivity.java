package io.daxqo.app;

import android.app.Activity;
import android.graphics.Color;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.Gravity;
import android.view.WindowManager;
import android.widget.TextView;

// A brief, system-supported wake request. Never requests keyguard dismissal.
public final class WakeActivity extends Activity {
    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        if (Build.VERSION.SDK_INT >= 27) { setTurnScreenOn(true); setShowWhenLocked(true); }
        else getWindow().addFlags(WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON | WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED);
        TextView label = new TextView(this);
        label.setGravity(Gravity.CENTER);
        label.setTextSize(20);
        label.setTextColor(Color.rgb(35, 48, 64));
        label.setBackgroundColor(Color.WHITE);
        label.setText("达序请求唤醒\n请亲自解锁手机后继续");
        setContentView(label);
        new Handler(Looper.getMainLooper()).postDelayed(this::finish, 1600);
    }
}
