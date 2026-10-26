package io.daxqo.app;

public final class WechatPolicyRegression {
    private static int checks;
    private static void check(boolean value, String label) {
        checks++;
        if (!value) throw new AssertionError(label);
    }

    public static void main(String[] args) {
        check(WechatPolicy.protectedPackage("com.tencent.mm"), "official WeChat package");
        check(WechatPolicy.protectedPackage("com.tencent.mm.profile"), "profile clone");
        check(WechatPolicy.protectedPackage("com.tencent.mm:tools"), "process suffix");
        check(WechatPolicy.protectedPackage(" COM.TENCENT.MM "), "case and whitespace");
        check(!WechatPolicy.protectedPackage("com.tencent.mobileqq"), "QQ is unrelated");
        check(!WechatPolicy.protectedPackage("org.example.tencent.mm"), "similar unrelated package");
        check(!WechatPolicy.protectedPackage("com.tencent.mmclone"), "prefix lookalike");
        check(WechatPolicy.packages(false, "com.example.app", "com.tencent.mm").equals(WechatPolicy.BLOCKED), "disabled blocks target");
        check(WechatPolicy.packages(true, "com.tencent.mm").isEmpty(), "enabled allows target");
        check(WechatPolicy.packages(false, "com.tencent.mobileqq").isEmpty(), "disabled allows QQ");
        check(WechatPolicy.launcherTarget("微信测试入口"), "Chinese label");
        check(WechatPolicy.launcherTarget("WeChat test"), "English WeChat label");
        check(WechatPolicy.launcherTarget("Weixin Test"), "Weixin label");
        check(WechatPolicy.launcherTarget("com.tencent.mm.daxqofixture"), "package text label");
        check(!WechatPolicy.launcherTarget("QQ Messenger"), "QQ launcher label unaffected");
        System.out.println("Wechat policy: " + checks + " checks passed");
    }
}
