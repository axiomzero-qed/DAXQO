package io.daxqo.app;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;

/** Identifies a UI target without trusting an old screen coordinate or a reused row index. */
final class NodeSnapshot {
    static final class Node {
        final String id, text, description, viewId, className;
        final int left, top, right, bottom;
        final boolean visible, enabled, password;
        Node(String id, String text, String description, String viewId, String className,
             int left, int top, int right, int bottom, boolean visible, boolean enabled, boolean password) {
            this.id = id; this.text = text; this.description = description; this.viewId = viewId; this.className = className;
            this.left = left; this.top = top; this.right = right; this.bottom = bottom;
            this.visible = visible; this.enabled = enabled; this.password = password;
        }
        boolean usable(int width, int height) {
            return visible && enabled && right > left && bottom > top
                    && Math.max(0, left) < Math.min(width, right) && Math.max(0, top) < Math.min(height, bottom);
        }
        boolean sameIdentity(Node other) {
            return other != null && Objects.equals(text, other.text) && Objects.equals(description, other.description)
                    && Objects.equals(viewId, other.viewId) && Objects.equals(className, other.className)
                    && enabled == other.enabled && password == other.password;
        }
    }
    final String id, packageName;
    private final Map<String, Node> nodes = new LinkedHashMap<>();
    NodeSnapshot(String id, String packageName, List<Node> nodes) {
        this.id = id; this.packageName = packageName;
        for (Node node : nodes) this.nodes.put(node.id, node);
    }
    private static String parent(String id) {
        int dot = id.lastIndexOf('.');
        return dot < 0 ? id : id.substring(0, dot);
    }
    Node match(String requestedSnapshot, String nodeId, NodeSnapshot current, int width, int height) {
        if (!id.equals(requestedSnapshot) || packageName.isEmpty() || !packageName.equals(current.packageName)) return null;
        Node before = nodes.get(nodeId), now = current.nodes.get(nodeId);
        if (before == null || now == null || !before.usable(width, height) || !now.usable(width, height)
                || !before.sameIdentity(now)) return null;
        // Button labels such as "Install" repeat. Include the surrounding row,
        // so inserting an ad cannot silently retarget the same child path.
        String anchor = parent(parent(nodeId));
        for (Map.Entry<String, Node> entry : nodes.entrySet()) {
            String key = entry.getKey();
            if ((key.equals(anchor) || key.startsWith(anchor + ".")) && !entry.getValue().sameIdentity(current.nodes.get(key))) return null;
        }
        for (String key : current.nodes.keySet()) {
            if ((key.equals(anchor) || key.startsWith(anchor + ".")) && !nodes.containsKey(key)) return null;
        }
        return now;
    }
}
