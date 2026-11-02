"""Tkinter desktop popup listing compare rows (SHORT in red)."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from .compare import STATUS_CN, STATUS_SHORT, STATUS_UNKNOWN, CompareResult


def show_popup(result: CompareResult, title: str | None = None) -> None:
    title = title or "Bambu AMS 检查 / Filament Check"
    root = tk.Tk()
    root.title(title)
    root.geometry("780x420")
    root.attributes("-topmost", True)

    header = ttk.Label(
        root,
        text=f"{result.path}\n设备: {result.device_id or '-'}",
        justify=tk.LEFT,
        wraplength=740,
    )
    header.pack(fill=tk.X, padx=10, pady=8)

    frame = ttk.Frame(root)
    frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=4)

    columns = ("status", "plate", "filament", "type", "color", "need_g", "tray", "remain_g", "note")
    tree = ttk.Treeview(frame, columns=columns, show="headings", height=12)
    headings = {
        "status": "状态",
        "plate": "盘",
        "filament": "丝号",
        "type": "类型",
        "color": "颜色",
        "need_g": "需要g",
        "tray": "托盘",
        "remain_g": "剩余g",
        "note": "说明",
    }
    widths = {
        "status": 60,
        "plate": 40,
        "filament": 50,
        "type": 70,
        "color": 90,
        "need_g": 70,
        "tray": 50,
        "remain_g": 70,
        "note": 240,
    }
    for col in columns:
        tree.heading(col, text=headings[col])
        tree.column(col, width=widths[col], anchor=tk.W)

    tree.tag_configure("SHORT", foreground="#cc0000")
    tree.tag_configure("UNKNOWN", foreground="#a66a00")
    tree.tag_configure("OK", foreground="#0a7a28")

    for row in result.rows:
        st = STATUS_CN.get(row.status, row.status)
        values = (
            st,
            row.plate_index if row.plate_index is not None else "",
            row.filament_id if row.filament_id is not None else "",
            row.type or "",
            row.color or "",
            f"{row.used_g:.2f}" if row.used_g is not None else "",
            row.ams_tray if row.ams_tray is not None else "",
            f"{row.remaining_g:.1f}" if row.remaining_g is not None else "",
            row.reason,
        )
        tag = row.status if row.status in {STATUS_SHORT, STATUS_UNKNOWN, "OK"} else "OK"
        tree.insert("", tk.END, values=values, tags=(tag,))

    scroll = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=tree.yview)
    tree.configure(yscrollcommand=scroll.set)
    tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    scroll.pack(side=tk.RIGHT, fill=tk.Y)

    summary = (
        "有托盘耗材不足（红色）" if result.has_short else "未发现明确不足（请留意“未知”）"
    )
    ttk.Label(root, text=summary).pack(anchor=tk.W, padx=10, pady=4)

    btn = ttk.Button(root, text="关闭 / Close", command=root.destroy)
    btn.pack(pady=8)

    root.lift()
    root.focus_force()
    root.mainloop()
