"""Phase 1 CLI: Fetch recent GOES-19 GLM L2 LCFA files and print per-site Oklahoma flash summaries."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from typing import Any, Dict, List

from ingest.goes_glm import fetch_and_parse_glm_key, list_recent_glm_keys, make_s3_client
from ingest.metrics import compute_site_status


def load_sites_config(path: str = "sites.json") -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Peek at the newest GOES-19 GLM NetCDF files over Oklahoma and summarize per YMCA site."
    )
    parser.add_argument(
        "--sites",
        default=os.path.join(os.path.dirname(os.path.dirname(__file__)), "sites.json"),
        help="Path to sites.json configuration",
    )
    parser.add_argument(
        "--files",
        type=int,
        default=9,
        help="Number of recent 20-second GLM NetCDF files to inspect (default: 9 = ~3 minutes)",
    )
    args = parser.parse_args()

    sites_cfg = load_sites_config(args.sites)
    bbox = sites_cfg["bbox"]
    s3 = make_s3_client()

    keys = list_recent_glm_keys(s3_client=s3, max_files=max(1, args.files))
    if not keys:
        print("No GOES-19 GLM files found in current/previous UTC hour prefix.")
        return

    all_flashes: List[Dict[str, Any]] = []
    total_raw = 0
    for key in keys:
        parsed = fetch_and_parse_glm_key(s3_key=key, bbox=bbox, s3_client=s3)
        total_raw += parsed["total_file_flashes"]
        all_flashes.extend(parsed["flashes"])

    now_utc = dt.datetime.now(dt.timezone.utc).timestamp()
    newest_scan_ts = parsed["scan_start_utc"]
    newest_age_s = int(round(max(0.0, now_utc - newest_scan_ts)))

    print("=" * 76)
    print(f"GOES-19 GLM Live Peek | Files inspected: {len(keys)} | Newest scan age: ~{newest_age_s}s")
    print(f"Newest S3 Key : {keys[-1]}")
    print(f"Total flashes : {total_raw} hemisphere-wide | {len(all_flashes)} in OK bbox (qflag==0)")
    print("-" * 76)

    for site in sites_cfg["sites"]:
        st = compute_site_status(
            site=site,
            flashes=all_flashes,
            raw_cells=[],
            now_utc=now_utc,
            clearance_window_min=float(sites_cfg.get("clearance_window_minutes", 30.0)),
            policy_ring_mi=float(sites_cfg.get("policy_ring_mi", 10.0)),
            advisory_ring_mi=float(sites_cfg.get("advisory_ring_mi", 15.0)),
        )
        nf = st["nearest_flash"]
        rc = st["ring_counts"]
        if nf:
            nf_str = (
                f"{nf['dist_mi']:4.1f} mi {nf['bearing']:<3s} "
                f"({nf['age_min']:.1f}m ago, {nf['energy_fj']:.1f} fJ)"
            )
        else:
            nf_str = "None within 30 mi"

        print(
            f"[{site['id']:<10s}] status={st['clearance_status']:<12s} | "
            f"nearest={nf_str} | "
            f"rings(30m) 0-10:{rc['0_10']['m30']} 10-15:{rc['10_15']['m30']} "
            f"15-20:{rc['15_20']['m30']} 20-30:{rc['20_30']['m30']}"
        )
    print("=" * 76)


if __name__ == "__main__":
    main()
