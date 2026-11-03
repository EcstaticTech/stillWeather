"""IEM NEXRAD storm cell attribute fetcher and cross-radar spatial deduplication."""

from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Optional

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore[assignment]

from ingest.geo import (
    LEAD_TIME_NOTE,
    bucket_eta_5min,
    compute_cpa_and_eta,
    format_eta_range_label,
    haversine_mi,
)

IEM_NEXRAD_ATTR_URL = "https://mesonet.agron.iastate.edu/geojson/nexrad_attr.py"
USER_AGENT = "stillWeather-homelab/1.0 (+https://github.com/EcstaticTech/stillWeather)"

# Known WSR-88D & TDWR radar coordinates covering Oklahoma and adjacent states
NEXRAD_COORDS: Dict[str, tuple[float, float]] = {
    "TLX": (35.3331, -97.2778),  # Oklahoma City (Twin Lakes), OK
    "OKC": (35.2361, -97.4603),  # TDWR Oklahoma City, OK
    "TUL": (36.0703, -95.8264),  # TDWR Tulsa, OK
    "VNX": (36.7406, -98.1278),  # Vance AFB (Enid), OK
    "INX": (36.1750, -95.5644),  # Tulsa (Inola), OK
    "FDR": (34.3622, -98.9764),  # Frederick, OK
    "SRX": (35.2906, -94.3619),  # Fort Smith, AR
    "ICT": (37.6544, -97.4431),  # Wichita, KS
    "DDC": (37.7608, -99.9689),  # Dodge City, KS
    "AMA": (35.2333, -101.7092), # Amarillo, TX
    "FWS": (32.5731, -97.3031),  # Fort Worth, TX
    "SGF": (37.2353, -93.4006),  # Springfield, MO
    "EAX": (38.8103, -94.2644),  # Kansas City, MO
    "TWX": (38.9969, -96.2325),  # Topeka, KS
    "LBB": (33.6542, -101.8142), # Lubbock, TX
}

CELL_BBOX_MARGIN_DEG = 0.6
CROSS_RADAR_CLUSTER_RADIUS_MI = 5.5


def parse_iem_valid_utc(valid_str: Optional[str], fallback_utc: int) -> int:
    """Parse ISO8601 `valid` timestamp from IEM GeoJSON properties."""
    if not valid_str:
        return int(fallback_utc)
    try:
        iso = str(valid_str).replace("Z", "+00:00")
        return int(dt.datetime.fromisoformat(iso).timestamp())
    except Exception:
        return int(fallback_utc)


def radar_range_to_cell_mi(nexrad: str, cell_lat: float, cell_lon: float) -> float:
    """Compute distance in statute miles from `nexrad` station to `(cell_lat, cell_lon)`."""
    code = (nexrad or "").upper().strip()
    if len(code) == 4 and code.startswith("K"):
        code = code[1:]
    elif len(code) == 4 and code.startswith("T"):
        code = code[1:]
    coords = NEXRAD_COORDS.get(code)
    if not coords:
        return 999.0
    return round(float(haversine_mi(coords[0], coords[1], cell_lat, cell_lon)), 1)


def parse_iem_geojson(
    payload: Dict[str, Any],
    bbox: Optional[Dict[str, float]] = None,
    now_utc: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """
    Parse IEM `nexrad_attr.py` GeoJSON into normalized per-radar cell dictionaries.
    Preserves cells with `sknt == 0` or missing `drct` so stationary storms are not dropped.
    """
    if now_utc is None:
        now_utc = dt.datetime.now(dt.timezone.utc).timestamp()
    fallback_ts = int(now_utc)

    features = payload.get("features") or []
    cells: List[Dict[str, Any]] = []

    for feat in features:
        props = feat.get("properties") or {}
        geom = feat.get("geometry") or {}
        coords = geom.get("coordinates") or []

        if len(coords) >= 2 and coords[0] is not None and coords[1] is not None:
            lon = float(coords[0])
            lat = float(coords[1])
        elif props.get("lat") is not None and props.get("lon") is not None:
            lat = float(props["lat"])
            lon = float(props["lon"])
        else:
            continue

        if bbox is not None:
            if not (
                (float(bbox["lat_min"]) - CELL_BBOX_MARGIN_DEG)
                <= lat
                <= (float(bbox["lat_max"]) + CELL_BBOX_MARGIN_DEG)
                and (float(bbox["lon_min"]) - CELL_BBOX_MARGIN_DEG)
                <= lon
                <= (float(bbox["lon_max"]) + CELL_BBOX_MARGIN_DEG)
            ):
                continue

        nexrad = str(props.get("nexrad") or "").upper().strip()
        storm_id = str(props.get("storm_id") or "").upper().strip()
        if not nexrad or not storm_id:
            continue

        valid_utc = parse_iem_valid_utc(props.get("valid"), fallback_ts)
        drct = float(props["drct"]) if props.get("drct") is not None else None
        sknt = float(props["sknt"]) if props.get("sknt") is not None else None
        max_dbz = float(props["max_dbz"]) if props.get("max_dbz") is not None else 0.0
        top_kft = float(props["top"]) if props.get("top") is not None else 0.0
        tvs = str(props.get("tvs") or "NONE").upper().strip()
        meso = str(props.get("meso") or "NONE").upper().strip()

        cells.append(
            {
                "nexrad": nexrad,
                "storm_id": storm_id,
                "valid_utc": valid_utc,
                "lat": round(lat, 5),
                "lon": round(lon, 5),
                "drct": drct,
                "sknt": sknt,
                "max_dbz": max_dbz,
                "top_kft": top_kft,
                "tvs": tvs,
                "meso": meso,
            }
        )

    return cells


def fetch_iem_cells(
    bbox: Optional[Dict[str, float]] = None,
    timeout_s: float = 12.0,
) -> List[Dict[str, Any]]:
    """Fetch live IEM NEXRAD storm cell attributes over HTTPS."""
    if requests is None:
        raise RuntimeError("requests is required to poll IEM NEXRAD (pip install -r ingest/requirements.txt)")
    resp = requests.get(
        IEM_NEXRAD_ATTR_URL,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=timeout_s,
    )
    resp.raise_for_status()
    payload = resp.json()
    return parse_iem_geojson(payload, bbox=bbox)


def _min_optional(vals: List[Optional[float]]) -> Optional[float]:
    present = [float(v) for v in vals if v is not None]
    return min(present) if present else None


def build_site_cells(
    raw_cells: List[Dict[str, Any]],
    site_lat: float,
    site_lon: float,
    max_site_dist_mi: float = 60.0,
    policy_ring_mi: float = 10.0,
    advisory_ring_mi: float = 15.0,
    cluster_radius_mi: float = CROSS_RADAR_CLUSTER_RADIUS_MI,
) -> List[Dict[str, Any]]:
    """
    Compute per-site CPA/ETA metrics and deduplicate physical cells across overlapping radars.
    """
    candidates: List[Dict[str, Any]] = []
    for cell in raw_cells:
        c_lat = float(cell["lat"])
        c_lon = float(cell["lon"])
        kin = compute_cpa_and_eta(
            site_lat=site_lat,
            site_lon=site_lon,
            cell_lat=c_lat,
            cell_lon=c_lon,
            drct_from_deg=cell.get("drct"),
            sknt=cell.get("sknt"),
            ring_radii_mi=(advisory_ring_mi, policy_ring_mi),
        )
        if kin["dist_mi"] > max_site_dist_mi:
            continue

        nexrad = str(cell["nexrad"]).upper()
        storm_id = str(cell["storm_id"]).upper()
        r_range = radar_range_to_cell_mi(nexrad, c_lat, c_lon)

        candidates.append(
            {
                "id": f"{nexrad}_{storm_id}",
                "nexrad": nexrad,
                "storm_id": storm_id,
                "lat": c_lat,
                "lon": c_lon,
                "radar_range_mi": r_range,
                "max_dbz": int(round(float(cell.get("max_dbz") or 0.0))),
                "top_kft": int(round(float(cell.get("top_kft") or 0.0))),
                "tvs": str(cell.get("tvs") or "NONE").upper(),
                "meso": str(cell.get("meso") or "NONE").upper(),
                **kin,
            }
        )

    candidates.sort(key=lambda c: (c["radar_range_mi"], c["dist_mi"]))

    clusters: List[List[Dict[str, Any]]] = []
    for cand in candidates:
        placed = False
        for cluster in clusters:
            existing_radars = {m["nexrad"] for m in cluster}
            if cand["nexrad"] in existing_radars:
                continue
            seed = cluster[0]
            sep_mi = float(haversine_mi(seed["lat"], seed["lon"], cand["lat"], cand["lon"]))
            if sep_mi <= cluster_radius_mi:
                cluster.append(cand)
                placed = True
                break
        if not placed:
            clusters.append([cand])

    deduped: List[Dict[str, Any]] = []
    for cluster in clusters:
        primary = dict(cluster[0])
        radars = sorted({m["nexrad"] for m in cluster})

        min_cpa = _min_optional([m.get("cpa_mi") for m in cluster])
        min_raw_eta15 = _min_optional([m.get("raw_eta_15mi_min") for m in cluster])
        min_raw_eta10 = _min_optional([m.get("raw_eta_10mi_min") for m in cluster])

        if not primary["has_motion"]:
            for m in cluster[1:]:
                if m["has_motion"]:
                    for k in (
                        "has_motion",
                        "drct_from_deg",
                        "heading_to_deg",
                        "heading_cardinal",
                        "speed_mph",
                    ):
                        primary[k] = m[k]
                    break

        primary["observed_by_radars"] = radars
        primary["cpa_mi"] = round(min_cpa, 1) if min_cpa is not None else None
        primary["eta_15mi_min"] = bucket_eta_5min(min_raw_eta15)
        primary["eta_15mi_label"] = format_eta_range_label(min_raw_eta15)
        primary["eta_10mi_min"] = bucket_eta_5min(min_raw_eta10)
        primary["eta_10mi_label"] = format_eta_range_label(min_raw_eta10)
        primary["lead_time_note"] = LEAD_TIME_NOTE
        primary["max_dbz"] = max(int(m["max_dbz"]) for m in cluster)
        primary["top_kft"] = max(int(m["top_kft"]) for m in cluster)

        non_none_tvs = [m["tvs"] for m in cluster if m["tvs"] not in ("NONE", "")]
        non_none_meso = [m["meso"] for m in cluster if m["meso"] not in ("NONE", "")]
        primary["tvs"] = non_none_tvs[0] if non_none_tvs else "NONE"
        primary["meso"] = non_none_meso[0] if non_none_meso else "NONE"

        primary.pop("raw_eta_15mi_min", None)
        primary.pop("raw_eta_10mi_min", None)
        primary.pop("t_cpa_min", None)

        deduped.append(primary)

    def _sort_key(c: Dict[str, Any]):
        has_tvs = 0 if c["tvs"] != "NONE" else 1
        eta15 = c["eta_15mi_min"] if c["eta_15mi_min"] is not None else 999
        cpa = c["cpa_mi"] if c["cpa_mi"] is not None else 999.0
        return (has_tvs, eta15, cpa, c["dist_mi"])

    deduped.sort(key=_sort_key)
    return deduped[:10]
