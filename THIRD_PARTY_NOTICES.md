# 第三方说明 / Third-party notices

## 中文

DAXQO 自有代码采用 [MIT 许可证](LICENSE)。标志图片是项目提供的品牌素材；开源许可证不授予以 DAXQO 名义背书或使用其商标的权利。

- **Codex CLI** 是单独下载的执行依赖，源码与许可证由 [openai/codex](https://github.com/openai/codex) 提供，采用 Apache-2.0；DAXQO 源码与 APK 不打包 Codex 可执行文件。
- **DeepSeek** 是用户自行配置的模型 API 服务，使用者需持有自己的账号并遵守服务商条款。模型目录参考 [DeepSeek 官方 Codex 接入说明](https://api-docs.deepseek.com/quick_start/agent_integrations/codex/)，兼容设置由 DAXQO 调整；项目不分发模型权重或 API 凭据。
- **Android SDK、Gradle、Eclipse Temurin JDK** 是构建工具，由安装脚本从各自官方渠道下载；不包含在源码仓库和发布的源码包内，各自许可证适用。
- **dkimpy 1.1.8** 与 **dnspython 2.8.0** 是邮件签名验证依赖，按固定版本和 SHA-256 哈希从 [PyPI](https://pypi.org/) 安装到 DAXQO APP 私有目录；dkimpy 使用 BSD-like 许可证，dnspython 使用 ISC 许可证。安装包包含各自许可证文本。
- **StepX / 阶跃终端** 提供本次适配使用的既有 Linux 环境。DAXQO 不分发其系统镜像或专有软件，与终端厂商没有官方关联。
- **Termux、PRoot / PRoot-Distro 与 Debian** 是 XM 版的独立运行依赖，按各项目官方来源安装，其许可证分别适用；源码与 APK 不嵌入这些系统组件。官方源码与说明：[Termux](https://github.com/termux/termux-app)、[PRoot-Distro](https://github.com/termux/proot-distro)、[Debian](https://www.debian.org/)；DAXQO 与小米、Termux、Debian 无官方关联。

## English

DAXQO's own code is licensed under the [MIT License](LICENSE). Logo images are project-provided brand assets; the open-source license does not grant trademark rights or permission to imply endorsement by DAXQO.

- **Codex CLI** is a separately downloaded execution dependency. Its source and license are provided by [openai/codex](https://github.com/openai/codex), licensed under Apache-2.0. DAXQO's source distribution and APK do not bundle the Codex executable.
- **DeepSeek** is a model API service configured by each user, who must supply an account and follow the provider's terms. The model catalog follows [DeepSeek's official Codex integration guide](https://api-docs.deepseek.com/quick_start/agent_integrations/codex/), with DAXQO compatibility adjustments. Model weights and API credentials are not distributed.
- **Android SDK, Gradle, and Eclipse Temurin JDK** are build tools downloaded by the setup script from their official sources. They are excluded from the repository and source release; their respective licenses apply.
- **dkimpy 1.1.8** and **dnspython 2.8.0** are email signature-verification dependencies, installed from [PyPI](https://pypi.org/) at pinned versions and SHA-256 hashes into a private DAXQO APP directory. dkimpy uses a BSD-like license; dnspython uses the ISC License. Their package distributions include their license texts.
- **StepX** supplies the existing Linux environment used for this device adaptation. DAXQO does not distribute vendor system images or proprietary software and is not officially affiliated with the device vendor.
- **Termux, PRoot / PRoot-Distro and Debian** are separate XM runtime dependencies installed from their official sources under their respective licenses. DAXQO source and APK do not embed these system components. See [Termux](https://github.com/termux/termux-app), [PRoot-Distro](https://github.com/termux/proot-distro) and [Debian](https://www.debian.org/). DAXQO is not affiliated with Xiaomi, Termux or Debian.
