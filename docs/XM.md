# DAXQO for XM

> **V1.0 当前路径：** V1.0 单 APK 的当前真机验收仅在 STEPX Neo 完成；本轮没有操作或验收小米设备。本文 XM 包名、Linux 前置条件与兼容构建说明保留给既有 V0.x 安装维护使用，不能据此推断小米已通过 V1.0 验收。V1.0 的取消链路和重启恢复仍待补验，见 [V1.0 指南](V1.0.md)。

V0.x 国内镜像及面向 WorkBuddy 的环境准备步骤见 [USB 部署指南](WORKBUDDY-DEPLOY.md)和[下载实测](MIRRORS-CN.md)。旧通用 APK 和 Neo 外部 Linux 用户环境记录见[架构说明](ARCHITECTURE.md)及[验收记录](NEO-PORTABLE-ACCEPTANCE.md)。

## 中文

DAXQO 使用同一 Android 主源码和 backend。默认通用 APK 为 `io.daxqo.app`，适用于 Neo、小米等设备；`PlatformProfile.forDevice` 按真实厂商显示小米设置提示。`-Xm` 仅生成旧包名 `io.daxqo.xm` 和 scheme `daxqo-xm`，供既有 XM 安装覆盖升级，不是单独维护的主版本。两种包共享功能与源码。

### 构建

Windows 下在仓库根目录构建默认通用 APK：

```powershell
powershell -ExecutionPolicy Bypass -File .\android\build.ps1
```

旧 XM 安装需要覆盖升级时，构建兼容包：

```powershell
powershell -ExecutionPolicy Bypass -File .\android\build.ps1 -Xm -Lint
```

兼容包使用 `io.daxqo.xm` / `daxqo-xm`；默认包使用 `io.daxqo.app` / `daxqo`。Gradle 的 `-Pplatform=xm|standard` 与 `-Pxm=true` 仍为兼容入口。

### Linux 前置条件与运行

手机端需要 Linux 用户环境。Neo 可使用 Termux + Ubuntu，无需先启动厂商 Linux；便携环境验收见[记录](NEO-PORTABLE-ACCEPTANCE.md)。从仓库根目录安装并生成配对信息：

```sh
python3 scripts/install_linux.py
python3 scripts/install_linux.py --print-pairing
```

当前通用包按真机厂商选择设置提示。Neo 便携环境验收使用隔离的 `19880` 测试服务，没有迁移现有数据或改动正式 `18880` / `18882` 服务；该结果不代表所有小米型号均已验收。旧 Termux/PRoot 路径的记录见 [XM-ACCEPTANCE](XM-ACCEPTANCE.md)，属于历史实验。

旧 XM 兼容包生成配对链接时增加 `--pairing-scheme daxqo-xm`。不同包名不能直接覆盖升级；既有 XM 安装可继续使用兼容包保留私有数据。

根据设备和系统版本，用户可能需要手动配置后台运行、省电策略、锁屏显示或弹窗权限；应用不会静默授予权限。若使用 Termux 承载服务，DAXQO 和 Termux 都需允许后台运行并保持会话。Accessibility 是当前 MCP phone backend；Shizuku 和 OEM backend 尚不可用，选中时会明确拒绝。

当前 K70 机主已手动允许 Termux 不受限后台运行；此设备前提不等于其它型号的后台验收。

### 验证范围

`XM-ACCEPTANCE.md` 是旧 Termux/PRoot 路径的历史记录。Neo 便携环境验收只覆盖记录中列出的 Neo 配置，不代表所有小米机型、Android 安全锁屏、长期息屏行为或全部 OEM 系统版本均已验证。

## English

DAXQO uses one Android main source tree and backend. The default general APK is `io.daxqo.app` for Neo, Xiaomi, and other devices; `PlatformProfile.forDevice` selects Xiaomi setup guidance from the actual manufacturer. `-Xm` builds the legacy `io.daxqo.xm` / `daxqo-xm` package only for upgrades of existing XM installs, not as a separately maintained release.

Build the default general APK on Windows from the repository root:

```powershell
powershell -ExecutionPolicy Bypass -File .\android\build.ps1
```

For an upgrade of a legacy XM install, build its compatibility package:

```powershell
powershell -ExecutionPolicy Bypass -File .\android\build.ps1 -Xm -Lint
```

The compatibility APK uses `io.daxqo.xm` / `daxqo-xm`; the default package uses `io.daxqo.app` / `daxqo`. Gradle's `-Pplatform=xm|standard` and `-Pxm=true` remain compatibility options.

The phone still needs a Linux userspace. Neo can use Termux + Ubuntu without starting vendor Linux; see the [portable-environment acceptance record](NEO-PORTABLE-ACCEPTANCE.md). Install and create pairing information from the repository root:

```sh
python3 scripts/install_linux.py
python3 scripts/install_linux.py --print-pairing
```

The current general package selects setup guidance from the device manufacturer. Neo portable validation used an isolated `19880` test service and did not migrate existing data or alter production `18880` / `18882` services; it does not establish compatibility across all Xiaomi models. The [XM acceptance record](XM-ACCEPTANCE.md) describes the earlier Termux/PRoot experiment.

For legacy XM pairing, add `--pairing-scheme daxqo-xm`. Different package IDs cannot replace one another through an in-place upgrade; existing XM users can retain their private data with the compatibility package.

Depending on the device and OS version, users may need to configure background operation, battery policy, lock-screen display, or pop-up permissions manually; the app does not grant them silently. If Termux hosts local services, allow both DAXQO and Termux to run in the background and keep the Termux session active. Accessibility is the current MCP phone backend. Shizuku and OEM backends are unavailable; selecting them is explicitly rejected.

The current K70 owner has manually enabled unrestricted background operation for Termux. This is a prerequisite on that device, not background acceptance for other models.

`XM-ACCEPTANCE.md` is a historical record of the old Termux/PRoot path. Neo portable validation covers only the configuration listed in its record; it does not establish compatibility across all Xiaomi models, secure lock-screen behavior, long screen-off runs, or every OEM version.
