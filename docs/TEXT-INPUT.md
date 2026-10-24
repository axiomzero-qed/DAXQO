# 中文与光标输入

`android_action` 的 `input` 操作一次提交完整 Unicode 文本，包括中文、多行和 emoji。先点击目标输入框，再观察焦点；无需逐键输入拼音。

```json
{
  "action": "input",
  "expectedPackage": "目标应用包名",
  "args": {"text": "你好，中文😀\n第二行", "mode": "insert"}
}
```

- `mode: "insert"` 为默认值，在当前光标处插入；有选区或正在组合的输入时，交由系统编辑器按输入法语义替换。
- `mode: "replace"` 明确替换整个文本框，通过目标节点的文本设置接口执行。
- 指定 `nodeId` 时必须同时携带最新 `snapshotId`；也可省略二者，操作当前获得输入焦点的编辑器。
- 每次请求最多 8000 个 UTF-16 单元；一个常见 emoji 通常占两个单元。拒绝无效代理项、错误模式或不完整节点绑定。

Android 13+ 使用无障碍服务的官方 `FLAG_INPUT_METHOD_EDITOR` / `InputConnection.commitText` 通道。在应用提供有效编辑器连接时，可对原生文本框、WebView textarea 和 contenteditable 整段输入，不切换用户输入法，不使用剪贴板。

系统输入连接为异步接口。返回 `method: "input_connection"`、`submitted: true`、`verificationRequired: true` 代表已提交，随后必须观察实际文本。不要因未立即看到文字就重复输入；同一个动作 ID 的重试由手机桥去重。

旧系统或无输入连接的标准编辑框，使用无障碍文本接口按已知选区保留前后文，并恢复光标。不知道选区时停止，不猜测追加位置；密码框不通过读取遮罩字符拼接原文。应用没有可用输入通道时，会返回明确错误，应修正焦点或报告该应用的限制。

输入继续遵守手机控制的暂停、锁屏、前台应用、目标快照和单次批准规则。输入操作使旧快照失效；提交后请重新观察。

## 回归

- `android/test-text-input.ps1`：中文、选区、emoji、UTF-16 边界与无效光标的纯 Java 回归。
- `backend/test_android_mcp.py`：协议参数校验、焦点/选区输出和密码信息清理。
- `android/input-fixture/`：无权限、无网络访问的隔离输入验收 APK。仅用于开发测试。
- `scripts/test_device_text_input.py`：在明确选中的 USB 手机运行真实输入回归，验证原生输入框、WebView、选区、动作去重及过期快照。

开发测试需先安装更新的 DAXQO 与输入验收 APK，保持 Linux 服务在线，且无正在运行的用户任务。设置 `DAXQO_ADB`、`DAXQO_SERIAL` 后运行设备脚本；脚本不会更改手机权限模式或替用户批准操作。

Android API 依据：[AccessibilityServiceInfo](https://developer.android.com/reference/android/accessibilityservice/AccessibilityServiceInfo#FLAG_INPUT_METHOD_EDITOR)、[AccessibilityInputConnection](https://developer.android.com/reference/android/accessibilityservice/InputMethod.AccessibilityInputConnection#commitText(java.lang.CharSequence,int,android.view.inputmethod.TextAttribute))。

## 真机回归（2026-10-24）

Standard profile 的 Neo、Android 16 上，7 项真机回归全部通过：`native-middle`、`native-selection`、`native-emoji`、WebView `textarea`、WebView `contenteditable`、指定节点的多行 emoji 插入、整框 `replace`。所有插入场景均通过 `input_connection` 提交。每项均观察实际文本，验证同一动作 ID 重试不会重复写入；指定节点的用例另确认过期快照会被拒绝。测试片段耗时为 93–281 ms；这只是手机输入片段的耗时，不代表完整模型任务耗时。

XM profile 已通过同源码构建和 profile 检查；尚无小米真机输入验收，不应视为已验证兼容。
