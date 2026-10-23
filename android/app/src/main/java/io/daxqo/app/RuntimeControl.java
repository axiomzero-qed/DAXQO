package io.daxqo.app;

import org.json.JSONObject;

import java.io.OutputStream;
import java.io.InputStream;
import java.io.ByteArrayOutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import android.os.SystemClock;

final class RuntimeControl {
    private RuntimeControl() {}

    static String request(String base, String action, String capability) {
        HttpURLConnection connection = null;
        try {
            String path = "status".equals(action) ? "/api/runtime/status" : "/api/runtime/" + action;
            connection = (HttpURLConnection) new URL(base + path).openConnection();
            connection.setInstanceFollowRedirects(false);
            connection.setConnectTimeout("status".equals(action) ? 1000 : 2500);
            connection.setReadTimeout("status".equals(action) ? 1500 : 5000);
            connection.setRequestProperty("Authorization", "Bearer " + capability);
            connection.setRequestProperty("Accept", "application/json");
            if (!"status".equals(action)) {
                connection.setRequestMethod("POST");
                connection.setDoOutput(true);
                connection.setRequestProperty("Content-Type", "application/json; charset=utf-8");
                try (OutputStream out = connection.getOutputStream()) { out.write("{}".getBytes("UTF-8")); }
            }
            int code = connection.getResponseCode();
            if (code == 409) return "有任务正在运行，运行桥拒绝了重启请求";
            if (code < 200 || code >= 300) return "本机桥返回错误 (HTTP " + code + ")";
            if ("status".equals(action)) {
                JSONObject body = new JSONObject(readAll(connection.getInputStream()));
                JSONObject backend = body.optJSONObject("backend");
                if (backend == null || !backend.optBoolean("connected", false)) return "OK backend_offline";
                return "OK · " + body.optString("state", "running");
            }
            return "OK · 请求已由本机桥接受";
        } catch (Exception e) {
            return "Linux 环境离线或运行桥不可用；请确认 Linux 容器（如 Termux）已运行后重试";
        } finally { if (connection != null) connection.disconnect(); }
    }

    static String ensureRunning(String base, String capability) {
        String result = request(base, "status", capability);
        if (!result.startsWith("OK")) return result;
        if (result.contains("backend_offline")) {
            String started = request(base, "start", capability);
            if (!started.startsWith("OK")) return started;
            long deadline = SystemClock.elapsedRealtime() + 20_000;
            while (SystemClock.elapsedRealtime() < deadline) {
                long pause = Math.min(1000, deadline - SystemClock.elapsedRealtime());
                try { Thread.sleep(pause); }
                catch (InterruptedException e) { Thread.currentThread().interrupt(); return "启动检查已取消"; }
                result = request(base, "status", capability);
                if (result.startsWith("OK") && !result.contains("backend_offline")) return "OK · 本机服务已就绪";
            }
            return "Linux环境在线，但 DAXQO 服务仍未就绪；可稍后重试";
        }
        return result;
    }

    static String waitUntilReady(String base, String capability) {
        long deadline = SystemClock.elapsedRealtime() + 20_000;
        while (SystemClock.elapsedRealtime() < deadline) {
            String result = request(base, "status", capability);
            if (!result.startsWith("OK")) return result;
            if (!result.contains("backend_offline")) return "OK · 本机服务已就绪";
            try { Thread.sleep(Math.min(1000, deadline - SystemClock.elapsedRealtime())); }
            catch (InterruptedException e) { Thread.currentThread().interrupt(); return "启动检查已取消"; }
        }
        return "Linux环境在线，但 DAXQO 服务仍未就绪；可稍后重试";
    }

    private static String readAll(InputStream input) throws Exception {
        try (InputStream in = input; ByteArrayOutputStream out = new ByteArrayOutputStream()) {
            byte[] buffer = new byte[2048];
            int count;
            while ((count = in.read(buffer)) >= 0) out.write(buffer, 0, count);
            return out.toString("UTF-8");
        }
    }
}
