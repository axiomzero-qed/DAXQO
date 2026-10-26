# DAXQO V0.5：交给 WorkBuddy 的 USB 部署指南

适用版本：0.5.0；更新日期：2026-10-25（北京时间）。无需自行编译 APK，也无需获取 Android Root。

## 给用户

1. 从 [WorkBuddy 官方网站](https://www.codebuddy.cn/work/)安装桌面客户端，选择你要使用的 **DeepSeek V4.1** 模型。名称以客户端实际提供的选项为准，找不到时先核对客户端或账号，不要把其它模型当成 V4.1。
2. 手机连接 USB，开启开发者选项和 USB 调试，并在手机上确认这台电脑的调试授权。
3. 给 WorkBuddy 本次部署目录、终端及所选手机的完整执行权限，将本页地址和下面的交办内容发给它。
4. 系统要求本人确认的界面、配对信息和模型 API 凭据，由你在本机完成；普通下载、校验、安装和检查交给 Agent。

**APK 只提供 Android 界面与手机控制桥。还需安装手机中的 Linux 后台、配对、配置模型，才算部署完成。** WorkBuddy 的模型额度或登录令牌不会自动成为 DAXQO 的模型凭据。

## 复制给 WorkBuddy

```text
请按 https://daxqo.com/install/workbuddy.md，为我通过 USB 部署 DAXQO V0.5。
我授权你在本次部署目录和选定手机上执行必要的下载、安装、配置和验收。
先读取 https://daxqo.com/downloads/manifest.json 和 https://daxqo.com/install/mirrors.json。
先识别手机型号、Android/CPU、ADB序列号、已有DAXQO和Linux环境；连接多台设备时先让我选择，不能选第一台就操作。
使用官网提供的Standard或XM预编译APK，逐一核对SHA256；不要让我编译，不要安装JDK/Gradle/完整Android SDK。
优先复用已运行的兼容Linux；XM缺少环境时，按本指南的实验性Termux/Ubuntu分支准备独立容器，保留原环境和数据。
下载使用本页已测直连源；手机Linux安装器指定 --registry npmmirror；保留TLS、包签名和哈希检查。
不要重复安装可选邮件/助手工具，不要卸载旧App、重建容器或重置state/workspace来解决问题。
安装后验证后台、正确版本、配对、模型连接及一个无害手机任务，并验证撤掉PC调试转发后手机仍能打开工作台。
只有实际验收通过才报告完成；失败要给具体阶段、错误与已保留的结果。
遇到必须本人操作的系统授权、配对或模型密钥填写，留在对应界面提醒我，完成后继续。不要把密钥、配对URL、手机号或私有日志发到聊天、网站或第三方。
```

## 机器入口与完成标准

- 下载清单：<https://daxqo.com/downloads/manifest.json>。包含两版 APK、公共部署源码包、字节数、SHA256、包名、版本和签名证书摘要。
- 国内下载源实测：<https://daxqo.com/install/mirrors.json>；人工说明：<https://daxqo.com/install/mirrors.md>。
- 部署源码包包含安装器和本指南，不含用户凭据。无需从 GitHub 克隆，不依赖 GitHub 下载代理。
- 当前为开发签名预览 APK。签名不兼容时不能通过卸载旧 App 清数据来强行更新；保留现场并报告。
- 下载实测与系统安装是两个验收层级。本次国内源已完整下载并核验关键包；全新 XM / Termux 路线仍为实验配置，待用户用 WorkBuddy 做端到端验收。

## 1. 探测：先了解现有设备

电脑端：

```sh
adb devices -l
adb -s SERIAL shell getprop ro.product.manufacturer
adb -s SERIAL shell getprop ro.product.model
adb -s SERIAL shell getprop ro.product.cpu.abi
adb -s SERIAL shell getprop ro.build.version.release
adb -s SERIAL shell pm path io.daxqo.app
adb -s SERIAL shell pm path io.daxqo.xm
adb -s SERIAL shell pm path com.termux
```

`SERIAL` 必须替换成用户选定的设备。`unauthorized` 等待手机确认；空列表检查数据线/USB模式/官方驱动。已有 ADB 可直接复用；没有时读取镜像清单的 `adb-windows` 项，下载平台工具、核对 SHA256，解压后运行 `adb version`。它是 Google 官方**直连**地址，不能称为国内镜像；不推荐过旧的华为镜像 ADB。

下载时可用 `curl.exe --noproxy "*" -fL -A "Mozilla/5.0" -o 文件 URL`。先下载到临时文件，核对清单中的字节数和 SHA256 再使用；Windows 可用 `Get-FileHash -Algorithm SHA256`。不要关闭证书验证。大包允许合理时间，失败先检查本次网络，禁止无限重试或改用不明代理站。

选择配置：STEPX Neo / 已兼容环境使用 Standard；小米配置使用 XM。两个包名不同，共用同一后台协议。存在既有后台时优先复用/受管升级，不要再启动占用相同端口的另一套后台。

## 2. 安装预编译 APK

从下载清单取得对应 APK，检查其哈希、包名及版本，再执行：

```sh
adb -s SERIAL install -r 对应的APK文件
```

Standard：`io.daxqo.app` / `0.5.0` / versionCode 5；XM：`io.daxqo.xm` / `0.5.0-xm` / versionCode 5。

USB 安装被系统拒绝时，改用手机正常安装界面并让本人完成实际要求的确认，不循环提交同一安装请求或关闭系统保护。不得使用 `adb uninstall`、降级或清应用数据作为默认修复。

## 3A. Standard / 已有兼容 Linux

在**手机已有的 Linux 终端或已经授权的设备执行通道**中检查：

```sh
uname -m
python3 --version
python3 -c "import ssl, sqlite3, urllib.request; print('python-ready')"
```

需要 Python 3.10+，ARM64/aarch64 或 x64/x86_64。核心后台使用 Python 标准库，不要求 Node/npm、JDK 或 Gradle。

已有 Linux 未启动时，通过该设备正常入口启动。不要将 Neo 的厂商系统镜像复制到小米，也不要修改厂商认证、端口或系统分区。

下载并校验本网站部署源码 ZIP，解压到独立目录，在源码根目录运行：

```sh
python3 scripts/install_linux.py --registry npmmirror
```

默认安装目录为 `~/.local/share/daxqo`，运行时固定为 Codex 0.163.0。安装器核对固定包名、版本、预置官方 SHA512，允许指定国内源及对应 CDN 后安全解包；不更改全局 npm/代理配置，也不静默回退到其它源。

已有安装：先识别实际 APP 路径，保留 `state/`、`workspace/`、模型配置及配对。正在执行的任务不得擅自中断。旧后台应通过已有受管更新/回退流程升级；不要在旧服务还占端口时误报新安装成功。需要正常重启时，核对进程属于该 APP 并确认无活动任务，只重启该应用服务，不改宿主系统服务。

## 3B. XM 首次环境准备：实验性 Termux / Ubuntu

仅在没有可复用的兼容 Linux、且用户选择尝试此路线时执行。此分支的下载材料已经实测；不同小米系统上的完整启动、手机控制与长期后台行为仍需逐机验收。

### 3B-1. Termux 宿主

已有 Termux 时保留其来源、签名和数据。没有时使用清单的 `termux-apk`：清华 F-Droid 镜像的稳定版 0.118.3，完整文件 SHA256 已核对；不要误选同目录的 beta 包。安装后先正常打开 Termux，等待初始化。

**ADB shell 不是 Termux shell，也不是 Ubuntu shell。** 不要把下面的 `pkg` 命令放进 `adb shell` 执行。通过 Termux 终端 UI 输入命令，或复用用户已授权的终端连接。不得为了访问私有目录而尝试 Root、利用漏洞或假定 `run-as com.termux` 可用。

只有 USB 通道时，可正常打开 Termux，确认初始化完成且终端输入获得焦点后，用 ADB 将 ASCII 命令整体输入，再发送 Enter。先用无害探测确认通道：

```sh
adb -s SERIAL shell am start -n com.termux/com.termux.app.TermuxActivity
adb -s SERIAL shell input text 'uname%s-m'
adb -s SERIAL shell input keyevent 66
```

`input text` 用 `%s` 表示空格；复杂命令需正确处理远端 shell 引号，不能直接拼接未转义的路径或用户输入。后续命令在 Termux 内执行，进入 Ubuntu 后再执行 Linux 安装器。没有可用焦点或系统限制输入时，报告实际限制，不能向其它 App 盲目输入。

在 Termux 中备份当前源文件，再使用官方文档对应的 USTC Termux 源：

```sh
cp "$PREFIX/etc/apt/sources.list" "$PREFIX/etc/apt/sources.list.daxqo-backup-$(date +%s)"
printf '%s\n' 'deb https://mirrors.ustc.edu.cn/termux/apt/termux-main stable main' > "$PREFIX/etc/apt/sources.list"
pkg update
pkg install -y proot-distro curl
proot-distro --version
proot-distro install --help
```

保留 APT 签名验证。若启用了其它 Termux 仓库，逐个依据其官方镜像说明处理，不混用 Ubuntu 源，不执行无关的全系统升级。该命令由包管理器解决传递依赖；本次实测样本包括 Python、proot、proot-distro 和 curl，不代表所有包版本永久不变。

### 3B-2. 使用已测的本地 rootfs，避开默认国外镜像拉取

从 `mirrors.json` 的 `ubuntu-rootfs-pinned` 获取日期固定的 Noble ARM64 rootfs，核对 SHA256：

```text
b68ef0444ca56ce4f00ae7e8ccfb0c553ffbc92e4a1b14ed7dd8aa18c6d68652
```

可以由 PC 下载并通过用户已授权的共享目录传入手机，也可在 Termux 直接下载到自己的 HOME。建议预留 5GB 可用空间，实际按下载、解包和缓存大小核对。不要下载虚拟机磁盘 `.img` / `.vhd` 代替 rootfs，也不要直接执行默认 `proot-distro install ubuntu` 后继续依赖国外容器仓库。

本次核对的 proot-distro 5.10.2 支持本地 rootfs 归档：

```sh
proot-distro list
proot-distro install /实际路径/noble-server-cloudimg-arm64-root.tar.xz --name daxqo-ubuntu
proot-distro login daxqo-ubuntu
```

若同名容器已存在则检查并复用，不 reset/remove。旧版命令行不同，应先查其 `--help`，不得猜参数或覆盖已有环境。

### 3B-3. Ubuntu 容器内

确认是预期的 Ubuntu Noble ARM64，再配置 Ubuntu **ports** 源 `https://mirrors.ustc.edu.cn/ubuntu-ports/`。备份原 APT 文件，按其实际 `.sources` / `.list` 格式只替换原 Ubuntu URI，保留 suite、组件和 Signed-By，不关闭签名检查；不要把 ARM64 换成 x86 的 `/ubuntu/` 源。

检查 Python；缺少依赖时在该容器内使用 APT 安装 `python3 ca-certificates`。源码 ZIP 可用 Python 标准库解压：`python3 -m zipfile -e 部署资料包.zip daxqo-source`，无需额外安装解压工具。随后按 3A 执行 DAXQO 安装器。已有 Debian 环境应使用匹配发行版的 Debian 源，不能套用 Ubuntu 源。

**保持 Termux 的交互式 PRoot 会话在线。** 不要仅以一条 `proot-distro login ... -- python3 ...` 运行安装器后马上退出会话；退出可能同时结束其后台进程。按小米正常设置将 DAXQO 和 Termux 的省电策略设为无限制并允许必要后台运行；保持应用会话不等于承诺全天保活。

## 4. 配对、模型与手机控制

- 在手机本地运行安装器的 `--print-pairing` / `--pairing-scheme daxqo-xm` 选项生成相应配对链接，并导入自己的 App。配对信息只在本机传递，不写到公共下载包或聊天报告。没有明确授权的本机保密传递通道时，让用户在手机完成这一小步。
- 模型 API 通过 DAXQO 设置页由用户填写并测试连接；不要读取 WorkBuddy 的登录令牌或从其他应用搜取密钥，也不要把 WorkBuddy 的模型选择直接写成 DAXQO 的模型 ID。
- 需要手机操作时，按系统正常界面启用对应 DAXQO 的无障碍服务，再回到 App 开始控制。沿用用户选择的 `confirm` / `full` 模式，不自行批准自己的操作。
- 邮件与原生助手转交是可选项，基础安装不必全部配置。用户确需邮件功能时，运行 `python3 scripts/install_mail_deps.py --app 实际APP路径 --index-url https://mirrors.ustc.edu.cn/pypi/simple`，保持依赖哈希锁定。

## 5. 验收清单：满足这些条件才算完成

1. APK 包名、版本、签名与下载清单一致，且原应用数据保留。
2. 手机 Linux 中运行实际 APP 的 `runtime/0.163.0/bin/codex --version` 为 0.163.0，Python 核心模块可用；安装器报告后台健康检查通过，实际后台版本是 0.5.0。
3. 手机上的 DAXQO 可打开本机工作台，配对成功，模型连接测试通过。
4. 用户授权手机控制后，做一个无害任务，例如打开计算器核验 `6+7=13`；如只验后台，则写一个测试文件并回读核验，不能仅凭“任务已接收”报成功。
5. 关闭本次 PC 的 ADB 临时转发，让 Termux/Linux 保持在线，再从手机自身打开 DAXQO 验证；不要依赖 PC localhost 转发假装手机部署成功。
6. 报告机型、所选配置、APK/后台版本、使用镜像、已通过项及具体未通过项；不附带密钥、配对 URL、私人文件或手机号。

## 实测范围

2026-10-25 已实测当前 WLAN 直连下载关键依赖，禁用请求代理，校验实际文件完整性；镜像状态见独立清单。Linux 安装器的国内源选择、保密边界与邮件选源共 22 项检查通过；x64 runtime 已在 Linux 完成实际安全解包和版本启动检查。APK 的构建、签名和平台标识已验证。

这不等于已经验证 WorkBuddy 的整套首次部署、所有 XM 型号或 24 小时后台运行。用户接下来的 WorkBuddy 测试用于补上这部分实际验收；失败时请保留具体阶段，勿直接宣称设备不支持或随意改用 VPN。

来源：[USTC Termux](https://mirrors.ustc.edu.cn/help/termux.html)、[USTC Ubuntu Ports](https://mirrors.ustc.edu.cn/help/ubuntu-ports.html)、[USTC PyPI](https://mirrors.ustc.edu.cn/help/pypi.html)、[proot-distro 上游](https://github.com/termux/proot-distro)。
