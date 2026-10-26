package io.daxqo.app;

import java.util.Locale;

/** Independent of full/confirm mode and of permissions supplied by callers. */
final class WechatPolicy {
    static final String BLOCKED = "wechat_blocked";
    static final String MESSAGE = "微信操作权限已关闭；未执行。只能由你在本机手机控制页阅读风险提示后开启。";
    static final String WARNING = "自动化操作微信可能触发平台风控，导致功能受限、账号封禁，甚至永久封号，也可能造成误发消息或隐私泄露。DAXQO 无法保证账号安全，也无法保证能够解封。\n\n请优先保持关闭。只有你已充分理解并愿意承担上述后果时，才继续开启。开启后仍受手机控制总开关和 Android 权限约束，可随时在本页关闭。";

    private WechatPolicy() {}

    static boolean protectedPackage(String name) {
        if (name == null) return false;
        String value = name.trim().toLowerCase(Locale.ROOT);
        // Android profiles/clones normally retain this package. Include child
        // packages/process-qualified identities, without matching unrelated QQ.
        return value.equals("com.tencent.mm") || value.startsWith("com.tencent.mm.")
                || value.startsWith("com.tencent.mm:");
    }

    static boolean launcherTarget(CharSequence label) {
        if (label == null) return false;
        String value = label.toString().toLowerCase(Locale.ROOT);
        return value.contains("微信") || value.contains("wechat") || value.contains("weixin")
                || value.contains("com.tencent.mm");
    }

    static String packages(boolean allowed, String... names) {
        if (!allowed) for (String name : names) if (protectedPackage(name)) return BLOCKED;
        return "";
    }
}
