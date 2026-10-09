import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from pypdf import PdfReader, PdfWriter

from app import config, db, importer
from app.compose import compose_group_pdf
from app.station_runtime import StationRuntime


class ClassJobTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        db.init(self.root / 'local.db', db.LOCAL_SCHEMA)
        self.addCleanup(db._conn.close)
        self.composed = self.root / 'composed'
        self.composed.mkdir()
        self.composed_patch = patch.object(config, 'COMPOSED_DIR', self.composed)
        self.composed_patch.start()
        self.addCleanup(self.composed_patch.stop)
        self.pdf = self.root / 'source.pdf'
        writer = PdfWriter()
        for _ in range(3):
            writer.add_blank_page(width=595, height=842)
        with self.pdf.open('wb') as stream:
            writer.write(stream)

    def test_duplex_composition_has_no_cover_and_pads_each_odd_copy(self):
        output = self.composed / 'class.pdf'
        pages, sheets = compose_group_pdf(self.pdf, output, '数学', '第 1 班', 2)
        expected_pages = 8 if config.DUPLEX else 6
        expected_sheets = 8 if not config.DUPLEX else 4
        self.assertEqual((pages, sheets), (3, expected_sheets))
        self.assertEqual(len(PdfReader(str(output)).pages), expected_pages)

    def test_twenty_classes_create_twenty_jobs_and_stable_7_7_6_allocation(self):
        task_id = importer.create_local_task(
            900, '数学练习', self.pdf, 800, meta={
                'school': '实验小学', 'grade': '六年级', 'classes': 20,
                'copies_per_class': 40,
            })
        groups = db.q('SELECT * FROM groups WHERE task_id=? ORDER BY seq', (task_id,))
        self.assertEqual(len(groups), 20)
        self.assertEqual([group['copies'] for group in groups], [40] * 20)
        self.assertEqual([group['label'] for group in groups], [f'第 {i} 班' for i in range(1, 21)])
        self.assertTrue(all(len(PdfReader(group['composed_path']).pages) == 160 for group in groups))

        printer_ids = [db.execute(
            "INSERT INTO printers(name,queue_name,state) VALUES(?,?, 'IDLE')", (name, f'q-{name}'))
            for name in ('C', 'A', 'B')]
        station = StationRuntime({'cloud_url': 'https://example.invalid'}, self.root, lambda _: None)
        station.executor = object()
        station.syncer = Mock(last_error='', cookie='pc_session=test')
        station.request_print({'id': 900, 'classes': 20, 'copies_per_class': 40, 'copies': 800}, printer_ids)
        assigned = db.q('SELECT seq,printer_id FROM groups WHERE task_id=? ORDER BY seq', (task_id,))
        counts = {pid: sum(row['printer_id'] == pid for row in assigned) for pid in printer_ids}
        self.assertEqual(counts, {printer_ids[1]: 7, printer_ids[2]: 7, printer_ids[0]: 6})
        self.assertEqual([row['printer_id'] for row in assigned[:7]], [printer_ids[1]] * 7)
        self.assertEqual([row['printer_id'] for row in assigned[7:14]], [printer_ids[2]] * 7)
        self.assertEqual([row['printer_id'] for row in assigned[14:]], [printer_ids[0]] * 6)

    def test_inconsistent_class_metadata_is_rejected(self):
        with self.assertRaisesRegex(ValueError, '不一致'):
            importer.create_local_task(901, 'bad', self.pdf, 10,
                                       meta={'classes': 3, 'copies_per_class': 4},
                                       )

    def test_last_completed_class_finishes_task_without_pickup_confirmation(self):
        tid = importer.create_local_task(903, '数学', self.pdf, 2,
                                         meta={'classes': 2, 'copies_per_class': 1})
        db.execute("UPDATE local_tasks SET status='PRINTING',print_requested=1 WHERE id=?", (tid,))
        groups = db.q('SELECT id FROM groups WHERE task_id=? ORDER BY seq', (tid,))
        db.execute("UPDATE groups SET status='DONE' WHERE id=?", (groups[0]['id'],))
        importer.on_group_done(groups[0]['id'])
        self.assertEqual(db.one('SELECT status FROM local_tasks WHERE id=?', (tid,))['status'], 'PRINTING')
        db.execute("UPDATE groups SET status='DONE' WHERE id=?", (groups[1]['id'],))
        importer.on_group_done(groups[1]['id'])
        importer.on_group_done(groups[1]['id'])
        task = db.one('SELECT status,done_at FROM local_tasks WHERE id=?', (tid,))
        self.assertEqual(task['status'], 'DONE')
        self.assertIsNotNone(task['done_at'])
        self.assertEqual(db.one("SELECT COUNT(*) n FROM events WHERE task_id=? AND type='TASK_DONE'", (tid,))['n'], 1)

    def test_existing_finished_pickup_task_is_closed_without_reprinting(self):
        tid = importer.create_local_task(904, '旧待收纸', self.pdf, 1,
                                         meta={'classes': 1, 'copies_per_class': 1})
        db.execute("UPDATE local_tasks SET status='AWAITING_PICKUP' WHERE id=?", (tid,))
        db.execute("UPDATE groups SET status='DONE' WHERE task_id=?", (tid,))
        importer.finalize_completed_pickup_tasks()
        task = db.one('SELECT status,done_at FROM local_tasks WHERE id=?', (tid,))
        self.assertEqual(task['status'], 'DONE')
        self.assertIsNotNone(task['done_at'])

    def test_unstarted_legacy_task_is_rejected_without_mutation(self):
        tid = db.execute("INSERT INTO local_tasks(cloud_id,title,meta_json,copies,pages,pdf_path,status,created_at,job_model) "
                         "VALUES(902,'old','{}',6,3,?,'QUEUED',0,'legacy')", (str(self.pdf),))
        printer = db.execute("INSERT INTO printers(name,queue_name,state) VALUES('A','q','IDLE')")
        station = StationRuntime({'cloud_url': 'https://example.invalid'}, self.root, lambda _: None)
        station.executor = object()
        station.syncer = Mock(last_error='', cookie='pc_session=test')
        with self.assertRaisesRegex(ValueError, '旧作业'):
            station.request_print({'id': 902, 'classes': 3, 'copies': 6,
                                   'copies_per_class': 2}, [printer])
        task = db.one('SELECT job_model,status FROM local_tasks WHERE id=?', (tid,))
        self.assertEqual((task['job_model'], task['status']), ('legacy', 'QUEUED'))
        self.assertEqual(db.one('SELECT COUNT(*) n FROM groups WHERE task_id=?', (tid,))['n'], 0)
        from app.executor import Executor
        backend = Mock()
        Executor(backend=backend)._assign()
        backend.submit.assert_not_called()


if __name__ == '__main__':
    unittest.main()
