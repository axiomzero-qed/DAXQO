package io.daxqo.app;

import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.pm.ResolveInfo;
import android.os.Build;
import org.json.JSONArray;
import org.json.JSONObject;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/** Read-only listing of enabled applications with a launcher activity. */
final class AppCatalog {
    static final int DEFAULT_LIMIT = 100;
    static final int MAX_LIMIT = 200;

    private AppCatalog() { }

    static JSONObject list(Context context, String query, int limit) {
        try {
            PackageManager manager = context.getPackageManager();
            Intent launcher = new Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER);
            List<ResolveInfo> matches;
            if (Build.VERSION.SDK_INT >= 33) {
                matches = manager.queryIntentActivities(launcher, PackageManager.ResolveInfoFlags.of(0));
            } else {
                matches = manager.queryIntentActivities(launcher, 0);
            }

            Map<String, App> byPackage = new LinkedHashMap<>();
            for (ResolveInfo info : matches) {
                if (info == null || info.activityInfo == null || info.activityInfo.applicationInfo == null
                        || !info.activityInfo.enabled || !info.activityInfo.applicationInfo.enabled) continue;
                String packageName = info.activityInfo.packageName;
                if (packageName == null || packageName.isEmpty()) continue;
                String label;
                try { label = String.valueOf(info.loadLabel(manager)); }
                catch (Exception ignored) { label = packageName; }
                App candidate = new App(clip(label, 80), clip(packageName, 255));
                App previous = byPackage.get(packageName);
                if (previous == null || compare(candidate, previous) < 0) byPackage.put(packageName, candidate);
            }

            String needle = query.toLowerCase(Locale.ROOT);
            List<App> filtered = new ArrayList<>();
            for (App app : byPackage.values()) {
                if (needle.isEmpty() || app.label.toLowerCase(Locale.ROOT).contains(needle)
                        || app.packageName.toLowerCase(Locale.ROOT).contains(needle)) filtered.add(app);
            }
            Collections.sort(filtered, Comparator.comparing((App app) -> app.label.toLowerCase(Locale.ROOT))
                    .thenComparing(app -> app.label).thenComparing(app -> app.packageName));

            int total = filtered.size();
            JSONArray apps = new JSONArray();
            for (int i = 0; i < Math.min(total, limit); i++) {
                App app = filtered.get(i);
                apps.put(new JSONObject().put("label", app.label).put("packageName", app.packageName));
            }
            return new JSONObject().put("apps", apps).put("total", total)
                    .put("truncated", total > limit).put("available", true);
        } catch (Exception failure) {
            return unavailable();
        }
    }

    static JSONObject unavailable() {
        JSONObject result = new JSONObject();
        try {
            result.put("apps", new JSONArray()).put("total", 0)
                    .put("truncated", false).put("available", false)
                    .put("error", "package_manager_unavailable");
        } catch (Exception ignored) { }
        return result;
    }

    private static String clip(String value, int max) {
        if (value.length() <= max) return value;
        int end = max;
        if (end > 0 && Character.isHighSurrogate(value.charAt(end - 1))) end--;
        return value.substring(0, end);
    }

    private static int compare(App left, App right) {
        int compared = left.label.toLowerCase(Locale.ROOT).compareTo(right.label.toLowerCase(Locale.ROOT));
        if (compared != 0) return compared;
        compared = left.label.compareTo(right.label);
        return compared != 0 ? compared : left.packageName.compareTo(right.packageName);
    }

    private static final class App {
        final String label;
        final String packageName;
        App(String label, String packageName) { this.label = label; this.packageName = packageName; }
    }
}
