"""未知业务响应只向私人通知提供有界诊断，不输出原文或凭证。"""

import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import csair_daily_sign as sign


class ResponseDiagnostics(unittest.TestCase):
    def test_unknown_schema_reports_codes_and_missing_fields(self):
        result = sign.classify_response({
            'respCode': '0000', 'respMsg': 'success',
            'data': {'code': '00A2', 'message': '签到成功'},
        })
        self.assertEqual(result.status, '未知结果')
        self.assertFalse(result.ok)
        for text in ('respCode=0000', 'data.code=00A2', 'data.result=missing',
                     'data.message=string'):
            self.assertIn(text, result.diagnostic)
        self.assertNotIn('签到成功', result.diagnostic)

    def test_sensitive_values_and_arbitrary_keys_are_not_copied(self):
        private = 'private-cookie-token-手机号13800000000\n'
        data = {key: private for key in
                ('respCode', 'code', 'respMsg', 'message', 'msg', 'success', 'result')}
        data[private] = {'token': private}
        data['data'] = dict(data)
        result = sign.classify_response(data)
        self.assertEqual(result.status, '未知结果')
        self.assertNotIn('private', result.diagnostic)
        self.assertNotIn('13800000000', result.diagnostic)
        self.assertNotIn('\n', result.diagnostic)
        self.assertIn('code=string(redacted)', result.diagnostic)
        self.assertLess(len(result.diagnostic), 600)

    def test_business_codes_have_strict_size_and_type_limits(self):
        for code, expected in [('0000', '0000'), ('00A2', '00A2'),
                               (200, '200'), (-1, '-1'),
                               (True, 'boolean(redacted)'),
                               (12345678901, 'number(redacted)'),
                               ('13800000000', 'string(redacted)'),
                               ('secret', 'string(redacted)'),
                               ('0000\n', 'string(redacted)'),
                               ({'token': 'secret'}, 'object(redacted)'),
                               (['secret'], 'array(redacted)')]:
            with self.subTest(code=code):
                diagnostic = sign.response_diagnostic({'code': code})
                self.assertIn(';code=' + expected + ';', diagnostic)
                self.assertNotIn('secret', diagnostic)

    def test_other_unknown_branches_have_bounded_diagnostics(self):
        for value, expected in [(['secret'], 'root=array'),
                                (None, 'root=null'), ('secret', 'root=string')]:
            result = sign.classify_response(value)
            self.assertEqual(result.diagnostic, expected)
            self.assertFalse(result.ok)
        result = sign.classify_response({'success': False, 'message': '签到成功'})
        self.assertEqual(result.status, '未知结果')
        self.assertIn('success=false', result.diagnostic)

    def test_confirmed_results_have_no_diagnostic(self):
        for message in ('签到成功', '今日已签到', '请登录', '签到失败'):
            self.assertEqual(sign.classify_response({'message': message}).diagnostic, '')

    def test_diagnostic_reaches_push_payload_but_never_console_or_log(self):
        result = sign.classify_response({
            'respCode': '0000', 'data': {'code': '00A2', 'message': 'private-response'},
        })
        for public in ('0', '1'):
            with self.subTest(public=public), tempfile.TemporaryDirectory() as directory:
                logfile = Path(directory) / 'sign.log'
                output = io.StringIO()
                with patch.dict(os.environ, {
                        'CSAIR_COOKIE_183': 'sid=private-cookie', 'PUSHPLUS_TOKEN': 'private-token',
                        'CSAIR_PUBLIC_LOG': public}, clear=True), \
                        patch.object(sign, 'sign_one', return_value=result), \
                        patch.object(sign.pushplus, 'post_once') as post, \
                        contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                    code = sign.main(['--log', str(logfile)])
                self.assertEqual(code, 1)
                post.assert_called_once()
                content = post.call_args.args[0]['content']
                self.assertIn('账号：183', content)
                self.assertEqual(content.count('安全诊断：'), 1)
                self.assertIn('data.code=00A2', content)
                for value in ('private-cookie', 'private-token', 'private-response'):
                    self.assertNotIn(value, content + output.getvalue() + logfile.read_text())
                self.assertNotIn('安全诊断', output.getvalue() + logfile.read_text())
                self.assertNotIn('00A2', output.getvalue() + logfile.read_text())


if __name__ == '__main__':
    unittest.main()
