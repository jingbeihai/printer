# -*- coding: utf-8 -*-
"""
正式桌面端本地任务导入与派发。
一个云端班级作业对应一个本地打印作业；仅保留当前班级任务模型。
"""
import json
import re
import time
from pathlib import Path

from . import config, db
from .compose import compose_group_pdf, pdf_page_count
from .models import GroupStatus, TaskStatus

_COPIES_RE = re.compile(r"__\s*(\d+)\s*份")


def parse_filename(filename: str) -> tuple[str, "int | None"]:
    """"三年级数学期中__50份.pdf" → ("三年级数学期中", 50);识别不到份数返回 None。"""
    stem = Path(filename).stem
    m = _COPIES_RE.search(stem)
    if not m:
        return stem.strip(), None
    title = _COPIES_RE.sub("", stem).strip(" _-")
    return title or stem, int(m.group(1))


def create_local_task(cloud_id: int, title: str, pdf_path, copies: int,
                      priority: int = 0, meta: dict | None = None) -> int:
    """Create one persistent local job per class. Idempotent by cloud_id."""
    dup = db.one("SELECT id FROM local_tasks WHERE cloud_id=?", (cloud_id,))
    if dup:
        return dup["id"]
    meta = meta or {}
    pdf_path = Path(pdf_path)
    pages = pdf_page_count(pdf_path)
    classes = int(meta.get("classes") or 0)
    per_class = int(meta.get("copies_per_class") or 0)
    if classes < 1 or per_class < 1 or classes * per_class != copies:
        raise ValueError("班级数、每班份数与总份数不一致，请重新提交任务")
    chunks = [per_class] * classes
    n = len(chunks)

    prepared = []
    for i, c in enumerate(chunks, 1):
        label = f"第 {i} 班"
        composed = config.COMPOSED_DIR / f"task{cloud_id}_class{i:04d}.pdf"
        _, sheets = compose_group_pdf(pdf_path, composed, title, label, c)
        prepared.append((i, label, c, sheets, composed))

    tid = db.execute(
        "INSERT INTO local_tasks(cloud_id,title,meta_json,copies,pages,pdf_path,"
        "status,priority,created_at,job_model) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (cloud_id, title, json.dumps(meta, ensure_ascii=False), copies, pages,
         str(pdf_path), TaskStatus.QUEUED.value, priority, time.time(),
         "class"),
    )
    for i, label, c, sheets, composed in prepared:
        db.execute(
            "INSERT INTO groups(task_id,seq,label,pdf_path,pages,copies,sheets,"
            "composed_path,status) VALUES(?,?,?,?,?,?,?,?,?)",
            (tid, i, label, str(pdf_path), pages, c, sheets, str(composed),
             GroupStatus.QUEUED.value),
        )
    db.log_event("TASK_SYNCED",
                 f"云端任务#{cloud_id}《{title}》{pages}页×{copies}份,生成{n}个班级作业", task_id=tid)
    maybe_release_next_task()
    return tid
# ---------------- 闸口 ----------------

def maybe_release_next_task():
    """Enable only class jobs explicitly requested by the user."""
    for task in db.q("SELECT id,title FROM local_tasks WHERE status='QUEUED' AND print_requested=1"):
        db.execute("UPDATE local_tasks SET status='PRINTING' WHERE id=?", (task['id'],))
        db.log_event("TASK_STARTED", f"任务《{task['title']}》已启用打印", task_id=task['id'])


def on_group_done(group_id: int):
    g = db.one("SELECT * FROM groups WHERE id=?", (group_id,))
    if not g:
        return
    tid = g["task_id"]
    remaining = db.one("SELECT COUNT(*) c FROM groups WHERE task_id=? AND status NOT IN ('DONE')",
                       (tid,))["c"]
    if remaining:
        return
    db.execute("UPDATE local_tasks SET status='AWAITING_PICKUP' WHERE id=?", (tid,))
    db.log_event("TASK_AWAITING_PICKUP", "全部小组完成,等待收纸确认", task_id=tid)


def confirm_pickup(local_task_id: int) -> bool:
    t = db.one("SELECT * FROM local_tasks WHERE id=?", (local_task_id,))
    if not t or t["status"] != "AWAITING_PICKUP":
        return False
    db.execute("UPDATE local_tasks SET status='DONE', done_at=? WHERE id=?",
               (time.time(), local_task_id))
    db.log_event("TASK_PICKED_UP", f"任务《{t['title']}》已收纸", task_id=local_task_id)
    maybe_release_next_task()
    return True


def cancel_by_cloud_ids(canceled_cloud_ids: list[int]):
    """云端取消 → 本地停打并作废(当前小组停打由执行器在状态机中处理)。"""
    for cid in canceled_cloud_ids:
        t = db.one("SELECT * FROM local_tasks WHERE cloud_id=? AND status NOT IN ('DONE','CANCELED')",
                   (cid,))
        if not t:
            continue
        db.execute("UPDATE local_tasks SET status='CANCELED' WHERE id=?", (t["id"],))
        db.execute("UPDATE groups SET status='PAUSED' WHERE task_id=? AND status='QUEUED'",
                   (t["id"],))
        db.log_event("TASK_CANCELED", f"云端取消任务#{cid}", task_id=t["id"])
    maybe_release_next_task()
