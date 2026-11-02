"""Bambu cloud login/devices + AMS remaining via MQTT report (or mock JSON)."""

from __future__ import annotations

import json
import ssl
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import requests

from .config import api_base_url, mqtt_host, tfa_login_url

try:
    import paho.mqtt.client as mqtt
except ImportError:  # pragma: no cover
    mqtt = None  # type: ignore


@dataclass
class AmsTrayRemain:
    ams_id: int
    tray_id: int  # slot within AMS (0–3 typically)
    absolute_tray: int  # ams_id * 4 + tray_id (254 = external)
    type: str | None = None
    color: str | None = None
    remain: int | None = None  # 0–100, or -1 unknown
    remaining_g: float | None = None
    estimated: bool = True
    tray_info_idx: str | None = None
    tray_uuid: str | None = None
    empty: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AmsSnapshot:
    device_id: str
    trays: list[AmsTrayRemain] = field(default_factory=list)
    source: str = ""
    raw: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "source": self.source,
            "warnings": list(self.warnings),
            "trays": [t.to_dict() for t in self.trays],
        }


class CloudError(RuntimeError):
    pass


class LoginNeedsVerification(CloudError):
    """Raised when password login requires email/SMS code or TFA."""

    def __init__(
        self,
        message: str,
        *,
        verification_type: str,
        account: str = "",
        tfa_key: str | None = None,
    ):
        super().__init__(message)
        self.verification_type = verification_type  # verifyCode | tfa
        self.account = account
        self.tfa_key = tfa_key


@dataclass
class LoginResult:
    access_token: str
    user_id: str = ""
    refresh_token: str = ""
    expires_in: int | None = None
    raw_keys: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "user_id": self.user_id,
            "refresh_token": self.refresh_token,
            "expiresIn": self.expires_in,
            "raw_keys": list(self.raw_keys),
        }


def _normalize_color(color: str | None) -> str | None:
    if not color:
        return None
    c = str(color).strip()
    if c.startswith("#"):
        c = c[1:]
    c = c.upper()
    if len(c) == 6:
        c = c + "FF"
    if c in {"00000000", "FFFFFFFF"} and color == "":
        return None
    return c


def remain_to_grams(remain: int | None, spool_weight_g: float) -> tuple[float | None, bool]:
    """Convert remain percent to grams. Returns (grams, estimated_flag)."""
    if remain is None or remain < 0:
        return None, True
    if remain > 100:
        # Some firmware dumps may already look like grams; treat >100 as unknown %
        # but keep raw as grams estimate only if clearly percentage-ish docs say 0-100.
        return None, True
    return (remain / 100.0) * float(spool_weight_g), True


class BambuCloudClient:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.region = (config.get("region") or "cn").lower()
        self.base = api_base_url(self.region)
        cloud = config.get("cloud") or {}
        self.access_token = str(cloud.get("access_token") or "").strip()
        self.refresh_token = str(cloud.get("refresh_token") or "").strip()
        self.user_id = str(cloud.get("user_id") or "").strip()
        self.account = str(cloud.get("account") or "").strip()
        self.password = str(cloud.get("password") or "")
        self._tfa_key: str | None = None
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "bambu-ams-check/0.1"})

    def _auth_headers(self) -> dict[str, str]:
        if not self.access_token:
            raise CloudError(
                "缺少 access_token。请运行 `python -m bambu_ams_check login` "
                "或 `login --gui` 写入 config.local.yaml。"
            )
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

    def _apply_token_payload(self, data: dict[str, Any], *, cookie_token: str | None = None) -> LoginResult:
        token = (
            data.get("accessToken")
            or data.get("access_token")
            or cookie_token
            or ""
        )
        token = str(token).strip()
        if not token:
            msg = data.get("message") or data.get("error") or "no accessToken"
            raise CloudError(f"Login failed: {msg}")
        refresh = str(data.get("refreshToken") or data.get("refresh_token") or "").strip()
        self.access_token = token
        if refresh:
            self.refresh_token = refresh
        # Resolve uid if possible (never log token)
        try:
            pref = self.get_preference()
            uid = pref.get("uid")
            if uid is not None:
                self.user_id = str(uid)
        except CloudError:
            pass
        expires = data.get("expiresIn")
        try:
            expires_in = int(expires) if expires is not None else None
        except (TypeError, ValueError):
            expires_in = None
        return LoginResult(
            access_token=self.access_token,
            user_id=self.user_id,
            refresh_token=self.refresh_token,
            expires_in=expires_in,
            raw_keys=sorted(str(k) for k in data.keys()),
        )

    def login(
        self,
        account: str | None = None,
        password: str | None = None,
        code: str | None = None,
        tfa_code: str | None = None,
        tfa_key: str | None = None,
    ) -> LoginResult:
        """Password / verification-code / TFA login against unofficial cloud API.

        Raises LoginNeedsVerification when email/SMS or authenticator code is required.
        Password is never logged.
        """
        account = (account or self.account or "").strip()
        password = password if password is not None else self.password
        if tfa_code:
            return self.login_tfa(tfa_code, tfa_key=tfa_key or self._tfa_key)

        if not account:
            raise CloudError("login 需要 account（邮箱/手机）")

        body: dict[str, Any] = {"account": account}
        if code:
            body["code"] = str(code).strip()
        else:
            if not password:
                raise CloudError("login 需要 password 或 verification code")
            body["password"] = password

        url = f"{self.base}/v1/user-service/user/login"
        try:
            resp = self.session.post(url, json=body, timeout=30)
        except requests.RequestException as exc:
            raise CloudError(f"Login network error: {exc}") from exc

        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise CloudError(f"Login response not JSON ({resp.status_code})") from exc
        if not isinstance(data, dict):
            raise CloudError(f"Login response unexpected ({resp.status_code})")

        login_type = str(data.get("loginType") or "").strip()
        if login_type == "verifyCode":
            raise LoginNeedsVerification(
                "需要邮箱/短信验证码。请查收邮件或短信后再次提交 --code。",
                verification_type="verifyCode",
                account=account,
            )
        if login_type == "tfa":
            self._tfa_key = data.get("tfaKey") or data.get("tfa_key")
            raise LoginNeedsVerification(
                "需要二次验证（身份验证器 TOTP）。请提交 --code（TFA）。",
                verification_type="tfa",
                account=account,
                tfa_key=str(self._tfa_key) if self._tfa_key else None,
            )

        if resp.status_code >= 400 and not (
            data.get("accessToken") or data.get("access_token")
        ):
            msg = data.get("message") or data.get("error") or resp.text[:200]
            raise CloudError(f"Login failed ({resp.status_code}): {msg}")

        return self._apply_token_payload(data)

    def login_tfa(self, tfa_code: str, tfa_key: str | None = None) -> LoginResult:
        """Complete TFA via web sign-in endpoint; token may arrive in Set-Cookie."""
        key = tfa_key or self._tfa_key
        if not key:
            raise CloudError("TFA 需要先完成密码登录以获取 tfaKey")
        code = str(tfa_code).strip()
        if not code:
            raise CloudError("缺少 TFA 验证码")
        url = tfa_login_url(self.region)
        # Also try API path as fallback (undocumented)
        urls = [url, f"{self.base}/v1/user-service/user/tfa/login"]
        last_err: Exception | None = None
        for attempt_url in urls:
            body_candidates = (
                {"tfaKey": key, "tfaCode": code},
                {"tfaKey": key, "code": code},
            )
            for body in body_candidates:
                try:
                    resp = self.session.post(attempt_url, json=body, timeout=30)
                except requests.RequestException as exc:
                    last_err = exc
                    continue
                cookie_token = None
                for cname, cval in resp.cookies.items():
                    if "token" in cname.lower() and cval:
                        cookie_token = cval
                        break
                try:
                    data = resp.json() if resp.content else {}
                except Exception:  # noqa: BLE001
                    data = {}
                if not isinstance(data, dict):
                    data = {}
                if data.get("accessToken") or data.get("access_token") or cookie_token:
                    self._tfa_key = None
                    return self._apply_token_payload(data, cookie_token=cookie_token)
                last_err = CloudError(
                    f"TFA failed ({resp.status_code}): "
                    f"{data.get('message') or data.get('error') or resp.text[:160]}"
                )
        raise CloudError(f"TFA login failed: {last_err}")

    def refresh(self, refresh_token: str | None = None) -> LoginResult:
        """Attempt token refresh. Unofficial; often returns 401 — callers should re-login."""
        token = (refresh_token or self.refresh_token or self.access_token or "").strip()
        if not token:
            raise CloudError("没有 refresh_token / access_token 可供刷新")
        url = f"{self.base}/v1/user-service/user/refreshtoken"
        try:
            resp = self.session.post(url, json={"refreshToken": token}, timeout=30)
        except requests.RequestException as exc:
            raise CloudError(f"Refresh network error: {exc}") from exc
        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise CloudError(f"Refresh response not JSON ({resp.status_code})") from exc
        if not isinstance(data, dict):
            raise CloudError(f"Refresh failed ({resp.status_code})")
        if resp.status_code >= 400 or not (
            data.get("accessToken") or data.get("access_token")
        ):
            msg = data.get("message") or data.get("error") or resp.text[:200]
            raise CloudError(
                f"Refresh failed ({resp.status_code}): {msg}。"
                "官方 refreshtoken 常不可用，请重新 login。"
            )
        return self._apply_token_payload(data)

    def get_preference(self) -> dict[str, Any]:
        url = f"{self.base}/v1/design-user-service/my/preference"
        resp = self.session.get(url, headers=self._auth_headers(), timeout=30)
        if resp.status_code != 200:
            raise CloudError(f"preference HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        if not isinstance(data, dict):
            raise CloudError("preference response not an object")
        return data

    def list_devices(self) -> list[dict[str, Any]]:
        url = f"{self.base}/v1/iot-service/api/user/bind"
        resp = self.session.get(url, headers=self._auth_headers(), timeout=30)
        if resp.status_code != 200:
            raise CloudError(f"bind HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        devices = data.get("devices") or []
        if not isinstance(devices, list):
            return []
        return devices

    def resolve_device_id(self) -> str:
        configured = str((self.config.get("device") or {}).get("device_id") or "").strip()
        if configured:
            return configured
        devices = self.list_devices()
        if not devices:
            raise CloudError("账号下没有绑定设备")
        online = [d for d in devices if d.get("online")]
        pick = online[0] if online else devices[0]
        return str(pick.get("dev_id") or pick.get("device_id") or "")

    def ensure_user_id(self) -> str:
        if self.user_id:
            return self.user_id
        pref = self.get_preference()
        uid = pref.get("uid")
        if uid is None:
            raise CloudError("无法获取 user_id（preference.uid）")
        self.user_id = str(uid)
        return self.user_id



def parse_ams_from_report(
    report: dict[str, Any],
    device_id: str = "",
    spool_weight_g: float = 1000.0,
) -> AmsSnapshot:
    """Parse MQTT push_status / report JSON into AmsSnapshot.

    Expected shapes:
      {"print": {"ams": {"ams": [{"id":"0","tray":[...]}]}}}
      or already-unwrapped print object / ams object / mock {"trays":[...]}
    """
    snap = AmsSnapshot(device_id=device_id, source="report")
    spool_weight_g = float(spool_weight_g)

    # Offline mock: {"trays":[...]} or {"ams":{"trays":...}}
    if "trays" in report and isinstance(report["trays"], list):
        for item in report["trays"]:
            if not isinstance(item, dict):
                continue
            ams_id = int(item.get("ams_id", 0))
            tray_id = int(item.get("tray_id", item.get("id", 0)))
            absolute = item.get("absolute_tray")
            if absolute is None:
                absolute = item.get("ams_tray")
            if absolute is None:
                absolute = ams_id * 4 + tray_id
            remain = item.get("remain")
            if remain is not None:
                try:
                    remain = int(remain)
                except (TypeError, ValueError):
                    remain = None
            grams = item.get("remaining_g")
            estimated = True
            if grams is None:
                grams, estimated = remain_to_grams(remain, spool_weight_g)
            else:
                grams = float(grams)
                estimated = bool(item.get("estimated", True))
            snap.trays.append(
                AmsTrayRemain(
                    ams_id=ams_id,
                    tray_id=tray_id,
                    absolute_tray=int(absolute),
                    type=item.get("type") or item.get("tray_type"),
                    color=_normalize_color(item.get("color") or item.get("tray_color")),
                    remain=remain,
                    remaining_g=grams,
                    estimated=estimated,
                    tray_info_idx=item.get("tray_info_idx"),
                    tray_uuid=item.get("tray_uuid"),
                    empty=bool(item.get("empty", False)),
                )
            )
        snap.source = report.get("source") or "ams-json"
        if report.get("device_id"):
            snap.device_id = str(report["device_id"])
        return snap

    print_obj = report.get("print") if isinstance(report.get("print"), dict) else report
    ams_block = None
    if isinstance(print_obj, dict):
        ams_block = print_obj.get("ams")
    if ams_block is None and isinstance(report.get("ams"), dict):
        ams_block = report["ams"]
    if not isinstance(ams_block, dict):
        snap.warnings.append("No ams block in report")
        snap.raw = report
        return snap

    units = ams_block.get("ams") or []
    if isinstance(units, dict):
        units = [units]
    for unit in units:
        if not isinstance(unit, dict):
            continue
        ams_id = int(unit.get("id") or 0)
        trays = unit.get("tray") or []
        if isinstance(trays, dict):
            trays = [trays]
        for tray in trays:
            if not isinstance(tray, dict):
                continue
            # Empty slot: often only has "id"
            tray_id = int(tray.get("id") or 0)
            absolute = ams_id * 4 + tray_id
            has_type = bool(tray.get("tray_type") or tray.get("type"))
            remain_raw = tray.get("remain")
            remain: int | None
            try:
                remain = int(remain_raw) if remain_raw is not None else None
            except (TypeError, ValueError):
                remain = None
            empty = not has_type and remain is None and not tray.get("tray_color")
            grams, estimated = remain_to_grams(remain, spool_weight_g)
            snap.trays.append(
                AmsTrayRemain(
                    ams_id=ams_id,
                    tray_id=tray_id,
                    absolute_tray=absolute,
                    type=tray.get("tray_type") or tray.get("type"),
                    color=_normalize_color(tray.get("tray_color") or tray.get("color")),
                    remain=remain,
                    remaining_g=grams,
                    estimated=estimated,
                    tray_info_idx=tray.get("tray_info_idx"),
                    tray_uuid=tray.get("tray_uuid"),
                    empty=empty,
                )
            )

    # External spool
    vt = None
    if isinstance(print_obj, dict):
        vt = print_obj.get("vt_tray")
    if isinstance(vt, dict) and (vt.get("tray_type") or vt.get("remain") is not None):
        remain_raw = vt.get("remain")
        try:
            remain = int(remain_raw) if remain_raw is not None else None
        except (TypeError, ValueError):
            remain = None
        grams, estimated = remain_to_grams(remain, spool_weight_g)
        snap.trays.append(
            AmsTrayRemain(
                ams_id=-1,
                tray_id=254,
                absolute_tray=254,
                type=vt.get("tray_type"),
                color=_normalize_color(vt.get("tray_color")),
                remain=remain,
                remaining_g=grams,
                estimated=estimated,
                tray_info_idx=vt.get("tray_info_idx"),
                tray_uuid=vt.get("tray_uuid"),
                empty=not bool(vt.get("tray_type")),
            )
        )

    snap.raw = report
    return snap


def load_ams_json(path: str | Path, spool_weight_g: float = 1000.0) -> AmsSnapshot:
    path = Path(path)
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise CloudError("ams-json root must be an object")
    return parse_ams_from_report(
        data,
        device_id=str(data.get("device_id") or "mock"),
        spool_weight_g=spool_weight_g,
    )


def fetch_ams_via_mqtt(
    config: dict[str, Any],
    device_id: str | None = None,
    timeout: float | None = None,
) -> AmsSnapshot:
    """Connect to cloud MQTT, send pushall, parse AMS trays from first full report."""
    if mqtt is None:
        raise CloudError("paho-mqtt 未安装")

    client_wrap = BambuCloudClient(config)
    if not client_wrap.access_token:
        raise CloudError("MQTT 需要 access_token")
    user_id = client_wrap.ensure_user_id()
    device_id = device_id or client_wrap.resolve_device_id()
    if not device_id:
        raise CloudError("无法确定 device_id")

    host = mqtt_host(client_wrap.region)
    timeout = float(
        timeout
        if timeout is not None
        else (config.get("mqtt") or {}).get("timeout_seconds", 12)
    )
    spool_weight_g = float((config.get("device") or {}).get("spool_weight_g", 1000))

    username = f"u_{user_id}"
    password = client_wrap.access_token
    report_topic = f"device/{device_id}/report"
    request_topic = f"device/{device_id}/request"

    result: dict[str, Any] = {"report": None, "error": None}
    done = threading.Event()

    def on_connect(client, userdata, flags, reason_code, properties=None):  # noqa: ANN001
        rc = int(getattr(reason_code, "value", reason_code))
        if rc != 0:
            result["error"] = f"MQTT connect failed rc={reason_code}"
            done.set()
            return
        client.subscribe(report_topic)
        payload = json.dumps({"pushing": {"sequence_id": "0", "command": "pushall"}})
        client.publish(request_topic, payload)

    def on_message(client, userdata, msg):  # noqa: ANN001
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return
        # Prefer messages that include AMS
        print_obj = payload.get("print") if isinstance(payload, dict) else None
        if isinstance(print_obj, dict) and "ams" in print_obj:
            result["report"] = payload
            done.set()
            client.disconnect()
        elif isinstance(payload, dict) and "ams" in payload and result["report"] is None:
            result["report"] = payload

    # paho-mqtt 2.x CallbackAPIVersion
    try:
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"bambu-ams-check-{int(time.time())}",
            protocol=mqtt.MQTTv311,
        )
    except Exception:  # noqa: BLE001
        client = mqtt.Client(client_id=f"bambu-ams-check-{int(time.time())}")

    client.username_pw_set(username, password)
    client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
    client.on_connect = on_connect
    client.on_message = on_message

    try:
        client.connect(host, 8883, keepalive=60)
    except Exception as exc:  # noqa: BLE001
        raise CloudError(f"MQTT connect error: {exc}") from exc

    client.loop_start()
    finished = done.wait(timeout=timeout)
    client.loop_stop()
    try:
        client.disconnect()
    except Exception:  # noqa: BLE001
        pass

    if result["error"]:
        raise CloudError(result["error"])
    if not finished or not result["report"]:
        raise CloudError(
            f"MQTT 在 {timeout}s 内未收到含 AMS 的 report（检查 device 在线 / token / region）"
        )

    snap = parse_ams_from_report(
        result["report"],
        device_id=device_id,
        spool_weight_g=spool_weight_g,
    )
    snap.source = f"mqtt:{host}"
    return snap


def fetch_ams(config: dict[str, Any], ams_json: str | Path | None = None) -> AmsSnapshot:
    spool = float((config.get("device") or {}).get("spool_weight_g", 1000))
    if ams_json:
        return load_ams_json(ams_json, spool_weight_g=spool)
    return fetch_ams_via_mqtt(config)
