package io.daxqo.app.platform;

/** Platform-specific setup guidance and known app-store ordering. */
public final class PlatformProfile {
    private static final String[] STANDARD_STORES = {
            "com.android.vending", "com.bbk.appstore", "com.heytap.market", "com.oppo.market",
            "com.huawei.appmarket", "com.sec.android.app.samsungapps", "com.xiaomi.market", "com.vivo.appstore",
            "com.stepos.appstore", "com.tencent.android.qqdownloader"
    };
    private static final String[] XM_STORES = {
            "com.xiaomi.market", "com.bbk.appstore", "com.vivo.appstore", "com.heytap.market",
            "com.oppo.market", "com.huawei.appmarket", "com.sec.android.app.samsungapps", "com.android.vending",
            "com.stepos.appstore", "com.tencent.android.qqdownloader"
    };
    public final String id;
    public final String setupGuidance;
    public final String setupButtonLabel;
    private final String[] stores;
    private PlatformProfile(String id, boolean xm) {
        this.id = id;
        this.setupGuidance = xm ? "澎湃 OS：请将 DAXQO 的省电策略设为“无限制”；在 DAXQO“权限管理 → 其他权限”中允许“锁屏显示”和“后台弹出界面”。请在系统设置中自行确认，本页不会自动更改授权。使用 Termux 承载运行环境时，Termux 也必须设为“无限制”并允许后台运行、保持会话；受限会导致本机服务中断。" : "";
        this.setupButtonLabel = xm ? "4  检查澎湃OS权限" : "4  检查应用设置";
        this.stores = (xm ? XM_STORES : STANDARD_STORES).clone();
    }
    public static PlatformProfile fromId(String id) {
        if ("standard".equals(id)) return new PlatformProfile("standard", false);
        if ("xm".equals(id)) return new PlatformProfile("xm", true);
        throw new IllegalArgumentException("Unknown platform profile: " + id);
    }
    public String[] appStoreCandidates() { return stores.clone(); }

    /** Returns known OEM stores first, preserving the selected profile's order for all others. */
    public String[] appStoreCandidates(String manufacturer) {
        String[] preferred = preferredStores(manufacturer);
        if (preferred.length == 0) return stores.clone();
        java.util.ArrayList<String> ordered = new java.util.ArrayList<>();
        for (String name : preferred) addIfPresent(ordered, name);
        for (String name : stores) {
            if (!"com.tencent.android.qqdownloader".equals(name)) addIfPresent(ordered, name);
        }
        addIfPresent(ordered, "com.tencent.android.qqdownloader");
        return ordered.toArray(new String[0]);
    }

    private void addIfPresent(java.util.List<String> output, String name) {
        for (String candidate : stores) {
            if (candidate.equals(name)) {
                if (!output.contains(name)) output.add(name);
                return;
            }
        }
    }

    private static String[] preferredStores(String manufacturer) {
        String name = manufacturer == null ? "" : manufacturer.toLowerCase(java.util.Locale.ROOT);
        if (name.contains("stepx") || name.contains("stepos")) return new String[]{"com.stepos.appstore"};
        if (name.contains("xiaomi") || name.contains("redmi") || name.contains("poco")) return new String[]{"com.xiaomi.market"};
        if (name.contains("vivo")) return new String[]{"com.vivo.appstore", "com.bbk.appstore"};
        if (name.contains("bbk")) return new String[]{"com.bbk.appstore", "com.vivo.appstore"};
        if (name.contains("oppo") || name.contains("realme") || name.contains("oneplus")) return new String[]{"com.heytap.market", "com.oppo.market"};
        if (name.contains("huawei")) return new String[]{"com.huawei.appmarket"};
        if (name.contains("honor")) return new String[]{"com.huawei.appmarket"};
        if (name.contains("samsung")) return new String[]{"com.sec.android.app.samsungapps"};
        return new String[0];
    }
}
