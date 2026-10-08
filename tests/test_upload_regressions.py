import io
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from pypdf import PdfWriter
from app import config, db, server_web
from app.webcommon import make_session


class UploadRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.uploads = Path(self.temp.name)
        for name, value in [('UPLOAD_DIR', self.uploads), ('STORAGE_BACKEND', 'local'),
                            ('BOOTSTRAP_ADMIN_PASSWORD', 'test-password-123')]:
            mock = patch.object(config, name, value)
            mock.start()
            self.addCleanup(mock.stop)
        db.init(':memory:', db.SERVER_SCHEMA)
        self.addCleanup(lambda: db._conn.close())
        with patch.object(server_web, 'start_retention_thread'):
            self.client = TestClient(server_web.create_app())
        self.addCleanup(self.client.close)
        self.client.cookies.set(server_web.SESSION_COOKIE, make_session({'uid': 1}))
        db.execute("INSERT INTO school_presets(school,grade,classes,copies) VALUES('School','Grade',1,2)")
        self.meta = dict(school='School', grade='Grade', classes=1, copies=2, pages=1,
                         filename='sample.pdf', size=100)

    def test_local_upload_and_download(self):
        reply = self.client.post('/api/v1/uploads/presign', json=self.meta)
        self.assertEqual(reply.json(), {'ok': True, 'storage_backend': 'local'})
        self.assertEqual(db.one('SELECT COUNT(*) AS n FROM tasks')['n'], 0)
        pdf = io.BytesIO()
        writer = PdfWriter()
        writer.add_blank_page(width=72, height=72)
        writer.write(pdf)
        reply = self.client.post('/api/v1/tasks', data=self.meta,
                                 files={'file': ('sample.pdf', pdf.getvalue(), 'application/pdf')})
        self.assertEqual(reply.status_code, 200, reply.text)
        task = db.one('SELECT * FROM tasks WHERE id=?', (reply.json()['task_id'],))
        self.assertEqual(task['status'], 'QUEUED')
        self.assertEqual(Path(task['pdf_path']).read_bytes(), pdf.getvalue())
        download = self.client.get(f"/api/agent/file/{task['id']}",
                                  headers={'X-Client-ID': str(uuid.uuid4())})
        self.assertEqual(download.content, pdf.getvalue())

    def test_download_encodes_unicode_and_special_filename_characters(self):
        from urllib.parse import unquote
        pdf_path = self.uploads / 'download.pdf'
        pdf_path.write_bytes(b'pdf-content')
        for filename in ['数学练习卷.pdf', '考试 "A" 100% #1.pdf', 'line\r\nbreak.pdf']:
            with self.subTest(filename=filename):
                tid = db.execute(
                    "INSERT INTO tasks(title,copies,pages,sheets,source_user,pdf_path,pdf_name,created_at) "
                    "VALUES('test',1,1,1,'admin',?,?,?)", (str(pdf_path), filename, time.time()))
                reply = self.client.get(f'/api/agent/file/{tid}',
                                        headers={'X-Client-ID': str(uuid.uuid4())})
                self.assertEqual(reply.status_code, 200)
                self.assertEqual(reply.content, b'pdf-content')
                header = reply.headers['content-disposition']
                self.assertTrue(header.isascii())
                self.assertNotIn('\r', header)
                self.assertNotIn('\n', header)
                self.assertEqual(unquote(header.split("UTF-8''", 1)[1]), filename)

    def test_cos_upload_still_requires_completion(self):
        with patch.object(config, 'STORAGE_BACKEND', 'cos'), patch.object(
                server_web.storage, 'prepare_upload', return_value=('cos://uploads/test.pdf', 'https://example.invalid/upload')):
            reply = self.client.post('/api/v1/uploads/presign', json=self.meta)
        self.assertEqual(reply.status_code, 200, reply.text)
        tid = reply.json()['task_id']
        self.assertEqual(db.one('SELECT status FROM tasks WHERE id=?', (tid,))['status'], 'UPLOADING')
        with patch.object(server_web.storage, 'exists', return_value=True):
            self.assertEqual(self.client.post(f'/api/v1/uploads/{tid}/complete').status_code, 200)
        self.assertEqual(db.one('SELECT status FROM tasks WHERE id=?', (tid,))['status'], 'QUEUED')

    def test_cos_proxy_reuses_object_and_requires_completion(self):
        with patch.object(config, 'STORAGE_BACKEND', 'cos'), patch.object(
                server_web.storage, 'prepare_upload', return_value=('cos://uploads/original.pdf', 'https://example.invalid/upload')):
            tid = self.client.post('/api/v1/uploads/presign', json=self.meta).json()['task_id']
        def receive(location, stream):
            self.assertEqual(location, 'cos://uploads/original.pdf')
            self.assertEqual(stream.read(), b'pdf-data')
        with patch.object(server_web.storage, 'upload_stream', side_effect=receive) as upload:
            reply = self.client.post(f'/api/v1/uploads/{tid}/file', files={'file': ('sample.pdf', b'pdf-data')})
            self.assertEqual(reply.status_code, 200, reply.text)
            upload.assert_called_once()
        self.assertEqual(db.one('SELECT COUNT(*) AS n FROM tasks')['n'], 1)
        self.assertEqual(db.one('SELECT status FROM tasks WHERE id=?', (tid,))['status'], 'UPLOADING')
        with patch.object(server_web.storage, 'exists', return_value=True):
            self.assertEqual(self.client.post(f'/api/v1/uploads/{tid}/complete').status_code, 200)
        self.assertEqual(self.client.post(f'/api/v1/uploads/{tid}/file').status_code, 409)

    def test_cos_proxy_rejects_other_owner_and_reports_storage_failure(self):
        tid = db.execute("INSERT INTO tasks(title,copies,pages,sheets,source_user,status,pdf_path,pdf_name,created_at) "
                         "VALUES('test',1,1,1,'other','UPLOADING','cos://uploads/test.pdf','test.pdf',?)", (time.time(),))
        with patch.object(server_web.storage, 'upload_stream', side_effect=RuntimeError('secret diagnostic')) as upload:
            self.assertEqual(self.client.post(f'/api/v1/uploads/{tid}/file').status_code, 404)
            upload.assert_not_called()
            db.execute("UPDATE tasks SET source_user='admin' WHERE id=?", (tid,))
            reply = self.client.post(f'/api/v1/uploads/{tid}/file', files={'file': ('test.pdf', b'pdf')})
            self.assertEqual(reply.status_code, 502)
            self.assertNotIn('secret diagnostic', reply.text)
        self.assertEqual(db.one('SELECT status FROM tasks WHERE id=?', (tid,))['status'], 'UPLOADING')

    def test_logged_in_clients_can_cache_queue_but_only_one_can_claim(self):
        pdf_path = self.uploads / 'queued.pdf'
        pdf_path.write_bytes(b'queued-pdf')
        tid = db.execute(
            "INSERT INTO tasks(title,copies,pages,sheets,source_user,pdf_path,pdf_name,created_at) "
            "VALUES('练习卷',1,1,1,'other-user',?,'queued.pdf',?)",
            (str(pdf_path), time.time()))
        client_a, client_b = str(uuid.uuid4()), str(uuid.uuid4())
        self.assertEqual(self.client.get(f'/api/agent/file/{tid}', headers={
            'X-Client-ID': client_a}).content, b'queued-pdf')
        first = self.client.post(f'/api/v1/tasks/{tid}/claim', json={'client_id': client_a})
        second = self.client.post(f'/api/v1/tasks/{tid}/claim', json={'client_id': client_b})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 409)
        visible = next(task for task in self.client.get('/api/v1/bootstrap').json()['tasks']
                       if task['id'] == tid)
        self.assertTrue(visible['claimed'])
        self.assertNotIn('claimed_client_id', visible)
        waiting = self.client.post('/api/agent/sync', headers={'X-Client-ID': client_b}, json={
            'client_id': client_b, 'task_states': {str(tid): 'PRINTING'}})
        self.assertEqual(waiting.status_code, 200)
        self.assertEqual(db.one('SELECT status FROM tasks WHERE id=?', (tid,))['status'], 'QUEUED')
        claimed = self.client.post('/api/agent/sync', headers={'X-Client-ID': client_a}, json={
            'client_id': client_a, 'task_states': {str(tid): 'PRINTING'}})
        self.assertEqual(claimed.status_code, 200)
        self.assertEqual(db.one('SELECT status FROM tasks WHERE id=?', (tid,))['status'], 'PRINTING')
        self.assertEqual(self.client.get(f'/api/agent/file/{tid}', headers={
            'X-Client-ID': client_a}).status_code, 403)

    def test_agent_token_alone_is_rejected_and_retired_pages_redirect(self):
        anonymous = TestClient(server_web.create_app())
        self.addCleanup(anonymous.close)
        reply = anonymous.post('/api/agent/sync', headers={'X-Agent-Token': 'old-token'}, json={})
        self.assertEqual(reply.status_code, 401)
        for path in ('/submit', '/queue', '/admin', '/dashboard'):
            with self.subTest(path=path):
                response = anonymous.get(path, follow_redirects=False)
                self.assertEqual(response.status_code, 307)
                self.assertEqual(response.headers['location'], '/desk/')
        self.assertEqual(anonymous.post('/submit').status_code, 405)
        self.assertEqual(anonymous.get('/dashboard.csv').status_code, 404)

    def test_retention_retries_failures_and_keeps_recent_objects(self):
        now = time.time()
        for location, age in [('cos://old', 40), ('cos://failed', 40), ('cos://recent', 1)]:
            db.execute("INSERT INTO tasks(title,copies,pages,sheets,source_user,pdf_path,pdf_name,created_at) VALUES('test',1,1,1,'admin',?,'test.pdf',?)",
                       (location, now - age * 86400))
        old = self.uploads / '2000-01-01'
        old.mkdir()
        (old / 'test.pdf').write_bytes(b'test')
        def remove(location):
            if location == 'cos://failed':
                raise RuntimeError('temporary storage error')
        with patch.object(server_web.storage, 'remove', side_effect=remove) as mocked:
            server_web.cleanup_old_uploads()
        self.assertEqual([c.args[0] for c in mocked.call_args_list], ['cos://old', 'cos://failed'])
        self.assertEqual([r['pdf_path'] for r in db.q('SELECT pdf_path FROM tasks ORDER BY id')],
                         ['', 'cos://failed', 'cos://recent'])
        self.assertFalse(old.exists())
        with patch.object(server_web.storage, 'remove') as mocked:
            server_web.cleanup_old_uploads()
        mocked.assert_called_once_with('cos://failed')
        self.assertEqual(db.one('SELECT COUNT(*) AS n FROM tasks')['n'], 3)

    def test_retention_queries_parse_with_postgres_driver(self):
        from psycopg._queries import PostgresQuery
        from psycopg.adapt import Transformer

        sqlite_query = db.q
        def postgres_checked_query(sql, args=()):
            # Exercise psycopg's real parameter parser, which rejected the old
            # literal LIKE 'cos://%' before any network request was made.
            query = PostgresQuery(Transformer())
            query.convert(db._pg_sql(sql), args)
            return sqlite_query(sql, args)

        with patch.object(db, 'q', side_effect=postgres_checked_query):
            server_web.cleanup_old_uploads()


if __name__ == '__main__':
    unittest.main()
