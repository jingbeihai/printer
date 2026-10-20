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
        self._halt_tasks = set()

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

    def _step_printer(self, p, info, observe_only=False):
        pid = p['id']
        if p['state'] in ('QUARANTINED', 'MAINTENANCE', 'RESTING'):
            return
        status = info['status']
        counter = info.get('page_counter', -1)
        if counter >= 0:
            db.execute('UPDATE printers SET page_counter=?,last_seen=? WHERE id=?',
                       (counter, time.time(), pid))
        faults = {'offline': PS.OFFLINE, 'paper_out': PS.WAIT_PAPER, 'jam': PS.SUSPECT}
        fault = status in faults
        if fault and not observe_only:
            self._print_tick.pop(pid, None)
            if p['state'] != faults[status].value:
                self._to(pid, faults[status], f"设备异常，保留队列，恢复后自动继续；{info.get('detail', '无详细信息')}")
        if status == 'printing' and not observe_only:
            self._record_running(pid, p)
        elif not observe_only:
            self._print_tick.pop(pid, None)
        # No successful queue snapshot means no evidence for completion.
        jobs = info.get('jobs')
        if not isinstance(jobs, list):
            if not observe_only and not fault:
                self._to(pid, PS.OFFLINE, f"无法读取 Windows 作业状态，等待重试；{info.get('detail', '')}")
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
            tracking = self._tracking(g['id'])
            matches = [job for job in jobs if job['id'] in tracking.get('job_ids', [])
                       or PureWindowsPath(job['document']).name.casefold() == expected]
            if matches:
                if not tracking.get('seen'):
                    db.log_event('SPOOL_JOB_SEEN', f"{g['label']} Windows 作业号：{', '.join(str(job['id']) for job in matches)}",
                                 printer_id=pid, group_id=g['id'], task_id=g['task_id'])
                tracking.update(accepted=True, seen=True,
                                job_ids=sorted(set(tracking.get('job_ids', [])) | {job['id'] for job in matches}))
                if any(job.get('printing') for job in matches) and not tracking.get('printing'):
                    tracking['printing'] = True
                    db.log_event('SPOOL_JOB_PRINTING', f"{g['label']} Windows 报告作业正在打印",
                                 printer_id=pid, group_id=g['id'], task_id=g['task_id'])
                self._save_tracking(g['id'], tracking)
                if any(job.get('failed') for job in matches):
                    db.execute("UPDATE groups SET status='PAUSED' WHERE id=?", (g['id'],))
                    if not tracking.get('failed'):
                        details = ', '.join(f"#{job['id']} 状态{job.get('status_code', 0)} {job.get('status_text', '')}"
                                            for job in matches if job.get('failed'))
                        db.log_event('SPOOL_JOB_FAILED', f"{g['label']} Windows 作业失败：{details}",
                                     printer_id=pid, group_id=g['id'], task_id=g['task_id'])
                        tracking['failed'] = True
                        self._save_tracking(g['id'], tracking)
                    continue
                if tracking.get('failed'):
                    continue
                db.execute("UPDATE groups SET status='PRINTING' WHERE id=?", (g['id'],))
            if observe_only or fault:
                continue
            if tracking.get('failed'):
                continue
            elif not tracking['accepted']:
                # Includes a restart between persisting intent and driver acknowledgement.
                db.execute("UPDATE groups SET status='PAUSED' WHERE id=?", (g['id'],))
            elif not tracking['seen'] and queue_drained:
                db.execute("UPDATE groups SET status='PAUSED' WHERE id=?", (g['id'],))
                if not tracking.get('unobserved'):
                    tracking['unobserved'] = True
                    self._save_tracking(g['id'], tracking)
                    db.log_event('SPOOL_UNOBSERVED', f"{g['label']} 下发后未观察到 Windows 作业；队列已空，结果待核对",
                                 printer_id=pid, group_id=g['id'], task_id=g['task_id'])
                continue
            ended = (matches and all(job.get('finished', False) for job in matches)) or (
                not matches and tracking['accepted'] and tracking['seen'])
            if ended:
                reason = ('Windows 报告作业处理完成' if matches else
                          '此前观察到的 Windows 作业已离开队列')
                self._finish_group(g, reason)
        if observe_only or fault:
            return
        active = db.one("SELECT id FROM groups WHERE printer_id=? AND status='PRINTING' ORDER BY id LIMIT 1", (pid,))
        db.execute('UPDATE printers SET current_group=?,state=? WHERE id=?',
                   (active['id'] if active else None, 'PRINTING' if active or status == 'printing' else 'IDLE', pid))
        fresh = dict(db.one('SELECT * FROM printers WHERE id=?', (pid,)))
        if not active and status == 'idle' and not any(not job.get('finished', False) for job in jobs):
            self._close_run_session(pid, counter, 'queue_drained')
            if fresh['continuous_print_seconds'] >= fresh['max_continuous_minutes'] * 60:
                self._start_rest(fresh)

    def _finish_group(self, g, reason='Windows 队列处理结束'):
        if not db.update_count("UPDATE groups SET status='DONE',done_at=? WHERE id=? AND status IN ('PRINTING','PAUSED')",
                               (time.time(), g['id'])):
            return
        db.execute('UPDATE printers SET pages_total=pages_total+? WHERE id=?', (g['sheets'], g['printer_id']))
        db.execute('DELETE FROM config_kv WHERE key=?', (f"spool:{g['id']}",))
        db.log_event('GROUP_DONE', f"{g['label']} {reason}", printer_id=g['printer_id'],
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

    def _submit_observed(self, printer, path):
        # Keep collecting evidence while Sumatra blocks. This observer never
        # completes a class or dispatches work; the main loop reconciles later.
        poll = getattr(type(self.backend), 'poll_queue', None)
        if not callable(poll):
            return self.backend.submit(printer, path)
        stopped = threading.Event()
        def observe():
            last_error = None
            while True:
                try:
                    info = self.backend.poll_queue(printer)
                    if info.get('status') in ('offline', 'paper_out', 'jam') or not isinstance(info.get('jobs'), list):
                        detail = f"{info.get('status')}: {info.get('detail', '无法读取队列')}"
                        if detail != last_error:
                            last_error = detail
                            db.log_event('SPOOL_MONITOR_ERROR', detail, printer_id=printer['id'])
                    self._step_printer(printer, info, observe_only=True)
                except Exception as exc:
                    if str(exc) != last_error:
                        last_error = str(exc)
                        db.log_event('SPOOL_MONITOR_ERROR', str(exc), printer_id=printer['id'])
                if stopped.wait(0.1):
                    break
        worker = threading.Thread(target=observe, daemon=True)
        worker.start()
        try:
            return self.backend.submit(printer, path)
        finally:
            stopped.set()
            worker.join()

    def _assign_task(self, task):
        if task['id'] in self._halt_tasks:
            return
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
                if task['id'] in self._halt_tasks:
                    break
                g = db.one("SELECT * FROM groups WHERE task_id=? AND printer_id=? AND status='QUEUED' ORDER BY seq LIMIT 1",
                           (task['id'], pid))
                if not g:
                    break
                # Persist intent before invoking the driver: restart must never blindly resubmit it.
                self._save_tracking(g['id'], {'accepted': False, 'seen': False})
                db.execute("UPDATE groups SET status='PRINTING',started_at=?,start_counter=? WHERE id=?",
                           (time.time(), p['page_counter'], g['id']))
                db.log_event('SUBMIT_STARTED', f"{g['label']} 开始下发；文件 {PureWindowsPath(g['composed_path']).name}",
                             printer_id=pid, group_id=g['id'], task_id=task['id'])
                submit_start = time.monotonic()
                try:
                    job_id = self._submit_observed(p, g['composed_path'])
                except SubmissionNotStarted as exc:
                    db.execute("UPDATE groups SET status='QUEUED',started_at=NULL WHERE id=?", (g['id'],))
                    self._retry_after[pid] = time.time() + 10
                    db.log_event('SUBMIT_FAIL', f'{exc}；耗时{time.monotonic()-submit_start:.2f}秒；10秒后自动重试', printer_id=pid, group_id=g['id'], task_id=task['id'])
                    break
                except Exception as exc:
                    db.execute("UPDATE groups SET status='PAUSED' WHERE id=?", (g['id'],))
                    db.log_event('SUBMIT_UNCERTAIN', f'{exc}；耗时{time.monotonic()-submit_start:.2f}秒；自动核对队列，避免重复下发',
                                 printer_id=pid, group_id=g['id'], task_id=task['id'])
                    break
                tracking = self._tracking(g['id'])
                tracking['accepted'] = True
                self._save_tracking(g['id'], tracking)
                db.log_event('SUBMIT_RETURNED', f"{g['label']} 打印组件已返回；耗时{time.monotonic()-submit_start:.2f}秒；组件标识{job_id}（非 Windows 作业号）",
                             printer_id=pid, group_id=g['id'], task_id=task['id'])
                db.execute("UPDATE printers SET current_group=COALESCE(current_group,?),state='PRINTING' WHERE id=?", (g['id'], pid))
                if not db.one('SELECT id FROM printer_run_sessions WHERE printer_id=? AND ended_at IS NULL', (pid,)):
                    db.execute('INSERT INTO printer_run_sessions(printer_id,group_id,started_at,start_counter) VALUES(?,?,?,?)',
                               (pid, g['id'], time.time(), p['page_counter']))
                db.log_event('GROUP_ASSIGNED', f"{g['label']} → {p['name']}(作业{job_id})", printer_id=pid,
                             group_id=g['id'], task_id=task['id'])
                if tracking.get('failed'):
                    break
