"""Match slice filament usage to AMS trays and decide OK / SHORT / UNKNOWN."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .ams_cloud import AmsSnapshot, AmsTrayRemain
from .threemf import FilamentUsage, ThreeMFUsage

STATUS_OK = "OK"
STATUS_SHORT = "SHORT"
STATUS_UNKNOWN = "UNKNOWN"

STATUS_CN = {
    STATUS_OK: "够",
    STATUS_SHORT: "不够",
    STATUS_UNKNOWN: "未知",
}


@dataclass
class CompareRow:
    plate_index: int | None
    filament_id: int | None
    extruder: int | None
    type: str | None
    color: str | None
    used_g: float | None
    used_m: float | None
    ams_tray: int | None
    remain: int | None
    remaining_g: float | None
    estimated: bool
    status: str
    reason: str = ""
    matched_by: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status_cn"] = STATUS_CN.get(self.status, self.status)
        return d


@dataclass
class CompareResult:
    rows: list[CompareRow] = field(default_factory=list)
    path: str = ""
    device_id: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def has_short(self) -> bool:
        return any(r.status == STATUS_SHORT for r in self.rows)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "device_id": self.device_id,
            "warnings": list(self.warnings),
            "has_short": self.has_short,
            "rows": [r.to_dict() for r in self.rows],
        }


def _norm_color(c: str | None) -> str | None:
    if not c:
        return None
    x = c.strip().lstrip("#").upper()
    if len(x) == 6:
        x += "FF"
    return x


def _colors_match(a: str | None, b: str | None) -> bool:
    ca, cb = _norm_color(a), _norm_color(b)
    if not ca or not cb:
        return False
    return ca[:6] == cb[:6]  # ignore alpha


def _find_tray(
    need: FilamentUsage,
    trays: list[AmsTrayRemain],
    used_absolutes: set[int],
) -> tuple[AmsTrayRemain | None, str]:
    # 1) Explicit AMS mapping
    if need.ams_tray is not None:
        for t in trays:
            if t.absolute_tray == need.ams_tray and not t.empty:
                return t, "ams_mapping"
        for t in trays:
            if t.absolute_tray == need.ams_tray:
                return t, "ams_mapping(empty)"

    # 2) tray_info_idx
    if need.tray_info_idx:
        for t in trays:
            if (
                t.tray_info_idx
                and t.tray_info_idx == need.tray_info_idx
                and t.absolute_tray not in used_absolutes
                and not t.empty
            ):
                return t, "tray_info_idx"

    # 3) type + color
    for t in trays:
        if t.empty or t.absolute_tray in used_absolutes:
            continue
        if need.type and t.type and need.type.upper() != t.type.upper():
            continue
        if need.color and t.color and _colors_match(need.color, t.color):
            return t, "color+type"

    # 4) color only
    if need.color:
        for t in trays:
            if t.empty or t.absolute_tray in used_absolutes:
                continue
            if _colors_match(need.color, t.color):
                return t, "color"

    # 5) type only (weak)
    if need.type:
        candidates = [
            t
            for t in trays
            if not t.empty
            and t.absolute_tray not in used_absolutes
            and t.type
            and t.type.upper() == need.type.upper()
        ]
        if len(candidates) == 1:
            return candidates[0], "type_unique"

    return None, ""


def compare_usage(
    usage: ThreeMFUsage,
    ams: AmsSnapshot,
    margin_g: float = 0.0,
) -> CompareResult:
    result = CompareResult(
        path=usage.path,
        device_id=ams.device_id,
        warnings=list(usage.warnings) + list(ams.warnings),
    )
    used_absolutes: set[int] = set()

    for need in usage.filaments:
        tray, matched_by = _find_tray(need, ams.trays, used_absolutes)
        if tray is not None:
            used_absolutes.add(tray.absolute_tray)

        used_g = need.used_g
        if tray is None:
            row = CompareRow(
                plate_index=need.plate_index,
                filament_id=need.filament_id,
                extruder=need.extruder,
                type=need.type,
                color=need.color,
                used_g=used_g,
                used_m=need.used_m,
                ams_tray=need.ams_tray,
                remain=None,
                remaining_g=None,
                estimated=True,
                status=STATUS_UNKNOWN,
                reason="未匹配到 AMS 托盘",
                matched_by="",
            )
            result.rows.append(row)
            continue

        remain = tray.remain
        remaining_g = tray.remaining_g
        if remaining_g is None or remain is None or remain < 0:
            status = STATUS_UNKNOWN
            reason = "余量未知（remain=-1 或无 RFID/估算）"
        elif used_g is None:
            status = STATUS_UNKNOWN
            reason = "切片用量未知"
        elif remaining_g + 1e-6 < (used_g + margin_g):
            status = STATUS_SHORT
            reason = f"剩余约 {remaining_g:.1f}g < 需要 {used_g:.1f}g"
        else:
            status = STATUS_OK
            reason = f"剩余约 {remaining_g:.1f}g ≥ 需要 {used_g:.1f}g"

        result.rows.append(
            CompareRow(
                plate_index=need.plate_index,
                filament_id=need.filament_id,
                extruder=need.extruder,
                type=need.type or tray.type,
                color=need.color or tray.color,
                used_g=used_g,
                used_m=need.used_m,
                ams_tray=tray.absolute_tray,
                remain=remain,
                remaining_g=remaining_g,
                estimated=tray.estimated,
                status=status,
                reason=reason,
                matched_by=matched_by,
            )
        )

    return result


def format_report(result: CompareResult, *, chinese: bool = True) -> str:
    lines: list[str] = []
    title = "耗材用量 vs AMS 余量" if chinese else "Filament need vs AMS remain"
    lines.append(title)
    lines.append(f"文件/File: {result.path}")
    if result.device_id:
        lines.append(f"设备/Device: {result.device_id}")
    lines.append("-" * 60)
    if not result.rows:
        lines.append("无耗材记录 / No filament rows")
    for i, row in enumerate(result.rows, 1):
        st = STATUS_CN.get(row.status, row.status) if chinese else row.status
        color = row.color or "-"
        typ = row.type or "-"
        used = f"{row.used_g:.2f}g" if row.used_g is not None else "?"
        rem = f"{row.remaining_g:.1f}g" if row.remaining_g is not None else "?"
        remain_pct = f"{row.remain}%" if row.remain is not None and row.remain >= 0 else "?"
        tray = row.ams_tray if row.ams_tray is not None else "?"
        est = "≈" if row.estimated else ""
        lines.append(
            f"[{i}] 盘{row.plate_index} 丝{row.filament_id}/{typ}/{color} "
            f"需要 {used} | 托盘 {tray} 剩余 {est}{rem} ({remain_pct}) => {st}"
        )
        if row.reason:
            lines.append(f"     {row.reason} ({row.matched_by or 'no-match'})")
    if result.warnings:
        lines.append("-" * 60)
        for w in result.warnings:
            lines.append(f"! {w}")
    summary = (
        "结论: 有托盘不够，请换料或减量"
        if result.has_short
        else "结论: 未发现明确不足（仍请留意 UNKNOWN）"
    )
    if not chinese:
        summary = (
            "Result: SHORT filament detected"
            if result.has_short
            else "Result: no definite SHORT (check UNKNOWN)"
        )
    lines.append("-" * 60)
    lines.append(summary)
    return "\n".join(lines)
