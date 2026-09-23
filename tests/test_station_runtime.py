import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from app import config, db
from app.cloudclient import CloudSync
from app.station_runtime import StationRuntime


class StationRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.previous_manual = config.MANUAL_PRINT
        config.MANUAL_PRINT = True
        self.addCleanup(setattr, config, 'MANUAL_PRINT', self.previous_manual)
        db.init(Path(self.temporary.name) / 'station.db', db.AGENT_SCHEMA)
        self.addCleanup(db._conn.close)
        self.station = StationRuntime(
            {'cloud_url': 'https://example.invalid'},
            Path(self.temporary.name), lambda _: None)
        self.station.executor = object()
        self.station.syncer = Mock(last_error='', cookie='pc_session=test')

    def test_only_installed_windows_queue_can_be_registered(self):
        with patch.object(self.station, 'available_printers',
                          return_value=[{'queue': 'Canon C5560', 'driver': 'Canon', 'port': 'IP'}]):
            with self.assertRaisesRegex(ValueError, 'Windows 中找不到'):
                self.station.add_printer('一号机', 'unknown')
            printer_id = self.station.add_printer('一号机', 'Canon C5560', '192.168.1.20')
        printer = db.one('SELECT * FROM printers WHERE id=?', (printer_id,))
        self.assertEqual(printer['queue_name'], 'Canon C5560')
        self.assertEqual(printer['state'], 'OFFLINE')

    def test_print_request_starts_selected_task_once(self):
        printer_id = db.execute(
            "INSERT INTO printers(name,queue_name,state) VALUES('一号机','Canon C5560','IDLE')")
        task_id = db.execute(
            "INSERT INTO local_tasks(cloud_id,title,meta_json,copies,pages,pdf_path,status,priority,created_at) "
            "VALUES(42,'练习卷','{}',1,2,'test.pdf','QUEUED',0,?)", (time.time(),))
        self.station.request_print({'id': 42}, [printer_id])
        task = db.one('SELECT * FROM local_tasks WHERE id=?', (task_id,))
        self.assertEqual(task['status'], 'PRINTING')
        self.assertEqual(json.loads(task['printer_ids']), [printer_id])
        self.station.syncer.task_action.assert_called_once_with(42, 'claim')
        with self.assertRaisesRegex(ValueError, '不能重复打印'):
            self.station.request_print({'id': 42}, [printer_id])

    def test_waiting_print_request_cannot_be_submitted_twice(self):
        printer_id = db.execute(
            "INSERT INTO printers(name,queue_name,state) VALUES('一号机','Canon C5560','IDLE')")
        db.execute("INSERT INTO local_tasks(cloud_id,title,meta_json,copies,pages,pdf_path,status,priority,created_at) "
                   "VALUES(41,'正在打印','{}',1,1,'first.pdf','PRINTING',0,?)", (time.time(),))
        db.execute("INSERT INTO local_tasks(cloud_id,title,meta_json,copies,pages,pdf_path,status,priority,created_at) "
                   "VALUES(42,'等待打印','{}',1,1,'second.pdf','QUEUED',0,?)", (time.time(),))
        self.station.request_print({'id': 42}, [printer_id])
        self.assertEqual(self.station.pending_print_ids(), [42])
        with self.assertRaisesRegex(ValueError, '已加入本机打印队列'):
            self.station.request_print({'id': 42}, [printer_id])

    def test_login_caches_every_queued_pdf_without_starting_print(self):
        cache = Path(self.temporary.name) / 'uploads'
        sync = CloudSync('https://example.invalid', cookie='pc_session=test',
                         client_id=self.station.settings['client_id'])
        sync_reply = Mock()
        sync_reply.json.return_value = {
            'ok': True, 'new_tasks': [
                {'id': 42, 'pdf_name': 'first.pdf'},
                {'id': 43, 'pdf_name': 'second.pdf'}],
            'canceled': [], 'priorities': {}}
        file_reply = Mock(is_redirect=False, content=b'%PDF-cached')
        with patch.object(config, 'UPLOAD_DIR', cache), patch.object(
                sync, '_request', side_effect=[sync_reply, file_reply, file_reply]):
            sync.tick()
        self.assertEqual(sync.last_error, '')
        self.assertEqual(len(list((cache / 'queue').glob('*.pdf'))), 2)
        self.assertEqual(db.one('SELECT COUNT(*) n FROM local_tasks')['n'], 0)


if __name__ == '__main__':
    unittest.main()
