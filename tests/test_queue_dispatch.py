import json
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from app import db
from app.backends import SubmissionNotStarted, WindowsBackend
from app.executor import Executor
from app.station_runtime import StationRuntime


class QueueDispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        db.init(Path(self.tmp.name) / 'local.db', db.LOCAL_SCHEMA)
        self.addCleanup(db._conn.close)
        self.backend = Mock()
        self.backend.submit.return_value = 'submitted'
        self.backend.control_group.return_value = 1
        self.executor = Executor(self.backend)
        self.pid = db.execute("INSERT INTO printers(name,queue_name,state) VALUES('A','A','IDLE')")
        self.tid = self.make_task(100, 5)

    def make_task(self, cloud_id, count):
        tid = db.execute("INSERT INTO local_tasks(cloud_id,title,copies,pages,pdf_path,status,created_at,"
                         "print_requested,print_requested_at,printer_ids,job_model) "
                         "VALUES(?,'test',?,1,'source.pdf','PRINTING',0,1,? ,?,'class')",
                         (cloud_id, count, cloud_id, json.dumps([self.pid])))
        for seq in range(1, count + 1):
            db.execute("INSERT INTO groups(task_id,seq,label,pdf_path,pages,copies,sheets,composed_path,printer_id) "
                       "VALUES(?,?,?,'source.pdf',1,1,1,?,?)",
                       (tid, seq, str(seq), f'{cloud_id}-{seq}.pdf', self.pid))
        return tid

    def jobs(self, *seqs):
        return [{'id': seq, 'document': f'100-{seq}.pdf', 'finished': False} for seq in seqs]

    def step(self, status='printing', jobs=None):
        info = {'status': status, 'page_counter': -1}
        if jobs is not None:
            info['jobs'] = jobs
        self.executor._step_printer(dict(db.one('SELECT * FROM printers WHERE id=?', (self.pid,))), info)

    def statuses(self):
        return [r['status'] for r in db.q('SELECT status FROM groups WHERE task_id=? ORDER BY seq', (self.tid,))]

    def test_prefetch_three_and_replenish_without_waiting_for_idle(self):
        self.executor._assign()
        self.assertEqual(self.statuses(), ['PRINTING'] * 3 + ['QUEUED'] * 2)
        self.step(jobs=self.jobs(1, 2, 3))
        self.step(jobs=self.jobs(2, 3))
        self.executor._assign()
        self.assertEqual(self.statuses(), ['DONE', 'PRINTING', 'PRINTING', 'PRINTING', 'QUEUED'])
        self.assertEqual(self.backend.submit.call_count, 4)
        self.backend.cancel.assert_not_called()

    def test_unobserved_fast_jobs_need_review_instead_of_false_completion(self):
        self.executor._assign()
        self.step('idle', [])
        self.executor._assign()
        self.assertEqual(self.statuses(), ['PAUSED'] * 3 + ['QUEUED'] * 2)
        self.assertEqual(self.backend.submit.call_count, 3)
        self.assertEqual(db.one('SELECT status FROM local_tasks WHERE id=?', (self.tid,))['status'], 'PRINTING')

    def test_short_jobs_are_observed_while_submit_is_blocked_and_all_five_finish(self):
        executor = self.executor
        class FastBackend:
            def __init__(self):
                self.path = None
                self.seen = threading.Event()
                self.calls = 0
            def poll_queue(self, printer):
                jobs = []
                if self.path:
                    jobs = [{'id': self.calls, 'document': self.path, 'finished': False}]
                return {'status': 'printing', 'jobs': jobs}
            def submit(self, printer, path):
                self.calls += 1
                self.path = path
                # Wait for persisted evidence, not just a queue read.
                for _ in range(200):
                    gid = db.one('SELECT id FROM groups WHERE composed_path=?', (path,))['id']
                    if executor._tracking(gid).get('seen'):
                        self.path = None
                        return 'submitted'
                    self.seen.wait(.01)
                raise AssertionError('observer did not run during submission')
        self.executor.backend = FastBackend()
        self.executor._assign()
        self.step('idle', [])
        self.executor._assign()
        self.step('idle', [])
        self.assertEqual(self.statuses(), ['DONE'] * 5)
        self.assertEqual(self.executor.backend.calls, 5)

    def test_fault_snapshot_preserves_job_evidence_and_raw_error(self):
        self.executor._assign()
        p = dict(db.one('SELECT * FROM printers WHERE id=?', (self.pid,)))
        self.executor._step_printer(p, {'status': 'offline', 'detail': 'win32 status=2',
                                       'jobs': self.jobs(1, 2, 3)})
        self.assertEqual(self.statuses()[:3], ['PRINTING'] * 3)
        self.assertIn('win32 status=2', db.one(
            "SELECT detail FROM events WHERE type='PRINTER_STATE' ORDER BY id DESC")['detail'])
        self.step('idle', [])
        self.assertEqual(self.statuses()[:3], ['DONE'] * 3)

    def test_failure_during_submit_is_retained_and_stops_prefetch(self):
        executor = self.executor
        ready = threading.Event()
        gid = db.one('SELECT id FROM groups WHERE task_id=? ORDER BY seq', (self.tid,))['id']
        class FailingBackend:
            def poll_queue(self, printer):
                return {'status': 'offline', 'detail': 'win32 status=2',
                        'jobs': [{'id': 17, 'document': '100-1.pdf', 'failed': True}]}
            def submit(self, printer, path):
                for _ in range(200):
                    if executor._tracking(gid).get('failed'):
                        return 'submitted'
                    ready.wait(.01)
                raise AssertionError('failure not observed')
        self.executor.backend = FailingBackend()
        self.executor._assign()
        self.step('idle', [])
        self.executor._assign()
        self.assertEqual(self.statuses(), ['PAUSED'] + ['QUEUED'] * 4)
        self.assertEqual(self.executor._tracking(gid)['job_ids'], [17])

    def test_known_job_id_survives_document_name_change(self):
        self.executor._assign()
        self.step(jobs=self.jobs(1, 2, 3))
        self.step(jobs=[{'id': 1, 'document': 'driver renamed', 'failed': True}]+self.jobs(2, 3))
        self.step('idle', [])
        self.assertEqual(self.statuses()[0], 'PAUSED')

    def test_seen_job_survives_restart_and_completes_amid_unrelated_jobs(self):
        self.executor._assign()
        self.step(jobs=self.jobs(1, 2, 3))
        self.executor = Executor(self.backend)
        self.step(jobs=self.jobs(2, 3) + [{'id': 90, 'document': 'someone-else.pdf'}])
        self.assertEqual(self.statuses()[0], 'DONE')
        self.executor._assign()
        self.assertEqual(self.backend.submit.call_count, 4)

    def test_faults_preserve_submitted_jobs_and_recover_automatically(self):
        self.executor._assign()
        for status in ('jam', 'offline', 'paper_out'):
            self.step(status, [])
            self.executor._assign()
            self.assertEqual(self.backend.submit.call_count, 3)
            self.assertEqual(self.statuses()[:3], ['PRINTING'] * 3)
        self.step(jobs=self.jobs(1, 2, 3))
        self.step('idle', [])
        self.executor._assign()
        self.assertEqual(self.backend.submit.call_count, 5)
        self.backend.cancel.assert_not_called()

    def test_missing_queue_snapshot_never_means_complete(self):
        self.executor._assign()
        self.step('idle')
        self.assertEqual(self.statuses()[:3], ['PRINTING'] * 3)

    def test_legacy_unsubmitted_blocked_class_recovers_without_manual_action(self):
        db.execute("UPDATE groups SET status='PAUSED' WHERE task_id=? AND seq=1", (self.tid,))
        self.step('idle', [])
        self.executor._assign()
        self.assertEqual(self.statuses()[:3], ['PRINTING'] * 3)

    def test_unrecognized_documents_wait_until_queue_drains_then_need_review(self):
        self.executor._assign()
        self.step(jobs=[{'id': 1, 'document': 'driver-renamed-document'}])
        self.executor._assign()
        self.assertEqual(self.statuses()[:3], ['PRINTING'] * 3)
        self.assertEqual(self.backend.submit.call_count, 3)
        self.step('idle', [])
        self.assertEqual(self.statuses()[:3], ['PAUSED'] * 3)

    def test_restart_with_unconfirmed_submission_does_not_resubmit(self):
        self.backend.submit.side_effect = TimeoutError('interrupted')
        self.executor._assign()
        db.execute("UPDATE groups SET status='PRINTING' WHERE task_id=? AND seq=1", (self.tid,))
        self.executor = Executor(self.backend)
        self.step('idle', [])
        self.executor._assign()
        self.assertEqual(self.backend.submit.call_count, 1)
        self.assertEqual(self.statuses()[0], 'PAUSED')

    def test_preflight_failure_retries_automatically_without_flooding(self):
        self.backend.submit.side_effect = SubmissionNotStarted('queue unavailable')
        self.executor._assign()
        self.executor._assign()
        self.assertEqual(self.backend.submit.call_count, 1)
        self.assertEqual(self.statuses(), ['QUEUED'] * 5)
        self.backend.submit.side_effect = None
        self.executor._retry_after[self.pid] = 0
        self.executor._assign()
        self.assertEqual(self.statuses()[:3], ['PRINTING'] * 3)

    def test_ambiguous_submit_does_not_duplicate_or_claim_success_without_evidence(self):
        self.backend.submit.side_effect = TimeoutError('driver timeout')
        self.executor._assign()
        self.step('idle', [])
        self.executor._assign()
        self.assertEqual(self.backend.submit.call_count, 1)
        self.assertEqual(self.statuses()[0], 'PAUSED')
        self.step(jobs=self.jobs(1))
        self.assertEqual(self.statuses()[0], 'PRINTING')
        self.step('idle', [])
        self.assertEqual(self.statuses()[0], 'DONE')

    def test_paused_task_holds_following_tasks_and_controls_all_prefetched_jobs(self):
        self.make_task(101, 2)
        station = StationRuntime({'cloud_url': 'https://example.invalid'}, Path(self.tmp.name), lambda _: None)
        station.executor = self.executor
        self.executor._assign()
        station.control_task(100, 'pause')
        self.assertEqual(self.backend.control_group.call_count, 3)
        self.step('idle', [])
        self.executor._assign()
        self.assertEqual(self.backend.submit.call_count, 3)
        self.assertEqual(self.statuses()[:3], ['PRINTING'] * 3)
        station.control_task(100, 'resume')
        station.control_task(100, 'cancel')
        self.assertEqual(self.backend.control_group.call_count, 9)
        self.executor._assign()
        self.assertEqual(self.backend.submit.call_count, 5)

    def test_rest_stops_replenishment_and_drains_prefetched_classes(self):
        self.executor._assign()
        db.execute('UPDATE printers SET continuous_print_seconds=max_continuous_minutes*60 WHERE id=?', (self.pid,))
        self.step(jobs=self.jobs(1, 2, 3))
        self.step(jobs=self.jobs(2, 3))
        self.executor._assign()
        self.assertEqual(self.backend.submit.call_count, 3)
        self.step('idle', [])
        self.assertEqual(db.one('SELECT state FROM printers WHERE id=?', (self.pid,))['state'], 'RESTING')
        self.executor._assign()
        self.assertEqual(self.backend.submit.call_count, 3)

    def test_retained_finished_jobs_do_not_block_completion(self):
        self.executor._assign()
        jobs = self.jobs(1, 2, 3)
        for job in jobs:
            job['finished'] = True
        self.step('idle', jobs)
        self.assertEqual(self.statuses()[:3], ['DONE'] * 3)
        self.executor._assign()
        final_jobs = jobs + [{'id': 4, 'document': '100-4.pdf', 'finished': True},
                             {'id': 5, 'document': '100-5.pdf', 'finished': True}]
        self.step('idle', final_jobs)
        self.assertEqual(self.statuses(), ['DONE'] * 5)

    def test_windows_failed_job_does_not_become_done_after_disappearing(self):
        self.executor._assign()
        failed = [{'id': 21, 'document': '100-1.pdf', 'failed': True,
                   'status_code': 2, 'status_text': 'Error', 'finished': False}]
        self.step(jobs=failed + self.jobs(2, 3))
        self.step('idle', [])
        self.assertEqual(self.statuses()[0], 'PAUSED')
        self.assertEqual(self.backend.submit.call_count, 3)
        self.assertEqual(db.one("SELECT COUNT(*) n FROM events WHERE type='SPOOL_JOB_FAILED'")['n'], 1)

    def test_backend_reads_windows_jobs_even_with_snmp_status(self):
        win = Mock(JOB_STATUS_PRINTED=128, JOB_STATUS_COMPLETE=4096, JOB_STATUS_DELETED=256)
        win.GetPrinter.return_value = {'Status': 0}
        win.EnumJobs.return_value = [{'JobId': 7, 'pDocument': '100-1.pdf', 'Status': 128}]
        with patch.dict('sys.modules', {'win32print': win}), patch.object(
                WindowsBackend, 'installed_printers', return_value=[{'queue': 'A'}]), patch(
                'app.backends._snmp_status', return_value={'status': 'idle', 'page_counter': 10}):
            info = WindowsBackend().poll({'queue_name': 'A', 'ip': '192.0.2.1'})
        self.assertEqual(info['jobs'], [{'id': 7, 'document': '100-1.pdf', 'status_code': 128,
                                        'status_text': '', 'printing': False, 'failed': False,
                                        'finished': True}])
        win.ClosePrinter.assert_called_once()


if __name__ == '__main__':
    unittest.main()
