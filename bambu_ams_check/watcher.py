"""Watch directories for new/changed .gcode.3mf with debounce (stdlib polling)."""

from __future__ import annotations

import fnmatch
import logging
import time
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)


def _iter_matches(directories: list[str], patterns: list[str]) -> dict[str, float]:
    found: dict[str, float] = {}
    for d in directories:
        root = Path(d)
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            name = path.name
            if any(fnmatch.fnmatch(name, pat) for pat in patterns):
                try:
                    found[str(path.resolve())] = path.stat().st_mtime
                except OSError:
                    continue
    return found


def _file_stable(path: Path, settle: float = 0.4) -> bool:
    """Return True if size/mtime unchanged across a short settle window."""
    try:
        s1 = path.stat()
    except OSError:
        return False
    time.sleep(settle)
    try:
        s2 = path.stat()
    except OSError:
        return False
    return s1.st_size == s2.st_size and s1.st_mtime == s2.st_mtime and s2.st_size > 0


def watch_directories(
    directories: list[str],
    callback: Callable[[Path], None],
    *,
    patterns: list[str] | None = None,
    debounce_seconds: float = 1.5,
    poll_interval_seconds: float = 1.0,
    stop_flag: Callable[[], bool] | None = None,
) -> None:
    """Poll directories; invoke callback for new or updated matching files.

    Ignores incomplete writes by requiring stable size/mtime after debounce.
    """
    patterns = patterns or ["*.gcode.3mf", "*.3mf"]
    known = _iter_matches(directories, patterns)
    pending: dict[str, float] = {}  # path -> first_seen_change_time

    logger.info(
        "Watching %s patterns=%s debounce=%.1fs",
        directories,
        patterns,
        debounce_seconds,
    )

    while True:
        if stop_flag and stop_flag():
            break
        current = _iter_matches(directories, patterns)
        now = time.time()

        for path_str, mtime in current.items():
            prev = known.get(path_str)
            if prev is None or mtime > prev + 1e-6:
                pending.setdefault(path_str, now)

        # Removed files
        for path_str in list(known):
            if path_str not in current:
                known.pop(path_str, None)
                pending.pop(path_str, None)

        ready: list[str] = []
        for path_str, first in list(pending.items()):
            if now - first < debounce_seconds:
                continue
            path = Path(path_str)
            if not path.is_file():
                pending.pop(path_str, None)
                continue
            if not _file_stable(path):
                pending[path_str] = now  # still writing
                continue
            ready.append(path_str)

        for path_str in ready:
            pending.pop(path_str, None)
            try:
                known[path_str] = Path(path_str).stat().st_mtime
            except OSError:
                continue
            try:
                callback(Path(path_str))
            except Exception:  # noqa: BLE001
                logger.exception("Callback failed for %s", path_str)

        time.sleep(max(0.2, float(poll_interval_seconds)))
