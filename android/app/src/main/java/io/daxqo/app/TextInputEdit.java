package io.daxqo.app;

/** UTF-16 selection edits for editors without Android's input-connection bridge. */
final class TextInputEdit {
    static final int MAX_TEXT_UNITS = 8000;
    final String text;
    final int cursor;

    private TextInputEdit(String text, int cursor) { this.text = text; this.cursor = cursor; }

    static boolean validText(String text) {
        if (text == null || text.length() > MAX_TEXT_UNITS) return false;
        for (int i = 0; i < text.length(); i++) {
            char c = text.charAt(i);
            if (Character.isHighSurrogate(c)) {
                if (++i >= text.length() || !Character.isLowSurrogate(text.charAt(i))) return false;
            } else if (Character.isLowSurrogate(c)) return false;
        }
        return true;
    }

    static TextInputEdit plan(String before, int start, int end, String incoming, String mode) {
        if (!validText(incoming) || !("insert".equals(mode) || "replace".equals(mode)))
            throw new IllegalArgumentException("invalid input");
        if ("replace".equals(mode)) return new TextInputEdit(incoming, incoming.length());
        if (before == null) throw new IllegalArgumentException("unknown existing text");
        if (before.isEmpty() && start == -1 && end == -1) start = end = 0;
        int left = Math.min(start, end), right = Math.max(start, end);
        if (left < 0 || right > before.length() || splitsPair(before, left) || splitsPair(before, right))
            throw new IllegalArgumentException("unknown or invalid selection");
        if (before.length() - (right - left) + incoming.length() > 16000)
            throw new IllegalArgumentException("editor too large for fallback");
        return new TextInputEdit(before.substring(0, left) + incoming + before.substring(right), left + incoming.length());
    }

    private static boolean splitsPair(String text, int position) {
        return position > 0 && position < text.length()
                && Character.isHighSurrogate(text.charAt(position - 1)) && Character.isLowSurrogate(text.charAt(position));
    }
}
