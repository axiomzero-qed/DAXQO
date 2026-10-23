# DAXQO for XM

## 中文

DAXQO for XM 在 Termux 的私有 HOME 中运行独立的 Debian PRoot 容器 `daxqo-xm`。不需要 root；服务和配对数据保存在 Termux 私有目录。它使用 Debian、Python venv、固定 Codex runtime 0.163.0 和邮件依赖。不要把仓库放在 `/sdcard` 等共享存储中。

### 安装与运行

1. 从 [Termux 官方安装说明](https://github.com/termux/termux-app#installation)安装 Termux。使用支持的官方来源版本；不要混装不同签名来源的 Termux 插件。
2. 在 Termux 中取得包含 XM 支持的 DAXQO 源码，并确认 checkout 位于 Termux 私有 HOME 下。进入仓库后安装：

   ```sh
   bash scripts/install_xm.sh install
   bash scripts/install_xm.sh doctor
   ```

   安装器创建独立 Debian 容器、配置与 Termux 用户匹配的 `daxqo` 用户，并在 `~/.local/share/daxqo` 安装 backend、Python venv、Codex runtime 与邮件依赖。`doctor` 输出环境检查结果，不显示 API key 或配对 capability。

   安装器先更新 Termux 整套基础包，避免旧 bootstrap 的 curl/OpenSSL ABI 冲突。PRoot-Distro v5 使用 Termux 官方 Debian trixie 系统包，并验证固定 SHA-256；不依赖 Docker Hub。若手机下载 GitHub 较慢，可在电脑下载[官方 ARM64 系统包](https://github.com/termux/proot-distro/releases/download/v4.29.0/debian-trixie-aarch64-pd-v4.29.0.tar.xz)，传入 Termux 私有 HOME 后设置 `DAXQO_XM_ROOTFS=/绝对路径/系统包.tar.xz` 再执行安装；安装器仍执行同一哈希校验。

   Debian 的 glibc 需要独立 DNS 配置。安装器仅在该容器内设置 [AliDNS](https://help.aliyun.com/en/dns/public-dns-free-version-access-speed-limit-notification-dns) `223.5.5.5 / 223.6.6.6`，可用 `DAXQO_XM_DNS="服务器IP1 服务器IP2"` 在安装时覆盖；Android 全局 DNS 不受影响。

3. 使用 XM 构建的 Android APK（包名 `io.daxqo.xm`）。Windows 构建命令：

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\android\build.ps1 -Xm -Lint
   ```

   XM 配对链接使用 `daxqo-xm://connect#token=…`。在 Termux 私有 checkout 中执行 `bash scripts/install_xm.sh pair` 会显示链接；将它视为秘密，只导入自己的 DAXQO for XM。
4. 在 DAXQO for XM 连接区点“打开 Termux”，或手动切换到 Termux，启动服务：

   ```sh
   daxqo-xm start
   ```

   保持该前台会话运行；可以切换到 DAXQO App。停止服务时在 Termux 中按 Ctrl+C。`daxqo-xm doctor` 检查环境，`daxqo-xm status` 检查后台、进程归属和 Codex 连接，`daxqo-xm pair` 重新显示配对链接，`daxqo-xm test` 运行 guest 单元测试。模型 API key 需在 XM 设置中另行填写，环境安装器不复制其他应用的密钥。

### 小米系统设置与边界

- 在 HyperOS 的应用电池/后台管理中允许 Termux 与 DAXQO 后台运行，并按设备提供的选项允许 Termux 自启动。不同 HyperOS 版本和地区的菜单名称可能不同；请以[小米 HyperOS 开发者文档](https://dev.mi.com/xiaomihyperos/documentation/detail?pId=1624)为参考，并在手机上手动确认权限。
- 本次真机在默认“智能限制后台运行”时，切到 XM 后 Termux 的本机 HTTP 服务会无响应；切回 Termux 则恢复。将“Termux → 应用信息 → 省电策略 → 无限制”设好后，切换应用可正常连接。该项是本机验收的必要环境设置，安装 shell 脚本不能代替系统设置。
- 如果 Android 提示受限设置，用户需从 DAXQO for XM 的系统应用信息页手动允许受限设置，再按需启用它的辅助功能服务。DAXQO 不会替用户更改这些系统权限。
- PRoot Debian 使用 Termux 的应用 UID 运行，不是完整 Linux 虚拟机，也不提供 init/systemd。不要同时启动原版 DAXQO Linux 服务：两者使用相同的 loopback 端口 `18880`、`18882`。
- `start` 持有 Termux wake lock，并以前台 Termux 会话承载服务。可以切换应用，但需保持 Termux 会话中的进程运行。HyperOS 仍可能因省电、内存回收、重启或用户强制停止而终止它；不保证息屏长跑或 24 小时保活。
- Termux 与 PRoot 的范围及限制见 [Termux](https://github.com/termux/termux-app#installation) 和 [proot-distro](https://github.com/termux/proot-distro)。仅安装官方 Termux；Android App 只打开 Termux，不通过权限桥执行命令。

### 当前验收状态

2026-10-23，在 Xiaomi 2410DPN6CC、HyperOS `OS3.0.309.0.WOBCNXM`、Android 16、ARM64、WebView 155 上完成：

- 已安装并配对独立 XM APK（`io.daxqo.xm`）、官方 Termux 0.118.3、PRoot-Distro 5.10.2、Debian trixie、Python 3.13 venv、Node 20 / npm、Codex 0.163.0、dkimpy / dnspython、证书、Git / ripgrep 与 DNS。
- 环境自检 15 项通过；手机本机 backend、运行桥和 Codex App Server 连接正常，WebView 显示“已连接”；强制停止并冷启动 XM 后仍能连接。已为本机 Termux 配置省电策略“无限制”。
- 息屏 30 秒后，本机 backend、进程归属和 Codex 连接仍正常；从手机 Debian 访问 DeepSeek HTTPS 返回预期的未认证 HTTP 401。该检查未发送模型任务，也不等于长时间保活验收。
- 原有 153 项后端测试在手机上全部通过，另新增 1 项 Android 内核无 `task/*/children` 时的进程发现回归通过。修复前曾复现 detached 子进程回收失败；修复保留 UID、PID 启动时间和发信号前身份验证。
- XM 构建与 Lint 通过；Android passive callbacks 2000 次、审批/唤醒 292 断言与 64 并发重试通过。WSL 原有 153 项为 137 passed、16 skipped，新增内核兼容回归与 3 项安装/配对测试通过。
- 尚未填写模型 API key；真实模型任务、手机控制授权、邮件账户闭环与长时间息屏任务尚未验收。

## English

DAXQO for XM runs in a separate Debian PRoot container named `daxqo-xm`, with its checkout and app data kept under Termux's private HOME. It needs no root access. See the steps above for installation, pairing, and startup. Build the Android app with `android/build.ps1 -Xm -Lint`; the XM package is `io.daxqo.xm` and its pairing scheme is `daxqo-xm`.

Keep the Termux foreground session running after `daxqo-xm start`. On the tested HyperOS device, set Termux's battery policy to Unrestricted so its HTTP service remains available while DAXQO is visible. The app does not grant restricted settings or accessibility permissions for you. Do not run the original DAXQO Linux service alongside XM because they share local ports. Installation, pairing, local Codex initialization, cold app launch, a 30-second screen-off connectivity check and device unit tests passed. A model API key is still required; actual model tasks and long screen-off runs remain unverified.
