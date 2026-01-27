"""
Atomic file writing with backup - Session 9.
"""

import os
import json
import shutil
import tempfile
import logging
from pathlib import Path
from datetime import datetime
from contextlib import contextmanager

logger = logging.getLogger(__name__)


class SafeFileWriter:
    def __init__(self, backup_count=5, backup_dir=None, enable_backups=True):
        self.backup_count = backup_count
        self.backup_dir = backup_dir
        self.enable_backups = enable_backups

    def _get_backup_path(self, filepath):
        filepath = Path(filepath)
        if self.backup_dir:
            backup_base = Path(self.backup_dir)
        else:
            backup_base = filepath.parent / "backups"
        backup_base.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        return backup_base / f"{filepath.stem}_{timestamp}{filepath.suffix}"

    def _create_backup(self, filepath):
        filepath = Path(filepath)
        if not filepath.exists():
            return None
        try:
            backup_path = self._get_backup_path(filepath)
            shutil.copy2(filepath, backup_path)
            return backup_path
        except Exception as e:
            logger.warning(f"Backup failed: {e}")
            return None

    def _cleanup_old_backups(self, filepath):
        filepath = Path(filepath)
        if self.backup_dir:
            backup_base = Path(self.backup_dir)
        else:
            backup_base = filepath.parent / "backups"
        if not backup_base.exists():
            return
        pattern = f"{filepath.stem}_*{filepath.suffix}"
        backups = sorted(backup_base.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in backups[self.backup_count:]:
            try:
                old.unlink()
            except Exception:
                pass

    def write_json(self, filepath, data, indent=2, ensure_ascii=False):
        filepath = Path(filepath)
        filepath.parent.mkdir(parents=True, exist_ok=True)

        if self.enable_backups and filepath.exists():
            self._create_backup(filepath)
            self._cleanup_old_backups(filepath)

        fd, temp_path = tempfile.mkstemp(suffix=filepath.suffix, prefix=f".{filepath.stem}_tmp_", dir=filepath.parent)

        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=indent, ensure_ascii=ensure_ascii)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, filepath)
            return True
        except Exception as e:
            logger.error(f"Write error: {e}")
            try:
                os.unlink(temp_path)
            except Exception:
                pass
            return False

    def read_json_safe(self, filepath, default=None):
        filepath = Path(filepath)
        try:
            if filepath.exists():
                with open(filepath, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except json.JSONDecodeError as e:
            logger.error(f"JSON error: {e}")
        except Exception as e:
            logger.error(f"Read error: {e}")

        if self.enable_backups:
            recovered = self._try_recover(filepath)
            if recovered is not None:
                return recovered
        return default

    def _try_recover(self, filepath):
        filepath = Path(filepath)
        if self.backup_dir:
            backup_base = Path(self.backup_dir)
        else:
            backup_base = filepath.parent / "backups"
        if not backup_base.exists():
            return None
        pattern = f"{filepath.stem}_*{filepath.suffix}"
        backups = sorted(backup_base.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
        for backup in backups:
            try:
                with open(backup, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                self.write_json(filepath, data)
                return data
            except Exception:
                continue
        return None


_default_writer = SafeFileWriter(backup_count=5, enable_backups=True)


def save_json_safe(filepath, data, **kwargs):
    return _default_writer.write_json(filepath, data, **kwargs)


def load_json_safe(filepath, default=None):
    return _default_writer.read_json_safe(filepath, default)


@contextmanager
def safe_open_write(filepath, mode='w'):
    filepath = Path(filepath)
    filepath.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(suffix=filepath.suffix, prefix=f".{filepath.stem}_tmp_", dir=filepath.parent)
    try:
        with os.fdopen(fd, mode, encoding='utf-8') as f:
            yield f
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, filepath)
    except Exception:
        try:
            os.unlink(temp_path)
        except Exception:
            pass
        raise
