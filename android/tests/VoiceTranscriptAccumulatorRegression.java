package io.daxqo.app;

/** Host-side regression coverage for revisable partials and committed endpoints. */
public final class VoiceTranscriptAccumulatorRegression {
    private static int checks;

    private static void equal(String expected, String actual, String label) {
        checks++;
        if (!expected.equals(actual)) {
            throw new AssertionError(label + ": expected <" + expected + "> but was <" + actual + ">");
        }
    }

    public static void main(String[] args) {
        VoiceTranscriptAccumulator transcript = new VoiceTranscriptAccumulator();
        equal("你", transcript.hypothesis("你"), "first partial");
        equal("拟好", transcript.hypothesis("拟好"), "revised partial replaces earlier hypothesis");
        equal("拟好", transcript.commitEndpoint("拟好"), "endpoint commits segment");
        equal("拟好世界", transcript.hypothesis("世界"), "next partial keeps committed prefix");
        equal("拟好世界", transcript.finish("世界"), "final transcript combines both segments");

        VoiceTranscriptAccumulator empty = new VoiceTranscriptAccumulator();
        equal("", empty.finish("  "), "whitespace-only final remains empty");
        equal("后续", empty.finish("后续"), "unsegmented final is preserved");
        System.out.println("VoiceTranscriptAccumulatorRegression: " + checks + " checks passed");
    }
}
