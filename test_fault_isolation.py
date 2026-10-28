import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock
import requests
import csair_daily_sign as m

class FaultIsolation(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def run_batch(self, env, results):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, env), patch.object(m, 'sign_one', side_effect=results) as sign, patch.object(m, 'notify_failures') as notify, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = m.main(['--log', str(Path(d)/'sign.txt'), '--sleep-min','0','--sleep-max','0'])
            return code, sign.call_count, notify.call_args.args[0] if notify.called else []

    def test_bad_cookie_does_not_block_other_accounts(self):
        code, calls, errors = self.run_batch({'CSAIR_COOKIE_159':'sid=valid', 'CSAIR_COOKIE_185':'invalid', 'CSAIR_COOKIE_183':'sid=valid2'}, [m.Result('签到成功','ok')]*2)
        self.assertEqual((code, calls), (1,2))
        self.assertIn('185', '\n'.join(errors))
        self.assertNotIn('invalid', '\n'.join(errors))

    def test_required_missing_account_does_not_block_configured_accounts(self):
        code, calls, errors = self.run_batch({'CSAIR_REQUIRED_ACCOUNTS':'159,185,183', 'CSAIR_COOKIE_159':'sid=valid', 'CSAIR_COOKIE_185':'sid=valid2'}, [m.Result('签到成功','ok')]*2)
        self.assertEqual((code, calls), (1,2))
        self.assertIn('183', '\n'.join(errors))

    def test_workflow_requires_all_three_and_missing_183_notifies_without_blocking_others(self):
        workflow = (Path(__file__).parent / '.github/workflows/csair-sign.yml').read_text(encoding='utf-8')
        import re
        required = re.search(r'CSAIR_REQUIRED_ACCOUNTS: "([^"]+)"', workflow).group(1)
        self.assertEqual(set(required.split(',')), {'159', '185', '183'})
        code, calls, errors = self.run_batch({
            'CSAIR_REQUIRED_ACCOUNTS': required,
            'CSAIR_COOKIE_159': 'sid=a', 'CSAIR_COOKIE_185': 'sid=b',
            'CSAIR_COOKIE_183': ''}, [m.Result('签到成功', 'ok')] * 2)
        self.assertEqual((code, calls), (1, 2))
        self.assertIn('CSAIR_COOKIE_183 未配置或为空', '\n'.join(errors))

    def test_unexpected_account_error_continues_without_retry(self):
        code, calls, errors = self.run_batch({'CSAIR_COOKIE_159':'sid=a', 'CSAIR_COOKIE_185':'sid=b'}, [RuntimeError('sid=a'),m.Result('签到成功','ok')])
        self.assertEqual((code,calls),(1,2))
        self.assertIn('159', '\n'.join(errors))
        self.assertNotIn('sid=a', '\n'.join(errors))

    def test_duplicate_reports_both_identifiers_and_submits_once(self):
        code, calls, errors = self.run_batch({'CSAIR_COOKIE_159':'sid=a','CSAIR_COOKIE_185':'sid=a'}, [m.Result('今日已签','ok')])
        self.assertEqual((code,calls),(1,1))
        self.assertIn('159', '\n'.join(errors))
        self.assertIn('185', '\n'.join(errors))

    def test_missing_push_token_cannot_report_green(self):
        code, calls, errors = self.run_batch({'CSAIR_COOKIE_159':'sid=a','CSAIR_REQUIRE_PUSHPLUS':'1'}, [m.Result('今日已签','ok')])
        self.assertEqual((code,calls),(1,1))
        self.assertIn('PUSHPLUS_TOKEN','\n'.join(errors))

    def test_check_only_errors_never_sign_or_notify(self):
        with patch.dict(os.environ, {'CSAIR_COOKIE_159':'invalid'}), patch.object(m,'sign_one') as sign, patch.object(m,'notify_failures') as notify, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(m.main(['--check']),2)
            sign.assert_not_called()
            notify.assert_not_called()

    def test_push_test_does_not_read_cookies_or_sign(self):
        for success in (True, False):
            with patch.object(m, 'load_cookies', side_effect=AssertionError('Cookie read')), patch.object(m,'sign_one') as sign, patch.object(m,'notify_failures',return_value=success) as notify:
                self.assertEqual(m.main(['--test-pushplus']), 0 if success else 1)
                sign.assert_not_called()
                self.assertIn('测试',notify.call_args.kwargs['title'])

    def test_no_proxy_blocks_environment_proxy(self):
        session = requests.Session()
        response = MagicMock()
        response.__enter__.return_value = response
        response.status_code=401
        with patch.dict(os.environ, {'https_proxy':'http://proxy.invalid:8080'}), patch.object(requests, 'Session', return_value=session), patch.object(session, 'post',return_value=response):
            result=m._sign_one('sid=a',requests,ignore_proxy=True)
            settings=session.merge_environment_settings(m.SIGN_URL,{},True,None,None)
            self.assertFalse(settings['proxies'])
            self.assertFalse(session.trust_env)
            self.assertEqual(result.status,'Cookie 失效')

    def test_success_notification_failure_fails_job_without_resigning(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {'CSAIR_COOKIE_159':'sid=a'}), patch.object(m,'sign_one',return_value=m.Result('签到成功','ok')) as sign, patch.object(m,'notify_failures',return_value=False) as notify, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(m.main(['--log',str(Path(d)/'log.txt')]),1)
            sign.assert_called_once()
            notify.assert_called_once()
            self.assertEqual(notify.call_args.kwargs['title'],'南航签到完成')

    def test_all_signed_accounts_send_one_summary(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {'CSAIR_COOKIE_159':'sid=a','CSAIR_COOKIE_183':'sid=b'}), patch.object(m,'sign_one',return_value=m.Result('今日已签','ok')), patch.object(m,'notify_failures',return_value=True) as notify, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(m.main(['--log',str(Path(d)/'log.txt'),'--sleep-min','0','--sleep-max','0']),0)
            notify.assert_called_once()
            content='\n'.join(notify.call_args.args[0])
            self.assertIn('159',content)
            self.assertIn('183',content)
            self.assertIn('今日已签',content)
