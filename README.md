# Stillwater Swim Club — Practice Weather Decision Tool

A standalone weather decision dashboard for **Stillwater YMCA**, **Mitch Park YMCA**, and **Bethany YMCA & Ripper Park** combining live **National Weather Service (NWS)** hourly forecasts/alerts with a zero-cost **NOAA GOES-19 GLM** lightning and **Iowa Environmental Mesonet (IEM) NEXRAD** storm-cell tracking pipeline.

- **Live Dashboard:** https://ecstatictech.github.io/stillWeather/
- **Edge Telemetry & Diagnostics:** https://stillweather-lightning.ronlab.workers.dev/

---

## Architecture Overview

1. **Homelab Python Ingest & Replay Suite (`ingest/`)**:
   - Polls public **NOAA GOES-19 Geostationary Lightning Mapper (GLM)** NetCDF files (`s3://noaa-goes19/GLM-L2-LCFA`, ~20s cadence) and **IEM NEXRAD** storm cell attributes (`nexrad_attr.py`).
   - Maintains a bounded 45-minute rolling SQLite WAL database (`stillweather_cache.db`).
   - Computes per-site 30-minute clearance countdowns, ring counts (`0–10`, `10–15`, `15–20`, `20–30` mi), flash trends, and cross-radar deduplicated Closest Point of Approach (CPA) / 5-minute bucketed ETAs.
   - Uploads a compact `status.json` to Cloudflare R2 every 45–60 seconds.
2. **Cloudflare Edge Distribution (`lightning-proxy.js` & R2)**:
   - Primary endpoint: `https://stillweather-lightning.ronlab.workers.dev/status` (reads from the bound `STATUS_BUCKET` R2 bucket with a 30s edge cache).
   - Automatic client fallback: `https://data.ronlab.site/status.json` (direct R2 custom domain) if the Worker times out (~3s).
   - Engineering status page at `GET /` displays full pipeline diagnostics, ring matrices, and NEXRAD cell tables.
3. **Coach Dashboard (`index.html`)**:
   - **30-Minute YMCA Clearance Gauge**: Circular countdown timer tracking active holds and recovery windows.
   - **Two-Tier Stale Handling**: Shows a calm `Standby · NWS Clear` badge when the sensor feed is idle during clear skies (`Threat Score < 40` and no alerts), and only surfaces an amber `Sensor Updating — NWS Forecast Active` warning when storms are in the forecast.
   - **Dual Map Toggle (`Windy | GLM + NEXRAD`)**: Defaults to the familiar Windy embed while providing a one-click toggle to an interactive Leaflet radar & lightning map backed by **CARTO Dark Matter**, **CARTO Voyager**, and **OpenStreetMap** basemaps, animated IEM N0Q reflectivity loops (`-25m` to `LIVE`), 10/15/20 mi facility rings, age-coded GLM flashes, and storm motion vectors.

---

## 10-Mile YMCA Policy Ring vs. 15-Mile GLM Parallax Buffer

GOES-19 GLM detects total lightning — both **in-cloud (IC)** and **cloud-to-ground (CG)** optical flashes — at cloud-top altitude (`10–15 km` with an `8–14 km` pixel footprint). Over Oklahoma (`~35–36°N`), satellite viewing geometry introduces ~3–5 miles of NNW optical parallax displacement.

To prevent premature pool clearance while active intra-cloud lightning sits on the 10-mile boundary:
- **0–10 mi (Policy Ring):** Strict YMCA facility lightning closure zone.
- **10–15 mi (Parallax & Anvil Buffer):** Enforces the 30-minute safety hold envelope to account for GLM cloud-top parallax and anvil lightning preceding the radar core.

---

## Homelab Setup & CLI Usage

### 1. Install Dependencies & Configure `.env`
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r ingest/requirements.txt
Copy-Item ingest\.env.example .env
```
Edit `.env` with your Cloudflare R2 account endpoint (`https://<ACCOUNT_ID>.r2.cloudflarestorage.com`) and R2 API token credentials.

### 2. Live Peek & Single Ingest Cycle
```powershell
# Inspect newest GOES-19 GLM files over Oklahoma
python -m ingest.glm_peek

# Run one full ingest cycle (writes local status.json and uploads to R2)
python -m ingest.daemon --once

# Run continuous daemon (45s GLM poll, 90s IEM NEXRAD poll)
python -m ingest.daemon
```

### 3. Historical Storm Replay & Backtesting
```powershell
python -m ingest.replay_glm --start 2026-11-03T01:00:00Z --hours 4 --site stillwater
```

### 4. Deploy Cloudflare Worker
```powershell
npx wrangler deploy
```

---

## Attribution
- **Forecast & Alerts:** National Weather Service API (`api.weather.gov`)
- **Total Lightning:** NOAA GOES-19 GLM (`noaa-goes19` Open Data)
- **Radar Mosaic & Storm Attributes:** Iowa Environmental Mesonet (IEM) · NEXRAD/NOAA
- **Basemaps:** OpenStreetMap contributors & CARTO
