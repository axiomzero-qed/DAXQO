package io.daxqo.app;

import org.json.JSONObject;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;

// A private loopback HTTP adapter, available only while Android has bound the accessibility service.
final class DeviceHttpBridge {
    private static final int PORT = 18883;
    private final DeviceAccessibilityService service;
    private final ThreadPoolExecutor workers = new ThreadPoolExecutor(2, 2, 0, TimeUnit.SECONDS, new ArrayBlockingQueue<>(8));
    private final Object actions = new Object();
    private final LinkedHashMap<String, Cached> completed = new LinkedHashMap<>();
    private volatile boolean running;
    private volatile ServerSocket listener;
    DeviceHttpBridge(DeviceAccessibilityService service) {
        this.service = service;
        try {
            JSONObject journal = new JSONObject(DeviceState.prefs(service).getString("request_journal", "{}"));
            java.util.Iterator<String> ids = journal.keys();
            while (ids.hasNext() && completed.size() < 64) {
                String id = ids.next();
                JSONObject entry = journal.getJSONObject(id);
                completed.put(id, new Cached(entry.getString("digest"), entry.getJSONObject("response")));
            }
        } catch (Exception ignored) { }
    }
    boolean isListening() { return running && listener != null && !listener.isClosed(); }
    void start() {
        running = true;
        new Thread(() -> {
            try (ServerSocket socket = new ServerSocket(PORT, 8, InetAddress.getByName("127.0.0.1"))) {
                listener = socket;
                while (running) {
                    Socket client = socket.accept();
                    try { workers.execute(() -> handle(client)); }
                    catch (java.util.concurrent.RejectedExecutionException ignored) { client.close(); }
                }
            } catch (Exception ignored) {
                if (running) service.lastError = "手机控制端口不可用，请关闭并重新启用无障碍服务";
            } finally { running = false; listener = null; }
        }, "daxqo-device-listener").start();
    }
    void close() {
        running = false;
        ServerSocket socket = listener;
        if (socket != null) try { socket.close(); } catch (Exception ignored) { }
        workers.shutdownNow();
    }
    private static final class Cached {
        final String digest;
        final JSONObject response;
        Cached(String digest, JSONObject response) { this.digest = digest; this.response = response; }
    }
    private void handle(Socket socket) {
        try (Socket client = socket) {
            client.setSoTimeout(4000);
            InputStream input = client.getInputStream();
            String head = readHeader(input);
            if (head == null) { reply(client, 400, error("invalid_request", "请求头无效或过长")); return; }
            String[] lines = head.split("\r\n");
            String[] start = lines[0].split(" ", -1);
            if (start.length != 3 || !"HTTP/1.1".equals(start[2])) { reply(client, 400, error("invalid_request", "仅支持HTTP/1.1")); return; }
            Map<String, String> headers = new HashMap<>();
            for (int i = 1; i < lines.length; i++) {
                int colon = lines[i].indexOf(':');
                if (colon < 1) { reply(client, 400, error("invalid_request", "请求头无效")); return; }
                String name = lines[i].substring(0, colon).toLowerCase(Locale.ROOT);
                if (headers.put(name, lines[i].substring(colon + 1).trim()) != null) { reply(client, 400, error("invalid_request", "请求头重复")); return; }
            }
            if (!"127.0.0.1:18883".equals(headers.get("host")) || headers.containsKey("origin") || headers.containsKey("transfer-encoding")) { reply(client, 403, error("forbidden", "仅接受已配对的本机服务请求")); return; }
            String cap = service.getSharedPreferences("pairing_private", android.content.Context.MODE_PRIVATE).getString("capability", "");
            String supplied = headers.get("authorization");
            if (cap.length() < 32 || supplied == null || !MessageDigest.isEqual(("Bearer " + cap).getBytes(StandardCharsets.UTF_8), supplied.getBytes(StandardCharsets.UTF_8))) { reply(client, 401, error("unauthorized", "本机服务尚未配对或认证失败")); return; }
            if ("GET".equals(start[0]) && "/api/device/status".equals(start[1])) { reply(client, 200, DeviceState.status(service)); return; }
            if ("GET".equals(start[0]) && "/api/device/approvals".equals(start[1])) { reply(client, 200, new JSONObject().put("approvals", DeviceState.approvalList())); return; }
            boolean approvalDecision = start[1].matches("/api/device/approvals/[a-fA-F0-9-]{36}");
            if (!"POST".equals(start[0]) || (!"/api/device/execute".equals(start[1]) && !approvalDecision)) { reply(client, 404, error("not_found", "接口不存在")); return; }
            String mime = headers.get("content-type");
            if (mime == null || !mime.toLowerCase(Locale.ROOT).matches("application/json(?:\\s*;.*)?")) { reply(client, 415, error("invalid_request", "请求必须使用JSON格式")); return; }
            int length;
            try { length = Integer.parseInt(headers.get("content-length")); }
            catch (Exception ignored) { reply(client, 411, error("invalid_request", "缺少有效请求长度")); return; }
            if (length < 2 || length > 65536) { reply(client, 413, error("invalid_request", "请求体超过限制")); return; }
            byte[] bytes = new byte[length];
            int offset = 0;
            while (offset < length) {
                int count = input.read(bytes, offset, length - offset);
                if (count < 0) throw new java.io.IOException("short request");
                offset += count;
            }
            JSONObject request;
            try { request = new JSONObject(new String(bytes, StandardCharsets.UTF_8)); }
            catch (Exception ignored) { reply(client, 400, error("invalid_request", "JSON格式无效")); return; }
            if (approvalDecision) {
                String approvalId = start[1].substring("/api/device/approvals/".length());
                reply(client, 200, service.decideApproval(approvalId, request.optString("decision", "")));
                return;
            }
            String id = request.optString("id", "");
            if (id.isEmpty() || id.length() > 128 || !(request.opt("action") instanceof String) || (request.has("args") && !(request.opt("args") instanceof JSONObject))) { reply(client, 400, DeviceAccessibilityService.failure(id, "invalid_args", "请提供id、action和对象格式args")); return; }
            JSONObject response;
            // Serialize actions and remember their result, so retrying an id cannot repeat a tap or input.
            synchronized (actions) {
                boolean mutation = !"status".equals(request.optString("action")) && !"tree".equals(request.optString("action")) && !"screenshot".equals(request.optString("action"));
                if (mutation) {
                    String digest = Base64Digest(bytes);
                    Cached cached = completed.get(id);
                    if (cached != null) response = cached.digest.equals(digest) ? cached.response : DeviceAccessibilityService.failure(id, "id_conflict", "相同请求id不能用于不同操作");
                    else {
                        // Persist an uncertain result before dispatch: process death must never make a retry repeat a write.
                        completed.put(id, new Cached(digest, DeviceAccessibilityService.failure(id, "outcome_unknown", "此前操作的结果尚未确定，请先核对手机，不要重复执行")));
                        if (completed.size() > 64) completed.remove(completed.keySet().iterator().next());
                        if (!saveJournal()) {
                            completed.remove(id);
                            response = DeviceAccessibilityService.failure(id, "storage_unavailable", "无法保存操作记录，已拒绝执行；请检查手机存储");
                        } else {
                            response = service.execute(request);
                            completed.put(id, new Cached(digest, response));
                            saveJournal();
                        }
                    }
                } else response = service.execute(request);
            }
            reply(client, 200, response);
        } catch (Exception ignored) { /* No request bodies, credentials, UI text or images enter logs. */ }
    }
    private boolean saveJournal() {
        try {
            JSONObject journal = new JSONObject();
            for (Map.Entry<String, Cached> item : completed.entrySet()) journal.put(item.getKey(), new JSONObject().put("digest", item.getValue().digest).put("response", item.getValue().response));
            return DeviceState.prefs(service).edit().putString("request_journal", journal.toString()).commit();
        } catch (Exception ignored) { return false; }
    }
    private String Base64Digest(byte[] bytes) throws Exception {
        return android.util.Base64.encodeToString(MessageDigest.getInstance("SHA-256").digest(bytes), android.util.Base64.NO_WRAP);
    }
    private static String readHeader(InputStream input) throws Exception {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        int last = 0;
        while (out.size() < 8192) {
            int value = input.read();
            if (value < 0) return null;
            out.write(value);
            last = (last << 8) | value;
            if (last == 0x0d0a0d0a) return new String(out.toByteArray(), StandardCharsets.US_ASCII);
        }
        return null;
    }
    private static JSONObject error(String code, String text) { return DeviceAccessibilityService.failure("", code, text); }
    private static void reply(Socket client, int status, JSONObject body) throws Exception {
        byte[] bytes = body.toString().getBytes(StandardCharsets.UTF_8);
        String reason = status == 200 ? "OK" : "Request Rejected";
        String header = "HTTP/1.1 " + status + " " + reason + "\r\nContent-Type: application/json; charset=utf-8\r\nContent-Length: " + bytes.length + "\r\nCache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\nConnection: close\r\n\r\n";
        OutputStream output = client.getOutputStream();
        output.write(header.getBytes(StandardCharsets.US_ASCII)); output.write(bytes); output.flush();
    }
}
