"""Chromium CDP helpers: find Chrome/Edge, launch with isolated profile, poll token cookie.

Uses HTTP ``/json`` plus a short WebSocket CDP session (Network.getCookies /
Runtime.evaluate). Never reads the user's default browser profile on disk.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

import requests

logger = logging.getLogger(__name__)

BROWSER_PROFILE_DIRNAME = ".browser-profile"

# Optional websocket-client; imported lazily so unit tests for pure helpers need no WS.
def _ws_create_connection(url: str, timeout: float = 10.0):
    try:
        from websocket import create_connection  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "缺少 websocket-client。请执行: pip install websocket-client"
        ) from exc
    return create_connection(url, timeout=timeout)


def _candidate_chromium_paths() -> list[Path]:
    """Return likely Chrome / Edge executable paths for the current OS."""
    paths: list[Path] = []
    env_override = os.environ.get("BAMBU_AMS_BROWSER") or os.environ.get("CHROME_PATH")
    if env_override:
        paths.append(Path(env_override))

    pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    pf86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
    local = os.environ.get("LOCALAPPDATA", "")

    win_candidates = [
        Path(pf) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(pf86) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(local) / "Google" / "Chrome" / "Application" / "chrome.exe" if local else None,
        Path(pf) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        Path(pf86) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        Path(local) / "Microsoft" / "Edge" / "Application" / "msedge.exe" if local else None,
    ]
    for p in win_candidates:
        if p is not None:
            paths.append(p)

    # macOS / Linux fallbacks (dev machines)
    paths.extend(
        [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
            Path("/usr/bin/google-chrome"),
            Path("/usr/bin/google-chrome-stable"),
            Path("/usr/bin/chromium"),
            Path("/usr/bin/chromium-browser"),
            Path("/usr/bin/microsoft-edge"),
            Path("/snap/bin/chromium"),
        ]
    )
    # de-dupe preserving order
    seen: set[str] = set()
    out: list[Path] = []
    for p in paths:
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def find_chromium_executable() -> str | None:
    """Return path to Chrome or Edge if found, else None."""
    for p in _candidate_chromium_paths():
        try:
            if p.is_file():
                return str(p)
        except OSError:
            continue
    return None


def find_free_port() -> int:
    """Bind to port 0 and return the OS-assigned free TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def project_browser_profile_dir(project_root: Path | None = None) -> Path:
    root = project_root or Path(__file__).resolve().parent.parent
    return root / BROWSER_PROFILE_DIRNAME


def build_chromium_launch_args(
    *,
    executable: str,
    user_data_dir: str | Path,
    port: int,
    url: str,
) -> list[str]:
    """CLI args for an isolated Chromium with remote debugging (not the user profile)."""
    return [
        executable,
        f"--remote-debugging-port={int(port)}",
        f"--user-data-dir={str(user_data_dir)}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
        str(url),
    ]


def parse_cdp_targets(raw: list[dict[str, Any]] | list[Any]) -> list[dict[str, Any]]:
    """Keep only page targets that expose a WebSocket debugger URL."""
    pages: list[dict[str, Any]] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "page":
            continue
        ws = item.get("webSocketDebuggerUrl")
        if not ws:
            continue
        pages.append(item)
    return pages


def pick_page_websocket_url(
    targets: list[dict[str, Any]],
    *,
    prefer_substr: str | None = None,
) -> str | None:
    """Prefer a page whose URL contains prefer_substr; else first page WS URL."""
    pages = parse_cdp_targets(targets)
    if not pages:
        # allow callers to pass already-filtered or raw list
        pages = [t for t in targets if isinstance(t, dict) and t.get("webSocketDebuggerUrl")]
        pages = [t for t in pages if t.get("type", "page") == "page"]
    if prefer_substr:
        prefer = prefer_substr.lower()
        for p in pages:
            if prefer in str(p.get("url") or "").lower():
                return str(p["webSocketDebuggerUrl"])
    if pages:
        return str(pages[0]["webSocketDebuggerUrl"])
    return None


def token_from_cdp_cookies(
    cookies: list[dict[str, Any]] | None,
    *,
    domain_substr: str = "makerworld",
) -> str | None:
    """Extract MakerWorld ``token`` cookie from a CDP cookie list."""
    if not cookies:
        return None
    needle = domain_substr.lower()
    for c in cookies:
        if not isinstance(c, dict):
            continue
        if str(c.get("name") or "").lower() != "token":
            continue
        domain = str(c.get("domain") or "").lower()
        if needle and needle not in domain:
            continue
        val = str(c.get("value") or "").strip()
        if val:
            return val
    return None


def token_from_document_cookie(cookie_str: str | None) -> str | None:
    """Parse token from a document.cookie string."""
    from .browser_login import extract_access_token

    return extract_access_token(cookie_str)


def decode_cdp_ws_payload(raw: str | bytes) -> dict[str, Any]:
    """Decode one CDP JSON message (text frame payload)."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("CDP payload is not an object")
    return data


def fetch_cdp_targets(debug_port: int, *, timeout: float = 3.0) -> list[dict[str, Any]]:
    url = f"http://127.0.0.1:{int(debug_port)}/json"
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        return []
    return [x for x in data if isinstance(x, dict)]


def cdp_send(
    ws: Any,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    msg_id: int = 1,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Send one CDP command and wait for the matching id response (skip events)."""
    payload = {"id": int(msg_id), "method": method}
    if params is not None:
        payload["params"] = params
    ws.send(json.dumps(payload))
    deadline = time.time() + float(timeout)
    while time.time() < deadline:
        remaining = max(0.1, deadline - time.time())
        try:
            ws.settimeout(remaining)
        except Exception:  # noqa: BLE001
            pass
        raw = ws.recv()
        msg = decode_cdp_ws_payload(raw)
        if msg.get("id") == msg_id:
            if "error" in msg:
                raise RuntimeError(f"CDP {method} error: {msg['error']}")
            result = msg.get("result")
            return result if isinstance(result, dict) else {"value": result}
    raise TimeoutError(f"CDP {method} timed out")


def read_token_via_websocket(
    ws_url: str,
    *,
    cookie_urls: list[str],
    domain_substr: str = "makerworld",
    timeout: float = 10.0,
) -> str | None:
    """Connect to a page's debugger WS and try Network.getCookies then document.cookie."""
    ws = _ws_create_connection(ws_url, timeout=timeout)
    try:
        msg_id = 1
        try:
            result = cdp_send(
                ws,
                "Network.getCookies",
                {"urls": cookie_urls},
                msg_id=msg_id,
                timeout=timeout,
            )
            token = token_from_cdp_cookies(
                result.get("cookies") if isinstance(result, dict) else None,
                domain_substr=domain_substr,
            )
            if token:
                return token
        except Exception as exc:  # noqa: BLE001
            logger.debug("Network.getCookies failed: %s", exc)

        msg_id = 2
        try:
            result = cdp_send(
                ws,
                "Runtime.evaluate",
                {"expression": "document.cookie", "returnByValue": True},
                msg_id=msg_id,
                timeout=timeout,
            )
            value = None
            if isinstance(result, dict):
                remote = result.get("result") or {}
                if isinstance(remote, dict):
                    value = remote.get("value")
            token = token_from_document_cookie(str(value) if value is not None else None)
            if token:
                return token
        except Exception as exc:  # noqa: BLE001
            logger.debug("Runtime.evaluate document.cookie failed: %s", exc)
        return None
    finally:
        try:
            ws.close()
        except Exception:  # noqa: BLE001
            pass


def poll_token_from_cdp(
    debug_port: int,
    *,
    prefer_url_substr: str,
    cookie_urls: list[str],
    domain_substr: str = "makerworld",
    timeout_seconds: float = 600.0,
    poll_interval: float = 1.5,
    should_stop: Callable[[], bool] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    fetch_targets: Callable[[int], list[dict[str, Any]]] | None = None,
    read_token: Callable[..., str | None] | None = None,
) -> str | None:
    """Poll Chromium until MakerWorld token cookie appears or timeout / should_stop."""
    fetch = fetch_targets or fetch_cdp_targets
    reader = read_token or read_token_via_websocket
    deadline = time.time() + float(timeout_seconds)
    while time.time() < deadline:
        if should_stop and should_stop():
            return None
        try:
            targets = fetch(debug_port)
        except Exception as exc:  # noqa: BLE001
            logger.debug("CDP /json not ready: %s", exc)
            targets = []
        ws_url = pick_page_websocket_url(targets, prefer_substr=prefer_url_substr)
        if not ws_url and targets:
            ws_url = pick_page_websocket_url(targets, prefer_substr=None)
        if ws_url:
            try:
                token = reader(
                    ws_url,
                    cookie_urls=cookie_urls,
                    domain_substr=domain_substr,
                )
                if token:
                    return token
            except Exception as exc:  # noqa: BLE001
                logger.debug("CDP read failed: %s", exc)
        sleep_fn(poll_interval)
    return None


def wait_cdp_ready(debug_port: int, *, timeout: float = 30.0, interval: float = 0.3) -> bool:
    deadline = time.time() + float(timeout)
    while time.time() < deadline:
        try:
            fetch_cdp_targets(debug_port, timeout=1.0)
            return True
        except Exception:  # noqa: BLE001
            time.sleep(interval)
    return False


def launch_chromium_isolated(
    *,
    login_url: str,
    user_data_dir: Path | str | None = None,
    port: int | None = None,
    executable: str | None = None,
) -> tuple[subprocess.Popen[Any], int, str]:
    """Launch Chrome/Edge with isolated profile + debugging. Returns (proc, port, exe)."""
    exe = executable or find_chromium_executable()
    if not exe:
        raise FileNotFoundError("Chrome / Edge 未找到")
    port = int(port or find_free_port())
    profile = Path(user_data_dir) if user_data_dir else project_browser_profile_dir()
    profile.mkdir(parents=True, exist_ok=True)
    args = build_chromium_launch_args(
        executable=exe,
        user_data_dir=profile,
        port=port,
        url=login_url,
    )
    # New process group friendly flags on Windows
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    proc = subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    return proc, port, exe


def open_helper_tab(debug_port: int, helper_url: str, *, timeout: float = 10.0) -> None:
    """Open the local helper page as a second tab via CDP Target.createTarget."""
    targets = fetch_cdp_targets(debug_port, timeout=timeout)
    # Prefer browser-level websocket if present
    browser_ws = None
    try:
        version = requests.get(f"http://127.0.0.1:{int(debug_port)}/json/version", timeout=timeout)
        version.raise_for_status()
        browser_ws = (version.json() or {}).get("webSocketDebuggerUrl")
    except Exception:  # noqa: BLE001
        browser_ws = None
    ws_url = browser_ws or pick_page_websocket_url(targets, prefer_substr=None)
    if not ws_url:
        raise RuntimeError("No CDP websocket for opening helper tab")
    ws = _ws_create_connection(ws_url, timeout=timeout)
    try:
        cdp_send(ws, "Target.createTarget", {"url": helper_url}, msg_id=1, timeout=timeout)
    finally:
        try:
            ws.close()
        except Exception:  # noqa: BLE001
            pass
