"""Signed, whole-directory Windows client releases and crash-safe version switching."""
import base64
import hashlib
import json
import os
import shutil
import sqlite3
import re
import tempfile
import time
import urllib.request
import zipfile
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit

APP_EXE = "绍兴数知-印务台.exe"
LAUNCHER_EXE = "印务台启动器.exe"
MAX_ARCHIVE = 600 * 1024 * 1024
MAX_EXPANDED = 1200 * 1024 * 1024


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def open_direct(url: str, timeout: int):
    """Avoid a stale machine proxy, matching the desktop cloud client."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url, timeout=timeout)


def verify_manifest(raw: bytes, public_key: bytes):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    if len(raw) > 32768:
        raise ValueError("更新清单过大")
    doc = json.loads(raw)
    signature = base64.b64decode(doc.pop("signature"), validate=True)
    Ed25519PublicKey.from_public_bytes(public_key).verify(signature, canonical(doc))
    if doc.get("format") != 1 or doc.get("platform") != "windows-x64":
        raise ValueError("更新包平台或格式不匹配")
    version = doc.get("version", "")
    if not isinstance(version, str) or len(version) > 64 or not re.fullmatch(
            r'\d+\.\d+\.\d+(?:-[0-9a-z.-]+)?', version):
        raise ValueError("版本号无效")
    if not isinstance(doc.get("sequence"), int) or doc["sequence"] < 1:
        raise ValueError("发布序号无效")
    if not isinstance(doc.get("expires"), int) or doc["expires"] < time.time():
        raise ValueError("更新清单已过期")
    if not isinstance(doc.get("size"), int) or not 0 < doc["size"] <= MAX_ARCHIVE:
        raise ValueError("升级包大小无效")
    if not isinstance(doc.get("sha256"), str) or len(doc["sha256"]) != 64:
        raise ValueError("升级包摘要无效")
    if not isinstance(doc.get("url"), str) or urlsplit(doc["url"]).scheme != 'https' or not urlsplit(doc["url"]).netloc:
        raise ValueError("升级包必须使用 HTTPS")
    if doc.get("min_updater") != 1 or doc.get("schema_from") != 1 or doc.get("schema_to") != 1:
        raise ValueError("此升级包需要新版安装器或数据库迁移，请手动安装")
    return doc


def read_manifest(source: str, public_key: bytes):
    if not source.startswith("https://"):
        raise ValueError("更新清单必须使用 HTTPS")
    with open_direct(source, timeout=15) as response:
        if not response.url.startswith("https://"):
            raise ValueError("更新清单跳转到了非 HTTPS 地址")
        raw = response.read(32769)
    return verify_manifest(raw, public_key)


def download(doc: dict, destination: Path, on_progress=None):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(destination.parent).free < doc['size'] + 100 * 1024 * 1024:
        raise OSError('下载磁盘空间不足')
    temporary = destination.with_suffix(".part")
    digest = hashlib.sha256()
    count = 0
    try:
        with open_direct(doc["url"], timeout=30) as response, temporary.open("wb") as target:
            if not response.url.startswith("https://"):
                raise ValueError("升级包跳转到了非 HTTPS 地址")
            while block := response.read(1024 * 1024):
                count += len(block)
                if count > doc["size"]:
                    raise ValueError("升级包超出清单大小")
                digest.update(block)
                target.write(block)
                if on_progress:
                    on_progress(min(99, int(count * 100 / doc['size'])))
        if count != doc["size"] or digest.hexdigest() != doc["sha256"]:
            raise ValueError("升级包校验失败")
        temporary.replace(destination)
        if on_progress:
            on_progress(100)
    finally:
        temporary.unlink(missing_ok=True)


def inspect_zip(archive: Path):
    total = 0
    names = set()
    with zipfile.ZipFile(archive) as bundle:
        for item in bundle.infolist():
            name = item.filename
            if (not name or name.startswith("/") or "\\" in name or ":" in name or
                    any(part in ("", ".", "..") for part in name.rstrip("/").split("/"))):
                raise ValueError("升级包包含非法路径")
            if ((item.external_attr >> 16) & 0o170000) == 0o120000:
                raise ValueError("升级包不能包含符号链接")
            for part in name.rstrip('/').split('/'):
                if part.rstrip(' .') != part or part.split('.')[0].upper() in {
                        'CON', 'PRN', 'AUX', 'NUL', 'COM1', 'COM2', 'COM3', 'COM4',
                        'LPT1', 'LPT2', 'LPT3', 'LPT4'}:
                    raise ValueError("升级包包含 Windows 不支持的路径")
            total += item.file_size
            if total > MAX_EXPANDED or item.file_size > MAX_ARCHIVE:
                raise ValueError("升级包解压大小超限")
            folded = name.casefold().rstrip("/")
            if folded in names:
                raise ValueError("升级包包含重复路径")
            names.add(folded)
        if APP_EXE.casefold() not in names or not any(
                name.startswith("_internal/") for name in names):
            raise ValueError("升级包缺少客户端程序")
    return total


def data_backup(data_dir: Path, backup_dir: Path):
    backup_dir.mkdir(parents=True, exist_ok=False)
    settings = data_dir / "client-settings.json"
    if settings.is_file():
        shutil.copy2(settings, backup_dir / settings.name)
    database = data_dir / "station" / "local.db"
    if database.is_file():
        with closing(sqlite3.connect(str(database))) as source, closing(sqlite3.connect(str(backup_dir / "local.db"))) as target:
            source.backup(target)
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("数据库备份完整性检查失败")


def atomic_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                     prefix=".update-", delete=False) as stream:
        json.dump(data, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
        temp = Path(stream.name)
    os.replace(temp, path)


def active_release(root: Path):
    state = json.loads((root / "current.json").read_text(encoding="utf-8"))
    version = state["version"]
    if version != Path(version).name or not (root / "versions" / version / APP_EXE).is_file():
        raise RuntimeError("已安装版本损坏")
    return state


def prepare_install(root: Path, data_dir: Path, archive: Path, doc: dict):
    """Call only after signature/hash verification and confirmed client exit."""
    if archive.stat().st_size != doc["size"]:
        raise ValueError("升级包大小不匹配")
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != doc["sha256"]:
        raise ValueError("升级包摘要不匹配")
    expanded = inspect_zip(archive)
    current = active_release(root)
    if doc["sequence"] <= current["sequence"]:
        raise ValueError("只能升级到更高的发布序号")
    free = shutil.disk_usage(root).free
    if free < expanded + 100 * 1024 * 1024:
        raise OSError("安装磁盘空间不足")
    target = root / "versions" / doc["version"]
    if target.exists():
        raise ValueError("目标版本目录已存在，请先检查上次升级状态")
    transaction = root / "update-transaction.json"
    backup = data_dir / "update-backups" / f"before-{doc['version']}-{int(time.time())}"
    source_db = data_dir / 'station' / 'local.db'
    if source_db.exists() and shutil.disk_usage(data_dir).free < source_db.stat().st_size * 2 + 100 * 1024 * 1024:
        raise OSError("数据备份磁盘空间不足")
    atomic_json(transaction, {"stage": "preparing", "old": current, "new": doc["version"],
                              "backup": str(backup)})
    data_backup(data_dir, backup)
    atomic_json(transaction, {"stage": "backed_up", "old": current, "new": doc["version"],
                              "backup": str(backup)})
    with zipfile.ZipFile(archive) as bundle:
        target.mkdir(parents=True)
        bundle.extractall(target)
    if not (target / APP_EXE).is_file():
        raise RuntimeError("解压后缺少客户端程序")
    atomic_json(transaction, {"stage": "switched", "old": current, "new": doc["version"],
                              "backup": str(backup)})
    atomic_json(root / "current.json", {"version": doc["version"], "sequence": doc["sequence"]})
    return transaction


def restore_before_open(root: Path, data_dir: Path):
    transaction = root / "update-transaction.json"
    if not transaction.exists():
        return False
    state = json.loads(transaction.read_text(encoding="utf-8"))
    if state["stage"] in ("preparing", "backed_up"):
        if active_release(root) != state["old"]:
            raise RuntimeError("更新状态不一致，请人工检查")
        candidate = root / "versions" / state["new"]
        if state["new"] != Path(state["new"]).name or not candidate.resolve().is_relative_to(
                (root / "versions").resolve()):
            raise RuntimeError("更新目录无效，已停止恢复")
        if candidate.exists():
            shutil.rmtree(candidate)
        transaction.unlink()
        return True
    if state["stage"] not in ("switched", "awaiting_health"):
        raise RuntimeError("更新中断，请人工检查更新事务记录")
    backup = Path(state["backup"])
    if not backup.resolve().is_relative_to(data_dir.resolve()) or not backup.is_dir():
        raise RuntimeError("升级备份路径无效")
    database = backup / "local.db"
    if database.is_file():
        (data_dir / "station").mkdir(parents=True, exist_ok=True)
        target = data_dir / "station" / "local.db"
        staged = target.with_suffix(".restore")
        shutil.copy2(database, staged)
        for suffix in ("-wal", "-shm"):
            Path(str(target) + suffix).unlink(missing_ok=True)
        os.replace(staged, target)
    settings = backup / "client-settings.json"
    if settings.is_file():
        shutil.copy2(settings, data_dir / settings.name)
    atomic_json(root / "current.json", state["old"])
    transaction.unlink()
    return True
