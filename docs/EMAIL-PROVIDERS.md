# 可信邮箱普通邮件 / Trusted mailbox ordinary email

## 中文

可信邮箱普通邮件可将登记联系人的新邮件提交为任务。默认关闭；使用前需要配置邮件账户、登记可信联系人，并显式启用普通邮件处理。系统会先验证邮件身份，只处理已登记且启用的联系人；仅有匹配的 `From` 地址不足以通过验证。收到的邮件不会自动添加联系人。

界面支持以下邮箱域名：

- Gmail
- QQ 邮箱：`qq.com`、`foxmail.com`
- 网易邮箱：`163.com`、`126.com`、`yeah.net`

使用流程：

1. 在邮箱远程接入中配置收件/发件账户并完成登录验证。
2. 将发件地址登记为可信联系人，并确认该联系人已启用。
3. 显式启用“可信邮箱普通邮件自动执行”。该开关只处理启用后的新邮件；升级支持 QQ/网易后，还要求来信晚于国内邮箱首次启用切点，旧信不会补执行，重启也不会重置切点。
4. 发送邮件后，主题与正文一起作为任务输入；正文为空时使用主题。任务结果会回复到发信的已登记联系人邮箱。

QQ/网易适配会独立校验提供商签名，并从已签名的原始正文解释邮件内容；未签名的顶层 MIME 头不能切换任务内容。修改未签名 Message-ID 不能绕过重复执行检查。邮件签名无效、缺少必要身份头或格式不支持时会拒绝执行并显示原因。

普通邮件不需要 Agent/MCP 配对密钥，但仍受联系人登记、启用状态和邮件身份验证限制。敏感手机写操作继续要求一次性审批。邮件附件策略与其他边界见 [V0.6.1 历史说明](V0.6.md)。

Gmail 的真实邮件闭环已有历史验收记录。QQ 与网易邮箱的真实收发和任务闭环尚未实测，界面列出的支持域名不代表这些提供商已完成真机验收。

## English

Trusted mailbox ordinary email can submit new messages from registered contacts as tasks. It is disabled by default. Before use, configure the mailbox, register trusted contacts, and explicitly enable ordinary-email handling. Mail identity is verified before processing; a matching `From` address alone is insufficient. Incoming messages never add contacts automatically.

The interface lists these providers and domains:

- Gmail
- QQ Mail: `qq.com`, `foxmail.com`
- NetEase Mail: `163.com`, `126.com`, `yeah.net`

Workflow:

1. Configure the inbound/outbound mailbox account and complete its login test.
2. Register the sender as a trusted contact and ensure the contact is enabled.
3. Explicitly enable “trusted mailbox ordinary email” handling. Only new mail received after enablement is considered. QQ/NetEase mail must additionally be newer than the first domestic-provider upgrade cutoff; older mail is not replayed, and restarting preserves the cutoff.
4. The subject and body are combined as task input; if the body is empty, the subject is used. Task results are emailed back to the registered sender.

QQ/NetEase compatibility verifies provider signatures independently and derives message content from the signed wire body. Unsigned top-level MIME headers cannot switch the interpreted task, and changing an unsigned Message-ID cannot bypass duplicate-command protection. Invalid signatures, missing identity headers and unsupported formats are rejected with an explanation.

Ordinary email does not require an Agent/MCP pairing key, but still requires contact registration, enabled status, and mail-identity verification. Sensitive phone writes still require one-time approval. See the [V0.6.1 historical notes](V0.6.md) for the existing email attachment boundaries.

A real Gmail email loop is recorded in the historical acceptance notes. Real QQ and NetEase mail-to-task-and-reply flows have not yet been tested; listing these domains as supported does not claim physical-device acceptance.
