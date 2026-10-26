# 国内下载源与 XM 部署素材（2026-10-25）

本文记录本机在 2026-10-25 的下载探测和完整性验证，供国内安装及 WorkBuddy USB 部署指南引用。该次本机使用 WLAN 默认路由；未见活动 TUN 路由。下载探测使用 Python `ProxyHandler({})`，即不走配置代理。本次结果只证明这台主机当时可直连这些地址，不代表全国网络可用性、其他运营商或手机网络体验。

## 验证范围

| 场景 | 地址与结果 | 完整性/范围 |
| --- | --- | --- |
| Codex runtime arm64 | [npmmirror 元数据](https://registry.npmmirror.com/@openai%2Fcodex/0.163.0-linux-arm64)；包：[tarball](https://registry.npmmirror.com/@openai/codex/-/codex-0.163.0-linux-arm64.tgz) → `cdn.npmmirror.com`；154,715,729 bytes | SHA-256 `dc6e7c4d6de71c23a0f60d05e2577ff7eef615b76f22630d58ad384b84763522`；SHA-512 SRI 与官方 npm 元数据完全匹配：`sha512-IH2/R+4OarjGvsAf1ll3fdyLwrPgYLl3uzp6u9CyD9sj2Lw+ViDSe3QBZVHdcHJs0eIiZFg6Wprcuy4CRZaUGw==` |
| Codex runtime x64 | [npmmirror 元数据](https://registry.npmmirror.com/@openai%2Fcodex/0.163.0-linux-x64)；包：[tarball](https://registry.npmmirror.com/@openai/codex/-/codex-0.163.0-linux-x64.tgz) → `cdn.npmmirror.com`；162,055,867 bytes | SHA-256 `30efc97738b975940d78b59a9bea115fa34040b732807d54ec35ae6b7528de94`；SHA-512 SRI 与官方 npm 元数据完全匹配：`sha512-uwRaOsrYogLQhcxAM/GoeWrw0QIWEMRo7c9gM8sKAviVkZQHlu9kRLJ5PkufbV+ABGjyZgIwiFdJ0LCJ0N6e8w==` |
| Termux APT | [USTC Termux stable InRelease](https://mirrors.ustc.edu.cn/termux/apt/termux-main/dists/stable/InRelease) 与 aarch64 [Packages.gz](https://mirrors.ustc.edu.cn/termux/apt/termux-main/dists/stable/main/binary-aarch64/Packages.gz) 可读取；下载 Python、proot、proot-distro、curl 四个样本完整 `.deb` | 下载文件 SHA-256 与 Termux 包索引中的相应值匹配。此为四个样本包，不是完整依赖闭包或完整安装验收。 |
| Ubuntu ARM64 rootfs | [USTC Noble current rootfs](https://mirrors.ustc.edu.cn/ubuntu-cloud-images/noble/current/noble-server-cloudimg-arm64-root.tar.xz)；216,215,656 bytes | SHA-256 `b68ef0444ca56ce4f00ae7e8ccfb0c553ffbc92e4a1b14ed7dd8aa18c6d68652` 与 current `SHA256SUMS` 一致。日期路径 [20261017 rootfs](https://mirrors.ustc.edu.cn/ubuntu-cloud-images/noble/20261017/noble-server-cloudimg-arm64-root.tar.xz) HEAD 返回 HTTP 200，内容哈希与 current 包一致。 |
| Termux APK | 清华 TUNA [F-Droid 仓库 Termux 0.118.3 / versionCode 1002](https://mirrors.tuna.tsinghua.edu.cn/fdroid/repo/com.termux_1002.apk)；113,880,067 bytes | 完整 APK SHA-256 `e6265a57eb5ca363808488e3b01955958bed93bc0c8a0d281849b363b11027ec` 已核对。选用稳定版，不选较新的 beta。 |
| 可选邮件包 | USTC PyPI 的 [dkimpy 1.1.8](https://mirrors.ustc.edu.cn/pypi/packages/f0/6f/84e91828186bbfcedd7f9385ef5e0d369632444195c20e08951b7ffe0481/dkimpy-1.1.8.tar.gz) 与 [dnspython 2.8.0](https://mirrors.ustc.edu.cn/pypi/packages/ba/5a/18ad964b0086c6e62e2e7500f7edc89e3faa45033c71c1893d34eed2b2de/dnspython-2.8.0-py3-none-any.whl) | 两个完整文件的 SHA-256 均与 `scripts/mail-requirements.txt` 固定值匹配。 |
| USB 安装工具（可选） | Google 官方 [platform-tools r37.0.1 Windows ZIP](https://dl.google.com/android/repository/platform-tools_r37.0.1-win.zip)；8,044,989 bytes | SHA-256 `45f4d63113e895ebde0c90f194099a4676b6ac653bd28d54314a9e022bbc1a99`。这是官方直连，不是国内镜像；若采用官网 APK 直接安装，不需要它。 |
| Huawei Android 镜像 | [Huawei Android repository 索引](https://repo.huaweicloud.com/android/repository/) 可读 | 页面样本中仅发现旧版 platform-tools r29.0.6，不建议用于新部署，也不作为 r37 的镜像替代。 |
| Ubuntu ports / Debian | [USTC Ubuntu ports Noble InRelease](https://mirrors.ustc.edu.cn/ubuntu-ports/dists/noble/InRelease)、[USTC Debian Bookworm InRelease](https://mirrors.ustc.edu.cn/debian/dists/bookworm/InRelease) 均可读 | 仅索引探测成功；没有验证完整包下载、签名验证、依赖解析或安装，不应表述为全链路验收。 |

USTC Termux APT 样本 `.deb` 的完整下载文件 SHA-256 与上方 `Packages.gz` 中对应包索引字段一致：

- Python 3.14.8：[下载](https://mirrors.ustc.edu.cn/termux/apt/termux-main/pool/main/p/python/python_3.14.8_aarch64.deb)，`55fcd7c23a603ba0caeca3ef9e670ec88ff71c1a480101de4983629792563b5e`。
- proot 5.1.107.99：[下载](https://mirrors.ustc.edu.cn/termux/apt/termux-main/pool/main/p/proot/proot_5.1.107.99_aarch64.deb)，`0c31d50e9c0fc201347ef541b482c1cae055944dfcac5f881a0d2932a32bb90d`。
- proot-distro 5.10.2：[下载](https://mirrors.ustc.edu.cn/termux/apt/termux-main/pool/main/p/proot-distro/proot-distro_5.10.2_all.deb)，`8d328fced2c0c4b21278a058e91e8242cd8eba471bb7c93909f5339404fd336e`。
- curl 8.23.0：[下载](https://mirrors.ustc.edu.cn/termux/apt/termux-main/pool/main/c/curl/curl_8.23.0_aarch64.deb)，`e57a9975a828eb48790bec0da950795af165aa714f6a33d266c45a4e2898aa85`。

## 安装入口与依赖分类

| 类别 | 运行或下载入口 | 说明 |
| --- | --- | --- |
| 必需：DAXQO 服务 | 在已运行的兼容 Linux 中，从源码根目录执行 `python3 scripts/install_linux.py --registry npmmirror` | 需要 Python 3.10+、arm64/aarch64 或 x64/x86_64、Codex runtime 0.163.0，以及用户配置的模型 API。安装器用标准库获取 runtime，核验包元数据中的 SRI 后安全解包；runtime 可由 `--runtime-dir` 复用。镜像版本新增 `--registry npmmirror`；18 项 Linux 单测通过，WSL 已完成真实 runtime 提取和 `codex --version`（0.163.0）核验。 |
| USB 安装（可选） | 用已配对且授权 USB 调试的手机，通过 platform-tools 执行 APK 安装 | 只在 WorkBuddy 部署指南选择 USB 方式时需要。PC 上的 platform-tools 不是客户手机运行依赖；APK 也可经网站下载后在手机上安装。 |
| 仅 XM 实验环境 | Termux 0.118.3 + USTC Termux 包 + Ubuntu Noble ARM64 rootfs / PRoot | 这些下载已核验，但不代表路线受支持或真机端到端通过。XM 当前 Linux 前提见 [XM 说明](XM.md)：必须有已运行的兼容 Linux。全新 Termux/PRoot 部署仍是实验路线，等待用户使用 WorkBuddy 验收。不要把本镜像清单当作 XM 兼容性承诺。 |
| 可选邮件 | `python3 scripts/install_mail_deps.py --app <APP> --index-url https://mirrors.ustc.edu.cn/pypi/simple` | 仅启用邮件签名校验相关能力时需要 pip 和两项固定依赖。安装器保留官方 PyPI 默认源，也允许白名单中的 USTC 源；4 项邮件安装器测试通过。 |

Android SDK、JDK、Gradle 是开发者构建 APK 的工具，不是客户安装或运行 DAXQO 的依赖。WorkBuddy USB 安装若使用 ADB，只需平台工具，不需要构建工具链。官网机器可读清单位于 `https://daxqo.com/install/mirrors.json`。

本次验证也生成了 V0.5 Standard 和 XM APK：Standard `io.daxqo.app`，SHA-256 `209a9a4fd03241ad8ea4abc4f36a57d0a9bf7fcff918d1755e0a42922200c935`；XM `io.daxqo.xm`，SHA-256 `8fd41c2086c38fe24d5eed11754f13c1489dd2769fd1941101385b88ba792819`。签名均通过校验且签名证书一致。构建/签名验证不等于 APK 在不同手机上的安装运行验收。
