package io.daxqo.app;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** Resume a previously activated workspace after Android has unlocked its storage. */
public final class RuntimeBootReceiver extends BroadcastReceiver {
    @Override public void onReceive(Context context, Intent intent) {
        if (!Intent.ACTION_BOOT_COMPLETED.equals(intent.getAction())) return;
        if (!context.getSharedPreferences("pairing_private", Context.MODE_PRIVATE)
                .getBoolean("embedded_active", false)) return;
        if (context.getSharedPreferences("pairing_private", Context.MODE_PRIVATE)
                .getBoolean("embedded_user_stopped", false)) return;
        try { EmbeddedRuntime.start(context); }
        catch (Exception ignored) { /* The next explicit app launch retries on restricted OEMs. */ }
    }
}
