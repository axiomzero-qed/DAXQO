package io.daxqo.app;

import android.content.Context;
import android.content.res.AssetManager;
import android.system.Os;
import android.system.OsConstants;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.zip.ZipEntry;
import java.util.zip.ZipInputStream;

/** Installs a verified, versioned Linux rootfs bundled in the APK assets. */
final class OfflineRuntimeInstaller {
    interface Progress { void update(String message); }

    private static final String MANIFEST_ASSET = "runtime/manifest.json";
    private static final String ARCHIVE_ASSET = "runtime/rootfs.zip";
    private static final String SCHEMA = "daxqo.runtime/1";
    private static final String INDEX_SCHEMA = "daxqo.rootfs/1";
    private static final String VERSION = "1.0.0";

    private OfflineRuntimeInstaller() {}

    static synchronized File ensureInstalled(Context context, Progress progress) throws Exception {
        if (context == null) throw new IllegalArgumentException("context is required");
        Progress callback = progress == null ? message -> {} : progress;
        AssetManager assets = context.getAssets();
        JSONObject manifest;
        try (InputStream in = assets.open(MANIFEST_ASSET)) { manifest = new JSONObject(readUtf8(in)); }
        validateManifest(manifest);
        String sha = manifest.getString("sha256").toLowerCase(Locale.ROOT);
        long archiveBytes = manifest.getLong("archiveBytes");
        long unpackedBytes = manifest.getLong("unpackedBytes");
        int fileCount = manifest.getInt("fileCount");

        File runtime = new File(context.getFilesDir(), "runtime");
        ensureDirectory(runtime);
        String releaseName = VERSION + "-" + sha.substring(0, 12);
        File release = new File(runtime, releaseName);
        File rootfs = new File(release, "rootfs");
        File ready = new File(release, "ready.json");
        if (isComplete(ready, rootfs, sha, archiveBytes, unpackedBytes, fileCount)) return rootfs;

        long usable = runtime.getUsableSpace();
        if (usable > 0 && usable < unpackedBytes + Math.max(8L * 1024 * 1024, unpackedBytes / 20))
            throw new java.io.IOException("Insufficient storage for offline runtime installation");

        String stageName = ".staging-" + UUID.randomUUID();
        File staging = new File(runtime, stageName);
        boolean stagingCreated = false, publishedRoot = false;
        try {
            callback.update("正在校验离线运行环境");
            String actualSha;
            long actualBytes = 0;
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            try (InputStream in = new BufferedInputStream(assets.open(ARCHIVE_ASSET))) {
                byte[] buffer = new byte[64 * 1024];
                int n;
                while ((n = in.read(buffer)) != -1) {
                    digest.update(buffer, 0, n);
                    actualBytes = checkedAdd(actualBytes, n);
                }
            }
            actualSha = hex(digest.digest());
            if (actualBytes != archiveBytes || !actualSha.equals(sha))
                throw new java.io.IOException("Offline runtime archive integrity check failed");

            if (!staging.mkdir()) throw new java.io.IOException("Cannot create runtime staging directory");
            stagingCreated = true;
            File stagedRoot = new File(staging, "rootfs");
            if (!stagedRoot.mkdir()) throw new java.io.IOException("Cannot create rootfs staging directory");
            callback.update("正在展开离线运行环境");
            installArchive(assets, stagedRoot, sha, archiveBytes, unpackedBytes, fileCount, callback);
            if (lstatExists(release)) deleteTreeNoFollow(release);
            if (!staging.renameTo(release)) throw new java.io.IOException("Cannot publish installed runtime directory");
            publishedRoot = true;
            writeReadyAtomically(ready, sha, archiveBytes, unpackedBytes, fileCount);
            callback.update("离线运行环境已就绪");
            return rootfs;
        } catch (Exception failure) {
            if (publishedRoot && !ready.exists()) deleteTreeNoFollow(release);
            throw failure;
        } finally {
            if (stagingCreated) deleteTreeNoFollow(staging);
        }
    }

    private static void validateManifest(JSONObject m) throws Exception {
        if (!SCHEMA.equals(m.optString("schema")) || !VERSION.equals(m.optString("version"))
                || !"arm64-v8a".equals(m.optString("abi")) || !ARCHIVE_ASSET.equals(m.optString("archive")))
            throw new java.io.IOException("Unsupported offline runtime manifest");
        String sha = m.optString("sha256");
        if (!sha.matches("(?i)[0-9a-f]{64}") || m.optLong("archiveBytes", -1) <= 0
                || m.optLong("unpackedBytes", -1) < 0 || m.optInt("fileCount", -1) < 1)
            throw new java.io.IOException("Invalid offline runtime manifest values");
    }

    private static boolean isComplete(File ready, File root, String sha, long archiveBytes,
                                      long unpackedBytes, int fileCount) {
        try {
            if (!isRealDirectory(root) || !isRegularFileNoFollow(ready)) return false;
            JSONObject marker;
            try (InputStream in = new java.io.FileInputStream(ready)) { marker = new JSONObject(readUtf8(in)); }
            return SCHEMA.equals(marker.optString("schema")) && VERSION.equals(marker.optString("version"))
                    && "arm64-v8a".equals(marker.optString("abi")) && ARCHIVE_ASSET.equals(marker.optString("archive"))
                    && sha.equalsIgnoreCase(marker.optString("sha256"))
                    && archiveBytes == marker.optLong("archiveBytes", -1)
                    && unpackedBytes == marker.optLong("unpackedBytes", -1)
                    && fileCount == marker.optInt("fileCount", -1);
        } catch (Exception ignored) { return false; }
    }

    private static void installArchive(AssetManager assets, File root, String expectedSha,
                                       long archiveBytes, long unpackedBytes, int expectedCount,
                                       Progress progress) throws Exception {
        Set<String> zipNames = new HashSet<>();
        Map<String, JSONObject> entries = new HashMap<>();
        List<JSONObject> links = new ArrayList<>();
        long seenFileBytes = 0;
        int seenEntries = 0;
        try (InputStream raw = new BufferedInputStream(assets.open(ARCHIVE_ASSET));
             ZipInputStream zip = new ZipInputStream(raw)) {
            ZipEntry first = zip.getNextEntry();
            if (first == null || first.isDirectory() || !"bundle-index.json".equals(first.getName()))
                throw new java.io.IOException("rootfs.zip must start with bundle-index.json");
            byte[] indexBytes = readZipEntry(zip);
            JSONObject index = new JSONObject(new String(indexBytes, "UTF-8"));
            if (!INDEX_SCHEMA.equals(index.optString("schema")) || !VERSION.equals(index.optString("version")))
                throw new java.io.IOException("Unsupported rootfs index");
            JSONArray rows = index.optJSONArray("entries");
            if (rows == null || rows.length() != expectedCount) throw new java.io.IOException("rootfs index fileCount mismatch");
            for (int i = 0; i < rows.length(); i++) {
                JSONObject row = rows.getJSONObject(i);
                String path = row.optString("path", "");
                validateRelativePath(path);
                if ("bundle-index.json".equals(path) || entries.put(path, row) != null)
                    throw new java.io.IOException("Duplicate or reserved rootfs path: " + path);
                String type = row.optString("type");
                if (!"file".equals(type) && !"dir".equals(type) && !"symlink".equals(type) && !"hardlink".equals(type))
                    throw new java.io.IOException("Invalid rootfs entry type");
                if (row.optLong("size", -1) < 0 || row.optInt("mode", -1) < 0)
                    throw new java.io.IOException("Invalid rootfs entry metadata");
                if ("file".equals(type)) {
                    if (!row.optString("sha256").matches("(?i)[0-9a-f]{64}")) throw new java.io.IOException("Invalid file digest");
                } else if ("symlink".equals(type) || "hardlink".equals(type)) {
                    if (row.optString("target").isEmpty()) throw new java.io.IOException("Empty link target");
                    links.add(row);
                }
            }
            validateNoFileAsParent(entries);
            zip.closeEntry();
            ZipEntry ze;
            while ((ze = zip.getNextEntry()) != null) {
                if (ze.isDirectory()) throw new java.io.IOException("Directory data entries are not allowed");
                String name = ze.getName();
                validateRelativePath(name);
                if (!zipNames.add(name)) throw new java.io.IOException("Duplicate zip entry: " + name);
                JSONObject row = entries.get(name);
                if (row == null || !"file".equals(row.optString("type")))
                    throw new java.io.IOException("Undeclared or non-file zip entry: " + name);
                ensureParentsAreDirectories(root, name);
                File destination = child(root, name);
                if (lstatExists(destination)) throw new java.io.IOException("Duplicate destination path: " + name);
                MessageDigest fileDigest = MessageDigest.getInstance("SHA-256");
                long fileSize = 0;
                try (OutputStream out = new BufferedOutputStream(new FileOutputStream(destination))) {
                    byte[] buffer = new byte[64 * 1024];
                    int n;
                    while ((n = zip.read(buffer)) != -1) {
                        fileSize = checkedAdd(fileSize, n);
                        if (fileSize > row.getLong("size")) throw new java.io.IOException("File exceeds indexed size: " + name);
                        seenFileBytes = checkedAdd(seenFileBytes, n);
                        fileDigest.update(buffer, 0, n);
                        out.write(buffer, 0, n);
                    }
                }
                zip.closeEntry();
                if (fileSize != row.getLong("size") || !hex(fileDigest.digest()).equalsIgnoreCase(row.getString("sha256")))
                    throw new java.io.IOException("File integrity check failed: " + name);
                Os.chmod(destination.getAbsolutePath(), row.getInt("mode") & 0777);
                seenEntries++;
                if (seenEntries % 1000 == 0)
                    progress.update("正在准备首次使用 · " + (seenEntries * 100 / Math.max(1, countType(entries, "file"))) + "%");
            }
        }
        if (seenFileBytes != unpackedBytes || seenEntries != countType(entries, "file"))
            throw new java.io.IOException("rootfs unpacked size or file count mismatch");
        if (zipNames.size() != countType(entries, "file")) throw new java.io.IOException("rootfs archive entry set mismatch");

        for (JSONObject row : entries.values()) if ("dir".equals(row.optString("type"))) createDirectoryEntry(root, row);
        for (JSONObject row : entries.values()) if ("file".equals(row.optString("type"))) {
            File f = child(root, row.getString("path"));
            if (!isRegularFileNoFollow(f)) throw new java.io.IOException("Missing regular file");
        }
        for (JSONObject row : links) {
            if ("hardlink".equals(row.optString("type"))) {
                String target = validateRelativePath(row.getString("target"));
                JSONObject targetEntry = entries.get(target);
                if (targetEntry == null || !("file".equals(targetEntry.optString("type"))
                        || "hardlink".equals(targetEntry.optString("type"))))
                    throw new java.io.IOException("Hardlink target is not an indexed file");
            }
            createLink(root, row);
        }
        for (JSONObject row : entries.values()) if ("dir".equals(row.optString("type"))) {
            File directory = child(root, row.getString("path"));
            Os.chmod(directory.getAbsolutePath(), row.getInt("mode") & 0777);
        }
        progress.update("正在完成离线运行环境校验");
    }

    private static byte[] readZipEntry(ZipInputStream zip) throws Exception {
        java.io.ByteArrayOutputStream out = new java.io.ByteArrayOutputStream();
        byte[] buffer = new byte[8192]; int n;
        while ((n = zip.read(buffer)) != -1) {
            if (out.size() + n > 16 * 1024 * 1024) throw new java.io.IOException("rootfs index is too large");
            out.write(buffer, 0, n);
        }
        return out.toByteArray();
    }

    private static void validateNoFileAsParent(Map<String, JSONObject> entries) throws Exception {
        for (String path : entries.keySet()) {
            int slash = path.indexOf('/');
            while (slash >= 0) {
                JSONObject parent = entries.get(path.substring(0, slash));
                if (parent != null && !"dir".equals(parent.optString("type")))
                    throw new java.io.IOException("Non-directory parent path: " + path);
                slash = path.indexOf('/', slash + 1);
            }
        }
    }

    private static int countType(Map<String, JSONObject> entries, String type) {
        int count = 0;
        for (JSONObject row : entries.values()) if (type.equals(row.optString("type"))) count++;
        return count;
    }

    static String validateRelativePath(String path) throws java.io.IOException {
        if (path == null || path.isEmpty() || path.startsWith("/") || path.indexOf('\\') >= 0 || path.indexOf('\0') >= 0)
            throw new java.io.IOException("Invalid POSIX relative path");
        String[] parts = path.split("/", -1);
        for (String part : parts) if (part.isEmpty() || ".".equals(part) || "..".equals(part))
            throw new java.io.IOException("Invalid POSIX relative path");
        return path;
    }

    private static void createDirectoryEntry(File root, JSONObject row) throws Exception {
        String path = row.getString("path");
        ensureParentsAreDirectories(root, path);
        File dir = child(root, path);
        if (!dir.mkdir() && !isRealDirectory(dir)) throw new java.io.IOException("Cannot create directory: " + path);
    }

    private static void createLink(File root, JSONObject row) throws Exception {
        String path = row.getString("path"), type = row.getString("type"), target = row.getString("target");
        ensureParentsAreDirectories(root, path);
        File link = child(root, path);
        if (lstatExists(link)) throw new java.io.IOException("Duplicate link destination: " + path);
        if ("hardlink".equals(type)) target = relativeLinkTarget(path, validateRelativePath(target));
        else validateSymlinkTarget(path, target);
        Os.symlink(target, link.getAbsolutePath());
    }

    static void validateSymlinkTarget(String linkPath, String target) throws Exception {
        if (target.indexOf('\\') >= 0 || target.indexOf('\0') >= 0 || target.isEmpty())
            throw new java.io.IOException("Invalid symlink target");
        if (target.startsWith("/")) return; // Absolute Linux targets are retained, never followed during extraction.
        String parent = linkPath.contains("/") ? linkPath.substring(0, linkPath.lastIndexOf('/')) : "";
        ArrayList<String> parts = new ArrayList<>();
        if (!parent.isEmpty()) for (String part : parent.split("/")) parts.add(part);
        for (String part : target.split("/", -1)) {
            if (part.isEmpty() || ".".equals(part)) continue;
            if ("..".equals(part)) { if (parts.isEmpty()) throw new java.io.IOException("Symlink escapes rootfs"); parts.remove(parts.size() - 1); }
            else parts.add(part);
        }
    }

    static String relativeLinkTarget(String linkPath, String rootTarget) throws Exception {
        ArrayList<String> target = new ArrayList<>();
        for (String part : validateRelativePath(rootTarget).split("/")) target.add(part);
        String parent = linkPath.contains("/") ? linkPath.substring(0, linkPath.lastIndexOf('/')) : "";
        ArrayList<String> from = new ArrayList<>();
        if (!parent.isEmpty()) for (String part : parent.split("/")) from.add(part);
        int common = 0;
        while (common < from.size() && common < target.size() && from.get(common).equals(target.get(common))) common++;
        StringBuilder result = new StringBuilder();
        for (int i = common; i < from.size(); i++) { if (result.length() > 0) result.append('/'); result.append(".."); }
        for (int i = common; i < target.size(); i++) { if (result.length() > 0) result.append('/'); result.append(target.get(i)); }
        if (result.length() == 0) throw new java.io.IOException("Hardlink cannot target itself");
        return result.toString();
    }

    private static void ensureParentsAreDirectories(File root, String path) throws Exception {
        File current = root;
        String[] parts = path.split("/");
        for (int i = 0; i < parts.length - 1; i++) {
            current = new File(current, parts[i]);
            if (!lstatExists(current)) {
                if (!current.mkdir()) throw new java.io.IOException("Cannot create parent directory");
                Os.chmod(current.getAbsolutePath(), 0755);
            }
            if (!isRealDirectory(current)) throw new java.io.IOException("Parent is not a real directory");
        }
    }

    private static File child(File root, String relative) throws Exception {
        validateRelativePath(relative);
        File candidate = new File(root, relative);
        String base = root.getCanonicalPath() + File.separator;
        String canonical = candidate.getCanonicalPath();
        if (!canonical.startsWith(base)) throw new java.io.IOException("Path escapes staging root");
        return candidate;
    }

    private static void ensureDirectory(File directory) throws Exception {
        if (!directory.exists() && !directory.mkdir()) throw new java.io.IOException("Cannot create runtime directory");
        if (!isRealDirectory(directory)) throw new java.io.IOException("Runtime path is not a real directory");
    }

    private static boolean isRealDirectory(File file) throws Exception {
        return (Os.lstat(file.getAbsolutePath()).st_mode & OsConstants.S_IFMT) == OsConstants.S_IFDIR;
    }
    private static boolean isRegularFileNoFollow(File file) throws Exception {
        return (Os.lstat(file.getAbsolutePath()).st_mode & OsConstants.S_IFMT) == OsConstants.S_IFREG;
    }
    private static boolean lstatExists(File file) {
        try { Os.lstat(file.getAbsolutePath()); return true; } catch (Exception ignored) { return false; }
    }

    private static void deleteTreeNoFollow(File file) throws Exception {
        if (!lstatExists(file)) return;
        int type = Os.lstat(file.getAbsolutePath()).st_mode & OsConstants.S_IFMT;
        if (type == OsConstants.S_IFDIR) {
            Os.chmod(file.getAbsolutePath(), 0700);
            File[] children = file.listFiles();
            if (children != null) for (File child : children) deleteTreeNoFollow(child);
        }
        if (!file.delete() && lstatExists(file)) throw new java.io.IOException("Cannot remove staging path");
    }

    private static void writeReadyAtomically(File ready, String sha, long archiveBytes,
                                             long unpackedBytes, int fileCount) throws Exception {
        File tmp = new File(ready.getParentFile(), ".ready-" + UUID.randomUUID());
        JSONObject marker = new JSONObject().put("schema", SCHEMA).put("version", VERSION).put("abi", "arm64-v8a")
                .put("archive", ARCHIVE_ASSET).put("sha256", sha).put("archiveBytes", archiveBytes)
                .put("unpackedBytes", unpackedBytes).put("fileCount", fileCount);
        try (FileOutputStream file = new FileOutputStream(tmp);
             OutputStream out = new BufferedOutputStream(file)) {
            out.write(marker.toString().getBytes("UTF-8")); out.flush(); file.getFD().sync();
        }
        if (!tmp.renameTo(ready)) { tmp.delete(); throw new java.io.IOException("Cannot write runtime ready marker"); }
    }

    private static String readUtf8(InputStream input) throws Exception {
        java.io.ByteArrayOutputStream out = new java.io.ByteArrayOutputStream();
        byte[] buffer = new byte[8192]; int n;
        while ((n = input.read(buffer)) != -1) {
            if (out.size() + n > 16 * 1024 * 1024) throw new java.io.IOException("Runtime metadata is too large");
            out.write(buffer, 0, n);
        }
        return new String(out.toByteArray(), "UTF-8");
    }
    private static long checkedAdd(long a, long b) throws java.io.IOException {
        if (b < 0 || a > Long.MAX_VALUE - b) throw new java.io.IOException("Runtime size overflow");
        return a + b;
    }
    private static String hex(byte[] bytes) {
        StringBuilder result = new StringBuilder(bytes.length * 2);
        for (byte b : bytes) result.append(String.format(Locale.ROOT, "%02x", b & 0xff));
        return result.toString();
    }
}
