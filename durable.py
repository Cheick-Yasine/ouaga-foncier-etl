"""Persistence locale et verrou OS, sans dépendance externe."""
from __future__ import annotations
import contextlib
import errno
import json
import os
import tempfile
from pathlib import Path


class AccountBusyError(RuntimeError):
    """Le verrou de ce compte appartient déjà à un autre processus."""


def _acquire_lock(stream, windows):
    try:
        if windows:
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        # Intercepter seulement l'appel de verrouillage. Un accès refusé lors
        # de l'ouverture/création du fichier reste une vraie erreur d'accès.
        if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
            raise AccountBusyError('Une collecte ou un diagnostic utilise déjà ce compte.') from exc
        raise


def atomic_json(path: Path, value, private=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=path.name + '.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        if private:
            os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


@contextlib.contextmanager
def account_lock(path: Path):
    """Le système libère le verrou même après arrêt brutal du processus."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            if path.stat().st_size == 0:
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
        _acquire_lock(stream, windows=os.name == 'nt')
        try:
            yield
        finally:
            if os.name == 'nt':
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_UN)
