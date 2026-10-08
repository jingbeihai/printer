import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import db


class SchemaCompatibilityTests(unittest.TestCase):
    def test_current_server_database_reopens_without_changing_records(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'current-cloud.db'
            db.init(path, db.SERVER_SCHEMA)
            uid = db.execute("INSERT INTO users(username,pw_hash,role,created_at) "
                             "VALUES('keep','hash','user',1)")
            tid = db.execute("INSERT INTO tasks(title,copies,pages,sheets,source_user,pdf_path,"
                             "pdf_name,created_at) VALUES('keep task',2,3,3,'keep','keep.pdf',"
                             "'keep.pdf',1)")
            db._conn.close()

            db.init(path, db.SERVER_SCHEMA)
            self.assertEqual(dict(db.one('SELECT username,role FROM users WHERE id=?', (uid,))),
                             {'username': 'keep', 'role': 'user'})
            self.assertEqual(dict(db.one('SELECT title,copies,pdf_path FROM tasks WHERE id=?', (tid,))),
                             {'title': 'keep task', 'copies': 2, 'pdf_path': 'keep.pdf'})
            db._conn.close()

    def test_unsupported_existing_local_database_is_rejected_without_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'old-local.db'
            connection = sqlite3.connect(path)
            connection.execute('CREATE TABLE printers(id INTEGER PRIMARY KEY, name TEXT)')
            connection.execute("INSERT INTO printers(name) VALUES('Existing printer')")
            connection.commit()
            connection.close()

            with self.assertRaisesRegex(RuntimeError, '数据库结构不受支持'):
                db.init(path, db.LOCAL_SCHEMA)

            connection = sqlite3.connect(path)
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            columns = {row[1] for row in connection.execute('PRAGMA table_info(printers)')}
            printer = connection.execute('SELECT name FROM printers').fetchone()
            connection.close()
            self.assertEqual(tables, {'printers'})
            self.assertEqual(columns, {'id', 'name'})
            self.assertEqual(printer, ('Existing printer',))

    def test_fresh_local_database_has_current_runtime_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            db.init(Path(directory) / 'new-local.db', db.LOCAL_SCHEMA)
            self.addCleanup(db._conn.close)
            printer_columns = {row['name'] for row in db.q('PRAGMA table_info(printers)')}
            task_columns = {row['name'] for row in db.q('PRAGMA table_info(local_tasks)')}
            self.assertTrue({'continuous_print_seconds', 'max_continuous_minutes',
                             'min_rest_minutes', 'rest_until'} <= printer_columns)
            self.assertTrue({'paused', 'print_requested', 'print_requested_at',
                             'printer_ids', 'job_model'} <= task_columns)
            db._conn.close()


if __name__ == '__main__':
    unittest.main()
