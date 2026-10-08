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
        db.init(Path(self.temporary.name) / 'station.db', db.LOCAL_SCHEMA)
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
            "INSERT INTO local_tasks(cloud_id,title,meta_json,copies,pages,pdf_path,status,priority,created_at,job_model) "
            "VALUES(42,'练习卷','{}',1,2,'test.pdf','QUEUED',0,?,'class')", (time.time(),))
        db.execute("INSERT INTO groups(task_id,seq,label,pdf_path,pages,copies,sheets,composed_path) "
                   "VALUES(?,1,'第 1 班','test.pdf',2,1,1,'class.pdf')", (task_id,))
        self.station.request_print({'id': 42, 'classes': 1, 'copies_per_class': 1, 'copies': 1}, [printer_id])
        task = db.one('SELECT * FROM local_tasks WHERE id=?', (task_id,))
        self.assertEqual(task['status'], 'PRINTING')
        self.assertEqual(json.loads(task['printer_ids']), [printer_id])
        self.station.syncer.task_action.assert_called_once_with(42, 'claim')
        with self.assertRaisesRegex(ValueError, '不能重复打印'):
            self.station.request_print({'id': 42, 'classes': 1, 'copies_per_class': 1, 'copies': 1}, [printer_id])

    def test_waiting_print_request_cannot_be_submitted_twice(self):
        printer_id = db.execute(
            "INSERT INTO printers(name,queue_name,state) VALUES('一号机','Canon C5560','IDLE')")
        db.execute("INSERT INTO local_tasks(cloud_id,title,meta_json,copies,pages,pdf_path,status,priority,created_at) "
                   "VALUES(41,'正在打印','{}',1,1,'first.pdf','PRINTING',0,?)", (time.time(),))
        tid = db.execute("INSERT INTO local_tasks(cloud_id,title,meta_json,copies,pages,pdf_path,status,priority,created_at,job_model) "
                         "VALUES(42,'等待打印','{}',1,1,'second.pdf','QUEUED',0,?,'class')", (time.time(),))
        db.execute("INSERT INTO groups(task_id,seq,label,pdf_path,pages,copies,sheets,composed_path) "
                   "VALUES(?,1,'第 1 班','second.pdf',1,1,1,'class.pdf')", (tid,))
        self.station.request_print({'id': 42, 'classes': 1, 'copies_per_class': 1, 'copies': 1}, [printer_id])
        self.assertEqual(self.station.pending_print_ids(), [])
        self.assertEqual(db.one("SELECT status FROM local_tasks WHERE cloud_id=42")['status'], 'PRINTING')
        with self.assertRaisesRegex(ValueError, '不能重复打印'):
            self.station.request_print({'id': 42, 'classes': 1, 'copies_per_class': 1, 'copies': 1}, [printer_id])

    def test_remove_printer_keeps_history_and_can_rebind(self):
        pid = db.execute("INSERT INTO printers(name,queue_name,state) VALUES('old','queue','OFFLINE')")
        self.station.delete_printer(pid)
        self.assertEqual(self.station.snapshot(), [])
        self.assertEqual(db.one('SELECT enabled FROM printers WHERE id=?', (pid,))['enabled'], 0)
        with patch.object(self.station, 'available_printers', return_value=[{'queue': 'queue'}]):
            self.assertEqual(self.station.add_printer('new', 'queue'), pid)

    def test_remove_printer_rejects_pending_work(self):
        pid = db.execute("INSERT INTO printers(name,queue_name,state) VALUES('busy','queue','IDLE')")
        db.execute("INSERT INTO local_tasks(cloud_id,title,copies,pages,pdf_path,status,created_at,print_requested,printer_ids) "
                   "VALUES(99,'test',1,1,'test.pdf','PRINTING',0,1,?)", (json.dumps([pid]),))
        with self.assertRaisesRegex(ValueError, '未完成任务'):
            self.station.delete_printer(pid)

    def test_different_tasks_dispatch_to_different_printers(self):
        from app.executor import Executor
        backend = Mock()
        backend.submit.return_value = 'test-job'
        for i in [1, 2]:
            pid = db.execute("INSERT INTO printers(name,queue_name,state) VALUES(?,?,'IDLE')", (str(i), str(i)))
            tid = db.execute("INSERT INTO local_tasks(cloud_id,title,copies,pages,pdf_path,status,created_at,print_requested,printer_ids,job_model) "
                             "VALUES(?,'test',1,1,'test.pdf','QUEUED',0,1,?,'class')", (i, json.dumps([pid])))
            db.execute("INSERT INTO groups(task_id,seq,label,pdf_path,pages,copies,sheets,composed_path) "
                       "VALUES(?,1,'g','test.pdf',1,1,1,?)", (tid, str(i)+'.pdf'))
            db.execute("UPDATE groups SET printer_id=? WHERE task_id=?", (pid, tid))
        executor = Executor(backend=backend)
        executor._assign()
        self.assertEqual(backend.submit.call_count, 2)
        self.assertEqual(db.one("SELECT COUNT(*) n FROM groups WHERE status='PRINTING'")['n'], 2)
        executor._assign()
        self.assertEqual(backend.submit.call_count, 2)

    def test_same_task_classes_dispatch_only_to_their_preassigned_printers(self):
        from app.executor import Executor
        backend = Mock()
        backend.submit.side_effect = ['job-a', 'job-b']
        pids = [db.execute("INSERT INTO printers(name,queue_name,state) VALUES(?,?, 'IDLE')", (f'机{i}', f'q{i}')) for i in (1, 2)]
        tid = db.execute("INSERT INTO local_tasks(cloud_id,title,copies,pages,pdf_path,status,created_at,print_requested,printer_ids,job_model) "
                         "VALUES(80,'class',2,1,'test.pdf','PRINTING',0,1,?,'class')", (json.dumps(pids),))
        for seq, pid in enumerate(pids, 1):
            db.execute("INSERT INTO groups(task_id,seq,label,pdf_path,pages,copies,sheets,composed_path,printer_id) "
                       "VALUES(?,?,?,'test.pdf',1,1,1,?,?)", (tid, seq, f'第 {seq} 班', f'{seq}.pdf', pid))
        executor = Executor(backend=backend)
        executor._assign()
        self.assertEqual([call.args[0]['id'] for call in backend.submit.call_args_list], pids)
        self.assertEqual([(r['printer_id'], r['status']) for r in db.q("SELECT printer_id,status FROM groups ORDER BY seq")],
                         [(pids[0], 'PRINTING'), (pids[1], 'PRINTING')])

    def test_class_submit_failure_blocks_without_moving_class(self):
        from app.executor import Executor
        backend = Mock()
        backend.submit.side_effect = RuntimeError('offline')
        pid = db.execute("INSERT INTO printers(name,queue_name,state) VALUES('original','q','IDLE')")
        tid = db.execute("INSERT INTO local_tasks(cloud_id,title,copies,pages,pdf_path,status,created_at,print_requested,printer_ids,job_model) "
                         "VALUES(81,'class',1,1,'test.pdf','PRINTING',0,1,?,'class')", (json.dumps([pid]),))
        gid = db.execute("INSERT INTO groups(task_id,seq,label,pdf_path,pages,copies,sheets,composed_path,printer_id) "
                         "VALUES(?,1,'第 1 班','test.pdf',1,1,1,'class.pdf',?)", (tid, pid))
        Executor(backend=backend)._assign()
        group = db.one("SELECT status,printer_id FROM groups WHERE id=?", (gid,))
        self.assertEqual((group['status'], group['printer_id']), ('PAUSED', pid))

    def test_class_recovery_and_uncertain_completion_require_manual_actions(self):
        from app.executor import Executor
        backend = Mock()
        backend.poll.return_value = {'status': 'idle', 'page_counter': -1}
        backend.control_group.return_value = 1
        self.station.executor = Executor(backend=backend)
        pid = db.execute("INSERT INTO printers(name,queue_name,state) VALUES('original','q','OFFLINE')")
        tid = db.execute("INSERT INTO local_tasks(cloud_id,title,meta_json,copies,pages,pdf_path,status,created_at,print_requested,printer_ids,job_model) "
                         "VALUES(82,'class','{\"classes\":2,\"copies_per_class\":1}',2,1,'test.pdf','PRINTING',0,1,?,'class')", (json.dumps([pid]),))
        waiting = db.execute("INSERT INTO groups(task_id,seq,label,pdf_path,pages,copies,sheets,composed_path,status,printer_id) "
                             "VALUES(?,1,'第 1 班','test.pdf',1,1,1,'waiting.pdf','PAUSED',?)", (tid, pid))
        uncertain = db.execute("INSERT INTO groups(task_id,seq,label,pdf_path,pages,copies,sheets,composed_path,status,printer_id,started_at) "
                               "VALUES(?,2,'第 2 班','test.pdf',1,1,1,'uncertain.pdf','PAUSED',?,1)", (tid, pid))
        self.station.resolve_class_group(82, waiting, 'recover')
        self.assertEqual(db.one("SELECT status,printer_id FROM groups WHERE id=?", (waiting,))['status'], 'QUEUED')
        with self.assertRaisesRegex(ValueError, '确认 Windows 队列'):
            self.station.resolve_class_group(82, uncertain, 'retry')
        self.station.resolve_class_group(82, uncertain, 'retry', True)
        self.assertEqual(db.one("SELECT status,printer_id,started_at FROM groups WHERE id=?", (uncertain,))['status'], 'QUEUED')
        backend.control_group.assert_called_once_with(dict(db.one('SELECT * FROM printers WHERE id=?', (pid,))), 'uncertain.pdf', 'cancel')

    def test_stuck_task_does_not_block_other_printer(self):
        from app.executor import Executor
        backend = Mock()
        for i, state in [(1, 'OFFLINE'), (2, 'IDLE')]:
            pid = db.execute("INSERT INTO printers(name,queue_name,state) VALUES(?,?,?)", (str(i), str(i), state))
            tid = db.execute("INSERT INTO local_tasks(cloud_id,title,copies,pages,pdf_path,status,created_at,print_requested,printer_ids,job_model) "
                             "VALUES(?,'test',1,1,'test.pdf','PRINTING',0,1,?,'class')", (i, json.dumps([pid])))
            db.execute("INSERT INTO groups(task_id,seq,label,pdf_path,pages,copies,sheets,composed_path) "
                       "VALUES(?,1,'g','test.pdf',1,1,1,'ready.pdf')", (tid,))
            db.execute("UPDATE groups SET printer_id=? WHERE task_id=?", (pid, tid))
        Executor(backend=backend)._assign()
        backend.submit.assert_called_once()
        self.assertEqual(backend.submit.call_args.args[0]['id'], 2)

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

    def prepare_control_task(self):
        from app.executor import Executor
        backend = Mock()
        backend.control_group.return_value = 1
        self.station.executor = Executor(backend=backend)
        pid = db.execute("INSERT INTO printers(name,queue_name,state) VALUES('control','queue','PRINTING')")
        tid = db.execute("INSERT INTO local_tasks(cloud_id,title,copies,pages,pdf_path,status,created_at,print_requested,printer_ids) "
                         "VALUES(50,'task',1,1,'test.pdf','PRINTING',0,1,?)", (json.dumps([pid]),))
        gid = db.execute("INSERT INTO groups(task_id,seq,label,pdf_path,pages,copies,sheets,composed_path,status,printer_id) "
                         "VALUES(?,1,'g','test.pdf',1,1,1,'composed.pdf','PRINTING',?)", (tid,pid))
        db.execute('UPDATE printers SET current_group=? WHERE id=?', (gid,pid))
        return pid, tid, gid, backend

    def test_pause_prevents_completion_and_resume_releases(self):
        pid,tid,gid,backend = self.prepare_control_task()
        self.station.control_task(50,'pause')
        self.assertEqual(db.one('SELECT paused FROM local_tasks WHERE id=?',(tid,))['paused'],1)
        self.station.executor._step_printer(dict(db.one('SELECT * FROM printers WHERE id=?',(pid,))), {'status':'idle'})
        self.assertEqual(db.one('SELECT status FROM groups WHERE id=?',(gid,))['status'],'PRINTING')
        self.assertTrue(self.station.fleet_tasks()[0]['paused'])
        self.station.control_task(50,'resume')
        self.assertEqual(db.one('SELECT paused FROM local_tasks WHERE id=?',(tid,))['paused'],0)
        self.assertEqual([c.args[2] for c in backend.control_group.call_args_list],['pause','resume'])

    def test_cancel_only_target_task_and_releases_printer(self):
        pid,tid,gid,backend = self.prepare_control_task()
        self.station.control_task(50,'cancel')
        self.assertEqual(db.one('SELECT status FROM local_tasks WHERE id=?',(tid,))['status'],'CANCELED')
        self.assertIsNone(db.one('SELECT current_group FROM printers WHERE id=?',(pid,))['current_group'])
        self.assertEqual(self.station.fleet_tasks(),[])
        backend.control_group.assert_called_once()

    def test_driver_error_stops_dispatch_without_claiming_cancel_success(self):
        pid,tid,gid,backend = self.prepare_control_task()
        backend.control_group.side_effect = RuntimeError('access denied')
        with self.assertRaisesRegex(RuntimeError,'控制失败'):
            self.station.control_task(50,'cancel')
        task = db.one('SELECT * FROM local_tasks WHERE id=?',(tid,))
        self.assertEqual(task['status'],'PRINTING')
        self.assertEqual(task['paused'],1)
        self.assertEqual(db.one('SELECT current_group FROM printers WHERE id=?',(pid,))['current_group'],gid)

    def test_waiting_order_can_move_with_equal_timestamps(self):
        from app.executor import Executor
        self.station.executor = Executor(backend=Mock())
        for cid in [51,52,53]:
            db.execute("INSERT INTO local_tasks(cloud_id,title,copies,pages,pdf_path,status,created_at,print_requested,printer_ids) "
                       "VALUES(?,'test',1,1,'test.pdf','PRINTING',0,1,'[1]')",(cid,))
        self.station.control_task(52,'up',1)
        self.assertEqual([t['id'] for t in self.station.fleet_tasks()],[52,51,53])
        self.station.control_task(52,'down',1)
        self.assertEqual([t['id'] for t in self.station.fleet_tasks()],[51,52,53])


if __name__ == '__main__':
    unittest.main()
