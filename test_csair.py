import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch, Mock

path = Path(__file__).resolve().parent / 'csair_daily_sign.py'
spec = importlib.util.spec_from_file_location('csair', path)
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)
import requests

class Response:
    def __init__(self, status=200, data=None, raw=None):
        self.status_code = status
        self.raw = raw if raw is not None else json.dumps(data).encode()
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def iter_content(self, **kw): yield self.raw

class Tests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def invoke(self, responses, **kw):
        session = Mock()
        session.__enter__ = Mock(return_value=session)
        session.__exit__ = Mock(return_value=False)
        session.post.side_effect = responses
        with patch.object(requests, 'Session', return_value=session):
            result = m.sign_one('sid=secret', requests, sleep=lambda _: None, **kw)
        return result, session

    def test_classification(self):
        cases = [({'respMsg':'success','respCode':'0000','data':{'code':'00A2','result':'签到成功'}}, '签到成功'),
                 ({'respMsg':'success','data':{'result':'今日已签到'}}, '今日已签'),
                 ({'respMsg':'签到成功','data':{'result':'签到失败'}}, '签到未完成'),
                 ({'respMsg':'success','data':{'result':'请登录'}}, 'Cookie 失效'),
                 ({'respMsg':'success','data':{'result':'success'}}, '未知结果'),
                 ({'respMsg':'success','data':{'success':False,'result':'签到成功'}}, '未知结果'),
                 ({'respMsg':'签到成功'}, '签到成功'),
                 ({'message':'今日已签到'}, '今日已签'),
                 ({'respMsg':'重复签到成功'}, '今日已签'),
                 ({'message':'签到未成功'}, '签到未完成'),
                 ({'message':'未能签到成功'}, '签到未完成'),
                 ({'message':'签到成功，但后续处理 error'}, '签到未完成'),
                 ({'message':'unsuccessful'}, '签到未完成'),
                 ({'message':'登录已过期'}, 'Cookie 失效'),
                 ({'message':'success'}, '未知结果'),
                 ({'message':'成功'}, '未知结果'),
                 ({'success':False,'message':'签到成功'}, '未知结果'),
                 ({'respMsg':'签到成功','message':'操作失败'}, '签到未完成'),
                 ([], '未知结果'), (None, '未知结果'), ('ok', '未知结果'),
                 ({'message': {'foo':'签到成功'}}, '未知结果')]
        for value, expected in cases:
            with self.subTest(value=value): self.assertEqual(m.classify_response(value).status, expected)

    def test_http(self):
        for status, expected in [(401,'Cookie 失效'),(403,'访问被拒绝'),(429,'请求受限'),
                                 (302,'请求失败'),(500,'结果不确定'),(400,'请求失败')]:
            with self.subTest(status=status):
                result, session = self.invoke([Response(status, {'message':'签到成功'})])
                self.assertEqual(result.status, expected)
                self.assertEqual(session.post.call_count, 1)

    def test_success_and_post(self):
        result, session = self.invoke([Response(data={'respMsg':'签到成功'})])
        self.assertTrue(result.ok)
        kw = session.post.call_args.kwargs
        self.assertFalse(kw['allow_redirects'])
        self.assertEqual(kw['json'], m.SIGN_BODY)
        self.assertEqual(kw['timeout'], (10,20))

    def test_connect_timeout_retry(self):
        result, session = self.invoke([requests.exceptions.ConnectTimeout(), Response(data={'message':'签到成功'})])
        self.assertTrue(result.ok)
        self.assertEqual(result.attempts, 2)
        result, session = self.invoke([requests.exceptions.ConnectTimeout()]*3)
        self.assertEqual(result.status, '请求失败')
        self.assertEqual(session.post.call_count, 3)

    def test_no_retry_ambiguous(self):
        # 裸 ConnectionError（无 urllib3 连接前原因）无法确认请求体是否已发出，不重试。
        for exc in (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError,
                    requests.exceptions.ChunkedEncodingError):
            with self.subTest(exc=exc):
                result, session = self.invoke([exc('sid=secret')])
                self.assertEqual(result.status, '结果不确定')
                self.assertEqual(session.post.call_count, 1)
                self.assertNotIn('secret', result.detail)

    def _connection_error_caused_by(self, name):
        # 构造异常链：ConnectionError 的 __cause__ 是名为 name 的 urllib3 风格异常。
        cause = type(name, (Exception,), {})('boom')
        exc = requests.exceptions.ConnectionError('wrapped')
        exc.__cause__ = cause
        return exc

    def test_connection_before_send_is_retried(self):
        # DNS 解析失败、连接被拒绝、建连超时：请求体未发出，应重试后成功。
        for name in ('NewConnectionError', 'NameResolutionError', 'ConnectTimeoutError'):
            with self.subTest(name=name):
                result, session = self.invoke([self._connection_error_caused_by(name),
                                               Response(data={'message': '签到成功'})])
                self.assertTrue(result.ok)
                self.assertEqual(result.attempts, 2)
        # 达到重试上限后给出明确诊断，且只含异常类名、不含内容。
        result, session = self.invoke([self._connection_error_caused_by('NewConnectionError')] * 3)
        self.assertEqual((result.status, session.post.call_count), ('请求失败', 3))
        self.assertIn('ConnectionError', result.detail)
        self.assertNotIn('boom', result.detail)

    def test_connection_after_send_not_retried(self):
        # 连接建立后断开（ProtocolError / RemoteDisconnected）无法确认是否已提交，不重试。
        for name in ('ProtocolError', 'RemoteDisconnected'):
            with self.subTest(name=name):
                result, session = self.invoke([self._connection_error_caused_by(name)])
                self.assertEqual((result.status, session.post.call_count), ('结果不确定', 1))

    def test_slow_body_hits_time_budget(self):
        # 真实阻塞复现：服务端在 next() 中逐块阻塞，单次 read 不超时，迭代器也不主动结束。
        # 后台线程读取 + 主线程限时等待，应在预算内主动中断、关闭响应、且不重发 POST。
        import time as _time
        closed = threading.Event()
        class Blocking(Response):
            def __init__(self):
                super().__init__(data={})
            def close(self):
                # 模拟 requests 的 close 会排空响应体而阻塞；必须在主线程路径之外执行。
                _time.sleep(0.6)
                closed.set()
            def iter_content(self, **kw):
                for _ in range(50):
                    _time.sleep(0.05)  # 阻塞发生在 next() 内部，检查无法在循环体执行
                    yield b'x'
        start = _time.monotonic()
        with patch.object(m, 'ACCOUNT_TIME_BUDGET', 0.2):
            result, session = self.invoke([Blocking()], connect_timeout=0.01, read_timeout=0.01)
        elapsed = _time.monotonic() - start
        self.assertEqual(result.status, '结果不确定')
        self.assertIn('时间上限', result.detail)
        self.assertEqual(session.post.call_count, 1)
        # 关键：close() 阻塞 0.6s，但主线程不等它，应在预算附近就返回。
        self.assertLess(elapsed, 0.5)
        # close() 确实在后台异步执行（打断连接），只是不在主线程热路径上。
        self.assertTrue(closed.wait(2.0))

    def test_read_error_is_uncertain_not_retried(self):
        # 读取响应体阶段抛异常（请求体已发出）：归为结果不确定，不重发，诊断只含类名。
        class Broken(Response):
            def iter_content(self, **kw):
                raise requests.exceptions.ChunkedEncodingError('sid=secret')
                yield b''  # pragma: no cover
        result, session = self.invoke([Broken(data={})])
        self.assertEqual((result.status, session.post.call_count), ('结果不确定', 1))
        self.assertIn('ChunkedEncodingError', result.detail)
        self.assertNotIn('secret', result.detail)

    def test_bad_json_and_size(self):
        for raw in (b'<html>secret</html>', b'\xff', b'x'*(m.MAX_RESPONSE_BYTES+1)):
            result, session = self.invoke([Response(raw=raw)])
            self.assertEqual(result.status, '结果不确定')
            self.assertNotIn('secret', result.detail)

    def test_configuration(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {}, clear=True):
            p = Path(d)/'cookies.txt'
            p.write_text('\ufeff# test\nCookie: sid=abc; x=1\nsid=abc; x=1\nsid=def\n', encoding='utf-8')
            accounts, duplicate = m.load_cookies(p)
            self.assertEqual(len(accounts), 2)
            self.assertEqual(duplicate, 1)
            self.assertEqual(accounts[0].label, '配置行2')
            with patch.dict(os.environ, {'CSAIR_COOKIES':'sid=env'}):
                self.assertEqual(m.load_cookies(p)[0][0].cookie, 'sid=env')
                self.assertEqual(m.load_cookies(p, explicit=True)[0][0].cookie, 'sid=abc; x=1')
            p.write_text('')
            with self.assertRaises(ValueError):
                m.load_cookies(p)
            # 代码内不再有可写入 Cookie 的位置；文件不存在也没有环境变量时同样报错。
            self.assertFalse(hasattr(m, 'CONFIG_COOKIES'))
            with self.assertRaises(ValueError):
                m.load_cookies(Path(d)/'missing.txt')
            for bad in ('not-a-cookie', '=abc', 'sid=中文', 'sid=abc\x00'):
                p.write_text(bad)
                with self.assertRaises(ValueError): m.load_cookies(p)

    def test_read_body_within_aborts_real_slow_socket(self):
        # 真实回归：真实 requests + 真实慢速 HTTP 服务。服务端发送头后逐字节滴送、永不结束。
        # read_body_within 必须在预算附近返回 timed_out，不被 requests 的阻塞式 close 拖住。
        try:
            import requests as real_requests
        except ImportError:
            self.skipTest('需要真实 requests；本地桩环境跳过，CI 覆盖')
        if not hasattr(real_requests, 'get'):
            self.skipTest('检测到最小桩 requests（无 get），跳过真实 socket 测试；CI 覆盖')
        import socket as _socket

        srv = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        srv.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        srv.bind(('127.0.0.1', 0)); srv.listen(1)
        port = srv.getsockname()[1]
        stop = threading.Event()

        def server():
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            try:
                conn.recv(65536)
                conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n')
                while not stop.is_set():
                    conn.sendall(b'x')      # 滴送：永不发完 100000 字节
                    stop.wait(0.3)
            except OSError:
                pass
            finally:
                conn.close()

        t = threading.Thread(target=server, daemon=True); t.start()
        self.addCleanup(lambda: (stop.set(), srv.close()))
        try:
            response = real_requests.get(f'http://127.0.0.1:{port}/', stream=True, timeout=(2, 2))
            start = time.monotonic()
            outcome = m.read_body_within(response, m.MAX_RESPONSE_BYTES, 0.2)
            elapsed = time.monotonic() - start
        finally:
            stop.set()
        self.assertTrue(outcome.timed_out)
        self.assertLess(elapsed, 1.0)   # 若主线程等阻塞的 close，会明显超过预算

    def test_lock_and_release(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/'run.lock'
            with m.run_lock(p):
                with self.assertRaises(m.AlreadyRunning):
                    with m.run_lock(p): pass
            with m.run_lock(p): pass

    def test_offline_check(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {}, clear=True):
            p=Path(d)/'cookies.txt'; p.write_text('sid=secret')
            with patch.object(requests, 'Session', side_effect=AssertionError('network forbidden')), contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(m.main(['--cookies',str(p),'--check']),0)
            self.assertNotIn('secret',output.getvalue())
            self.assertEqual(list(Path(d).iterdir()), [p])

    def test_batch_log_and_exit(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'cookies.txt'; p.write_text('sid=secret\nsid=secret2')
            log=Path(d)/'log.txt'
            for results, expected in [([m.Result('签到成功','ok'),m.Result('今日已签','ok')],0),
                                      ([m.Result('签到成功','ok'),m.Result('未知结果','unknown')],1)]:
                with patch.object(m,'sign_one',side_effect=results), patch.object(m, 'notify_failures'), contextlib.redirect_stdout(io.StringIO()):
                    code=m.main(['--cookies',str(p),'--log',str(log),'--sleep-min','0','--sleep-max','0'])
                self.assertEqual(code,expected)
            content=log.read_text()
            self.assertIn('+0800',content)
            self.assertIn('配置行2',content)
            self.assertNotIn('secret',content)

    def test_missing_file_exit(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(m.main(['--cookies','/nonexistent/config.txt','--check']),2)

    def test_invalid_numeric_flags(self):
        for args in (['--connect-timeout','nan'],['--retries','6'],['--sleep-min','-1']):
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exc:
                m.parse_args(args)
            self.assertEqual(exc.exception.code,2)

if __name__ == '__main__': unittest.main(verbosity=2)
