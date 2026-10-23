import io.daxqo.app.platform.PlatformProfile;

public final class PlatformProfileRegression {
    public static void main(String[] args) {
        PlatformProfile standard = PlatformProfile.fromId("standard");
        PlatformProfile xm = PlatformProfile.fromId("xm");
        check("xm".equals(xm.id) && "standard".equals(standard.id), "profile ids");
        check("com.android.vending".equals(standard.appStoreCandidates()[0]), "Standard store order is neutral");
        check("com.xiaomi.market".equals(xm.appStoreCandidates()[0]), "XM store is first");
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
