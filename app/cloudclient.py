# -*- coding: utf-8 -*-
"""
云同步客户端(代理侧): 每 SYNC_INTERVAL 秒一次:
  上报: 本地任务状态(打印中/已完成等；兼容历史待收纸) + 未送事件
  拉取: 新任务列表(含优先级) → 下载原始PDF → 本地拆组; 云端取消列表 → 本地取消
断网: 静默跳过,下个周期重试(本地自治,已拉任务照打)。
"""
import json
import os
import threading
from pathlib import Path
from urllib.parse import urljoin

import requests

from . import config, db, importer


class CloudSync:
    def __init__(self, cloud_url=None, cookie=None, client_id=None):
        self.base = (cloud_url or config.CLOUD_URL).rstrip("/")
        self.cookie = cookie or ""
        self.client_id = client_id or ""
        self.last_error = ""
        self._tick_lock = threading.Lock()
        self._download_lock = threading.Lock()
        self._active = True

    def deactivate(self):
        self._active = False

    def _headers(self):
        return {"Cookie": self.cookie, "X-Client-ID": self.client_id}

    def _request(self, method, path, **kwargs):
        # 与桌面登录一致，避开失效的环境代理；每次请求使用独立会话。
        with requests.Session() as session:
            session.trust_env = False
            return session.request(method, f"{self.base}{path}",
                                   headers=self._headers(), **kwargs)

    def tick(self):
        with self._tick_lock:
            if not self._active:
                return
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
        resp = self._request("POST", "/api/agent/sync",
                             json={"task_states": states, "client_id": self.client_id}, timeout=15)
        if resp.status_code == 401:
            raise RuntimeError("云端尚未支持登录后同步，或登录已过期")
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

        # Cache queued PDFs for this signed-in desktop client; never auto-print.
        for nt in data.get("new_tasks", []):
            if not self._active:
                break
            self._download(nt["id"], nt["pdf_name"])

    def task_action(self, cloud_task_id: int, action: str):
        response = self._request(
            "POST", f"/api/v1/tasks/{cloud_task_id}/{action}",
            json={"client_id": self.client_id}, timeout=20)
        if response.status_code == 404:
            raise RuntimeError("云端服务尚未更新打印领取接口")
        try:
            data = response.json()
        except ValueError:
            raise RuntimeError("云端返回异常，请稍后重试") from None
        if not response.ok or not data.get("ok"):
            raise ValueError(data.get("error", "云端操作失败"))
        return data

    def _download(self, cloud_task_id: int, pdf_name: str) -> Path:
        day_dir = config.UPLOAD_DIR / "queue"
        day_dir.mkdir(parents=True, exist_ok=True)
        safe = Path(pdf_name).name.replace("/", "_")
        path = day_dir / f"cloud{cloud_task_id}_{safe}"
        with self._download_lock:
            if path.exists():
                return path
            r = self._request("GET", f"/api/agent/file/{cloud_task_id}",
                              timeout=120, allow_redirects=False)
            if r.is_redirect:
                # COS 签名地址无需客户端凭据；跨域下载时不转发登录 Cookie。
                with requests.Session() as session:
                    session.trust_env = False
                    r = session.get(urljoin(self.base, r.headers["Location"]), timeout=120)
            r.raise_for_status()
            part = path.with_name(path.name + ".part")
            try:
                part.write_bytes(r.content)
                os.replace(part, path)
            finally:
                part.unlink(missing_ok=True)
            db.log_event("TASK_DOWNLOADED", f"云端任务#{cloud_task_id} PDF 已下载({safe})")
            return path

    # ---- 给本地界面展示同步健康状况 ----
    def status_line(self) -> str:
        if self.last_error:
            return f"云同步异常: {self.last_error}"
        return "云同步正常"
