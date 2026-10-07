import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from fastapi.testclient import TestClient
from urllib3._collections import HTTPHeaderDict
from app.desktop_web import create_desktop_app


def reply(status=200, body=b'{}', headers=None, cookie=None):
    response = requests.Response()
    response.status_code = status
    response._content = body
    response.headers.update(headers or {'Content-Type': 'application/json'})
    raw = HTTPHeaderDict()
    if cookie:
        raw.add('Set-Cookie', cookie)
    response.raw = SimpleNamespace(headers=raw)
    return response


class DesktopGatewayTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(create_desktop_app('https://cloud.example'),
                                 base_url='http://127.0.0.1:18765')
        self.addCleanup(self.client.close)

    def test_login_uses_cloud_and_keeps_session_on_loopback(self):
        upstream = reply(303, headers={'Location': '/desk/'},
                         cookie='pc_session=cloud-session; HttpOnly; Secure; Path=/')
        with patch('app.desktop_web.requests.Session.request', return_value=upstream) as request:
            result = self.client.post('/login', data={'username': 'teacher', 'password': 'test'},
                                      follow_redirects=False)
        self.assertEqual(result.status_code, 303)
        self.assertEqual(result.headers['location'], '/desk/')
        self.assertEqual(self.client.cookies['pc_session'], 'cloud-session')
        self.assertEqual(request.call_args.args[:2], ('POST', 'https://cloud.example/login'))
        self.assertFalse(request.call_args.kwargs['allow_redirects'])

    def test_api_preserves_auth_query_and_cloud_data(self):
        self.client.cookies.set('pc_session', 'cloud-session')
        with patch('app.desktop_web.requests.Session.request', return_value=reply(body=b'{"ok":true,"tasks":[]}')) as request:
            result = self.client.get('/api/v1/bootstrap?refresh=1')
        self.assertEqual(result.json(), {'ok': True, 'tasks': [], 'printers': [], 'client_kind': 'desktop',
                                         'station_available': False, 'station_ready': False,
                                         'station_sync_error': '', 'station_pending_print_ids': [],
                                         'station_pickup_ids': []})
        self.assertEqual(request.call_args.args[1], 'https://cloud.example/api/v1/bootstrap?refresh=1')
        self.assertIn('pc_session=cloud-session', request.call_args.kwargs['headers']['cookie'])

    def test_cloud_failure_is_recoverable(self):
        with patch('app.desktop_web.requests.Session.request', side_effect=requests.ConnectionError):
            result = self.client.get('/api/v1/bootstrap')
        self.assertEqual(result.status_code, 502)
        self.assertFalse(result.json()['ok'])

    def test_cloud_connection_ignores_broken_environment_proxy(self):
        with patch('app.desktop_web.requests.Session') as factory:
            session = factory.return_value.__enter__.return_value
            session.request.return_value = reply(body=b'{"ok":true}')
            result = self.client.get('/api/v1/bootstrap')
        self.assertEqual(result.status_code, 200)
        self.assertIs(session.trust_env, False)

    def test_logged_in_client_starts_background_queue_sync_without_printer(self):
        station = Mock(enabled=True, ready=True, syncer=None)
        station.snapshot.return_value = []
        station.pending_print_ids.return_value = []
        station.pickup_ids.return_value = []
        client = TestClient(create_desktop_app('https://cloud.example', station),
                            base_url='http://127.0.0.1:18765')
        self.addCleanup(client.close)
        client.cookies.set('pc_session', 'cloud-session')
        with patch('app.desktop_web.requests.Session.request',
                   return_value=reply(body=b'{"ok":true,"tasks":[]}')):
            self.assertEqual(client.get('/api/v1/bootstrap').status_code, 200)
        station.bind_session.assert_called_once_with('pc_session=cloud-session')

    def test_unrelated_websites_cannot_post_to_desktop(self):
        with patch('app.desktop_web.requests.Session.request') as request:
            result = self.client.post('/login', headers={'Origin': 'https://unrelated.example'})
        self.assertEqual(result.status_code, 403)
        request.assert_not_called()

    def test_delete_printer_requires_login_and_calls_station(self):
        station = Mock()
        with TestClient(create_desktop_app('https://cloud.example', station),
                        base_url='http://127.0.0.1:18765') as client:
            with patch('app.desktop_web.requests.Session.request', return_value=reply(401)):
                self.assertEqual(client.delete('/desktop/printers/5').status_code, 401)
                station.delete_printer.assert_not_called()
            client.cookies.set('pc_session', 'cloud-session')
            with patch('app.desktop_web.requests.Session.request', return_value=reply(body=b'{"ok":true,"tasks":[]}')):
                self.assertEqual(client.delete('/desktop/printers/5').json(), {'ok': True})
                station.delete_printer.assert_called_once_with(5)

    def test_formal_login_has_no_preview_entry(self):
        result = self.client.get('/login')
        self.assertEqual(result.status_code, 200)
        self.assertNotIn('/preview/start', result.text)
        self.assertEqual(self.client.get('/preview/start').status_code, 404)

    def test_print_button_reaches_selected_local_printer_after_cloud_auth(self):
        station = Mock(enabled=True, ready=True, syncer=None)
        station.snapshot.return_value = []
        station.request_print.return_value = None
        client = TestClient(create_desktop_app('https://cloud.example', station),
                            base_url='http://127.0.0.1:18765')
        self.addCleanup(client.close)
        client.cookies.set('pc_session', 'cloud-session')
        payload = {'ok': True, 'user': {'username': 'teacher', 'role': 'teacher'},
                   'tasks': [{'id': 42, 'status': 'QUEUED', 'source_user': 'another'}]}
        with patch('app.desktop_web.requests.Session.request',
                   return_value=reply(body=json.dumps(payload).encode())):
            result = client.post('/desktop/tasks/42/print', json={'printer_ids': [3, 5]})
        self.assertEqual(result.status_code, 200)
        station.request_print.assert_called_once_with(payload['tasks'][0], [3, 5])


if __name__ == '__main__':
    unittest.main()
