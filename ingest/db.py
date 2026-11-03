"""SQLite WAL state manager for rolling 45-minute GLM flash and IEM cell cache."""

from __future__ import annotations

import os
import sqlite3
from typing import Any, Dict, Iterable, List

FLASH_RETENTION_SECONDS = 45 * 60          # 45 minutes (2700s)
PROCESSED_FILES_RETENTION_SECONDS = 2 * 3600  # 2 hours (7200s) — covers hourly S3 rollover window


def get_connection(db_path: str = "stillweather_cache.db") -> sqlite3.Connection:
    """Open SQLite connection configured with WAL mode and NORMAL synchronous durability."""
    parent = os.path.dirname(os.path.abspath(db_path))
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)

    conn = sqlite3.connect(db_path, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA temp_store=MEMORY;")
    init_schema(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Create tables and indexes if they do not exist."""
    with conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS processed_files (
                s3_key TEXT PRIMARY KEY,
                processed_at_utc INTEGER NOT NULL,
                flash_count INTEGER NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_processed_files_time
                ON processed_files(processed_at_utc);

            CREATE TABLE IF NOT EXISTS flashes (
                flash_uid TEXT PRIMARY KEY,
                timestamp_utc REAL NOT NULL,
                lat REAL NOT NULL,
                lon REAL NOT NULL,
                energy_fj REAL NOT NULL DEFAULT 0.0
            );

            CREATE INDEX IF NOT EXISTS idx_flashes_timestamp
                ON flashes(timestamp_utc);

            CREATE TABLE IF NOT EXISTS iem_cells (
                nexrad TEXT NOT NULL,
                storm_id TEXT NOT NULL,
                valid_utc INTEGER NOT NULL,
                lat REAL NOT NULL,
                lon REAL NOT NULL,
                drct REAL,
                sknt REAL,
                max_dbz REAL,
                top_kft REAL,
                tvs TEXT,
                meso TEXT,
                PRIMARY KEY (nexrad, storm_id)
            );

            CREATE INDEX IF NOT EXISTS idx_iem_cells_valid
                ON iem_cells(valid_utc);
            """
        )


def get_processed_keys(conn: sqlite3.Connection, candidate_keys: Iterable[str]) -> set[str]:
    """Return subset of `candidate_keys` already recorded in `processed_files`."""
    keys = list(candidate_keys)
    if not keys:
        return set()
    placeholders = ",".join("?" for _ in keys)
    rows = conn.execute(
        f"SELECT s3_key FROM processed_files WHERE s3_key IN ({placeholders})",
        keys,
    ).fetchall()
    return {row["s3_key"] for row in rows}


def record_glm_batch(
    conn: sqlite3.Connection,
    s3_key: str,
    processed_at_utc: int,
    flashes: List[Dict[str, Any]],
) -> int:
    """Insert flashes (`flash_uid = f'{s3_key}:{flash_id}'`) and mark `s3_key` processed."""
    with conn:
        if flashes:
            conn.executemany(
                """
                INSERT OR IGNORE INTO flashes (flash_uid, timestamp_utc, lat, lon, energy_fj)
                VALUES (:flash_uid, :timestamp_utc, :lat, :lon, :energy_fj)
                """,
                flashes,
            )
        conn.execute(
            """
            INSERT OR REPLACE INTO processed_files (s3_key, processed_at_utc, flash_count)
            VALUES (?, ?, ?)
            """,
            (s3_key, int(processed_at_utc), len(flashes)),
        )
    return len(flashes)


def replace_iem_cells(
    conn: sqlite3.Connection,
    cells: List[Dict[str, Any]],
    fetched_at_utc: int,
) -> int:
    """Upsert latest IEM NEXRAD storm cell attributes and remove stale cells from prior scans."""
    with conn:
        # Remove cells older than 20 minutes before upserting fresh scan snapshot
        conn.execute("DELETE FROM iem_cells WHERE valid_utc < ?", (int(fetched_at_utc) - 1200,))
        if cells:
            conn.executemany(
                """
                INSERT OR REPLACE INTO iem_cells (
                    nexrad, storm_id, valid_utc, lat, lon,
                    drct, sknt, max_dbz, top_kft, tvs, meso
                ) VALUES (
                    :nexrad, :storm_id, :valid_utc, :lat, :lon,
                    :drct, :sknt, :max_dbz, :top_kft, :tvs, :meso
                )
                """,
                cells,
            )
    return len(cells)


def prune_old_records(
    conn: sqlite3.Connection,
    now_utc: float,
    flash_retention_s: int = FLASH_RETENTION_SECONDS,
    processed_retention_s: int = PROCESSED_FILES_RETENTION_SECONDS,
) -> Dict[str, int]:
    """
    Prune rolling tables so SQLite file size stays bounded indefinitely:
      - `flashes` older than 45 min (`2700s`)
      - `iem_cells` older than 45 min (`2700s`)
      - `processed_files` older than 2 hours (`7200s`)
    """
    flash_cutoff = float(now_utc) - float(flash_retention_s)
    proc_cutoff = int(now_utc) - int(processed_retention_s)
    with conn:
        cur_f = conn.execute("DELETE FROM flashes WHERE timestamp_utc < ?", (flash_cutoff,))
        cur_c = conn.execute("DELETE FROM iem_cells WHERE valid_utc < ?", (int(flash_cutoff),))
        cur_p = conn.execute(
            "DELETE FROM processed_files WHERE processed_at_utc < ?", (proc_cutoff,)
        )
    return {
        "flashes_pruned": cur_f.rowcount,
        "cells_pruned": cur_c.rowcount,
        "processed_files_pruned": cur_p.rowcount,
    }


def fetch_recent_flashes(
    conn: sqlite3.Connection,
    since_utc: float,
) -> List[Dict[str, Any]]:
    """Return all flashes with `timestamp_utc >= since_utc` ordered newest first."""
    rows = conn.execute(
        """
        SELECT flash_uid, timestamp_utc, lat, lon, energy_fj
        FROM flashes
        WHERE timestamp_utc >= ?
        ORDER BY timestamp_utc DESC
        """,
        (float(since_utc),),
    ).fetchall()
    return [dict(r) for r in rows]


def fetch_active_iem_cells(
    conn: sqlite3.Connection,
    since_utc: float,
) -> List[Dict[str, Any]]:
    """Return active IEM storm cells with `valid_utc >= since_utc`."""
    rows = conn.execute(
        """
        SELECT nexrad, storm_id, valid_utc, lat, lon, drct, sknt, max_dbz, top_kft, tvs, meso
        FROM iem_cells
        WHERE valid_utc >= ?
        ORDER BY max_dbz DESC
        """,
        (int(since_utc),),
    ).fetchall()
    return [dict(r) for r in rows]


def count_processed_files_since(conn: sqlite3.Connection, since_utc: float) -> int:
    """Count number of GLM NetCDF files processed since `since_utc`."""
    row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM processed_files WHERE processed_at_utc >= ?",
        (int(since_utc),),
    ).fetchone()
    return int(row["cnt"]) if row else 0
