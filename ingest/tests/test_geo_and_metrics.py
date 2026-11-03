"""Unit tests verifying geometry, drct convention, CPA/ETA bucketing, cross-radar dedupe, DB pruning, and state machine."""

from __future__ import annotations

import os
import tempfile
import unittest

from ingest.db import (
    get_connection,
    prune_old_records,
    record_glm_batch,
)
from ingest.geo import (
    bearing_deg,
    bucket_eta_5min,
    compute_cpa_and_eta,
    deg_to_cardinal,
    drct_to_heading_deg,
    haversine_mi,
)
from ingest.goes_glm import (
    clamp_time_offset_s,
    make_flash_uid,
    normalize_energy_fj,
)
from ingest.iem_cells import build_site_cells
from ingest.metrics import compute_flash_trend, compute_site_status


class TestGeoAndKinematics(unittest.TestCase):
    def test_drct_from_convention(self) -> None:
        # Meteorological drct = direction moving FROM -> heading_to = (drct + 180) % 360
        self.assertAlmostEqual(drct_to_heading_deg(240.0), 60.0)
        self.assertAlmostEqual(drct_to_heading_deg(203.0), 23.0)
        self.assertAlmostEqual(drct_to_heading_deg(0.0), 180.0)
        self.assertAlmostEqual(drct_to_heading_deg(180.0), 0.0)

    def test_haversine_and_bearing(self) -> None:
        # Stillwater YMCA: 36.1152, -97.0624
        # Point ~20 miles due West (~ -97.4209)
        dist = haversine_mi(36.1152, -97.0624, 36.1152, -97.4209)
        self.assertAlmostEqual(float(dist), 20.0, delta=0.3)
        brg = bearing_deg(36.1152, -97.0624, 36.1152, -97.4209)
        self.assertEqual(deg_to_cardinal(brg), "W")

    def test_cpa_and_bucketed_eta_approaching_vs_receding(self) -> None:
        # Cell ~20 mi West of Stillwater moving FROM West (drct=270 -> heading East=90) at 30 mph (26.07 kt)
        sknt = 30.0 / 1.15077945
        approaching = compute_cpa_and_eta(
            site_lat=36.1152,
            site_lon=-97.0624,
            cell_lat=36.1152,
            cell_lon=-97.4209,
            drct_from_deg=270.0,
            sknt=sknt,
        )
        self.assertTrue(approaching["has_motion"])
        self.assertEqual(approaching["heading_to_deg"], 90)
        self.assertEqual(approaching["heading_cardinal"], "E")
        self.assertAlmostEqual(approaching["cpa_mi"], 0.0, delta=0.3)
        self.assertEqual(approaching["eta_15mi_min"], 10)
        self.assertEqual(approaching["eta_10mi_min"], 20)
        self.assertIsNotNone(approaching["eta_10mi_label"])
        self.assertIn("min", approaching["eta_10mi_label"])

        # Flip drct to 90 (moving FROM East -> heading West, receding from site)
        receding = compute_cpa_and_eta(
            site_lat=36.1152,
            site_lon=-97.0624,
            cell_lat=36.1152,
            cell_lon=-97.4209,
            drct_from_deg=90.0,
            sknt=sknt,
        )
        self.assertIsNone(receding["eta_15mi_min"])
        self.assertIsNone(receding["eta_10mi_min"])

    def test_bucket_eta_5min(self) -> None:
        self.assertIsNone(bucket_eta_5min(None))
        self.assertEqual(bucket_eta_5min(0.0), 0)
        self.assertEqual(bucket_eta_5min(1.2), 5)
        self.assertEqual(bucket_eta_5min(22.8), 25)


class TestCrossRadarDeduplication(unittest.TestCase):
    def test_cross_radar_cluster_prefers_lowest_range_and_min_eta(self) -> None:
        # Physical cell near Mitch Park (35.68, -97.86) seen by both TLX (close) and VNX (farther)
        raw_cells = [
            {
                "nexrad": "VNX",
                "storm_id": "A1",
                "valid_utc": 1700000000,
                "lat": 35.6847,
                "lon": -97.8650,
                "drct": 270.0,
                "sknt": 35.0,
                "max_dbz": 54.0,
                "top_kft": 38.0,
                "tvs": "NONE",
                "meso": "MESO",
            },
            {
                "nexrad": "TLX",
                "storm_id": "K9",
                "valid_utc": 1700000000,
                "lat": 35.6860,
                "lon": -97.8600,
                "drct": 270.0,
                "sknt": 25.0,
                "max_dbz": 60.0,
                "top_kft": 44.0,
                "tvs": "NONE",
                "meso": "NONE",
            },
        ]
        deduped = build_site_cells(
            raw_cells=raw_cells,
            site_lat=35.6847,
            site_lon=-97.5083,
        )
        self.assertEqual(len(deduped), 1)
        cell = deduped[0]
        self.assertEqual(cell["id"], "TLX_K9")
        self.assertEqual(cell["observed_by_radars"], ["TLX", "VNX"])
        self.assertEqual(cell["max_dbz"], 60)
        self.assertEqual(cell["meso"], "MESO")
        self.assertEqual(cell["eta_15mi_min"], 10)


class TestFlashUnitsDatabaseAndStateMachine(unittest.TestCase):
    def test_energy_fj_and_time_offset_clamping(self) -> None:
        self.assertAlmostEqual(normalize_energy_fj(1.3e-15), 1.3)
        self.assertAlmostEqual(normalize_energy_fj(4.2e-13), 420.0)
        self.assertAlmostEqual(normalize_energy_fj(4.5e-12), 4500.0)
        self.assertEqual(clamp_time_offset_s(-0.74), 0.0)
        self.assertAlmostEqual(clamp_time_offset_s(18.9), 18.9)
        self.assertEqual(make_flash_uid("GLM-L2-LCFA/2026/166/21/f1.nc", 42), "GLM-L2-LCFA/2026/166/21/f1.nc:42")

    def test_flash_uid_collision_prevention_and_pruning(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            conn = get_connection(db_path)
            now = 1700010000.0

            record_glm_batch(
                conn,
                s3_key="GLM-L2-LCFA/2026/166/21/file1.nc",
                processed_at_utc=int(now - 8000),
                flashes=[
                    {
                        "flash_uid": "GLM-L2-LCFA/2026/166/21/file1.nc:1",
                        "timestamp_utc": now - 3000.0,
                        "lat": 36.11,
                        "lon": -97.06,
                        "energy_fj": 120.0,
                    }
                ],
            )
            record_glm_batch(
                conn,
                s3_key="GLM-L2-LCFA/2026/166/21/file2.nc",
                processed_at_utc=int(now - 60),
                flashes=[
                    {
                        "flash_uid": "GLM-L2-LCFA/2026/166/21/file2.nc:1",
                        "timestamp_utc": now - 60.0,
                        "lat": 36.12,
                        "lon": -97.07,
                        "energy_fj": 250.0,
                    }
                ],
            )
            total_before = conn.execute("SELECT COUNT(*) FROM flashes").fetchone()[0]
            self.assertEqual(total_before, 2)

            stats = prune_old_records(conn, now_utc=now)
            self.assertEqual(stats["flashes_pruned"], 1)
            self.assertEqual(stats["processed_files_pruned"], 1)
            conn.close()

    def test_trend_insufficient_data_and_recovering_state_machine(self) -> None:
        dists = [12.0, 18.0, 22.0]
        ages = [2.0, 5.0, 15.0]
        self.assertEqual(compute_flash_trend(dists, ages), "insufficient_data")

        site = {"id": "stillwater", "name": "Stillwater YMCA", "lat": 36.1152, "lon": -97.0624}
        now_utc = 1700000000.0
        flashes = [
            {
                "flash_uid": "key:1",
                "timestamp_utc": now_utc - (20.8 * 60.0),
                "lat": 36.1152,
                "lon": -97.3168,
                "energy_fj": 420.0,
            }
        ]
        st = compute_site_status(
            site=site,
            flashes=flashes,
            raw_cells=[],
            now_utc=now_utc,
        )
        self.assertEqual(st["clearance_status"], "recovering")
        self.assertFalse(st["strike_within_policy_ring"])
        self.assertTrue(st["strike_within_advisory_ring"])
        self.assertAlmostEqual(st["mins_since_flash_within_15mi"], 20.8, places=1)
        self.assertAlmostEqual(st["clearance_remaining_min"], 9.2, places=1)
        self.assertEqual(st["trend"], "insufficient_data")


if __name__ == "__main__":
    unittest.main()
