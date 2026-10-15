"""Create an Ed25519 publisher key outside the repository; print only the public key."""
import argparse
import base64
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

parser = argparse.ArgumentParser()
parser.add_argument('private_key', type=Path)
args = parser.parse_args()
destination = args.private_key.resolve()
if destination.exists():
    parser.error('私钥已存在，不会覆盖')
destination.parent.mkdir(parents=True, exist_ok=True)
key = Ed25519PrivateKey.generate()
flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
with os.fdopen(os.open(destination, flags, 0o600), 'wb') as stream:
    stream.write(key.private_bytes(serialization.Encoding.PEM,
                                   serialization.PrivateFormat.PKCS8,
                                   serialization.NoEncryption()))
public = key.public_key().public_bytes(serialization.Encoding.Raw,
                                       serialization.PublicFormat.Raw)
print('发布公钥（写入 app/update-channel.json）：', base64.b64encode(public).decode('ascii'))
print('私钥已保存：', destination)
