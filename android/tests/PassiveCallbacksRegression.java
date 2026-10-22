package io.daxqo.app;

import android.view.accessibility.AccessibilityEvent;
import java.lang.reflect.Field;
import sun.misc.Unsafe;

/**
 * Host regression over the actual compiled service, not source-text assertions.
 * Android's host SDK methods throw if invoked. Allocating without lifecycle
 * constructors lets these deliberately passive callbacks run on the host; any
 * attempt to access preferences or schedule Android work fails the test.
 * This does not replace real-device checks of permissions or active tool gates.
 */
public final class PassiveCallbacksRegression {
    public static void main(String[] args) throws Exception {
        Field singleton = Unsafe.class.getDeclaredField("theUnsafe");
        singleton.setAccessible(true);
        Unsafe allocator = (Unsafe) singleton.get(null);
        DeviceAccessibilityService service = (DeviceAccessibilityService)
                allocator.allocateInstance(DeviceAccessibilityService.class);
        AccessibilityEvent event = (AccessibilityEvent) allocator.allocateInstance(AccessibilityEvent.class);
        service.lastAction = "existing agent action";
        service.lastError = "existing protection reason";
        DeviceAccessibilityService.current = service;
        try {
            for (int i = 0; i < 1000; i++) {
                service.onAccessibilityEvent(i % 2 == 0 ? event : null);
                service.onInterrupt();
            }
            if (!"existing agent action".equals(service.lastAction)
                    || !"existing protection reason".equals(service.lastError)
                    || DeviceAccessibilityService.current != service) {
                throw new AssertionError("Passive events changed existing control state");
            }
            System.out.println("PASS: 2000 passive callbacks performed no Android access or control-state changes");
        } finally { DeviceAccessibilityService.current = null; }
    }
}
