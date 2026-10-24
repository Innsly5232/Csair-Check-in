import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
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
        cases = [({'respMsg':'签到成功'}, '签到成功'),
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
        for exc in (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError,
                    requests.exceptions.ChunkedEncodingError):
            with self.subTest(exc=exc):
                result, session = self.invoke([exc('sid=secret')])
                self.assertEqual(result.status, '结果不确定')
                self.assertEqual(session.post.call_count, 1)
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
            with patch.object(m, 'CONFIG_COOKIES', ['sid=old']), self.assertRaises(ValueError):
                m.load_cookies(p)
            for bad in ('not-a-cookie', '=abc', 'sid=中文', 'sid=abc\x00'):
                p.write_text(bad)
                with self.assertRaises(ValueError): m.load_cookies(p)

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
