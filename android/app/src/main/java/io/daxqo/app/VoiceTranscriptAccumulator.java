package io.daxqo.app;

/** Keeps endpoint-committed CTC text while exposing a revisable cumulative hypothesis. */
final class VoiceTranscriptAccumulator {
    private final StringBuilder committed = new StringBuilder();

    String hypothesis(String current) {
        return committed.toString() + (current == null ? "" : current);
    }

    String commitEndpoint(String endpointText) {
        if (endpointText != null) committed.append(endpointText);
        return committed.toString();
    }

    String finish(String current) {
        return hypothesis(current).trim();
    }
}
