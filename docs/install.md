# 在已有 Linux 环境中安装 DAXQO

## 中文

`install_linux.py` 将 DAXQO 安装为当前 Linux 环境中的独立应用。它只在 Linux 已经运行时工作，不创建或唤醒厂商管理的容器。StepX 的 Linux 容器仍是厂商依赖；容器运行后，DAXQO 可自行启动服务、管理会话和任务，不必用 Amoo 助手聊天来启动 runtime 或发送任务。

需要 Python 3.10+，支持 `arm64`/`aarch64` 或 `x64`/`x86_64`。在仓库根目录运行：

```sh
python3 scripts/install_linux.py
```

默认应用目录为 `~/.local/share/daxqo`。安装器复制 backend、安装或复用 Codex runtime `0.163.0`、在独立 session 中启动 supervisor，并检查 `127.0.0.1:18880` 的本机健康接口。它不会连接 Android 设备，也不读取模型 API key 或 PC 凭据。请通过 DAXQO 本机 GUI 配置模型 API。

默认 sandbox 为 `danger-full-access`，任务可访问该 Linux 用户可访问的文件与进程。需要时明确选择较窄的执行策略；安装器不会静默更改所选策略：

```sh
python3 scripts/install_linux.py --sandbox workspace-write
python3 scripts/install_linux.py --sandbox read-only
python3 scripts/install_linux.py --home "$HOME/apps/daxqo" --sandbox workspace-write
```

也可复用已解压的官方 Codex runtime 目录（包含可执行文件的目录）；安装器复制普通文件到管理的版本化目录，并验证其版本为 `0.163.0`：

```sh
python3 scripts/install_linux.py --runtime-dir /path/to/extracted/codex-runtime
```

未指定 `--runtime-dir` 时，安装器从 [npm 官方 registry](https://registry.npmjs.org/) 查询固定平台包 `@openai/codex`，版本为 `0.163.0-linux-arm64` 或 `0.163.0-linux-x64`。下载后先按包元数据中的 SHA-512 `dist.integrity` 校验 tarball。仅允许解包普通文件和目录，并拒绝越界路径、链接、特殊文件、重复项和过大归档。下载缓存位于 `--home/cache`。

持久数据位于 `state/` 和 `workspace/`。重复运行会保留这两个目录以及已有 `workspace/AGENTS.md`；仅首次安装时复制 workspace 模板。`state/capability` 是随机生成、仅 owner 可读写的本机配对凭据，后续运行会复用；默认不会打印。要显式生成配对链接：

```sh
python3 scripts/install_linux.py --print-pairing
```

将输出的 `daxqo://connect#token=…` 视为秘密，只导入自己的 DAXQO App。Capability 授权访问本机 harness，不是模型 API key。Supervisor 日志位于 `state/supervisor.log`，安装日志位于 `logs/install.log`，权限均限 owner。

如果 `18880` 已有未知进程监听，安装器会拒绝向其发送 capability。健康检查通过只说明该 Linux 环境中的 DAXQO backend 正在运行，不表示 DAXQO 可以启动或保持厂商容器运行。

V0.3 Android App 每条消息最多导入 6 个文件、每个最多 20 MiB 到 Linux workspace。文件由系统文档选择器和本地 DAXQO 服务传递；模型能否理解图片取决于所选模型与 runtime。定时任务支持一次、每日北京时间和间隔执行，仅在 Linux 服务在线时检查。Android App 与安装器都不会启动或保活 vendor 容器。

默认执行设置：并发 2（可调 1–4）、最多 2 个子智能体且深度 1、上下文压缩阈值 32000 tokens、窗口 65536 tokens。曾以 8000/16384 临时值完成受控压缩检查，之后恢复默认；这不替代长时间负载测试。此前已验证关闭 App、撤掉电脑调试通道后执行一次定时任务，详见 [V0.3 验收记录](V0.3-ACCEPTANCE.md)。

---

## English

`install_linux.py` installs DAXQO as an independently managed app inside the current Linux environment. Linux must already be running; the installer does not create or wake vendor-managed containers. On StepX, the Linux container remains a vendor dependency. Once it is running, DAXQO can manage its own service, conversations, and tasks without using the Amoo assistant chat to start the runtime or send tasks.

Requirements: Python 3.10 or newer and `arm64`/`aarch64` or `x64`/`x86_64`. Run this from the repository root:

```sh
python3 scripts/install_linux.py
```

The default app directory is `~/.local/share/daxqo`. The installer copies the backend, installs or reuses Codex runtime `0.163.0`, starts the supervisor in a separate session, and checks the local health endpoint at `127.0.0.1:18880`. It does not contact the Android device or read model API keys or PC credentials. Configure the model API through DAXQO's local GUI.

The default sandbox is `danger-full-access`, which lets tasks access files and processes available to that Linux user. Choose a narrower policy explicitly when needed; the installer never silently changes the selected policy:

```sh
python3 scripts/install_linux.py --sandbox workspace-write
python3 scripts/install_linux.py --sandbox read-only
python3 scripts/install_linux.py --home "$HOME/apps/daxqo" --sandbox workspace-write
```

You can reuse an already-extracted official Codex runtime by passing its directory (the directory containing the executable). The installer copies regular files into its managed versioned layout and verifies version `0.163.0`:

```sh
python3 scripts/install_linux.py --runtime-dir /path/to/extracted/codex-runtime
```

Without `--runtime-dir`, the installer queries the pinned platform package `@openai/codex` from [npm's official registry](https://registry.npmjs.org/), using version `0.163.0-linux-arm64` or `0.163.0-linux-x64`. It verifies the downloaded tarball against the SHA-512 `dist.integrity` in package metadata before extraction. Only regular files and directories are extracted; paths escaping the destination, links, special files, duplicate entries, and oversized archives are rejected. The download cache is stored under `--home/cache`.

Durable data lives in `state/` and `workspace/`. Re-running the installer preserves both directories and any existing `workspace/AGENTS.md`; the workspace template is copied only on first install. `state/capability` is a randomly generated local pairing credential with owner-only permissions and is reused on later runs. It is not printed by default. To explicitly create a pairing link:

```sh
python3 scripts/install_linux.py --print-pairing
```

Treat the displayed `daxqo://connect#token=…` link as a secret and import it only into your DAXQO app. The capability authorizes the local harness; it is not a model API key. Supervisor logs are in `state/supervisor.log` and installer logs are in `logs/install.log`; both are owner-only.

If an unknown process already listens on port `18880`, the installer refuses to send it the capability. A successful health check only confirms that DAXQO's backend is running in this Linux environment; it does not mean DAXQO can start or keep a vendor container alive.

The V0.3 Android app can import up to 6 files per message, each up to 20 MiB, into the Linux workspace. Files pass through Android's system document picker and the local DAXQO service. Image understanding depends on the selected model and runtime. Scheduled tasks support one-time, daily China Standard Time, and interval runs; they are checked only while the Linux service is online. Neither the Android app nor the installer starts or keeps a vendor-managed container alive.

Default execution settings are concurrency 2 (configurable from 1 to 4), at most 2 subagents at depth 1, a 32000-token compaction threshold, and a 65536-token context window. A controlled compaction check used temporary 8000/16384 settings and defaults were then restored; it does not replace long-duration load testing. Earlier acceptance verified one scheduled task with the app closed and PC forwarding removed; see the [V0.3 acceptance record](V0.3-ACCEPTANCE.md).
