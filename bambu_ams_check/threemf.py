"""Parse Bambu/Orca sliced .gcode.3mf (ZIP) filament usage from Metadata/."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class FilamentUsage:
    """One filament slot needed by a plate."""

    filament_id: int | None  # 1-based id from slice_info when present
    extruder: int | None  # 0-based project filament index when known
    type: str | None = None
    color: str | None = None
    used_g: float | None = None
    used_m: float | None = None
    tray_info_idx: str | None = None
    ams_tray: int | None = None  # absolute AMS tray id if mapped
    plate_index: int | None = None
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PlateUsage:
    plate_index: int
    prediction_s: int | None = None
    weight_g: float | None = None
    filaments: list[FilamentUsage] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "plate_index": self.plate_index,
            "prediction_s": self.prediction_s,
            "weight_g": self.weight_g,
            "filaments": [f.to_dict() for f in self.filaments],
        }


@dataclass
class ThreeMFUsage:
    path: str
    plates: list[PlateUsage] = field(default_factory=list)
    ams_mapping: list[int] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def filaments(self) -> list[FilamentUsage]:
        out: list[FilamentUsage] = []
        for plate in self.plates:
            out.extend(plate.filaments)
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "ams_mapping": list(self.ams_mapping),
            "warnings": list(self.warnings),
            "plates": [p.to_dict() for p in self.plates],
            "filaments": [f.to_dict() for f in self.filaments],
        }


def _normalize_color(color: str | None) -> str | None:
    if not color:
        return None
    c = color.strip()
    if c.startswith("#"):
        c = c[1:]
    c = c.upper()
    if len(c) == 6:
        c = c + "FF"
    return c


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


def _parse_ams_mapping(raw: Any) -> list[int]:
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return []
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            parts = re.split(r"[,;\s]+", raw)
            out: list[int] = []
            for part in parts:
                if not part:
                    continue
                n = _as_int(part)
                if n is not None:
                    out.append(n)
            return out
    if isinstance(raw, list):
        out = []
        for item in raw:
            n = _as_int(item)
            if n is not None:
                out.append(n)
        return out
    return []


def _read_zip_text(zf: zipfile.ZipFile, name: str) -> str | None:
    try:
        data = zf.read(name)
    except KeyError:
        return None
    for encoding in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _find_member(zf: zipfile.ZipFile, *candidates: str) -> str | None:
    names = {n.replace("\\", "/"): n for n in zf.namelist()}
    for cand in candidates:
        key = cand.replace("\\", "/")
        if key in names:
            return names[key]
        # case-insensitive
        lower = {k.lower(): v for k, v in names.items()}
        if key.lower() in lower:
            return lower[key.lower()]
    return None


def _parse_slice_info_xml(text: str) -> list[PlateUsage]:
    plates: list[PlateUsage] = []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return plates

    for plate_el in root.findall(".//plate"):
        meta: dict[str, str] = {}
        for m in plate_el.findall("metadata"):
            key = m.get("key")
            if key:
                meta[key] = m.get("value", "")
        plate_index = _as_int(meta.get("index")) or (len(plates) + 1)
        plate = PlateUsage(
            plate_index=plate_index,
            prediction_s=_as_int(meta.get("prediction")),
            weight_g=_as_float(meta.get("weight")),
        )
        for fil in plate_el.findall("filament"):
            fid = _as_int(fil.get("id"))
            extruder = (fid - 1) if fid is not None else None
            usage = FilamentUsage(
                filament_id=fid,
                extruder=extruder,
                type=fil.get("type") or None,
                color=_normalize_color(fil.get("color")),
                used_g=_as_float(fil.get("used_g")),
                used_m=_as_float(fil.get("used_m")),
                tray_info_idx=fil.get("tray_info_idx") or None,
                plate_index=plate_index,
                source="slice_info.config",
            )
            plate.filaments.append(usage)
        plates.append(plate)
    return plates


def _parse_project_settings(text: str) -> tuple[list[int], list[str], list[str]]:
    """Return (ams_mapping, filament_types, filament_colours) best-effort."""
    ams_mapping: list[int] = []
    types: list[str] = []
    colors: list[str] = []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Sometimes stored as key=value lines; ignore if not JSON
        return ams_mapping, types, colors

    if not isinstance(data, dict):
        return ams_mapping, types, colors

    for key in ("ams_mapping", "curr_bed_type"):  # ams_mapping primary
        if key == "ams_mapping" and key in data:
            ams_mapping = _parse_ams_mapping(data.get(key))

    # filament arrays may be parallel lists
    fil_type = data.get("filament_type") or data.get("filament_types")
    if isinstance(fil_type, list):
        types = [str(x) for x in fil_type]
    elif isinstance(fil_type, str):
        types = [fil_type]

    fil_color = data.get("filament_colour") or data.get("filament_colors")
    if isinstance(fil_color, list):
        colors = [_normalize_color(str(x)) or str(x) for x in fil_color]
    elif isinstance(fil_color, str):
        colors = [_normalize_color(fil_color) or fil_color]

    # Nested settings sometimes wrap under "settings"
    if not ams_mapping and isinstance(data.get("settings"), dict):
        ams_mapping = _parse_ams_mapping(data["settings"].get("ams_mapping"))

    return ams_mapping, types, colors


def _parse_plate_json(text: str, plate_index: int) -> PlateUsage | None:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None

    plate = PlateUsage(plate_index=plate_index)
    # filament usage may appear under different keys
    filaments_raw = (
        data.get("filament")
        or data.get("filaments")
        or data.get("filament_used")
        or []
    )
    if isinstance(filaments_raw, dict):
        filaments_raw = [filaments_raw]

    if isinstance(filaments_raw, list):
        for item in filaments_raw:
            if not isinstance(item, dict):
                continue
            fid = _as_int(item.get("id") or item.get("filament_id"))
            extruder = _as_int(item.get("extruder"))
            if extruder is None and fid is not None:
                extruder = fid - 1 if fid >= 1 else fid
            plate.filaments.append(
                FilamentUsage(
                    filament_id=fid,
                    extruder=extruder,
                    type=item.get("type") or item.get("filament_type"),
                    color=_normalize_color(
                        item.get("color") or item.get("filament_color")
                    ),
                    used_g=_as_float(item.get("used_g") or item.get("weight")),
                    used_m=_as_float(item.get("used_m") or item.get("length")),
                    tray_info_idx=item.get("tray_info_idx") or item.get("filamentId"),
                    ams_tray=_as_int(item.get("ams_tray") or item.get("ams")),
                    plate_index=plate_index,
                    source=f"plate_{plate_index}.json",
                )
            )

    # filament_ids: 0-based project slots used on this plate
    filament_ids = data.get("filament_ids")
    if isinstance(filament_ids, list) and not plate.filaments:
        for idx in filament_ids:
            extruder = _as_int(idx)
            plate.filaments.append(
                FilamentUsage(
                    filament_id=(extruder + 1) if extruder is not None else None,
                    extruder=extruder,
                    plate_index=plate_index,
                    source=f"plate_{plate_index}.json:filament_ids",
                )
            )

    return plate if plate.filaments else plate


def _apply_ams_mapping(result: ThreeMFUsage) -> None:
    if not result.ams_mapping:
        return
    for fil in result.filaments:
        if fil.ams_tray is not None:
            continue
        idx = fil.extruder
        if idx is None and fil.filament_id is not None:
            idx = fil.filament_id - 1
        if idx is None:
            continue
        if 0 <= idx < len(result.ams_mapping):
            mapped = result.ams_mapping[idx]
            if mapped >= 0:
                fil.ams_tray = mapped


def parse_3mf(path: str | Path) -> ThreeMFUsage:
    """Parse a Bambu/Orca .3mf / .gcode.3mf and return structured filament usage."""
    path = Path(path)
    result = ThreeMFUsage(path=str(path))
    if not path.is_file():
        result.warnings.append(f"File not found: {path}")
        return result

    try:
        zf = zipfile.ZipFile(path, "r")
    except zipfile.BadZipFile:
        result.warnings.append("Not a valid ZIP/3MF file (possibly still writing)")
        return result

    with zf:
        # slice_info.config — primary usage source
        slice_name = _find_member(
            zf,
            "Metadata/slice_info.config",
            "Metadata/SliceInfo.config",
        )
        if slice_name:
            text = _read_zip_text(zf, slice_name)
            if text:
                result.plates.extend(_parse_slice_info_xml(text))

        # project_settings.config — ams_mapping + filament meta
        proj_name = _find_member(
            zf,
            "Metadata/project_settings.config",
            "Metadata/ProjectSettings.config",
        )
        types: list[str] = []
        colors: list[str] = []
        if proj_name:
            text = _read_zip_text(zf, proj_name)
            if text:
                result.ams_mapping, types, colors = _parse_project_settings(text)

        # plate_*.json — supplement / fallback
        plate_json_names = sorted(
            n
            for n in zf.namelist()
            if re.search(r"(?i)Metadata/plate_\d+\.json$", n.replace("\\", "/"))
        )
        existing_indexes = {p.plate_index for p in result.plates}
        for name in plate_json_names:
            m = re.search(r"plate_(\d+)\.json$", name.replace("\\", "/"), re.I)
            plate_index = int(m.group(1)) if m else (len(result.plates) + 1)
            text = _read_zip_text(zf, name)
            if not text:
                continue
            plate = _parse_plate_json(text, plate_index)
            if plate is None:
                continue
            if plate_index in existing_indexes:
                # Merge missing ams_tray / color if slice_info already present
                target = next(p for p in result.plates if p.plate_index == plate_index)
                if not target.filaments and plate.filaments:
                    target.filaments = plate.filaments
                continue
            if plate.filaments:
                result.plates.append(plate)
                existing_indexes.add(plate_index)

        # Fill type/color from project settings when missing
        for fil in result.filaments:
            idx = fil.extruder
            if idx is None and fil.filament_id is not None:
                idx = fil.filament_id - 1
            if idx is None:
                continue
            if not fil.type and 0 <= idx < len(types):
                fil.type = types[idx]
            if not fil.color and 0 <= idx < len(colors):
                fil.color = colors[idx]

        _apply_ams_mapping(result)

        if not result.filaments:
            result.warnings.append(
                "No filament usage found in Metadata "
                "(need sliced .gcode.3mf with slice_info.config)"
            )

    return result
