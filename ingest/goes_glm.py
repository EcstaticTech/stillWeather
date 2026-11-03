"""NOAA GOES-19 GLM L2 LCFA S3 discovery and in-memory NetCDF4 parser."""

from __future__ import annotations

import datetime as dt
import math
import re
from typing import Any, Dict, List, Optional

try:
    import boto3
    from botocore import UNSIGNED
    from botocore.config import Config
except ImportError:  # pragma: no cover
    boto3 = None  # type: ignore[assignment]
    UNSIGNED = None  # type: ignore[assignment]
    Config = None  # type: ignore[assignment]

try:
    import netCDF4
    import numpy as np
except ImportError:  # pragma: no cover
    netCDF4 = None  # type: ignore[assignment]
    np = None  # type: ignore[assignment]

BUCKET_NAME = "noaa-goes19"
PRODUCT_PREFIX = "GLM-L2-LCFA"

# Example filename:
# OR_GLM-L2-LCFA_G19_s20261662100000_e20261662100200_c20261662100223.nc
FILENAME_SCAN_RE = re.compile(r"_s(\d{4})(\d{3})(\d{2})(\d{2})(\d{2})(\d)")


def make_s3_client():
    """Create an anonymous S3 client for public NOAA GOES buckets."""
    if boto3 is None:
        raise RuntimeError("boto3 is required for S3 operations (pip install -r ingest/requirements.txt)")
    return boto3.client(
        "s3",
        region_name="us-east-1",
        config=Config(
            signature_version=UNSIGNED,
            connect_timeout=10,
            read_timeout=20,
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )


def s3_hour_prefix(utc_dt: dt.datetime) -> str:
    """Return S3 prefix `GLM-L2-LCFA/YYYY/DDD/HH/` for a UTC datetime."""
    if utc_dt.tzinfo is None:
        utc_dt = utc_dt.replace(tzinfo=dt.timezone.utc)
    else:
        utc_dt = utc_dt.astimezone(dt.timezone.utc)
    day_of_year = utc_dt.timetuple().tm_yday
    return f"{PRODUCT_PREFIX}/{utc_dt.year:04d}/{day_of_year:03d}/{utc_dt.hour:02d}/"


def recent_s3_prefixes(now_utc: Optional[dt.datetime] = None, rollover_minutes: int = 12) -> List[str]:
    """
    Return the S3 hour prefix(es) to list for recent GLM polling.
    Includes the previous UTC hour when within `rollover_minutes` of the hour boundary
    (handles both hourly and 00Z day/year rollovers cleanly).
    """
    if now_utc is None:
        now_utc = dt.datetime.now(dt.timezone.utc)
    elif now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=dt.timezone.utc)

    prefixes = []
    if now_utc.minute < rollover_minutes:
        prev_hour = now_utc - dt.timedelta(hours=1)
        prefixes.append(s3_hour_prefix(prev_hour))
    curr_prefix = s3_hour_prefix(now_utc)
    if curr_prefix not in prefixes:
        prefixes.append(curr_prefix)
    return prefixes


def parse_scan_start_utc_from_key(s3_key: str) -> Optional[float]:
    """Extract Unix timestamp (UTC seconds) of scan start from GOES GLM S3 key."""
    m = FILENAME_SCAN_RE.search(s3_key)
    if not m:
        return None
    year = int(m.group(1))
    doy = int(m.group(2))
    hour = int(m.group(3))
    minute = int(m.group(4))
    second = int(m.group(5))
    tenths = int(m.group(6))

    base_dt = dt.datetime(year, 1, 1, hour, minute, second, tzinfo=dt.timezone.utc) + dt.timedelta(
        days=doy - 1, seconds=tenths * 0.1
    )
    return base_dt.timestamp()


def list_recent_glm_keys(
    s3_client=None,
    now_utc: Optional[dt.datetime] = None,
    max_files: int = 35,
    rollover_minutes: int = 12,
) -> List[str]:
    """List the newest `max_files` GLM L2 LCFA object keys across current/previous hour prefixes."""
    if s3_client is None:
        s3_client = make_s3_client()
    prefixes = recent_s3_prefixes(now_utc=now_utc, rollover_minutes=rollover_minutes)
    keys: List[str] = []
    for prefix in prefixes:
        paginator = s3_client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=BUCKET_NAME, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj.get("Key", "")
                if key.endswith(".nc"):
                    keys.append(key)
    keys.sort()
    return keys[-max_files:] if max_files > 0 else keys


def list_glm_keys_in_window(
    start_utc: dt.datetime,
    end_utc: dt.datetime,
    s3_client=None,
) -> List[str]:
    """List all GLM L2 LCFA keys whose scan start falls within `[start_utc, end_utc]`."""
    if s3_client is None:
        s3_client = make_s3_client()
    if start_utc.tzinfo is None:
        start_utc = start_utc.replace(tzinfo=dt.timezone.utc)
    if end_utc.tzinfo is None:
        end_utc = end_utc.replace(tzinfo=dt.timezone.utc)

    start_ts = start_utc.timestamp()
    end_ts = end_utc.timestamp()

    cursor = start_utc.replace(minute=0, second=0, microsecond=0)
    prefixes: List[str] = []
    while cursor <= end_utc:
        p = s3_hour_prefix(cursor)
        if p not in prefixes:
            prefixes.append(p)
        cursor += dt.timedelta(hours=1)

    matched_keys: List[str] = []
    paginator = s3_client.get_paginator("list_objects_v2")
    for prefix in prefixes:
        for page in paginator.paginate(Bucket=BUCKET_NAME, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj.get("Key", "")
                if not key.endswith(".nc"):
                    continue
                scan_ts = parse_scan_start_utc_from_key(key)
                if scan_ts is not None and start_ts <= scan_ts <= end_ts:
                    matched_keys.append(key)
    matched_keys.sort()
    return matched_keys


def normalize_energy_fj(raw_energy: float) -> float:
    """
    Normalize decoded GOES GLM `flash_energy` to femtofoules (fJ = 10^-15 J).

    Empirically verified on live GOES-19 L2 LCFA files:
    `flash_energy` carries `units='J'` and decodes via CF scale_factor/add_offset to
    `1.3e-15 .. 4.5e-12` (i.e. Joules at femtofoule magnitude). Multiply values in
    `(0, 1e-6)` by `1e15` so `energy_fj` is stored in femtofoules (e.g. `1.3 .. 4500.0`).
    """
    val = float(raw_energy)
    if math.isnan(val) or val <= 0.0:
        return 0.0
    if val < 1e-6:
        return round(val * 1e15, 2)
    return round(val, 2)


def clamp_time_offset_s(raw_offset_s: float) -> float:
    """
    Clamp decoded `flash_time_offset_of_first_event` to `>= 0.0` seconds.
    Live GOES-19 files occasionally contain small negative offsets (e.g. `-0.74 s`)
    for flashes beginning nominally before scan start.
    """
    val = float(raw_offset_s)
    if math.isnan(val) or val < 0.0:
        return 0.0
    return val


def make_flash_uid(s3_key: str, flash_id: int) -> str:
    """
    Build a globally unique flash primary key.
    Because `flash_id` resets in every 20-second GLM NetCDF file, combining
    `s3_key` and `flash_id` prevents cross-file primary-key collisions.
    """
    return f"{s3_key}:{int(flash_id)}"


def parse_glm_netcdf_bytes(
    nc_bytes: bytes,
    s3_key: str,
    bbox: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """
    Parse an in-memory GOES-19 GLM L2 LCFA NetCDF payload and extract quality-controlled
    flashes (`flash_quality_flag == 0`) inside `bbox`.
    """
    if netCDF4 is None or np is None:
        raise RuntimeError(
            "netCDF4 and numpy are required to parse GLM NetCDF files (pip install -r ingest/requirements.txt)"
        )

    scan_start_utc = parse_scan_start_utc_from_key(s3_key)

    with netCDF4.Dataset("inmem.nc", mode="r", memory=nc_bytes) as ds:
        if scan_start_utc is None:
            t_start_attr = getattr(ds, "time_coverage_start", None)
            if t_start_attr:
                iso_str = str(t_start_attr).replace("Z", "+00:00")
                scan_start_utc = dt.datetime.fromisoformat(iso_str).timestamp()
            elif "product_time" in ds.variables:
                pt = float(np.asarray(ds.variables["product_time"][...]).flat[0])
                scan_start_utc = 946728000.0 + pt
            else:
                scan_start_utc = dt.datetime.now(dt.timezone.utc).timestamp()

        if "number_of_flashes" in ds.dimensions and len(ds.dimensions["number_of_flashes"]) == 0:
            return {
                "s3_key": s3_key,
                "scan_start_utc": scan_start_utc,
                "total_file_flashes": 0,
                "flashes": [],
            }

        if "flash_lat" not in ds.variables or "flash_lon" not in ds.variables:
            return {
                "s3_key": s3_key,
                "scan_start_utc": scan_start_utc,
                "total_file_flashes": 0,
                "flashes": [],
            }

        lats = np.ma.filled(ds.variables["flash_lat"][...], np.nan).astype(np.float64)
        lons = np.ma.filled(ds.variables["flash_lon"][...], np.nan).astype(np.float64)
        total_flashes = int(lats.size)
        if total_flashes == 0:
            return {
                "s3_key": s3_key,
                "scan_start_utc": scan_start_utc,
                "total_file_flashes": 0,
                "flashes": [],
            }

        if "flash_id" in ds.variables:
            flash_ids = np.ma.filled(ds.variables["flash_id"][...], -1).astype(np.int64)
        else:
            flash_ids = np.arange(total_flashes, dtype=np.int64)

        if "flash_quality_flag" in ds.variables:
            qflags = np.ma.filled(ds.variables["flash_quality_flag"][...], 99).astype(np.int64)
        else:
            qflags = np.zeros(total_flashes, dtype=np.int64)

        if "flash_time_offset_of_first_event" in ds.variables:
            time_offsets = np.ma.filled(
                ds.variables["flash_time_offset_of_first_event"][...], 0.0
            ).astype(np.float64)
        else:
            time_offsets = np.zeros(total_flashes, dtype=np.float64)

        if "flash_energy" in ds.variables:
            energies = np.ma.filled(ds.variables["flash_energy"][...], 0.0).astype(np.float64)
        else:
            energies = np.zeros(total_flashes, dtype=np.float64)

    mask = (qflags == 0) & np.isfinite(lats) & np.isfinite(lons)
    if bbox is not None:
        mask &= (
            (lats >= float(bbox["lat_min"]))
            & (lats <= float(bbox["lat_max"]))
            & (lons >= float(bbox["lon_min"]))
            & (lons <= float(bbox["lon_max"]))
        )

    indices = np.nonzero(mask)[0]
    flashes: List[Dict[str, Any]] = []
    for idx in indices:
        fid = int(flash_ids[idx])
        offset_s = clamp_time_offset_s(float(time_offsets[idx]))
        flash_ts = float(scan_start_utc) + offset_s
        flashes.append(
            {
                "flash_uid": make_flash_uid(s3_key, fid),
                "flash_id": fid,
                "timestamp_utc": round(flash_ts, 3),
                "lat": round(float(lats[idx]), 5),
                "lon": round(float(lons[idx]), 5),
                "energy_fj": normalize_energy_fj(float(energies[idx])),
            }
        )

    return {
        "s3_key": s3_key,
        "scan_start_utc": float(scan_start_utc),
        "total_file_flashes": total_flashes,
        "flashes": flashes,
    }


def fetch_and_parse_glm_key(
    s3_key: str,
    bbox: Optional[Dict[str, float]] = None,
    s3_client=None,
) -> Dict[str, Any]:
    """Download a single GOES-19 GLM NetCDF object into memory and parse flashes."""
    if s3_client is None:
        s3_client = make_s3_client()
    resp = s3_client.get_object(Bucket=BUCKET_NAME, Key=s3_key)
    nc_bytes = resp["Body"].read()
    return parse_glm_netcdf_bytes(nc_bytes=nc_bytes, s3_key=s3_key, bbox=bbox)
