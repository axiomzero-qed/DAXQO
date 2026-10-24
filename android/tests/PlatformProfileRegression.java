import io.daxqo.app.platform.PlatformProfile;

public final class PlatformProfileRegression {
    public static void main(String[] args) {
        PlatformProfile standard = PlatformProfile.fromId("standard");
        PlatformProfile xm = PlatformProfile.fromId("xm");
        check("xm".equals(xm.id) && "standard".equals(standard.id), "profile ids");
        check("com.android.vending".equals(standard.appStoreCandidates()[0]), "Standard store order is neutral");
        check(java.util.Arrays.asList(standard.appStoreCandidates()).contains("com.tencent.android.qqdownloader"), "Standard includes verified Tencent app store");
        check("com.xiaomi.market".equals(xm.appStoreCandidates()[0]), "XM store is first");
        check("com.stepos.appstore".equals(standard.appStoreCandidates("StepOS")[0]), "StepOS store is first");
        check("com.stepos.appstore".equals(xm.appStoreCandidates("StepX")[0]), "XM includes StepX store");
        check("com.xiaomi.market".equals(standard.appStoreCandidates("Redmi")[0]), "Redmi uses Xiaomi store");
        check("com.vivo.appstore".equals(standard.appStoreCandidates("vivo")[0]), "vivo store is first");
        check("com.bbk.appstore".equals(standard.appStoreCandidates("BBK")[0]), "BBK store is first");
        check("com.heytap.market".equals(standard.appStoreCandidates("realme")[0]), "realme uses HeyTap first");
        check("com.heytap.market".equals(xm.appStoreCandidates("OnePlus")[0]), "OnePlus uses HeyTap first");
        check("com.huawei.appmarket".equals(standard.appStoreCandidates("HUAWEI")[0]), "Huawei store is first");
        check("com.huawei.appmarket".equals(xm.appStoreCandidates("HONOR")[0]), "Honor uses Huawei store");
        check("com.sec.android.app.samsungapps".equals(standard.appStoreCandidates("Samsung")[0]), "Samsung store is first");
        check("com.tencent.android.qqdownloader".equals(xm.appStoreCandidates("Xiaomi")[xm.appStoreCandidates("Xiaomi").length - 1]), "XM Tencent store is fallback");
        check("com.tencent.android.qqdownloader".equals(standard.appStoreCandidates("unknown")[standard.appStoreCandidates("unknown").length - 1]), "Standard Tencent store is fallback");
        String[] clone = standard.appStoreCandidates("StepOS");
        clone[0] = "tampered";
        check("com.stepos.appstore".equals(standard.appStoreCandidates("StepOS")[0]), "manufacturer candidates are cloned");
        check(java.util.Arrays.equals(standard.appStoreCandidates(), standard.appStoreCandidates("unknown")), "unknown manufacturer keeps profile order");
        check(!standard.setupGuidance.contains("澎湃") && !standard.setupGuidance.contains("锁屏显示"), "standard has no XM guidance");
        check(xm.setupGuidance.contains("后台弹出界面") && xm.setupGuidance.contains("锁屏显示"), "XM permissions guidance");
        check(!xm.setupGuidance.toLowerCase(java.util.Locale.ROOT).contains("termux"), "XM guidance is environment-neutral");
        boolean rejected = false;
        try { PlatformProfile.fromId("other"); } catch (IllegalArgumentException expected) { rejected = true; }
        check(rejected, "unknown profile rejected");
        System.out.println("PlatformProfile regression checks passed.");
    }
    private static void check(boolean value, String description) {
        if (!value) throw new AssertionError(description);
    }
}
