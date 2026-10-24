package io.daxqo.inputfixture;

import android.app.Activity;
import android.content.Context;
import android.os.Bundle;
import android.view.View;
import android.view.inputmethod.InputMethodManager;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;

public final class MainActivity extends Activity {
    private static final String[] SCENARIOS = {
            "native-middle", "native-selection", "native-emoji",
            "web-middle", "web-contenteditable"
    };

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        getWindow().setSoftInputMode(android.view.WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE);

        String scenario = getIntent().getStringExtra("scenario");
        if (!contains(SCENARIOS, scenario)) scenario = "native-middle";
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(dp(18), dp(18), dp(18), dp(12));
        root.setFocusableInTouchMode(true);
        setContentView(root);

        TextView heading = new TextView(this);
        heading.setText("DAXQO 输入验收 · " + scenario + "\n请在键盘中输入：中文");
        heading.setTextSize(18);
        heading.setPadding(0, 0, 0, dp(14));
        root.addView(heading, new LinearLayout.LayoutParams(-1, -2));

        if (scenario.startsWith("native-")) {
            addNativeField(root, scenario);
        } else {
            addWebField(root, scenario);
        }
    }

    private void addNativeField(LinearLayout root, String scenario) {
        EditText field = new EditText(this);
        field.setSingleLine(false);
        field.setTextSize(20);
        field.setHint("验收输入框");
        int selectionStart;
        int selectionEnd;
        if (scenario.equals("native-selection")) {
            field.setText("甲乙丙");
            selectionStart = 1;
            selectionEnd = 2;
        } else if (scenario.equals("native-emoji")) {
            field.setText("A\uD83D\uDE00B");
            selectionStart = 3;
            selectionEnd = 3;
        } else {
            field.setText("甲乙");
            selectionStart = 1;
            selectionEnd = 1;
        }
        root.addView(field, new LinearLayout.LayoutParams(-1, dp(100)));
        field.requestFocus();
        field.setSelection(selectionStart, selectionEnd);
        showKeyboard(field);
    }

    private void addWebField(LinearLayout root, String scenario) {
        WebView web = new WebView(this);
        web.getSettings().setJavaScriptEnabled(true);
        web.setWebViewClient(new WebViewClient() {
            @Override
            public void onPageFinished(WebView view, String url) {
                super.onPageFinished(view, url);
                String script = scenario.equals("web-contenteditable")
                        ? "(()=>{const e=document.querySelector('[contenteditable]');const r=document.createRange();r.setStart(e.firstChild,1);r.collapse(true);const s=getSelection();s.removeAllRanges();s.addRange(r);e.focus();})()"
                        : "(()=>{const e=document.querySelector('textarea');e.focus();e.setSelectionRange(1,1);})()";
                view.evaluateJavascript(script, ignored -> {
                    view.requestFocus();
                    showKeyboard(view);
                });
            }
        });
        root.addView(web, new LinearLayout.LayoutParams(-1, 0, 1f));

        String editor = scenario.equals("web-contenteditable")
                ? "<div contenteditable='true' role='textbox' aria-label='验收输入框' style='min-height:180px;padding:14px;border:1px solid #789;background:#fff;font-size:22px'>甲乙</div>"
                : "<textarea aria-label='验收输入框' style='width:100%;height:180px;padding:14px;box-sizing:border-box;font-size:22px'>甲乙</textarea>";
        String html = "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
                + "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                + "<style>body{font-family:sans-serif;background:#f6f8fb}label{display:block;margin:12px 0}</style>"
                + "</head><body><label for='fixture'>本地 WebView 输入框</label>" + editor + "</body></html>";
        web.loadDataWithBaseURL(null, html, "text/html", "UTF-8", null);
    }

    private void showKeyboard(View target) {
        target.post(() -> {
            target.requestFocus();
            InputMethodManager imm = (InputMethodManager) getSystemService(Context.INPUT_METHOD_SERVICE);
            if (imm != null) imm.showSoftInput(target, InputMethodManager.SHOW_IMPLICIT);
        });
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }

    private static boolean contains(String[] values, String value) {
        if (value == null) return false;
        for (String item : values) if (item.equals(value)) return true;
        return false;
    }
}
