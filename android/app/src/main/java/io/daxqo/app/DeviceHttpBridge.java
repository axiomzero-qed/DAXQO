package io.daxqo.app;

import org.json.JSONObject;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.URLDecoder;
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
            if (start[1].startsWith("/api/sms/")) { handleSms(client, input, start[0], start[1], headers); return; }
            if ("GET".equals(start[0]) && "/api/device/status".equals(start[1])) { reply(client, 200, DeviceState.status(service)); return; }
            if ("GET".equals(start[0]) && "/api/device/approvals".equals(start[1])) { reply(client, 200, new JSONObject().put("approvals", DeviceState.approvalList())); return; }

            if ("GET".equals(start[0]) && start[1].startsWith("/api/motor/")) {
                if ("/api/motor/status".equals(start[1])) { reply(client, 200, service.motorStatus()); return; }
                if (start[1].startsWith("/api/motor/events?")) {
                    android.net.Uri uri = android.net.Uri.parse(start[1]);
                    try { reply(client, 200, service.motorEvents(Long.parseLong(uri.getQueryParameter("after")), Integer.parseInt(uri.getQueryParameter("timeoutMs")))); }
                    catch (IllegalArgumentException ignored) { reply(client, 400, error("invalid_args", "事件参数无效")); }
                    return;
                }
                reply(client, 404, error("not_found", "Motor接口不存在")); return;
            }
            TrajectoryRecorder recorder = service.trajectory();
            if ("GET".equals(start[0]) && start[1].startsWith("/api/trajectory/")) {
                if (recorder == null) { reply(client, 503, error("collector_unavailable", "采集模块不可用，手机控制仍可使用")); return; }
                if ("/api/trajectory/status".equals(start[1])) { reply(client, 200, recorder.status()); return; }
                if (start[1].startsWith("/api/trajectory/batch?")) {
                    android.net.Uri uri = android.net.Uri.parse(start[1]);
                    try { reply(client, 200, recorder.batch(Long.parseLong(uri.getQueryParameter("after")), Integer.parseInt(uri.getQueryParameter("limit")))); }
                    catch (IllegalArgumentException ignored) { reply(client, 400, error("invalid_args", "批次参数无效")); }
                    return;
                }
                reply(client, 404, error("not_found", "采集接口不存在")); return;
            }
            if ("GET".equals(start[0]) && start[1].startsWith("/api/device/apps")) {
                CatalogArgs args = parseCatalogArgs(start[1]);
                if (args == null) { reply(client, 400, error("invalid_request", "应用目录查询参数无效")); return; }
                reply(client, 200, AppCatalog.list(service, args.query, args.limit));
                return;
            }
            boolean approvalDecision = start[1].matches("/api/device/approvals/[a-fA-F0-9-]{36}");
            boolean trajectoryRoute = start[1].matches("/api/trajectory/(config|ack|feedback)");
            boolean motorRoute = start[1].matches("/api/motor/(lease|release|cancel)");
            if (!"POST".equals(start[0]) || (!"/api/device/execute".equals(start[1]) && !approvalDecision && !trajectoryRoute && !motorRoute)) { reply(client, 404, error("not_found", "接口不存在")); return; }
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

            if (motorRoute) {
                if (start[1].endsWith("/cancel")) { reply(client, 200, service.motorCancel(request)); return; }
                if (start[1].endsWith("/release")) { reply(client, 200, service.motorRelease(request)); return; }
                synchronized (actions) { reply(client, 200, service.motorAcquire(request)); }
                return;
            }
            if (trajectoryRoute) {
                if (recorder == null) { reply(client, 503, error("collector_unavailable", "采集模块不可用，手机控制仍可使用")); return; }
                try {
                    JSONObject value = start[1].endsWith("/config") ? recorder.configure(request)
                            : start[1].endsWith("/ack") ? recorder.ack(request) : recorder.feedback(request);
                    reply(client, 200, value);
                } catch (Exception ignored) { reply(client, 400, error("invalid_args", "采集参数或存储不可用")); }
                return;
            }
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
                JSONObject policyDenied = service.wechatGuard(id, request.optString("action"), request.optJSONObject("args") == null ? new JSONObject() : request.optJSONObject("args"));
                if (policyDenied != null) { reply(client, 200, policyDenied); return; }
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
    private void handleSms(Socket client, InputStream input, String method, String path, Map<String, String> headers) throws Exception {
        SmsController sms = new SmsController(service);
        try {
            if ("GET".equals(method) && "/api/sms/status".equals(path)) { reply(client, 200, sms.status()); return; }
            if ("GET".equals(method) && path.startsWith("/api/sms/sends/")) {
                reply(client, 200, sms.sendStatus(URLDecoder.decode(path.substring("/api/sms/sends/".length()), "UTF-8"))); return;
            }
            if (!"POST".equals(method) || !("/api/sms/config".equals(path) || "/api/sms/query".equals(path) || "/api/sms/send".equals(path))) {
                reply(client, 404, error("not_found", "短信接口不存在")); return;
            }
            String mime = headers.get("content-type");
            if (mime == null || !mime.toLowerCase(Locale.ROOT).matches("application/json(?:\\s*;.*)?")) {
                reply(client, 415, error("invalid_request", "短信接口仅接受 JSON")); return;
            }
            int length;
            try { length = Integer.parseInt(headers.get("content-length")); }
            catch (Exception ignored) { reply(client, 411, error("invalid_request", "缺少有效请求长度")); return; }
            if (length < 2 || length > 16384) { reply(client, 413, error("invalid_request", "短信请求超过限制")); return; }
            byte[] bytes = new byte[length];
            int offset = 0;
            while (offset < length) { int count = input.read(bytes, offset, length - offset); if (count < 0) throw new java.io.IOException("short SMS request"); offset += count; }
            JSONObject request;
            try { request = new JSONObject(new String(bytes, StandardCharsets.UTF_8)); }
            catch (Exception ignored) { reply(client, 400, error("invalid_request", "短信请求 JSON 无效")); return; }
            JSONObject result = "/api/sms/config".equals(path) ? sms.configure(request) : "/api/sms/query".equals(path) ? sms.query(request) : sms.send(request);
            reply(client, 200, result);
        } catch (SmsController.Failure error) { reply(client, 409, error(error.code, error.getMessage())); }
        catch (SecurityException ignored) { reply(client, 403, error("permission_required", "系统尚未允许短信访问，请在本机授权")); }
        catch (IllegalArgumentException error) { reply(client, 400, error("invalid_args", error.getMessage())); }
    }
    private String Base64Digest(byte[] bytes) throws Exception {
        return android.util.Base64.encodeToString(MessageDigest.getInstance("SHA-256").digest(bytes), android.util.Base64.NO_WRAP);
    }
    private static final class CatalogArgs {
        final String query;
        final int limit;
        CatalogArgs(String query, int limit) { this.query = query; this.limit = limit; }
    }
    private static CatalogArgs parseCatalogArgs(String target) {
        String[] parts = target.split("\\?", -1);
        if (parts.length > 2 || !"/api/device/apps".equals(parts[0])) return null;
        String query = "";
        int limit = AppCatalog.DEFAULT_LIMIT;
        boolean seenQuery = false, seenLimit = false;
        if (parts.length == 2 && !parts[1].isEmpty()) {
            for (String pair : parts[1].split("&", -1)) {
                int equals = pair.indexOf('=');
                if (equals < 1) return null;
                String key, value;
                try {
                    key = URLDecoder.decode(pair.substring(0, equals), "UTF-8");
                    value = URLDecoder.decode(pair.substring(equals + 1), "UTF-8");
                } catch (Exception ignored) { return null; }
                if ("query".equals(key) && !seenQuery) {
                    if (value.length() > 80) return null;
                    query = value;
                    seenQuery = true;
                } else if ("limit".equals(key) && !seenLimit) {
                    if (!value.matches("[0-9]{1,3}")) return null;
                    try { limit = Integer.parseInt(value); } catch (Exception ignored) { return null; }
                    if (limit < 1 || limit > AppCatalog.MAX_LIMIT) return null;
                    seenLimit = true;
                } else return null;
            }
        }
        return new CatalogArgs(query, limit);
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
