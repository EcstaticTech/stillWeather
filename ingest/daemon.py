"""
Phase 3 Homelab Ingest Daemon (`python -m ingest.daemon`).

Polls NOAA GOES-19 GLM L2 LCFA NetCDF files from S3 and IEM NEXRAD storm cell attributes,
maintains a 45-minute rolling SQLite WAL database, writes a local `status.json`, uploads to
Cloudflare R2, and pings Healthchecks.io on healthy cycles.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import signal
import time
from typing import Any, Dict, Optional

import boto3
from botocore.config import Config
import requests

from ingest.db import (
    count_processed_files_since,
    fetch_active_iem_cells,
    fetch_recent_flashes,
    get_connection,
    get_processed_keys,
    prune_old_records,
    record_glm_batch,
    replace_iem_cells,
)
from ingest.goes_glm import (
    fetch_and_parse_glm_key,
    list_recent_glm_keys,
    make_s3_client,
    parse_scan_start_utc_from_key,
)
from ingest.iem_cells import fetch_iem_cells
from ingest.metrics import compile_status_payload

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
logging.Formatter.converter = time.gmtime
log = logging.getLogger("stillweather.daemon")


def make_r2_client() -> Optional[Any]:
    """Create a boto3 S3 client for Cloudflare R2 if credentials are configured."""
    endpoint = os.environ.get("R2_ENDPOINT_URL", "").strip()
    access_key = os.environ.get("R2_ACCESS_KEY_ID", "").strip()
    secret_key = os.environ.get("R2_SECRET_ACCESS_KEY", "").strip()
    if not (endpoint and access_key and secret_key):
        return None
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto",
        config=Config(
            connect_timeout=10,
            read_timeout=15,
            retries={"max_attempts": 2, "mode": "standard"},
        ),
    )


def write_atomic_json(path: str, payload: Dict[str, Any]) -> str:
    """Atomically write compact JSON to `path` and return serialized string."""
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)
    tmp_path = f"{path}.tmp"
    serialized = json.dumps(payload, separators=(",", ":"), sort_keys=False)
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(serialized)
    os.replace(tmp_path, path)
    return serialized


def upload_status_to_r2(r2_client: Any, bucket: str, key: str, body_str: str) -> None:
    """Upload serialized `status.json` to Cloudflare R2 with 30s edge Cache-Control."""
    r2_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=body_str.encode("utf-8"),
        ContentType="application/json",
        CacheControl="public, max-age=30",
    )


def ping_healthcheck(ping_url: str) -> None:
    """Send a non-blocking dead-man-switch ping to Healthchecks.io (or Uptime Kuma)."""
    if not ping_url:
        return
    try:
        requests.get(ping_url, timeout=5.0)
    except Exception as exc:
        log.warning("Healthcheck ping failed: %s", exc)


class IngestCycleRunner:
    """Stateful runner coordinating GOES-19 S3 polling, IEM NEXRAD polling, and R2 publishing."""

    def __init__(
        self,
        sites_cfg: Dict[str, Any],
        db_path: str,
        output_json_path: str,
        iem_poll_interval_s: int = 90,
        dry_run: bool = False,
    ) -> None:
        self.sites_cfg = sites_cfg
        self.bbox = sites_cfg["bbox"]
        self.conn = get_connection(db_path)
        self.output_json_path = output_json_path
        self.iem_poll_interval_s = iem_poll_interval_s
        self.dry_run = dry_run

        self.goes_s3 = make_s3_client()
        self.r2_client = None if dry_run else make_r2_client()
        self.r2_bucket = os.environ.get("R2_BUCKET_NAME", "stillweather").strip()
        self.r2_object_key = os.environ.get("R2_OBJECT_KEY", "status.json").strip()
        self.hc_ping_url = os.environ.get("HEALTHCHECKS_PING_URL", "").strip()

        self.last_iem_poll_utc: float = 0.0
        self.iem_ok: bool = False
        self.newest_glm_scan_utc: Optional[float] = None

    def run_single_cycle(self) -> Dict[str, Any]:
        now_utc = dt.datetime.now(dt.timezone.utc).timestamp()

        # 1. Poll GOES-19 GLM L2 LCFA keys from S3
        try:
            recent_keys = list_recent_glm_keys(s3_client=self.goes_s3, max_files=35)
            already_done = get_processed_keys(self.conn, recent_keys)
            new_keys = [k for k in recent_keys if k not in already_done]

            new_flashes_total = 0
            for key in new_keys:
                parsed = fetch_and_parse_glm_key(
                    s3_key=key, bbox=self.bbox, s3_client=self.goes_s3
                )
                record_glm_batch(
                    self.conn,
                    s3_key=key,
                    processed_at_utc=int(now_utc),
                    flashes=parsed["flashes"],
                )
                new_flashes_total += len(parsed["flashes"])

            if recent_keys:
                latest_scan = parse_scan_start_utc_from_key(recent_keys[-1])
                if latest_scan is not None:
                    # Scan duration is 20s; age is measured from end of scan window
                    self.newest_glm_scan_utc = latest_scan + 20.0

            log.info(
                "GLM poll complete: %d new files ingested (%d OK flashes), %d listed",
                len(new_keys),
                new_flashes_total,
                len(recent_keys),
            )
        except Exception as exc:
            log.error("GOES-19 GLM poll error: %s", exc)

        # 2. Poll IEM NEXRAD attributes every `iem_poll_interval_s` (default 90s)
        if (now_utc - self.last_iem_poll_utc) >= self.iem_poll_interval_s:
            try:
                cells = fetch_iem_cells(bbox=self.bbox)
                replace_iem_cells(self.conn, cells=cells, fetched_at_utc=int(now_utc))
                self.last_iem_poll_utc = now_utc
                self.iem_ok = True
                log.info("IEM NEXRAD poll complete: %d cells in extended OK bbox", len(cells))
            except Exception as exc:
                self.iem_ok = False
                log.error("IEM NEXRAD poll error: %s", exc)

        # 3. Prune rolling tables (flashes & cells > 45 min, processed_files > 2 hr)
        prune_old_records(self.conn, now_utc=now_utc)

        # 4. Compile status.json payload
        flashes_30m = fetch_recent_flashes(self.conn, since_utc=now_utc - 1800.0)
        active_cells = fetch_active_iem_cells(self.conn, since_utc=now_utc - 1200.0)
        files_10m = count_processed_files_since(self.conn, since_utc=now_utc - 600.0)

        newest_glm_age_s: Optional[int] = None
        if self.newest_glm_scan_utc is not None:
            newest_glm_age_s = int(round(max(0.0, now_utc - self.newest_glm_scan_utc)))

        iem_age_s: Optional[int] = None
        if self.last_iem_poll_utc > 0:
            iem_age_s = int(round(max(0.0, now_utc - self.last_iem_poll_utc)))

        payload = compile_status_payload(
            sites_config=self.sites_cfg,
            flashes=flashes_30m,
            raw_cells=active_cells,
            now_utc=now_utc,
            newest_glm_age_s=newest_glm_age_s,
            glm_files_last_10min=files_10m,
            iem_ok=self.iem_ok,
            iem_age_s=iem_age_s,
        )

        # 5. Write local status.json atomically
        serialized = write_atomic_json(self.output_json_path, payload)
        log.info(
            "Wrote %s (%d bytes, feed_ok=%s)",
            self.output_json_path,
            len(serialized),
            payload["health"]["feed_ok"],
        )

        # 6. Upload to Cloudflare R2 (unless --dry-run or unconfigured)
        if not self.dry_run and self.r2_client is not None:
            try:
                upload_status_to_r2(
                    self.r2_client,
                    bucket=self.r2_bucket,
                    key=self.r2_object_key,
                    body_str=serialized,
                )
                log.info("Uploaded %s to R2 bucket '%s'", self.r2_object_key, self.r2_bucket)
            except Exception as exc:
                log.error("R2 upload failed: %s", exc)

        # 7. Dead-man-switch ping if feed is healthy
        if not self.dry_run and payload["health"]["feed_ok"]:
            ping_healthcheck(self.hc_ping_url)

        return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="stillWeather GOES-19 GLM & IEM NEXRAD ingest daemon")
    parser.add_argument(
        "--sites",
        default=os.environ.get(
            "SITES_CONFIG_PATH",
            os.path.join(os.path.dirname(os.path.dirname(__file__)), "sites.json"),
        ),
        help="Path to sites.json",
    )
    parser.add_argument(
        "--db",
        default=os.environ.get("SQLITE_DB_PATH", "stillweather_cache.db"),
        help="Path to SQLite WAL database",
    )
    parser.add_argument(
        "--out",
        default=os.environ.get("STATUS_JSON_PATH", "status.json"),
        help="Local output path for status.json",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=int(os.environ.get("GLM_POLL_INTERVAL_SECONDS", "45")),
        help="Main poll interval in seconds (default: 45)",
    )
    parser.add_argument(
        "--iem-interval",
        type=int,
        default=int(os.environ.get("IEM_POLL_INTERVAL_SECONDS", "90")),
        help="IEM NEXRAD poll interval in seconds (default: 90)",
    )
    parser.add_argument("--once", action="store_true", help="Run a single ingest cycle and exit")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip R2 upload and Healthchecks.io ping (writes local SQLite & status.json only)",
    )
    args = parser.parse_args()

    with open(args.sites, "r", encoding="utf-8") as f:
        sites_cfg = json.load(f)

    runner = IngestCycleRunner(
        sites_cfg=sites_cfg,
        db_path=args.db,
        output_json_path=args.out,
        iem_poll_interval_s=args.iem_interval,
        dry_run=args.dry_run,
    )

    stop_requested = False

    def _handle_sig(signum, _frame):
        nonlocal stop_requested
        log.info("Received signal %s, shutting down cleanly...", signum)
        stop_requested = True

    signal.signal(signal.SIGINT, _handle_sig)
    signal.signal(signal.SIGTERM, _handle_sig)

    if args.once:
        runner.run_single_cycle()
        return

    log.info("Starting continuous ingest loop (GLM=%ds, IEM=%ds)", args.interval, args.iem_interval)
    while not stop_requested:
        t0 = time.monotonic()
        runner.run_single_cycle()
        elapsed = time.monotonic() - t0
        sleep_s = max(1.0, float(args.interval) - elapsed)
        end_sleep = time.monotonic() + sleep_s
        while time.monotonic() < end_sleep and not stop_requested:
            time.sleep(0.5)


if __name__ == "__main__":
    main()
