/**
 * Mirrors the dataclasses in `xnios/telemetry.py` and `xnios/health.py`.
 * Field names are identical on purpose — the wire format is the telemetry
 * schema itself, so there is no translation layer to drift out of sync.
 * `schema_version` travels with every record; bump both sides together.
 */

export interface LinkRecord {
  sat_id: string;
  station_id: string;
  active: boolean;
  beam: number | null;
  channel: number | null;
  elev_deg: number;
  az_deg: number;
  range_km: number;
  scan_deg: number;
  snr_db: number;
  sinr_db: number;
  inr_db: number;
  ber: number;
  rain_fade_db: number;
  alloc_bw_hz: number;
  alloc_power_w: number;
  rate_bps: number;
  clean_rate_bps: number;
  bits_delivered: number;
  slewing: boolean;
  session_age_s: number;
}

export interface StationRecord {
  station_id: string;
  lat_deg: number;
  lon_deg: number;
  up: boolean;
  beams_total: number;
  beams_available: number;
  beams_active: number;
  beam_utilization: number;
  bandwidth_base_hz: number;
  bandwidth_pool_hz: number;
  bandwidth_alloc_hz: number;
  bandwidth_utilization: number;
  link_power_w: number;
  rate_bps: number;
  bits_delivered: number;
  weather: string;
  rain_fade_db: number;
  connected_sats: string[];
  visible_sats: number;
  mean_sinr_db: number;
  phased_array: boolean;
  n_channels: number;
  channels_in_use: number;
  degraded: boolean;
}

export interface SatelliteRecord {
  sat_id: string;
  lat_deg: number;
  lon_deg: number;
  alt_km: number;
  state: "idle" | "waiting" | "slewing" | "transmitting" | "done";
  backlog_bits: number;
  backlog0_bits: number;
  delivered_bits: number;
  bits_delivered_step: number;
  wait_s: number;
  ready_since: number | null;
  priority: number;
  tier: string;
  deadline_s: number | null;
  time_to_deadline_s: number | null;
  visible_stations: string[];
  n_visible: number;
  current_station: string | null;
  current_beam: number | null;
  rate_bps: number;
  best_visible_rate_bps: number;
}

export interface NetworkRecord {
  t: number;
  bits_delivered_step: number;
  bits_delivered_total: number;
  throughput_bps: number;
  queue_bits: number;
  demand_bits: number;
  completion_rate: number;
  delivery_fraction: number;
  n_sats: number;
  n_completed: number;
  n_backlogged: number;
  n_waiting: number;
  stations_total: number;
  stations_up: number;
  beams_total: number;
  beams_available: number;
  beams_active: number;
  beam_utilization: number;
  bandwidth_pool_hz: number;
  bandwidth_alloc_hz: number;
  bandwidth_utilization: number;
  contention_ratio: number;
  n_visible_pairs: number;
  n_sats_with_link: number;
  coverage: number;
  mean_elev_deg: number;
  mean_sinr_db: number;
  min_sinr_db: number;
  energy_j_step: number;
  energy_j_total: number;
  power_w: number;
  weather_counts: Record<string, number>;
  mean_rain_fade_db: number;
  max_rain_fade_db: number;
  sessions_active: number;
  sessions_started_total: number;
  interruptions_total: number;
  handovers_total: number;
  proactive_handovers_total: number;
  mean_wait_s: number;
  decision_ms: number;
}

export interface DecisionRecord {
  scheduler: string;
  bandwidth_allocator: string;
  power_allocator: string;
  freq_allocator: string;
  decision_ms: number;
  assignments: { sat_id: string; station_id: string; beam: number }[];
  n_assigned: number;
  n_free_candidates: number;
  n_unserved: number;
  /** "static" today; the AI decision engine will write "ai" here. */
  source: string;
  rationale: string | null;
  reasons: Record<string, string>;
  expected: Record<string, number>;
}

export interface EventRecord {
  t: number;
  kind: string;
  sat_id: string | null;
  station_id: string | null;
  detail: Record<string, unknown>;
}

export interface TelemetryRecord {
  t: number;
  step: number;
  schema_version: string;
  network: NetworkRecord;
  stations: StationRecord[];
  links: LinkRecord[];
  satellites: SatelliteRecord[];
  decision: DecisionRecord | null;
  events: EventRecord[];
}

export type Level = "low" | "moderate" | "high" | "critical" | "";

export interface Indicator {
  name: string;
  score: number;
  level: Level;
  value: number;
  unit: string;
  /** true = higher score is WORSE (congestion, failure risk, weather) */
  severity: boolean;
  factors: Record<string, unknown>;
  note: string;
}

export interface StationHealth {
  station_id: string;
  health: number;
  level: Level;
  up: boolean;
  beams_available: number;
  beams_total: number;
  beam_utilization: number;
  bandwidth_utilization: number;
  weather: string;
  rain_fade_db: number;
  mean_sinr_db: number;
  connected: number;
  degraded: boolean;
  reasons: string[];
}

export interface HealthReport {
  t: number;
  network_health: number;
  level: Level;
  indicators: Record<string, Indicator>;
  stations: StationHealth[];
  headline: Record<string, string | number>;
  weights: Record<string, number>;
  notes: string[];
  window_steps: number;
}

export interface Frame {
  record: TelemetryRecord;
  health: HealthReport;
  index: number;
  steps: number;
  total_steps: number;
}

export interface RunInfo {
  run_id: string;
  preset: string;
  name: string;
  status: "queued" | "running" | "done" | "error";
  error: string | null;
  created: number;
  finished: number | null;
  steps: number;
  total_steps: number;
  progress: number;
  policy: {
    scheduler: string;
    bandwidth_allocator: string;
    power_allocator: string;
    freq_allocator: string;
  };
  summary: Record<string, number> | null;
  meta: {
    run_id: string;
    scenario: string;
    seed: number | null;
    duration_s: number;
    dt_s: number;
    n_satellites: number;
    n_stations: number;
    n_beams_total: number;
    scheduler: string;
    weather_model: string;
    dynamics: boolean;
    handover: boolean;
    stations: {
      id: string;
      lat: number;
      lon: number;
      num_beams: number;
      phased_array: boolean;
      max_scan_deg: number;
    }[];
  } | null;
}

export interface Preset {
  key: string;
  name: string;
  description: string;
  n_satellites: number;
  n_stations: number;
  duration_s: number;
  dt_s: number;
  weather: string;
  failures: boolean;
  handover: boolean;
}

export interface Policies {
  schedulers: string[];
  bandwidth_allocators: string[];
  power_allocators: string[];
  freq_allocators: string[];
  kpi_keys: string[];
  health_weights: Record<string, number>;
  schema_version: string;
}

export interface TimelinePoint {
  t: number;
  network_health: number;
  level: Level;
  congestion: number;
  failure_risk: number;
  coverage: number;
  link_quality: number;
  availability: number;
}
