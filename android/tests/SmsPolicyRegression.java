package io.daxqo.app;

public final class SmsPolicyRegression {
    private static int checks;
    private static void check(boolean value) { if (!value) throw new AssertionError("SMS check " + checks); checks++; }
    public static void main(String[] args) {
        check(SmsPolicy.eligibleSim("460", "cn"));
        check(SmsPolicy.eligibleSim("460", "CN"));
        for (String[] sim : new String[][] {{"234", "gb"}, {"235", "gb"}, {"234", "cn"},
                {"460", "gb"}, {"", "cn"}, {"460", ""}, {null, "cn"}, {"460", null}})
            check(!SmsPolicy.eligibleSim(sim[0], sim[1]));
        for (String phone : new String[] {"13800138000", "+8613800138000", "008613800138000", "86 138-0013-8000"})
            check("+8613800138000".equals(SmsPolicy.normalizePhone(phone)));
        for (String phone : new String[] {"+447700900000", "00447700900000", "106900", "10086", "13800138000;SEND", "", "+8612800138000"}) {
            boolean denied = false;
            try { SmsPolicy.normalizePhone(phone); } catch (IllegalArgumentException expected) { denied = true; }
            check(denied);
        }
        check(SmsPolicy.sameNumber("13800138000", "+86 13800138000"));
        check(!SmsPolicy.sameNumber("13800138000", "+447700900000"));
        System.out.println("SMS policy: " + checks + " checks passed");
    }
}
