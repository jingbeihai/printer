"""Bounded per-printer class queue; completion follows Windows spooler handling."""
import json
import threading
import time
from pathlib import PureWindowsPath

from . import config, db, importer
from .backends import WindowsBackend, SubmissionNotStarted
from .models import PrinterState as PS


class Executor:
    PREFETCH = 3

    def __init__(self, backend=None, syncer=None):
        self.backend = backend or WindowsBackend()
        self.syncer = syncer
        self.control_lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._sync_thread = None
        self._idle_since = {}  # compatibility with task controls
        self._print_tick = {}
        self._retry_after = {}

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        last_sync = 0.0
        while not self._stop.is_set():
            try:
                now = time.time()
                if self.syncer and now - last_sync >= config.SYNC_INTERVAL:
                    if self._sync_thread is None or not self._sync_thread.is_alive():
                        self._sync_thread = threading.Thread(target=self.syncer.tick, daemon=True)
                        self._sync_thread.start()
                        last_sync = now
                with self.control_lock:
                    self._step_all_printers()
                    self._release_finished_rests(now)
                    if config.in_print_window(time.strftime("%H:%M")):
                        self._assign()
            except Exception as exc:
                db.log_event('EXECUTOR_ERROR', repr(exc))
            self._stop.wait(config.POLL_INTERVAL)

    @staticmethod
    def _tracking(gid):
        row = db.one('SELECT value FROM config_kv WHERE key=?', (f'spool:{gid}',))
        return json.loads(row['value']) if row else {'accepted': True, 'seen': False}

    @staticmethod
    def _save_tracking(gid, state):
        db.execute('INSERT OR REPLACE INTO config_kv(key,value) VALUES(?,?)',
                   (f'spool:{gid}', json.dumps(state)))

    def _step_all_printers(self):
        for row in db.q('SELECT * FROM printers WHERE enabled=1'):
            printer = dict(row)
            try:
                info = self.backend.poll(printer)
            except Exception as exc:
                info = {'status': 'offline', 'detail': str(exc)}
            self._step_printer(printer, info)

    def _step_printer(self, p, info):
        pid = p['id']
        if p['state'] in ('QUARANTINED', 'MAINTENANCE', 'RESTING'):
            return
        status = info['status']
        counter = info.get('page_counter', -1)
        if counter >= 0:
            db.execute('UPDATE printers SET page_counter=?,last_seen=? WHERE id=?',
                       (counter, time.time(), pid))
        faults = {'offline': PS.OFFLINE, 'paper_out': PS.WAIT_PAPER, 'jam': PS.SUSPECT}
        if status in faults:
            self._print_tick.pop(pid, None)
            if p['state'] != faults[status].value:
                self._to(pid, faults[status], '设备异常，保留队列，恢复后自动继续')
            return
        if status == 'printing':
            self._record_running(pid, p)
        else:
            self._print_tick.pop(pid, None)
        # No successful queue snapshot means no evidence for completion.
        jobs = info.get('jobs')
        if not isinstance(jobs, list):
            self._to(pid, PS.OFFLINE, '无法读取 Windows 作业状态，等待重试')
            return
        queue_drained = not any(not job.get('finished', False) for job in jobs)
        groups = db.q("SELECT g.* FROM groups g JOIN local_tasks t ON t.id=g.task_id "
                      "WHERE g.printer_id=? AND g.status IN ('PRINTING','PAUSED') "
                      "AND t.status='PRINTING' AND t.job_model='class' AND t.paused=0", (pid,))
        for g in groups:
            if g['started_at'] is None:
                db.execute("UPDATE groups SET status='QUEUED' WHERE id=?", (g['id'],))
                continue
            expected = PureWindowsPath(g['composed_path']).name.casefold()
            matches = [job for job in jobs if PureWindowsPath(job['document']).name.casefold() == expected]
            tracking = self._tracking(g['id'])
            if matches:
                tracking.update(accepted=True, seen=True)
                self._save_tracking(g['id'], tracking)
                db.execute("UPDATE groups SET status='PRINTING' WHERE id=?", (g['id'],))
            elif not tracking['accepted']:
                # Includes a restart between persisting intent and driver acknowledgement.
                db.execute("UPDATE groups SET status='PAUSED' WHERE id=?", (g['id'],))
            ended = (matches and all(job.get('finished', False) for job in matches)) or (
                not matches and tracking['accepted'] and (tracking['seen'] or queue_drained))
            if ended:
                self._finish_group(g)
        active = db.one("SELECT id FROM groups WHERE printer_id=? AND status='PRINTING' ORDER BY id LIMIT 1", (pid,))
        db.execute('UPDATE printers SET current_group=?,state=? WHERE id=?',
                   (active['id'] if active else None, 'PRINTING' if active or status == 'printing' else 'IDLE', pid))
        fresh = dict(db.one('SELECT * FROM printers WHERE id=?', (pid,)))
        if not active and status == 'idle' and not any(not job.get('finished', False) for job in jobs):
            self._close_run_session(pid, counter, 'queue_drained')
            if fresh['continuous_print_seconds'] >= fresh['max_continuous_minutes'] * 60:
                self._start_rest(fresh)

    def _finish_group(self, g):
        if not db.update_count("UPDATE groups SET status='DONE',done_at=? WHERE id=? AND status IN ('PRINTING','PAUSED')",
                               (time.time(), g['id'])):
            return
        db.execute('UPDATE printers SET pages_total=pages_total+? WHERE id=?', (g['sheets'], g['printer_id']))
        db.execute('DELETE FROM config_kv WHERE key=?', (f"spool:{g['id']}",))
        db.log_event('GROUP_DONE', f"{g['label']} Windows 队列处理结束", printer_id=g['printer_id'],
                     group_id=g['id'], task_id=g['task_id'])
        importer.on_group_done(g['id'])
        task = db.one('SELECT status FROM local_tasks WHERE id=?', (g['task_id'],))
        if task['status'] == 'DONE' and self.syncer:
            threading.Thread(target=self.syncer.tick, daemon=True).start()

    def _to(self, pid: int, state: PS, detail: str = ""):
        db.execute("UPDATE printers SET state=? WHERE id=?", (state.value, pid))
        if detail:
            db.log_event("PRINTER_STATE", f"{state.value}: {detail}", printer_id=pid)

    def _record_running(self, pid: int, p: dict):
        """仅累计实际处于 PRINTING 的秒数，短暂空闲不会清零连续周期。"""
        now = time.time()
        previous = self._print_tick.get(pid, now)
        self._print_tick[pid] = now
        delta = min(max(0, now - previous), config.POLL_INTERVAL * 2 + 2)
        if delta:
            db.execute("UPDATE printers SET continuous_print_seconds="
                       "continuous_print_seconds+? WHERE id=?", (delta, pid))

    def _close_run_session(self, pid: int, counter: int, reason: str):
        row = db.one("SELECT id,started_at FROM printer_run_sessions "
                     "WHERE printer_id=? AND ended_at IS NULL ORDER BY id DESC LIMIT 1", (pid,))
        if row:
            db.execute("UPDATE printer_run_sessions SET ended_at=?, active_seconds=?, "
                       "end_counter=?, end_reason=? WHERE id=?",
                       (time.time(), max(0, time.time() - row["started_at"]), counter, reason, row["id"]))
        self._print_tick.pop(pid, None)

    def _start_rest(self, p: dict):
        required = int(p["min_rest_minutes"] or 30) * 60
        until = time.time() + required
        db.execute("UPDATE printers SET state=?, rest_until=? WHERE id=?",
                   (PS.RESTING.value, until, p["id"]))
        db.execute("INSERT INTO printer_rest_sessions(printer_id,started_at,required_seconds) "
                   "VALUES(?,?,?)", (p["id"], time.time(), required))
        db.log_event("PRINTER_REST_STARTED", f"连续打印达到上限，强制休息{required // 60}分钟",
                     printer_id=p["id"])

    def _release_finished_rests(self, now: float):
        for p in db.q("SELECT * FROM printers WHERE enabled=1 AND state='RESTING'"):
            p = dict(p)
            if now < p["rest_until"]:
                continue
            db.execute("UPDATE printers SET state=?, rest_until=0, continuous_print_seconds=0 "
                       "WHERE id=?", (PS.IDLE.value, p["id"]))
            db.execute("UPDATE printer_rest_sessions SET ended_at=? WHERE printer_id=? "
                       "AND ended_at IS NULL", (now, p["id"]))
            db.log_event("PRINTER_REST_FINISHED", "强制休息结束，恢复派单", printer_id=p["id"])

    def _assign(self):
        importer.maybe_release_next_task()
        for task in db.q("SELECT * FROM local_tasks WHERE status='PRINTING' AND job_model='class' "
                         "AND print_requested=1 AND paused=0 ORDER BY print_requested_at,id"):
            self._assign_task(task)

    def _assign_task(self, task):
        allowed = set(json.loads(task['printer_ids'] or '[]'))
        for row in db.q("SELECT * FROM printers WHERE enabled=1 AND state IN ('IDLE','PRINTING')"):
            p = dict(row)
            pid = p['id']
            if pid not in allowed or time.time() < self._retry_after.get(pid, 0):
                continue
            if p['continuous_print_seconds'] >= p['max_continuous_minutes'] * 60:
                continue  # drain already submitted classes before resting
            first = db.one("SELECT t.id FROM local_tasks t JOIN groups g ON g.task_id=t.id "
                           "WHERE g.printer_id=? AND g.status!='DONE' AND t.status='PRINTING' "
                           "AND t.print_requested=1 ORDER BY t.print_requested_at,t.id LIMIT 1", (pid,))
            if not first or first['id'] != task['id']:
                continue
            # An ambiguous submission must be reconciled before any further dispatch.
            if db.one("SELECT id FROM groups WHERE printer_id=? AND task_id=? AND status='PAUSED'", (pid, task['id'])):
                continue
            count = db.one("SELECT COUNT(*) n FROM groups WHERE printer_id=? AND status='PRINTING'", (pid,))['n']
            for _ in range(max(0, self.PREFETCH - count)):
                g = db.one("SELECT * FROM groups WHERE task_id=? AND printer_id=? AND status='QUEUED' ORDER BY seq LIMIT 1",
                           (task['id'], pid))
                if not g:
                    break
                # Persist intent before invoking the driver: restart must never blindly resubmit it.
                self._save_tracking(g['id'], {'accepted': False, 'seen': False})
                db.execute("UPDATE groups SET status='PRINTING',started_at=?,start_counter=? WHERE id=?",
                           (time.time(), p['page_counter'], g['id']))
                try:
                    job_id = self.backend.submit(p, g['composed_path'])
                except SubmissionNotStarted as exc:
                    db.execute("UPDATE groups SET status='QUEUED',started_at=NULL WHERE id=?", (g['id'],))
                    self._retry_after[pid] = time.time() + 10
                    db.log_event('SUBMIT_FAIL', f'{exc}；10秒后自动重试', printer_id=pid, group_id=g['id'], task_id=task['id'])
                    break
                except Exception as exc:
                    db.execute("UPDATE groups SET status='PAUSED' WHERE id=?", (g['id'],))
                    db.log_event('SUBMIT_UNCERTAIN', f'{exc}；自动核对队列，避免重复下发',
                                 printer_id=pid, group_id=g['id'], task_id=task['id'])
                    break
                self._save_tracking(g['id'], {'accepted': True, 'seen': False})
                db.execute("UPDATE printers SET current_group=COALESCE(current_group,?),state='PRINTING' WHERE id=?", (g['id'], pid))
                if not db.one('SELECT id FROM printer_run_sessions WHERE printer_id=? AND ended_at IS NULL', (pid,)):
                    db.execute('INSERT INTO printer_run_sessions(printer_id,group_id,started_at,start_counter) VALUES(?,?,?,?)',
                               (pid, g['id'], time.time(), p['page_counter']))
                db.log_event('GROUP_ASSIGNED', f"{g['label']} → {p['name']}(作业{job_id})", printer_id=pid,
                             group_id=g['id'], task_id=task['id'])
