import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import config, db, server_web
from app.webcommon import hash_password, make_session


class CompanyAccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        with patch.object(config, 'INVITE_CODE', ''), patch.object(
                config, 'BOOTSTRAP_ADMIN_PASSWORD', 'test-password-123'), patch.object(
                config, 'UPLOAD_DIR', Path(self.temp.name)), patch.object(
                server_web, 'start_retention_thread'):
            db.init(':memory:', db.SERVER_SCHEMA)
            self.client = TestClient(server_web.create_app())
        self.addCleanup(self.client.close)
        self.addCleanup(lambda: db._conn.close())
        self.super_id = db.one("SELECT id FROM users WHERE role='superadmin'")['id']
        self.client.cookies.set(server_web.SESSION_COOKIE, make_session({'uid': self.super_id}))

    def test_company_invite_creates_normal_user_without_school_binding(self):
        code = db.get_conf('invite_code')
        self.assertTrue(code)
        response = self.client.post('/register', data={
            'username': 'colleague', 'password': 'password-123', 'invite': code},
            follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        user = db.one("SELECT * FROM users WHERE username='colleague'")
        self.assertEqual(user['role'], 'user')
        self.assertEqual(self.client.get('/api/v1/bootstrap').json()['client_kind'], 'web')
        self.assertEqual(self.client.get('/api/v1/admin/overview').status_code, 403)
        self.assertEqual(self.client.post('/api/v1/schools', json={
            'school': '甲学校', 'grade': '一年级', 'classes': 1, 'copies': 1}).status_code, 200)
        self.assertEqual(self.client.get('/api/v1/account').json()['account']['username'], 'colleague')
        changed = self.client.put('/api/v1/account', json={
            'display_name': '张老师', 'old_password': 'password-123',
            'new_password': 'new-password-123'})
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(changed.json()['account']['display_name'], '张老师')
        self.client.cookies.clear()
        self.assertEqual(self.client.post('/login', data={
            'username': 'colleague', 'password': 'password-123'},
            follow_redirects=False).status_code, 200)
        self.assertEqual(self.client.post('/login', data={
            'username': 'colleague', 'password': 'new-password-123'},
            follow_redirects=False).status_code, 303)

    def test_only_superadmin_manages_users_and_invite(self):
        create = self.client.post('/api/v1/admin/users', json={
            'username': 'worker', 'password': 'password-123', 'role': 'user'})
        self.assertEqual(create.status_code, 200, create.text)
        worker = next(u for u in create.json()['users'] if u['username'] == 'worker')
        self.assertNotIn('pw_hash', worker)
        self.assertEqual(self.client.post('/api/v1/admin/users', json={
            'username': 'old-admin', 'password': 'password-123', 'role': 'admin'}).status_code, 400)
        self.assertEqual(self.client.put(f'/api/v1/admin/users/{self.super_id}', json={
            'role': 'user', 'active': True}).status_code, 400)
        self.assertEqual(self.client.put('/api/v1/admin/invite', json={
            'invite_code': 'company-new-code'}).status_code, 200)
        self.assertEqual(db.get_conf('invite_code'), 'company-new-code')
        school = self.client.post('/api/v1/schools', json={
            'school': '乙学校', 'grade': '二年级', 'classes': 2, 'copies': 30})
        self.assertEqual(school.status_code, 200, school.text)
        self.client.cookies.set(server_web.SESSION_COOKIE, make_session({'uid': worker['id']}))
        self.assertEqual(self.client.get('/api/v1/admin/overview').status_code, 403)
        self.assertEqual(self.client.put('/api/v1/admin/invite', json={
            'invite_code': 'blocked-code'}).status_code, 403)
        self.assertEqual(self.client.get('/api/v1/bootstrap').json()['presets'][0]['school'], '乙学校')

    def test_school_crud_is_shared_by_user_and_superadmin(self):
        worker_id = db.execute(
            "INSERT INTO users(username,pw_hash,role,created_at) VALUES(?,?,?,0)",
            ('worker', hash_password('password-123'), 'user'))
        for uid in (self.super_id, worker_id):
            with self.subTest(uid=uid):
                self.client.cookies.set(server_web.SESSION_COOKIE, make_session({'uid': uid}))
                payload = {'school': '共享学校', 'grade': '一年级', 'classes': 2, 'copies': 30}
                created = self.client.post('/api/v1/schools', json=payload)
                self.assertEqual(created.status_code, 200, created.text)
                sid = created.json()['presets'][0]['id']
                payload.update(grade='二年级', copies=40)
                updated = self.client.put(f'/api/v1/schools/{sid}', json=payload)
                self.assertEqual(updated.status_code, 200, updated.text)
                self.assertEqual(updated.json()['presets'][0]['copies'], 40)
                self.assertEqual(self.client.get('/api/v1/bootstrap').json()['presets'][0]['grade'], '二年级')
                # POST also supports updating an existing school/grade preset.
                payload['classes'] = 3
                self.assertEqual(self.client.post('/api/v1/schools', json=payload).status_code, 200)
                self.assertEqual(db.one('SELECT classes FROM school_presets WHERE id=?', (sid,))['classes'], 3)
                invalid = dict(payload, classes=0)
                self.assertEqual(self.client.post('/api/v1/schools', json=invalid).status_code, 400)
                self.assertEqual(self.client.put(f'/api/v1/schools/{sid}', json=invalid).status_code, 400)
                deleted = self.client.delete(f'/api/v1/schools/{sid}')
                self.assertEqual(deleted.status_code, 200, deleted.text)
                self.assertEqual(deleted.json()['presets'], [])

        for method, path, body in (
            ('GET', '/api/v1/admin/overview', None),
            ('POST', '/api/v1/admin/users', {'username': 'forbidden', 'password': 'password-123'}),
            ('PUT', f'/api/v1/admin/users/{self.super_id}', {'role': 'user'}),
            ('DELETE', f'/api/v1/admin/users/{self.super_id}', None),
            ('PUT', '/api/v1/admin/invite', {'invite_code': 'forbidden-code'}),
        ):
            with self.subTest(path=path, method=method):
                self.assertEqual(self.client.request(method, path, json=body).status_code, 403)

    def test_school_endpoints_require_active_login(self):
        sid = db.execute("INSERT INTO school_presets(school,grade,classes,copies) VALUES('学校','一年级',1,1)")
        disabled_id = db.execute(
            "INSERT INTO users(username,pw_hash,role,active,created_at) VALUES(?,?,?,0,0)",
            ('disabled', hash_password('password-123'), 'user'))
        for uid in (None, disabled_id):
            self.client.cookies.clear()
            if uid is not None:
                self.client.cookies.set(server_web.SESSION_COOKIE, make_session({'uid': uid}))
            for method, path in (('GET', '/api/v1/bootstrap'), ('POST', '/api/v1/schools'),
                                 ('PUT', f'/api/v1/schools/{sid}'), ('DELETE', f'/api/v1/schools/{sid}')):
                with self.subTest(uid=uid, method=method):
                    response = self.client.request(method, path, json={
                        'school': '篡改', 'grade': '二年级', 'classes': 2, 'copies': 2})
                    self.assertEqual(response.status_code, 401)
            self.assertEqual(db.one('SELECT school FROM school_presets WHERE id=?', (sid,))['school'], '学校')
            self.assertEqual(len(db.q('SELECT * FROM school_presets')), 1)

    def test_old_roles_are_not_converted_or_promoted(self):
        db.execute("INSERT INTO users(username,pw_hash,role,created_at) VALUES(?,?,?,0)",
                   ('old-admin', hash_password('password-123'), 'admin'))
        db.execute("INSERT INTO users(username,pw_hash,role,created_at) VALUES(?,?,?,0)",
                   ('old-teacher', hash_password('password-123'), 'teacher'))
        server_web.ensure_defaults()
        self.assertEqual({u['role'] for u in db.q('SELECT role FROM users')},
                         {'superadmin', 'admin', 'teacher'})
        with TestClient(server_web.create_app()) as legacy_client:
            legacy_client.cookies.set(server_web.SESSION_COOKIE,
                                      make_session({'uid': db.one("SELECT id FROM users WHERE username='old-admin'")['id']}))
            self.assertEqual(legacy_client.get('/api/v1/admin/overview').status_code, 403)

    def test_public_old_invite_is_rotated_on_upgrade(self):
        db.set_conf('invite_code', '101010')
        with patch.object(config, 'INVITE_CODE', '101010'):
            server_web.ensure_defaults()
        self.assertNotEqual(db.get_conf('invite_code'), '101010')


if __name__ == '__main__':
    unittest.main()
