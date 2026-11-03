"""Spatial geometry, meteorological motion vectors, and CPA/ETA calculations."""

from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Optional, Sequence, Union

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None  # type: ignore[assignment]

EARTH_RADIUS_MI = 3958.7613
KNOTS_TO_MPH = 1.15077945

CARDINAL_16 = (
    "N", "NNE", "NE", "ENE",
    "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW",
    "W", "WNW", "NW", "NNW",
)

LEAD_TIME_NOTE = (
    "Radar core ETA — anvil lightning may precede the reflectivity core by 5–10 min"
)


def _scalar_haversine_mi(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    lat1_r = math.radians(float(lat1))
    lon1_r = math.radians(float(lon1))
    lat2_r = math.radians(float(lat2))
    lon2_r = math.radians(float(lon2))

    dlat = lat2_r - lat1_r
    dlon = lon2_r - lon1_r

    a = (
        math.sin(dlat / 2.0) ** 2
        + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(dlon / 2.0) ** 2
    )
    c = 2.0 * math.asin(min(1.0, math.sqrt(a)))
    return EARTH_RADIUS_MI * c


def haversine_mi(
    lat1: Union[float, Sequence[float], Any],
    lon1: Union[float, Sequence[float], Any],
    lat2: Union[float, Sequence[float], Any],
    lon2: Union[float, Sequence[float], Any],
) -> Union[float, List[float], Any]:
    """Compute great-circle distance in statute miles (supports scalars, sequences, or NumPy arrays)."""
    if isinstance(lat1, (int, float)) and isinstance(lon1, (int, float)) and isinstance(lat2, (int, float)) and isinstance(lon2, (int, float)):
        return _scalar_haversine_mi(float(lat1), float(lon1), float(lat2), float(lon2))

    if np is not None:
        lat1_r = np.radians(lat1)
        lon1_r = np.radians(lon1)
        lat2_r = np.radians(lat2)
        lon2_r = np.radians(lon2)

        dlat = lat2_r - lat1_r
        dlon = lon2_r - lon1_r

        a = (
            np.sin(dlat / 2.0) ** 2
            + np.cos(lat1_r) * np.cos(lat2_r) * np.sin(dlon / 2.0) ** 2
        )
        c = 2.0 * np.arcsin(np.minimum(1.0, np.sqrt(a)))
        dist = EARTH_RADIUS_MI * c
        if np.ndim(dist) == 0:
            return float(dist)
        return dist

    # Pure-Python sequence fallback when NumPy is not installed
    if isinstance(lat1, (int, float)) and isinstance(lon1, (int, float)):
        return [
            _scalar_haversine_mi(float(lat1), float(lon1), float(la2), float(lo2))
            for la2, lo2 in zip(lat2, lon2)
        ]
    return [
        _scalar_haversine_mi(float(la1), float(lo1), float(la2), float(lo2))
        for la1, lo1, la2, lo2 in zip(lat1, lon1, lat2, lon2)
    ]


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Compute initial compass bearing in degrees [0, 360) from (lat1, lon1) to (lat2, lon2)."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dlon = math.radians(lon2 - lon1)

    y = math.sin(dlon) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlon)
    theta = math.degrees(math.atan2(y, x))
    return (theta + 360.0) % 360.0


def deg_to_cardinal(deg: float) -> str:
    """Convert compass degrees [0, 360) to 16-point cardinal direction."""
    idx = int(round((deg % 360.0) / 22.5)) % 16
    return CARDINAL_16[idx]


def knots_to_mph(sknt: float) -> float:
    """Convert speed in knots to statute miles per hour."""
    return float(sknt) * KNOTS_TO_MPH


def drct_to_heading_deg(drct_from_deg: float) -> float:
    """
    Convert IEM NEXRAD meteorological `drct` (direction the cell is moving FROM)
    into the forward motion heading (direction the cell is moving TOWARD).

    Empirically verified against Oklahoma NEXRAD tracks:
    heading_to_deg = (drct_from_deg + 180) % 360.
    """
    return (float(drct_from_deg) + 180.0) % 360.0


def bucket_eta_5min(raw_eta_min: Optional[float]) -> Optional[int]:
    """
    Round raw ETA in minutes to nearest 5-minute bucket (minimum 0 if already inside,
    otherwise minimum 5 min) to avoid false tenth-of-a-minute precision.
    """
    if raw_eta_min is None:
        return None
    if raw_eta_min <= 0.0:
        return 0
    return max(5, int(round(raw_eta_min / 5.0) * 5))


def format_eta_range_label(raw_eta_min: Optional[float]) -> Optional[str]:
    """
    Format an ETA range string accounting for the empirical 0.6–2x cell displacement
    speed variance (using ~0.65x to ~1.35x travel-time bounds rounded to 5-min increments).
    """
    if raw_eta_min is None:
        return None
    if raw_eta_min <= 0.0:
        return "Inside ring now"
    low = max(5, int(math.floor((raw_eta_min * 0.65) / 5.0) * 5))
    high = max(low + 5, int(math.ceil((raw_eta_min * 1.35) / 5.0) * 5))
    return f"~{low}–{high} min"


def compute_cpa_and_eta(
    site_lat: float,
    site_lon: float,
    cell_lat: float,
    cell_lon: float,
    drct_from_deg: Optional[float],
    sknt: Optional[float],
    ring_radii_mi: tuple[float, float] = (15.0, 10.0),
) -> Dict[str, Any]:
    """
    Compute Closest Point of Approach (CPA) and ring entry ETAs on a local tangent plane
    centered at the site `(0, 0)` (+x East, +y North, in miles).
    """
    dist = float(haversine_mi(site_lat, site_lon, cell_lat, cell_lon))
    brg_deg = bearing_deg(site_lat, site_lon, cell_lat, cell_lon)
    brg_card = deg_to_cardinal(brg_deg)

    has_motion = (
        drct_from_deg is not None
        and sknt is not None
        and not math.isnan(float(drct_from_deg))
        and not math.isnan(float(sknt))
        and float(sknt) > 0.0
    )

    result: Dict[str, Any] = {
        "dist_mi": round(dist, 1),
        "bearing_deg": int(round(brg_deg)) % 360,
        "bearing": brg_card,
        "has_motion": has_motion,
        "drct_from_deg": int(round(float(drct_from_deg))) % 360 if has_motion else None,
        "heading_to_deg": None,
        "heading_cardinal": None,
        "speed_mph": 0.0,
        "cpa_mi": None,
        "t_cpa_min": None,
        "raw_eta_15mi_min": None,
        "eta_15mi_min": None,
        "eta_15mi_label": None,
        "raw_eta_10mi_min": None,
        "eta_10mi_min": None,
        "eta_10mi_label": None,
        "lead_time_note": LEAD_TIME_NOTE,
    }

    if not has_motion:
        return result

    heading_deg = drct_to_heading_deg(float(drct_from_deg))
    speed_mph = knots_to_mph(float(sknt))
    speed_mi_per_min = speed_mph / 60.0

    result["heading_to_deg"] = int(round(heading_deg)) % 360
    result["heading_cardinal"] = deg_to_cardinal(heading_deg)
    result["speed_mph"] = round(speed_mph, 1)

    brg_rad = math.radians(brg_deg)
    px = dist * math.sin(brg_rad)
    py = dist * math.cos(brg_rad)

    hdg_rad = math.radians(heading_deg)
    vx = speed_mi_per_min * math.sin(hdg_rad)
    vy = speed_mi_per_min * math.cos(hdg_rad)
    v2 = vx * vx + vy * vy

    if v2 <= 1e-12:
        return result

    p_dot_v = px * vx + py * vy
    if p_dot_v >= 0.0:
        result["cpa_mi"] = round(dist, 1)
        result["t_cpa_min"] = 0.0
        r15, r10 = ring_radii_mi
        if dist <= r15:
            result["raw_eta_15mi_min"] = 0.0
            result["eta_15mi_min"] = 0
            result["eta_15mi_label"] = "Inside ring now"
        if dist <= r10:
            result["raw_eta_10mi_min"] = 0.0
            result["eta_10mi_min"] = 0
            result["eta_10mi_label"] = "Inside ring now"
        return result

    t_cpa = -p_dot_v / v2
    cpa_x = px + vx * t_cpa
    cpa_y = py + vy * t_cpa
    cpa_dist = math.hypot(cpa_x, cpa_y)

    result["cpa_mi"] = round(cpa_dist, 1)
    result["t_cpa_min"] = round(t_cpa, 1)

    def _ring_entry_eta(radius_mi: float) -> Optional[float]:
        if dist <= radius_mi:
            return 0.0
        if cpa_dist > radius_mi:
            return None
        half_chord_mi = math.sqrt(max(0.0, radius_mi * radius_mi - cpa_dist * cpa_dist))
        dt = half_chord_mi / math.sqrt(v2)
        return max(0.0, t_cpa - dt)

    r15, r10 = ring_radii_mi
    raw_eta_15 = _ring_entry_eta(r15)
    raw_eta_10 = _ring_entry_eta(r10)

    if raw_eta_15 is not None:
        result["raw_eta_15mi_min"] = round(raw_eta_15, 1)
        result["eta_15mi_min"] = bucket_eta_5min(raw_eta_15)
        result["eta_15mi_label"] = format_eta_range_label(raw_eta_15)

    if raw_eta_10 is not None:
        result["raw_eta_10mi_min"] = round(raw_eta_10, 1)
        result["eta_10mi_min"] = bucket_eta_5min(raw_eta_10)
        result["eta_10mi_label"] = format_eta_range_label(raw_eta_10)

    return result
