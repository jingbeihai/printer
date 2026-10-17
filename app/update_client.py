"""Desktop-only update discovery and handoff to the stable launcher."""
import base64
import json
import os
import subprocess
import sys
import threading
import hashlib
from pathlib import Path

from .update_core import (LAUNCHER_EXE, download, open_direct, read_manifest,
                          verify_manifest)

VERSION = "0.1.0"
SEQUENCE = 1


class UpdateClient:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.lock = threading.Lock()
        self.transfer_lock = threading.Lock()
        self.doc = None
        self.offline_raw = None
        self.progress = 0
        self.manifest_url = ""
        self.public_key = b""
        config_file = Path(__file__).with_name("update-channel.json")
        if config_file.is_file():
            config = json.loads(config_file.read_text(encoding="utf-8"))
            self.manifest_url = config.get("manifest_url", "")
            self.public_key = base64.b64decode(config.get("public_key", ""), validate=True)

    @property
    def enabled(self):
        return self.disabled_reason is None

    @property
    def disabled_reason(self):
        if not getattr(sys, "frozen", False):
            return "development"
        if not self.manifest_url or len(self.public_key) != 32:
            return "channel"
        if not (self.install_root / LAUNCHER_EXE).is_file() or not (self.install_root / "current.json").is_file():
            return "launcher"
        return None

    @property
    def install_root(self):
        return Path(sys.executable).resolve().parent.parent.parent

    def status(self):
        return {"version": VERSION, "sequence": SEQUENCE, "enabled": self.enabled,
                "disabled_reason": self.disabled_reason,
                "download_percent": self.progress,
                "update": self.doc and {key: self.doc.get(key) for key in
                                         ("version", "sequence", "notes", "size")}}

    def check(self):
        if not self.enabled:
            raise ValueError("此客户端尚未配置更新渠道或未通过启动器安装")
        doc = read_manifest(self.manifest_url, self.public_key)
        with self.lock:
            self.doc = doc if doc["sequence"] > SEQUENCE else None
            self.offline_raw = None
        return self.status()

    def import_offline(self, manifest_raw: bytes, archive: Path):
        if not self.enabled:
            raise ValueError("请先通过启动器安装客户端")
        doc = verify_manifest(manifest_raw, self.public_key)
        if doc['sequence'] <= SEQUENCE:
            raise ValueError("离线包不是更新版本")
        with archive.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        if archive.stat().st_size != doc['size'] or digest != doc['sha256']:
            raise ValueError("离线包校验失败")
        destination = self.data_dir / 'updates' / f"{doc['version']}.zip"
        with self.transfer_lock:
            destination.parent.mkdir(parents=True, exist_ok=True)
            archive.replace(destination)
            with self.lock:
                self.doc = doc
                self.offline_raw = manifest_raw
        return self.status()

    def download(self):
        with self.lock:
            doc = self.doc
        if not doc:
            raise ValueError("请先检查更新")
        destination = self.data_dir / "updates" / f"{doc['version']}.zip"
        with self.transfer_lock:
            if not destination.is_file():
                self.progress = 0
                try:
                    download(doc, destination, lambda percent: setattr(self, 'progress', percent))
                except Exception:
                    self.progress = 0
                    raise
            else:
                self.progress = 100
        return {"ok": True, "ready": True, "version": doc["version"]}

    def ready_install(self):
        with self.lock:
            doc = self.doc
        if not doc:
            raise ValueError("请先检查更新")
        archive = self.data_dir / "updates" / f"{doc['version']}.zip"
        if not archive.is_file():
            raise ValueError("请先下载升级包")
        manifest = self.data_dir / "updates" / f"{doc['version']}.json"
        if self.offline_raw is not None:
            raw = self.offline_raw
        else:
            with open_direct(self.manifest_url, timeout=15) as response:
                if not response.url.startswith("https://"):
                    raise ValueError("更新清单跳转到了非 HTTPS 地址")
                raw = response.read(32769)
        checked = verify_manifest(raw, self.public_key)
        if checked["sequence"] != doc["sequence"] or checked["sha256"] != doc["sha256"]:
            raise ValueError("发布清单已变化，请重新检查更新")
        with archive.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if archive.stat().st_size != doc["size"] or digest != doc["sha256"]:
            raise ValueError("下载文件校验失败，请重新下载")
        manifest.write_bytes(raw)
        return manifest, archive

    def install(self, manifest: Path, archive: Path):
        subprocess.Popen([str(self.install_root / LAUNCHER_EXE), "--install", str(manifest),
                          "--archive", str(archive),
                          "--wait-pid", str(os.getpid())], cwd=self.install_root,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
