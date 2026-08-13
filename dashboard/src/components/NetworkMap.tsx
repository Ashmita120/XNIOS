"use client";

/**
 * Ground stations, satellites, active beams and weather, on a MapLibre canvas.
 *
 * No external tile server is used: the basemap is a vector graticule drawn from
 * the theme tokens. That keeps the map self-contained (no API key, no network
 * dependency, works offline) and — more importantly — keeps it monochrome,
 * which a photographic satellite basemap would destroy.
 *
 * Everything drawn here comes from one telemetry record: station lat/lon and
 * state from `stations`, sub-satellite points from `satellites`, and one line
 * per *active* link from `links`.
 *
 * Paint colours are resolved through `useThemeColors` rather than written as
 * CSS variables: MapLibre parses colours itself and never resolves `var(...)`.
 */

import * as React from "react";
import Map, { Layer, Marker, NavigationControl, Source } from "react-map-gl/maplibre";
import type { MapRef } from "react-map-gl/maplibre";
import "maplibre-gl/dist/maplibre-gl.css";
import type { Frame } from "@/lib/types";
import { bps, cn } from "@/lib/format";
import { useThemeColors, viz } from "@/lib/theme";

/** Minimal GeoJSON shapes — declared locally so the component does not depend
 *  on the global `GeoJSON` namespace being present. */
type LineFeature = {
  type: "Feature";
  properties: Record<string, unknown>;
  geometry: { type: "LineString"; coordinates: [number, number][] };
};
type FC = { type: "FeatureCollection"; features: LineFeature[] };

/** A 15° graticule — the only "basemap" the design wants. */
function graticule(): FC {
  const features: LineFeature[] = [];
  for (let lon = -180; lon <= 180; lon += 15) {
    features.push({
      type: "Feature",
      properties: { major: lon % 90 === 0 },
      geometry: {
        type: "LineString",
        coordinates: Array.from({ length: 37 }, (_, i) => [lon, -90 + i * 5] as [number, number]),
      },
    });
  }
  for (let lat = -75; lat <= 75; lat += 15) {
    features.push({
      type: "Feature",
      properties: { major: lat === 0 },
      geometry: {
        type: "LineString",
        coordinates: Array.from({ length: 73 }, (_, i) => [-180 + i * 5, lat] as [number, number]),
      },
    });
  }
  return { type: "FeatureCollection", features };
}

const GRATICULE = graticule();
const EMPTY_FC: FC = { type: "FeatureCollection", features: [] };

export function NetworkMap({ frame, focus }: { frame: Frame | null; focus?: string | null }) {
  const mapRef = React.useRef<MapRef | null>(null);
  const [ready, setReady] = React.useState(false);
  const c = useThemeColors();

  const stations = frame?.record.stations ?? [];
  const sats = frame?.record.satellites ?? [];
  const links = React.useMemo(
    () => (frame?.record.links ?? []).filter((l) => l.active),
    [frame],
  );
  const healthById = React.useMemo(
    () => Object.fromEntries((frame?.health.stations ?? []).map((h) => [h.station_id, h])),
    [frame],
  );

  const mapStyle = React.useMemo(
    () => ({
      version: 8 as const,
      sources: {},
      layers: [
        { id: "bg", type: "background" as const, paint: { "background-color": c.bg2 } },
      ],
    }),
    [c.bg2],
  );

  // fit to the ground segment once, when the first frame lands
  React.useEffect(() => {
    if (!ready || !mapRef.current || stations.length === 0) return;
    const lats = stations.map((s) => s.lat_deg);
    const lons = stations.map((s) => s.lon_deg);
    mapRef.current.fitBounds(
      [
        [Math.min(...lons) - 18, Math.min(...lats) - 14],
        [Math.max(...lons) + 18, Math.max(...lats) + 14],
      ],
      { padding: 60, duration: 900 },
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, stations.length]);

  const beamLines: FC = React.useMemo(() => {
    const byStation = Object.fromEntries(stations.map((s) => [s.station_id, s]));
    const bySat = Object.fromEntries(sats.map((s) => [s.sat_id, s]));
    const features: LineFeature[] = [];
    for (const l of links) {
      const g = byStation[l.station_id];
      const s = bySat[l.sat_id];
      if (!g || !s) continue;
      features.push({
        type: "Feature",
        properties: { rate: l.rate_bps },
        geometry: {
          type: "LineString",
          coordinates: [
            [g.lon_deg, g.lat_deg],
            [s.lon_deg, s.lat_deg],
          ],
        },
      });
    }
    return features.length ? { type: "FeatureCollection", features } : EMPTY_FC;
  }, [links, stations, sats]);

  return (
    <div className="relative h-full w-full overflow-hidden rounded-card border border-line bg-bg-2">
      <Map
        ref={mapRef}
        mapStyle={mapStyle as never}
        initialViewState={{ longitude: 79, latitude: 21, zoom: 2.6 }}
        onLoad={() => setReady(true)}
        attributionControl={false}
        dragRotate={false}
        style={{ width: "100%", height: "100%", background: c.bg2 }}
      >
        <NavigationControl position="bottom-right" showCompass={false} />

        <Source id="grat" type="geojson" data={GRATICULE as never}>
          <Layer
            id="grat-line"
            type="line"
            paint={{
              "line-color": viz(c, 0.1),
              "line-width": ["case", ["get", "major"], 1.1, 0.5] as never,
            }}
          />
        </Source>

        <Source id="beams" type="geojson" data={beamLines as never}>
          <Layer
            id="beam-line"
            type="line"
            paint={{
              "line-color": viz(c, 0.55),
              "line-width": 1.2,
              "line-dasharray": [2, 2] as never,
            }}
          />
        </Source>

        {sats.map((s) => {
          const on = s.state === "transmitting";
          const slew = s.state === "slewing";
          const done = s.state === "done";
          return (
            <Marker key={s.sat_id} longitude={s.lon_deg} latitude={s.lat_deg}>
              <div className="group relative -translate-x-1/2 -translate-y-1/2">
                <div
                  className={cn(
                    "h-[7px] w-[7px] rotate-45 border transition-all duration-300",
                    on
                      ? "border-transparent bg-fg"
                      : done
                        ? "border-line-2 bg-transparent"
                        : "border-mute bg-transparent",
                    slew && "animate-pulse",
                  )}
                />
                <div className="pointer-events-none absolute left-3 top-1/2 z-10 hidden -translate-y-1/2 whitespace-nowrap rounded border border-line bg-bg px-2 py-1 font-mono text-[10px] text-dim group-hover:block">
                  {s.sat_id} · {s.state} · {(s.backlog_bits / 1e9).toFixed(1)} Gb left
                </div>
              </div>
            </Marker>
          );
        })}

        {stations.map((g) => {
          const h = healthById[g.station_id];
          const tone = !g.up ? c.crit : g.degraded ? c.warn : c.fg;
          const focused = focus === g.station_id;
          return (
            <Marker key={g.station_id} longitude={g.lon_deg} latitude={g.lat_deg}>
              <div className="group relative -translate-x-1/2 -translate-y-1/2">
                <div
                  className="relative flex h-3 w-3 items-center justify-center rounded-full border"
                  style={{
                    borderColor: tone,
                    background: g.beams_active > 0 ? tone : "transparent",
                  }}
                >
                  {g.beams_active > 0 && (
                    <span
                      className="absolute -inset-[6px] animate-ping2 rounded-full border"
                      style={{ borderColor: tone }}
                    />
                  )}
                </div>
                <div
                  className={cn(
                    "pointer-events-none absolute left-4 top-1/2 -translate-y-1/2 whitespace-nowrap font-mono text-[10px] uppercase tracking-[.12em]",
                    focused ? "text-fg" : "text-mute",
                  )}
                >
                  {g.station_id}
                </div>
                <div className="pointer-events-none absolute bottom-4 left-0 z-10 hidden w-max rounded border border-line bg-bg px-2.5 py-1.5 font-mono text-[10px] leading-relaxed text-dim group-hover:block">
                  <div className="text-fg">{g.station_id}</div>
                  <div>
                    {g.beams_active}/{g.beams_available} beams · {bps(g.rate_bps)}
                  </div>
                  <div>
                    {g.weather} · {g.rain_fade_db.toFixed(1)} dB fade
                  </div>
                  {h && (
                    <div>
                      health {(100 * h.health).toFixed(0)}% — {h.reasons[0]}
                    </div>
                  )}
                </div>
              </div>
            </Marker>
          );
        })}
      </Map>

      {/* HUD, in the site's method-stage style */}
      <div className="pointer-events-none absolute bottom-4 left-5 font-mono text-[11px] uppercase tracking-[.12em] text-mute">
        {frame ? (
          <>
            <span className="text-fg">{links.length}</span> active beams ·{" "}
            <span className="text-fg">{sats.filter((s) => s.n_visible > 0).length}</span> in view ·{" "}
            <span className="text-fg">{stations.filter((s) => s.up).length}</span>/{stations.length}{" "}
            stations up
          </>
        ) : (
          "awaiting telemetry"
        )}
      </div>
      <div className="pointer-events-none absolute right-5 top-4 font-mono text-[10px] uppercase tracking-[.16em] text-mute">
        ground segment
      </div>
      <span className="pointer-events-none absolute left-3.5 top-3.5 h-3.5 w-3.5 border-l border-t border-line-2" />
      <span className="pointer-events-none absolute right-3.5 top-3.5 h-3.5 w-3.5 border-r border-t border-line-2" />
      <span className="pointer-events-none absolute bottom-3.5 left-3.5 h-3.5 w-3.5 border-b border-l border-line-2" />
      <span className="pointer-events-none absolute bottom-3.5 right-3.5 h-3.5 w-3.5 border-b border-r border-line-2" />
    </div>
  );
}
