import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import db
from app.class_plan import expand_special_copies


class ClassPlanMigrationTests(unittest.TestCase):
    def test_special_rules_support_ranges_and_scattered_classes(self):
        self.assertEqual(sum(expand_special_copies(20, 50,
            [{'classes': '6、10、15', 'copies': 40}])), 970)
        self.assertEqual(sum(expand_special_copies(20, 50,
            [{'classes': '1-10', 'copies': 40}])), 900)
        with self.assertRaisesRegex(ValueError, '重复'):
            expand_special_copies(20, 50, [{'classes': '1-10、6', 'copies': 40}])
        with self.assertRaisesRegex(ValueError, '超出'):
            expand_special_copies(20, 50, [{'classes': '21', 'copies': 40}])

    def test_existing_sqlite_database_requires_explicit_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'server.db'
            old_schema = '\n'.join(line for line in db.SERVER_SCHEMA.splitlines()
                                   if not any(name in line for name in (
                                       'class_copies_json', 'cover_enabled', 'overrides_json')))
            connection = sqlite3.connect(path)
            connection.executescript(old_schema)
            connection.execute("INSERT INTO school_presets(school,grade,classes,copies) "
                               "VALUES('旧学校','八年级',20,50)")
            connection.execute("INSERT INTO tasks(title,copies,pages,sheets,source_user,pdf_path,"
                               "pdf_name,created_at) VALUES('旧任务',1,1,1,'user','a.pdf','a.pdf',0)")
            connection.commit()
            connection.close()
            with self.assertRaisesRegex(RuntimeError, '缺少字段'):
                db.init(path, db.SERVER_SCHEMA)
            connection = sqlite3.connect(path)
            migration = (Path(__file__).resolve().parents[1] / 'deploy' / 'migrations' /
                         '20261015_class_copies_sqlite.sql').read_text(encoding='utf-8')
            connection.executescript(migration)
            connection.close()
            db.init(path, db.SERVER_SCHEMA)
            try:
                preset = db.one('SELECT * FROM school_presets')
                task = db.one('SELECT * FROM tasks')
                self.assertEqual(json.loads(preset['overrides_json']), [])
                self.assertEqual(json.loads(task['class_copies_json']), [])
                self.assertEqual(task['cover_enabled'], 0)
                self.assertEqual(preset['school'], '旧学校')
                self.assertEqual(task['title'], '旧任务')
            finally:
                db._conn.close()


if __name__ == '__main__':
    unittest.main()
