"""真实本地 HTTP 回归：慢响应头/响应体、DNS 阻塞、超时后继续其他账号。不会访问外网。"""

import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import csair_daily_sign as sign
import network_worker
import pushplus


class NetworkDeadlines(unittest.TestCase):
    def server(self, mode):
        hits = []
        stop = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length', 0)))
                cookie = self.headers.get('Cookie', '')
                hits.append(cookie)
                try:
                    slow = mode != 'batch' or 'slow' in cookie
                    if mode in ('fast', 'reject', '5xx', 'unknown', 'nested'):
                        slow = False
                    if slow and mode != 'body':
                        self.wfile.write(b'HTTP/1.1 200 OK\r\nX-Slow: ')
                        while not stop.is_set():
                            self.wfile.write(b'x')
                            self.wfile.flush()
                            stop.wait(0.03)
                    elif slow:
                        self.wfile.write(b'HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n')
                        while not stop.is_set():
                            self.wfile.write(b'x')
                            self.wfile.flush()
                            stop.wait(0.03)
                    else:
                        # 同时适用于签到结果和 PushPlus 接受响应。
                        raw = json.dumps({'message': '签到成功',
                                          'code': 400 if mode == 'reject' else 200, 'data': []}).encode()
                        if mode == 'unknown':
                            raw = json.dumps({'respCode': '0000', 'respMsg': 'success',
                                              'data': {'code': '00A2', 'message': 'private-response'}}).encode()
                        if mode == 'nested':
                            raw = json.dumps({'respMsg': 'success', 'data': {'message': '签到成功'}}).encode()
                        self.send_response(500 if mode == '5xx' else 200)
                        self.send_header('Content-Length', str(len(raw)))
                        self.end_headers()
                        self.wfile.write(raw)
                except OSError:
                    pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
        thread.start()

        def cleanup():
            stop.set()
            server.shutdown()
            server.server_close()
            thread.join(1)

        self.addCleanup(cleanup)
        return f'http://127.0.0.1:{server.server_port}/', hits

    def test_sign_header_and_body_timeouts_do_not_resubmit(self):
        for mode in ('headers', 'body'):
            with self.subTest(mode=mode):
                url, hits = self.server(mode)
                with patch.object(sign, 'SIGN_URL', url), patch.object(sign, 'ACCOUNT_TIME_BUDGET', 0.2):
                    start = time.monotonic()
                    result = sign.sign_one('sid=synthetic', connect_timeout=0.2,
                                           read_timeout=0.2, retries=2, ignore_proxy=True)
                    elapsed = time.monotonic() - start
                self.assertEqual(result.status, '结果不确定')
                self.assertIn('总时间上限', result.detail)
                self.assertIsNone(result.attempts)
                self.assertLess(elapsed, 1.3)
                self.assertEqual(hits, ['sid=synthetic'])
                time.sleep(0.12)
                self.assertEqual(len(hits), 1)

    def test_sign_fast_success_keeps_confirmed_attempt_count(self):
        url, hits = self.server('fast')
        with patch.object(sign, 'SIGN_URL', url):
            result = sign.sign_one('sid=synthetic', connect_timeout=1, read_timeout=1,
                                   retries=0, ignore_proxy=True)
        self.assertEqual((result.status, result.attempts), ('签到成功', 1))
        self.assertEqual(len(hits), 1)

    def test_nested_success_survives_real_network_child(self):
        url, hits = self.server('nested')
        with patch.object(sign, 'SIGN_URL', url):
            result = sign.sign_one('sid=synthetic', connect_timeout=1, read_timeout=1,
                                   retries=2, ignore_proxy=True)
        self.assertEqual((result.status, result.attempts), ('签到成功', 1))
        self.assertEqual(result.diagnostic, '')
        self.assertEqual(len(hits), 1)

    def test_unknown_response_diagnostic_survives_real_network_child(self):
        url, hits = self.server('unknown')
        with patch.object(sign, 'SIGN_URL', url):
            result = sign.sign_one('sid=synthetic', connect_timeout=1, read_timeout=1,
                                   retries=2, ignore_proxy=True)
        self.assertEqual((result.status, result.attempts), ('未知结果', 1))
        self.assertFalse(result.ok)
        self.assertIn('data.code=00A2', result.diagnostic)
        self.assertIn('data.result=missing', result.diagnostic)
        self.assertNotIn('private-response', result.diagnostic)
        self.assertEqual(len(hits), 1, '未知结果不能触发重复签到')

    def test_timeout_account_does_not_block_next_account_or_summary(self):
        url, hits = self.server('batch')
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                'CSAIR_COOKIE_159': 'sid=slow', 'CSAIR_COOKIE_183': 'sid=fast',
                'CSAIR_REQUIRED_ACCOUNTS': '159,183', 'CSAIR_PUBLIC_LOG': '1'}), \
                patch.object(sign, 'SIGN_URL', url), \
                patch.object(sign, 'ACCOUNT_TIME_BUDGET', 0.3), \
                patch.object(sign, 'notify_failures', return_value=True) as notify, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = sign.main(['--log', str(Path(directory) / 'sign.log'),
                              '--connect-timeout', '0.2', '--read-timeout', '0.2',
                              '--sleep-min', '0', '--sleep-max', '0', '--no-proxy'])
            messages = '\n'.join(notify.call_args.args[0])
        self.assertEqual(code, 1)
        self.assertEqual(hits, ['sid=slow', 'sid=fast'])
        notify.assert_called_once()
        self.assertIn('账号：159\n结果：结果不确定', messages)
        self.assertIn('账号：183\n结果：签到成功', messages)
        self.assertNotIn('sid=', messages)

    def test_blocked_dns_is_killed_without_a_late_post(self):
        url, hits = self.server('fast')
        url = url.replace('127.0.0.1', 'slow-dns.invalid')
        with tempfile.TemporaryDirectory() as directory:
            # sitecustomize 仅在本测试的子进程内替换 DNS；不接触真实 DNS，不包含凭证。
            marker = Path(directory) / 'dns-entered'
            (Path(directory) / 'sitecustomize.py').write_text(
                'import socket, time\n'
                'from pathlib import Path\n'
                '_real = socket.getaddrinfo\n'
                'def delayed(host, *args, **kwargs):\n'
                '    if host == "slow-dns.invalid":\n'
                f'        Path({str(marker)!r}).write_text("entered")\n'
                '        time.sleep(1.2)\n'
                '        host = "127.0.0.1"\n'
                '    return _real(host, *args, **kwargs)\n'
                'socket.getaddrinfo = delayed\n', encoding='utf-8')
            pythonpath = directory + os.pathsep + os.environ.get('PYTHONPATH', '')
            with patch.dict(os.environ, {'PYTHONPATH': pythonpath}), \
                    patch.object(sign, 'SIGN_URL', url), patch.object(sign, 'ACCOUNT_TIME_BUDGET', 0.2):
                start = time.monotonic()
                result = sign.sign_one('sid=synthetic', connect_timeout=0.2,
                                       read_timeout=0.2, retries=2, ignore_proxy=True)
                elapsed = time.monotonic() - start
            self.assertTrue(marker.exists(), '必须真实进入阻塞的 DNS 阶段')
            self.assertEqual(result.status, '结果不确定')
            self.assertLess(elapsed, 1.2)
            time.sleep(1.3)
            self.assertEqual(hits, [], '被终止的 DNS 工作不能在超时后继续提交签到')

    def test_push_slow_headers_and_body_have_a_total_attempt_limit(self):
        for mode in ('headers', 'body'):
            with self.subTest(mode=mode):
                url, hits = self.server(mode)
                with patch.object(pushplus, 'PUSHPLUS_URL', url):
                    start = time.monotonic()
                    with self.assertRaises(pushplus.Retryable):
                        pushplus.post_once({'token': 'synthetic'}, timeout=0.4)
                    elapsed = time.monotonic() - start
                self.assertLess(elapsed, 1.1)
                self.assertEqual(len(hits), 1)

    def test_push_overall_budget_includes_retries(self):
        url, hits = self.server('body')
        with patch.dict(os.environ, {'PUSHPLUS_TOKEN': 'synthetic'}), \
                patch.object(pushplus, 'PUSHPLUS_URL', url), \
                patch.object(pushplus, 'NOTIFY_ATTEMPT_TIMEOUT', 0.5), \
                patch.object(pushplus, 'NOTIFY_TIME_BUDGET', 0.8), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            start = time.monotonic()
            accepted = pushplus.send(['synthetic'], retries=2, sleep=lambda _: None)
            elapsed = time.monotonic() - start
        self.assertFalse(accepted)
        self.assertLess(elapsed, 1.5)
        self.assertEqual(len(hits), 2)

    def test_push_real_success_and_rejection_preserve_protocol(self):
        url, hits = self.server('fast')
        with patch.object(pushplus, 'PUSHPLUS_URL', url):
            pushplus.post_once({'token': 'synthetic'}, timeout=2)
        self.assertEqual(hits, [''])
        url, hits = self.server('reject')
        with patch.object(pushplus, 'PUSHPLUS_URL', url):
            with self.assertRaises(ValueError):
                pushplus.post_once({'token': 'synthetic'}, timeout=2)
        self.assertEqual(hits, [''])
        # 无效子进程输入不能输出 Cookie、Token 或原始异常。
        reply = network_worker.run('sign', {'cookie': 'synthetic-private'}, timeout=2)
        self.assertEqual(reply, {'state': 'failed'})

    def test_push_backoff_is_limited_by_the_remaining_budget(self):
        url, hits = self.server('5xx')
        with patch.dict(os.environ, {'PUSHPLUS_TOKEN': 'synthetic'}), \
                patch.object(pushplus, 'PUSHPLUS_URL', url), \
                patch.object(pushplus, 'NOTIFY_TIME_BUDGET', 0.4), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            start = time.monotonic()
            accepted = pushplus.send(['synthetic'], retries=2)
            elapsed = time.monotonic() - start
        self.assertFalse(accepted)
        self.assertLess(elapsed, 1.1)
        self.assertEqual(len(hits), 1, '预算耗尽后不能再启动下一次重试')

    def test_fallback_push_still_sends_when_network_worker_is_broken(self):
        # 真实场景：network_worker.py 有语法错误时，py_compile 失败，工作流随即运行兜底提醒。
        # pushplus.py 必须降级为进程内发送，而不是因导入失败让这次提醒丢失。
        url, hits = self.server('fast')
        with tempfile.TemporaryDirectory() as directory:
            source = Path(__file__).resolve().parent / 'pushplus.py'
            (Path(directory) / 'pushplus.py').write_text(source.read_text(encoding='utf-8'), encoding='utf-8')
            (Path(directory) / 'network_worker.py').write_text('def broken(:\n', encoding='utf-8')
            script = ('import sys; sys.path.insert(0, sys.argv[1]); import pushplus; '
                      'pushplus.PUSHPLUS_URL = sys.argv[2]; '
                      'raise SystemExit(0 if pushplus.send(["synthetic"]) else 1)')
            env = {key: value for key, value in os.environ.items() if key != 'PYTHONPATH'}
            env['PUSHPLUS_TOKEN'] = 'synthetic-token'
            import subprocess
            done = subprocess.run([sys.executable, '-c', script, directory, url],
                                  capture_output=True, text=True, timeout=30, env=env, cwd=directory)
        self.assertEqual(done.returncode, 0, '兜底提醒必须在 network_worker 损坏时仍能发出')
        self.assertEqual(len(hits), 1)
        self.assertIn('进程内直接发送', done.stderr)
        self.assertNotIn('synthetic-token', done.stdout + done.stderr)

    def test_completed_child_result_is_kept_at_the_deadline_boundary(self):
        # 子进程已在时限内完整返回，但父进程读完时钟恰好越过时限：必须采用结果，不能改报超时。
        child = MagicMock()
        child.communicate.return_value = (
            b'{"state":"ok","result":{"status":"\\u7b7e\\u5230\\u6210\\u529f","detail":"d","attempts":1}}', None)
        child.returncode = 0
        child.poll.return_value = 0
        clock = iter([0.0, 0.0] + [100.0] * 20)
        with patch.object(network_worker.subprocess, 'Popen', return_value=child), \
                patch.object(network_worker.time, 'monotonic', side_effect=lambda: next(clock)):
            reply = network_worker.run('sign', {'cookie': 'synthetic'}, timeout=2)
        self.assertEqual(reply['state'], 'ok')
        self.assertEqual(reply['result']['status'], '签到成功')

    def test_push_accepted_while_crossing_budget_is_not_reported_as_failure(self):
        # PushPlus 已接受，但这次请求跨过了总预算：应报成功且不重试，避免误报和重复推送。
        now = {'t': 0.0}
        def accepted_slowly(payload, timeout):
            now['t'] += pushplus.NOTIFY_TIME_BUDGET + 1
        with patch.dict(os.environ, {'PUSHPLUS_TOKEN': 'synthetic'}), \
                patch.object(pushplus, 'post_once', side_effect=accepted_slowly) as post, \
                patch.object(pushplus.time, 'monotonic', side_effect=lambda: now['t']), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            accepted = pushplus.send(['synthetic'], retries=2, sleep=lambda _: None)
        self.assertTrue(accepted)
        self.assertEqual(post.call_count, 1)

    def test_credentials_use_stdin_and_are_removed_from_child_environment(self):
        child = MagicMock()
        child.communicate.return_value = (b'{"state":"ok"}', None)
        child.returncode = 0
        child.poll.return_value = 0
        with patch.dict(os.environ, {'CSAIR_COOKIE_159': 'private-cookie',
                                    'CSAIR_COOKIES': 'private-legacy',
                                    'PUSHPLUS_TOKEN': 'private-token'}), \
                patch.object(network_worker.subprocess, 'Popen', return_value=child) as popen:
            self.assertEqual(network_worker.run('push', {'token': 'private-token'}, 2), {'state': 'ok'})
        command = popen.call_args.args[0]
        self.assertNotIn('private', ' '.join(command))
        env = popen.call_args.kwargs['env']
        for key in ('CSAIR_COOKIE_159', 'CSAIR_COOKIES', 'PUSHPLUS_TOKEN'):
            self.assertNotIn(key, env)
        self.assertEqual(json.loads(child.communicate.call_args_list[0].args[0]), {'token': 'private-token'})


if __name__ == '__main__':
    unittest.main()
