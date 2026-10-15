import base64
import hashlib
import json
import sqlite3
import tempfile
import time
import unittest
import zipfile
import subprocess
import sys
from contextlib import closing
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from app.update_core import (APP_EXE, active_release, atomic_json, canonical,
                             inspect_zip, prepare_install, restore_before_open,
                             verify_manifest)
from run_updater import bootstrap


class UpdateCoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.key = Ed25519PrivateKey.generate()
        self.public = self.key.public_key().public_bytes_raw()

    def manifest(self, archive):
        document = {'format': 1, 'platform': 'windows-x64', 'version': '0.2.0',
                    'sequence': 2, 'expires': int(time.time()) + 3600,
                    'notes': 'test', 'url': 'https://example.test/release.zip',
                    'size': archive.stat().st_size,
                    'sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
                    'min_updater': 1, 'schema_from': 1, 'schema_to': 1}
        document['signature'] = base64.b64encode(self.key.sign(canonical(document))).decode()
        return document

    def test_signature_rejects_modified_release(self):
        archive = self.base / 'release.zip'
        archive.write_bytes(b'archive')
        document = self.manifest(archive)
        self.assertEqual(verify_manifest(json.dumps(document).encode(), self.public)['sequence'], 2)
        document['url'] = 'https://attacker.test/release.zip'
        with self.assertRaises(Exception):
            verify_manifest(json.dumps(document).encode(), self.public)

    def test_zip_rejects_escape_and_symlink(self):
        archive = self.base / 'bad.zip'
        with zipfile.ZipFile(archive, 'w') as bundle:
            bundle.writestr('../escape.txt', 'bad')
            bundle.writestr(APP_EXE, 'exe')
        with self.assertRaises(ValueError):
            inspect_zip(archive)

    def test_install_and_restore_database_before_business_opens(self):
        root = self.base / 'client'
        old = root / 'versions' / '0.1.0'
        old.mkdir(parents=True)
        (old / APP_EXE).write_bytes(b'old')
        atomic_json(root / 'current.json', {'version': '0.1.0', 'sequence': 1})
        data = self.base / 'data'
        (data / 'station').mkdir(parents=True)
        database = data / 'station' / 'local.db'
        with closing(sqlite3.connect(database)) as connection:
            connection.execute('CREATE TABLE items(value TEXT)')
            connection.execute("INSERT INTO items VALUES('before')")
            connection.commit()
        archive = self.base / 'release.zip'
        with zipfile.ZipFile(archive, 'w') as bundle:
            bundle.writestr(APP_EXE, b'new')
            bundle.writestr('_internal/library.dll', b'dll')
        transaction = prepare_install(root, data, archive, self.manifest(archive))
        self.assertTrue(transaction.exists())
        self.assertEqual(active_release(root)['version'], '0.2.0')
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("UPDATE items SET value='after'")
            connection.commit()
        self.assertTrue(restore_before_open(root, data))
        self.assertEqual(active_release(root)['version'], '0.1.0')
        with closing(sqlite3.connect(database)) as connection:
            self.assertEqual(connection.execute('SELECT value FROM items').fetchone()[0], 'before')

    def test_interrupted_extraction_does_not_switch_version(self):
        root = self.base / 'client'
        old = root / 'versions' / '0.1.0'
        old.mkdir(parents=True)
        (old / APP_EXE).write_bytes(b'old')
        atomic_json(root / 'current.json', {'version': '0.1.0', 'sequence': 1})
        partial = root / 'versions' / '0.2.0'
        partial.mkdir()
        (partial / 'unfinished').write_text('partial')
        atomic_json(root / 'update-transaction.json',
                    {'stage': 'backed_up', 'old': {'version': '0.1.0', 'sequence': 1},
                     'new': '0.2.0', 'backup': str(self.base / 'backup')})
        self.assertTrue(restore_before_open(root, self.base / 'data'))
        self.assertFalse(partial.exists())
        self.assertEqual(active_release(root)['version'], '0.1.0')

    def test_release_tool_signs_complete_bundle(self):
        bundle = self.base / 'bundle'
        channel = bundle / '_internal' / 'app'
        channel.mkdir(parents=True)
        (bundle / APP_EXE).write_bytes(b'client')
        (bundle / '印务台启动器.exe').write_bytes(b'launcher')
        (bundle / '版本.json').write_text(json.dumps({'version': '0.1.0', 'sequence': 1}), encoding='utf-8')
        (channel / 'update-channel.json').write_text(json.dumps({
            'manifest_url': 'https://example.test/stable.json',
            'public_key': base64.b64encode(self.public).decode()}), encoding='utf-8')
        (bundle / '_internal' / 'lib.bin').write_bytes(b'library')
        private = self.base / 'private.pem'
        private.write_bytes(self.key.private_bytes(serialization.Encoding.PEM,
                                                   serialization.PrivateFormat.PKCS8,
                                                   serialization.NoEncryption()))
        output = self.base / 'release'
        subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / 'tools' / 'make_release.py'),
                        '--bundle', str(bundle), '--output', str(output),
                        '--url', 'https://example.test/client.zip', '--private-key', str(private)],
                       check=True, capture_output=True)
        document = verify_manifest((output / 'stable.json').read_bytes(), self.public)
        archive = output / 'print-desk-0.1.0-1.zip'
        self.assertEqual(document['sha256'], hashlib.sha256(archive.read_bytes()).hexdigest())
        inspect_zip(archive)

    def test_first_install_places_stable_launcher_outside_version(self):
        source = self.base / 'source'
        (source / '_internal').mkdir(parents=True)
        (source / APP_EXE).write_bytes(b'client')
        (source / '印务台启动器.exe').write_bytes(b'launcher')
        root = self.base / 'installation'
        from unittest.mock import patch
        with patch.dict('os.environ', {'APPDATA': str(self.base / 'data')}), patch('run_updater.socket.socket') as sockets:
            sockets.return_value.__enter__.return_value.connect_ex.return_value = 1
            bootstrap(root, source, '0.1.0', 1)
        self.assertEqual(active_release(root)['version'], '0.1.0')
        self.assertTrue((root / '印务台启动器.exe').is_file())


if __name__ == '__main__':
    unittest.main()
