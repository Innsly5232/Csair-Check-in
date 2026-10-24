import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock
import csair_daily_sign as m


class Notifications(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def test_names_follow_original_lines_and_duplicates(self):
        with patch.dict(os.environ, {'CSAIR_COOKIES': 'sid=a\nsid=a\nsid=b',
                                    'CSAIR_ACCOUNT_NAMES': '本人\n重复\n家人'}):
            accounts, duplicates = m.load_cookies()
        self.assertEqual(duplicates, 1)
        self.assertEqual([a.label for a in accounts], ['本人（配置行1）', '家人（配置行3）'])

    def test_names_mismatch_or_cookie_as_name_rejected(self):
        for names in ('本人', '本人\nsid=secret'):
            with patch.dict(os.environ, {'CSAIR_COOKIES': 'sid=a\nsid=b', 'CSAIR_ACCOUNT_NAMES': names}):
                with self.assertRaises(ValueError) as error:
                    m.load_cookies()
                self.assertNotIn('secret', str(error.exception))

    def test_batch_includes_all_results_and_success_notifies(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {
                'CSAIR_COOKIES': 'sid=secret1\nsid=secret2\nsid=secret3',
                'CSAIR_ACCOUNT_NAMES': '本人\n家人\n备用'}):
            args = ['--log', str(Path(d)/'log.txt'), '--sleep-min', '0', '--sleep-max', '0']
            results = [m.Result('今日已签', 'ok'), m.Result('Cookie 失效', 'HTTP 401'),
                       m.Result('结果不确定', '网络异常')]
            with patch.object(m, 'sign_one', side_effect=results), patch.object(m, 'notify_failures') as notify, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(m.main(args), 1)
                notify.assert_called_once()
                content = '\n'.join(notify.call_args.args[0])
                self.assertIn('家人', content)
                self.assertIn('备用', content)
                self.assertIn('HTTP 401', content)
                self.assertIn('本人', content)
                self.assertNotIn('secret', content)
            with patch.object(m, 'sign_one', return_value=m.Result('签到成功', 'ok')), patch.object(m, 'notify_failures') as notify, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(m.main(args), 0)
                notify.assert_called_once()
                self.assertEqual(notify.call_args.kwargs['title'], '南航签到完成')
                self.assertIn('本人', '\n'.join(notify.call_args.args[0]))

    def test_configuration_errors_notify_but_check_is_offline(self):
        for cookie in ('', 'invalid'):
            with patch.dict(os.environ, {'CSAIR_COOKIES': cookie}), patch.object(m, 'notify_failures') as notify, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(m.main([]), 2)
                notify.assert_called_once()
                notify.reset_mock()
                self.assertEqual(m.main(['--check']), 2)
                notify.assert_not_called()

    def test_push_payload_and_acceptance(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.read.return_value = b'{"code":200,"data":[{"code":200,"channel":"cmcc"}]}'
        opener = MagicMock()
        opener.open.return_value = response
        with patch.dict(os.environ, {'PUSHPLUS_TOKEN': 'private-token', 'CSAIR_COOKIES': 'sid=private-cookie'}), patch.object(m.urllib.request, 'build_opener', return_value=opener), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertTrue(m.notify_failures(['账号：家人；原因：HTTP 401']))
        request = opener.open.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, 'https://www.pushplus.plus/send')
        self.assertEqual(payload['token'], 'private-token')
        self.assertEqual(payload['template'], 'txt')
        self.assertEqual(payload['channel'], 'cmcc')
        self.assertNotIn('private-cookie', request.data.decode())
        self.assertNotIn('Cookie', request.headers)
        self.assertNotIn('private-token', out.getvalue())

    def test_notification_errors_are_redacted_and_rejections_not_retried(self):
        for raw in (b'{"code":400,"msg":"private-token"}', b'not json', b'[]',
                    b'{"code":200,"data":[{"code":400}]}', b'x'*65537):
            response = MagicMock()
            response.__enter__.return_value = response
            response.status = 200
            response.read.return_value = raw
            opener = MagicMock()
            opener.open.return_value = response
            with patch.dict(os.environ, {'PUSHPLUS_TOKEN': 'private-token'}), patch.object(m.urllib.request, 'build_opener', return_value=opener), contextlib.redirect_stderr(io.StringIO()) as out:
                self.assertFalse(m.notify_failures(['失败']))
            self.assertNotIn('private-token', out.getvalue())
            opener.open.assert_called_once()
        # 网络层异常属于可重试：共 1 次 + 2 次重试，输出不含 Token。
        with patch.dict(os.environ, {'PUSHPLUS_TOKEN': 'private-token'}), patch.object(m.pushplus.time, 'sleep'), patch.object(m.urllib.request, 'build_opener', side_effect=TimeoutError('private-token')) as connect, contextlib.redirect_stderr(io.StringIO()) as out:
            self.assertFalse(m.notify_failures(['失败']))
            self.assertEqual(connect.call_count, 3)
            self.assertNotIn('private-token', out.getvalue())

    def test_body_transfer_interrupted_is_retried(self):
        import http.client
        # IncompleteRead 不是 OSError，且在读响应体时抛出：须被归为可重试并重试满 3 次。
        for exc in (http.client.IncompleteRead(b'partial-private'), http.client.HTTPException('private')):
            with self.subTest(exc=type(exc).__name__):
                response = MagicMock()
                response.__enter__.return_value = response
                response.status = 200
                response.read.side_effect = exc  # 连接建立后，读响应体时中断
                opener = MagicMock()
                opener.open.return_value = response
                with patch.dict(os.environ, {'PUSHPLUS_TOKEN': 'private-token'}), patch.object(m.pushplus.time, 'sleep'), patch.object(m.urllib.request, 'build_opener', return_value=opener), contextlib.redirect_stderr(io.StringIO()) as out:
                    self.assertFalse(m.notify_failures(['失败']))
                    self.assertEqual(opener.open.call_count, 3)
                self.assertNotIn('private', out.getvalue())
        # HTTPException 在 open() 阶段抛出同样应重试（例如 BadStatusLine）。
        with patch.dict(os.environ, {'PUSHPLUS_TOKEN': 'private-token'}), patch.object(m.pushplus.time, 'sleep'), patch.object(m.urllib.request, 'build_opener', side_effect=http.client.BadStatusLine('private')) as connect, contextlib.redirect_stderr(io.StringIO()) as out:
            self.assertFalse(m.notify_failures(['失败']))
            self.assertEqual(connect.call_count, 3)
            self.assertNotIn('private', out.getvalue())

    def test_missing_token_or_no_failures_do_not_connect(self):
        with patch.object(m.urllib.request, 'build_opener') as connect, contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(m.notify_failures([]))
            self.assertFalse(m.notify_failures(['失败']))
            connect.assert_not_called()

    def test_redirects_blocked(self):
        self.assertIsNone(m.NoRedirect().redirect_request(None, None, 307, '', {}, 'https://example.com'))


if __name__ == '__main__':
    unittest.main()
