# 南航 GitHub Actions 自动签到

由 GitHub 托管的 Linux 运行器执行，不需要在 Mac 安装、运行或保持开机。
默认每天北京时间 08:17 执行一次；GitHub 调度可能延迟，不保证准点。

## 部署

1. 已部署目标：[Innsly5232/Csair-Check-in](https://github.com/Innsly5232/Csair-Check-in)，使用 Python 3.12。
2. 将本项目文件放在仓库默认分支根目录，必须包含 `.github/workflows/`。
   只上传 ZIP 文件不会启用工作流。Mac Finder 可用 Command+Shift+. 显示隐藏目录。
3. 在仓库 Settings → Secrets and variables → Actions → New repository secret 中添加：
   - Name：`CSAIR_COOKIES`
   - Secret：完整南航 Cookie；多个账号每行一个，不加 Python 引号或列表符号。
   不要把 Cookie 提交到代码、Issue、README 或聊天中。
4. 在 Actions → 南航每日签到 → Run workflow 手动运行一次。
   勾选 `check_only` 只验证格式；取消勾选才会向南航提交签到。
5. 在运行摘要查看逐账号结果。仅当所有账号明确成功或今日已签，真实签到运行才会通过。
   “离线测试”通过和“仅检查”通过均不代表真实签到成功。

## 维护

- Cookie 过期后更新同名 Secret；此项目不自动登录或刷新 Cookie。
- 提交代码只触发离线测试，不会触发真实签到。
- 请求读取超时、断连或服务端异常会返回“结果不确定”，需先在 App 核对。
- 同一仓库的签到工作流串行执行，不会取消正在提交的签到任务。
- 日志只保留固定诊断和配置行号，原始响应与 Cookie 不写入日志。
- GitHub 只读仓库权限；凭证只注入配置检查和签到步骤。
- 私有仓库使用账号的 Actions 配额；请在 GitHub 账号中查看实际余量。
- 公共仓库长期无活动可能自动停用定时工作流；不通过无意义提交维持活跃。
- 南航是否接受 GitHub 云端网络、当前接口和返回消息，仍需首次真实运行验证。
- 不提供积分查询或自动验证码处理。

## 文件

- `csair_daily_sign.py`：升级后的签到脚本。
- `.github/workflows/csair-sign.yml`：定时及手动签到。
- `.github/workflows/test.yml`：代码更新时的离线回归测试。
- `test_csair.py`：12 组离线测试，不使用真实 Cookie、不连接南航。
- `requirements.txt`：固定 Requests 版本。

## 官方参考

- https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows
- https://docs.github.com/en/actions/security-for-github-actions/security-guides/using-secrets-in-github-actions

## 部署前检查结论

- 原附件中的 12 组离线测试已通过；补齐了缺失的两个 Actions 工作流和 `.gitignore`。
- 无账号密码自动登录或 Cookie 刷新功能；Cookie 失效需手动更新。
- 成功判定基于接口文字消息；接口地址、请求体和业务返回仍须用真实账号验证。
- 未配置 `CSAIR_COOKIES` 时工作流会明确报错，不会尝试签到。
- 手动运行默认仅检查格式，真实签到需取消 `check_only`；每日定时运行执行真实签到。

## PushPlus 失败通知与账号备注

在仓库 Settings → Secrets and variables → Actions 中设置以下 Repository secrets；无需 Cookie 文件：

| Secret 名称 | 内容 |
| --- | --- |
| `CSAIR_COOKIES` | 每行一个完整 Cookie，不同账号各一行 |
| `CSAIR_ACCOUNT_NAMES` | 可选，每行一个备注，如“本人”“家人”，与 Cookie 原始行号一一对应 |
| `PUSHPLUS_TOKEN` | PushPlus 个人 Token，绑定接收微信后填写 |

备注与 Cookie 的行数必须一致，包括空行和注释行；建议两处都不放空行和注释。备注最长 40 字，不填写 Cookie、Token 或个人敏感信息。未设置备注时使用“配置行 N”。重复 Cookie 仅签到一次，保留首次出现的备注和原始行号，后面的账号不会错位。

所有失败或无法确认成功的账号会合并发送一条 PushPlus 微信通知，含备注、原始配置行号、北京时间及固定原因说明。涵盖 Cookie 失效、403、429、网络错误、服务器错误、未知结果以及配置错误。不会把 Cookie、原始响应或异常文本发给 PushPlus。全部成功或今日已签时不通知；手动 `check_only` 模式不联网、不推送，即使格式错误也仅报告到 Actions。

Cookie 格式错误会停止整批运行并通知配置行号，请修正后重试。推送失败不会让签到变为成功，也不会重发签到。通知请求不自动重试；PushPlus 返回接受只代表收到请求，不保证微信最终送达。未配置或填错 PUSHPLUS_TOKEN 时无法收到通知，需查看 Actions 日志。

签到前的安装/测试失败会尝试发送工作流失败提醒。GitHub 未启动任务、拉取代码失败、运行器中断或整个任务被强制终止时，脚本无法保证发出通知。网络断开也可能同时影响签到和推送。

每次真实运行失败都会提醒；Cookie 持续失效时每天都会提醒，直到更新。当前没有钉钉或其他机器人通知。

新增 `test_notifications.py` 覆盖账号对应、重复项、失败合并、成功静默、配置错误、通知接口异常和凭证脱敏。运行全部测试：`python -m unittest discover -v`。

PushPlus 接口参考：https://www.pushplus.plus/doc/guide/api.html
