package io.daxqo.app;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.HashMap;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.atomic.AtomicInteger;

// Executes the app's compiled grant ledger and policy helpers, without Android
// instrumentation or a phone. Android stub methods must never be invoked.
public final class ApprovalLedgerRegression {
    private static int assertions;
    private static void check(boolean condition, String message) {
        assertions++;
        if (!condition) throw new AssertionError(message);
    }
    private static ApprovalLedger.Result attempt(ApprovalLedger ledger, String operation, String page, long now) {
        return ledger.check(operation, page, "tap", "com.android.settings", "点击：操作系统授权界面", now);
    }
    private static void code(ApprovalLedger.Result result, String expected) {
        check(expected.equals(result.code), "expected " + expected + ", got " + result.code);
    }
    public static void main(String[] args) throws Exception {
        ApprovalLedger ledger = new ApprovalLedger();
        ApprovalLedger.Result pending = attempt(ledger, "args-sha-A", "page-sha-A", 100);
        code(pending, "approval_required");
        String id = pending.entry.id;
        long deadline = pending.entry.deadline;
        check(id.equals(attempt(ledger, "args-sha-A", "page-sha-A", 101).entry.id), "same operation must reuse pending id");
        ApprovalLedger.Result firstAccept = ledger.decide(id, true, 102);
        code(firstAccept, ""); check(firstAccept.newlyApproved, "only pending-to-approved may return owner GUI to target");
        ApprovalLedger.Result repeatAccept = ledger.decide(id, true, 103);
        code(repeatAccept, ""); check(!repeatAccept.newlyApproved, "repeat accept cannot move the GUI again");
        check(deadline == pending.entry.deadline, "repeated accept cannot extend TTL");
        code(attempt(ledger, "args-sha-A", "page-sha-A", 104), "");
        check("consumed".equals(pending.entry.status), "approval must be consumed");
        code(ledger.decide(id, true, 105), "stale_approval");
        code(ledger.decide(id, false, 105), "stale_approval");
        ApprovalLedger.Result next = attempt(ledger, "args-sha-A", "page-sha-A", 106);
        code(next, "approval_required");
        check(!id.equals(next.entry.id), "consumed grant must not replay");

        ApprovalLedger.Result decline = ledger.decide(next.entry.id, false, 107);
        code(decline, ""); check(!decline.newlyApproved, "decline cannot move the GUI");
        code(ledger.decide(next.entry.id, false, 108), "");
        code(ledger.decide(next.entry.id, true, 109), "stale_approval");
        code(attempt(ledger, "args-sha-A", "page-sha-A", 110), "action_denied");
        code(attempt(ledger, "args-sha-A", "page-sha-B", 111), "stale_approval");
        ApprovalLedger.Result changed = attempt(ledger, "args-sha-A", "page-sha-B", 112);
        code(changed, "approval_required");
        code(ledger.decide(changed.entry.id, true, 113), "");
        code(attempt(ledger, "args-sha-A", "page-sha-C", 114), "stale_approval");
        code(attempt(ledger, "args-sha-A", "page-sha-B", 115), "approval_required");
        code(ledger.decide(changed.entry.id, true, 116), "stale_approval");

        ledger = new ApprovalLedger();
        pending = attempt(ledger, "password-hash-A", "page", 1000);
        code(ledger.decide(pending.entry.id, true, 1001), "");
        ApprovalLedger.Result differentArgs = attempt(ledger, "password-hash-B", "page", 1002);
        code(differentArgs, "approval_required");
        check(!differentArgs.entry.id.equals(pending.entry.id), "different text/args must not borrow a grant");
        code(ledger.decide(pending.entry.id, true, 1003), "stale_approval");
        code(attempt(ledger, "password-hash-A", "page", 1004), "approval_required");

        ledger = new ApprovalLedger();
        pending = attempt(ledger, "args", "page", 10);
        code(ledger.decide(pending.entry.id, true, 11), "");
        code(attempt(ledger, "args", "page", 10 + ApprovalLedger.TTL_MS), "stale_approval");
        check("expired".equals(pending.entry.status), "exact deadline expires grant");
        code(ledger.decide(pending.entry.id, true, 10 + ApprovalLedger.TTL_MS), "stale_approval");
        code(attempt(ledger, "args", "page", 11 + ApprovalLedger.TTL_MS), "approval_required");
        ledger.revoke();
        code(attempt(ledger, "args", "page", 12 + ApprovalLedger.TTL_MS), "stale_approval");
        code(new ApprovalLedger().decide(pending.entry.id, true, 12), "stale_approval");

        // Entering either owner GUI causes only GET/status and then a decision.
        // These APIs deliberately accept no current-page argument; the original
        // page is checked only when the tool actually retries its mutation.
        ApprovalLedger ownerGui = new ApprovalLedger();
        ApprovalLedger.Result originalPage = attempt(ownerGui, "args", "target-app-page", 200);
        for (int i = 0; i < 20; i++) {
            check("pending".equals(ownerGui.list(201 + i).get(0).status), "owner GUI polling must not stale pending consent");
        }
        code(ownerGui.decide(originalPage.entry.id, true, 222), "");
        check("approved".equals(ownerGui.list(223).get(0).status), "owner GUI polling preserves approved consent");
        code(attempt(ownerGui, "args", "target-app-page", 224), "");
        code(ownerGui.decide(originalPage.entry.id, true, 225), "stale_approval");
        ApprovalLedger.Result changedTarget = attempt(ownerGui, "args", "target-app-page", 226);
        code(ownerGui.decide(changedTarget.entry.id, true, 227), "");
        code(attempt(ownerGui, "args", "changed-target-app-page", 228), "stale_approval");

        // The actual page identity helper binds semantic content, without a
        // runtime Android window id that can change after a lock/wake cycle.
        String settingsContent = "app=io.daxqo.app;button=权限;enabled=true;checked=false;bounds=0,100,400,300";
        String beforeSleep = DeviceAccessibilityService.pageIdentity("com.android.settings", settingsContent);
        String afterWake = DeviceAccessibilityService.pageIdentity("com.android.settings", settingsContent);
        ApprovalLedger sleepingPhone = new ApprovalLedger();
        ApprovalLedger.Result sleepPending = attempt(sleepingPhone, "back{}", beforeSleep, 1);
        code(sleepingPhone.decide(sleepPending.entry.id, true, 1000), "");
        code(attempt(sleepingPhone, "back{}", afterWake, 1001), "");
        check(!beforeSleep.equals(DeviceAccessibilityService.pageIdentity("different.package", settingsContent)), "package remains bound after window-id removal");
        check(!beforeSleep.equals(DeviceAccessibilityService.pageIdentity("com.android.settings", settingsContent.replace("checked=false", "checked=true"))), "permission toggle remains bound");
        check(!beforeSleep.equals(DeviceAccessibilityService.pageIdentity("com.android.settings", settingsContent.replace("enabled=true", "enabled=false"))), "enabled state remains bound");
        String installTarget = DeviceAccessibilityService.pageIdentity("com.android.packageinstaller", "app=华住会;button=安装");
        check(!installTarget.equals(DeviceAccessibilityService.pageIdentity("com.android.packageinstaller", "app=其他应用;button=安装")), "installer target remains bound");
        String payment = DeviceAccessibilityService.pageIdentity("payment.app", "payee=商家A;amount=12.34;button=确认支付");
        check(!payment.equals(DeviceAccessibilityService.pageIdentity("payment.app", "payee=商家A;amount=12.35;button=确认支付")), "payment amount remains bound");
        check(!payment.equals(DeviceAccessibilityService.pageIdentity("payment.app", "payee=商家B;amount=12.34;button=确认支付")), "payment recipient remains bound");

        Map<String, String> inputFields = new HashMap<>();
        inputFields.put("nodeId", "\"0.1.2\""); inputFields.put("snapshotId", "\"snapshot-before\"");
        inputFields.put("text", "\"测试输入\""); inputFields.put("expectedPackage", "\"target.app\"");
        String inputBefore = DeviceAccessibilityService.operationIdentity("input", inputFields, true);
        String unvalidatedBefore = DeviceAccessibilityService.operationIdentity("input", inputFields, false);
        inputFields.put("snapshotId", "\"snapshot-after\"");
        String inputAfter = DeviceAccessibilityService.operationIdentity("input", inputFields, true);
        check(inputBefore.equals(inputAfter), "validated input after fresh observation must reuse operation identity");
        check(!unvalidatedBefore.equals(DeviceAccessibilityService.operationIdentity("input", inputFields, false)), "snapshot omission is limited to validated node input");
        ApprovalLedger inputLedger = new ApprovalLedger();
        ApprovalLedger.Result inputPending = inputLedger.check(inputBefore, "same-page", "input", "target.app", "输入文本", 10);
        check(inputPending.entry.id.equals(inputLedger.check(inputAfter, "same-page", "input", "target.app", "输入文本", 11).entry.id), "fresh snapshot reuses pending id");
        code(inputLedger.decide(inputPending.entry.id, true, 12), "");
        code(inputLedger.check(inputAfter, "same-page", "input", "target.app", "输入文本", 13), "");
        for (String field : new String[] {"nodeId", "text", "expectedPackage"}) {
            String old = inputFields.put(field, "\"changed\"");
            check(!inputBefore.equals(DeviceAccessibilityService.operationIdentity("input", inputFields, true)), "input still binds " + field);
            inputFields.put(field, old);
        }
        inputFields.remove("nodeId");
        String focusedBefore = DeviceAccessibilityService.operationIdentity("input", inputFields, false);
        inputFields.put("snapshotId", "\"third-snapshot\"");
        check(!focusedBefore.equals(DeviceAccessibilityService.operationIdentity("input", inputFields, false)), "unaddressed input keeps its original arguments");
        String focusA = DeviceAccessibilityService.pageIdentity("target.app", "nodeA.inputFocused=true;nodeB.inputFocused=false");
        String focusB = DeviceAccessibilityService.pageIdentity("target.app", "nodeA.inputFocused=false;nodeB.inputFocused=true");
        check(!focusA.equals(focusB), "unaddressed input page binding distinguishes target focus");
        ApprovalLedger focusedInput = new ApprovalLedger();
        ApprovalLedger.Result focusPending = focusedInput.check(focusedBefore, focusA, "input", "target.app", "输入文本", 100);
        code(focusedInput.decide(focusPending.entry.id, true, 101), "");
        code(focusedInput.check(focusedBefore, focusB, "input", "target.app", "输入文本", 102), "stale_approval");

        Map<String, String> tapNodeFields = new HashMap<>();
        tapNodeFields.put("nodeId", "\"0.1.2.3\"");
        tapNodeFields.put("snapshotId", "\"snapshot-before\"");
        tapNodeFields.put("expectedPackage", "\"com.xiaomi.market\"");
        String tapNodeBefore = DeviceAccessibilityService.operationIdentity("tap_node", tapNodeFields, true);
        String unvalidatedTapBefore = DeviceAccessibilityService.operationIdentity("tap_node", tapNodeFields, false);
        tapNodeFields.put("snapshotId", "\"snapshot-after\"");
        String tapNodeAfter = DeviceAccessibilityService.operationIdentity("tap_node", tapNodeFields, true);
        check(tapNodeBefore.equals(tapNodeAfter), "validated tap_node may reuse approval across fresh snapshots");
        check(!unvalidatedTapBefore.equals(DeviceAccessibilityService.operationIdentity("tap_node", tapNodeFields, false)),
                "unvalidated tap_node identity must retain snapshotId");
        ApprovalLedger tapNodeLedger = new ApprovalLedger();
        ApprovalLedger.Result tapNodePending = tapNodeLedger.check(tapNodeBefore, "same-page", "tap_node",
                "com.xiaomi.market", "点击选中的界面元素", 20);
        ApprovalLedger.Result tapNodeRepeat = tapNodeLedger.check(tapNodeAfter, "same-page", "tap_node",
                "com.xiaomi.market", "点击选中的界面元素", 21);
        check(tapNodePending.entry.id.equals(tapNodeRepeat.entry.id), "snapshot change reuses same tap_node approval");
        code(tapNodeLedger.decide(tapNodePending.entry.id, true, 22), "");
        check(!tapNodeBefore.equals(DeviceAccessibilityService.operationIdentity("tap_node", new HashMap<String, String>() {{
            put("nodeId", "\"0.1.2.4\""); put("snapshotId", "\"snapshot-after\"");
            put("expectedPackage", "\"com.xiaomi.market\"");
        }}, true)), "tap_node still binds nodeId");
        check(!tapNodeBefore.equals(DeviceAccessibilityService.operationIdentity("tap", tapNodeFields, true)),
                "tap_node identity differs from coordinate tap action");

        for (int i = 0; i < 100; i++) attempt(ledger, "operation-" + i, "page", 1000000 + i);
        check(ledger.list(1000100).size() == 16, "history must remain bounded");
        check(ledger.list(1000100).stream().filter(e -> "pending".equals(e.status) || "approved".equals(e.status)).count() == 1, "only one actionable grant");

        final ApprovalLedger concurrent = new ApprovalLedger();
        pending = attempt(concurrent, "args", "page", 1);
        code(concurrent.decide(pending.entry.id, true, 2), "");
        CountDownLatch start = new CountDownLatch(1);
        AtomicInteger allowed = new AtomicInteger();
        List<Thread> threads = new ArrayList<>();
        for (int i = 0; i < 64; i++) {
            Thread thread = new Thread(() -> {
                try { start.await(); } catch (InterruptedException error) { throw new AssertionError(error); }
                if (attempt(concurrent, "args", "page", 3).code.isEmpty()) allowed.incrementAndGet();
            });
            threads.add(thread); thread.start();
        }
        start.countDown();
        for (Thread thread : threads) thread.join();
        check(allowed.get() == 1, "concurrent retries may consume exactly once");
        for (int mask = 0; mask < 64; mask++) {
            boolean actual = DeviceState.mayDismissKeyguard((mask & 1) != 0, (mask & 2) != 0, (mask & 4) != 0, (mask & 8) != 0, (mask & 16) != 0, (mask & 32) != 0);
            check(actual == (mask == 13), "secure/paused/unknown/unlocked wake gate: " + mask);
            check(DeviceState.mayRecordApproval((mask & 1) != 0, (mask & 2) != 0) == ((mask & 3) == 1), "recording consent ignores sleep/secure/unknown lock state but respects manual pause: " + mask);
        }
        DeviceState.LockState unknown = DeviceState.lockState(null);
        check(!unknown.known && unknown.locked && unknown.secureLock && unknown.deviceSecure, "unknown lock state fails closed");
        check(DeviceAccessibilityService.stableLabel("12:34").equals(DeviceAccessibilityService.stableLabel("12:35")), "clock must not cause spurious stale approval");
        check(DeviceAccessibilityService.stableLabel("5秒后").equals(DeviceAccessibilityService.stableLabel("4秒后")), "countdown must not cause spurious stale approval");
        check(!DeviceAccessibilityService.stableLabel("支付 ¥12.34").equals(DeviceAccessibilityService.stableLabel("支付 ¥12.35")), "payment amount must remain bound");
        check(!DeviceAccessibilityService.stableLabel("华住会").equals(DeviceAccessibilityService.stableLabel("其他应用")), "install target must remain bound");
        for (String mode : new String[] {"confirm", "full", "invalid"}) {
            for (String action : new String[] {"status", "tree", "screenshot"}) {
                check(!DeviceState.requiresApproval(mode, action, "系统授权或密码"), "read-only observation cannot require sensitive consent");
            }
            for (String action : new String[] {"tap", "input", "open_app", "swipe", "back", "home", "long_press"}) {
                check(DeviceState.requiresApproval(mode, action, "系统授权或密码") == !"full".equals(mode), "full vs confirm mutation policy");
                check(!DeviceState.requiresApproval(mode, action, ""), "ordinary operations remain autonomous");
            }
        }
        for (int mask = 0; mask < 16; mask++) {
            check(MainActivity.mayReturnToTarget((mask & 1) != 0, (mask & 2) != 0, (mask & 4) != 0, (mask & 8) != 0) == (mask == 7), "GUI continuation requires visible trusted focused non-closing MainActivity: " + mask);
        }
        for (int mask = 0; mask < 4; mask++) {
            check(MainActivity.shouldRetryConnection((mask & 1) != 0, (mask & 2) != 0) == (mask == 0), "resume retries only a disconnected, idle GUI: " + mask);
        }
        java.lang.reflect.Method continuation = MainActivity.class.getDeclaredMethod("returnToTargetAfterApproval");
        check(!java.lang.reflect.Modifier.isPublic(continuation.getModifiers()), "owner GUI continuation must not be public");
        check(continuation.getAnnotation(android.webkit.JavascriptInterface.class) == null, "owner GUI continuation must not be a JS bridge");
        System.out.println("Approval and wake regressions passed: " + assertions + " assertions, 64 concurrent retries.");
    }
}
