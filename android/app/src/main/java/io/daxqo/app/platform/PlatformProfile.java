package io.daxqo.app.platform;

/** Platform-specific setup guidance and known app-store ordering. */
public final class PlatformProfile {
    private static final String[] STANDARD_STORES = {
            "com.android.vending", "com.bbk.appstore", "com.heytap.market", "com.oppo.market",
            "com.huawei.appmarket", "com.sec.android.app.samsungapps", "com.xiaomi.market", "com.vivo.appstore",
            "com.tencent.android.qqdownloader"
    };
    private static final String[] XM_STORES = {
            "com.xiaomi.market", "com.bbk.appstore", "com.vivo.appstore", "com.heytap.market",
            "com.oppo.market", "com.huawei.appmarket", "com.sec.android.app.samsungapps", "com.android.vending"
    };
    public final String id;
    public final String setupGuidance;
    public final String setupButtonLabel;
    private final String[] stores;
    private PlatformProfile(String id, boolean xm) {
        this.id = id;
        this.setupGuidance = xm ? "澎湃 OS：请将 DAXQO 的省电策略设为“无限制”；在 DAXQO“权限管理 → 其他权限”中允许“锁屏显示”和“后台弹出界面”。请在系统设置中自行确认，本页不会自动更改授权。" : "";
        this.setupButtonLabel = xm ? "4  检查澎湃OS权限" : "4  检查应用设置";
        this.stores = (xm ? XM_STORES : STANDARD_STORES).clone();
    }
    public static PlatformProfile fromId(String id) {
        if ("standard".equals(id)) return new PlatformProfile("standard", false);
        if ("xm".equals(id)) return new PlatformProfile("xm", true);
        throw new IllegalArgumentException("Unknown platform profile: " + id);
    }
    public String[] appStoreCandidates() { return stores.clone(); }
}
