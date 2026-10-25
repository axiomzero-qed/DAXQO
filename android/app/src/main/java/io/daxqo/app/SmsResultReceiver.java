package io.daxqo.app;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** Private callbacks for this app's explicit-SIM SMS sends; not an inbox receiver. */
public final class SmsResultReceiver extends BroadcastReceiver {
    @Override public void onReceive(Context context, Intent intent) {
        new SmsController(context).recordResult(intent, getResultCode());
    }
}
