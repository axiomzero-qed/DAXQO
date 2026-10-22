package io.daxqo.app;

import java.util.ArrayList;
import java.util.List;
import java.util.UUID;

// Process-local grants intentionally do not survive a service/process restart.
// Only hashes and fixed summaries are retained; never raw action arguments.
final class ApprovalLedger {
    static final long TTL_MS = 10 * 60 * 1000;
    private static final int LIMIT = 16;
    static final class Entry {
        final String id = UUID.randomUUID().toString();
        final String operation, page, action, packageName, summary;
        final long expiresAt, deadline;
        String status = "pending";
        boolean staleReported;
        Entry(String operation, String page, String action, String packageName, String summary, long now) {
            this.operation = operation; this.page = page; this.action = action;
            this.packageName = packageName; this.summary = summary;
            deadline = now + TTL_MS; expiresAt = System.currentTimeMillis() + TTL_MS;
        }
    }
    static final class Result {
        final String code;
        final Entry entry;
        final boolean newlyApproved;
        Result(String code, Entry entry) { this(code, entry, false); }
        Result(String code, Entry entry, boolean newlyApproved) { this.code = code; this.entry = entry; this.newlyApproved = newlyApproved; }
    }
    private final List<Entry> entries = new ArrayList<>();
    private void expire(long now) {
        for (Entry entry : entries) if (now >= entry.deadline && ("pending".equals(entry.status) || "approved".equals(entry.status))) entry.status = "expired";
    }
    synchronized List<Entry> list(long now) { expire(now); return new ArrayList<>(entries); }
    synchronized void revoke() {
        for (Entry entry : entries) if ("pending".equals(entry.status) || "approved".equals(entry.status)) entry.status = "revoked";
    }
    synchronized Result decide(String id, boolean accept, long now) {
        expire(now);
        for (Entry entry : entries) if (entry.id.equals(id)) {
            if ("pending".equals(entry.status)) {
                entry.status = accept ? "approved" : "declined";
                return new Result("", entry, accept);
            }
            else if (!(accept && "approved".equals(entry.status)) && !(!accept && "declined".equals(entry.status))) return new Result("stale_approval", entry);
            return new Result("", entry);
        }
        return new Result("stale_approval", null);
    }
    synchronized Result check(String operation, String page, String action, String packageName, String summary, long now) {
        expire(now);
        for (int i = entries.size() - 1; i >= 0; i--) {
            Entry entry = entries.get(i);
            if (!entry.operation.equals(operation)) continue;
            if (("pending".equals(entry.status) || "approved".equals(entry.status) || "declined".equals(entry.status)) && !entry.page.equals(page)) {
                entry.status = "stale"; entry.staleReported = true;
                return new Result("stale_approval", entry);
            }
            if ("declined".equals(entry.status) && now < entry.deadline) return new Result("action_denied", entry);
            if ("approved".equals(entry.status)) { entry.status = "consumed"; return new Result("", entry); }
            if ("pending".equals(entry.status)) return new Result("approval_required", entry);
            if (("expired".equals(entry.status) || "revoked".equals(entry.status)) && !entry.staleReported) {
                entry.staleReported = true; return new Result("stale_approval", entry);
            }
            break;
        }
        // At most one actionable grant; a different request supersedes it.
        for (Entry entry : entries) if ("pending".equals(entry.status) || "approved".equals(entry.status)) entry.status = "superseded";
        Entry entry = new Entry(operation, page, action, packageName, summary, now);
        entries.add(entry);
        if (entries.size() > LIMIT) entries.remove(0);
        return new Result("approval_required", entry);
    }
}
