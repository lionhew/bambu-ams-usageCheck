"""Browser-based Bambu/MakerWorld login helper (no official OAuth redirect).

Bambu Lab does not expose a public OAuth redirect for third-party apps (HA /
OpenBambuAPI use password or pasted tokens). Community practice:

1. Prefer launching Chrome/Edge with an isolated ``--user-data-dir`` and
   ``--remote-debugging-port``, then poll Chromium CDP for the MakerWorld
   ``token`` cookie after the user completes SSO (Cloudflare/captcha stay
   in the real browser window).
2. Fallback: local 127.0.0.1 helper page (bookmarklet / paste form) with
   system ``webbrowser.open``.
3. Resolve ``user_id`` through the preference API and write config.local.yaml.

We never read cookies from the user's default browser profile on disk.
"""

from __future__ import annotations

import html
import json
import logging
import re
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

import requests

from .ams_cloud import BambuCloudClient, CloudError, LoginResult
from .config import api_base_url, load_config, local_config_path, mask_token, save_cloud_tokens

logger = logging.getLogger(__name__)

# Exact CN web SSO entry (Bambu Lab CN → MakerWorld ticket).
CN_BROWSER_LOGIN_URL = (
    "https://bambulab.cn/zh-cn/sign-in?ticket=1&to="
    "https%3A%2F%2Fmakerworld.com.cn%2Fapi%2Fsign-in%2Fticket"
    "%3Fto%3Dhttps%253A%252F%252Fmakerworld.com.cn%252Fzh"
)
US_BROWSER_LOGIN_URL = "https://makerworld.com/login"
CN_MAKERWORLD_HOME = "https://makerworld.com.cn"
US_MAKERWORLD_HOME = "https://makerworld.com"

_TOKEN_COOKIE_RE = re.compile(r"(?:^|[;\s])token=([^;]+)", re.I)
_BEARER_RE = re.compile(r"(?:Bearer\s+|Authorization:\s*Bearer\s+)(\S+)", re.I)
_JWTISH_RE = re.compile(r"\beyJ[A-Za-z0-9_\-]+=*\.[A-Za-z0-9_\-]+=*\.[A-Za-z0-9_\-+=]*\b")


@dataclass(frozen=True)
class BrowserLoginUrls:
    region: str
    makerworld_home: str
    makerworld_login: str
    preference_hint: str


def browser_login_urls(region: str | None = None) -> BrowserLoginUrls:
    """Return MakerWorld / Bambu web URLs for the given cloud region."""
    r = (region or "cn").lower()
    if r == "cn":
        home = CN_MAKERWORLD_HOME
        login = CN_BROWSER_LOGIN_URL
        pref = "https://makerworld.com.cn/api/v1/design-user-service/my/preference"
    else:
        home = US_MAKERWORLD_HOME
        login = US_BROWSER_LOGIN_URL
        pref = "https://makerworld.com/api/v1/design-user-service/my/preference"
    return BrowserLoginUrls(
        region="cn" if r == "cn" else "us",
        makerworld_home=home,
        makerworld_login=login,
        preference_hint=pref,
    )


def extract_access_token(text: str | None) -> str | None:
    """Parse an access token from cookie header, Bearer line, or raw JWT paste."""
    if not text:
        return None
    raw = str(text).strip().strip('"').strip("'")
    if not raw:
        return None

    # Full Cookie header or "token=..."
    m = _TOKEN_COOKIE_RE.search(raw)
    if m:
        return urllib.parse.unquote(m.group(1).strip())

    # token: value / access_token: value (yaml-ish)
    for key in ("access_token", "accessToken", "token"):
        m2 = re.search(rf"(?:^|[\s,;]){key}\s*[:=]\s*(\S+)", raw, re.I)
        if m2:
            return m2.group(1).strip().strip(",").strip('"').strip("'")

    m3 = _BEARER_RE.search(raw)
    if m3:
        return m3.group(1).strip().rstrip(",")

    # Bare JWT or opaque token (single line, no spaces)
    if "\n" not in raw and " " not in raw and len(raw) >= 20:
        return raw

    m4 = _JWTISH_RE.search(raw)
    if m4:
        return m4.group(0)
    return None


def validate_and_resolve_user(
    access_token: str,
    *,
    region: str = "cn",
    timeout: float = 30.0,
) -> LoginResult:
    """Validate token against preference API and return LoginResult with user_id."""
    token = extract_access_token(access_token) or str(access_token).strip()
    if not token:
        raise CloudError("空的 access_token")
    cfg = {"region": region, "cloud": {"access_token": token}}
    client = BambuCloudClient(cfg)
    client.access_token = token
    try:
        pref = client.get_preference()
    except CloudError:
        # Fallback: hit API base directly (same path)
        base = api_base_url(region)
        resp = requests.get(
            f"{base}/v1/design-user-service/my/preference",
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": "bambu-ams-check/0.1",
            },
            timeout=timeout,
        )
        if resp.status_code != 200:
            raise CloudError(
                f"Token 无效或区域不匹配 (HTTP {resp.status_code})。"
                "请确认 region=cn|us 与 MakerWorld 站点一致，且已完成浏览器登录。"
            )
        pref = resp.json()
    uid = pref.get("uid") if isinstance(pref, dict) else None
    if uid is not None:
        client.user_id = str(uid)
    return LoginResult(
        access_token=token,
        user_id=client.user_id,
        refresh_token="",
        expires_in=None,
        raw_keys=sorted(str(k) for k in (pref or {}).keys()) if isinstance(pref, dict) else [],
    )


def _bookmarklet(callback_origin: str) -> str:
    # Minimal bookmarklet: read token cookie on MakerWorld, send to local helper
    origin = callback_origin.rstrip("/")
    js = (
        "(function(){"
        "var m=document.cookie.match(/(?:^|;\\s*)token=([^;]+)/);"
        "if(!m){alert('未找到 token cookie。请确认已在 MakerWorld 登录成功。');return;}"
        f"location.href='{origin}/callback?access_token='+"
        "encodeURIComponent(decodeURIComponent(m[1]));"
        "})();"
    )
    return "javascript:" + urllib.parse.quote(js)



def _page_html(*, region: str, urls: BrowserLoginUrls, port: int, status: str = "") -> str:
    origin = f"http://127.0.0.1:{port}"
    bm = _bookmarklet(origin)
    status_block = (
        f'<p class="ok">{html.escape(status)}</p>' if status else ""
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8"/>
  <title>bambu-ams-check 浏览器登录</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 720px; margin: 2rem auto; padding: 0 1rem; }}
    code, textarea {{ font-family: ui-monospace, monospace; }}
    textarea {{ width: 100%; min-height: 5rem; }}
    .ok {{ color: #0a7a28; }}
    .warn {{ color: #a66a00; }}
    .box {{ border: 1px solid #ccc; border-radius: 8px; padding: 1rem; margin: 1rem 0; }}
    button, .btn {{ background: #0b5fff; color: #fff; border: 0; padding: .6rem 1rem;
      border-radius: 6px; cursor: pointer; text-decoration: none; display: inline-block; }}
    .secondary {{ background: #555; }}
  </style>
</head>
<body>
  <h1>拓竹云浏览器登录</h1>
  <p>区域 <strong>{html.escape(urls.region)}</strong>。Bambu <em>没有</em>面向第三方的 OAuth 回调。
  <strong>已打开浏览器，登录成功后会自动回传；无需书签。</strong>
  工具通过独立浏览器配置目录 + CDP 读取 MakerWorld 的 <code>token</code> cookie
  （即 cloud access_token）。若自动回传失败，可用下方备用方式。</p>
  {status_block}
  <div class="box">
    <h2>步骤 1 — 在已打开的窗口登录</h2>
    <p>请在工具拉起的 Chrome/Edge 窗口完成 MakerWorld / 拓竹登录。
    <a class="btn" href="{html.escape(urls.makerworld_login)}" target="_blank" rel="noopener">
      再次打开登录页</a></p>
    <p class="warn">若遇 Cloudflare / 验证码，请在浏览器中完成；本工具不会绕过风控。</p>
  </div>
  <div class="box">
    <h2>步骤 2a — 书签回传（备用）</h2>
    <p>仅当自动回传不可用时：把下面链接拖到书签栏，在 <strong>MakerWorld 标签页</strong>点击：</p>
    <p><a href="{html.escape(bm)}">发送 token 到 bambu-ams-check</a></p>
  </div>
  <div class="box">
    <h2>步骤 2b — 粘贴 token（备用）</h2>
    <p>DevTools → Application → Cookies → 选 MakerWorld 域 → 复制名为 <code>token</code> 的值
    （或整段 Cookie / <code>Authorization: Bearer …</code>）。</p>
    <form method="POST" action="/submit">
      <textarea name="token" placeholder="粘贴 token / Cookie / Bearer …"></textarea>
      <p><button type="submit">验证并保存到 config.local.yaml</button></p>
    </form>
  </div>
  <p>本地回环仅监听 {html.escape(origin)}，不会上传密码到本工具进程。</p>
</body>
</html>
"""


class _BrowserLoginState:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.result: LoginResult | None = None
        self.error: str | None = None
        self.region = "cn"
        self.config_path: str | None = None
        self.port = 0


def _make_handler(state: _BrowserLoginState) -> type[BaseHTTPRequestHandler]:
    urls = browser_login_urls(state.region)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            logger.debug("browser-login: " + fmt, *args)

        def _send(self, code: int, body: str, content_type: str = "text/html; charset=utf-8") -> None:
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path in {"/", "/index.html"}:
                self._send(200, _page_html(region=state.region, urls=urls, port=state.port))
                return
            if parsed.path == "/callback":
                qs = urllib.parse.parse_qs(parsed.query)
                token_raw = (qs.get("access_token") or qs.get("token") or [""])[0]
                self._finish_with_token(token_raw)
                return
            if parsed.path == "/status":
                payload = {
                    "done": state.done.is_set(),
                    "ok": state.result is not None,
                    "error": state.error,
                    "user_id": state.result.user_id if state.result else None,
                    "token": mask_token(state.result.access_token) if state.result else None,
                }
                self._send(200, json.dumps(payload), "application/json")
                return
            self._send(404, "not found")

        def do_POST(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode("utf-8", errors="replace") if length else ""
            if parsed.path == "/submit":
                form = urllib.parse.parse_qs(body)
                token_raw = (form.get("token") or [""])[0]
                self._finish_with_token(token_raw)
                return
            if parsed.path == "/api/token":
                try:
                    data = json.loads(body) if body else {}
                except json.JSONDecodeError:
                    data = {}
                token_raw = str(data.get("access_token") or data.get("token") or "")
                try:
                    result = self._save_token(token_raw)
                    self._send(
                        200,
                        json.dumps(
                            {
                                "ok": True,
                                "user_id": result.user_id,
                                "token": mask_token(result.access_token),
                            }
                        ),
                        "application/json",
                    )
                except CloudError as exc:
                    self._send(400, json.dumps({"ok": False, "error": str(exc)}), "application/json")
                return
            self._send(404, "not found")

        def _save_token(self, token_raw: str) -> LoginResult:
            token = extract_access_token(token_raw)
            if not token:
                raise CloudError("无法从粘贴内容解析 access_token")
            result = validate_and_resolve_user(token, region=state.region)
            save_cloud_tokens(
                access_token=result.access_token,
                user_id=result.user_id or None,
                region=state.region,
                path=local_config_path(state.config_path),
            )
            state.result = result
            state.error = None
            state.done.set()
            return result

        def _finish_with_token(self, token_raw: str) -> None:
            try:
                result = self._save_token(token_raw)
                msg = (
                    f"登录成功。user_id={html.escape(str(result.user_id))}；"
                    f"token={html.escape(mask_token(result.access_token))}。"
                    "可以关闭本页，回到终端 / 程序。"
                )
                self._send(200, _page_html(region=state.region, urls=urls, port=state.port, status=msg))
            except CloudError as exc:
                state.error = str(exc)
                msg = f"失败：{html.escape(str(exc))}"
                self._send(400, _page_html(region=state.region, urls=urls, port=state.port, status=msg))

    return Handler


def run_browser_login(
    *,
    config_path: str | None = None,
    region: str | None = None,
    open_browser: bool = True,
    timeout_seconds: float = 600.0,
    port: int = 0,
    open_url: Callable[[str], bool] | None = None,
    use_cdp: bool = True,
) -> LoginResult:
    """Start local helper, open MakerWorld, wait until token saved or timeout.

    Primary path: launch Chrome/Edge with isolated profile + CDP cookie poll.
    Fallback: system webbrowser + helper bookmarklet/paste form.
    """
    from .cdp_browser import (
        find_chromium_executable,
        launch_chromium_isolated,
        open_helper_tab,
        poll_token_from_cdp,
        project_browser_profile_dir,
        wait_cdp_ready,
    )

    cfg = load_config(config_path)
    region = (region or cfg.get("region") or "cn").lower()
    if region not in {"cn", "us"}:
        region = "cn"
    urls = browser_login_urls(region)

    state = _BrowserLoginState()
    state.region = region
    state.config_path = config_path

    handler = _make_handler(state)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    state.port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    helper = f"http://127.0.0.1:{state.port}/"
    opener = open_url or webbrowser.open
    browser_proc = None
    debug_port: int | None = None
    cdp_mode = False

    def _persist_token(token: str) -> LoginResult:
        result = validate_and_resolve_user(token, region=state.region)
        save_cloud_tokens(
            access_token=result.access_token,
            user_id=result.user_id or None,
            region=state.region,
            path=local_config_path(state.config_path),
        )
        state.result = result
        state.error = None
        state.done.set()
        return result

    try:
        if open_browser and use_cdp:
            exe = find_chromium_executable()
            if exe:
                try:
                    browser_proc, debug_port, exe = launch_chromium_isolated(
                        login_url=urls.makerworld_login,
                        user_data_dir=project_browser_profile_dir(),
                    )
                    if wait_cdp_ready(debug_port, timeout=30.0):
                        cdp_mode = True
                        try:
                            open_helper_tab(debug_port, helper)
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("Could not open helper tab via CDP: %s", exc)
                        print(
                            f"已用独立配置启动浏览器 ({exe})；调试端口 {debug_port}。",
                            flush=True,
                        )
                        print(
                            "已打开浏览器，登录成功后会自动回传；无需书签。"
                            f" 辅助页: {helper}",
                            flush=True,
                        )
                    else:
                        print(
                            "CDP 未能在时限内就绪，回退到系统浏览器 + 辅助页。",
                            flush=True,
                        )
                        try:
                            browser_proc.terminate()
                        except Exception:  # noqa: BLE001
                            pass
                        browser_proc = None
                        debug_port = None
                except Exception as exc:  # noqa: BLE001
                    logger.warning("CDP browser launch failed: %s", exc)
                    print(f"无法启动 Chrome/Edge CDP（{exc}），回退到系统浏览器。", flush=True)
                    browser_proc = None
                    debug_port = None
            else:
                print(
                    "未找到 Chrome/Edge，回退到系统浏览器 + 辅助页"
                    "（书签/粘贴仍可用）。",
                    flush=True,
                )

        if open_browser and not cdp_mode:
            try:
                opener(helper)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not open helper page: %s", exc)
            try:
                opener(urls.makerworld_login)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not open MakerWorld: %s", exc)
            print(f"已打开浏览器登录辅助页: {helper}", flush=True)
            print(f"MakerWorld ({region}): {urls.makerworld_login}", flush=True)
            print("在浏览器完成登录后，用书签回传或粘贴 token。等待中…", flush=True)
        elif not open_browser:
            print(f"辅助页: {helper}", flush=True)
            print(f"MakerWorld ({region}): {urls.makerworld_login}", flush=True)

        cookie_urls = [
            urls.makerworld_home,
            urls.makerworld_home.rstrip("/") + "/",
            "https://makerworld.com.cn/",
            "https://makerworld.com/",
        ]
        prefer = "makerworld.com.cn" if region == "cn" else "makerworld.com"
        domain_substr = "makerworld.com.cn" if region == "cn" else "makerworld.com"

        deadline = time.time() + float(timeout_seconds)
        while not state.done.is_set():
            remaining = deadline - time.time()
            if remaining <= 0:
                raise CloudError(
                    f"浏览器登录超时（{int(timeout_seconds)}s）。"
                    "可重试 `login --browser`，或改用 `login --gui` / 密码登录。"
                )
            if cdp_mode and debug_port is not None and not state.done.is_set():
                token = poll_token_from_cdp(
                    debug_port,
                    prefer_url_substr=prefer,
                    cookie_urls=cookie_urls,
                    domain_substr=domain_substr,
                    timeout_seconds=min(2.0, remaining),
                    poll_interval=0.5,
                    should_stop=lambda: state.done.is_set(),
                )
                if token:
                    try:
                        _persist_token(token)
                        print(
                            f"CDP 已捕获 token；user_id={state.result.user_id if state.result else '?'}",
                            flush=True,
                        )
                        break
                    except CloudError as exc:
                        # Token present but API rejected — keep polling / allow paste
                        state.error = str(exc)
                        logger.warning("CDP token validate failed: %s", exc)
                        time.sleep(1.0)
                        continue
            state.done.wait(timeout=0.5)

        if state.result is None:
            raise CloudError(state.error or "浏览器登录未得到 token")
        return state.result
    finally:
        server.shutdown()
        server.server_close()
        if browser_proc is not None:
            try:
                browser_proc.terminate()
            except Exception:  # noqa: BLE001
                pass
