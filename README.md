# 南航自动签到 + PushPlus 失败通知

仓库：https://github.com/Innsly5232/Csair-Check-in

每天北京时间 08:19 由 GitHub Actions 执行，无需 Mac 开机。调度可能延迟。

## 设置账号

打开仓库 Settings → Secrets and variables → Actions → New repository secret。
每个账号只需一个独立 Secret，名称后面的数字就是日志和通知中的账号标识：

| Secret 名称 | Secret 内容 |
| --- | --- |
| `CSAIR_COOKIE_159` | 159 账号的完整 Cookie |
| `CSAIR_COOKIE_157` | 157 账号的完整 Cookie |
| `CSAIR_COOKIE_185` | 185 账号的完整 Cookie |
| `CSAIR_COOKIE_183` | 183 账号的完整 Cookie |
| `PUSHPLUS_TOKEN` | PushPlus 个人 Token，已绑定新消息ClawBot（cmcc）渠道 |

每个 Cookie Secret 只填该账号的一条完整 Cookie，不填账号名、不加引号、不放多个账号。无需 Cookie 文件，也无需 `CSAIR_ACCOUNT_NAMES` 或 `CSAIR_COOKIES`；这两个旧 Secret 不再被 GitHub 工作流读取，可以删除。

工作流要求 159、157、185 三个账号必须配置，183 暂为可选。缺少必需账号、Cookie 格式错误或重复都会显示账号标识、发送提醒并让任务报错，其他有效账号会继续签到。183 未配置时跳过；如以后需要强制检查它，可在工作流 CSAIR_REQUIRED_ACCOUNTS 中追加 183。相同 Cookie 只提交一次。

例如 157 账号失效，通知显示“账号：157；原因：Cookie 失效”，只需更新 `CSAIR_COOKIE_157`。

## 运行

Actions → 南航每日签到 → Run workflow。默认勾选 `check_only`，只检查格式，不联网、不推送，也不验证 Cookie 有效期。取消勾选执行真实签到。每日定时任务执行真实签到。

代码提交只运行离线测试，不提交签到。仓库是公开的，Actions 日志和运行摘要任何人可见，所以只显示账号数量和各状态计数（工作流设置了 `CSAIR_PUBLIC_LOG=1`）；逐账号结果只通过 PushPlus 发送。配置错误提示（如某个 Secret 未填写）仍会在日志里出现，其中只有账号数字，没有凭证。本地运行不设置该变量时，控制台仍显示逐账号结果。所有已配置账号明确成功或今日已签、必需账号无遗漏且 PushPlus Token 已配置时运行才通过。Token 非空不代表有效，需主动推送测试验证。

## PushPlus 通知

全部账号的成功、今日已签、失败和结果不确定的结果合并为一条通知，包含账号数字、北京时间和固定原因说明。涵盖凭证失效、403、429、网络错误、服务器异常、未知响应及配置错误。成功或今日已签也推送，没有钉钉或其他机器人通知。

不发送 Cookie、原始服务器响应或异常文本。PushPlus 请求独立发送，不携带南航 Cookie。网络异常、响应体传输中断（IncompleteRead）和服务端 5xx 最多重试 2 次（退避等待），所以偶尔可能收到重复提醒；Token 错误、额度用尽、4xx、重定向和无效响应不重试。通知失败不会改变签到失败状态，详细诊断可查 Actions。PushPlus 接受请求不代表手机最终送达；缺少或填错 Token、额度用尽或网络故障都会影响通知。

签到前的安装/测试失败会发送提醒，由独立的 `pushplus.py`（仅标准库）发出，即使主脚本有错误也能发送。签到步骤有 8 分钟上限，超时被终止时会发送“签到超时”提醒；任务被手动取消或整体超过 15 分钟时会尽力发送“取消或超时”提醒，若取消发生在签到过程中，可能收到两条。GitHub 未启动任务、拉取代码失败、运行器中断、任务被强制终止时无法保证通知。每次真实运行失败都会提醒，直到修复；仅检查模式即使失败也不推送。

## 限制与测试

- 目前不自动登录或刷新 Cookie；过期后需更新对应 Secret。
- 南航接口、请求体和业务响应仍需首次真实运行验证，离线测试通过不代表真实签到成功。
- 仅建连超时自动重试；读超时、断连、5xx 等结果不确定时不重复提交，请先在 App 核对。
- 工作流只读仓库权限，凭证只注入必要步骤，签到任务串行执行。
- 使用 Python 3.12 和固定版本 Requests；运行测试：`python -m unittest discover -v`。
- 脚本保留旧版本地文件/环境变量用法用于兼容，但当前 GitHub 工作流仅使用四个独立 Secret。代码里不再保存 Cookie（已移除 `CONFIG_COOKIES`）。

PushPlus 接口文档：https://www.pushplus.plus/doc/guide/api.html

## 推送检查与故障隔离

- 设置 `PUSHPLUS_TOKEN` 后，手动运行时勾选 `test_pushplus`，只发送一条明确标为测试的通知，不读取 Cookie、不签到；该选项优先于 `check_only`。
- 实际签到即使全部成功，缺少 PushPlus Token 仍会明确报错，避免误认为通知可用；正常成功也发送汇总推送。
- 一个账号 Cookie 配置错误、重复、遗漏或出现未预期异常，不会阻止其他可用账号签到；所有错误合并通知并保留任务失败状态。
- `--no-proxy` 已修正为关闭 Requests 的环境配置读取，包括环境代理；不绕过系统 TUN。
- 离线测试不会访问南航或发送真实通知。

## CMCC 新消息ClawBot 渠道

推送明确指定 `channel: cmcc` 和 `template: txt`，接口为 `https://www.pushplus.plus/send`。继续使用 `PUSHPLUS_TOKEN`，不需要 `option`，无需增加手机号或 Channel API Key 的 GitHub Secret。

此渠道仅支持中国移动用户。先在手机 5G 消息的“新消息ClawBot”应用号中获取以 `ak_` 或 `app_` 开头的 Channel API Key，再到 PushPlus 个人中心 → 渠道配置 → 新消息ClawBot 中保存绑定。GitHub 的 `PUSHPLUS_TOKEN` 填 PushPlus Token，不要填 Channel API Key。

官方将 cmcc 列为免费渠道，正文使用 txt 模板尽量完整展示，可携带账号数字和失败原因。多个账号失败合并一次通知（仅网络异常和 5xx 会重试，见上文）；持续失败会在后续每天运行时再次提醒。

勾选 `test_pushplus` 可单独发送一条明确标记为测试的消息，不提交签到。离线测试模拟接口，不发送真实通知。接受请求不等于手机最终收到，需通过手机消息确认。

官方说明：https://www.pushplus.plus/doc/channel/cmcc.html

## 自动保活

每周一 UTC 01:41（北京时间 09:41）检查默认分支，距最近一次提交满 30 天时，自动创建一次空提交并推送默认分支。无需额外 PAT，也不会改动代码或保存凭证。只有保活工作流拥有仓库写权限，签到工作流仍只读。提交通过 GitHub 自带令牌推送，不会再次触发 push 工作流。

GitHub 的 60 天无活动自动停用规则适用于公开仓库。保活依赖工作流仍被启用、GitHub 调度正常和默认分支允许此令牌推送；无法在工作流已停用后自行启动，也无法保证平台中断时持续运行。分支保护拒绝推送时需手动维护。

## 通知与 Cookie 续期

每次真实签到结束后，成功、今日已签和失败都发送一条 CMCC 汇总通知；失败保留原因。通知提交失败时，已成功的签到不会重新提交，但整个工作流会标记失败。仅检查模式不通知。

Cookie 仍需手动更新。现有 HAR 能看到设备相关的 token 登录请求，但未验证它能在旧 Cookie 过期后换取新登录凭证，不会把这类请求直接重放当作自动续期。每天请求是否会延长服务端会话也无法保证。失效后会标明账号并提醒更新相应 Secret。
