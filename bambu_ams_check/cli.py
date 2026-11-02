"""CLI entry: check / watch / devices / login / setup / reset-config."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .ams_cloud import BambuCloudClient, CloudError, fetch_ams
from .autoconfig import ensure_first_run_autoconfig, reset_config, run_autoconfig
from .compare import compare_usage, format_report
from .config import has_access_token, load_config, local_config_path, mask_token
from .threemf import parse_3mf
from .watcher import watch_directories

logger = logging.getLogger("bambu_ams_check")


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )


def _maybe_show(result, *, no_ui: bool, config: dict) -> None:
    text = format_report(result)
    print(text)
    show = (not no_ui) and bool((config.get("ui") or {}).get("show_popup", True))
    if show:
        try:
            from .ui_popup import show_popup

            show_popup(result)
        except Exception as exc:  # noqa: BLE001
            logger.warning("UI popup failed (%s); text report already printed", exc)


def _first_run_setup(
    config_path: str | None,
    *,
    region: str | None = None,
    quiet: bool = False,
) -> None:
    """Auto-detect Studio paths on this PC when local config is missing/incomplete."""
    result = ensure_first_run_autoconfig(
        config_path=config_path,
        region=region or "cn",
    )
    if result is None:
        return
    if not quiet:
        print("首次运行：已对本机自动探测并写入 config.local.yaml", file=sys.stderr)
        for line in result.summary_lines():
            print(f"  {line}", file=sys.stderr)


def _ensure_token(
    config: dict,
    *,
    config_path: str | None,
    no_ui: bool,
    ams_json: str | None,
) -> dict:
    """If cloud token missing (and not using ams-json), offer login GUI / hint."""
    if ams_json or has_access_token(config):
        return config
    if no_ui:
        print(
            "缺少 cloud.access_token。请运行:\n"
            "  python -m bambu_ams_check login --gui\n"
            "或:\n"
            "  python -m bambu_ams_check login",
            file=sys.stderr,
        )
        raise SystemExit(2)
    try:
        from .ui_login import offer_login_when_token_missing

        ok = offer_login_when_token_missing(
            config_path=config_path,
            reason="缺少 cloud.access_token，无法拉取 AMS",
            auto_open=False,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Login prompt failed: %s", exc)
        ok = False
    if not ok:
        print("仍未配置 access_token，中止。", file=sys.stderr)
        raise SystemExit(2)
    return load_config(config_path)


def cmd_check(args: argparse.Namespace) -> int:
    _first_run_setup(args.config)
    config = load_config(args.config)
    config = _ensure_token(
        config, config_path=args.config, no_ui=args.no_ui, ams_json=args.ams_json
    )
    path = Path(args.path)
    usage = parse_3mf(path)
    try:
        ams = fetch_ams(config, ams_json=args.ams_json)
    except CloudError as exc:
        print(f"AMS 获取失败: {exc}", file=sys.stderr)
        return 2
    result = compare_usage(usage, ams)
    _maybe_show(result, no_ui=args.no_ui, config=config)
    return 1 if result.has_short else 0


def cmd_watch(args: argparse.Namespace) -> int:
    _first_run_setup(args.config)
    config = load_config(args.config)
    config = _ensure_token(
        config, config_path=args.config, no_ui=args.no_ui, ams_json=args.ams_json
    )
    watch_cfg = config.get("watch") or {}
    directories = list(watch_cfg.get("directories") or [])
    if not directories:
        print(
            "配置 watch.directories 为空。请确认本机已安装 Bambu Studio，或运行:\n"
            "  python -m bambu_ams_check reset-config\n"
            "或手动填写 config.local.yaml 中的监视目录。",
            file=sys.stderr,
        )
        return 2

    def on_file(path: Path) -> None:
        logger.info("Detected: %s", path)
        live = load_config(args.config)
        if not args.ams_json and not has_access_token(live):
            logger.error("缺少 access_token — 请登录拓竹云")
            if not args.no_ui:
                try:
                    from .ui_login import offer_login_when_token_missing

                    offer_login_when_token_missing(
                        config_path=args.config,
                        reason="监视中检测到缺少 access_token",
                        auto_open=True,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Login UI failed: %s", exc)
            return
        usage = parse_3mf(path)
        if not usage.filaments:
            logger.warning("No filament usage in %s: %s", path, usage.warnings)
            return
        try:
            ams = fetch_ams(live, ams_json=args.ams_json)
        except CloudError as exc:
            logger.error("AMS fetch failed: %s", exc)
            return
        result = compare_usage(usage, ams)
        _maybe_show(result, no_ui=args.no_ui, config=live)

    try:
        watch_directories(
            directories,
            on_file,
            patterns=list(watch_cfg.get("patterns") or ["*.gcode.3mf", "*.3mf"]),
            debounce_seconds=float(watch_cfg.get("debounce_seconds", 1.5)),
            poll_interval_seconds=float(watch_cfg.get("poll_interval_seconds", 1.0)),
        )
    except KeyboardInterrupt:
        print("\n已停止监视 / stopped")
    return 0


def cmd_devices(args: argparse.Namespace) -> int:
    _first_run_setup(args.config)
    config = load_config(args.config)
    if not has_access_token(config) and not args.no_ui:
        config = _ensure_token(
            config, config_path=args.config, no_ui=False, ams_json=None
        )
    client = BambuCloudClient(config)
    try:
        devices = client.list_devices()
    except CloudError as exc:
        print(f"失败: {exc}", file=sys.stderr)
        return 2
    if not devices:
        print("无绑定设备")
        return 0
    for d in devices:
        print(
            json.dumps(
                {
                    "dev_id": d.get("dev_id"),
                    "name": d.get("name") or d.get("dev_name"),
                    "online": d.get("online", d.get("dev_online")),
                    "model": d.get("dev_product_name") or d.get("dev_model_name"),
                    "print_status": d.get("print_status"),
                },
                ensure_ascii=False,
            )
        )
    return 0


def cmd_login(args: argparse.Namespace) -> int:
    region = args.region
    _first_run_setup(args.config, region=region or "cn")
    if region is None:
        try:
            region = load_config(args.config).get("region") or "cn"
        except Exception:  # noqa: BLE001
            region = "cn"

    def _print_success_from_config() -> None:
        cfg = load_config(args.config)
        cloud = cfg.get("cloud") or {}
        print(
            f"登录成功，已写入 {local_config_path(args.config)}\n"
            f"  cloud.user_id: {cloud.get('user_id')}\n"
            f"  cloud.access_token: {mask_token(cloud.get('access_token'))}"
        )

    use_browser = bool(getattr(args, "browser", False))
    use_password = bool(getattr(args, "password_login", False)) or bool(
        args.account or args.password or args.code
    )
    # Default: browser when no password flags; --gui opens dialog (browser button first)
    if args.refresh and not use_browser:
        use_password = True

    if use_browser or (not args.gui and not use_password and not args.refresh):
        from .browser_login import run_browser_login

        try:
            info = run_browser_login(
                config_path=args.config,
                region=region,
                open_browser=True,
            )
        except CloudError as exc:
            print(f"浏览器登录失败: {exc}", file=sys.stderr)
            print(
                "可改用: python -m bambu_ams_check login --gui\n"
                "或: python -m bambu_ams_check login --password",
                file=sys.stderr,
            )
            return 2
        except (EOFError, KeyboardInterrupt):
            print("\n已取消", file=sys.stderr)
            return 2
        print(f"登录成功，已写入 {local_config_path(args.config)}（勿提交 git）")
        print(f"  cloud.user_id: {info.user_id}")
        print(f"  cloud.access_token: {mask_token(info.access_token)}")
        return 0

    if args.gui:
        from .ui_login import show_login_dialog

        ok = show_login_dialog(
            config_path=args.config, region=region, prefer_browser=True
        )
        if not ok:
            print("登录取消或失败", file=sys.stderr)
            return 2
        _print_success_from_config()
        return 0

    from .ui_login import interactive_cli_login

    try:
        info = interactive_cli_login(
            config_path=args.config,
            region=region,
            account=args.account,
            password=args.password,
            code=args.code,
            refresh=bool(args.refresh),
        )
    except CloudError as exc:
        print(f"登录失败: {exc}", file=sys.stderr)
        print(
            "\n备选：python -m bambu_ams_check login --browser",
            file=sys.stderr,
        )
        return 2
    except (EOFError, KeyboardInterrupt):
        print("\n已取消", file=sys.stderr)
        return 2

    out = local_config_path(args.config)
    print(f"登录成功，已写入 {out}（勿提交 git）")
    print(f"  cloud.user_id: {info.user_id}")
    print(f"  cloud.access_token: {mask_token(info.access_token)}")
    if info.refresh_token:
        print(f"  cloud.refresh_token: {mask_token(info.refresh_token)}")
    return 0



def _setup_login_launcher(args: argparse.Namespace, region: str):
    def _launch_login() -> bool:
        if args.no_login:
            return False
        if args.gui or not sys.stdin.isatty():
            from .ui_login import show_login_dialog

            return show_login_dialog(config_path=args.config, region=region)
        from .ui_login import interactive_cli_login

        try:
            interactive_cli_login(config_path=args.config, region=region)
            return True
        except (CloudError, EOFError, KeyboardInterrupt) as exc:
            print(f"登录未完成: {exc}", file=sys.stderr)
            return False

    return _launch_login


def cmd_setup(args: argparse.Namespace) -> int:
    region = args.region or "cn"
    _launch_login = _setup_login_launcher(args, region)

    if getattr(args, "reset", False):
        result = reset_config(
            config_path=args.config,
            keep_token=bool(getattr(args, "keep_token", False)),
            region=region,
            delete_file=True,
            launch_login=not args.no_login and (args.login or args.gui),
            login_launcher=_launch_login if (args.login or args.gui) else None,
        )
        print("已清空本地自动配置并重新探测本机。")
    else:
        result = run_autoconfig(
            config_path=args.config,
            region=region,
            launch_login=not args.no_login and (args.login or args.gui),
            login_launcher=_launch_login if (args.login or args.gui) else None,
        )

    if result.needs_login and not args.no_login and not (args.login or args.gui):
        print("\n".join(result.summary_lines()))
        if sys.stdin.isatty() and not args.yes:
            ans = input("缺少 access_token，现在登录？ [Y/n] ").strip().lower()
            if ans in ("", "y", "yes", "是"):
                if _launch_login():
                    result.needs_login = not has_access_token(load_config(args.config))
        elif args.yes:
            if _launch_login():
                result.needs_login = not has_access_token(load_config(args.config))
        print()
    for line in result.summary_lines():
        print(line)
    return 0


def cmd_reset_config(args: argparse.Namespace) -> int:
    region = args.region or "cn"
    result = reset_config(
        config_path=args.config,
        keep_token=bool(args.keep_token),
        region=region,
        delete_file=not bool(args.in_place),
    )
    print("已重置本地配置并重新对本机自动探测。")
    if args.keep_token:
        print("已保留 cloud access_token / user_id（若原先存在）。")
    for line in result.summary_lines():
        print(line)
    if result.needs_login and not args.no_login:
        if args.gui or (sys.stdin.isatty() and args.yes):
            from .ui_login import show_login_dialog

            show_login_dialog(config_path=args.config, region=region)
        elif sys.stdin.isatty():
            ans = input("缺少 access_token，现在登录？ [Y/n] ").strip().lower()
            if ans in ("", "y", "yes", "是"):
                from .ui_login import show_login_dialog

                show_login_dialog(config_path=args.config, region=region)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bambu-ams-check",
        description="Bambu Studio sidecar: compare slice filament usage vs AMS remaining",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("check", help="Parse a .gcode.3mf and compare with AMS")
    c.add_argument("path", help="Path to .3mf / .gcode.3mf")
    c.add_argument("--config", "-c", default=None)
    c.add_argument("--no-ui", action="store_true", help="Text report only")
    c.add_argument(
        "--ams-json",
        default=None,
        help="Offline mock AMS snapshot JSON (skip cloud/MQTT)",
    )
    c.set_defaults(func=cmd_check)

    w = sub.add_parser("watch", help="Watch dirs for new sliced 3MF files")
    w.add_argument("--config", "-c", default=None)
    w.add_argument("--no-ui", action="store_true")
    w.add_argument("--ams-json", default=None)
    w.set_defaults(func=cmd_watch)

    d = sub.add_parser("devices", help="List bound cloud devices")
    d.add_argument("--config", "-c", default=None)
    d.add_argument("--no-ui", action="store_true", help="Do not open login GUI")
    d.set_defaults(func=cmd_devices)

    l = sub.add_parser(
        "login",
        help="Login to Bambu cloud (browser SSO preferred) and save tokens",
    )
    l.add_argument("--config", "-c", default=None, help="Config path (default: auto)")
    l.add_argument(
        "--region",
        choices=("cn", "us"),
        default=None,
        help="Cloud region (default from config / cn)",
    )
    l.add_argument(
        "--browser",
        action="store_true",
        help="Launch Chrome/Edge (isolated profile) + CDP auto-capture MakerWorld token (recommended)",
    )
    l.add_argument(
        "--password-login",
        action="store_true",
        help="Force account/password CLI login instead of browser",
    )
    l.add_argument("--account", default=None, help="Email or phone (password flow)")
    l.add_argument("--password", default=None, help="Password (prefer prompt; not logged)")
    l.add_argument("--code", default=None, help="Email/SMS or TFA verification code")
    l.add_argument(
        "--gui",
        action="store_true",
        help="Open desktop dialog (browser button + password fallback)",
    )
    l.add_argument(
        "--refresh",
        action="store_true",
        help="Try refresh_token endpoint (often fails; re-login if so)",
    )
    l.add_argument("--write-config", default=None, help=argparse.SUPPRESS)
    l.set_defaults(func=cmd_login)

    s = sub.add_parser(
        "setup",
        aliases=["autoconfig"],
        help="Auto-detect Studio paths / user_id into config.local.yaml (no token invented)",
    )
    s.add_argument("--config", "-c", default=None, help="Write target (default config.local.yaml)")
    s.add_argument("--region", choices=("cn", "us"), default="cn")
    s.add_argument(
        "--reset",
        action="store_true",
        help="Clear local autoconfig cache and re-detect this PC (same as reset-config)",
    )
    s.add_argument(
        "--keep-token",
        action="store_true",
        help="With --reset: keep access_token / refresh_token / account",
    )
    s.add_argument("--login", action="store_true", help="Run login after autoconfig")
    s.add_argument("--gui", action="store_true", help="Use login GUI if login needed")
    s.add_argument("--no-login", action="store_true", help="Never prompt for login")
    s.add_argument("-y", "--yes", action="store_true", help="Assume yes for login prompt")
    s.set_defaults(func=cmd_setup)

    r = sub.add_parser(
        "reset-config",
        help="Clear autoconfig cache / config.local.yaml and re-detect paths on this PC",
    )
    r.add_argument("--config", "-c", default=None)
    r.add_argument("--region", choices=("cn", "us"), default="cn")
    r.add_argument(
        "--keep-token",
        action="store_true",
        help="Preserve cloud.access_token / refresh_token / account (user_id re-detected when possible)",
    )
    r.add_argument(
        "--in-place",
        action="store_true",
        help="Clear derived fields without deleting the YAML file",
    )
    r.add_argument("--gui", action="store_true", help="Open login GUI if token missing")
    r.add_argument("--no-login", action="store_true", help="Do not prompt for login")
    r.add_argument("-y", "--yes", action="store_true", help="Assume yes for login prompt")
    r.set_defaults(func=cmd_reset_config)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "command", None) == "login" and getattr(args, "write_config", None):
        if not args.config:
            args.config = args.write_config
    _setup_logging(getattr(args, "verbose", False))
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
