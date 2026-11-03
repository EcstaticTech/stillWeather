"""
Phase 2 CLI: Historical GOES-19 GLM storm replay & 30-minute clearance state-machine backtester.

Quantifies total-lightning (IC + CG) countdown resets, total hold duration, and state transitions
so facility thresholds and staff guidance are empirically validated before wiring UI controls.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from typing import Any, Dict, List, Optional

from ingest.goes_glm import (
    fetch_and_parse_glm_key,
    list_glm_keys_in_window,
    make_s3_client,
)
from ingest.metrics import compute_site_status, format_utc_iso


def parse_iso_utc(val: str) -> dt.datetime:
    iso = val.strip().replace("Z", "+00:00")
    parsed = dt.datetime.fromisoformat(iso)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def run_replay(
    sites_cfg: Dict[str, Any],
    site_id: str,
    start_utc: dt.datetime,
    hours: float,
    step_files: int = 3,
) -> Dict[str, Any]:
    """
    Replay historical GOES-19 GLM files across `[start_utc, start_utc + hours]`
    through the production 30-minute clearance state machine.
    """
    sites_by_id = {s["id"]: s for s in sites_cfg["sites"]}
    if site_id not in sites_by_id:
        raise ValueError(f"Unknown site '{site_id}'. Valid sites: {list(sites_by_id.keys())}")
    site = sites_by_id[site_id]
    bbox = sites_cfg["bbox"]

    end_utc = start_utc + dt.timedelta(hours=hours)
    s3 = make_s3_client()
    keys = list_glm_keys_in_window(start_utc=start_utc, end_utc=end_utc, s3_client=s3)

    print("=" * 84)
    print(f"GOES-19 GLM Historical Replay — Site: {site['name']} ({site_id})")
    print(f"Window : {format_utc_iso(start_utc.timestamp())} -> {format_utc_iso(end_utc.timestamp())}")
    print(f"S3 keys: {len(keys)} NetCDF files (~20s cadence)")
    print("=" * 84)

    rolling_flashes: List[Dict[str, Any]] = []
    seen_uids: set[str] = set()

    prev_status: Optional[str] = None
    prev_remaining: float = 0.0
    transitions: List[Dict[str, Any]] = []

    countdown_resets = 0
    policy_ring_resets = 0
    advisory_only_resets = 0
    hold_seconds = 0.0
    last_eval_ts: Optional[float] = None
    peak_30m_within_15mi = 0

    for idx, key in enumerate(keys):
        parsed = fetch_and_parse_glm_key(s3_key=key, bbox=bbox, s3_client=s3)
        sim_now_utc = parsed["scan_start_utc"] + 20.0

        for f in parsed["flashes"]:
            if f["flash_uid"] not in seen_uids:
                seen_uids.add(f["flash_uid"])
                rolling_flashes.append(f)

        # Prune rolling buffer to 45 minutes prior to sim_now_utc
        cutoff = sim_now_utc - 2700.0
        rolling_flashes = [f for f in rolling_flashes if float(f["timestamp_utc"]) >= cutoff]

        if (idx + 1) % max(1, step_files) != 0 and idx != len(keys) - 1:
            continue

        st = compute_site_status(
            site=site,
            flashes=rolling_flashes,
            raw_cells=[],
            now_utc=sim_now_utc,
            clearance_window_min=float(sites_cfg.get("clearance_window_minutes", 30.0)),
            policy_ring_mi=float(sites_cfg.get("policy_ring_mi", 10.0)),
            advisory_ring_mi=float(sites_cfg.get("advisory_ring_mi", 15.0)),
        )

        cur_status = st["clearance_status"]
        cur_rem = float(st["clearance_remaining_min"])
        cnt_15mi = st["ring_counts"]["0_10"]["m30"] + st["ring_counts"]["10_15"]["m30"]
        peak_30m_within_15mi = max(peak_30m_within_15mi, cnt_15mi)

        if last_eval_ts is not None and cur_status in ("active_hold", "recovering"):
            hold_seconds += max(0.0, sim_now_utc - last_eval_ts)

        # Detect a 30-minute timer reset while already in a hold/recovering window
        if (
            prev_status in ("active_hold", "recovering")
            and cur_status == "active_hold"
            and cur_rem > prev_remaining + 0.3
        ):
            countdown_resets += 1
            if st["mins_since_flash_within_10mi"] is not None and st["mins_since_flash_within_10mi"] <= 1.5:
                policy_ring_resets += 1
            else:
                advisory_only_resets += 1

        if cur_status != prev_status:
            nf = st["nearest_flash"]
            nf_desc = (
                f"{nf['dist_mi']:.1f} mi {nf['bearing']} ({nf['energy_fj']:.0f} fJ)"
                if nf
                else "none <30mi"
            )
            event = {
                "timestamp_utc": format_utc_iso(sim_now_utc),
                "from_status": prev_status or "INIT",
                "to_status": cur_status,
                "clearance_remaining_min": cur_rem,
                "nearest_flash": nf_desc,
                "trend": st["trend"],
            }
            transitions.append(event)
            print(
                f"[{event['timestamp_utc']}] {event['from_status']:<12s} -> {cur_status.upper():<12s} | "
                f"rem={cur_rem:4.1f}m | nearest={nf_desc} | trend={st['trend']}"
            )

        prev_status = cur_status
        prev_remaining = cur_rem
        last_eval_ts = sim_now_utc

    hold_mins = round(hold_seconds / 60.0, 1)
    summary = {
        "site_id": site_id,
        "files_processed": len(keys),
        "unique_bbox_flashes": len(seen_uids),
        "peak_30m_flashes_within_15mi": peak_30m_within_15mi,
        "total_hold_minutes": hold_mins,
        "countdown_resets_total": countdown_resets,
        "countdown_resets_inner_10mi": policy_ring_resets,
        "countdown_resets_parallax_10_15mi": advisory_only_resets,
        "transitions": transitions,
    }

    print("-" * 84)
    print(
        f"Replay Summary | Total Hold: {hold_mins} min | "
        f"Timer Resets (IC+CG): {countdown_resets} "
        f"(<=10mi: {policy_ring_resets}, 10-15mi buffer: {advisory_only_resets}) | "
        f"Peak 30m flashes <=15mi: {peak_30m_within_15mi}"
    )
    print("=" * 84)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Replay historical GOES-19 GLM data through the 30-min clearance state machine."
    )
    parser.add_argument("--start", required=True, help="Start UTC ISO timestamp (e.g. 2025-06-15T21:00:00Z)")
    parser.add_argument("--hours", type=float, default=3.0, help="Duration in hours to replay (default: 3.0)")
    parser.add_argument("--site", default="stillwater", help="Site ID from sites.json (default: stillwater)")
    parser.add_argument(
        "--sites-config",
        default=os.path.join(os.path.dirname(os.path.dirname(__file__)), "sites.json"),
        help="Path to sites.json",
    )
    parser.add_argument(
        "--step-files",
        type=int,
        default=3,
        help="Evaluate state machine every N 20s files (default: 3 = every 60s)",
    )
    args = parser.parse_args()

    with open(args.sites_config, "r", encoding="utf-8") as f:
        sites_cfg = json.load(f)

    run_replay(
        sites_cfg=sites_cfg,
        site_id=args.site,
        start_utc=parse_iso_utc(args.start),
        hours=args.hours,
        step_files=args.step_files,
    )


if __name__ == "__main__":
    main()
