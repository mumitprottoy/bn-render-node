"""Disk space, folder sizes and the graphics card's name."""

import logging
import os
import shutil
import subprocess
from pathlib import Path

from .settings import IS_MAC
from .settings import IS_WINDOWS

logger = logging.getLogger(__name__)


def free_bytes(path: Path) -> int | None:
    try:
        return shutil.disk_usage(str(path)).free
    except OSError:
        return None


def drives_to_report(root: Path) -> list[tuple[str, Path]]:
    """C: and D: on Windows (plus the root's drive if it is another); the root's volume elsewhere."""
    if IS_WINDOWS:
        drives = [('C:', Path('C:\\')), ('D:', Path('D:\\'))]
        root_drive = (root.drive or 'C:').upper()
        if root_drive not in ('C:', 'D:'):
            drives.append((root_drive, Path(root_drive + '\\')))
        return [(label, path) for label, path in drives if path.exists()]
    anchor = root if root.exists() else Path(root.anchor or '/')
    return [('Disk', anchor)]


def folder_bytes(path: Path) -> int:
    total = 0
    if not path.exists():
        return total
    for folder, _, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(folder, name)).st_size
            except OSError:
                continue
    return total


def remove_folder(path: Path) -> None:
    """Delete a folder tree; on Windows clear read-only flags that would stop it."""
    def clear_and_retry(function: object, failed_path: str, _: object) -> None:
        try:
            os.chmod(failed_path, 0o700)
            function(failed_path)
        except OSError:
            logger.warning(f'Could not delete {failed_path}')

    if path.exists():
        shutil.rmtree(path, onexc=clear_and_retry)


def gpu_name_from_system() -> str:
    """Best effort, for `bn status` when Blender's self-test has not named the device."""
    try:
        if IS_WINDOWS:
            completed = subprocess.run(
                ['powershell', '-NoProfile', '-Command',
                 '(Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name) -join ", "'],
                capture_output=True, text=True, timeout=30)
            return completed.stdout.strip()
        if IS_MAC:
            completed = subprocess.run(['system_profiler', 'SPDisplaysDataType'], capture_output=True, text=True,
                                       timeout=30)
            for line in completed.stdout.splitlines():
                if 'Chipset Model:' in line:
                    return line.split(':', 1)[1].strip()
    except (OSError, subprocess.TimeoutExpired):
        return str()
    return str()


def format_gb(byte_count: int | None) -> str:
    if byte_count is None:
        return 'unknown'
    gigabytes = byte_count / 1024 ** 3
    if gigabytes >= 100:
        return f'{gigabytes:,.0f} GB'
    return f'{gigabytes:.1f} GB'
