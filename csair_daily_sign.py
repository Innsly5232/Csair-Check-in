#!/usr/bin/env python3
# coding: utf-8
"""南航自动签到 v2.1（PushPlus 失败通知，Python 3.10+）

安装：python3 -m pip install requests
检查：python3 csair_daily_sign.py --check   # 只检查本地配置，不联网
运行：python3 csair_daily_sign.py
帮助：python3 csair_daily_sign.py --help

GitHub 使用 CSAIR_COOKIE_159 / 157 / 185 / 183，后缀即账号标识；无需备注配置。
独立 Secret 存在时优先读取；CSAIR_REQUIRED_ACCOUNTS 指定必需账号，空值报错。
单个账号配置错误不阻止其他有效账号；失败统一汇总并返回非零退出码。
以下为本地旧版兼容方式。cookies.txt：每行一个完整 Cookie，支持 Cookie: 前缀、UTF-8 BOM 和 # 注释。
优先级：显式 --cookies > CSAIR_COOKIES 环境变量（多行）> cookies.txt
        > CONFIG_COOKIES（仅当 cookies.txt 不存在）。空文件不会回退旧账号。
接口、请求体和客户端标识沿用原版，未独立验证当前接口业务协议。
仅明确的签到成功/已签到文字被视为完成；通用 success 不作为签到成功。
仅建连超时自动重试，读超时/断连/5xx 均视为结果不确定，不自动重发 POST。
无状态查询、自动登录、积分查询、定时调度或验证码处理功能。
日志仅保留固定诊断，不输出 Cookie、原始服务器消息、响应体或异常文本。
日志以北京时间记录，1 MiB 滚动，保留 3 份备份；每个账号立即写入。
本地锁防止使用同一 Cookie 路径的任务同时运行（不提供每日完成缓存）。
退出码：0=全部完成或本地检查通过；1=有失败/未确认；2=配置/文件错误；
        3=同一配置正在运行；130=用户中断。
"""

import argparse
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
import datetime as dt
import logging
import json
import urllib.request
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import random
import re
import sys
import time

BASE_DIR = Path(__file__).resolve().parent
COOKIE_FILE = BASE_DIR / "cookies.txt"
LOG_FILE = BASE_DIR / "sign_log.txt"
ACCOUNT_IDS = ("159", "157", "185", "183")
CONFIG_COOKIES = []  # 兼容旧版；建议把凭证放在 cookies.txt
SLEEP_MIN, SLEEP_MAX = 3, 8
MAX_RETRY = 2
CHINA_TZ = dt.timezone(dt.timedelta(hours=8))
MAX_RESPONSE_BYTES = 1024 * 1024
SIGN_URL = "https://wxapi.csair.com/marketing-tools/activity/join?type=APPTYPE&chanel=ss&lang=zh"
SIGN_BODY = {"activityType": "sign", "channel": "app", "entrance": 4}
HEADERS = {
    "Connection": "keep-alive",
    "Pragma": "no-cache",
    "Cache-Control": "no-cache",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://wxapi.csair.com",
    "User-Agent": "Mozilla/5.0 (Linux; Android 6.0.1; SM-G9008V Build/MMB29M; wv) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
                  "Chrome/55.0.2883.91 Mobile Safari/537.36",
    "Content-Type": "application/json",
    "Referer": "https://wxapi.csair.com/h5/sign/",
    "Accept-Language": "zh-CN,en-US;q=0.8",
    "X-Requested-With": "com.csair.mbp",
}


@dataclass(frozen=True)
class Account:
    label: str
    cookie: str


@dataclass(frozen=True)
class Result:
    status: str
    detail: str
    attempts: int = 1

    @property
    def ok(self):
        return self.status in ("签到成功", "今日已签")


class AlreadyRunning(Exception):
    pass


def load_cookies(path=COOKIE_FILE, explicit=False, errors=None):
    """返回账号及精确重复条数；不输出凭证，也不重新排列 Cookie 字段。"""
    path = Path(path)
    independent = not explicit and any(f"CSAIR_COOKIE_{key}" in os.environ for key in ACCOUNT_IDS)
    if independent:
        accounts, seen, duplicates = [], {}, 0
        required = [key.strip() for key in os.environ.get("CSAIR_REQUIRED_ACCOUNTS", "").split(",") if key.strip()]
        if any(key not in ACCOUNT_IDS for key in required):
            raise ValueError("CSAIR_REQUIRED_ACCOUNTS 包含不支持的账号标识")
        def report(message):
            if errors is None:
                raise ValueError(message)
            errors.append(message)
        for key in ACCOUNT_IDS:
            cookie = os.environ.get(f"CSAIR_COOKIE_{key}", "").strip().lstrip("\ufeff")
            if not cookie:
                if key in required:
                    report(f"账号 {key}：CSAIR_COOKIE_{key} 未配置或为空")
                continue
            cookie = re.sub(r"^cookie\s*:\s*", "", cookie, flags=re.I)
            parts = [part.strip() for part in cookie.split(";") if part.strip()]
            if (not cookie or any(ord(c) < 32 or ord(c) > 126 for c in cookie)
                    or not parts or any("=" not in part or not part.split("=", 1)[0].strip() for part in parts)):
                report(f"账号 {key}：CSAIR_COOKIE_{key} 格式错误，须填写单个账号的完整 Cookie")
                continue
            if cookie in seen:
                duplicates += 1
                if errors is not None:
                    errors.append(f"账号 {key}：Cookie 与账号 {seen[cookie]} 完全相同，已跳过重复提交，请核对配置")
                continue
            seen[cookie] = key
            accounts.append(Account(key, cookie))
        if not accounts and not errors:
            raise ValueError("未配置账号：请至少填写一个 CSAIR_COOKIE_159 / 157 / 185 / 183 Secret")
        return accounts, duplicates
    if explicit:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    elif "CSAIR_COOKIES" in os.environ:
        lines = os.environ["CSAIR_COOKIES"].splitlines()
    elif path.exists():
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    else:
        lines = CONFIG_COOKIES
    names = os.environ.get("CSAIR_ACCOUNT_NAMES", "").splitlines()
    if names and len(names) != len(lines):
        raise ValueError("CSAIR_ACCOUNT_NAMES 与 Cookie 行数不一致，请逐行对应（含空行或注释行）")
    for name in names:
        if len(name) > 40 or any(ord(c) < 32 for c in name) or any(c in name for c in "=;<>`"):
            raise ValueError("账号备注须为不超过 40 字的普通文字，不能填写 Cookie")
    accounts, seen, duplicates = [], set(), 0
    for number, raw in enumerate(lines, 1):
        cookie = raw.strip().lstrip("\ufeff")
        if not cookie or cookie.startswith("#"):
            continue
        cookie = re.sub(r"^cookie\s*:\s*", "", cookie, flags=re.I)
        if not cookie or any(ord(c) < 32 or ord(c) > 126 for c in cookie):
            raise ValueError(f"第 {number} 行 Cookie 含非 ASCII 或控制字符")
        parts = [part.strip() for part in cookie.split(";") if part.strip()]
        if not parts or any("=" not in p or not p.split("=", 1)[0].strip() for p in parts):
            raise ValueError(f"第 {number} 行不是完整的 name=value Cookie 格式")
        if cookie in seen:
            duplicates += 1
            continue
        seen.add(cookie)
        name = names[number - 1].strip() if names else ""
        label = f"{name}（配置行{number}）" if name else f"配置行{number}"
        accounts.append(Account(label, cookie))
    if not accounts:
        raise ValueError("没有有效账号：请在 CSAIR_COOKIES Secret 中每行填写一个 Cookie")
    return accounts, duplicates


def classify_response(data):
    """保守解释原脚本使用的消息字段；未知业务码不自行猜测。"""
    if not isinstance(data, dict):
        return Result("未知结果", "JSON 顶层不是对象，需核对接口响应格式")
    messages = [data[k].strip() for k in ("respMsg", "message")
                if isinstance(data.get(k), str) and data[k].strip()]
    # 实际签到 HAR 的 respMsg 为通用 success，明确业务结果位于 data.result。
    nested = data.get("data")
    if isinstance(nested, dict) and isinstance(nested.get("result"), str):
        messages.append(nested["result"].strip())
    msg = " | ".join(messages).lower()
    if any(s in msg for s in ("未登录", "请登录", "重新登录", "登录过期", "登录失效",
                             "登录已过期", "登录已失效", "登录状态失效",
                             "cookie过期", "cookie失效", "not logged in", "login required",
                             "session expired", "token expired")):
        return Result("Cookie 失效", "接口提示登录凭证无效，请更新 Cookie")
    # 对否定语义先行判断，防止“未成功”“unsuccessful”等假阳性。
    if any(s in msg for s in ("失败", "未成功", "不成功", "未签到", "尚未签到",
                             "未签成功", "未能", "没有", "不能", "无法", "unsuccess", "not success",
                             "fail", "error", "not signed", "not checked")):
        return Result("签到未完成", "接口消息含失败或未完成信息")
    if data.get("success") is False or (isinstance(nested, dict) and nested.get("success") is False):
        return Result("未知结果", "响应 success=false，不能确认签到完成")
    if any(s in msg for s in ("已签到", "已签过", "已经签到", "重复签到",
                             "今日已签", "今天已签", "already signed in", "already checked in")):
        return Result("今日已签", "接口消息提示今日已签到")
    if any(s in msg for s in ("签到成功", "sign-in successful", "check-in successful",
                             "sign in successful", "check in successful")):
        return Result("签到成功", "接口消息明确提示签到成功")
    return Result("未知结果", "缺少明确的签到结果；请在南航 App 核对，未记录响应原文")


def sign_one(cookie, requests_module, connect_timeout=10, read_timeout=20,
             retries=MAX_RETRY, ignore_proxy=False, sleep=time.sleep):
    """各账号独立 Session；自动重试仅限 requests 明确可安全重试的 ConnectTimeout。"""
    req = requests_module
    with req.Session() as session:
        if ignore_proxy:
            session.trust_env = False
            session.proxies.clear()
        headers = dict(HEADERS, Cookie=cookie)
        for attempt in range(1, retries + 2):
            try:
                with session.post(SIGN_URL, headers=headers, json=SIGN_BODY,
                                  timeout=(connect_timeout, read_timeout),
                                  allow_redirects=False, stream=True) as response:
                    code = response.status_code
                    if code == 401:
                        return Result("Cookie 失效", "HTTP 401：请更新 Cookie", attempt)
                    if code == 403:
                        return Result("访问被拒绝", "HTTP 403：可能是权限或风控问题，停止重试", attempt)
                    if code == 429:
                        return Result("请求受限", "HTTP 429：请求过于频繁，本次不再提交", attempt)
                    if 300 <= code < 400:
                        return Result("请求失败", "接口发生重定向，已阻止跟随；请核对接口或登录状态", attempt)
                    if code >= 500:
                        return Result("结果不确定", f"HTTP {code}：服务端异常，请在 App 核对", attempt)
                    if not 200 <= code < 300:
                        return Result("请求失败", f"HTTP {code}：请求未成功", attempt)
                    # 有界读取，避免错误页面或异常响应无限占用内存。
                    body = bytearray()
                    for chunk in response.iter_content(chunk_size=8192):
                        body.extend(chunk)
                        if len(body) > MAX_RESPONSE_BYTES:
                            return Result("结果不确定", "响应超过 1 MiB，停止读取，请在 App 核对", attempt)
                    import json
                    try:
                        data = json.loads(bytes(body))
                    except (ValueError, UnicodeError, RecursionError):
                        return Result("结果不确定", "返回非有效 JSON，请在 App 核对是否已签到", attempt)
                    result = classify_response(data)
                    return Result(result.status, result.detail, attempt)
            except req.exceptions.ConnectTimeout:
                if attempt > retries:
                    return Result("请求失败", "连接超时，已达到重试上限", attempt)
                sleep(min(2 ** attempt, 20) + random.uniform(0, 1))
            except req.exceptions.SSLError:
                return Result("请求失败", "TLS 证书验证失败；未关闭证书验证", attempt)
            except req.exceptions.InvalidHeader:
                return Result("配置错误", "Cookie 或请求头格式无效", attempt)
            except req.exceptions.RequestException:
                return Result("结果不确定", "读取超时或网络异常，可能已提交；请在 App 核对，本次不重发", attempt)


@contextmanager
def run_lock(path):
    """使用系统文件锁，进程终止后自动释放；不删除锁文件以避免 inode 竞争。"""
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "r+b") as handle:
        if os.name == "nt":
            import msvcrt
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise AlreadyRunning from exc
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise AlreadyRunning from exc
        # 关闭句柄释放锁，包括异常和 Ctrl-C。
        yield


class ChinaFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        return dt.datetime.fromtimestamp(record.created, CHINA_TZ).strftime("%Y-%m-%d %H:%M:%S +0800")


class StrictFileHandler(RotatingFileHandler):
    def handleError(self, record):
        # 标准 logging 默认可能吞掉磁盘写入错误；此处明确终止。
        raise OSError("无法写入签到日志") from None


def make_logger(path):
    logger = logging.getLogger("csair_sign")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    file_handler = StrictFileHandler(path, maxBytes=1024 * 1024, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(ChinaFormatter("%(asctime)s %(message)s"))
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(file_handler)
    logger.addHandler(console)
    return logger


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cookies", type=Path, help="指定 Cookie 文件；显式参数优先于环境变量")
    parser.add_argument("--log", type=Path, default=LOG_FILE, help="日志文件路径")
    parser.add_argument("--test-pushplus", action="store_true", help="仅发送一条固定推送测试消息，不读取 Cookie、不签到")
    parser.add_argument("--check", action="store_true", help="只校验本地 Cookie 格式，不联网，不验证有效期")
    parser.add_argument("--connect-timeout", type=float, default=10)
    parser.add_argument("--read-timeout", type=float, default=20)
    parser.add_argument("--retries", type=int, default=MAX_RETRY, help="仅建连超时的重试次数，0–5")
    parser.add_argument("--sleep-min", type=float, default=SLEEP_MIN)
    parser.add_argument("--sleep-max", type=float, default=SLEEP_MAX)
    parser.add_argument("--no-proxy", action="store_true", help="忽略 Requests 的代理配置；不绕过系统 TUN")
    args = parser.parse_args(argv)
    if args.check and args.test_pushplus:
        parser.error("--check 与 --test-pushplus 不能同时使用")
    values = (args.connect_timeout, args.read_timeout, args.sleep_min, args.sleep_max)
    if (not all(math.isfinite(v) for v in values) or min(values[:2]) <= 0
            or not 0 <= args.sleep_min <= args.sleep_max or not 0 <= args.retries <= 5):
        parser.error("超时须为正数；间隔须满足 0 <= min <= max；重试须为 0–5；参数须为有限数值")
    return args


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def notify_failures(failures, title="南航签到失败提醒"):
    """只发送固定诊断和用户备注；独立连接，不带南航 Cookie，不重试。"""
    if not failures:
        return True
    token = os.environ.get("PUSHPLUS_TOKEN", "").strip()
    if not token:
        print("PushPlus 未配置：请添加 PUSHPLUS_TOKEN Secret，失败通知未发送。", file=sys.stderr)
        return False
    content = "时间：" + dt.datetime.now(CHINA_TZ).strftime("%Y-%m-%d %H:%M:%S +0800")
    content += "\n\n" + "\n\n".join(failures)
    content += "\n\nCookie 失效请更新对应账号的 CSAIR_COOKIE_数字 Secret；结果不确定请先在南航 App 核对。"
    payload = {"token": token, "title": title, "content": content,
               "template": "txt", "channel": "sms"}
    try:
        request = urllib.request.Request("https://www.pushplus.plus/send",
                  data=json.dumps(payload).encode("utf-8"),
                  headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=20) as response:
            raw = response.read(65537)
            if response.status != 200 or len(raw) > 65536:
                raise ValueError("invalid response")
            data = json.loads(raw)
            if not isinstance(data, dict) or data.get("code") != 200:
                raise ValueError("rejected")
            channels = data.get("data")
            if isinstance(channels, list) and any(
                not isinstance(item, dict) or item.get("code") != 200 for item in channels
            ):
                raise ValueError("channel rejected")
        print("PushPlus 已接受通知请求（不代表短信已送达）。")
        return True
    except Exception:
        print("PushPlus 通知未确认：请检查 Token、额度或网络；签到失败状态保留。", file=sys.stderr)
        return False


def main(argv=None):
    args = parse_args(argv)
    if args.test_pushplus:
        return 0 if notify_failures(["这是一条主动推送测试消息，不代表签到失败；本次没有提交签到。"],
                                    title="南航 PushPlus 推送测试") else 1
    cookie_path = (args.cookies or COOKIE_FILE).expanduser().resolve()
    logger = None
    failures = []
    try:
        config_errors = []
        accounts, duplicates = load_cookies(cookie_path, explicit=args.cookies is not None, errors=config_errors)
        failures.extend(config_errors)
        for error in config_errors:
            print("配置错误：" + error, file=sys.stderr)
        if args.check:
            if config_errors:
                return 2
            print(f"本地格式检查通过：{len(accounts)} 个账号，移除 {duplicates} 条精确重复项。未联网验证 Cookie。")
            return 0
        if os.environ.get("CSAIR_REQUIRE_PUSHPLUS") == "1" and not os.environ.get("PUSHPLUS_TOKEN", "").strip():
            failures.append("推送配置错误：PUSHPLUS_TOKEN 未设置或为空，无法发送失败提醒")
            print(failures[-1], file=sys.stderr)
        if not accounts:
            return 2
        try:
            import requests
        except ImportError:
            failures.append("运行失败：缺少 requests 依赖，请查看 Actions 安装步骤。")
            print("缺少 requests：请运行 python3 -m pip install requests", file=sys.stderr)
            return 2
        with run_lock(cookie_path.with_name(cookie_path.name + ".lock")):
            logger = make_logger(args.log.expanduser().resolve())
            logger.info("===== 共 %d 个账号；移除 %d 条精确重复项 =====", len(accounts), duplicates)
            counts = Counter()
            for index, account in enumerate(accounts):
                try:
                    result = sign_one(account.cookie, requests, args.connect_timeout, args.read_timeout,
                                      args.retries, args.no_proxy)
                except Exception:
                    result = Result("结果不确定", "账号处理出现未预期错误，可能已提交；请在 App 核对，本次不重发")
                if not result.ok:
                    failures.append(f"账号：{account.label}\n原因：{result.status}｜{result.detail}")
                counts[result.status] += 1
                logger.info("%s：%s｜%s｜请求 %d 次", account.label, result.status, result.detail, result.attempts)
                if index + 1 < len(accounts):
                    time.sleep(random.uniform(args.sleep_min, args.sleep_max))
            logger.info("汇总：%s", "；".join(f"{key} {value}" for key, value in counts.items()))
            return 0 if not failures and counts["签到成功"] + counts["今日已签"] == len(accounts) else 1
    except AlreadyRunning:
        failures.append("任务未执行：同一配置已有签到任务正在运行。")
        print("已有任务正在使用此 Cookie 配置，本次跳过。", file=sys.stderr)
        return 3
    except (OSError, UnicodeError):
        failures.append("运行失败：配置、锁或日志文件读写失败，请检查路径、编码和权限。")
        print("配置、锁或日志文件读写失败：请检查路径、UTF-8 编码、权限和磁盘空间。", file=sys.stderr)
        return 2
    except ValueError as exc:
        failures.append(f"配置错误：{exc}")
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        failures.append("任务中断：签到结果可能不确定，请在南航 App 核对。")
        print("已中断；正在提交的账号结果可能不确定，请在 App 核对。", file=sys.stderr)
        return 130
    except Exception:
        failures.append("运行失败：脚本发生未预期错误；本次可能未完成全部账号，请在 App 核对。")
        print("脚本发生未预期错误，未输出原始异常以保护凭证。", file=sys.stderr)
        return 1
    finally:
        if failures and not args.check:
            notify_failures(failures)
        if logger:
            for handler in logger.handlers[:]:
                logger.removeHandler(handler)
                handler.close()


if __name__ == "__main__":
    sys.exit(main())
