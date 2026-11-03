"""Per-site YMCA 30-minute clearance state machine, ring counts, trends, and status.json builder."""

from __future__ import annotations

import datetime as dt
import statistics
from typing import Any, Dict, List, Optional, Sequence

from ingest.geo import bearing_deg, deg_to_cardinal, haversine_mi
from ingest.iem_cells import build_site_cells

MIN_FLASHES_FOR_TREND = 3
TREND_DELTA_THRESHOLD_MI = 1.5


def format_utc_iso(ts_utc: Optional[float]) -> Optional[str]:
    """Format Unix epoch seconds as ISO-8601 UTC string (`YYYY-MM-DDTHH:MM:SSZ`)."""
    if ts_utc is None:
        return None
    return (
        dt.datetime.fromtimestamp(float(ts_utc), tz=dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def compute_flash_trend(
    dists_mi: Sequence[float],
    ages_min: Sequence[float],
    max_radius_mi: float = 30.0,
) -> str:
    """
    Compute storm flash distance trend over the last 25 minutes inside `max_radius_mi`.

    Returns one of:
      - `"none"`: zero flashes within `max_radius_mi` in the last 25 minutes
      - `"insufficient_data"`: fewer than 3 flashes in either the recent (`0–10 min`)
        or comparison (`10–25 min`) window (avoids noisy onset/dissipation labels)
      - `"approaching"`: median distance decreased by >= 1.5 miles
      - `"receding"`: median distance increased by >= 1.5 miles
      - `"stationary"`: median distance change within (-1.5, +1.5) miles
    """
    pairs = [
        (float(d), float(a))
        for d, a in zip(dists_mi, ages_min)
        if float(d) <= max_radius_mi and 0.0 <= float(a) <= 25.0
    ]
    if not pairs:
        return "none"

    recent_dists = [d for d, a in pairs if a <= 10.0]
    prior_dists = [d for d, a in pairs if 10.0 < a <= 25.0]

    if len(recent_dists) < MIN_FLASHES_FOR_TREND or len(prior_dists) < MIN_FLASHES_FOR_TREND:
        return "insufficient_data"

    delta_mi = float(statistics.median(recent_dists) - statistics.median(prior_dists))
    if delta_mi <= -TREND_DELTA_THRESHOLD_MI:
        return "approaching"
    if delta_mi >= TREND_DELTA_THRESHOLD_MI:
        return "receding"
    return "stationary"


def compute_site_status(
    site: Dict[str, Any],
    flashes: List[Dict[str, Any]],
    raw_cells: List[Dict[str, Any]],
    now_utc: float,
    clearance_window_min: float = 30.0,
    policy_ring_mi: float = 10.0,
    advisory_ring_mi: float = 15.0,
) -> Dict[str, Any]:
    """
    Compute a single YMCA site's lightning clearance state, ring counts, nearest flash,
    trend, and deduplicated approaching NEXRAD cells.
    """
    site_lat = float(site["lat"])
    site_lon = float(site["lon"])

    valid_flashes = [
        f
        for f in flashes
        if 0.0 <= (float(now_utc) - float(f["timestamp_utc"])) <= (clearance_window_min * 60.0)
    ]

    f_lats: List[float] = []
    f_lons: List[float] = []
    f_ts: List[float] = []
    f_energies: List[float] = []
    dists_mi: List[float] = []
    ages_min: List[float] = []

    for f in valid_flashes:
        la = float(f["lat"])
        lo = float(f["lon"])
        ts = float(f["timestamp_utc"])
        en = float(f.get("energy_fj", 0.0))
        d = float(haversine_mi(site_lat, site_lon, la, lo))
        age_m = (float(now_utc) - ts) / 60.0

        f_lats.append(la)
        f_lons.append(lo)
        f_ts.append(ts)
        f_energies.append(en)
        dists_mi.append(d)
        ages_min.append(age_m)

    # Policy ring (<= 10 mi) & Advisory parallax ring (<= 15 mi) last-flash timers
    ts_within_10 = [ts for ts, d in zip(f_ts, dists_mi) if d <= policy_ring_mi]
    ts_within_15 = [ts for ts, d in zip(f_ts, dists_mi) if d <= advisory_ring_mi]

    if ts_within_10:
        last_10_ts = max(ts_within_10)
        mins_since_10: Optional[float] = round(max(0.0, (float(now_utc) - last_10_ts) / 60.0), 1)
        last_10_iso = format_utc_iso(last_10_ts)
    else:
        mins_since_10 = None
        last_10_iso = None

    if ts_within_15:
        last_15_ts = max(ts_within_15)
        mins_since_15: Optional[float] = round(max(0.0, (float(now_utc) - last_15_ts) / 60.0), 1)
        last_15_iso = format_utc_iso(last_15_ts)
    else:
        mins_since_15 = None
        last_15_iso = None

    strike_within_policy = mins_since_10 is not None and mins_since_10 <= clearance_window_min
    strike_within_advisory = mins_since_15 is not None and mins_since_15 <= clearance_window_min

    # Ring counts: 0-10, 10-15, 15-20, 20-30 mi over 5m, 15m, 30m
    ring_defs = [
        ("0_10", 0.0, 10.0),
        ("10_15", 10.0, 15.0),
        ("15_20", 15.0, 20.0),
        ("20_30", 20.0, 30.0),
    ]
    ring_counts: Dict[str, Dict[str, int]] = {}
    for key, r_min, r_max in ring_defs:
        if r_min == 0.0:
            ring_ages = [a for d, a in zip(dists_mi, ages_min) if d <= r_max]
        else:
            ring_ages = [a for d, a in zip(dists_mi, ages_min) if r_min < d <= r_max]
        ring_counts[key] = {
            "m5": sum(1 for a in ring_ages if a <= 5.0),
            "m15": sum(1 for a in ring_ages if a <= 15.0),
            "m30": sum(1 for a in ring_ages if a <= 30.0),
        }

    # Nearest flash within 30 miles in last 30 minutes
    within_30_indices = [i for i, d in enumerate(dists_mi) if d <= 30.0]
    nearest_flash: Optional[Dict[str, Any]] = None
    if within_30_indices:
        nearest_idx = min(within_30_indices, key=lambda i: dists_mi[i])
        n_dist = dists_mi[nearest_idx]
        n_brg = bearing_deg(site_lat, site_lon, f_lats[nearest_idx], f_lons[nearest_idx])
        nearest_flash = {
            "dist_mi": round(n_dist, 1),
            "bearing_deg": int(round(n_brg)) % 360,
            "bearing": deg_to_cardinal(n_brg),
            "age_min": round(ages_min[nearest_idx], 1),
            "energy_fj": round(f_energies[nearest_idx], 1),
        }

    trend = compute_flash_trend(dists_mi=dists_mi, ages_min=ages_min, max_radius_mi=30.0)

    site_cells = build_site_cells(
        raw_cells=raw_cells,
        site_lat=site_lat,
        site_lon=site_lon,
        policy_ring_mi=policy_ring_mi,
        advisory_ring_mi=advisory_ring_mi,
    )

    # Clearance State Machine:
    # 1. "active_hold": flash within 10 mi in last 30 min OR within 15 mi in last 10 min
    # 2. "recovering": flash within 15 mi between 10 and 30 min ago (countdown running)
    # 3. "approaching": no flash within 15 mi in 30m, but flashes in 15-30 mi with approaching trend
    #                   OR a tracked NEXRAD cell has eta_15mi_min <= 30
    # 4. "clear": otherwise
    if strike_within_advisory and mins_since_15 is not None:
        clearance_remaining_min = round(max(0.0, float(clearance_window_min) - mins_since_15), 1)
        if strike_within_policy or mins_since_15 <= 10.0:
            clearance_status = "active_hold"
        else:
            clearance_status = "recovering"
    else:
        clearance_remaining_min = 0.0
        flashes_15_30 = ring_counts["15_20"]["m30"] + ring_counts["20_30"]["m30"]
        cell_approaching = any(
            (c.get("eta_15mi_min") is not None and int(c["eta_15mi_min"]) <= 30)
            or c.get("tvs") not in (None, "NONE")
            for c in site_cells
        )
        if (flashes_15_30 > 0 and trend == "approaching") or cell_approaching:
            clearance_status = "approaching"
        else:
            clearance_status = "clear"

    return {
        "clearance_status": clearance_status,
        "strike_within_policy_ring": bool(strike_within_policy),
        "strike_within_advisory_ring": bool(strike_within_advisory),
        "last_flash_within_10mi_utc": last_10_iso,
        "mins_since_flash_within_10mi": mins_since_10,
        "last_flash_within_15mi_utc": last_15_iso,
        "mins_since_flash_within_15mi": mins_since_15,
        "clearance_remaining_min": clearance_remaining_min,
        "nearest_flash": nearest_flash,
        "ring_counts": ring_counts,
        "trend": trend,
        "cells": site_cells,
    }


def compile_status_payload(
    sites_config: Dict[str, Any],
    flashes: List[Dict[str, Any]],
    raw_cells: List[Dict[str, Any]],
    now_utc: float,
    newest_glm_age_s: Optional[int],
    glm_files_last_10min: int,
    iem_ok: bool,
    iem_age_s: Optional[int],
    max_recent_flashes: int = 250,
) -> Dict[str, Any]:
    """Assemble the complete `status.json` dictionary for upload to Cloudflare R2."""
    clearance_window_min = float(sites_config.get("clearance_window_minutes", 30.0))
    policy_ring_mi = float(sites_config.get("policy_ring_mi", 10.0))
    advisory_ring_mi = float(sites_config.get("advisory_ring_mi", 15.0))

    feed_ok = (
        newest_glm_age_s is not None
        and newest_glm_age_s <= 180
        and glm_files_last_10min >= 1
    )
    health_status = "ok" if feed_ok else "degraded"

    sites_dict: Dict[str, Any] = {}
    for site in sites_config.get("sites", []):
        site_id = str(site["id"])
        sites_dict[site_id] = compute_site_status(
            site=site,
            flashes=flashes,
            raw_cells=raw_cells,
            now_utc=now_utc,
            clearance_window_min=clearance_window_min,
            policy_ring_mi=policy_ring_mi,
            advisory_ring_mi=advisory_ring_mi,
        )

    recent_flashes_out: List[Dict[str, Any]] = []
    for f in flashes:
        age_s = int(round(max(0.0, float(now_utc) - float(f["timestamp_utc"]))))
        if age_s <= int(clearance_window_min * 60):
            recent_flashes_out.append(
                {
                    "lat": round(float(f["lat"]), 4),
                    "lon": round(float(f["lon"]), 4),
                    "age_s": age_s,
                    "energy_fj": round(float(f.get("energy_fj", 0.0)), 1),
                }
            )
            if len(recent_flashes_out) >= max_recent_flashes:
                break

    return {
        "schema_version": 1,
        "generated_at": format_utc_iso(now_utc),
        "meta": {
            "lightning_sensor": "GOES-19 GLM (total lightning: IC + CG)",
            "lightning_note": (
                "GLM detects both in-cloud (IC) and cloud-to-ground (CG) flashes; "
                "30-min safety holds reset on all optical flashes within 15 mi."
            ),
            "radar_attribution": "Radar mosaic & storm attributes © IEM · NEXRAD/NOAA",
            "eta_lead_time_note": (
                "ETAs reflect radar reflectivity core; anvil lightning can precede "
                "the radar core by 5–10 min."
            ),
        },
        "health": {
            "status": health_status,
            "feed_ok": bool(feed_ok),
            "newest_glm_age_s": newest_glm_age_s,
            "glm_files_last_10min": int(glm_files_last_10min),
            "iem_ok": bool(iem_ok),
            "iem_age_s": iem_age_s,
        },
        "sites": sites_dict,
        "recent_flashes": recent_flashes_out,
    }
