package io.daxqo.app;

/** Host-side regression coverage for UTF-16 text insertion planning. */
public final class TextInputRegression {
    private static void expect(String name, String expected, int expectedCursor,
                               String before, int start, int end, String incoming, String mode) {
        TextInputEdit result = TextInputEdit.plan(before, start, end, incoming, mode);
        check(name + " text", expected.equals(result.text), result.text);
        check(name + " cursor", expectedCursor == result.cursor, String.valueOf(result.cursor));
    }

    private static void rejects(String name, String before, int start, int end, String incoming, String mode) {
        try {
            TextInputEdit.plan(before, start, end, incoming, mode);
            throw new AssertionError(name + " should reject input");
        } catch (IllegalArgumentException expected) {
            // Expected invalid state: the editor must not be rewritten.
        }
    }

    private static void check(String name, boolean passed, String detail) {
        if (!passed) throw new AssertionError(name + " failed: " + detail);
    }

    public static void main(String[] args) {
        expect("cursor insertion", "甲中文乙", 3, "甲乙", 1, 1, "中文", "insert");
        expect("selected text replacement", "甲中文丙", 3, "甲乙丙", 1, 2, "中文", "insert");
        expect("reversed selection", "甲中文丙", 3, "甲乙丙", 2, 1, "中文", "insert");
        expect("emoji cursor", "A😀中文B", 5, "A😀B", 3, 3, "中文", "insert");

        String multiline = "中文\n第二行😀";
        expect("multiline emoji", "甲" + multiline + "乙", 1 + multiline.length(),
                "甲乙", 1, 1, multiline, "insert");
        expect("explicit whole-field replace", "新值", 2, "旧内容", -1, -1, "新值", "replace");
        rejects("unknown selection", "旧内容", -1, -1, "新值", "insert");
        rejects("selection splits surrogate pair", "A😀B", 2, 2, "中文", "insert");

        String atLimit = "😀".repeat(4000);
        check("8000 UTF-16 units accepted", TextInputEdit.validText(atLimit), "valid boundary rejected");
        expect("8000 UTF-16 units inserted", atLimit, atLimit.length(), "", 0, 0, atLimit, "insert");
        check("8001 UTF-16 units rejected", !TextInputEdit.validText(atLimit + "x"), "oversized text accepted");
        rejects("8001 UTF-16 units plan", "", 0, 0, atLimit + "x", "insert");
        check("unpaired high surrogate rejected", !TextInputEdit.validText("\uD800"), "high surrogate accepted");
        check("unpaired low surrogate rejected", !TextInputEdit.validText("\uDC00"), "low surrogate accepted");
        rejects("unpaired surrogate plan", "", 0, 0, "\uD800", "insert");

        System.out.println("TextInputRegression: all cases passed");
    }
}
