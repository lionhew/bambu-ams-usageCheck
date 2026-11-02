# Design Notes — bambu-ams-check

## Purpose

Sidecar (does **not** patch Bambu Studio). After slicing, read filament usage from
the plate `.gcode.3mf`, query AMS remaining from Bambu cloud MQTT, and show
need vs remain per tray.

## Data flow

```
.gcode.3mf (ZIP)
  └─ Metadata/slice_info.config   → used_g / used_m / type / color
  └─ Metadata/project_settings.config → ams_mapping[]
  └─ Metadata/plate_N.json        → fallback filament_ids

config.local.yaml
  └─ region, access_token, user_id, device_id, spool_weight_g

Bambu cloud MQTT (TLS :8883)
  └─ subscribe device/<id>/report
  └─ publish pushall
  └─ print.ams.ams[].tray[].remain   (0–100 or -1)

compare → OK / SHORT / UNKNOWN → tkinter popup or --no-ui text
```

## 3MF parsing

- File is a ZIP. Primary source: `Metadata/slice_info.config` (XML).
- Each `<filament id="N" ... used_g="..." used_m="..." type="..." color="..."/>`.
- `id` is 1-based; extruder index = id - 1.
- `ams_mapping` is a list parallel to project filaments: absolute tray id =
  `ams_id * 4 + slot_id`, or `254` external, or `-1` unmapped.
- Defensive: missing members, incomplete writes, multi-plate.

## AMS remaining

| Field | Meaning |
|-------|---------|
| `remain` | 0–100 estimated percent; `-1` unknown / RFID off / third-party spool |
| `remaining_g` | `remain/100 * spool_weight_g` (default 1000); always flagged `estimated` |
| `tray_type` / `tray_color` | Match heuristics when mapping missing |
| `tray_info_idx` | Bambu filament SKU id |

HTTP `GET /v1/iot-service/api/user/bind` lists devices; live AMS state comes from MQTT.

Regions:

- CN: `https://api.bambulab.cn`, `cn.mqtt.bambulab.com`
- US/global: `https://api.bambulab.com`, `us.mqtt.bambulab.com`

MQTT auth: username `u_<user_id>`, password = access token.

## Comparison rules

1. Map by `ams_mapping` absolute tray.
2. Else `tray_info_idx`, else color+type, else color, else unique type.
3. `SHORT` if `remaining_g < used_g`; `UNKNOWN` if remain &lt; 0 or unmatched.

## Watcher

Stdlib polling (no `watchdog` required). Debounce 1–2s; require stable size/mtime
so mid-write `.3mf` files are ignored.

When `cloud.access_token` is missing, `watch` / `check` offer a「登录拓竹云」
dialog (`ui_login`) instead of failing silently (unless `--no-ui`).

## Auth strategy (unofficial cloud API)

Password login frequently demands email/SMS codes or TFA. The sidecar now has a
first-class login flow that **writes** `access_token` + `user_id` into
`config.local.yaml` (gitignored). Full tokens are masked in CLI output; passwords
are never logged or persisted by the login helpers.

### Endpoints used (defensive — shapes may change)

| Method | Path / URL | Purpose |
|--------|------------|---------|
| POST | `{api}/v1/user-service/user/login` | Password or email/SMS `code` login; `loginType=verifyCode\|tfa` |
| POST | `https://bambulab.(cn\|com)/api/sign-in/tfa` | TFA completion (`tfaKey` + `tfaCode`); token may be in cookie |
| POST | `{api}/v1/user-service/user/tfa/login` | Fallback TFA path (undocumented) |
| POST | `{api}/v1/user-service/user/refreshtoken` | Optional refresh; **often 401** in the wild |
| GET | `{api}/v1/design-user-service/my/preference` | Resolve numeric `uid` |
| GET | `{api}/v1/iot-service/api/user/bind` | List bound devices |

`{api}` is `https://api.bambulab.cn` (cn) or `https://api.bambulab.com` (us).

Treat Cloudflare / captcha / empty bodies as hard failures and ask the user to
retry later or paste a browser token.

## Autoconfig (`setup`)

Reads Windows Studio layout when env vars exist:

- `%APPDATA%\BambuStudio\user\<digits>` → `cloud.user_id`
- `%APPDATA%\BambuStudio\BambuStudio.conf` (JSON) → `app.download_path`,
  `recent.last_opened_folder`, optional `user_last_selected_machine`
- `%LOCALAPPDATA%\Temp\bamboo_model`, `%APPDATA%\BambuStudio\cache` if present

Never invents `access_token`; launches login when missing (unless `--no-login`).

## First-run autoconfig & reset

Long-term source of truth is **this PC at runtime**, not a checked-in path list.

- Commands `watch` / `check` / `devices` / `login` call `ensure_first_run_autoconfig`
  when `config.local.yaml` is missing or incomplete (`autoconfig.done` unset and
  empty `watch.directories`).
- Marker `autoconfig.done` is written after a successful detect pass.
- `reset-config` / `setup --reset` deletes (or clears) local config and re-runs
  detection; `--keep-token` preserves cloud tokens across the reset.

## Browser SSO helper

Bambu does not document a third-party OAuth redirect. Preferred UX:

1. `webbrowser.open` region login: `CN_BROWSER_LOGIN_URL` (`https://bambulab.cn/zh-cn/sign-in?ticket=1&to=…makerworld.com.cn…`) or `US_BROWSER_LOGIN_URL` (`https://makerworld.com/login`).
2. Local `ThreadingHTTPServer` on `127.0.0.1` serves instructions + paste form
   + bookmarklet that reads the MakerWorld `token` cookie and hits `/callback`.
3. Validate via `GET /v1/design-user-service/my/preference`, then
   `save_cloud_tokens`.

Password / TFA login remains as fallback. Do not scrape on-disk browser profiles.

