# -*- coding: utf-8 -*-
"""
云同步客户端(代理侧): 每 SYNC_INTERVAL 秒一次:
  上报: 本地任务状态(打印中/待收纸/已完成) + 未送事件
  拉取: 新任务列表(含优先级) → 下载原始PDF → 本地拆组; 云端取消列表 → 本地取消
断网: 静默跳过,下个周期重试(本地自治,已拉任务照打)。
"""
import json
import time
from pathlib import Path

import requests

from . import config, db, importer


class CloudSync:
    def __init__(self, cloud_url=None, token=None):
        self.base = (cloud_url or config.CLOUD_URL).rstrip("/")
        self.token = token or config.AGENT_TOKEN
        self.last_error = ""

    def _headers(self):
        return {"X-Agent-Token": self.token}

    def tick(self):
        try:
            self._do_sync()
            self.last_error = ""
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)

    def _do_sync(self):
        # ---- 上报本地任务状态 ----
        states = {}
        for t in db.q("SELECT cloud_id, status FROM local_tasks"):
            states[str(t["cloud_id"])] = t["status"]
        resp = requests.post(f"{self.base}/api/agent/sync",
                             json={"task_states": states},
                             headers=self._headers(), timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("error", "sync rejected"))

        # ---- 取消 ----
        importer.cancel_by_cloud_ids(data.get("canceled", []))

        # ---- 优先级更新 ----
        for cid, pri in (data.get("priorities") or {}).items():
            db.execute("UPDATE local_tasks SET priority=? WHERE cloud_id=?",
                       (int(pri), int(cid)))

        # ---- 新任务 ----
        for nt in data.get("new_tasks", []):
            if db.one("SELECT id FROM local_tasks WHERE cloud_id=?", (nt["id"],)):
                continue
            pdf_path = self._download(nt["id"], nt["pdf_name"])
            meta = {k: nt.get(k, "") for k in
                    ("school", "grade", "classes", "note", "source_user")}
            importer.create_local_task(nt["id"], nt["title"], pdf_path,
                                       int(nt["copies"]),
                                       priority=int(nt.get("priority", 0)), meta=meta)

    def _download(self, cloud_task_id: int, pdf_name: str) -> Path:
        day_dir = config.UPLOAD_DIR / time.strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        safe = Path(pdf_name).name.replace("/", "_")
        path = day_dir / f"cloud{cloud_task_id}_{safe}"
        if path.exists():
            return path
        r = requests.get(f"{self.base}/api/agent/file/{cloud_task_id}",
                         headers=self._headers(), timeout=120)
        r.raise_for_status()
        path.write_bytes(r.content)
        db.log_event("TASK_DOWNLOADED", f"云端任务#{cloud_task_id} PDF 已下载({safe})")
        return path

    # ---- 给本地界面展示同步健康状况 ----
    def status_line(self) -> str:
        if self.last_error:
            return f"云同步异常: {self.last_error}"
        return "云同步正常"
