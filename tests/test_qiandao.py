import os
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
import resilience as net
import soushuba as app


def response(body='', status=200, encoding='utf-8'):
    value = requests.Response()
    value.status_code = status
    value._content = body.encode(encoding)
    value.encoding = encoding
    value.url = 'https://example.test/start'
    return value


class ResilienceTests(unittest.TestCase):
    @patch('resilience.time.sleep')
    def test_http_503_read_recovers(self, sleep):
        session = Mock()
        session.get.side_effect = [response('Unavailable', 503), response('ok')]
        self.assertEqual(net.read_page(session, 'https://example.test', 'read').text, 'ok')
        self.assertEqual(session.get.call_count, 2)

    @patch('soushuba.requests.get')
    def test_relative_refresh_url(self, get):
        get.return_value = response('<meta HTTP-EQUIV="Refresh" content="0; URL=/next">')
        self.assertEqual(app.get_refresh_url('https://example.test/start'), 'https://example.test/next')

    def test_login_rejection_posts_only_once(self):
        client = app.SouShuBaClient('example.test', 'user', 'password')
        client.login_form_hash = Mock(return_value=('hash', 'token'))
        client.session = Mock()
        client.session.post.return_value = response('登录失败，您还可以尝试 3 次')
        with self.assertRaisesRegex(net.TaskError, '论坛拒绝登录'):
            client.login()
        client.session.post.assert_called_once()
        client.session.get.assert_not_called()

    @patch('resilience.time.sleep')
    def test_transient_reads_recover(self, sleep):
        session = Mock()
        session.get.side_effect = [requests.ConnectTimeout(), response('Discuz! Database Error (0) notconnect'), response('ok')]
        self.assertEqual(net.read_page(session, 'https://example.test', 'read').text, 'ok')
        self.assertEqual([x.args[0] for x in sleep.call_args_list], [10, 30])
        self.assertTrue(all(c.kwargs['timeout'] == (10, 30) for c in session.get.call_args_list))

    @patch('resilience.time.sleep')
    def test_reads_stop_after_three(self, sleep):
        session = Mock()
        session.get.side_effect = requests.ReadTimeout('secret-query-string')
        with self.assertRaises(net.TaskError) as error:
            net.read_page(session, 'https://example.test', 'read')
        self.assertEqual(session.get.call_count, 3)
        self.assertNotIn('secret-query-string', str(error.exception))

    def test_invalid_url_stops_before_network(self):
        session = Mock()
        with self.assertRaises(net.TaskError):
            net.read_page(session, None, 'entry')
        session.get.assert_not_called()

    def test_access_denied_is_not_retried(self):
        session = Mock()
        session.get.return_value = response('Forbidden', 403)
        with self.assertRaises(net.TaskError):
            net.read_page(session, 'https://example.test', 'read')
        session.get.assert_called_once()

    def test_post_timeout_is_not_retried(self):
        session = Mock()
        session.post.side_effect = requests.ReadTimeout()
        with self.assertRaises(net.TaskError) as error:
            net.send_once(session, 'https://example.test', 'post')
        self.assertTrue(error.exception.uncertain)
        session.post.assert_called_once()

    @patch('resilience.time.sleep')
    def test_missing_form_has_useful_error(self, sleep):
        session = Mock()
        session.get.return_value = response('<html>Maintenance</html>')
        with self.assertRaisesRegex(net.TaskError, 'formhash'):
            net.read_page(session, 'https://example.test', 'login', lambda r: app.form_fields(r, True))
        self.assertEqual(session.get.call_count, 3)

    def test_form_attributes_can_change_order(self):
        page = response("<div class='x' id='main_messaqge_ab12'><input value='token' type='hidden' name='formhash'></div>")
        self.assertEqual(app.form_fields(page, True), ('ab12', 'token'))

    @patch('soushuba.time.sleep')
    def test_partial_database_failure_does_not_replay_posts(self, sleep):
        client = app.SouShuBaClient('example.test', 'user', 'password')
        client.space_form_hash = Mock(return_value='token')
        client.session = Mock()
        client.session.post.side_effect = [response('操作成功'), response('操作成功'), response('Discuz! Database Error (0) notconnect')]
        with self.assertRaises(net.TaskError) as error:
            client.space()
        self.assertEqual(client.completed, 2)
        self.assertEqual(client.session.post.call_count, 3)
        self.assertTrue(error.exception.uncertain)

    @patch('soushuba.time.sleep')
    def test_five_gbk_successes_and_four_intervals(self, sleep):
        client = app.SouShuBaClient('example.test', 'user', 'password')
        client.space_form_hash = Mock(return_value='token')
        client.session = Mock()
        client.session.post.return_value = response('<?xml version="1.0" encoding="gbk"?><root><![CDATA[操作成功]]></root>', encoding='gbk')
        client.space()
        self.assertEqual(client.completed, 5)
        self.assertEqual(client.session.post.call_count, 5)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [120] * 4)

    def test_summary_preserves_partial_count(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, 'summary.md')
            with patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': path}):
                net.write_summary(False, 2, net.TaskError('发布第 3 条动态', '数据库错误', True))
            with open(path, encoding='utf-8') as stream:
                result = stream.read()
            self.assertIn('2/5', result)
            self.assertIn('不要直接重跑整轮', result)

    @patch.dict(os.environ, {'SOUSHUBA_HOSTNAME': 'example.test', 'SOUSHUBA_USERNAME': 'user', 'SOUSHUBA_PASSWORD': 'password'})
    @patch('soushuba.time.sleep')
    @patch('soushuba.get_refresh_url', return_value='https://example.test')
    @patch('soushuba.get_url', return_value='https://example.test')
    @patch('soushuba.SouShuBaClient')
    @patch('soushuba.write_summary')
    def test_credit_failure_does_not_fail_completed_posts(self, summary, factory, *unused):
        factory.return_value.completed = 5
        factory.return_value.credit.side_effect = net.TaskError('查询银币', '数据库错误')
        with patch('sys.argv', ['soushuba.py']):
            self.assertEqual(app.main(), 0)
        self.assertEqual(summary.call_args.args, (False, 5, None, None, True))

    @patch.dict(os.environ, {'SOUSHUBA_HOSTNAME': 'example.test', 'SOUSHUBA_USERNAME': 'user', 'SOUSHUBA_PASSWORD': 'password'})
    @patch('soushuba.time.sleep')
    @patch('soushuba.get_refresh_url', return_value='https://example.test')
    @patch('soushuba.get_url', return_value='https://example.test')
    @patch('soushuba.SouShuBaClient')
    @patch('soushuba.write_summary')
    def test_login_only_never_publishes(self, summary, factory, *unused):
        with patch('sys.argv', ['soushuba.py', '--check-login']):
            self.assertEqual(app.main(), 0)
        factory.return_value.space.assert_not_called()
        factory.return_value.credit.assert_not_called()


if __name__ == '__main__':
    unittest.main()
