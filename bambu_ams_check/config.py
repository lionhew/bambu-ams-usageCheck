"""Load / merge / save YAML config with sensible defaults."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_NAMES = (
    "config.local.yaml",
    "config.yaml",
    "config.example.yaml",
)

LOCAL_CONFIG_NAME = "config.local.yaml"

DEFAULTS: dict[str, Any] = {
    "region": "cn",
    "cloud": {
        "access_token": "",
        "refresh_token": "",
        "user_id": "",
        "account": "",
        "password": "",
    },
    "device": {
        "device_id": "",
        "spool_weight_g": 1000,
    },
    "watch": {
        "directories": [],
        "patterns": ["*.gcode.3mf", "*.3mf"],
        "debounce_seconds": 1.5,
        "poll_interval_seconds": 1.0,
    },
    "ui": {
        "show_popup": True,
    },
    "mqtt": {
        "timeout_seconds": 12,
    },
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def find_config_path(explicit: str | Path | None = None) -> Path | None:
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"Config not found: {path}")
        return path
    cwd = Path.cwd()
    for name in DEFAULT_CONFIG_NAMES:
        candidate = cwd / name
        if candidate.is_file():
            return candidate
    pkg_root = project_root()
    for name in DEFAULT_CONFIG_NAMES:
        candidate = pkg_root / name
        if candidate.is_file():
            return candidate
    return None


def local_config_path(explicit: str | Path | None = None) -> Path:
    """Path where secrets should be written (config.local.yaml).

    If ``explicit`` points at an existing file, use it. Otherwise prefer an
    existing local file under cwd / project root, else create under cwd.
    """
    if explicit:
        return Path(explicit).expanduser()
    cwd = Path.cwd()
    for base in (cwd, project_root()):
        candidate = base / LOCAL_CONFIG_NAME
        if candidate.is_file():
            return candidate
    return cwd / LOCAL_CONFIG_NAME


def example_config_path() -> Path | None:
    for base in (Path.cwd(), project_root()):
        candidate = base / "config.example.yaml"
        if candidate.is_file():
            return candidate
    return None


def load_config(explicit: str | Path | None = None) -> dict[str, Any]:
    path = find_config_path(explicit)
    data: dict[str, Any] = {}
    if path is not None:
        with path.open("r", encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"Config root must be a mapping: {path}")
        data = loaded
    return _deep_merge(DEFAULTS, data)


def load_raw_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"Config root must be a mapping: {path}")
    return loaded


def ensure_local_config_file(path: Path | None = None) -> Path:
    """Create config.local.yaml from example (or defaults) if missing."""
    out = path or local_config_path()
    if out.is_file():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    example = example_config_path()
    if example is not None:
        text = example.read_text(encoding="utf-8")
        # Strip example directory placeholders so setup can fill real paths
        out.write_text(text, encoding="utf-8")
    else:
        seed = {
            "region": "cn",
            "cloud": {
                "access_token": "",
                "refresh_token": "",
                "user_id": "",
            },
            "device": {"device_id": "", "spool_weight_g": 1000},
            "watch": {
                "directories": [],
                "patterns": ["*.gcode.3mf", "*.3mf"],
                "debounce_seconds": 1.5,
                "poll_interval_seconds": 1.0,
            },
            "ui": {"show_popup": True},
            "mqtt": {"timeout_seconds": 12},
        }
        with out.open("w", encoding="utf-8") as fh:
            yaml.safe_dump(seed, fh, allow_unicode=True, sort_keys=False)
    return out


def merge_into_local_config(
    updates: dict[str, Any],
    *,
    path: str | Path | None = None,
    drop_password: bool = True,
) -> Path:
    """Deep-merge ``updates`` into config.local.yaml and write it.

    Never invents access_token. Strips cloud.password by default so secrets
    from interactive login are not persisted as plaintext password.
    """
    out = ensure_local_config_file(Path(path) if path else None)
    existing = load_raw_yaml(out)
    merged = _deep_merge(existing, updates)
    if drop_password:
        cloud = merged.get("cloud")
        if isinstance(cloud, dict):
            cloud.pop("password", None)
    with out.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(merged, fh, allow_unicode=True, sort_keys=False)
    return out


def save_cloud_tokens(
    *,
    access_token: str,
    user_id: str | None = None,
    refresh_token: str | None = None,
    region: str | None = None,
    account: str | None = None,
    path: str | Path | None = None,
) -> Path:
    """Write access_token (+ optional user_id / refresh_token) into local config."""
    cloud: dict[str, Any] = {"access_token": access_token}
    if user_id:
        cloud["user_id"] = str(user_id)
    if refresh_token:
        cloud["refresh_token"] = refresh_token
    if account:
        cloud["account"] = account
    updates: dict[str, Any] = {"cloud": cloud}
    if region:
        updates["region"] = region
    return merge_into_local_config(updates, path=path, drop_password=True)


def mask_token(token: str | None, keep: int = 8) -> str:
    """Mask a secret for display; never return the full token."""
    if not token:
        return "(empty)"
    t = str(token)
    if len(t) <= keep * 2:
        return t[:2] + "…" + t[-2:] if len(t) > 4 else "****"
    return f"{t[:keep]}…{t[-4:]} (len={len(t)})"



def local_config_exists(explicit: str | Path | None = None) -> bool:
    """True only when config.local.yaml (or explicit path) exists on disk."""
    if explicit:
        return Path(explicit).expanduser().is_file()
    for base in (Path.cwd(), project_root()):
        if (base / LOCAL_CONFIG_NAME).is_file():
            return True
    return False


def needs_first_run_autoconfig(explicit: str | Path | None = None) -> bool:
    """Whether this PC still needs autoconfig before normal commands.

    Triggers when:
    - no writable local config file yet, or
    - local file exists but autoconfig never completed and watch.directories is empty
      (blank copy of example / empty seed).
    """
    path = local_config_path(explicit)
    if not path.is_file():
        return True
    # If caller did not pass explicit and only example.yaml exists, local_config_path
    # returns a non-existing cwd path — already handled. If a local file exists:
    raw = load_raw_yaml(path)
    auto = raw.get("autoconfig") if isinstance(raw.get("autoconfig"), dict) else {}
    if auto.get("done"):
        return False
    watch = raw.get("watch") if isinstance(raw.get("watch"), dict) else {}
    dirs = watch.get("directories") or []
    if isinstance(dirs, list) and any(str(d).strip() for d in dirs):
        return False
    return True


def has_access_token(config: dict[str, Any]) -> bool:
    cloud = config.get("cloud") or {}
    return bool(str(cloud.get("access_token") or "").strip())


def api_base_url(region: str) -> str:
    region = (region or "cn").lower()
    if region == "cn":
        return "https://api.bambulab.cn"
    return "https://api.bambulab.com"


def tfa_login_url(region: str) -> str:
    """Web sign-in TFA endpoint (HA / community convention)."""
    region = (region or "cn").lower()
    if region == "cn":
        return "https://bambulab.cn/api/sign-in/tfa"
    return "https://bambulab.com/api/sign-in/tfa"


def mqtt_host(region: str) -> str:
    region = (region or "cn").lower()
    if region == "cn":
        return "cn.mqtt.bambulab.com"
    return "us.mqtt.bambulab.com"
