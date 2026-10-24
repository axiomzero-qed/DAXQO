# DAXQO for XM

## 中文

DAXQO for XM 是同一仓库、同一 Android 主源码和同一 backend 的小米平台配置，不含第二套 Agent 核心。Standard 是基准版本（基准提交 `8374d89`）；XM 通过应用标识、配对 scheme 和平台提示形成独立 APK。现有对话、任务、文件、审批、调度与邮件功能沿用共享核心。

### 构建

Windows 下在仓库根目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\android\build.ps1 -Xm -Lint
```

XM APK 使用包名 `io.daxqo.xm` 和配对 scheme `daxqo-xm`。显式平台别名也可用于 Gradle：`-Pplatform=xm`；Standard 为 `-Pplatform=standard`。现有 `-Xm` / `-Pxm=true` 仍可用。Standard APK 默认包名为 `io.daxqo.app`，配对 scheme 为 `daxqo`。

### Linux 前置条件与运行

XM 复用 Standard 的 Linux 安装器和服务协议，不移植 STEPX Neo 的厂商系统镜像，也不假定小米自带兼容 Linux。仅当设备已有且正在运行的兼容 Linux 环境满足前置条件时，才可从仓库根目录执行：

```sh
python3 scripts/install_linux.py
python3 scripts/install_linux.py --print-pairing --pairing-scheme daxqo-xm
```

随后由安装器配置的 supervisor 管理服务；运行桥使用本机端口 `18882`，backend 使用 `18880`。DAXQO 不创建或唤醒厂商 Linux 环境。Termux/PRoot 安装流程已退出当前支持路径；历史实验记录见 [XM-ACCEPTANCE](XM-ACCEPTANCE.md)，其结果不证明 XM 可直接运行 Neo 厂商 Linux，也不证明这条共用安装路径已完成端到端验收。

Android 的 PlatformProfile 提供小米系统设置提示。根据设备和系统版本，用户可能需要手动配置后台运行、省电策略、锁屏显示或弹窗权限；应用不会静默授予权限。若选择以 Termux 承载本机服务，DAXQO 和 Termux 都必须设为“无限制”、允许后台运行，并保持 Termux 会话；任一应用受限都可能导致本机服务中断。当前 K70 用户已手动为 Termux 开启无限制后台运行；这是该设备的运行前置，不代表 Termux 在其他机型上正式受支持。Accessibility 是当前 MCP phone backend；Shizuku 和 OEM backend 尚不可用，选中时会明确拒绝，不代表已有实现。

### 验证范围

XM 新路径的手机端到端运行尚未验证。Android 安全锁屏、长期息屏运行及所有小米/其他 OEM 系统版本均未验证。请将 `XM-ACCEPTANCE.md` 视为旧 Termux/PRoot 路径的历史实验记录，不作为当前架构的兼容性或验收证明。

## English

DAXQO for XM is the Xiaomi platform configuration of the same repository, Android main source, and backend. It does not introduce a second Agent core. Standard remains the baseline (baseline commit `8374d89`); XM produces a separate APK through its application ID, pairing scheme, and platform guidance. Existing conversations, tasks, files, approvals, schedules, and email features use the shared core.

Build on Windows from the repository root:

```powershell
powershell -ExecutionPolicy Bypass -File .\android\build.ps1 -Xm -Lint
```

The XM APK uses application ID `io.daxqo.xm` and pairing scheme `daxqo-xm`. Gradle also accepts the explicit aliases `-Pplatform=xm` and `-Pplatform=standard`; the existing `-Xm` / `-Pxm=true` forms remain supported. Standard defaults to application ID `io.daxqo.app` and pairing scheme `daxqo`.

XM reuses the Standard Linux installer and service protocol; it does not port the STEPX Neo system image or assume Xiaomi provides compatible Linux. Only if a compatible Linux environment already exists and is running on the device, run from the repository root:

```sh
python3 scripts/install_linux.py
python3 scripts/install_linux.py --print-pairing --pairing-scheme daxqo-xm
```

The configured supervisor manages the service; the runtime bridge uses local port `18882` and the backend uses `18880`. DAXQO does not create or wake the vendor Linux environment. Termux/PRoot installation has left the supported path. The [XM acceptance record](XM-ACCEPTANCE.md) documents an earlier experiment and does not prove compatibility with Neo vendor Linux or end-to-end operation of the current shared path.

Android's PlatformProfile provides Xiaomi settings guidance. Depending on the device and OS version, users may need to configure background operation, battery policy, lock-screen display, or pop-up permissions manually; the app does not grant them silently. If Termux is chosen to host local services, both DAXQO and Termux must use the unrestricted battery policy, be allowed to run in the background, and the Termux session must stay active; restricting either app can interrupt local services. The current K70 owner has manually enabled unrestricted background operation for Termux. This is a prerequisite for that device and does not establish official Termux support on other models. Accessibility is the current MCP phone backend. Shizuku and OEM backends are unavailable; selecting them is explicitly rejected.

End-to-end operation of the current XM path has not been verified on a phone. Secure lock-screen behavior, long screen-off runs, and all Xiaomi or other OEM versions remain unverified. Treat `XM-ACCEPTANCE.md` as a historical record of the old Termux/PRoot experiment, not proof of compatibility or acceptance for the current architecture.
