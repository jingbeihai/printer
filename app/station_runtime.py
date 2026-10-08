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
            config.DATA_DIR = self.settings_dir / "station"
            config.UPLOAD_DIR = config.DATA_DIR / "uploads"
            config.COMPOSED_DIR = config.DATA_DIR / "composed"
            config.LOCAL_DB = config.DATA_DIR / "local.db"
            config.ensure_dirs()
            db.init(config.LOCAL_DB, db.LOCAL_SCHEMA)
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
            groups = db.q("SELECT printer_id,status,label FROM groups WHERE task_id=? ORDER BY seq", (task['id'],))
            per_printer = {}
            for group in groups:
                if group['printer_id'] is not None:
                    per_printer.setdefault(group['printer_id'], []).append(group)
            result.append({'id': task['cloud_id'], 'title': task['title'],
                           'status': task['status'], 'paused': bool(task['paused']),
                           'printer_ids': json.loads(task['printer_ids']),
                           'active_printer_ids': [g['printer_id'] for g in groups if g['status']=='PRINTING'],
                           'completed_groups': sum(g['status']=='DONE' for g in groups),
                           'total_groups': len(groups),
                           'allocations': [{'printer_id': pid, 'groups': len(items),
                                            'labels': [g['label'] for g in items],
                                            'completed': sum(g['status']=='DONE' for g in items),
                                            'blocked': sum(g['status']=='PAUSED' for g in items)}
                                           for pid, items in sorted(per_printer.items())],
                           'blocked_groups': [{'id': g['id'], 'label': g['label'],
                                               'printer_id': g['printer_id'], 'status': g['status']}
                                              for g in db.q("SELECT id,label,printer_id,status FROM groups WHERE task_id=? AND status='PAUSED'", (task['id'],))]})
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

    def resolve_class_group(self, cloud_id, group_id, action, confirm_stopped=False):
        if action not in ('recover', 'complete', 'retry'):
            raise ValueError("不支持的班级作业处理方式")
        with self.print_lock, self.executor.control_lock:
            task = db.one("SELECT * FROM local_tasks WHERE cloud_id=? AND job_model='class'", (cloud_id,))
            group = db.one("SELECT * FROM groups WHERE id=? AND task_id=?", (group_id, task['id'])) if task else None
            if not group or group['status'] != 'PAUSED':
                raise ValueError("该班级作业不存在或不需要人工处理")
            if action == 'recover' and group['started_at'] is not None:
                raise ValueError("该班级已提交到队列，必须确认已完成或整班重打")
            printer = db.one("SELECT * FROM printers WHERE id=? AND enabled=1", (group['printer_id'],))
            if not printer:
                raise ValueError("原分配打印机已不可用")
            try:
                info = self.executor.backend.poll(dict(printer))
            except Exception as exc:
                raise ValueError(f"无法确认原打印机状态：{exc}") from exc
            if info.get('status') != 'idle':
                raise ValueError("原打印机仍有故障或正在打印，请先处理至空闲状态")
            if action == 'complete':
                if group['started_at'] is None:
                    raise ValueError("该班级尚未提交，不能确认已完成")
                db.execute("UPDATE groups SET status='DONE',done_at=? WHERE id=?", (time.time(), group_id))
                db.log_event("CLASS_MARKED_DONE", f"人工确认{group['label']}已完成", printer_id=printer['id'], group_id=group_id, task_id=task['id'])
                importer.on_group_done(group_id)
                return "已记录该班级完成"
            if action == 'retry':
                if group['started_at'] is None:
                    raise ValueError("该班级尚未提交，请使用恢复操作")
                if not confirm_stopped:
                    raise ValueError("请先确认 Windows 队列中的旧作业已停止，并确认废弃已出纸张")
                try:
                    matched = self.executor.backend.control_group(dict(printer), group['composed_path'], 'cancel')
                except Exception as exc:
                    raise ValueError(f"无法确认旧作业已停止：{exc}") from exc
                if not matched:
                    raise ValueError("未能精确匹配并停止旧作业；请在 Windows 队列处理后重试")
            db.execute("UPDATE groups SET status='QUEUED',started_at=NULL,start_counter=NULL WHERE id=?", (group_id,))
            db.execute("UPDATE printers SET state='IDLE' WHERE id=?", (printer['id'],))
            db.log_event("CLASS_RELEASED", f"人工将{group['label']}留在原打印机重新派发({action})", printer_id=printer['id'], group_id=group_id, task_id=task['id'])
            return "该班级已保留在原打印机等待派发"

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
                    ("school", "grade", "classes", "copies_per_class", "note", "source_user")}
            if not meta.get("copies_per_class") and meta.get("classes"):
                total = int(cloud_task.get("copies", 0))
                classes = int(meta["classes"] or 0)
                if classes < 1 or total % classes:
                    raise ValueError("任务缺少完整班级份数信息，请重新提交任务")
                meta["copies_per_class"] = total // classes
            importer.create_local_task(
                cloud_id, cloud_task["title"], pdf_path, int(cloud_task["copies"]),
                priority=int(cloud_task.get("priority", 0)), meta=meta)
            task = db.one("SELECT * FROM local_tasks WHERE cloud_id=?", (cloud_id,))
        elif task['job_model'] != 'class':
            raise ValueError("本地缓存为不受支持的旧作业。请先取消旧任务，再重新提交云端任务")
        task_meta = json.loads(task['meta_json'] or '{}')
        classes = int(cloud_task.get('classes') or task_meta.get('classes') or 0)
        total_copies = int(cloud_task.get('copies') or task['copies'] or 0)
        copies_per_class = int(cloud_task.get('copies_per_class') or task_meta.get('copies_per_class') or 0)
        if not copies_per_class and classes and total_copies % classes == 0:
            copies_per_class = total_copies // classes
        if classes < 1 or copies_per_class < 1 or classes * copies_per_class != total_copies:
            raise ValueError("班级数、每班份数与总份数不完整或不一致，请重新提交任务")
        groups = db.q("SELECT id,seq,copies FROM groups WHERE task_id=? ORDER BY seq", (task['id'],))
        if len(groups) != classes or any(group['seq'] != index or group['copies'] != copies_per_class
                                         for index, group in enumerate(groups, 1)):
            raise ValueError("本地班级作业数量或份数与云端任务不一致，请重新提交")
        ordered_printers = sorted(chosen, key=lambda pid: (str(db.one("SELECT name FROM printers WHERE id=?", (pid,))["name"]).lower(), pid))
        base, extra = divmod(len(groups), len(ordered_printers))
        cursor = 0
        for index, pid in enumerate(ordered_printers):
            count = base + (1 if index < extra else 0)
            for group in groups[cursor:cursor + count]:
                db.execute("UPDATE groups SET printer_id=? WHERE id=?", (pid, group['id']))
            cursor += count
        db.log_event("CLASS_ALLOCATION", "; ".join(
            f"{db.one('SELECT name FROM printers WHERE id=?', (pid,))['name']}:{base + (1 if i < extra else 0)}班"
            for i, pid in enumerate(ordered_printers)), task_id=task['id'])
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
