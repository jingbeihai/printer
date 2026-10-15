"""Create a signed, immutable complete client release from the PyInstaller folder."""
import argparse
import base64
import hashlib
import json
import time
import zipfile
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.update_client import VERSION, SEQUENCE
from app.update_core import APP_EXE, LAUNCHER_EXE, canonical


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--url', required=True, help='Final HTTPS URL of immutable ZIP')
    parser.add_argument('--private-key', type=Path, required=True)
    parser.add_argument('--notes', default='客户端更新')
    args = parser.parse_args()
    if not args.url.startswith('https://'):
        parser.error('升级包必须使用 HTTPS')
    if not (args.bundle / APP_EXE).is_file() or not (args.bundle / LAUNCHER_EXE).is_file():
        parser.error('请先完成客户端与启动器打包')
    metadata = args.bundle / '版本.json'
    if not metadata.is_file() or json.loads(metadata.read_text(encoding='utf-8')) != {
            'version': VERSION, 'sequence': SEQUENCE}:
        parser.error('打包目录版本与当前源码不一致，请重新打包')
    args.output.mkdir(parents=True, exist_ok=True)
    archive = args.output / f'print-desk-{VERSION}-{SEQUENCE}.zip'
    if archive.exists():
        parser.error('该版本升级包已存在；发布内容必须不可变')
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for file in sorted(args.bundle.rglob('*')):
            if not file.is_file() or file.name in (LAUNCHER_EXE, '安装客户端.ps1', '版本.json'):
                continue
            bundle.write(file, file.relative_to(args.bundle).as_posix())
    with archive.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    private = serialization.load_pem_private_key(args.private_key.read_bytes(), password=None)
    if not isinstance(private, Ed25519PrivateKey):
        parser.error('发布私钥必须为 Ed25519')
    channel = args.bundle / '_internal' / 'app' / 'update-channel.json'
    if not channel.is_file():
        parser.error('客户端包缺少更新渠道配置')
    expected = json.loads(channel.read_text(encoding='utf-8'))['public_key']
    actual = base64.b64encode(private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode('ascii')
    if expected != actual:
        parser.error('发布私钥与客户端内置公钥不匹配')
    manifest = {'format': 1, 'platform': 'windows-x64', 'version': VERSION,
                'sequence': SEQUENCE, 'expires': int(time.time()) + 180 * 86400,
                'notes': args.notes, 'url': args.url, 'size': archive.stat().st_size,
                'sha256': digest, 'min_updater': 1, 'schema_from': 1, 'schema_to': 1}
    manifest['signature'] = base64.b64encode(private.sign(canonical(manifest))).decode('ascii')
    (args.output / 'stable.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print(archive)


if __name__ == '__main__':
    main()
