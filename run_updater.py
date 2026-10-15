"""Stable per-user launcher; packaged separately as a one-file Windows program."""
import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import time
import socket
from pathlib import Path

from app.update_core import (APP_EXE, LAUNCHER_EXE, active_release, atomic_json, data_backup,
                             prepare_install, restore_before_open, verify_manifest)


def root_path():
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "CanonPrintDesk" / "client"


def data_path():
    return Path(os.environ.get("APPDATA", str(Path.home()))) / "CanonPrintDesk"


def exclusive(root):
    import msvcrt
    root.mkdir(parents=True, exist_ok=True)
    stream = (root / "launcher.lock").open("a+b")
    stream.seek(0)
    stream.write(b"0")
    stream.flush()
    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    return stream


def wait_for_exit(pid: int, deadline=60):
    if pid <= 0:
        return
    import ctypes
    SYNCHRONIZE = 0x00100000
    handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if handle:
        try:
            if ctypes.windll.kernel32.WaitForSingleObject(handle, deadline * 1000) != 0:
                raise TimeoutError("客户端尚未退出，已取消更新")
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)


def launch(root: Path, health_file: Path | None = None):
    release = active_release(root)
    exe = root / "versions" / release["version"] / APP_EXE
    env = os.environ.copy()
    if health_file:
        env["PRINT_UPDATE_HEALTH_FILE"] = str(health_file)
    return subprocess.Popen([str(exe)], cwd=exe.parent, env=env)


def bootstrap(root: Path, source: Path, version: str, sequence: int):
    """First installation from a trusted, locally distributed complete bundle."""
    if (root / "current.json").exists():
        raise RuntimeError("客户端已安装，请使用签名升级包")
    if not (source / APP_EXE).is_file() or not (source / "_internal").is_dir():
        raise ValueError("初装包不完整")
    if not version or version != Path(version).name or sequence < 1:
        raise ValueError("初装版本号无效")
    settings = data_path() / 'client-settings.json'
    try:
        port = int(json.loads(settings.read_text(encoding='utf-8')).get('desktop_port', 18765))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        port = 18765
    with socket.socket() as probe:
        probe.settimeout(.5)
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            raise RuntimeError("请先关闭旧客户端，再运行首次安装")
    database = data_path() / "station" / "local.db"
    if database.is_file():
        backup = data_path() / "update-backups" / f"before-bootstrap-{int(time.time())}"
        data_backup(data_path(), backup)
    target = root / "versions" / version
    if target.exists():
        raise RuntimeError("目标目录已存在")
    shutil.copytree(source, target)
    shutil.copy2(source / LAUNCHER_EXE, root / LAUNCHER_EXE)
    atomic_json(root / "current.json", {"version": version, "sequence": sequence})


def install(root: Path, manifest: Path, archive: Path, public_key: bytes, pid: int):
    wait_for_exit(pid)
    doc = verify_manifest(manifest.read_bytes(), public_key)
    state = active_release(root)
    if doc["sequence"] <= state["sequence"]:
        raise ValueError("没有更高版本可安装")
    transaction = prepare_install(root, data_path(), archive, doc)
    health = root / "health-confirmed.json"
    health.unlink(missing_ok=True)
    record = json.loads(transaction.read_text(encoding="utf-8"))
    record["stage"] = "awaiting_health"
    atomic_json(transaction, record)
    child = launch(root, health)
    until = time.monotonic() + 30
    while time.monotonic() < until:
        if health.exists():
            check = json.loads(health.read_text(encoding="utf-8"))
            if check.get("version") == doc["version"] and check.get("pid") == child.pid:
                try:
                    healthy_exit = child.wait(timeout=10) == 0
                except subprocess.TimeoutExpired:
                    healthy_exit = False
                if not healthy_exit:
                    break
                transaction.unlink()
                health.unlink()
                launch(root)
                return
        if child.poll() is not None:
            break
        time.sleep(.2)
    child.terminate()
    try:
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        raise RuntimeError("新版未能退出，需人工检查数据后恢复")
    restore_before_open(root, data_path())
    launch(root)
    raise RuntimeError("新版健康检查失败，已恢复上一版")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bootstrap", type=Path)
    parser.add_argument("--version")
    parser.add_argument("--sequence", type=int, default=1)
    parser.add_argument("--install", type=Path, help="已签名的 manifest JSON")
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--wait-pid", type=int, default=0)
    args = parser.parse_args()
    root = root_path()
    guard = exclusive(root)
    try:
        if args.bootstrap:
            bootstrap(root, args.bootstrap.resolve(), args.version or "", args.sequence)
            launch(root)
        elif args.install:
            if not args.archive:
                raise ValueError("缺少升级包")
            channel = Path(__file__).resolve().parent / "app" / "update-channel.json"
            configured = json.loads(channel.read_text(encoding="utf-8"))
            public_key = base64.b64decode(configured["public_key"], validate=True)
            if len(public_key) != 32:
                raise ValueError("更新器没有可信发布公钥")
            install(root, args.install.resolve(), args.archive.resolve(),
                    public_key, args.wait_pid)
        elif (root / "update-transaction.json").exists():
            restore_before_open(root, data_path())
            launch(root)
        else:
            launch(root)
    finally:
        guard.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, str(exc), "印务台更新", 0x10)
        sys.exit(1)
