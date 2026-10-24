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
| `CSAIR_COOKIE_193` | 193 账号的完整 Cookie |
| `PUSHPLUS_TOKEN` | PushPlus 个人 Token，已绑定接收微信 |

每个 Cookie Secret 只填该账号的一条完整 Cookie，不填账号名、不加引号、不放多个账号。无需 Cookie 文件，也无需 `CSAIR_ACCOUNT_NAMES` 或 `CSAIR_COOKIES`；这两个旧 Secret 不再被 GitHub 工作流读取，可以删除。

可以先配置部分账号；未设置或为空的账号会跳过，四个都为空则报错并尝试推送提醒。格式错误会指出对应数字并停止整批运行。相同 Cookie 只处理一次，按 159、157、185、193 顺序保留第一个标识。

例如 157 账号失效，通知显示“账号：157；原因：Cookie 失效”，只需更新 `CSAIR_COOKIE_157`。

## 运行

Actions → 南航每日签到 → Run workflow。默认勾选 `check_only`，只检查格式，不联网、不推送，也不验证 Cookie 有效期。取消勾选执行真实签到。每日定时任务执行真实签到。

代码提交只运行离线测试，不提交签到。日志与运行摘要会显示各账号结果；只有全部明确成功或今日已签时真实签到运行才通过。

## PushPlus 通知

失败和结果不确定的账号合并为一条通知，包含账号数字、北京时间和固定原因说明。涵盖凭证失效、403、429、网络错误、服务器异常、未知响应及配置错误。成功或今日已签不推送，没有钉钉或其他机器人通知。

不发送 Cookie、原始服务器响应或异常文本。PushPlus 请求独立发送，不携带南航 Cookie；不自动重试通知。通知失败不会改变签到失败状态，详细诊断可查 Actions。PushPlus 接受请求不代表微信最终送达；缺少或填错 Token、额度用尽或网络故障都会影响通知。

签到前的安装/测试失败会尝试发送工作流提醒。GitHub 未启动任务、拉取代码失败、运行器中断、任务被强制终止时无法保证通知。每次真实运行失败都会提醒，直到修复；仅检查模式即使失败也不推送。

## 限制与测试

- 不自动登录或刷新 Cookie；过期后需更新对应 Secret。
- 南航接口、请求体和业务响应仍需首次真实运行验证，离线测试通过不代表真实签到成功。
- 仅建连超时自动重试；读超时、断连、5xx 等结果不确定时不重复提交，请先在 App 核对。
- 工作流只读仓库权限，凭证只注入必要步骤，签到任务串行执行。
- 使用 Python 3.12 和固定版本 Requests；运行测试：`python -m unittest discover -v`。
- 脚本保留旧版本地文件/环境变量用法用于兼容，但当前 GitHub 工作流仅使用四个独立 Secret。

PushPlus 接口文档：https://www.pushplus.plus/doc/guide/api.html
