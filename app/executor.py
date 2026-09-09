# -*- coding: utf-8 -*-
"""
调度执行器(代理侧核心): 每 POLL_INTERVAL 秒一轮:
  1. 云同步 tick(每 SYNC_INTERVAL 秒一次)
  2. 逐台打印机 poll → 状态机迁移(含故障转移、缺纸等待、三级完成确认)
  3. 给 IDLE 健康机派下一小组(闸口内)
  4. 维护页调度(每日测试页/闲置期周维护)
现场按钮(故障/复位/加纸/改派/隔离)由 agent_web 调本类的动作方法。
"""
import threading
import time

from . import config, db, importer
from .backends import get_backend
from .models import GroupStatus as GS
from .models import PrinterState as PS
from .models import TaskStatus as TS


class Executor:
    def __init__(self, backend=None, syncer=None):
        self.backend = backend or get_backend()
        self.syncer = syncer              # CloudSync 实例,可空(纯本地模式)
        self._stop = threading.Event()
        self._thread = None
        self._idle_since: dict[int, float] = {}   # 打印机回 idle 的起始时刻(完成判定用)
        self._print_tick: dict[int, float] = {}   # 实际 PRINTING 时间采样点

    # ---------------- 生命周期 ----------------
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
                    self.syncer.tick()
                    last_sync = now
                self._step_all_printers()
                self._release_finished_rests(now)
                if config.in_print_window(time.strftime("%H:%M")):
                    self._assign()
                self._maintenance_tick(now)
            except Exception as e:  # noqa: BLE001 - 调度循环永不因单次异常退出
                db.log_event("EXECUTOR_ERROR", repr(e))
            self._stop.wait(config.POLL_INTERVAL)

    # ---------------- 状态机 ----------------
    def _step_all_printers(self):
        printers = db.q("SELECT * FROM printers WHERE enabled=1")
        for p in printers:
            p = dict(p)
            try:
                info = self.backend.poll(p)
            except Exception as e:  # noqa: BLE001
                info = {"status": "offline", "page_counter": -1, "detail": repr(e)}
            self._step_printer(p, info)

    def _step_printer(self, p: dict, info: dict):
        st = info["status"]
        pid = p["id"]
        old = p["state"]
        counter = info.get("page_counter", -1)
        if counter >= 0:
            db.execute("UPDATE printers SET page_counter=?, last_seen=? WHERE id=?",
                       (counter, time.time(), pid))

        if old in (PS.QUARANTINED.value, PS.MAINTENANCE.value, PS.RESTING.value):
            return  # 隔离/维护中的机器不自动迁移,等人工

        if st == "offline":
            if old != PS.OFFLINE.value:
                self._to(pid, PS.OFFLINE, f"离线:{info.get('detail','')}")
                self._requeue_current(p, "打印机离线")
            return
        if old == PS.OFFLINE.value:
            self._to(pid, PS.IDLE, "上线")

        if st == "paper_out":
            if old == PS.PRINTING.value:
                self._to(pid, PS.WAIT_PAPER, "缺纸,等待加纸(不转派)")
            return
        if st == "jam":
            if old not in (PS.SUSPECT.value,):
                self._to(pid, PS.SUSPECT, f"卡纸/错误:{info.get('detail','')}")
                self._requeue_current(p, "卡纸")
            return

        if st == "printing":
            self._record_running(pid, p)
            if old in (PS.IDLE.value, PS.WAIT_PAPER.value) and p["current_group"]:
                self._to(pid, PS.PRINTING, "打印中")
            self._idle_since.pop(pid, None)
            return

        # st == idle
        self._print_tick.pop(pid, None)
        if old == PS.PRINTING.value and p["current_group"]:
            self._confirm_completion(p, counter)
        elif old == PS.WAIT_PAPER.value and p["current_group"]:
            # 缺纸被人工处理后机器回 idle 但作业可能已被搅乱 → 保守起见整组重打
            self._requeue_current(p, "缺纸后状态不确定,整组重打")
            self._to(pid, PS.IDLE, "恢复空闲")
        elif old == PS.SUSPECT.value:
            pass  # 等人工

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

    # ---------------- 完成确认(三级) ----------------
    def _confirm_completion(self, p: dict, counter: int):
        pid, gid = p["id"], p["current_group"]
        # 回 idle 需持续 COMPLETION_SETTLE_SEC 秒,防止中途假空闲
        since = self._idle_since.setdefault(pid, time.time())
        if time.time() - since < config.COMPLETION_SETTLE_SEC:
            return
        self._idle_since.pop(pid, None)

        g = db.one("SELECT * FROM groups WHERE id=?", (gid,))
        if not g:
            db.execute("UPDATE printers SET current_group=NULL WHERE id=?", (pid,))
            return
        expected = g["sheets"]
        start = g["start_counter"] or 0

        if counter >= 0 and start:
            delta = counter - start
            if delta < expected:  # 页计数不足 → 卡纸/中断未报,整组重打
                db.log_event("GROUP_VERIFY_FAIL",
                             f"{g['label']} 页计数{delta}/{expected},整组重打",
                             printer_id=pid, group_id=gid, task_id=g["task_id"])
                self._requeue_current(p, f"完成校验失败({delta}/{expected})")
                self._to(pid, PS.SUSPECT, "完成校验失败,待人工检查")
                return

        db.execute("UPDATE groups SET status='DONE', done_at=? WHERE id=?",
                   (time.time(), gid))
        db.execute("UPDATE printers SET current_group=NULL, state=?, "
                   "pages_total=pages_total+? WHERE id=?",
                   (PS.IDLE.value, expected, pid))
        db.log_event("GROUP_DONE", f"{g['label']} 完成({expected}张)",
                     printer_id=pid, group_id=gid, task_id=g["task_id"])
        self._close_run_session(pid, counter, "group_done")
        # 当前小组完整结束后才开始休息，绝不在一摞文件中途停机。
        fresh = dict(db.one("SELECT * FROM printers WHERE id=?", (pid,)))
        if fresh["continuous_print_seconds"] >= fresh["max_continuous_minutes"] * 60:
            self._start_rest(fresh)
        importer.on_group_done(gid)

    # ---------------- 故障转移 ----------------
    def _requeue_current(self, p: dict, reason: str):
        gid = p["current_group"]
        if not gid:
            return
        g = db.one("SELECT * FROM groups WHERE id=?", (gid,))
        if not g or g["status"] != GS.PRINTING.value:
            db.execute("UPDATE printers SET current_group=NULL WHERE id=?", (p["id"],))
            return
        attempts = g["attempts"] + 1
        if attempts >= config.MAX_GROUP_ATTEMPTS:
            db.execute("UPDATE groups SET status='PAUSED', attempts=? WHERE id=?",
                       (attempts, gid))
            db.log_event("GROUP_PAUSED",
                         f"{g['label']} 重试{attempts}次仍失败,暂停待人工:{reason}",
                         printer_id=p["id"], group_id=gid, task_id=g["task_id"])
        else:
            db.execute("UPDATE groups SET status='QUEUED', attempts=?, printer_id=NULL,"
                       " start_counter=NULL WHERE id=?", (attempts, gid))
            db.log_event("GROUP_REQUEUED", f"{g['label']} 整组重派(第{attempts}次):{reason}",
                         printer_id=p["id"], group_id=gid, task_id=g["task_id"])
        try:
            self.backend.cancel(p)
        except Exception:  # noqa: BLE001
            pass
        db.execute("UPDATE printers SET current_group=NULL WHERE id=?", (p["id"],))
        self._close_run_session(p["id"], int(p.get("page_counter", -1)), reason)

    # ---------------- 派单 ----------------
    def _assign(self):
        task = importer.current_active_task()
        if not task:
            importer.maybe_release_next_task()
            task = importer.current_active_task()
        if not task:
            return
        idle = db.q("SELECT * FROM printers WHERE enabled=1 AND state=? "
                    "AND current_group IS NULL", (PS.IDLE.value,))
        for p in idle:
            g = db.one("SELECT * FROM groups WHERE task_id=? AND status='QUEUED'"
                       " ORDER BY seq LIMIT 1", (task["id"],))
            if not g:
                break
            p = dict(p)
            p["_sheets"] = g["sheets"]
            try:
                job_id = self.backend.submit(p, g["composed_path"])
            except Exception as e:  # noqa: BLE001
                db.log_event("SUBMIT_FAIL", f"{g['label']} 下发失败:{e}", printer_id=p["id"],
                             group_id=g["id"], task_id=task["id"])
                self._to(p["id"], PS.SUSPECT, "下发失败")
                continue
            db.execute("UPDATE groups SET status='PRINTING', printer_id=?,"
                       " start_counter=?, started_at=? WHERE id=?",
                       (p["id"], p["page_counter"], time.time(), g["id"]))
            db.execute("UPDATE printers SET current_group=?, state=? WHERE id=?",
                       (g["id"], PS.PRINTING.value, p["id"]))
            db.execute("INSERT INTO printer_run_sessions(printer_id,group_id,started_at,start_counter) "
                       "VALUES(?,?,?,?)", (p["id"], g["id"], time.time(), p["page_counter"]))
            db.log_event("GROUP_ASSIGNED", f"{g['label']} → {p['name']}(作业{job_id})",
                         printer_id=p["id"], group_id=g["id"], task_id=task["id"])

    # ---------------- 维护 ----------------
    def _maintenance_tick(self, now: float):
        last = float(db.get_conf("last_maintenance", "0") or 0)
        if now - last < config.MAINTENANCE_INTERVAL_DAYS * 86400:
            return
        # 仅在没有活动任务时自动打维护页(闲置期场景)
        if importer.current_active_task():
            return
        self.send_maintenance_pages()
        db.set_conf("last_maintenance", str(now))

    def send_maintenance_pages(self):
        """给所有健康机发一张维护测试页(由 agent_web 生成或现成PDF)。"""
        from .maintpdf import maintenance_pdf_path
        pdf = maintenance_pdf_path()
        for p in db.q("SELECT * FROM printers WHERE enabled=1 AND state IN ('IDLE','OFFLINE')"):
            try:
                p = dict(p)
                p["_sheets"] = 1
                self.backend.submit(p, str(pdf))
                db.log_event("MAINTENANCE_PAGE", "维护页已下发", printer_id=p["id"])
            except Exception as e:  # noqa: BLE001
                db.log_event("MAINTENANCE_FAIL", str(e), printer_id=p["id"])

    # ---------------- 现场按钮动作(agent_web 调用) ----------------
    def act_fault(self, pid: int):
        p = db.one("SELECT * FROM printers WHERE id=?", (pid,))
        if not p:
            return
        self._requeue_current(dict(p), "人工报故障")
        db.execute("UPDATE printers SET state=? WHERE id=?", (PS.QUARANTINED.value, pid))
        db.log_event("PRINTER_QUARANTINE", "人工报故障,已隔离", printer_id=pid)

    def act_reset(self, pid: int):
        db.execute("UPDATE printers SET state=? WHERE id=?", (PS.IDLE.value, pid))
        db.log_event("PRINTER_RESET", "人工复位为空闲", printer_id=pid)

    def act_refill(self, pid: int):
        if hasattr(self.backend, "refill"):
            self.backend.refill(pid)
        db.execute("UPDATE printers SET tray_sheets=? WHERE id=?",
                   (config.SIM_TRAY_CAPACITY, pid))
        p = db.one("SELECT * FROM printers WHERE id=?", (pid,))
        if p and p["state"] == PS.WAIT_PAPER.value:
            db.execute("UPDATE printers SET state=? WHERE id=?", (PS.PRINTING.value, pid))
        db.log_event("PRINTER_REFILL", "已加纸", printer_id=pid)

    def act_reassign(self, pid: int):
        """缺纸等场景下手动把当前小组改派他机。"""
        p = db.one("SELECT * FROM printers WHERE id=?", (pid,))
        if p:
            self._requeue_current(dict(p), "人工改派")
            db.execute("UPDATE printers SET state=? WHERE id=?", (PS.IDLE.value, pid))

    def act_sim_inject(self, pid: int, fault):
        if hasattr(self.backend, "inject"):
            self.backend.inject(pid, fault)
            db.log_event("SIM_INJECT", f"注入故障:{fault or '清除'}", printer_id=pid)
