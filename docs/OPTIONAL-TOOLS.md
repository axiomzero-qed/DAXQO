# Optional tools / 可选独立工具

DAXQO 主仓库提供 Android App、手机桥和任务 Harness。采集器和 Motor 在各自的 MIT 开源仓库维护，采用独立版本、安装和测试；DAXQO 的标准安装不依赖这两个 Python 包。官网源码不属于本仓库的发布内容。

| 工具 | 独立仓库 | 版本与用途 |
| --- | --- | --- |
| Trajectory Collector | [DAXQO-Trajectory-Collector](https://github.com/axiomzero-qed/DAXQO-Trajectory-Collector) | [v0.1.0](https://github.com/axiomzero-qed/DAXQO-Trajectory-Collector/tree/v0.1.0)：轨迹格式、隐私过滤、数据集、CLI 和 MCP |
| Motor | [DAXQO-Motor](https://github.com/axiomzero-qed/DAXQO-Motor) | [v0.1.0](https://github.com/axiomzero-qed/DAXQO-Motor/tree/v0.1.0)：独立手机操作接口、设备适配器、CLI 和 MCP |

其他 Harness 可直接接入各工具的 Python API、CLI 或 MCP，不必启动 DAXQO 的 Agent 服务。使用方法和兼容性以对应版本的文档为准。DAXQO 主仓库不再保存采集器和 Motor 的实现副本。

需要原生轨迹观察或 Motor 手机桥扩展时，请使用独立仓库中的显式 staging 构建流程：

- [Collector Android integration v0.1.0](https://github.com/axiomzero-qed/DAXQO-Trajectory-Collector/blob/v0.1.0/docs/INTEGRATION.md)
- [Motor native extension v0.1.0](https://github.com/axiomzero-qed/DAXQO-Motor/blob/v0.1.0/native/README.md)

这两个 v0.1.0 流程固定使用 DAXQO 提交 `3de65f9e9751c67d7118e72e789f898901a50ce6`，不是自动跟随 `main`。它们在指定的独立目录生成扩展源码，不修改输入源码；普通 DAXQO APK 不包含这些可选原生扩展。请按工具文档选择正确的 APK，采集需由用户明确启用。

## English

The DAXQO repository owns the Android app, phone bridge and task harness. Collector and Motor are independently versioned MIT projects, with their own installation and tests. The normal DAXQO installation does not require either Python package. Website source is outside this repository's release scope.

Other harnesses can use either project's Python API, CLI or MCP without running the DAXQO agent server. The table above links to the v0.1.0 sources; follow each project's versioned integration documentation. Their implementation is maintained in those repositories rather than copied here.

Optional native observation and Motor bridge extensions use explicit staging against DAXQO commit `3de65f9e9751c67d7118e72e789f898901a50ce6`, not a floating `main`. Staging writes a separate source directory and leaves the selected input unchanged. A standard DAXQO APK does not contain these native extensions. Choose the appropriate APK using those guides; collection requires explicit user activation.
