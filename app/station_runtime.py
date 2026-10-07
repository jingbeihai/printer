"""The printing station that runs inside the formal Windows client."""
import json
import threading
import time
import uuid
from pathlib import Path

from . import config, db, importer
from .backends import WindowsBackend
from .cloudclient import CloudSync
from .executor import Executor
from .models import PrinterState


STATE_LABELS = {
    "OFFLINE": "离线", "IDLE": "空闲", "PRINTING": "打印中",
    "WAIT_PAPER": "缺纸", "SUSPECT": "需检查", "QUARANTINED": "已隔离",
    "RESTING": "休息中", "MAINTENANCE": "维护中",
}


class StationRuntime:
    def __init__(self, settings: dict, settings_dir: Path, save_settings):
        self.settings = settings
        self.settings_dir = settings_dir
        self.save_settings = save_settings
        self.executor = None
        self.syncer = None
        self.lock = threading.RLock()
        self.print_lock = threading.Lock()
        if not self.settings.get("client_id"):
            self.settings["client_id"] = str(uuid.uuid4())
            self.save_settings(self.settings)

    @property
    def enabled(self):
        return self.executor is not None

    @property
    def ready(self):
        return self.enabled and self.syncer is not None

    def start(self):
        with self.lock:
            if self.enabled:
                return
            config.CLOUD_URL = self.settings["cloud_url"]
            config.AGENT_TOKEN = ""
            config.BACKEND = "windows"
            config.MANUAL_PRINT = True
            config.DATA_DIR = self.settings_dir / "station"
            config.UPLOAD_DIR = config.DATA_DIR / "uploads"
            config.COMPOSED_DIR = config.DATA_DIR / "composed"
            config.LOCAL_DB = config.DATA_DIR / "local.db"
            config.ensure_dirs()
            db.init(config.LOCAL_DB, db.AGENT_SCHEMA)
            self.executor = Executor(backend=WindowsBackend(), syncer=None)
            self.executor.start()

    def bind_session(self, cookie: str):
        with self.lock:
            if not self.enabled:
                self.start()
            if cookie and (self.syncer is None or self.syncer.cookie != cookie):
                if self.syncer:
                    self.syncer.deactivate()
                self.syncer = CloudSync(self.settings["cloud_url"], cookie=cookie,
                                        client_id=self.settings["client_id"])
                self.executor.syncer = self.syncer

    def clear_session(self):
        with self.lock:
            if self.syncer:
                self.syncer.deactivate()
            self.syncer = None
            if self.executor:
                self.executor.syncer = None

    def snapshot(self):
        if not self.enabled:
            return []
        return [{"id": row["id"], "name": row["name"], "ip": row["ip"],
                 "queue": row["queue_name"], "state": STATE_LABELS.get(row["state"], row["state"]),
                 "minutes": round(row["continuous_print_seconds"] / 60),
                 "total": round(row["continuous_print_seconds"] / 60),
                 "printed": row["pages_total"], "limit": row["max_continuous_minutes"],
                 "rest": row["min_rest_minutes"], "left": 0, "mode": "自动休息"}
                for row in db.q("SELECT * FROM printers WHERE enabled=1 ORDER BY name")]

    def pending_print_ids(self):
        if not self.enabled:
            return []
        return [row["cloud_id"] for row in db.q(
            "SELECT cloud_id FROM local_tasks WHERE status='QUEUED' AND print_requested=1")]

    def pickup_ids(self):
        if not self.enabled:
            return []
        return [row["cloud_id"] for row in db.q(
            "SELECT cloud_id FROM local_tasks WHERE status='AWAITING_PICKUP'")]

    def available_printers(self):
        return WindowsBackend.installed_printers()

    def fleet_tasks(self):
        if not self.enabled:
            return []
        result = []
        for task in db.q("SELECT * FROM local_tasks WHERE print_requested=1 AND status NOT IN ('DONE','CANCELED') ORDER BY print_requested_at,id"):
            groups = db.q("SELECT printer_id,status FROM groups WHERE task_id=?", (task['id'],))
            result.append({'id': task['cloud_id'], 'title': task['title'],
                           'status': task['status'], 'paused': bool(task['paused']),
                           'printer_ids': json.loads(task['printer_ids']),
                           'active_printer_ids': [g['printer_id'] for g in groups if g['status']=='PRINTING'],
                           'completed_groups': sum(g['status']=='DONE' for g in groups),
                           'total_groups': len(groups)})
        return result

    def control_task(self, cloud_id, action, printer_id=None):
        if action not in ('pause', 'resume', 'cancel', 'up', 'down'):
            raise ValueError("不支持的任务操作")
        with self.print_lock, self.executor.control_lock:
            task = db.one("SELECT * FROM local_tasks WHERE cloud_id=? AND print_requested=1", (cloud_id,))
            if not task or task['status'] in ('DONE','CANCELED'):
                raise ValueError("本机任务不存在或已结束")
            active = db.q("SELECT * FROM groups WHERE task_id=? AND status='PRINTING'", (task['id'],))
            if action in ('up','down'):
                waiting = [t for t in self.fleet_tasks() if printer_id in t['printer_ids']
                           and not t['active_printer_ids'] and t['status'] != 'AWAITING_PICKUP']
                ids = [t['id'] for t in waiting]
                if cloud_id not in ids:
                    raise ValueError("只能调整等待任务")
                index = ids.index(cloud_id); target = index + (-1 if action=='up' else 1)
                if target < 0 or target >= len(ids):
                    raise ValueError("任务已在列表边界")
                ids[index], ids[target] = ids[target], ids[index]
                ordered = [t['id'] for t in self.fleet_tasks()]
                first, second = ordered.index(ids[index]), ordered.index(ids[target])
                ordered[first], ordered[second] = ordered[second], ordered[first]
                for position, cid in enumerate(ordered, 1):
                    db.execute('UPDATE local_tasks SET print_requested_at=? WHERE cloud_id=?', (position,cid))
                return
            if task['status']=='AWAITING_PICKUP' and action!='cancel':
                raise ValueError("该任务已打印完成，请确认收纸")
            # Stop dispatch first, including when a driver rejects the control.
            db.execute('UPDATE local_tasks SET paused=1 WHERE id=?', (task['id'],))
            matched = 0
            for group in active:
                printer = dict(db.one('SELECT * FROM printers WHERE id=?', (group['printer_id'],)))
                try:
                    matched += self.executor.backend.control_group(printer, group['composed_path'], action)
                except Exception as exc:
                    raise RuntimeError(f'后续下发已暂停，但 Windows 作业控制失败：{exc}') from exc
            if action=='cancel':
                db.execute("UPDATE local_tasks SET status='CANCELED',done_at=? WHERE id=?", (time.time(),task['id']))
                db.execute("UPDATE groups SET status='PAUSED' WHERE task_id=? AND status!='DONE'", (task['id'],))
                for group in active:
                    db.execute("UPDATE printers SET current_group=NULL,state='IDLE' WHERE id=?", (group['printer_id'],))
                    self.executor._idle_since.pop(group['printer_id'], None)
                    self.executor._close_run_session(group['printer_id'], -1, 'task_canceled')
            elif action=='resume':
                db.execute('UPDATE local_tasks SET paused=0 WHERE id=?', (task['id'],))
                for group in active:
                    self.executor._idle_since.pop(group['printer_id'], None)
            db.log_event('TASK_CONTROL', action, task_id=task['id'])
            return ('已停止后续下发；已进入打印机缓存的页面可能继续出纸' if action!='resume' and active and not matched else '')

    def add_printer(self, name: str, queue: str, ip: str = ""):
        if not self.enabled:
            raise ValueError("请先启用本机打印站")
        installed = {printer["queue"] for printer in self.available_printers()}
        if queue not in installed:
            raise ValueError("Windows 中找不到该打印机，请先安装驱动并添加打印机")
        name = name.strip() or queue
        existing = db.one("SELECT * FROM printers WHERE name=? OR queue_name=?", (name, queue))
        if existing and existing['enabled']:
            raise ValueError("这台打印机已添加")
        if existing:
            db.execute("UPDATE printers SET name=?,queue_name=?,ip=?,enabled=1,state='OFFLINE' WHERE id=?",
                       (name, queue, ip.strip(), existing['id']))
            return existing['id']
        pid = db.execute("INSERT INTO printers(name,ip,queue_name,state) VALUES(?,?,?,?)",
                         (name, ip.strip(), queue, PrinterState.OFFLINE.value))
        db.log_event("PRINTER_ADDED", f"已绑定 Windows 队列：{queue}", printer_id=pid)
        return pid

    def delete_printer(self, pid: int):
        with self.print_lock:
            printer = db.one("SELECT * FROM printers WHERE id=? AND enabled=1", (pid,))
            if not printer:
                raise ValueError("打印机不存在")
            if printer['current_group'] or printer['state'] == 'PRINTING':
                raise ValueError("打印机正在执行任务，请先取消或完成任务")
            for task in db.q("SELECT printer_ids FROM local_tasks WHERE print_requested=1 AND status IN ('QUEUED','PRINTING')"):
                if pid in json.loads(task['printer_ids'] or '[]'):
                    raise ValueError("打印机仍有未完成任务，请先取消或完成任务")
            db.execute("UPDATE printers SET enabled=0 WHERE id=?", (pid,))
            db.log_event("PRINTER_REMOVED", f"已移除打印机：{printer['name']}", printer_id=pid)

    def request_print(self, cloud_task: dict, printer_ids: list[int]):
        with self.print_lock:
            return self._request_print_locked(cloud_task, printer_ids)

    def _request_print_locked(self, cloud_task: dict, printer_ids: list[int]):
        if not self.ready or not self.syncer:
            raise ValueError("请先登录云端")
        syncer = self.syncer
        cloud_id = cloud_task["id"]
        if not printer_ids:
            raise ValueError("请选择至少一台打印机")
        chosen = set(printer_ids)
        configured = {row["id"]: row["state"] for row in
                      db.q("SELECT id,state FROM printers WHERE enabled=1")}
        if not chosen <= configured.keys():
            raise ValueError("所选打印机未在本机配置")
        if any(configured[pid] not in (PrinterState.IDLE.value, PrinterState.PRINTING.value) for pid in chosen):
            raise ValueError("请选择已连接且可用的打印机")
        task = db.one("SELECT * FROM local_tasks WHERE cloud_id=?", (cloud_id,))
        if task and task["status"] != "QUEUED":
            raise ValueError("该任务已经开始或结束，不能重复打印")
        if task and task["print_requested"]:
            raise ValueError("该任务已加入本机打印队列，请等待前一任务收纸")
        if not task:
            pdf_path = syncer._download(cloud_id, cloud_task["pdf_name"])
            meta = {key: cloud_task.get(key, "") for key in
                    ("school", "grade", "classes", "note", "source_user")}
            importer.create_local_task(
                cloud_id, cloud_task["title"], pdf_path, int(cloud_task["copies"]),
                priority=int(cloud_task.get("priority", 0)), meta=meta)
            task = db.one("SELECT * FROM local_tasks WHERE cloud_id=?", (cloud_id,))
        syncer.task_action(cloud_id, "claim")
        db.execute("UPDATE local_tasks SET print_requested=1,print_requested_at=?,printer_ids=? WHERE id=?",
                   (time.time(), json.dumps(sorted(chosen)), task["id"]))
        db.log_event("PRINT_REQUESTED", f"已选择 {len(chosen)} 台打印机", task_id=task["id"])
        importer.maybe_release_next_task()

    def confirm_pickup(self, cloud_id: int):
        task = db.one("SELECT id FROM local_tasks WHERE cloud_id=?", (cloud_id,))
        if not task or not importer.confirm_pickup(task["id"]):
            raise ValueError("该任务还不能确认收纸")
        if self.syncer:
            threading.Thread(target=self.syncer.tick, daemon=True).start()

    def stop(self):
        if self.executor:
            self.executor.stop()
