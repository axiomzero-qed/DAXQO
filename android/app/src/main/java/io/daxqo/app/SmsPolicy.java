package io.daxqo.app;

import java.util.Locale;

/** SMS routing rules shared by real Android access and host regressions. */
final class SmsPolicy {
    private SmsPolicy() { }
    static boolean eligibleSim(String mcc, String country) {
        return "460".equals(mcc) && country != null && "cn".equals(country.toLowerCase(Locale.ROOT));
    }
    static String normalizePhone(String value) {
        if (value == null || value.length() > 48 || !value.matches("[+0-9 ()\\-]+"))
            throw new IllegalArgumentException("请填写国内手机号码");
        String phone = value.replaceAll("[ ()\\-]", "");
        if (phone.startsWith("0086")) phone = "+86" + phone.substring(4);
        else if (phone.matches("86[1][3-9][0-9]{9}")) phone = "+" + phone;
        else if (phone.matches("1[3-9][0-9]{9}")) phone = "+86" + phone;
        if (!phone.matches("\\+861[3-9][0-9]{9}"))
            throw new IllegalArgumentException("短信任务当前只支持国内手机号码");
        return phone;
    }
    static boolean sameNumber(String a, String b) {
        try { return normalizePhone(a).equals(normalizePhone(b)); }
        catch (IllegalArgumentException ignored) { return false; }
    }
}
