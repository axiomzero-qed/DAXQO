package io.daxqo.app;

import java.util.Arrays;
import java.util.List;

public final class NodeSnapshotRegression {
    private static final int WIDTH = 1080, HEIGHT = 2400;
    private static int checks;

    private static NodeSnapshot.Node node(String id, String text, int l, int t, int r, int b,
                                          boolean visible, boolean enabled) {
        return new NodeSnapshot.Node(id, text, "", "", "Button", l, t, r, b, visible, enabled, false);
    }
    private static NodeSnapshot snapshot(String ident, String pkg, String rowText, int buttonTop,
                                         boolean visible, boolean enabled, List<NodeSnapshot.Node> extra) {
        java.util.ArrayList<NodeSnapshot.Node> nodes = new java.util.ArrayList<>();
        nodes.add(node("0", "", 0, 0, WIDTH, HEIGHT, true, true));
        nodes.add(node("0.1", "结果列表", 0, 1000, WIDTH, 2000, true, true));
        nodes.add(node("0.1.2", rowText, 0, 1100, WIDTH, 1300, true, true));
        nodes.add(node("0.1.2.1", "安装", 700, 1150, 900, 1220, true, true));
        nodes.add(node("0.1.2.2", "安装", 900, buttonTop, 1060, buttonTop + 68, visible, enabled));
        nodes.addAll(extra);
        return new NodeSnapshot(ident, pkg, nodes);
    }
    private static void check(boolean condition, String message) {
        checks++;
        if (!condition) throw new AssertionError(message);
    }

    public static void main(String[] args) {
        NodeSnapshot original = snapshot("snap-a", "com.xiaomi.market", "途虎养车", 1157, true, true,
                Arrays.asList(node("0.2", "后续内容", 0, 1300, 1080, 1400, true, true)));
        NodeSnapshot currentOriginalBounds = snapshot("snap-b", "com.xiaomi.market", "途虎养车", 1157, true, true,
                Arrays.asList(node("0.2", "后续内容", 0, 1300, 1080, 1400, true, true)));
        NodeSnapshot.Node target = original.match("snap-a", "0.1.2.2", currentOriginalBounds, WIDTH, HEIGHT);
        check(target != null && target.left == 900 && target.top == 1157 && target.right == 1060 && target.bottom == 1225,
                "Tuhu target should match the supplied 1080x2400 bounds");
        NodeSnapshot currentMoved = snapshot("snap-b", "com.xiaomi.market", "途虎养车", 1170, true, true,
                Arrays.asList(node("0.2", "后续内容", 0, 1300, 1080, 1400, true, true)));
        target = original.match("snap-a", "0.1.2.2", currentMoved, WIDTH, HEIGHT);
        check(target != null && target.top == 1170 && target.bottom == 1238,
                "bounds-only movement should return current bounds");

        NodeSnapshot changedRow = snapshot("snap-b", "com.xiaomi.market", "招商广告", 1170, true, true,
                Arrays.asList(node("0.2", "后续内容", 0, 1300, 1080, 1400, true, true)));
        check(original.match("snap-a", "0.1.2.2", changedRow, WIDTH, HEIGHT) == null,
                "same Install path in a different row must be rejected");
        check(original.match("wrong-snapshot", "0.1.2.2", currentMoved, WIDTH, HEIGHT) == null, "snapshot change must reject");
        check(original.match("snap-a", "0.1.2.2", snapshot("snap-b", "other.package", "途虎养车", 1170, true, true,
                Arrays.asList(node("0.2", "后续内容", 0, 1300, 1080, 1400, true, true))), WIDTH, HEIGHT) == null,
                "package change must reject");

        for (NodeSnapshot.Node invalid : Arrays.asList(
                node("0.1.2.2", "安装", 900, 1170, 1060, 1238, false, true),
                node("0.1.2.2", "安装", 900, 1170, 1060, 1238, true, false),
                node("0.1.2.2", "安装", 1081, 1170, 1100, 1238, true, true),
                node("0.1.2.2", "安装", 900, 1238, 1060, 1170, true, true))) {
            List<NodeSnapshot.Node> invalidTree = Arrays.asList(node("0", "", 0, 0, WIDTH, HEIGHT, true, true),
                    node("0.1", "结果列表", 0, 1000, WIDTH, 2000, true, true),
                    node("0.1.2", "途虎养车", 0, 1100, WIDTH, 1300, true, true),
                    node("0.1.2.1", "安装", 700, 1150, 900, 1220, true, true), invalid);
            NodeSnapshot current = new NodeSnapshot("snap-b", "com.xiaomi.market", invalidTree);
            check(original.match("snap-a", "0.1.2.2", current, WIDTH, HEIGHT) == null,
                    "invisible, disabled, out-of-bounds and reversed targets must reject");
        }

        NodeSnapshot changedSubtree = snapshot("snap-b", "com.xiaomi.market", "途虎养车", 1170, true, true,
                Arrays.asList(node("0.1.2.3", "新增控件", 10, 1225, 500, 1290, true, true),
                        node("0.2", "后续内容", 0, 1300, 1080, 1400, true, true)));
        check(original.match("snap-a", "0.1.2.2", changedSubtree, WIDTH, HEIGHT) == null,
                "subtree structure change within the two-ancestor anchor must reject");
        System.out.println("PASS: " + checks + " NodeSnapshot checks");
    }
}
