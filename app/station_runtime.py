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

    def add_printer(self, name: str, queue: str, ip: str = ""):
        if not self.enabled:
            raise ValueError("请先启用本机打印站")
        installed = {printer["queue"] for printer in self.available_printers()}
        if queue not in installed:
            raise ValueError("Windows 中找不到该打印机，请先安装驱动并添加打印机")
        name = name.strip() or queue
        if db.one("SELECT id FROM printers WHERE name=? OR queue_name=?", (name, queue)):
            raise ValueError("这台打印机已添加")
        pid = db.execute("INSERT INTO printers(name,ip,queue_name,state) VALUES(?,?,?,?)",
                         (name, ip.strip(), queue, PrinterState.OFFLINE.value))
        db.log_event("PRINTER_ADDED", f"已绑定 Windows 队列：{queue}", printer_id=pid)
        return pid

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
        if any(configured[pid] != PrinterState.IDLE.value for pid in chosen):
            raise ValueError("请选择空闲且已连接的打印机")
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
