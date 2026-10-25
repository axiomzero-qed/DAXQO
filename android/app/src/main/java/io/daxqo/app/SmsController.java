package io.daxqo.app;

import android.Manifest;
import android.annotation.SuppressLint;
import android.app.Activity;
import android.app.PendingIntent;
import android.content.Context;
import android.content.ContentValues;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.database.Cursor;
import android.database.sqlite.SQLiteDatabase;
import android.database.sqlite.SQLiteOpenHelper;
import android.net.Uri;
import android.os.Build;
import android.provider.Telephony;
import android.telephony.SmsManager;
import android.telephony.SmsMessage;
import android.telephony.SubscriptionInfo;
import android.telephony.SubscriptionManager;
import android.util.Base64;
import org.json.JSONArray;
import org.json.JSONObject;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

/** Selected domestic SIM only. Never reads foreign-SIM SMS or uses a default SMS route. */
final class SmsController {
    private static final Object LOCK = new Object();
    private static SQLiteOpenHelper sendDatabase;
    private final Context context;
    SmsController(Context context) { this.context = context.getApplicationContext(); }
    private SharedPreferences prefs() { return context.getSharedPreferences("sms_private", Context.MODE_PRIVATE); }
    private boolean granted(String permission) { return context.checkSelfPermission(permission) == PackageManager.PERMISSION_GRANTED; }
    static final class Failure extends Exception {
        final String code;
        Failure(String code, String message) { super(message); this.code = code; }
    }
    @SuppressLint("MissingPermission") // Every read is guarded by the runtime permission check below.
    private List<SubscriptionInfo> subscriptions() {
        if (!granted(Manifest.permission.READ_PHONE_STATE)) return new ArrayList<>();
        try {
            SubscriptionManager manager = context.getSystemService(SubscriptionManager.class);
            List<SubscriptionInfo> values = manager == null ? null : manager.getActiveSubscriptionInfoList();
            return values == null ? new ArrayList<>() : values;
        } catch (SecurityException ignored) { return new ArrayList<>(); }
    }
    private String mcc(SubscriptionInfo info) {
        return Build.VERSION.SDK_INT >= 29 ? info.getMccString() : String.format(java.util.Locale.ROOT, "%03d", info.getMcc());
    }
    private boolean eligible(SubscriptionInfo info) {
        return info != null && info.getSimSlotIndex() >= 0 && SmsPolicy.eligibleSim(mcc(info), info.getCountryIso());
    }
    private SubscriptionInfo eligibleSubscription(int id) throws Failure {
        for (SubscriptionInfo info : subscriptions()) if (info.getSubscriptionId() == id) {
            if (!eligible(info)) throw new Failure("sim_blocked", "这张卡不是已确认的国内 SIM，不能用于短信读取或发送");
            return info;
        }
        throw new Failure("sim_unavailable", "所选国内 SIM 不可用，请重新检查短信与 SIM 卡设置");
    }
    private int selected(boolean sending) throws Failure {
        if (!prefs().getBoolean("enabled", false)) throw new Failure("sms_disabled", "请先在本机开启短信访问");
        if (!granted(Manifest.permission.READ_PHONE_STATE) || !granted(Manifest.permission.READ_SMS)
                || (sending && !granted(Manifest.permission.SEND_SMS)))
            throw new Failure("permission_required", "请在手机的短信与 SIM 卡设置中授予短信权限");
        int id = prefs().getInt("subscription_id", SubscriptionManager.INVALID_SUBSCRIPTION_ID);
        eligibleSubscription(id);
        return id;
    }
    @SuppressLint("MissingPermission") // Only eligible domestic numbers with READ_SMS are queried.
    JSONObject status() throws Exception {
        JSONArray sims = new JSONArray();
        int selectedId = prefs().getInt("subscription_id", -1);
        boolean selectedEligible = false;
        for (SubscriptionInfo info : subscriptions()) {
            boolean allowed = eligible(info);
            selectedEligible |= allowed && info.getSubscriptionId() == selectedId;
            String number = "";
            if (allowed && granted(Manifest.permission.READ_SMS)) try { number = SmsPolicy.normalizePhone(info.getNumber()); } catch (Exception ignored) { }
            sims.put(new JSONObject().put("subscriptionId", info.getSubscriptionId()).put("slotIndex", info.getSimSlotIndex())
                    .put("label", String.valueOf(info.getDisplayName())).put("countryIso", info.getCountryIso()).put("mcc", mcc(info))
                    .put("eligible", allowed).put("number", number)
                    .put("blockedReason", allowed ? "" : "英国卡及未确认的非国内卡已排除"));
        }
        boolean enabled = prefs().getBoolean("enabled", false);
        return new JSONObject().put("enabled", enabled).put("selectedSubscriptionId", selectedId).put("subscriptions", sims)
                .put("permissions", new JSONObject().put("readSms", granted(Manifest.permission.READ_SMS))
                        .put("sendSms", granted(Manifest.permission.SEND_SMS)).put("readPhoneState", granted(Manifest.permission.READ_PHONE_STATE)))
                .put("ready", enabled && selectedEligible && granted(Manifest.permission.READ_SMS) && granted(Manifest.permission.READ_PHONE_STATE));
    }
    JSONObject configure(JSONObject args) throws Exception {
        for (java.util.Iterator<String> keys = args.keys(); keys.hasNext();) {
            String key = keys.next();
            if (!"enabled".equals(key) && !"subscriptionId".equals(key)) throw new Failure("invalid_args", "短信设置字段无效");
        }
        if (args.has("enabled") && !(args.opt("enabled") instanceof Boolean)) throw new Failure("invalid_args", "短信开关无效");
        synchronized (LOCK) {
            boolean enabled = args.has("enabled") ? args.getBoolean("enabled") : prefs().getBoolean("enabled", false);
            int id = args.has("subscriptionId") ? integer(args, "subscriptionId", 0, Integer.MAX_VALUE) : prefs().getInt("subscription_id", -1);
            if (enabled) {
                if (!granted(Manifest.permission.READ_SMS) || !granted(Manifest.permission.READ_PHONE_STATE))
                    throw new Failure("permission_required", "请先在本机授予短信与电话状态权限");
                eligibleSubscription(id);
            } else if (args.has("subscriptionId")) eligibleSubscription(id);
            if (!prefs().edit().putBoolean("enabled", enabled).putInt("subscription_id", id).commit())
                throw new Failure("storage_unavailable", "无法保存短信设置");
        }
        return status();
    }
    private static int integer(JSONObject args, String key, int min, int max) throws Exception {
        long value = number(args, key, min, max);
        return (int) value;
    }
    private static long number(JSONObject args, String key, long min, long max) throws Exception {
        Object raw = args.opt(key);
        if (!(raw instanceof Integer || raw instanceof Long)) throw new Failure("invalid_args", "短信查询数字字段无效");
        long value = ((Number) raw).longValue();
        if (value < min || value > max) throw new Failure("invalid_args", "短信查询范围无效");
        return value;
    }
    @SuppressLint("MissingPermission") // selected(false) checks READ_SMS and the domestic SIM before provider access.
    JSONObject query(JSONObject args) throws Exception {
        int id = selected(false);
        for (java.util.Iterator<String> keys = args.keys(); keys.hasNext();) {
            String key = keys.next();
            if (!java.util.Arrays.asList("since", "afterId", "limit", "senders").contains(key)) throw new Failure("invalid_args", "短信查询字段无效");
        }
        long since = args.has("since") ? number(args, "since", 0, Long.MAX_VALUE) : System.currentTimeMillis() - 86_400_000L;
        long after = args.has("afterId") ? number(args, "afterId", 0, Long.MAX_VALUE) : 0;
        int limit = args.has("limit") ? integer(args, "limit", 1, 50) : 10;
        ArrayList<String> values = new ArrayList<>();
        values.add(Integer.toString(id)); values.add(Long.toString(since)); values.add(Long.toString(after));
        StringBuilder selection = new StringBuilder("sub_id=? AND type=1 AND date>=? AND _id>?");
        Set<String> allowed = null;
        if (args.has("senders")) {
            if (!(args.opt("senders") instanceof JSONArray)) throw new Failure("invalid_args", "短信发件号码列表无效");
            JSONArray senders = args.getJSONArray("senders");
            if (senders.length() > 50) throw new Failure("invalid_args", "短信白名单最多50个号码");
            allowed = new LinkedHashSet<>();
            for (int i = 0; i < senders.length(); i++) {
                if (!(senders.opt(i) instanceof String)) throw new Failure("invalid_args", "短信号码无效");
                allowed.add(SmsPolicy.normalizePhone(senders.getString(i)));
            }
            if (allowed.isEmpty()) return new JSONObject().put("subscriptionId", id).put("messages", new JSONArray()).put("truncated", false);
            Set<String> variants = new LinkedHashSet<>();
            for (String phone : allowed) { variants.add(phone); variants.add(phone.substring(3)); variants.add(phone.substring(1)); variants.add("00" + phone.substring(1)); }
            selection.append(" AND address IN (");
            for (String phone : variants) { if (!selection.toString().endsWith("(")) selection.append(','); selection.append('?'); values.add(phone); }
            selection.append(')');
        }
        JSONArray rows = new JSONArray();
        String[] projection = {"_id", "address", "body", "date", "sub_id"};
        // The provider applies SIM and optional sender filtering before any body is read.
        try (Cursor cursor = context.getContentResolver().query(Telephony.Sms.Inbox.CONTENT_URI, projection,
                selection.toString(), values.toArray(new String[0]), "_id ASC")) {
            if (cursor != null) while (cursor.moveToNext() && rows.length() < limit + 1) {
                eligibleSubscription(id); // A SIM switch/removal fails closed, including during a query.
                if (cursor.getInt(4) != id) continue;
                String sender = cursor.getString(1);
                if (allowed != null) {
                    try { sender = SmsPolicy.normalizePhone(sender); } catch (IllegalArgumentException ignored) { continue; }
                    if (!allowed.contains(sender)) continue;
                }
                String text = cursor.getString(2);
                rows.put(new JSONObject().put("id", Long.toString(cursor.getLong(0))).put("sender", sender)
                        .put("body", text == null ? "" : text.substring(0, Math.min(4000, text.length())))
                        .put("receivedAt", cursor.getLong(3)).put("subscriptionId", id));
            }
        }
        boolean truncated = rows.length() > limit;
        if (truncated) rows.remove(limit);
        return new JSONObject().put("subscriptionId", id).put("messages", rows).put("truncated", truncated);
    }
    @SuppressLint("MissingPermission") // selected(true) checks SEND_SMS; the explicit domestic subscription is rechecked before sending.
    JSONObject send(JSONObject args) throws Exception {
        int id = selected(true);
        if (integer(args, "subscriptionId", 0, Integer.MAX_VALUE) != id) throw new Failure("sim_blocked", "发送必须明确使用当前所选国内 SIM");
        for (java.util.Iterator<String> keys = args.keys(); keys.hasNext();) {
            if (!java.util.Arrays.asList("subscriptionId", "to", "text", "clientRequestId").contains(keys.next())) throw new Failure("invalid_args", "短信发送字段无效");
        }
        if (!(args.opt("to") instanceof String) || !(args.opt("text") instanceof String) || !(args.opt("clientRequestId") instanceof String))
            throw new Failure("invalid_args", "短信发送参数无效");
        String to = SmsPolicy.normalizePhone(args.getString("to")), text = args.getString("text"), requestId = args.getString("clientRequestId");
        if (!requestId.matches("[A-Za-z0-9][A-Za-z0-9._-]{0,127}") || text.trim().isEmpty() || text.length() > 2000)
            throw new Failure("invalid_args", "短信内容或请求编号无效");
        String digest = Base64.encodeToString(MessageDigest.getInstance("SHA-256").digest((id + "\n" + to + "\n" + text).getBytes(StandardCharsets.UTF_8)), Base64.NO_WRAP);
        synchronized (LOCK) {
            if (selected(true) != id) throw new Failure("sim_blocked", "短信设置已变化，请重新检查国内 SIM 卡");
            JSONObject old = loadRecord(requestId);
            if (old != null) {
                if (!digest.equals(old.optString("digest"))) throw new Failure("id_conflict", "同一短信请求编号不能用于不同内容");
                return publicSend(requestId, old);
            }
            SmsManager manager = Build.VERSION.SDK_INT >= 31 ? context.getSystemService(SmsManager.class).createForSubscriptionId(id) : SmsManager.getSmsManagerForSubscriptionId(id);
            ArrayList<String> parts = manager.divideMessage(text);
            if (parts.isEmpty() || parts.size() > 3) throw new Failure("message_too_long", "每条指令或结果最多发送3段短信");
            JSONObject record = new JSONObject().put("digest", digest).put("status", "queued").put("parts", parts.size())
                    .put("subscriptionId", id).put("createdAt", System.currentTimeMillis()).put("sentParts", new JSONArray()).put("deliveredParts", new JSONArray());
            saveRecord(requestId, record);
            ArrayList<PendingIntent> sent = new ArrayList<>(), delivered = new ArrayList<>();
            for (int index = 0; index < parts.size(); index++) {
                sent.add(resultIntent(requestId, index, false)); delivered.add(resultIntent(requestId, index, true));
            }
            try {
                eligibleSubscription(id);
                if (parts.size() == 1) manager.sendTextMessage(to, null, parts.get(0), sent.get(0), delivered.get(0));
                else manager.sendMultipartTextMessage(to, null, parts, sent, delivered);
            } catch (Exception error) {
                record.put("status", error instanceof SecurityException ? "failed" : "unknown");
                saveRecord(requestId, record);
            }
            return publicSend(requestId, record);
        }
    }
    private PendingIntent resultIntent(String id, int part, boolean delivery) {
        Intent intent = new Intent(context, SmsResultReceiver.class).setAction(delivery ? "io.daxqo.SMS_DELIVERED" : "io.daxqo.SMS_SENT")
                .setData(Uri.parse("daxqo-sms://result/" + Uri.encode(id) + "/" + part + "/" + delivery))
                .putExtra("requestId", id).putExtra("part", part).putExtra("delivery", delivery);
        int mutable = delivery ? (Build.VERSION.SDK_INT >= 31 ? PendingIntent.FLAG_MUTABLE : 0) : PendingIntent.FLAG_IMMUTABLE;
        return PendingIntent.getBroadcast(context, 0, intent, PendingIntent.FLAG_UPDATE_CURRENT | mutable);
    }
    JSONObject sendStatus(String id) throws Exception {
        if (!id.matches("[A-Za-z0-9][A-Za-z0-9._-]{0,127}")) throw new Failure("invalid_args", "短信请求编号无效");
        synchronized (LOCK) {
            JSONObject record = loadRecord(id);
            if (record == null) throw new Failure("not_found", "短信请求不存在");
            return publicSend(id, record);
        }
    }
    private JSONObject publicSend(String id, JSONObject record) throws Exception {
        return new JSONObject().put("id", id).put("status", record.optString("status", "unknown"))
                .put("subscriptionId", record.optInt("subscriptionId", -1)).put("parts", record.optInt("parts", 0));
    }
    private SQLiteDatabase database() {
        // Called under LOCK. Retain compact replay tombstones without limiting the phone to a fixed lifetime send count.
        if (sendDatabase == null) sendDatabase = new SQLiteOpenHelper(context, "sms_sends.db", null, 1) {
            @Override public void onConfigure(SQLiteDatabase db) { db.execSQL("PRAGMA synchronous=FULL"); }
            @Override public void onCreate(SQLiteDatabase db) { db.execSQL("CREATE TABLE sends (id TEXT PRIMARY KEY NOT NULL, record TEXT NOT NULL)"); }
            @Override public void onUpgrade(SQLiteDatabase db, int oldVersion, int newVersion) { throw new IllegalStateException("Unsupported SMS journal version"); }
        };
        return sendDatabase.getWritableDatabase();
    }
    private JSONObject loadRecord(String id) throws Exception {
        try (Cursor cursor = database().query("sends", new String[] {"record"}, "id=?", new String[] {id}, null, null, null)) {
            return cursor.moveToFirst() ? new JSONObject(cursor.getString(0)) : null;
        }
    }
    private void saveRecord(String id, JSONObject record) throws Exception {
        ContentValues values = new ContentValues(); values.put("id", id); values.put("record", record.toString());
        if (database().insertWithOnConflict("sends", null, values, SQLiteDatabase.CONFLICT_REPLACE) == -1)
            throw new Failure("storage_unavailable", "无法保存短信发送记录，已停止发送");
    }
    void recordResult(Intent intent, int resultCode) {
        synchronized (LOCK) {
            try {
                String id = intent.getStringExtra("requestId");
                if (id == null) return;
                JSONObject record = loadRecord(id);
                if (record == null) return;
                int part = intent.getIntExtra("part", -1);
                if (part < 0 || part >= record.getInt("parts")) return;
                boolean delivery = intent.getBooleanExtra("delivery", false);
                if (delivery && resultCode == Activity.RESULT_OK) {
                    byte[] pdu = intent.getByteArrayExtra("pdu");
                    if (pdu == null) return; // A broadcast alone is not proof of carrier delivery.
                    String format = intent.getStringExtra("format");
                    SmsMessage receipt = SmsMessage.createFromPdu(pdu, format == null ? "3gpp" : format);
                    if (receipt == null) return;
                    int status = receipt.getStatus();
                    if (status >= 32 && status < 64) return;
                    if (status < 0 || status >= 64) resultCode = Activity.RESULT_CANCELED;
                }
                if (resultCode != Activity.RESULT_OK) record.put("status", "failed");
                else {
                    String field = delivery ? "deliveredParts" : "sentParts";
                    JSONArray done = record.getJSONArray(field);
                    boolean seen = false;
                    for (int i = 0; i < done.length(); i++) seen |= done.getInt(i) == part;
                    if (!seen) done.put(part);
                    if (record.getJSONArray("deliveredParts").length() == record.getInt("parts")) record.put("status", "delivered");
                    else if (record.getJSONArray("sentParts").length() == record.getInt("parts") && !"failed".equals(record.optString("status"))) record.put("status", "sent");
                }
                saveRecord(id, record);
            } catch (Exception ignored) { /* No phone numbers, SMS text or credentials in logs. */ }
        }
    }
}
