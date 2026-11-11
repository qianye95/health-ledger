"""Encrypt the private ledger archive with a server-local AES-GCM key."""
from datetime import datetime, timezone
import fcntl
import os
from pathlib import Path
import secrets
import tarfile

from Crypto.Cipher import AES
from ledger import STATE

HEADER = b"HEALTH-LEDGER-1\n"
KEY = Path.home() / ".codex/secrets/health-backup.key"
PARTS = ("ledger.sqlite3", "raw", "reports", "profile.json")


class EncryptedStream:
    def __init__(self, file, cipher):
        self.file = file
        self.cipher = cipher

    def write(self, data):
        return self.file.write(self.cipher.encrypt(data))


def get_key():
    KEY.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(KEY, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(descriptor, "wb") as file:
            file.write(secrets.token_bytes(32))
    info = KEY.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PermissionError("备份密钥须由运行用户拥有，且权限为 600")
    key = KEY.read_bytes()
    if len(key) != 32:
        raise ValueError("备份密钥格式不正确")
    return key


def backup():
    os.umask(0o077)
    with (STATE / "ledger.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        folder = STATE / "backups"
        folder.mkdir(exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        target = folder / f"{stamp}.tar.gz.gcm"
        temp = target.with_suffix(".tmp")
        cipher = AES.new(get_key(), AES.MODE_GCM, nonce=secrets.token_bytes(12))
        cipher.update(HEADER)
        try:
            with temp.open("xb") as file:
                file.write(HEADER + cipher.nonce)
                with tarfile.open(mode="w|gz", fileobj=EncryptedStream(file, cipher)) as archive:
                    for part in PARTS:
                        path = STATE / part
                        if path.exists():
                            archive.add(path, arcname=part)
                file.write(cipher.digest())
                file.flush()
                os.fsync(file.fileno())
            temp.replace(target)
        finally:
            temp.unlink(missing_ok=True)
        print("总账加密备份已保存")


if __name__ == "__main__":
    backup()
