package io.daxqo.app;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.os.IBinder;
import android.os.PowerManager;
import java.io.File;
import java.io.FileOutputStream;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** Owns the entire PRoot child tree in the DAXQO Android UID. */
public final class RuntimeService extends Service {
    private static volatile RuntimeService current;
    private static final String STOP = "io.daxqo.app.STOP_RUNTIME";
    static volatile String progress = "正在准备本机环境…";
    static volatile String failure = "";
    static volatile boolean launched;
    private static final String CHANNEL = "daxqo_runtime";
    private static final int NOTICE = 10001;
    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    private volatile Process process;
    private volatile boolean stopped;
    private boolean submitted;
    private PowerManager.WakeLock wake;

    @Override public void onCreate() {
        super.onCreate();
        current = this;
        ((NotificationManager) getSystemService(NOTIFICATION_SERVICE)).createNotificationChannel(
                new NotificationChannel(CHANNEL, "DAXQO 本机运行环境", NotificationManager.IMPORTANCE_LOW));
        startForeground(NOTICE, notification("正在启动本机工作台"));
    }

    private Notification notification(String text) {
        PendingIntent open = PendingIntent.getActivity(this, 0, new Intent(this, MainActivity.class),
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        PendingIntent stop = PendingIntent.getService(this, 1, new Intent(this, RuntimeService.class).setAction(STOP),
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        return new Notification.Builder(this, CHANNEL).setSmallIcon(R.drawable.ic_launcher_foreground)
                .setContentTitle("达氙 DAXQO").setContentText(text).setContentIntent(open).setOngoing(true)
                .addAction(new Notification.Action.Builder(null, "停止运行", stop).build()).build();
    }

    private void update(String message) {
        if (current != this) return;
        progress = message;
        ((NotificationManager) getSystemService(NOTIFICATION_SERVICE)).notify(NOTICE, notification(message));
    }

    @Override public synchronized int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && STOP.equals(intent.getAction())) {
            getSharedPreferences("pairing_private", MODE_PRIVATE).edit().putBoolean("embedded_user_stopped", true).apply();
            stopSelf();
            return START_NOT_STICKY;
        }
        if (!submitted) {
            submitted = true;
            failure = "";
            launched = false;
            worker.execute(this::runRuntime);
        }
        return START_STICKY;
    }

    @android.annotation.SuppressLint("WakelockTimeout")
    private void runRuntime() {
        try {
            File rootfs = OfflineRuntimeInstaller.ensureInstalled(this, this::update);
            if (stopped) return;
            String token = EmbeddedRuntime.capability(this);
            // A foreign/legacy runtime must not be mistaken for this app's runtime.
            for (int port : new int[] {18880, 18882}) {
                try (java.net.Socket socket = new java.net.Socket()) {
                    socket.connect(new java.net.InetSocketAddress("127.0.0.1", port), 500);
                    throw new Exception("检测到已有运行环境，请先停止旧版后台，再重试启动");
                } catch (java.net.ConnectException expected) { /* Port is available. */ }
            }
            EmbeddedRuntime.activateCapability(this, token);
            File state = EmbeddedRuntime.directory(this, "daxqo-state");
            File workspace = EmbeddedRuntime.directory(this, "daxqo-workspace");
            File stage = EmbeddedRuntime.directory(this, "daxqo-update-stage");
            File temporary = EmbeddedRuntime.directory(this, "daxqo-tmp");
            File links = new File(rootfs, ".l2s");
            if (!links.isDirectory() && !links.mkdir()) throw new Exception("无法准备本机文件系统");
            File dns = new File(temporary, "resolv.conf");
            StringBuilder servers = new StringBuilder();
            try {
                android.net.ConnectivityManager network = (android.net.ConnectivityManager) getSystemService(CONNECTIVITY_SERVICE);
                android.net.LinkProperties link = network.getLinkProperties(network.getActiveNetwork());
                if (link != null) for (java.net.InetAddress address : link.getDnsServers())
                    servers.append("nameserver ").append(address.getHostAddress()).append('\n');
            } catch (Exception ignored) { }
            if (servers.length() == 0) servers.append("nameserver 223.5.5.5\nnameserver 1.1.1.1\n");
            try (FileOutputStream out = new FileOutputStream(dns)) { out.write(servers.toString().getBytes("US-ASCII")); }
            android.system.Os.chmod(dns.getAbsolutePath(), 0600);
            File nativeDir = new File(getApplicationInfo().nativeLibraryDir);
            List<String> command = new ArrayList<>();
            command.add(new File(nativeDir, "libproot.so").getAbsolutePath());
            java.util.Collections.addAll(command, "--kill-on-exit", "--link2symlink", "-0", "-r", rootfs.getAbsolutePath(),
                    "-b", "/dev", "-b", "/proc", "-b", "/sys", "-b", temporary.getAbsolutePath(),
                    "-b", links.getAbsolutePath(),
                    "-b", dns + ":/etc/resolv.conf",
                    "-b", state + ":/opt/daxqo/state", "-b", workspace + ":/opt/daxqo/workspace",
                    "-b", stage + ":/opt/daxqo/update-stage", "-w", "/opt/daxqo", "/usr/bin/env", "-i",
                    "HOME=/root", "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                    "LANG=C.UTF-8", "TMPDIR=/tmp", "TERM=xterm-256color", "PYTHONUNBUFFERED=1",
                    "DAXQO_EMBEDDED=1", "PYTHONPATH=/opt/daxqo:/opt/daxqo/runtime/mail-libs",
                    "/usr/bin/python3", "/opt/daxqo/embedded_runtime.py");
            ProcessBuilder builder = new ProcessBuilder(command).directory(rootfs).redirectErrorStream(true);
            Map<String, String> env = builder.environment();
            env.remove("LD_PRELOAD");
            env.put("LD_LIBRARY_PATH", nativeDir.getAbsolutePath());
            env.put("PROOT_LOADER", new File(nativeDir, "libproot_loader.so").getAbsolutePath());
            env.put("PROOT_TMP_DIR", temporary.getAbsolutePath());
            env.put("PROOT_L2S_DIR", links.getAbsolutePath());
            File log = new File(getFilesDir(), "runtime.log");
            // Bound logs between launches; never include credentials in the command line.
            if (log.length() > 2 * 1024 * 1024) try (FileOutputStream out = new FileOutputStream(log)) { out.write(new byte[0]); }
            builder.redirectOutput(ProcessBuilder.Redirect.appendTo(log));
            PowerManager power = (PowerManager) getSystemService(POWER_SERVICE);
            wake = power.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, getPackageName() + ":runtime");
            wake.setReferenceCounted(false);
            // The user-visible, stoppable service owns the local scheduler/mail
            // loop. A ten-minute timeout would silently suspend longer schedules.
            wake.acquire();
            update("正在启动本机工作台…");
            process = builder.start();
            if (stopped) { process.destroy(); return; }
            if (current == this) launched = true;
            OsPermissions.privateLog(log);
            update("本机环境运行中");
            int code = process.waitFor();
            if (!stopped) throw new Exception("本机环境已退出（" + code + "），点启动重试可恢复");
        } catch (Exception error) {
            if (!stopped) {
                failure = error.getMessage() == null ? "本机环境启动失败，请重试" : error.getMessage();
                update(failure);
                stopSelf();
            }
        } finally {
            if (current == this) launched = false;
            Process child = process;
            if (child != null && child.isAlive()) child.destroy();
            releaseWake();
        }
    }

    @Override public void onDestroy() {
        stopped = true;
        if (current == this) { launched = false; current = null; }
        Process child = process;
        if (child != null) child.destroy();
        worker.shutdownNow();
        releaseWake();
        super.onDestroy();
    }

    private synchronized void releaseWake() {
        if (wake != null && wake.isHeld()) wake.release();
    }

    @Override public IBinder onBind(Intent intent) { return null; }

    private static final class OsPermissions {
        static void privateLog(File file) throws Exception { android.system.Os.chmod(file.getAbsolutePath(), 0600); }
    }
}
