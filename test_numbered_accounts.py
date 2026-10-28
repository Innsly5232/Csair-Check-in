import os
import unittest
from unittest.mock import patch
import csair_daily_sign as m

class NumberedAccounts(unittest.TestCase):
    def test_three_identifiers(self):
        with patch.dict(os.environ, {f'CSAIR_COOKIE_{n}': f'sid={n}' for n in m.ACCOUNT_IDS}, clear=True):
            accounts, count = m.load_cookies()
        self.assertEqual([a.label for a in accounts], list(m.ACCOUNT_IDS))
        self.assertEqual([a.cookie for a in accounts], [f'sid={n}' for n in m.ACCOUNT_IDS])
        self.assertEqual(count, 0)

    def test_partial_and_legacy_ignored(self):
        with patch.dict(os.environ, {'CSAIR_COOKIE_159':'', 'CSAIR_COOKIE_185':'sid=new',
                                    'CSAIR_COOKIES':'sid=old', 'CSAIR_ACCOUNT_NAMES':'wrong'}, clear=True):
            accounts, _ = m.load_cookies()
        self.assertEqual(accounts, [m.Account('185', 'sid=new')])

    def test_empty_never_falls_back(self):
        with patch.dict(os.environ, {'CSAIR_COOKIE_159':'', 'CSAIR_COOKIES':'sid=old'}, clear=True):
            with self.assertRaises(ValueError): m.load_cookies()

    def test_invalid_reports_identifier_without_cookie(self):
        for bad in ('secret', 'sid=secret\nsid=other', 'sid=中文'):
            with patch.dict(os.environ, {'CSAIR_COOKIE_185':bad}, clear=True):
                with self.assertRaises(ValueError) as error: m.load_cookies()
                self.assertIn('185', str(error.exception))
                self.assertNotIn(bad, str(error.exception))

    def test_duplicate_keeps_first_identifier(self):
        with patch.dict(os.environ, {'CSAIR_COOKIE_159':'sid=same', 'CSAIR_COOKIE_185':'sid=same',
                                    'CSAIR_COOKIE_183':'Cookie: sid=other'}, clear=True):
            accounts, count = m.load_cookies()
        self.assertEqual([a.label for a in accounts], ['159','183'])
        self.assertEqual(count, 1)
