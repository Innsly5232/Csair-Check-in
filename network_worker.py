#!/usr/bin/env python3
"""用独立进程限制网络调用的总时长；凭证仅经 stdin 管道传递。仅依赖标准库。"""

import json
import os
from pathlib import Path
import subprocess
import sys
import time

# 超时后直接杀掉子进程，最多额外等半秒回收。网络线程、慢 DNS 或 close 均不会拖住父进程。
REAP_TIMEOUT = 0.5


def run(operation, parameters, timeout):
    """返回固定状态 ok/retryable/failed/timeout；不转发子进程日志或原始异常。"""
    deadline = time.monotonic() + max(timeout, 0)
    if timeout <= 0:
        return {"state": "timeout"}
    # 子进程只得到本次请求所需的凭证，不继承其他账号 Cookie 或推送 Token。
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("CSAIR_COOKIE_")
           and key not in ("CSAIR_COOKIES", "CSAIR_ACCOUNT_NAMES", "PUSHPLUS_TOKEN")}
    process = None
    try:
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), operation],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=env,
        )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"state": "timeout"}
        try:
            stdout, _ = process.communicate(
                json.dumps(parameters).encode("utf-8"), timeout=remaining)
        except subprocess.TimeoutExpired:
            return {"state": "timeout"}
        # communicate 在时限内返回即子进程已完整结束：采用其结果。若再按时钟判超时，
        # 会把已确认的签到成功记成“不确定”，或把已被接受的推送当成失败去重试。
        if process.returncode != 0:
            return {"state": "failed"}
        reply = json.loads(stdout)
        if not isinstance(reply, dict) or reply.get("state") not in ("ok", "retryable", "failed"):
            return {"state": "failed"}
        return reply
    except Exception:
        return {"state": "failed"}
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            try:
                process.communicate(timeout=REAP_TIMEOUT)
            except subprocess.TimeoutExpired:
                # 极端 OS 故障时也不无限等待回收；杀进程请求已经发出。
                pass
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    stream.close()


def main():
    reply = {"state": "failed"}
    try:
        parameters = json.loads(sys.stdin.buffer.read())
        if sys.argv[1:] == ["sign"]:
            from dataclasses import asdict
            import csair_daily_sign as sign
            import requests
            sign.SIGN_URL = parameters["url"]
            sign.ACCOUNT_TIME_BUDGET = parameters["extra_budget"]
            result = sign._sign_one(
                parameters["cookie"], requests,
                connect_timeout=parameters["connect_timeout"],
                read_timeout=parameters["read_timeout"],
                retries=parameters["retries"], ignore_proxy=parameters["ignore_proxy"])
            reply = {"state": "ok", "result": asdict(result)}
        elif sys.argv[1:] == ["push"]:
            import pushplus
            pushplus.PUSHPLUS_URL = parameters["url"]
            try:
                pushplus._post_once(parameters["payload"], timeout=parameters["socket_timeout"])
                reply = {"state": "ok"}
            except pushplus.Retryable:
                reply = {"state": "retryable"}
    except Exception:
        # 包括 JSON/依赖/网络意外错误：不输出可能带凭证的异常文本。
        pass
    sys.stdout.write(json.dumps(reply))


if __name__ == "__main__":
    main()
