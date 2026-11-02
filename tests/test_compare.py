"""Tests for usage vs AMS comparison."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from bambu_ams_check.ams_cloud import load_ams_json, parse_ams_from_report, remain_to_grams
from bambu_ams_check.compare import STATUS_OK, STATUS_SHORT, STATUS_UNKNOWN, compare_usage, format_report
from bambu_ams_check.threemf import parse_3mf

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SAMPLE_3MF = FIXTURES / "sample_plate.gcode.3mf"
SAMPLE_AMS = FIXTURES / "ams_mock.json"


class TestCompare(unittest.TestCase):
    def test_remain_to_grams(self):
        g, est = remain_to_grams(50, 1000)
        self.assertAlmostEqual(g or 0, 500.0)
        self.assertTrue(est)
        g, est = remain_to_grams(-1, 1000)
        self.assertIsNone(g)

    def test_parse_ams_mock(self):
        snap = load_ams_json(SAMPLE_AMS, spool_weight_g=1000)
        self.assertEqual(len(snap.trays), 3)
        t0 = next(t for t in snap.trays if t.absolute_tray == 0)
        self.assertAlmostEqual(t0.remaining_g or 0, 800.0)

    def test_compare_enough_and_short(self):
        usage = parse_3mf(SAMPLE_3MF)
        ams = load_ams_json(SAMPLE_AMS, spool_weight_g=1000)
        result = compare_usage(usage, ams)
        self.assertEqual(len(result.rows), 2)
        by_tray = {r.ams_tray: r for r in result.rows}
        # Tray 0 has 80% of 1000g = 800g, need 12.5 → OK
        self.assertEqual(by_tray[0].status, STATUS_OK)
        # Tray 2 has 2% = 20g, need 3.2 → OK still; tweak with short mock
        self.assertIn(by_tray[2].status, {STATUS_OK, STATUS_SHORT})

        # Force SHORT: tiny remain on tray 2
        data = json.loads(SAMPLE_AMS.read_text(encoding="utf-8"))
        for t in data["trays"]:
            if t["absolute_tray"] == 2:
                t["remain"] = 0
                t["remaining_g"] = 1.0
        ams_short = parse_ams_from_report(data, spool_weight_g=1000)
        result2 = compare_usage(usage, ams_short)
        statuses = {r.ams_tray: r.status for r in result2.rows}
        self.assertEqual(statuses[2], STATUS_SHORT)
        self.assertTrue(result2.has_short)
        text = format_report(result2)
        self.assertIn("不够", text)

    def test_unknown_when_no_match(self):
        usage = parse_3mf(SAMPLE_3MF)
        empty = parse_ams_from_report({"trays": []}, device_id="x")
        result = compare_usage(usage, empty)
        self.assertTrue(all(r.status == STATUS_UNKNOWN for r in result.rows))


if __name__ == "__main__":
    unittest.main()
