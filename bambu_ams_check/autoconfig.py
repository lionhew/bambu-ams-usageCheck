"""Auto-detect Bambu Studio paths / user_id / device for config.local.yaml."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .config import (
    ensure_local_config_file,
    has_access_token,
    load_config,
    load_raw_yaml,
    local_config_path,
    merge_into_local_config,
    needs_first_run_autoconfig,
)

logger = logging.getLogger(__name__)

_DIGITS_RE = re.compile(r"^\d+$")


def _path_join(base: str | Path, *parts: str) -> str:
    """Join paths; keep Windows separators when base looks like a Win path.

    Allows unit tests on Linux to mock %APPDATA% / %LOCALAPPDATA% with
    ``C:\\...`` strings without pathlib turning them into mixed separators.
    """
    base_s = str(base)
    winish = "\\" in base_s or (len(base_s) >= 2 and base_s[1] == ":")
    if winish:
        bits = [base_s.rstrip("\\/")]
        for p in parts:
            bits.append(str(p).strip("\\/"))
        return "\\".join(bits)
    return str(Path(base, *parts))




@dataclass
class AutoconfigResult:
    path: Path
    region: str = "cn"
    user_id: str | None = None
    device_id: str | None = None
    directories: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    needs_login: bool = False
    first_run: bool = False
    reset: bool = False

    def summary_lines(self) -> list[str]:
        lines = [
            f"已写入: {self.path}",
            f"region: {self.region}",
            f"cloud.user_id: {self.user_id or '(未检测到)'}",
            f"device.device_id: {self.device_id or '(未设置)'}",
            "watch.directories:",
        ]
        if self.directories:
            for d in self.directories:
                lines.append(f"  - {d}")
        else:
            lines.append("  (空 — 请手动填写切片输出目录)")
        for note in self.notes:
            lines.append(f"注意: {note}")
        if self.needs_login:
            lines.append("缺少 access_token — 请运行: python -m bambu_ams_check login --gui")
        return lines


def _env_path(name: str, environ: dict[str, str] | None = None) -> Path | None:
    env = environ if environ is not None else os.environ
    raw = env.get(name) or ""
    raw = raw.strip().strip('"')
    if not raw:
        return None
    return Path(raw)


def studio_roaming_dir(environ: dict[str, str] | None = None) -> Path | None:
    appdata = _env_path("APPDATA", environ)
    if appdata is None:
        return None
    return Path(_path_join(appdata, "BambuStudio"))


def studio_conf_path(environ: dict[str, str] | None = None) -> Path | None:
    root = studio_roaming_dir(environ)
    if root is None:
        return None
    return Path(_path_join(root, "BambuStudio.conf"))


def detect_user_id_from_studio(
    environ: dict[str, str] | None = None,
    *,
    exists: Callable[[Path], bool] | None = None,
    listdir: Callable[[Path], list[str]] | None = None,
) -> str | None:
    """Detect numeric uid from %APPDATA%\\BambuStudio\\user\\<digits>."""
    root = studio_roaming_dir(environ)
    if root is None:
        return None
    user_dir = Path(_path_join(root, "user"))
    exists_fn = exists or (lambda p: p.is_dir())
    if not exists_fn(user_dir):
        return None
    try:
        names = listdir(user_dir) if listdir else [p.name for p in user_dir.iterdir()]
    except OSError:
        return None
    digits = sorted(n for n in names if _DIGITS_RE.match(n))
    if not digits:
        return None
    # Prefer the longest / lexicographically last (usually the real cloud uid folder)
    digits.sort(key=lambda s: (len(s), s))
    return digits[-1]


def _read_studio_conf(
    path: Path | None,
    *,
    read_text: Callable[[Path], str] | None = None,
) -> dict[str, Any]:
    """Parse BambuStudio.conf; Studio often appends a ``# MD5 checksum ...`` trailer."""
    if path is None:
        return {}
    try:
        raw = read_text(path) if read_text else path.read_text(encoding="utf-8")
    except OSError:
        return {}
    # Prefer first top-level JSON object (ignore MD5 footer / extra data).
    try:
        data, _end = json.JSONDecoder().raw_decode(raw.lstrip())
    except json.JSONDecodeError:
        # Fallback: strip comment-like trailing lines then loads
        lines = []
        for line in raw.splitlines():
            if line.lstrip().startswith("#"):
                break
            lines.append(line)
        try:
            data = json.loads("\n".join(lines))
        except json.JSONDecodeError:
            logger.debug("BambuStudio.conf is not valid JSON: %s", path)
            return {}
    return data if isinstance(data, dict) else {}


def _unique_existing_dirs(
    candidates: list[str],
    *,
    exists: Callable[[Path], bool] | None = None,
) -> list[str]:
    exists_fn = exists or (lambda p: p.is_dir())
    out: list[str] = []
    seen: set[str] = set()
    for raw in candidates:
        if not raw or not str(raw).strip():
            continue
        p = Path(str(raw).strip())
        key = str(p)
        # Normalize for dedupe on Windows-ish paths
        key_norm = key.replace("/", "\\").lower()
        if key_norm in seen:
            continue
        if not exists_fn(p):
            continue
        seen.add(key_norm)
        out.append(str(p))
    return out


def detect_watch_directories(
    environ: dict[str, str] | None = None,
    *,
    exists: Callable[[Path], bool] | None = None,
    read_text: Callable[[Path], str] | None = None,
) -> list[str]:
    """Collect likely Studio export / cache dirs that currently exist."""
    conf = _read_studio_conf(studio_conf_path(environ), read_text=read_text)
    app = conf.get("app") if isinstance(conf.get("app"), dict) else {}
    recent = conf.get("recent") if isinstance(conf.get("recent"), dict) else {}

    candidates: list[str] = []
    for key in ("download_path", "last_export_path", "last_backup_path"):
        val = app.get(key)
        if isinstance(val, str) and val.strip():
            # last_backup_path may point at a dated bamboo_model subfolder — use parent chain
            candidates.append(val)
            if key == "last_backup_path" and "bamboo_model" in val.replace("\\", "/"):
                p = Path(val)
                for parent in list(p.parents)[:4]:
                    if parent.name.lower() == "bamboo_model":
                        candidates.append(str(parent))
                        break

    last_opened = recent.get("last_opened_folder")
    if isinstance(last_opened, str) and last_opened.strip():
        candidates.append(last_opened)

    local = _env_path("LOCALAPPDATA", environ)
    if local is not None:
        candidates.append(_path_join(local, "Temp", "bamboo_model"))

    roaming = studio_roaming_dir(environ)
    if roaming is not None:
        candidates.append(_path_join(roaming, "cache"))

    return _unique_existing_dirs(candidates, exists=exists)


def detect_device_id(
    environ: dict[str, str] | None = None,
    *,
    read_text: Callable[[Path], str] | None = None,
) -> str | None:
    """Optionally read user_last_selected_machine (or similar) from Studio conf."""
    conf = _read_studio_conf(studio_conf_path(environ), read_text=read_text)
    # Known / heuristic keys across Studio versions
    for key in (
        "user_last_selected_machine",
        "last_selected_machine",
        "printer_last_select",
    ):
        val = conf.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
        if isinstance(val, dict):
            for sub in ("dev_id", "device_id", "id"):
                inner = val.get(sub)
                if isinstance(inner, str) and inner.strip():
                    return inner.strip()

    # Sometimes nested under app / presets
    app = conf.get("app") if isinstance(conf.get("app"), dict) else {}
    for key in ("user_last_selected_machine", "last_selected_machine"):
        val = app.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()

    # access_code map keys are device ids
    access = conf.get("access_code")
    if isinstance(access, dict) and access:
        # Prefer a key that looks like a device serial
        keys = [str(k) for k in access.keys() if str(k).strip()]
        if len(keys) == 1:
            return keys[0]
    return None


def run_autoconfig(
    *,
    config_path: str | Path | None = None,
    region: str = "cn",
    environ: dict[str, str] | None = None,
    launch_login: bool = False,
    login_launcher: Callable[[], bool] | None = None,
    exists: Callable[[Path], bool] | None = None,
    listdir: Callable[[Path], list[str]] | None = None,
    read_text: Callable[[Path], str] | None = None,
) -> AutoconfigResult:
    """Create/update config.local.yaml with detected Studio paths. Never invents token."""
    out = ensure_local_config_file(Path(config_path) if config_path else None)
    region = (region or "cn").lower()
    if region not in {"cn", "us"}:
        region = "cn"

    user_id = detect_user_id_from_studio(environ, exists=exists, listdir=listdir)
    directories = detect_watch_directories(environ, exists=exists, read_text=read_text)
    device_id = detect_device_id(environ, read_text=read_text)

    from datetime import datetime, timezone

    updates: dict[str, Any] = {
        "region": region,
        "watch": {"directories": directories},
        "autoconfig": {
            "done": True,
            "detected_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    }
    if user_id:
        updates["cloud"] = {"user_id": user_id}
    if device_id:
        updates["device"] = {"device_id": device_id}

    merge_into_local_config(updates, path=out, drop_password=True)

    cfg = load_config(out)
    needs_login = not has_access_token(cfg)
    notes: list[str] = []
    if not directories:
        notes.append("未找到可用监视目录；请确认已安装 Bambu Studio 或手动填写 watch.directories")
    if user_id is None:
        notes.append("未在 APPDATA\\BambuStudio\\user\\<digits> 检测到 user_id")

    result = AutoconfigResult(
        path=out,
        region=region,
        user_id=user_id,
        device_id=device_id,
        directories=directories,
        notes=notes,
        needs_login=needs_login,
    )

    if needs_login and launch_login and login_launcher is not None:
        ok = bool(login_launcher())
        if ok:
            result.needs_login = not has_access_token(load_config(out))
        else:
            result.notes.append("登录未完成")
    return result


def _extract_kept_cloud(raw: dict[str, Any], *, keep_token: bool) -> dict[str, Any]:
    if not keep_token:
        return {}
    cloud = raw.get("cloud") if isinstance(raw.get("cloud"), dict) else {}
    kept: dict[str, Any] = {}
    for key in ("access_token", "refresh_token", "user_id", "account"):
        val = cloud.get(key)
        if val is not None and str(val).strip():
            kept[key] = val
    return kept


def reset_config(
    *,
    config_path: str | Path | None = None,
    keep_token: bool = False,
    region: str = "cn",
    delete_file: bool = True,
    environ: dict[str, str] | None = None,
    exists: Callable[[Path], bool] | None = None,
    listdir: Callable[[Path], list[str]] | None = None,
    read_text: Callable[[Path], str] | None = None,
    launch_login: bool = False,
    login_launcher: Callable[[], bool] | None = None,
) -> AutoconfigResult:
    """Clear autoconfig-derived local config (optionally keep cloud tokens) and re-detect.

    By default deletes ``config.local.yaml`` then recreates via autoconfig on this PC.
    With ``keep_token=True``, preserves access_token / refresh_token / user_id / account.
    """
    out = Path(config_path) if config_path else local_config_path()
    kept_cloud: dict[str, Any] = {}
    if out.is_file():
        raw = load_raw_yaml(out)
        kept_cloud = _extract_kept_cloud(raw, keep_token=keep_token)
        if delete_file:
            out.unlink()
        else:
            # Clear derived fields in place
            cleared = dict(raw)
            cleared["watch"] = dict(cleared.get("watch") or {})
            cleared["watch"]["directories"] = []
            cleared["device"] = dict(cleared.get("device") or {})
            cleared["device"]["device_id"] = ""
            cloud = dict(cleared.get("cloud") or {})
            if not keep_token:
                cloud["access_token"] = ""
                cloud["refresh_token"] = ""
                cloud["user_id"] = ""
            cleared["cloud"] = cloud
            cleared["autoconfig"] = {"done": False}
            import yaml

            with out.open("w", encoding="utf-8") as fh:
                yaml.safe_dump(cleared, fh, allow_unicode=True, sort_keys=False)

    result = run_autoconfig(
        config_path=out,
        region=region,
        environ=environ,
        exists=exists,
        listdir=listdir,
        read_text=read_text,
        launch_login=launch_login,
        login_launcher=login_launcher,
    )
    result.reset = True
    if kept_cloud:
        # Prefer freshly detected user_id; only fall back to previous login uid
        restore = {
            k: v
            for k, v in kept_cloud.items()
            if k in {"access_token", "refresh_token", "account"}
        }
        if not result.user_id and kept_cloud.get("user_id"):
            restore["user_id"] = kept_cloud["user_id"]
            result.user_id = str(kept_cloud["user_id"])
        if restore:
            merge_into_local_config({"cloud": restore}, path=out, drop_password=True)
        cfg = load_config(out)
        result.needs_login = not has_access_token(cfg)
    return result


def ensure_first_run_autoconfig(
    *,
    config_path: str | Path | None = None,
    region: str = "cn",
    environ: dict[str, str] | None = None,
    exists: Callable[[Path], bool] | None = None,
    listdir: Callable[[Path], list[str]] | None = None,
    read_text: Callable[[Path], str] | None = None,
    force: bool = False,
) -> AutoconfigResult | None:
    """Run autoconfig once when local config is missing / incomplete. Returns None if skipped."""
    if not force and not needs_first_run_autoconfig(config_path):
        return None
    result = run_autoconfig(
        config_path=config_path,
        region=region,
        environ=environ,
        exists=exists,
        listdir=listdir,
        read_text=read_text,
        launch_login=False,
    )
    result.first_run = True
    return result
