"""Tkinter dialog: login to Bambu cloud and save tokens to config.local.yaml."""

from __future__ import annotations

import logging
import threading
from getpass import getpass
from typing import Any, Callable

from .ams_cloud import BambuCloudClient, CloudError, LoginNeedsVerification, LoginResult
from .config import has_access_token, load_config, local_config_path, mask_token, save_cloud_tokens

logger = logging.getLogger(__name__)


def _prompt_cli(prompt: str, *, secret: bool = False) -> str:
    if secret:
        return getpass(prompt)
    return input(prompt)


def interactive_cli_login(
    *,
    config_path: str | None = None,
    region: str | None = None,
    account: str | None = None,
    password: str | None = None,
    code: str | None = None,
    refresh: bool = False,
    prompt: Callable[..., str] = _prompt_cli,
) -> LoginResult:
    """CLI login with interactive prompts; always writes config.local.yaml on success."""
    cfg = load_config(config_path)
    if region:
        cfg["region"] = region
    client = BambuCloudClient(cfg)
    out_path = local_config_path(config_path)

    if refresh:
        try:
            result = client.refresh()
        except CloudError as exc:
            raise CloudError(f"刷新失败: {exc}") from exc
        save_cloud_tokens(
            access_token=result.access_token,
            user_id=result.user_id or None,
            refresh_token=result.refresh_token or None,
            region=cfg.get("region"),
            account=client.account or None,
            path=out_path,
        )
        return result

    account = (account or client.account or "").strip()
    if not account:
        account = prompt("拓竹账号（邮箱/手机）: ").strip()
    password = password if password is not None else client.password
    if not code and not password:
        password = prompt("密码: ", secret=True)

    tfa_key: str | None = None
    try:
        result = client.login(account=account, password=password, code=code)
    except LoginNeedsVerification as need:
        vtype = need.verification_type
        if vtype == "tfa":
            tfa_key = need.tfa_key
            code = prompt("请输入身份验证器 TFA 验证码: ").strip()
            result = client.login(account=account, tfa_code=code, tfa_key=tfa_key)
        else:
            code = prompt("请输入邮箱/短信验证码: ").strip()
            result = client.login(account=account, code=code)

    save_cloud_tokens(
        access_token=result.access_token,
        user_id=result.user_id or None,
        refresh_token=result.refresh_token or None,
        region=cfg.get("region"),
        account=account,
        path=out_path,
    )
    return result


def show_login_dialog(
    *,
    config_path: str | None = None,
    region: str | None = None,
    prefill_account: str | None = None,
    prefer_browser: bool = True,
) -> bool:
    """Modal tkinter login. Returns True if tokens were saved.

    Prefers browser SSO (MakerWorld) via 「浏览器登录」; password form remains fallback.
    """
    import tkinter as tk
    from tkinter import messagebox, ttk

    cfg = load_config(config_path)
    if region:
        cfg["region"] = region
    out_path = local_config_path(config_path)
    success = {"ok": False}

    root = tk.Tk()
    root.title("登录拓竹云 / Bambu Cloud Login")
    root.geometry("480x420")
    root.attributes("-topmost", True)

    frm = ttk.Frame(root, padding=12)
    frm.pack(fill=tk.BOTH, expand=True)

    ttk.Label(
        frm,
        text="推荐：系统浏览器打开 MakerWorld 完成 SSO，再回传 token（支持 Cloudflare / 扫码等）。",
        wraplength=440,
    ).grid(row=0, column=0, columnspan=2, sticky=tk.W, pady=(0, 8))

    ttk.Label(frm, text="区域 region").grid(row=1, column=0, sticky=tk.W, pady=4)
    region_var = tk.StringVar(value=(region or cfg.get("region") or "cn"))
    region_box = ttk.Combobox(
        frm, textvariable=region_var, values=("cn", "us"), state="readonly", width=28
    )
    region_box.grid(row=1, column=1, sticky=tk.EW, pady=4)

    browser_row = ttk.Frame(frm)
    browser_row.grid(row=2, column=0, columnspan=2, sticky=tk.EW, pady=8)

    status_var = tk.StringVar(value="")
    status = ttk.Label(frm, textvariable=status_var, foreground="#333", wraplength=440)
    status.grid(row=3, column=0, columnspan=2, sticky=tk.W, pady=4)

    # --- password fallback fields ---
    ttk.Separator(frm).grid(row=4, column=0, columnspan=2, sticky=tk.EW, pady=8)
    ttk.Label(frm, text="备用：账号密码登录（可能触发验证码 / 无 OAuth）").grid(
        row=5, column=0, columnspan=2, sticky=tk.W
    )

    ttk.Label(frm, text="账号 account").grid(row=6, column=0, sticky=tk.W, pady=4)
    account_var = tk.StringVar(
        value=prefill_account or (cfg.get("cloud") or {}).get("account") or ""
    )
    ttk.Entry(frm, textvariable=account_var, width=32).grid(
        row=6, column=1, sticky=tk.EW, pady=4
    )

    ttk.Label(frm, text="密码 password").grid(row=7, column=0, sticky=tk.W, pady=4)
    password_var = tk.StringVar()
    ttk.Entry(frm, textvariable=password_var, show="*", width=32).grid(
        row=7, column=1, sticky=tk.EW, pady=4
    )

    ttk.Label(frm, text="验证码 code").grid(row=8, column=0, sticky=tk.W, pady=4)
    code_var = tk.StringVar()
    ttk.Entry(frm, textvariable=code_var, width=32).grid(
        row=8, column=1, sticky=tk.EW, pady=4
    )

    state: dict[str, Any] = {"busy": False, "tfa_key": None, "mode": "password"}

    def set_busy(busy: bool) -> None:
        state["busy"] = busy
        for w in (btn_browser, btn_login):
            w.configure(state=tk.DISABLED if busy else tk.NORMAL)

    def on_done(ok: bool, message: str) -> None:
        set_busy(False)
        status_var.set(message)
        if ok:
            success["ok"] = True
            messagebox.showinfo("登录成功", message, parent=root)
            root.destroy()
        else:
            messagebox.showerror("登录失败", message, parent=root)

    def browser_worker() -> None:
        try:
            from .browser_login import run_browser_login

            result = run_browser_login(
                config_path=config_path,
                region=region_var.get().strip() or "cn",
                open_browser=True,
            )
            msg = (
                f"已保存到 {out_path}\n"
                f"user_id={result.user_id or '?'}\n"
                f"token={mask_token(result.access_token)}"
            )
            root.after(0, lambda: on_done(True, msg))
        except CloudError as exc:
            root.after(0, lambda: on_done(False, str(exc)))
        except Exception as exc:  # noqa: BLE001
            logger.exception("browser login GUI error")
            root.after(0, lambda: on_done(False, f"异常: {exc}"))

    def do_browser() -> None:
        if state["busy"]:
            return
        set_busy(True)
        status_var.set("已打开浏览器…请在 MakerWorld 登录后用书签/粘贴回传 token")
        # Withdraw main dialog so it does not block the helper page interaction visually
        try:
            root.withdraw()
        except Exception:  # noqa: BLE001
            pass
        threading.Thread(target=browser_worker, daemon=True).start()

    def worker() -> None:
        try:
            local_cfg = dict(cfg)
            local_cfg["region"] = region_var.get().strip() or "cn"
            client = BambuCloudClient(local_cfg)
            account = account_var.get().strip()
            password = password_var.get()
            code = code_var.get().strip() or None

            if state["mode"] == "tfa" and code:
                result = client.login(
                    account=account,
                    tfa_code=code,
                    tfa_key=state.get("tfa_key"),
                )
            elif code and state["mode"] == "verifyCode":
                result = client.login(account=account, code=code)
            else:
                try:
                    result = client.login(account=account, password=password, code=code)
                except LoginNeedsVerification as need:
                    state["mode"] = need.verification_type
                    state["tfa_key"] = need.tfa_key
                    hint = (
                        "请输入身份验证器验证码后再次点击登录"
                        if need.verification_type == "tfa"
                        else "验证码已发送，请填写后再次点击登录"
                    )
                    root.after(0, lambda: (set_busy(False), status_var.set(hint), root.deiconify()))
                    return

            save_cloud_tokens(
                access_token=result.access_token,
                user_id=result.user_id or None,
                refresh_token=result.refresh_token or None,
                region=local_cfg.get("region"),
                account=account,
                path=out_path,
            )
            msg = (
                f"已保存到 {out_path}\n"
                f"user_id={result.user_id or '?'}\n"
                f"token={mask_token(result.access_token)}"
            )
            root.after(0, lambda: on_done(True, msg))
        except CloudError as exc:
            root.after(0, lambda: on_done(False, str(exc)))
        except Exception as exc:  # noqa: BLE001
            logger.exception("login GUI error")
            root.after(0, lambda: on_done(False, f"异常: {exc}"))

    def do_login() -> None:
        if state["busy"]:
            return
        if not account_var.get().strip():
            messagebox.showwarning("提示", "请填写账号", parent=root)
            return
        if state["mode"] == "password" and not code_var.get().strip() and not password_var.get():
            messagebox.showwarning("提示", "请填写密码或验证码", parent=root)
            return
        set_busy(True)
        status_var.set("正在登录…")
        threading.Thread(target=worker, daemon=True).start()

    btn_browser = ttk.Button(browser_row, text="浏览器登录（推荐）", command=do_browser)
    btn_browser.pack(side=tk.LEFT, padx=4)
    ttk.Button(browser_row, text="取消", command=root.destroy).pack(side=tk.LEFT, padx=4)

    btn_row = ttk.Frame(frm)
    btn_row.grid(row=9, column=0, columnspan=2, pady=10)
    btn_login = ttk.Button(btn_row, text="账号密码登录", command=do_login)
    btn_login.pack(side=tk.LEFT, padx=6)

    frm.columnconfigure(1, weight=1)
    if prefer_browser:
        status_var.set("可直接点「浏览器登录」；密码登录为备用。")
    root.lift()
    root.focus_force()
    root.mainloop()
    return bool(success["ok"])


def offer_login_when_token_missing(
    *,
    config_path: str | None = None,
    reason: str = "缺少 cloud.access_token",
    auto_open: bool = False,
) -> bool:
    """Show a small prompt with login options when token is missing."""
    cfg = load_config(config_path)
    if has_access_token(cfg):
        return True
    if auto_open:
        # Prefer browser SSO path from the dialog
        show_login_dialog(
            config_path=config_path, region=cfg.get("region"), prefer_browser=True
        )
        return has_access_token(load_config(config_path))

    import tkinter as tk
    from tkinter import ttk

    decided = {"mode": ""}  # browser | gui | ""
    root = tk.Tk()
    root.title("需要登录拓竹云")
    root.geometry("460x180")
    root.attributes("-topmost", True)
    ttk.Label(
        root,
        text=f"{reason}\n推荐使用浏览器登录 MakerWorld（SSO），写入 config.local.yaml。",
        wraplength=420,
        justify=tk.LEFT,
    ).pack(padx=16, pady=16)
    row = ttk.Frame(root)
    row.pack(pady=8)

    def go(mode: str) -> None:
        decided["mode"] = mode
        root.destroy()

    ttk.Button(row, text="浏览器登录", command=lambda: go("browser")).pack(
        side=tk.LEFT, padx=8
    )
    ttk.Button(row, text="登录对话框", command=lambda: go("gui")).pack(side=tk.LEFT, padx=8)
    ttk.Button(row, text="稍后", command=root.destroy).pack(side=tk.LEFT, padx=8)
    root.lift()
    root.focus_force()
    root.mainloop()
    if decided["mode"] == "browser":
        try:
            from .browser_login import run_browser_login

            run_browser_login(config_path=config_path, region=cfg.get("region"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("browser login failed: %s", exc)
            show_login_dialog(config_path=config_path, region=cfg.get("region"))
    elif decided["mode"] == "gui":
        show_login_dialog(config_path=config_path, region=cfg.get("region"))
    return has_access_token(load_config(config_path))
