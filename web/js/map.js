/**
 * Ground stations, satellites, active beams and weather.
 *
 * Drawn as plain SVG on an equirectangular (plate carrée) projection, which is
 * what the MapLibre version effectively rendered: it never loaded a tile server
 * either — the basemap was a vector graticule built from the theme tokens, to
 * keep the map self-contained (no API key, no network dependency, works offline)
 * and monochrome, which a photographic satellite basemap would destroy. With no
 * tiles to fetch there was nothing left for a 800 kB map engine to do, so the
 * projection, the pan/zoom and the markers are all local now.
 *
 * A further simplification falls out of that: MapLibre parses paint colours
 * itself and never resolves `var(...)`, which is why the old component had to
 * resolve every CSS custom property through a `useThemeColors` hook and re-read
 * them on theme change. SVG resolves the variables natively, so the theme swap
 * is once again pure CSS and that hook is gone.
 *
 * Everything drawn here comes from one telemetry record: station lat/lon and
 * state from `stations`, sub-satellite points from `satellites`, and one line
 * per *active* link from `links`.
 */

import { html } from "htm/preact";
import { useEffect, useMemo, useRef, useState } from "preact/hooks";
import { useMeasure } from "./state.js";
import { bps, cn } from "./format.js";

/** MapLibre zoom 2.6 over a 512px tile world, in px per degree of longitude. */
const K0 = 512 * Math.pow(2, 2.6) / 360;
const K_MIN = 0.4;
const K_MAX = 400;

const GRAT_STEP = 15;

/** Duration in the units an operator reads a pass in. */
function dur(s) {
  if (s < 0) return "—";
  if (s < 90) return `${Math.round(s)}s`;
  if (s < 5400) return `${Math.floor(s / 60)}m ${String(Math.round(s % 60)).padStart(2, "0")}s`;
  return `${Math.floor(s / 3600)}h ${String(Math.round((s % 3600) / 60)).padStart(2, "0")}m`;
}

/**
 * The analytical contact forecast for one satellite.
 *
 * Closed-form orbital mechanics from `xnios/forecast.py` — exact, not predicted.
 * LEO ground tracks precess ~24 deg west per orbit, so a satellite that misses
 * its pass can wait many hours: this is the number that says so.
 */
function contactLine(s) {
  if (s.time_to_los_s >= 0) return `LOS in ${dur(s.time_to_los_s)}`;
  if (s.next_contact_s < 0) return "no contact within 24h";
  return `next contact ${dur(s.next_contact_s)} → ${s.next_contact_station} · ${dur(s.contact_window_s)} window`;
}

export function NetworkMap({ frame, focus }) {
  const [ref, size] = useMeasure();
  const [view, setView] = useState({ cx: 79, cy: 21, k: K0 });
  const [dragging, setDragging] = useState(false);
  const drag = useRef(null);
  const fitted = useRef(false);

  const [showAll, setShowAll] = useState(false);

  const stations = (frame && frame.record.stations) || [];
  const sats = (frame && frame.record.satellites) || [];

  // `record.links` is one row per *visible* pair, not per served pair. Drawing
  // both tells the operator what the scheduler chose AND what it passed over:
  // a faint line is a station that could have carried this satellite.
  const links = (frame && frame.record.links) || [];
  const served = useMemo(() => links.filter((l) => l.active), [links]);
  const offered = useMemo(() => links.filter((l) => !l.active), [links]);

  /** A satellite is on the map only while some station can see it. Once the pass
   *  ends it leaves, which is the whole point — an empty map is the twin telling
   *  you the constellation is out of range, not that something failed. */
  const shown = useMemo(
    () =>
      showAll
        ? sats
        : sats.filter(
            (s) => s.n_visible > 0 || s.state === "transmitting" || s.state === "slewing",
          ),
    [sats, showAll],
  );
  const stationById = useMemo(
    () => Object.fromEntries(stations.map((s) => [s.station_id, s])),
    [stations],
  );
  const healthById = useMemo(
    () =>
      Object.fromEntries(
        ((frame && frame.health.stations) || []).map((h) => [h.station_id, h]),
      ),
    [frame],
  );

  const W = size.width;
  const H = size.height;

  // fit to the ground segment once, when the first frame lands
  useEffect(() => {
    if (fitted.current || !W || !H || stations.length === 0) return;
    const lats = stations.map((s) => s.lat_deg);
    const lons = stations.map((s) => s.lon_deg);
    const lon0 = Math.min(...lons) - 18;
    const lon1 = Math.max(...lons) + 18;
    const lat0 = Math.min(...lats) - 14;
    const lat1 = Math.max(...lats) + 14;
    const pad = 60;
    const k = Math.min(
      (W - 2 * pad) / Math.max(lon1 - lon0, 1e-6),
      (H - 2 * pad) / Math.max(lat1 - lat0, 1e-6),
    );
    fitted.current = true;
    setView({
      cx: (lon0 + lon1) / 2,
      cy: (lat0 + lat1) / 2,
      k: Math.max(K_MIN, Math.min(K_MAX, k)),
    });
  }, [W, H, stations.length]);

  const px = (lon) => W / 2 + (lon - view.cx) * view.k;
  const py = (lat) => H / 2 - (lat - view.cy) * view.k;

  /* ------------------------------------------------------------ interaction */

  const zoomAbout = (factor, ax, ay) => {
    setView((v) => {
      const k = Math.max(K_MIN, Math.min(K_MAX, v.k * factor));
      if (k === v.k) return v;
      // keep the geographic point under (ax, ay) pinned
      const lon = v.cx + (ax - W / 2) / v.k;
      const lat = v.cy - (ay - H / 2) / v.k;
      return {
        k,
        cx: lon - (ax - W / 2) / k,
        cy: Math.max(-85, Math.min(85, lat + (ay - H / 2) / k)),
      };
    });
  };

  const onWheel = (e) => {
    e.preventDefault();
    const box = e.currentTarget.getBoundingClientRect();
    zoomAbout(Math.exp(-e.deltaY * 0.0015), e.clientX - box.left, e.clientY - box.top);
  };

  const onPointerDown = (e) => {
    if (e.button !== 0) return;
    // The zoom and filter controls live inside the map, so their pointerdown
    // bubbles to here. Capturing the pointer would retarget the compatibility
    // mouse events — including `click` — onto the map itself, and the buttons
    // would silently stop working. Leave control presses alone.
    if (e.target.closest && e.target.closest(".map-zoom, .map-filter")) return;
    e.currentTarget.setPointerCapture(e.pointerId);
    drag.current = { x: e.clientX, y: e.clientY };
    setDragging(true);
  };

  const onPointerMove = (e) => {
    if (!drag.current) return;
    const dx = e.clientX - drag.current.x;
    const dy = e.clientY - drag.current.y;
    drag.current = { x: e.clientX, y: e.clientY };
    setView((v) => ({
      k: v.k,
      cx: v.cx - dx / v.k,
      cy: Math.max(-85, Math.min(85, v.cy + dy / v.k)),
    }));
  };

  const endDrag = (e) => {
    if (!drag.current) return;
    drag.current = null;
    setDragging(false);
    if (e.currentTarget.hasPointerCapture(e.pointerId)) {
      e.currentTarget.releasePointerCapture(e.pointerId);
    }
  };

  /* ----------------------------------------------------------------- layers */

  // a 15° graticule — the only "basemap" the design wants. Both families are
  // straight lines under this projection, so they need no densifying.
  const graticule = [];
  if (W && H) {
    for (let lon = -180; lon <= 180; lon += GRAT_STEP) {
      const x = px(lon);
      if (x < -40 || x > W + 40) continue;
      graticule.push(
        html`<line
          key=${`m${lon}`}
          class=${cn("grat", lon % 90 === 0 ? "grat-major" : "grat-minor")}
          x1=${x}
          x2=${x}
          y1=${py(90)}
          y2=${py(-90)}
        />`,
      );
    }
    for (let lat = -75; lat <= 75; lat += GRAT_STEP) {
      const y = py(lat);
      if (y < -40 || y > H + 40) continue;
      graticule.push(
        html`<line
          key=${`p${lat}`}
          class=${cn("grat", lat === 0 ? "grat-major" : "grat-minor")}
          x1=${px(-180)}
          x2=${px(180)}
          y1=${y}
          y2=${y}
        />`,
      );
    }
  }

  const bySat = Object.fromEntries(sats.map((s) => [s.sat_id, s]));
  const lineFor = (cls) => (l) => {
    const g = stationById[l.station_id];
    const s = bySat[l.sat_id];
    if (!g || !s) return null;
    return html`<line
      key=${`${cls}-${l.sat_id}-${l.station_id}`}
      class=${cls}
      x1=${px(g.lon_deg)}
      y1=${py(g.lat_deg)}
      x2=${px(s.lon_deg)}
      y2=${py(s.lat_deg)}
    />`;
  };
  // offered first so a served link always paints on top of the faint one
  const beams = [...offered.map(lineFor("beam offered")), ...served.map(lineFor("beam"))];

  const onScreen = (x, y) => x > -60 && x < W + 60 && y > -60 && y < H + 60;

  return html`
    <div
      class=${cn("map", dragging && "dragging")}
      ref=${ref}
      onWheel=${onWheel}
      onPointerDown=${onPointerDown}
      onPointerMove=${onPointerMove}
      onPointerUp=${endDrag}
      onPointerCancel=${endDrag}
    >
      ${W > 0 &&
      html`<svg width=${W} height=${H}>${graticule}${beams}</svg>`}

      ${shown.map((s) => {
        const x = px(s.lon_deg);
        const y = py(s.lat_deg);
        if (!onScreen(x, y)) return null;
        const link = s.current_station ? stationById[s.current_station] : null;
        return html`
          <div class="marker" key=${s.sat_id} style=${{ left: `${x}px`, top: `${y}px` }}>
            <div class="marker-inner">
              <div
                class=${cn(
                  "sat",
                  s.state === "transmitting" && "on",
                  s.state === "done" && "done",
                  s.state === "slewing" && "slewing",
                  !s.n_visible && "faded",
                )}
              ></div>
              <div class="marker-tip sat-tip">
                <div class="on">${s.sat_id} · ${s.state}</div>
                <div>
                  ${link
                    ? `→ ${s.current_station} (beam ${s.current_beam}) · ${bps(s.rate_bps)}`
                    : s.n_visible
                      ? `in view of ${s.visible_stations.join(", ")} · unserved`
                      : "no station in view"}
                </div>
                <div>${(s.backlog_bits / 1e9).toFixed(1)} Gb left · waited ${Math.round(s.wait_s)}s</div>
                ${/* analytical forecast — exact orbital mechanics, not a prediction */ null}
                <div class="tip-fc">${contactLine(s)}</div>
              </div>
            </div>
          </div>
        `;
      })}

      ${stations.map((g) => {
        const x = px(g.lon_deg);
        const y = py(g.lat_deg);
        if (!onScreen(x, y)) return null;
        const h = healthById[g.station_id];
        const tone = !g.up
          ? "var(--st-crit)"
          : g.degraded
            ? "var(--st-warn)"
            : "var(--fg)";
        return html`
          <div class="marker" key=${g.station_id} style=${{ left: `${x}px`, top: `${y}px` }}>
            <div class="marker-inner">
              <div
                class="gs"
                style=${{
                  borderColor: tone,
                  background: g.beams_active > 0 ? tone : "transparent",
                }}
              >
                ${g.beams_active > 0 &&
                html`<span class="gs-ping" style=${{ borderColor: tone }}></span>`}
              </div>
              <div class=${cn("gs-name", focus === g.station_id && "focused")}>
                ${g.station_id}
              </div>
              <div class="marker-tip gs-tip">
                <div class="on">${g.station_id}</div>
                <div>${g.beams_active}/${g.beams_available} beams · ${bps(g.rate_bps)}</div>
                <div>${g.weather} · ${g.rain_fade_db.toFixed(1)} dB fade</div>
                ${h &&
                html`<div>health ${(100 * h.health).toFixed(0)}% — ${h.reasons[0]}</div>`}
              </div>
            </div>
          </div>
        `;
      })}

      <div class="map-zoom">
        <button
          type="button"
          aria-label="Zoom in"
          onClick=${() => zoomAbout(1.6, W / 2, H / 2)}
        >
          +
        </button>
        <button
          type="button"
          aria-label="Zoom out"
          onClick=${() => zoomAbout(1 / 1.6, W / 2, H / 2)}
        >
          −
        </button>
      </div>

      <!-- HUD, in the site's method-stage style -->
      <div class="map-hud">
        ${/* the explicit ${" "} are load-bearing: htm trims the whitespace that
              sits between a newline and an element, exactly as JSX does */ null}
        ${frame
          ? html`<span class="on">${served.length}</span> serving ·${" "}
              <span class="on">${offered.length}</span> offered ·${" "}
              <span class="on">${sats.filter((s) => s.n_visible > 0).length}</span>/${sats.length}${" "}
              sats in view ·${" "}
              <span class="on">${stations.filter((s) => s.up).length}</span>/${stations.length}${" "}
              stations up`
          : "awaiting telemetry"}
      </div>

      <div class="map-filter">
        <button
          type="button"
          class=${cn(!showAll && "on")}
          onClick=${() => setShowAll(false)}
          title="Only satellites a station can currently see"
        >
          in view
        </button>
        <button
          type="button"
          class=${cn(showAll && "on")}
          onClick=${() => setShowAll(true)}
          title="The whole constellation, including satellites out of range"
        >
          all
        </button>
      </div>

      <div class="map-tag">ground segment</div>
      <span class="map-corner tl"></span>
      <span class="map-corner tr"></span>
      <span class="map-corner bl"></span>
      <span class="map-corner br"></span>
    </div>
  `;
}
