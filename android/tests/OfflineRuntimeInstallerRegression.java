package io.daxqo.app;

public final class OfflineRuntimeInstallerRegression {
    private static int checks;

    private static void check(boolean condition, String message) {
        checks++;
        if (!condition) throw new AssertionError(message);
    }

    private static void rejects(String path) throws Exception {
        boolean rejected = false;
        try { OfflineRuntimeInstaller.validateRelativePath(path); }
        catch (java.io.IOException expected) { rejected = true; }
        check(rejected, "must reject path: " + path);
    }

    public static void main(String[] args) throws Exception {
        check("usr/bin/python3".equals(OfflineRuntimeInstaller.validateRelativePath("usr/bin/python3")),
                "accept ordinary POSIX relative paths");
        rejects("");
        rejects("/etc/passwd");
        rejects("../escape");
        rejects("usr/../escape");
        rejects("usr//bin");
        rejects("usr/./bin");
        rejects("usr\\bin");
        rejects("usr/bin/");

        OfflineRuntimeInstaller.validateSymlinkTarget("usr/lib/tool", "../../bin/tool");
        OfflineRuntimeInstaller.validateSymlinkTarget("usr/lib/tool", "/system/bin/linker64");
        boolean escaped = false;
        try { OfflineRuntimeInstaller.validateSymlinkTarget("bin/tool", "../../outside"); }
        catch (java.io.IOException expected) { escaped = true; }
        check(escaped, "relative symlink must not escape rootfs");
        check("../../bin/python3".equals(OfflineRuntimeInstaller.relativeLinkTarget("usr/bin/python", "bin/python3")),
                "hardlink target becomes relative symlink from link parent");
        check("target".equals(OfflineRuntimeInstaller.relativeLinkTarget("target-link", "target")),
                "top-level hardlink target remains relative");
        System.out.println("PASS: " + checks + " OfflineRuntimeInstaller checks");
    }
}
