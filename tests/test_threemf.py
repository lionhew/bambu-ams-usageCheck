"""Tests for 3MF filament usage parsing."""

from __future__ import annotations

import unittest
import zipfile
from pathlib import Path

from bambu_ams_check.threemf import parse_3mf

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SAMPLE = FIXTURES / "sample_plate.gcode.3mf"


class TestThreeMF(unittest.TestCase):
    def test_fixture_exists(self):
        self.assertTrue(SAMPLE.is_file(), f"missing fixture {SAMPLE}")

    def test_parse_usage(self):
        usage = parse_3mf(SAMPLE)
        self.assertFalse(usage.warnings, usage.warnings)
        self.assertGreaterEqual(len(usage.plates), 1)
        fils = usage.filaments
        self.assertEqual(len(fils), 2)
        by_id = {f.filament_id: f for f in fils}
        self.assertIn(1, by_id)
        self.assertIn(2, by_id)
        self.assertAlmostEqual(by_id[1].used_g or 0, 12.5, places=2)
        self.assertAlmostEqual(by_id[2].used_g or 0, 3.2, places=2)
        self.assertEqual(by_id[1].type, "PLA")
        self.assertEqual(by_id[2].type, "PLA")
        # ams_mapping [0, 2] → absolute trays
        self.assertEqual(by_id[1].ams_tray, 0)
        self.assertEqual(by_id[2].ams_tray, 2)

    def test_bad_zip(self):
        bad = FIXTURES / "not_a_zip.gcode.3mf"
        bad.write_text("not zip", encoding="utf-8")
        try:
            usage = parse_3mf(bad)
            self.assertTrue(any("ZIP" in w or "zip" in w.lower() for w in usage.warnings))
        finally:
            bad.unlink(missing_ok=True)

    def test_zip_members(self):
        with zipfile.ZipFile(SAMPLE, "r") as zf:
            names = [n.replace("\\", "/") for n in zf.namelist()]
        self.assertTrue(any(n.endswith("slice_info.config") for n in names))


if __name__ == "__main__":
    unittest.main()
