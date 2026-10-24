# 南航自动签到 + PushPlus 失败通知

仓库：https://github.com/Innsly5232/Csair-Check-in

每天北京时间 08:17 由 GitHub Actions 执行，无需 Mac 开机。调度可能延迟。

## 设置账号

打开仓库 Settings → Secrets and variables → Actions → New repository secret。
每个账号只需一个独立 Secret，名称后面的数字就是日志和通知中的账号标识：

| Secret 名称 | Secret 内容 |
| --- | --- |
| `CSAIR_COOKIE_159` | 159 账号的完整 Cookie |
| `CSAIR_COOKIE_157` | 157 账号的完整 Cookie |
| `CSAIR_COOKIE_185` | 185 账号的完整 Cookie |
| `CSAIR_COOKIE_183` | 183 账号的完整 Cookie |
| `PUSHPLUS_TOKEN` | PushPlus 个人 Token，已绑定接收短信的手机号 |

每个 Cookie Secret 只填该账号的一条完整 Cookie，不填账号名、不加引号、不放多个账号。无需 Cookie 文件，也无需 `CSAIR_ACCOUNT_NAMES` 或 `CSAIR_COOKIES`；这两个旧 Secret 不再被 GitHub 工作流读取，可以删除。

工作流要求 159、157、185 三个账号必须配置，183 暂为可选。缺少必需账号、Cookie 格式错误或重复都会显示账号标识、发送提醒并让任务报错，其他有效账号会继续签到。183 未配置时跳过；如以后需要强制检查它，可在工作流 CSAIR_REQUIRED_ACCOUNTS 中追加 183。相同 Cookie 只提交一次。

例如 157 账号失效，通知显示“账号：157；原因：Cookie 失效”，只需更新 `CSAIR_COOKIE_157`。

## 运行

Actions → 南航每日签到 → Run workflow。默认勾选 `check_only`，只检查格式，不联网、不推送，也不验证 Cookie 有效期。取消勾选执行真实签到。每日定时任务执行真实签到。

代码提交只运行离线测试，不提交签到。日志与运行摘要会显示各账号结果；所有已配置账号明确成功或今日已签、必需账号无遗漏且 PushPlus Token 已配置时运行才通过。Token 非空不代表有效，需主动推送测试验证。

## PushPlus 通知

失败和结果不确定的账号合并为一条通知，包含账号数字、北京时间和固定原因说明。涵盖凭证失效、403、429、网络错误、服务器异常、未知响应及配置错误。成功或今日已签不推送，没有钉钉或其他机器人通知。

不发送 Cookie、原始服务器响应或异常文本。PushPlus 请求独立发送，不携带南航 Cookie；不自动重试通知。通知失败不会改变签到失败状态，详细诊断可查 Actions。PushPlus 接受请求不代表短信最终送达；缺少或填错 Token、额度用尽或网络故障都会影响通知。

签到前的安装/测试失败会尝试发送工作流提醒。GitHub 未启动任务、拉取代码失败、运行器中断、任务被强制终止时无法保证通知。每次真实运行失败都会提醒，直到修复；仅检查模式即使失败也不推送。

## 限制与测试

- 不自动登录或刷新 Cookie；过期后需更新对应 Secret。
- 南航接口、请求体和业务响应仍需首次真实运行验证，离线测试通过不代表真实签到成功。
- 仅建连超时自动重试；读超时、断连、5xx 等结果不确定时不重复提交，请先在 App 核对。
- 工作流只读仓库权限，凭证只注入必要步骤，签到任务串行执行。
- 使用 Python 3.12 和固定版本 Requests；运行测试：`python -m unittest discover -v`。
- 脚本保留旧版本地文件/环境变量用法用于兼容，但当前 GitHub 工作流仅使用四个独立 Secret。

PushPlus 接口文档：https://www.pushplus.plus/doc/guide/api.html

## 推送检查与故障隔离

- 设置 `PUSHPLUS_TOKEN` 后，手动运行时勾选 `test_pushplus`，只发送一条明确标为测试的通知，不读取 Cookie、不签到；该选项优先于 `check_only`。
- 实际签到即使全部成功，缺少 PushPlus Token 仍会明确报错，避免误认为通知可用；正常成功不发推送。
- 一个账号 Cookie 配置错误、重复、遗漏或出现未预期异常，不会阻止其他可用账号签到；所有错误合并通知并保留任务失败状态。
- `--no-proxy` 已修正为关闭 Requests 的环境配置读取，包括环境代理；不绕过系统 TUN。
- 离线测试不会访问南航或发送真实通知。

## 短信渠道

推送明确指定 `channel: sms`，接口为 `https://www.pushplus.plus/send`，继续使用 `PUSHPLUS_TOKEN`，无需单独设置短信 URL 或手机号 Secret。手机号须在 PushPlus 个人资料中绑定，账号需有足够积分。

官方说明成功发送一条短信扣减 10 积分（0.1 元）。短信仅展示固定提醒，无法显示自定义账号、错误原因或链接；完整失败详情仍提交到 PushPlus，可在消息记录或 GitHub Actions 日志查看。每次运行的多个失败合并为一条通知，不自动重试；持续失败会在后续每天运行时再次提醒。

手动勾选 `test_pushplus` 也会请求发送短信，可能产生上述费用。离线测试使用模拟接口，不发送真实短信。

短信官方说明：https://pushplus.plus/doc/extend/sms.html
