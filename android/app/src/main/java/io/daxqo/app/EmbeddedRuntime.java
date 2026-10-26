package io.daxqo.app;

import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Build;
import android.system.Os;
import android.util.Base64;
import java.io.File;
import java.io.FileOutputStream;
import java.security.SecureRandom;

/** App-owned runtime lifecycle. No external Termux app or pairing link is needed. */
final class EmbeddedRuntime {
    private EmbeddedRuntime() {}

    static synchronized String capability(Context context) throws Exception {
        SharedPreferences prefs = context.getSharedPreferences("pairing_private", Context.MODE_PRIVATE);
        String token = prefs.getString("embedded_capability", "");
        if (!token.matches("[A-Za-z0-9_-]{32,512}")) {
            byte[] random = new byte[48];
            new SecureRandom().nextBytes(random);
            token = Base64.encodeToString(random, Base64.NO_WRAP | Base64.NO_PADDING | Base64.URL_SAFE);
            if (!prefs.edit().putString("embedded_capability", token).commit()) throw new Exception("无法保存本机连接配置");
        }
        return token;
    }

    static void activateCapability(Context context, String token) throws Exception {
        SharedPreferences prefs = context.getSharedPreferences("pairing_private", Context.MODE_PRIVATE);
        SharedPreferences.Editor edit = prefs.edit();
        if (!prefs.getBoolean("embedded_active", false)) {
            String previous = prefs.getString("capability", "");
            if (!previous.isEmpty()) edit.putString("legacy_capability", previous);
        }
        if (!edit.putString("capability", token).putBoolean("embedded_active", true).commit()) {
            throw new Exception("无法保存本机连接配置");
        }
        File state = directory(context, "daxqo-state");
        File file = new File(state, "capability");
        try (FileOutputStream out = new FileOutputStream(file)) {
            out.write((token + "\n").getBytes("US-ASCII"));
            out.getFD().sync();
        }
        Os.chmod(file.getAbsolutePath(), 0600);
    }

    static File directory(Context context, String name) throws Exception {
        File file = new File(context.getFilesDir(), name);
        if (!file.isDirectory() && !file.mkdirs()) throw new Exception("无法创建本机工作目录");
        Os.chmod(file.getAbsolutePath(), 0700);
        return file;
    }

    static void start(Context context) {
        RuntimeService.failure = "";
        context.getSharedPreferences("pairing_private", Context.MODE_PRIVATE).edit()
                .putBoolean("embedded_user_stopped", false).apply();
        Intent intent = new Intent(context, RuntimeService.class);
        if (Build.VERSION.SDK_INT >= 26) context.startForegroundService(intent);
        else context.startService(intent);
    }
}
