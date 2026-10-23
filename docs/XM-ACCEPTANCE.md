# XM 真机复测：2026-10-23

设备：Xiaomi 2410DPN6CC，HyperOS OS3.0.309.0.WOBCNXM，Android 16 / ARM64。
版本：DAXQO for XM 0.4.0-xm，Codex 0.163.0，模型 DeepSeek Flash。
以下时间均为北京时间（UTC+08:00）。本轮未读取或复制模型 API key；只调用手机内已配置的服务。

## 锁屏条件修正

首轮“息屏 30 秒通过”的操作期间，用户曾亲自解锁，所以不能作为连续锁屏验收。
本轮开始时，用户明确告知已删除密码；Android 系统及 DAXQO 原生状态均返回 `secureLock=false / deviceSecure=false`。
因此以下结论仅适用于**无密码滑动锁屏**，不包含密码、PIN、图案或生物识别解锁。

## 实际结果

| 项目 | 时间 | 证据与结果 |
| --- | --- | --- |
| 模型 API | 09:42:42 | 手机端 Responses API 验证返回 `verified=true`。 |
| 真实工具执行 | 09:42:56–09:42:59 | 模型执行 Python，在手机 Linux 工作区生成 `xm-acceptance-3f2c6ab2/foreground.json`，再读回；任务返回 `XM_FOREGROUND_OK`。独立通过文件 API 核对随机标识、SHA-256、完成标记及 `aarch64` 架构。 |
| 息屏定时触发 | 09:46:43–09:47:15 | 在熄屏后约 90 秒自动触发一次性任务。模型通过 Python 写入开始记录、等待 30 秒，再写入完成记录，返回 `XM_LOCKED_OK`；完成文件的时间戳为 09:47:15，随机标识、SHA-256 与架构均核对通过。 |
| 定时执行期间锁屏 | 09:45:26、09:46:19、09:46:57、09:47:45 | 四次独立采样均为原生 `locked=true / screenOn=false / secureLock=false`；Android window policy 为 keyguard showing。期间没有发出唤醒或解锁指令，任务完成后的采样仍保持息屏锁屏。 |
| 原生自动唤醒 | 09:48:01–09:48:07 | 单独提交真实模型任务，依次调用 `android_status → android_action(wake) → android_status`。任务前 `screenOn=false / locked=true`；任务后独立采样为 `screenOn=true / locked=false`。没有使用 shell、ADB 输入、密码输入或手动滑动执行解锁。 |
| 唤醒来源核验 | 同上 | 仅查看 DAXQO-Wake 日志，出现 `request_dismiss → dismiss_succeeded → service_ready`；各阶段 `secure=false / deviceSecure=false`。支持原生唤醒成功的结论。 |

本轮共执行 1 次最小 API 验证、3 个真实模型任务；测试指令均不请求启动子智能体。
定时任务已完成且为一次性任务，不会重复触发。测试输出只在本应用工作区中。

## 边界

- 手机保持 USB 调试连接；HTTP 临时转发在每次检查后均移除。尚未验证拔线后的运行、强制 Doze 或小时级息屏。
- 本轮未测试密码锁屏、邮件账户闭环、支付、安装权限或其他敏感系统操作。
- 原有 154 项手机后端回归、3 项安装/配对回归与 Android 构建/回归记录仍见 [XM 说明](XM.md)。本轮只更新验收证据，没有更改运行代码或系统锁屏设置。
