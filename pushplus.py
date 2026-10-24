#!/usr/bin/env python3
# coding: utf-8
"""PushPlus 推送（仅标准库）。

由 csair_daily_sign.py 导入，也可独立运行，供工作流在主脚本不可用时发送兜底提醒：
    PUSHPLUS_TOKEN=... python3 pushplus.py --title 标题 "正文"
Token 只从环境变量读取；不读取、不发送南航 Cookie；日志不输出 Token、响应体或异常文本。
重试仅针对网络异常和 HTTP 5xx（最多 2 次，退避）；PushPlus 明确拒绝、4xx、重定向、无效响应不重试。
重试可能让同一条提醒送达两次，这里可以接受；签到 POST 的重试规则与此无关。
每次请求在独立子进程中执行，默认总时限 20 秒；含重试、退避的一条通知总预算为 75 秒。
"""

import argparse
import datetime as dt
import http.client
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request

import network_worker

CHINA_TZ = dt.timezone(dt.timedelta(hours=8))
PUSHPLUS_URL = "https://www.pushplus.plus/send"
DEFAULT_TITLE = "南航签到异常提醒"
NOTIFY_RETRIES = 2
NOTIFY_ATTEMPT_TIMEOUT = 20  # 每次请求包含 DNS、响应头/体和关闭的总预算
NOTIFY_TIME_BUDGET = 75  # 一条通知含全部重试与退避的总预算


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Retryable(Exception):
    """网络层失败或服务端 5xx，值得重试。"""


def post_once(payload, timeout=NOTIFY_ATTEMPT_TIMEOUT):
    """网络请求放在可终止的子进程中，缓慢响应或 DNS 不能无限占用推送时间。"""
    reply = network_worker.run("push", {
        "url": PUSHPLUS_URL, "payload": payload,
        "socket_timeout": min(20, timeout),
    }, timeout=timeout)
    if reply["state"] in ("timeout", "retryable"):
        raise Retryable from None
    if reply["state"] != "ok":
        raise ValueError("notification unconfirmed")


def _post_once(payload, timeout=20):
    """发送一次。成功返回；可重试的失败抛 Retryable；其余失败抛 ValueError。"""
    request = urllib.request.Request(PUSHPLUS_URL, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            status = response.status
            # read() 可能在此抛 IncompleteRead（响应体传输中断），须包含在 try 内。
            raw = response.read(65537)
            if status != 200 or len(raw) > 65536:
                raise ValueError("invalid response")
            data = json.loads(raw)
            if not isinstance(data, dict) or data.get("code") != 200:
                raise ValueError("rejected")
            channels = data.get("data")
            if isinstance(channels, list) and any(
                not isinstance(item, dict) or item.get("code") != 200 for item in channels
            ):
                raise ValueError("channel rejected")
    except urllib.error.HTTPError as exc:  # 先于 URLError/OSError，HTTPError 是它们的子类
        if exc.code >= 500:
            raise Retryable from None
        raise ValueError("http error") from None
    except http.client.HTTPException:  # 含 IncompleteRead：响应体传输中断，非 OSError，值得重试
        raise Retryable from None
    except OSError:  # 含 URLError、超时、连接重置
        raise Retryable from None


def send(lines, title=DEFAULT_TITLE, retries=NOTIFY_RETRIES, sleep=None):
    """发送一条汇总通知。返回 True 仅表示 PushPlus 接受了请求，不代表手机已送达。"""
    sleep = sleep or time.sleep  # 调用时再取，便于测试替换
    if not lines:
        return True
    token = os.environ.get("PUSHPLUS_TOKEN", "").strip()
    if not token:
        print("PushPlus 未配置：请添加 PUSHPLUS_TOKEN Secret，失败通知未发送。", file=sys.stderr)
        return False
    content = "时间：" + dt.datetime.now(CHINA_TZ).strftime("%Y-%m-%d %H:%M:%S +0800")
    content += "\n\n" + "\n\n".join(lines)
    content += "\n\nCookie 失效请更新对应账号的 CSAIR_COOKIE_数字 Secret；结果不确定请先在南航 App 核对。"
    payload = {"token": token, "title": title, "content": content,
               "template": "txt", "channel": "cmcc"}
    deadline = time.monotonic() + NOTIFY_TIME_BUDGET
    for attempt in range(retries + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            post_once(payload, timeout=min(NOTIFY_ATTEMPT_TIMEOUT, remaining))
        except Retryable:
            if attempt < retries:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                print(f"PushPlus 网络异常，稍后重试（{attempt + 1}/{retries}）。", file=sys.stderr)
                sleep(min(min(2 ** (attempt + 1), 8) + random.uniform(0, 1), remaining))
                continue
            break
        except Exception:
            break
        if time.monotonic() >= deadline:
            break
        print("PushPlus 已接受通知请求（不代表手机已送达）。")
        return True
    print("PushPlus 通知未确认：请检查 Token、额度、网络或超时；签到失败状态保留。", file=sys.stderr)
    return False


def main(argv=None):
    parser = argparse.ArgumentParser(description="发送一条 PushPlus 提醒；Token 来自环境变量 PUSHPLUS_TOKEN")
    parser.add_argument("--title", default=DEFAULT_TITLE)
    parser.add_argument("message", nargs="+", help="固定文字，不要把 Cookie 或 Token 放进来")
    args = parser.parse_args(argv)
    return 0 if send(args.message, args.title) else 1


if __name__ == "__main__":
    sys.exit(main())

