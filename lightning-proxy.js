/**
 * stillWeather — Cloudflare Worker: GOES-19 GLM + IEM NEXRAD Status Edge Reader & Diagnostics
 * File: lightning-proxy.js  (deploy to Cloudflare Workers via `npx wrangler deploy`)
 * ──────────────────────────────────────────────────────────────────────────────
 *
 * ROUTES
 *   • GET /status (or /status.json)
 *       Reads `status.json` from the bound Cloudflare R2 bucket (`env.STATUS_BUCKET`),
 *       caches at the Cloudflare edge for 30s, and serves with `Access-Control-Allow-Origin: *`.
 *       Falls back to `https://data.ronlab.site/status.json` if the R2 binding is absent.
 *   • GET /
 *       Full engineering & homelab diagnostics dashboard showing NetCDF scan age,
 *       files ingested (10m), IEM scan age, payload byte size, R2 cache hit/miss,
 *       per-site ring matrices, tracked NEXRAD storm cell tables, and IC+CG parallax notes.
 *   • GET /lightning?lat=...&lon=...
 *       Backward-compatible endpoint that synthesizes the legacy `{ count, lightning: [...] }`
 *       format directly from `status.json` (`recent_flashes`).
 */

const STATUS_CACHE_TTL_SECONDS = 30;
const LEGACY_CACHE_TTL_SECONDS = 120;
const R2_PUBLIC_FALLBACK_URL   = 'https://data.ronlab.site/status.json';

const SITES_METADATA = {
  stillwater: { name: 'Stillwater YMCA',            lat: 36.1152, lon: -97.0624 },
  mitch:      { name: 'Mitch Park YMCA',            lat: 35.6847, lon: -97.5083 },
  bethany:    { name: 'Bethany YMCA & Ripper Park', lat: 35.5051, lon: -97.6316 },
};

const CARDINAL_16 = [
  'N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE',
  'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW',
];

function haversineMi(lat1, lon1, lat2, lon2) {
  const R = 3958.7613;
  const toRad = d => (d * Math.PI) / 180;
  const dLat = toRad(lat2 - lat1);
  const dLon = toRad(lon2 - lon1);
  const a =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.min(1, Math.sqrt(a)));
}

function bearingCardinal(lat1, lon1, lat2, lon2) {
  const toRad = d => (d * Math.PI) / 180;
  const toDeg = r => (r * 180) / Math.PI;
  const dLon = toRad(lon2 - lon1);
  const y = Math.sin(dLon) * Math.cos(toRad(lat2));
  const x =
    Math.cos(toRad(lat1)) * Math.sin(toRad(lat2)) -
    Math.sin(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.cos(dLon);
  const brg = (toDeg(Math.atan2(y, x)) + 360) % 360;
  return CARDINAL_16[Math.round(brg / 22.5) % 16];
}

async function loadStatusJsonText(env, ctx) {
  const t0 = Date.now();
  const cache = caches.default;
  const cacheKey = new Request('https://cache.stillweather/r2/status.json');

  const cached = await cache.match(cacheKey);
  if (cached) {
    const text = await cached.text();
    return {
      text,
      cacheHit: 'HIT',
      source: cached.headers.get('X-Source') || 'R2-Edge-Cache',
      latencyMs: Date.now() - t0,
    };
  }

  let text = null;
  let source = 'R2-Binding';

  if (env.STATUS_BUCKET && typeof env.STATUS_BUCKET.get === 'function') {
    const obj = await env.STATUS_BUCKET.get('status.json');
    if (obj) {
      text = await obj.text();
    }
  }

  if (!text) {
    const fallbackUrl = env.R2_PUBLIC_STATUS_URL || R2_PUBLIC_FALLBACK_URL;
    const res = await fetch(fallbackUrl, { cf: { cacheTtl: STATUS_CACHE_TTL_SECONDS } });
    if (res.ok) {
      text = await res.text();
      source = 'R2-Public-Domain';
    }
  }

  if (!text) {
    return null;
  }

  const toCache = new Response(text, {
    status: 200,
    headers: {
      'Content-Type': 'application/json',
      'Cache-Control': `public, max-age=${STATUS_CACHE_TTL_SECONDS}`,
      'X-Source': source,
    },
  });
  ctx.waitUntil(cache.put(cacheKey, toCache));

  return {
    text,
    cacheHit: 'MISS',
    source,
    latencyMs: Date.now() - t0,
  };
}

function renderStatusHtml(statusObj, diagMeta) {
  const gen = statusObj?.generated_at || 'Unknown';
  const genMs = statusObj?.generated_at ? Date.parse(statusObj.generated_at) : NaN;
  const payloadAgeS = Number.isNaN(genMs) ? '—' : Math.max(0, Math.round((Date.now() - genMs) / 1000));
  const health = statusObj?.health || {};
  const sites = statusObj?.sites || {};
  const flashesCount = (statusObj?.recent_flashes || []).length;
  const byteSize = diagMeta?.byteSize ?? 0;
  const source = diagMeta?.source ?? 'Unknown';
  const cacheHit = diagMeta?.cacheHit ?? '—';
  const latencyMs = diagMeta?.latencyMs ?? '—';

  const siteRows = Object.entries(SITES_METADATA)
    .map(([id, meta]) => {
      const s = sites[id] || {};
      const status = (s.clearance_status || 'unknown').toUpperCase();
      const rem = s.clearance_remaining_min ?? 0;
      const rc = s.ring_counts || {};
      const r0_10 = rc['0_10'] || { m5: 0, m15: 0, m30: 0 };
      const r10_15 = rc['10_15'] || { m5: 0, m15: 0, m30: 0 };
      const r15_20 = rc['15_20'] || { m5: 0, m15: 0, m30: 0 };
      const r20_30 = rc['20_30'] || { m5: 0, m15: 0, m30: 0 };
      const nf = s.nearest_flash
        ? `${s.nearest_flash.dist_mi} mi ${s.nearest_flash.bearing} (${s.nearest_flash.age_min}m ago, ${Math.round(s.nearest_flash.energy_fj)} fJ)`
        : 'None <30 mi';
      return `<tr>
        <td><strong>${meta.name}</strong><br><small>${meta.lat}, ${meta.lon}</small></td>
        <td><code>${status}</code></td>
        <td>${rem > 0 ? `<strong>${rem.toFixed(1)} min</strong>` : '0.0 min'}</td>
        <td>${r0_10.m5}/${r0_10.m15}/${r0_10.m30}</td>
        <td>${r10_15.m5}/${r10_15.m15}/${r10_15.m30}</td>
        <td>${r15_20.m5}/${r15_20.m15}/${r15_20.m30}</td>
        <td>${r20_30.m5}/${r20_30.m15}/${r20_30.m30}</td>
        <td>${nf}</td>
        <td><code>${s.trend || 'none'}</code></td>
      </tr>`;
    })
    .join('');

  const cellSections = Object.entries(SITES_METADATA)
    .map(([id, meta]) => {
      const cells = sites[id]?.cells || [];
      if (cells.length === 0) {
        return `<p style="font-size:0.85rem;color:#71717a;margin:6px 0;"><strong>${meta.name}:</strong> No active NEXRAD cells within 60 mi.</p>`;
      }
      const rows = cells
        .slice(0, 5)
        .map(c => {
          const radars = (c.observed_by_radars || [c.nexrad]).join(', ');
          const motion = c.has_motion
            ? `${c.heading_cardinal} (${c.heading_to_deg}&deg;) @ ${c.speed_mph} mph`
            : 'Stationary';
          return `<tr>
            <td><code>${c.id}</code></td>
            <td>${radars} <small>(${c.radar_range_mi} mi)</small></td>
            <td>${c.dist_mi} mi ${c.bearing}</td>
            <td>${motion}</td>
            <td>${c.cpa_mi !== null ? `${c.cpa_mi} mi` : '—'}</td>
            <td>${c.eta_15mi_label || '—'}</td>
            <td>${c.eta_10mi_label || '—'}</td>
            <td>${c.max_dbz} dBZ / ${c.top_kft} kft</td>
            <td><code>${c.tvs}/${c.meso}</code></td>
          </tr>`;
        })
        .join('');
      return `<h4 style="margin:14px 0 6px 0;font-size:0.9rem;">${meta.name} — Top Tracked NEXRAD Cells</h4>
      <table>
        <thead><tr><th>Cell</th><th>Radars (Range)</th><th>Site Dist</th><th>Motion</th><th>CPA</th><th>ETA 15mi</th><th>ETA 10mi</th><th>dBZ / Top</th><th>TVS/MESO</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
    })
    .join('');

  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>stillWeather Homelab &amp; Edge Diagnostics</title>
<style>
  body { font-family: system-ui, -apple-system, sans-serif; max-width: 1020px; margin: 28px auto; padding: 0 16px; color: #18181b; background: #f4f4f5; line-height: 1.45; }
  .card { background: #fff; border: 1px solid #e4e4e7; padding: 20px; margin-bottom: 18px; }
  .kpi-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(145px, 1fr)); gap: 10px; margin: 14px 0; }
  .kpi { border: 1px solid #e4e4e7; background: #fafafa; padding: 10px 12px; }
  .kpi-val { font-size: 1.15rem; font-weight: 700; }
  .kpi-lbl { font-size: 0.74rem; color: #71717a; }
  table { width: 100%; border-collapse: collapse; font-size: 0.82rem; }
  th, td { text-align: left; padding: 8px 6px; border-bottom: 1px solid #e4e4e7; }
  th { color: #52525b; font-weight: 600; background: #fafafa; }
  code { background: #f4f4f5; padding: 2px 5px; border-radius: 3px; font-size: 0.8rem; }
</style>
</head>
<body>
  <div class="card">
    <h2 style="margin:0 0 6px 0;">stillWeather — GOES-19 GLM &amp; IEM NEXRAD Diagnostics</h2>
    <div style="font-size:0.84rem;color:#52525b;">
      Generated UTC: <code>${gen}</code> &bull; Raw JSON: <a href="/status">/status</a> &bull; Dashboard: <a href="https://ecstatictech.github.io/stillWeather/">GitHub Pages UI</a>
    </div>
    <div class="kpi-grid">
      <div class="kpi"><div class="kpi-val">${health.feed_ok ? 'HEALTHY' : 'DEGRADED'}</div><div class="kpi-lbl">Pipeline Health</div></div>
      <div class="kpi"><div class="kpi-val">${payloadAgeS}s</div><div class="kpi-lbl">Payload Age (&le;180s)</div></div>
      <div class="kpi"><div class="kpi-val">${health.newest_glm_age_s ?? '—'}s</div><div class="kpi-lbl">GOES-19 GLM Scan Age</div></div>
      <div class="kpi"><div class="kpi-val">${health.glm_files_last_10min ?? 0}</div><div class="kpi-lbl">GLM Files (Last 10m)</div></div>
      <div class="kpi"><div class="kpi-val">${health.iem_ok ? `${health.iem_age_s ?? 0}s` : 'ERR'}</div><div class="kpi-lbl">IEM NEXRAD Age</div></div>
      <div class="kpi"><div class="kpi-val">${flashesCount}</div><div class="kpi-lbl">OK Bbox Flashes (30m)</div></div>
      <div class="kpi"><div class="kpi-val">${(byteSize / 1024).toFixed(1)} KB</div><div class="kpi-lbl">status.json Size</div></div>
      <div class="kpi"><div class="kpi-val">${source} (${cacheHit})</div><div class="kpi-lbl">Edge Read (${latencyMs} ms)</div></div>
    </div>

    <h3 style="margin:18px 0 8px 0;font-size:1rem;">YMCA Site Clearance &amp; Ring Matrix (5m / 15m / 30m counts)</h3>
    <table>
      <thead>
        <tr>
          <th>Facility</th><th>State</th><th>30m Hold Rem</th>
          <th>0–10mi (Policy)</th><th>10–15mi (Parallax)</th><th>15–20mi</th><th>20–30mi</th>
          <th>Nearest Flash (&le;30mi)</th><th>Trend</th>
        </tr>
      </thead>
      <tbody>${siteRows}</tbody>
    </table>
  </div>

  <div class="card">
    <h3 style="margin:0 0 8px 0;font-size:1rem;">IEM NEXRAD Storm Cells (Cross-Radar Deduplicated)</h3>
    <div style="font-size:0.78rem;color:#71717a;margin-bottom:8px;">
      Cells within 5.5 mi across overlapping radars (TLX, OKC, VNX, INX, TUL, FDR, SRX, ICT) are grouped; display attributes use the lowest-range radar while CPA/ETA take the minimum across all observing radars. ETAs reflect radar reflectivity core (anvil lightning can precede core by 5–10 min).
    </div>
    ${cellSections}
  </div>

  <div class="card" style="font-size:0.83rem;color:#52525b;">
    <h3 style="margin:0 0 6px 0;font-size:0.95rem;color:#18181b;">Pipeline Reference Notes (Total Lightning &amp; Parallax Buffer)</h3>
    <ul style="margin:0;padding-left:18px;">
      <li><strong>GOES-19 GLM Total Lightning (IC + CG):</strong> GLM measures optical cloud-top illumination at 777.4 nm (8–14 km pixel footprint), capturing both in-cloud (IC) and cloud-to-ground (CG) flashes. The 30-minute hold timer resets on all quality-controlled (<code>qflag == 0</code>) flashes within 15 miles.</li>
      <li><strong>10 mi Policy Ring vs. 15 mi Parallax Buffer:</strong> High-altitude storm anvils at 10–15 km over Oklahoma (~35–36&deg;N) experience ~3–5 miles of NNW satellite parallax displacement. Tracking the 10–15 mi buffer prevents premature pool/practice clearance while active intra-cloud lightning sits on the 10-mile boundary.</li>
      <li><strong>Attribution:</strong> Radar mosaic &amp; NEXRAD storm attributes &copy; Iowa Environmental Mesonet (IEM) &middot; NOAA/NWS.</li>
    </ul>
  </div>
</body>
</html>`;
}

export default {
  async fetch(request, env, ctx) {
    const corsHeaders = {
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Methods': 'GET, OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type',
    };

    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: corsHeaders });
    }

    const url = new URL(request.url);
    const path = url.pathname;

    // ── 1. Primary GOES-19 + IEM status endpoint (/status, /status.json) ────
    if (path === '/status' || path === '/status.json') {
      try {
        const result = await loadStatusJsonText(env, ctx);
        if (!result) {
          return new Response(
            JSON.stringify({ error: 'status.json not available in R2 bucket' }),
            {
              status: 503,
              headers: { 'Content-Type': 'application/json', ...corsHeaders },
            }
          );
        }
        return new Response(result.text, {
          status: 200,
          headers: {
            'Content-Type': 'application/json',
            'Cache-Control': `public, max-age=${STATUS_CACHE_TTL_SECONDS}`,
            'X-Cache': result.cacheHit,
            'X-Source': result.source,
            ...corsHeaders,
          },
        });
      } catch (err) {
        return new Response(
          JSON.stringify({ error: 'Failed to read status.json', detail: err.message }),
          {
            status: 502,
            headers: { 'Content-Type': 'application/json', ...corsHeaders },
          }
        );
      }
    }

    // ── 2. Worker Root Engineering Diagnostics Page (/) ─────────────────────
    if (path === '/' || path === '/index.html') {
      try {
        const result = await loadStatusJsonText(env, ctx);
        const statusObj = result ? JSON.parse(result.text) : null;
        const diagMeta = result
          ? {
              byteSize: result.text.length,
              source: result.source,
              cacheHit: result.cacheHit,
              latencyMs: result.latencyMs,
            }
          : {};
        return new Response(renderStatusHtml(statusObj, diagMeta), {
          status: 200,
          headers: { 'Content-Type': 'text/html; charset=utf-8' },
        });
      } catch (err) {
        return new Response(`stillWeather Worker online (status load error: ${err.message})`, {
          status: 200,
          headers: { 'Content-Type': 'text/plain; charset=utf-8' },
        });
      }
    }

    // ── 3. Legacy /lightning endpoint (synthesized from R2 status.json) ─────
    if (path !== '/lightning') {
      return new Response(JSON.stringify({ error: 'Not found' }), {
        status: 404,
        headers: { 'Content-Type': 'application/json', ...corsHeaders },
      });
    }

    const lat = parseFloat(url.searchParams.get('lat'));
    const lon = parseFloat(url.searchParams.get('lon'));
    if (Number.isNaN(lat) || Number.isNaN(lon)) {
      return new Response(JSON.stringify({ error: 'lat and lon are required' }), {
        status: 400,
        headers: { 'Content-Type': 'application/json', ...corsHeaders },
      });
    }

    const searchKm = parseFloat(url.searchParams.get('search_distance_km') || '40');
    const searchMi = searchKm / 1.60934;
    const searchMins = parseFloat(url.searchParams.get('search_mins') || '30');
    const limit = parseInt(url.searchParams.get('limit') || '20', 10);

    try {
      const result = await loadStatusJsonText(env, ctx);
      if (result) {
        const statusObj = JSON.parse(result.text);
        const flashes = statusObj.recent_flashes || [];
        const matched = [];
        for (const f of flashes) {
          const pastMins = (f.age_s || 0) / 60.0;
          if (pastMins > searchMins) continue;
          const distMi = haversineMi(lat, lon, f.lat, f.lon);
          if (distMi <= searchMi) {
            matched.push({
              lat: f.lat,
              lon: f.lon,
              distance_mi: Number(distMi.toFixed(2)),
              past_mins: Number(pastMins.toFixed(1)),
              bearing_cardinal: bearingCardinal(lat, lon, f.lat, f.lon),
              energy_fj: f.energy_fj,
            });
          }
        }
        matched.sort((a, b) => a.distance_mi - b.distance_mi);
        const payload = {
          count: matched.length,
          source: 'goes19-glm-r2',
          generated_at: statusObj.generated_at,
          lightning: matched.slice(0, limit),
        };
        return new Response(JSON.stringify(payload), {
          status: 200,
          headers: {
            'Content-Type': 'application/json',
            'Cache-Control': `public, max-age=${STATUS_CACHE_TTL_SECONDS}`,
            'X-Cache': result.cacheHit,
            'X-Source': result.source,
            ...corsHeaders,
          },
        });
      }
    } catch (_e) {
      // Fall through to legacy Weatherbit only if R2 synthesis fails
    }

    if (!env.WEATHERBIT_API_KEY) {
      return new Response(
        JSON.stringify({ error: 'R2 status.json unavailable and no legacy WEATHERBIT_API_KEY set' }),
        { status: 503, headers: { 'Content-Type': 'application/json', ...corsHeaders } }
      );
    }

    const sort = url.searchParams.get('sort') || 'distance';
    const upstreamUrl =
      `https://api.weatherbit.io/v2.0/current/lightning` +
      `?lat=${lat}&lon=${lon}` +
      `&search_distance_km=${searchKm}` +
      `&search_mins=${searchMins}` +
      `&sort=${sort}&limit=${limit}` +
      `&key=${env.WEATHERBIT_API_KEY}`;

    try {
      const apiRes = await fetch(upstreamUrl);
      const payload = await apiRes.json();
      return new Response(JSON.stringify(payload), {
        status: apiRes.status,
        headers: {
          'Content-Type': 'application/json',
          'Cache-Control': `public, max-age=${LEGACY_CACHE_TTL_SECONDS}`,
          ...corsHeaders,
        },
      });
    } catch (err) {
      return new Response(
        JSON.stringify({ error: 'Upstream API error', detail: err.message }),
        { status: 502, headers: { 'Content-Type': 'application/json', ...corsHeaders } }
      );
    }
  },
};
