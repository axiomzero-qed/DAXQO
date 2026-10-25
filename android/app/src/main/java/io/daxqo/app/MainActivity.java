package io.daxqo.app;

import android.app.Activity;
import android.content.ContentValues;
import android.content.Intent;
import android.content.ClipData;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.graphics.Color;
import android.os.Environment;
import android.os.Bundle;
import android.view.Gravity;
import android.view.View;
import android.view.WindowInsets;
import android.view.WindowInsetsController;
import android.view.WindowManager;
import android.webkit.JavascriptInterface;
import android.webkit.SafeBrowsingResponse;
import android.webkit.WebChromeClient;
import android.webkit.ValueCallback;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.provider.MediaStore;
import android.webkit.MimeTypeMap;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;
import org.json.JSONObject;

import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.io.File;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.security.MessageDigest;

public final class MainActivity extends Activity {
    private static MainActivity visibleActivity;
    private static final String UI_BASE = "http://127.0.0.1:18880/";
    private static final String PREFS = "pairing_private";
    private static final String CAP_KEY = "capability";
    private static final String USED_LINKS_KEY = "used_pair_links";
    private static final String BRIDGE_BASE = "http://127.0.0.1:18882";
    private final ExecutorService io = Executors.newSingleThreadExecutor();
    private WebView webView;
    private LinearLayout connectionPanel;
    private TextView status;
    private volatile boolean trustedGuiLoaded;
    private boolean connectionInProgress;
    private static final int FILE_CHOOSER_REQUEST = 1042;
    private static final int VOICE_PERMISSION_REQUEST = 1043;
    private ValueCallback<Uri[]> fileChooserCallback;
    private VoiceInputController voiceInput;
    private boolean voicePermissionPending;
    private String pendingVoiceSession;
    private String activeVoiceSession;
    private long pendingVoicePage;
    private long pageGeneration;

    @Override protected void onStart() { super.onStart(); visibleActivity = this; }
    @Override protected void onStop() {
        cancelVoiceSession(true);
        if (visibleActivity == this) visibleActivity = null;
        super.onStop();
    }
    @Override protected void onResume() {
        super.onResume();
        if (shouldRetryConnection(trustedGuiLoaded, connectionInProgress)) prepareAndLoadGui();
    }
    static boolean shouldRetryConnection(boolean trusted, boolean inProgress) { return !trusted && !inProgress; }
    static boolean mayReturnToTarget(boolean visible, boolean trusted, boolean focused, boolean closing) {
        return visible && trusted && focused && !closing;
    }
    // Package-only native continuation. Not a JS bridge, intent or tool action.
    // Called exactly once when consent transitions from pending to approved.
    static void returnToTargetAfterApproval() {
        MainActivity activity = visibleActivity;
        if (activity == null || !mayReturnToTarget(visibleActivity == activity, activity.trustedGuiLoaded,
                activity.hasWindowFocus(), activity.isFinishing() || activity.isDestroyed())) return;
        if (activity.webView == null || activity.webView.getVisibility() != View.VISIBLE) return;
        try {
            if (activity.isGuiUrl(Uri.parse(activity.webView.getUrl()))) activity.moveTaskToBack(true);
        } catch (Exception ignored) { /* Consent remains valid if Android refuses task navigation. */ }
    }

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        getWindow().setStatusBarColor(Color.WHITE);
        getWindow().setNavigationBarColor(Color.WHITE);
        getWindow().getDecorView().setSystemUiVisibility(
                View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR | View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR);
        if (Build.VERSION.SDK_INT >= 30 && getWindow().getInsetsController() != null) {
            int lightBars = WindowInsetsController.APPEARANCE_LIGHT_STATUS_BARS
                    | WindowInsetsController.APPEARANCE_LIGHT_NAVIGATION_BARS;
            getWindow().getInsetsController().setSystemBarsAppearance(lightBars, lightBars);
        }
        getWindow().setSoftInputMode(WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE);
        if (Build.VERSION.SDK_INT >= 29) {
            getWindow().setStatusBarContrastEnforced(false);
            getWindow().setNavigationBarContrastEnforced(false);
        }
        consumePairIntent(getIntent());
        voiceInput = new VoiceInputController(this, new VoiceInputController.Callback() {
            @Override public void onStatus(String message) {
                String type = message.contains("整理") || message.contains("加载") ? "processing" : "listening";
                emitVoiceEvent(type, null, message, activeVoiceSession);
            }
            @Override public void onPartialText(String text) {
                emitVoiceEvent("partial", text, null, activeVoiceSession);
            }
            @Override public void onFinalText(String text) {
                emitVoiceEvent("result", text, null, activeVoiceSession);
                activeVoiceSession = null;
            }
            @Override public void onError(String message) {
                emitVoiceEvent("error", null, message, activeVoiceSession);
                activeVoiceSession = null;
            }
            @Override public void onCancelled() {
                emitVoiceEvent("cancelled", null, null, activeVoiceSession);
                activeVoiceSession = null;
            }
        });
        buildView();
        prepareAndLoadGui();
        if (ControlActivity.ACTION_CONTROL.equals(getIntent().getAction())) {
            startActivity(new Intent(this, ControlActivity.class));
            setIntent(new Intent(this, MainActivity.class));
        }
    }

    @Override protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(new Intent(this, MainActivity.class));
        if (ControlActivity.ACTION_CONTROL.equals(intent.getAction())) startActivity(new Intent(this, ControlActivity.class));
        if (consumePairIntent(intent)) prepareAndLoadGui();
    }

    private boolean consumePairIntent(Intent intent) {
        Uri uri = intent == null ? null : intent.getData();
        if (uri == null || !getString(R.string.pairing_scheme).equals(uri.getScheme()) || !"connect".equals(uri.getHost())) return false;
        String fragment = uri.getFragment();
        String cap = fragment != null && fragment.startsWith("token=") ? Uri.decode(fragment.substring(6)) : null;
        if (cap == null || cap.length() < 32 || cap.length() > 4096 || !cap.matches("[A-Za-z0-9._~-]+")) {
            Toast.makeText(this, "配对链接无效", Toast.LENGTH_SHORT).show();
            return false;
        }
        android.content.SharedPreferences prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
        String fingerprint = fingerprint(cap);
        java.util.Set<String> consumed = new java.util.HashSet<>(prefs.getStringSet(USED_LINKS_KEY, java.util.Collections.emptySet()));
        if (fingerprint == null || consumed.contains(fingerprint)) {
            Toast.makeText(this, "配对链接无效或已使用", Toast.LENGTH_SHORT).show();
            return false;
        }
        consumed.add(fingerprint);
        prefs.edit().putString(CAP_KEY, cap).putStringSet(USED_LINKS_KEY, consumed).apply();
        // Remove sensitive URI from the Activity's retained launch intent.
        setIntent(new Intent(this, MainActivity.class));
        return true;
    }

    private String fingerprint(String value) {
        try {
            byte[] digest = MessageDigest.getInstance("SHA-256").digest(value.getBytes("UTF-8"));
            StringBuilder hex = new StringBuilder(digest.length * 2);
            for (byte b : digest) hex.append(String.format(java.util.Locale.ROOT, "%02x", b & 0xff));
            return hex.toString();
        } catch (Exception ignored) { return null; }
    }

    private void buildView() {
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(Color.WHITE);
        LinearLayout nativeActions = new LinearLayout(this);
        nativeActions.setGravity(Gravity.END | Gravity.CENTER_VERTICAL);
        nativeActions.setPadding(12, 0, 12, 0);
        TextView brand = new TextView(this);
        brand.setText(R.string.app_name);
        brand.setTextSize(15);
        brand.setTextColor(Color.rgb(35, 48, 64));
        nativeActions.addView(brand, new LinearLayout.LayoutParams(0, -2, 1));
        Button deviceControl = new Button(this);
        deviceControl.setText("手机控制");
        deviceControl.setOnClickListener(v -> startActivity(new Intent(this, ControlActivity.class)));
        nativeActions.addView(deviceControl);
        root.addView(nativeActions, new LinearLayout.LayoutParams(-1, -2));
        connectionPanel = new LinearLayout(this);
        connectionPanel.setOrientation(LinearLayout.VERTICAL);
        connectionPanel.setGravity(Gravity.CENTER_VERTICAL);
        connectionPanel.setPadding(16, 10, 16, 10);
        connectionPanel.setBackgroundColor(Color.rgb(248, 250, 252));
        status = new TextView(this);
        status.setText("正在连接本机服务…");
        status.setTextColor(Color.rgb(55, 65, 81));
        status.setTextSize(14);
        connectionPanel.addView(status, new LinearLayout.LayoutParams(-1, -2));
        LinearLayout actions = new LinearLayout(this);
        actions.setGravity(Gravity.END | Gravity.CENTER_VERTICAL);
        Button start = new Button(this);
        start.setText("启动 / 重试");
        start.setOnClickListener(v -> prepareAndLoadGui());
        actions.addView(start);
        Button restart = new Button(this);
        restart.setText("重启服务");
        restart.setOnClickListener(v -> callRuntime("restart"));
        actions.addView(restart);
        connectionPanel.addView(actions, new LinearLayout.LayoutParams(-1, -2));
        root.addView(connectionPanel, new LinearLayout.LayoutParams(-1, -2));
        webView = new WebView(this);
        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true); // Required by the first-party GUI only.
        settings.setDomStorageEnabled(true);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(false);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        settings.setSafeBrowsingEnabled(true);
        if (Build.VERSION.SDK_INT >= 29) settings.setForceDark(WebSettings.FORCE_DARK_OFF);
        if (Build.VERSION.SDK_INT >= 33) settings.setAlgorithmicDarkeningAllowed(false);
        webView.setWebChromeClient(new WebChromeClient() {
            @Override public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback,
                    FileChooserParams params) {
                if (fileChooserCallback != null) fileChooserCallback.onReceiveValue(null);
                fileChooserCallback = callback;
                Intent picker = new Intent(Intent.ACTION_OPEN_DOCUMENT);
                picker.addCategory(Intent.CATEGORY_OPENABLE);
                picker.setType("*/*");
                picker.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true);
                picker.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
                try {
                    startActivityForResult(picker, FILE_CHOOSER_REQUEST);
                } catch (Exception ignored) {
                    fileChooserCallback = null;
                    callback.onReceiveValue(null);
                    Toast.makeText(MainActivity.this, "无法打开系统文件选择器", Toast.LENGTH_SHORT).show();
                }
                return true;
            }
        });
        webView.setWebViewClient(new WebViewClient() {
            @Override public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
                if (isGuiUrl(request.getUrl())) return super.shouldInterceptRequest(view, request);
                return new WebResourceResponse("text/plain", "UTF-8", 403, "Blocked", null,
                        new java.io.ByteArrayInputStream(new byte[0]));
            }
            @Override public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri uri = request.getUrl();
                if (isGuiUrl(uri)) return false;
                openExternal(uri);
                return true;
            }
            @Override public void onPageStarted(WebView view, String url, android.graphics.Bitmap favicon) {
                pageGeneration++;
                cancelVoiceSession(true);
                trustedGuiLoaded = false;
            }
            @Override public void onPageFinished(WebView view, String url) {
                boolean trusted = false;
                try { trusted = isGuiUrl(Uri.parse(url)); } catch (Exception ignored) { }
                trustedGuiLoaded = trusted;
                if (trusted) {
                    connectionPanel.setVisibility(View.GONE);
                    webView.setVisibility(View.VISIBLE);
                }
            }
            @Override public void onReceivedError(WebView view, WebResourceRequest request,
                    android.webkit.WebResourceError error) {
                if (request.isForMainFrame()) showConnectionError("无法连接 DAXQO 界面，请重试");
            }
            @Override public void onReceivedHttpError(WebView view, WebResourceRequest request,
                    android.webkit.WebResourceResponse response) {
                if (request.isForMainFrame() && response.getStatusCode() >= 400) {
                    showConnectionError("DAXQO 界面返回错误，请重试");
                }
            }
            @Override public void onSafeBrowsingHit(WebView view, WebResourceRequest request,
                    int threatType, SafeBrowsingResponse callback) {
                if (Build.VERSION.SDK_INT >= 27) callback.backToSafety(true);
            }
        });
        webView.addJavascriptInterface(new FileDownloads(), "DAXQOFiles");
        webView.addJavascriptInterface(new VoiceBridge(), "DAXQOVoice");
        webView.setVisibility(View.GONE);
        root.addView(webView, new LinearLayout.LayoutParams(-1, 0, 1));
        if (Build.VERSION.SDK_INT >= 35) {
            root.setOnApplyWindowInsetsListener((view, insets) -> {
                android.graphics.Insets safe = insets.getInsets(WindowInsets.Type.systemBars()
                        | WindowInsets.Type.displayCutout() | WindowInsets.Type.ime());
                view.setPadding(safe.left, safe.top, safe.right, safe.bottom);
                return insets;
            });
        }
        setContentView(root);
    }

    private boolean isGuiUrl(Uri uri) {
        return "http".equals(uri.getScheme()) && "127.0.0.1".equals(uri.getHost())
                && uri.getPort() == 18880 && uri.getUserInfo() == null;
    }

    @Override protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode != FILE_CHOOSER_REQUEST) return;
        ValueCallback<Uri[]> callback = fileChooserCallback;
        fileChooserCallback = null;
        if (callback == null) return;
        Uri[] selected = null;
        if (resultCode == RESULT_OK && data != null) {
            ClipData clip = data.getClipData();
            if (clip != null && clip.getItemCount() > 0) {
                selected = new Uri[clip.getItemCount()];
                for (int i = 0; i < clip.getItemCount(); i++) selected[i] = clip.getItemAt(i).getUri();
            } else if (data.getData() != null) {
                selected = new Uri[] {data.getData()};
            }
        }
        // ACTION_OPEN_DOCUMENT grants this app temporary read access to the selected content URIs.
        callback.onReceiveValue(selected);
    }

    private void prepareAndLoadGui() {
        if (connectionInProgress) return;
        connectionInProgress = true;
        connectionPanel.setVisibility(View.VISIBLE);
        webView.setVisibility(View.GONE);
        trustedGuiLoaded = false;
        String cap = getSharedPreferences(PREFS, MODE_PRIVATE).getString(CAP_KEY, null);
        if (cap == null) {
            connectionInProgress = false;
            loadGuiWithCapability(null);
            return;
        }
        status.setText("检查本机服务");
        io.execute(() -> {
            String result = RuntimeControl.ensureRunning(BRIDGE_BASE, cap);
            runOnUiThread(() -> {
                connectionInProgress = false;
                if (result.startsWith("OK")) {
                    status.setText("本机服务在线");
                    loadGuiWithCapability(cap);
                } else {
                    showConnectionError(result);
                }
            });
        });
    }

    private void loadGuiWithCapability(String cap) {
        // Fragment is intentionally not sent in the HTTP request to the Linux GUI.
        webView.setVisibility(View.VISIBLE);
        // Force a fresh document so the GUI's one-time startup code reads a newly imported token.
        String entry = "?entry=" + Long.toString(System.nanoTime());
        webView.loadUrl(UI_BASE + entry + (cap == null ? "" : "#token=" + Uri.encode(cap)));
    }

    private void showConnectionError(String message) {
        trustedGuiLoaded = false;
        status.setText(message);
        connectionPanel.setVisibility(View.VISIBLE);
        webView.setVisibility(View.GONE);
    }

    private void openExternal(Uri uri) {
        boolean hasCredentialField = false;
        for (String name : uri.getQueryParameterNames()) {
            String key = name.toLowerCase(java.util.Locale.ROOT).replaceAll("[^a-z0-9]", "");
            if (isCredentialParameter(key)) {
                hasCredentialField = true;
                break;
            }
        }
        if (uri.getUserInfo() != null || uri.getFragment() != null || hasCredentialField || uri.toString().length() > 4096) {
            Toast.makeText(this, "已拦截包含凭据或过长的外链", Toast.LENGTH_SHORT).show();
            return;
        }
        try { startActivity(new Intent(Intent.ACTION_VIEW, uri)); }
        catch (Exception ignored) { Toast.makeText(this, "无法打开此链接", Toast.LENGTH_SHORT).show(); }
    }

    private boolean mayUseVoice() {
        if (isFinishing() || isDestroyed() || visibleActivity != this || !trustedGuiLoaded
                || webView == null || webView.getVisibility() != View.VISIBLE) return false;
        try { return isGuiUrl(Uri.parse(webView.getUrl())); }
        catch (Exception ignored) { return false; }
    }

    private boolean isTrustedGuiVisible() {
        if (isFinishing() || isDestroyed() || visibleActivity != this || !trustedGuiLoaded
                || webView == null || webView.getVisibility() != View.VISIBLE) return false;
        try { return isGuiUrl(Uri.parse(webView.getUrl())); }
        catch (Exception ignored) { return false; }
    }

    private void startVoice(String sessionId) {
        if (sessionId == null || sessionId.isEmpty() || sessionId.length() > 128) {
            emitVoiceEvent("error", null, "语音会话无效", sessionId);
            return;
        }
        if (!mayUseVoice()) {
            emitVoiceEvent("error", null, "当前页面暂不可使用语音输入", sessionId);
            return;
        }
        if (voicePermissionPending || activeVoiceSession != null
                || (voiceInput != null && voiceInput.isListening())) return;
        if (!VoiceInputController.isAvailable(this)) {
            emitVoiceEvent("error", null, "此设备没有可用的麦克风", sessionId);
            return;
        }
        if (checkSelfPermission(android.Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            voicePermissionPending = true;
            pendingVoiceSession = sessionId;
            pendingVoicePage = pageGeneration;
            requestPermissions(new String[] {android.Manifest.permission.RECORD_AUDIO}, VOICE_PERMISSION_REQUEST);
            return;
        }
        activeVoiceSession = sessionId;
        voiceInput.start();
    }

    private void stopVoice() {
        if (voicePermissionPending) {
            cancelVoiceSession(true);
        } else if (voiceInput != null && voiceInput.isListening()) {
            voiceInput.stop();
        }
    }

    private void cancelVoiceSession(boolean notify) {
        String sessionId = activeVoiceSession != null ? activeVoiceSession : pendingVoiceSession;
        boolean hadSession = voicePermissionPending || (voiceInput != null && voiceInput.isListening());
        voicePermissionPending = false;
        pendingVoiceSession = null;
        activeVoiceSession = null;
        if (voiceInput != null) voiceInput.cancel();
        if (notify && hadSession && sessionId != null) {
            emitVoiceEvent("cancelled", null, null, sessionId);
        }
    }

    private void emitVoiceEvent(String type, String text, String message, String sessionId) {
        if (sessionId == null || webView == null || isDestroyed()) return;
        JSONObject event = new JSONObject();
        try {
            event.put("type", type);
            if (text != null) event.put("text", text);
            if (message != null) event.put("message", message);
            event.put("sessionId", sessionId);
        } catch (org.json.JSONException ignored) { return; }
        String script = "window.daxqoVoiceEvent&&window.daxqoVoiceEvent(" + event + ")";
        runOnUiThread(() -> {
            if (mayUseVoice()) webView.evaluateJavascript(script, null);
        });
    }

    private final class VoiceBridge {
        @JavascriptInterface public void start(String sessionId) {
            runOnUiThread(() -> startVoice(sessionId));
        }
        @JavascriptInterface public void stop() { runOnUiThread(MainActivity.this::stopVoice); }
        @JavascriptInterface public void cancel() { runOnUiThread(() -> cancelVoiceSession(true)); }
    }

    @Override public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode != VOICE_PERMISSION_REQUEST) return;
        String sessionId = pendingVoiceSession;
        boolean samePage = pendingVoicePage == pageGeneration;
        voicePermissionPending = false;
        pendingVoiceSession = null;
        if (sessionId == null) return;
        if (grantResults.length > 0 && grantResults[0] == PackageManager.PERMISSION_GRANTED
                && samePage && mayUseVoice()) {
            emitVoiceEvent("error", null, "麦克风权限已授予，请再次按住开始语音输入", sessionId);
        } else {
            emitVoiceEvent("error", null, "未授予麦克风权限，无法使用语音输入", sessionId);
        }
    }

    private boolean isCredentialParameter(String key) {
        return key.equals("key") || key.endsWith("key") || key.endsWith("keyid")
                || key.contains("token") || key.contains("capability") || key.contains("secret")
                || key.contains("password") || key.contains("passwd") || key.contains("credential")
                || key.contains("authorization") || key.equals("auth") || key.endsWith("auth")
                || key.equals("code") || key.equals("state") || key.equals("jwt")
                || key.equals("sig") || key.endsWith("signature") || key.endsWith("assertion");
    }

    private void callRuntime(String action) {
        final String cap = getSharedPreferences(PREFS, MODE_PRIVATE).getString(CAP_KEY, null);
        if (cap == null) { Toast.makeText(this, "请先配对本机服务", Toast.LENGTH_SHORT).show(); return; }
        status.setText("请求中");
        io.execute(() -> {
            String message = RuntimeControl.request(BRIDGE_BASE, action, cap);
            if (message.startsWith("OK")) message = RuntimeControl.waitUntilReady(BRIDGE_BASE, cap);
            final String result = message;
            runOnUiThread(() -> {
                if (result.startsWith("OK")) {
                    status.setText("服务已响应，检查连接…");
                    connectionInProgress = false;
                    loadGuiWithCapability(cap);
                } else {
                    showConnectionError(result);
                }
            });
        });
    }

    private final class FileDownloads {
        private static final long MAX_BYTES = 20L * 1024L * 1024L;

        @JavascriptInterface public boolean downloadFile(String relativePath) {
            return queueDownload(relativePath, false);
        }

        @JavascriptInterface public boolean openFile(String relativePath) {
            return queueDownload(relativePath, true);
        }

        private boolean queueDownload(String relativePath, boolean openAfterSave) {
            if (!trustedGuiLoaded || !isSafeRelativePath(relativePath)) return false;
            String capability = getSharedPreferences(PREFS, MODE_PRIVATE).getString(CAP_KEY, null);
            if (capability == null) return false;
            io.execute(() -> {
                boolean saved = saveFile(relativePath, capability, openAfterSave);
                if (!openAfterSave || !saved) runOnUiThread(() -> Toast.makeText(MainActivity.this,
                        saved ? "文件已保存到下载目录" : "文件下载失败或超过 20 MB 限制", Toast.LENGTH_LONG).show());
            });
            return true;
        }

        private boolean isSafeRelativePath(String input) {
            if (input == null || input.isEmpty() || input.length() > 2048 || input.startsWith("/")
                    || input.startsWith("\\") || input.contains(":") || input.contains("?") || input.contains("#")) return false;
            String decoded = input;
            for (int i = 0; i < 4; i++) {
                String next = Uri.decode(decoded);
                if (next.equals(decoded)) break;
                decoded = next;
            }
            if (decoded.startsWith("/") || decoded.startsWith("\\") || decoded.contains("\\")
                    || decoded.contains(":") || decoded.indexOf('\0') >= 0) return false;
            for (String part : decoded.split("/", -1)) if (part.equals("..") || part.isEmpty()) return false;
            return true;
        }

        private boolean saveFile(String relativePath, String capability, boolean openAfterSave) {
            HttpURLConnection connection = null;
            Uri inserted = null;
            File legacyFile = null;
            try {
                Uri request = new Uri.Builder().scheme("http").encodedAuthority("127.0.0.1:18880")
                        .appendPath("api").appendPath("files").appendPath("download")
                        .appendQueryParameter("path", relativePath).build();
                connection = (HttpURLConnection) new URL(request.toString()).openConnection();
                connection.setConnectTimeout(5000);
                connection.setReadTimeout(30000);
                connection.setInstanceFollowRedirects(false);
                connection.setRequestProperty("Authorization", "Bearer " + capability);
                connection.setRequestProperty("Accept", "application/octet-stream");
                int code = connection.getResponseCode();
                if (code < 200 || code >= 300 || connection.getContentLengthLong() > MAX_BYTES) return false;

                String filename = safeFilename(relativePath, connection.getHeaderField("Content-Disposition"));
                String mime = connection.getContentType();
                if (mime == null || mime.contains(";") || "application/octet-stream".equalsIgnoreCase(mime.trim())) {
                    mime = MimeTypeMap.getSingleton().getMimeTypeFromExtension(
                            MimeTypeMap.getFileExtensionFromUrl(filename).toLowerCase(java.util.Locale.ROOT));
                }
                if (mime == null) mime = "application/octet-stream";

                OutputStream output;
                if (Build.VERSION.SDK_INT >= 29) {
                    ContentValues values = new ContentValues();
                    values.put(MediaStore.Downloads.DISPLAY_NAME, filename);
                    values.put(MediaStore.Downloads.MIME_TYPE, mime);
                    values.put(MediaStore.Downloads.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS + "/DAXQO");
                    values.put(MediaStore.Downloads.IS_PENDING, 1);
                    inserted = getContentResolver().insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values);
                    if (inserted == null) return false;
                    output = getContentResolver().openOutputStream(inserted, "w");
                    if (output == null) {
                        getContentResolver().delete(inserted, null, null);
                        inserted = null;
                        return false;
                    }
                } else {
                    File folder = getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS);
                    if (folder == null) return false;
                    legacyFile = new File(folder, filename);
                    output = new java.io.FileOutputStream(legacyFile);
                }

                try (InputStream input = connection.getInputStream(); OutputStream out = output) {
                    byte[] buffer = new byte[8192];
                    long total = 0;
                    int count;
                    while ((count = input.read(buffer)) != -1) {
                        total += count;
                        if (total > MAX_BYTES) throw new java.io.IOException("download limit");
                        out.write(buffer, 0, count);
                    }
                }
                if (inserted != null) {
                    ContentValues ready = new ContentValues();
                    ready.put(MediaStore.Downloads.IS_PENDING, 0);
                    getContentResolver().update(inserted, ready, null, null);
                }
                if (openAfterSave) {
                    Uri openUri = inserted;
                    String openMime = mime;
                    runOnUiThread(() -> openSavedFile(openUri, openMime));
                }
                return true;
            } catch (Exception ignored) {
                if (inserted != null) getContentResolver().delete(inserted, null, null);
                if (legacyFile != null) legacyFile.delete();
                return false;
            } finally { if (connection != null) connection.disconnect(); }
        }

        private void openSavedFile(Uri uri, String mime) {
            if (!isTrustedGuiVisible()) {
                Toast.makeText(MainActivity.this, "文件已保存到下载目录", Toast.LENGTH_LONG).show();
                return;
            }
            if (uri == null) {
                Toast.makeText(MainActivity.this, "已保存到下载目录，请在文件管理器打开", Toast.LENGTH_LONG).show();
                return;
            }
            Intent view = new Intent(Intent.ACTION_VIEW);
            view.setDataAndType(uri, mime == null ? "application/octet-stream" : mime);
            view.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
            try {
                startActivity(view);
            } catch (Exception noViewer) {
                Toast.makeText(MainActivity.this, "已保存到下载目录，请安装相应阅读应用", Toast.LENGTH_LONG).show();
            }
        }

        private String safeFilename(String relativePath, String disposition) {
            String name = null;
            if (disposition != null) {
                for (String piece : disposition.split(";")) {
                    String item = piece.trim();
                    if (item.regionMatches(true, 0, "filename=", 0, 9)) {
                        name = item.substring(9).trim().replace("\"", "");
                        break;
                    }
                }
            }
            if (name == null || name.isEmpty()) name = relativePath.substring(relativePath.lastIndexOf('/') + 1);
            name = name.replaceAll("[\\\\/:*?\"<>|\\r\\n]", "_").trim();
            while (name.startsWith(".")) name = name.substring(1);
            if (name.isEmpty()) name = "download";
            return name.length() > 180 ? name.substring(name.length() - 180) : name;
        }
    }

    @Override public void onBackPressed() {
        if (webView != null && webView.canGoBack()) webView.goBack();
        else super.onBackPressed();
    }

    @Override protected void onDestroy() {
        cancelVoiceSession(false);
        if (voiceInput != null) { voiceInput.destroy(); voiceInput = null; }
        if (visibleActivity == this) visibleActivity = null;
        if (fileChooserCallback != null) {
            fileChooserCallback.onReceiveValue(null);
            fileChooserCallback = null;
        }
        if (webView != null) { webView.stopLoading(); webView.destroy(); webView = null; }
        io.shutdownNow();
        super.onDestroy();
    }
}
