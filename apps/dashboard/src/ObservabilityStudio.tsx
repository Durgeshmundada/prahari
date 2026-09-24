import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import type {
  ActorIdentity,
  IncidentCard,
  IncidentDetail,
  MetricRecord,
  NetworkPath,
  Topology,
  TrafficDemo
} from "./types";

type Props = {
  actor: ActorIdentity;
  fixtureOnly: boolean;
  simulationActive: boolean;
  apiConnected: boolean;
  telemetryReady: boolean;
  detectionError: string | null;
  primaryIncident: IncidentCard | null;
  detail: IncidentDetail | null;
  metrics: MetricRecord[];
  traffic: TrafficDemo | null;
  trafficVerified: boolean;
  fileSent: boolean;
  fileReceived: boolean;
  sender: string;
  receiver: string;
  topology: Topology | null;
  path: NetworkPath | null;
  alertCount: number;
  lastUpdated: string | null;
  refreshing: boolean;
  refreshSeconds: number;
  onRefresh: () => void;
  onSignOut?: () => void;
  children: ReactNode;
};

function percent(value: number | null | undefined): string {
  return value === null || value === undefined || !Number.isFinite(value)
    ? "—"
    : `${Math.round(Math.max(0, Math.min(1, value)) * 100)}%`;
}

function number(value: number | null | undefined, digits = 0): string {
  return value === null || value === undefined || !Number.isFinite(value)
    ? "—"
    : value.toFixed(digits);
}

function bytes(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  if (value < 1024) return `${Math.round(value)} B`;
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 ** 2).toFixed(1)} MB`;
}

function time(value: string | null | undefined, seconds = false): string {
  if (!value) return "Not available";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.valueOf())) return value;
  return new Intl.DateTimeFormat("en-IN", {
    dateStyle: seconds ? undefined : "medium",
    timeStyle: seconds ? "medium" : "short"
  }).format(parsed);
}

function label(value: string | null | undefined): string {
  if (!value) return "Not available";
  return value.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function locationLabel(value: string | null | undefined): string {
  if (!value) return "Not available";
  return value
    .replace(/^interface:/, "")
    .replaceAll(":", " · ")
    .replaceAll("-", " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function MetricIcon({ tone = "healthy" }: { tone?: "healthy" | "critical" | "info" | "warning" }) {
  return <span className={`studio-status-dot studio-status-dot--${tone}`} aria-hidden="true" />;
}

function StatusTile({ label, value, tone, icon }: {
  label: string;
  value: string;
  tone: "healthy" | "critical" | "info" | "warning";
  icon?: string;
}) {
  return (
    <div className="studio-status-tile">
      {icon ? <span className={`studio-tile-icon studio-tile-icon--${tone}`} aria-hidden="true">{icon}</span> : <MetricIcon tone={tone} />}
      <span><small>{label}</small><strong>{value}</strong></span>
    </div>
  );
}

function RiskChart({ incident }: { incident: IncidentCard | null }) {
  const risk = incident?.prediction.impact_risk;
  const values = [0, risk?.within_30_seconds ?? 0, risk?.within_60_seconds ?? 0,
    ((risk?.within_60_seconds ?? 0) + (risk?.within_120_seconds ?? 0)) / 2,
    risk?.within_120_seconds ?? 0];
  const xs = [50, 145, 240, 335, 430];
  const points = values.map((value, index) => `${xs[index]},${205 - value * 165}`).join(" ");
  const area = `50,205 ${points} 430,205`;
  return (
    <section id="risk-impact" className="studio-card risk-impact-card" aria-labelledby="risk-title">
      <h2 id="risk-title">Risk of Impact</h2>
      {incident ? (
        <figure>
          <svg viewBox="0 0 470 235" role="img" aria-label={`Predicted impact risk: ${percent(risk?.within_30_seconds)} in 30 seconds, ${percent(risk?.within_60_seconds)} in 60 seconds, and ${percent(risk?.within_120_seconds)} in 120 seconds.`}>
            <defs>
              <linearGradient id="riskFill" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0" stopColor="#ef233c" stopOpacity="0.24" />
                <stop offset="1" stopColor="#ef233c" stopOpacity="0" />
              </linearGradient>
            </defs>
            {[0, 0.25, 0.5, 0.75, 1].map((tick) => (
              <g key={tick}>
                <line x1="50" x2="430" y1={205 - tick * 165} y2={205 - tick * 165} className="chart-grid-line" />
                <text x="40" y={209 - tick * 165} textAnchor="end" className="chart-axis-label">{Math.round(tick * 100)}%</text>
              </g>
            ))}
            <polygon points={area} fill="url(#riskFill)" />
            <polyline points={points} className="risk-line" />
            <line x1="430" x2="430" y1="30" y2="205" className="risk-threshold" />
            <circle cx="430" cy={205 - values[4] * 165} r="6" className="risk-point" />
            <text x="440" y={209 - values[4] * 165} className="risk-end-label">{percent(values[4])}</text>
            {["Now", "30s", "60s", "90s", "120s"].map((tick, index) => (
              <text key={tick} x={xs[index]} y="226" textAnchor="middle" className="chart-axis-label">{tick}</text>
            ))}
          </svg>
        </figure>
      ) : <div className="studio-empty">Evaluating current telemetry…</div>}
    </section>
  );
}

function Sparkline({ values, tone = "critical" }: { values: number[]; tone?: "critical" | "warning" | "info" }) {
  if (values.length < 2) return <div className="sparkline-empty">Latest measurement only</div>;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;
  const points = values.map((value, index) => {
    const x = 4 + index * (152 / Math.max(1, values.length - 1));
    const y = 48 - ((value - min) / range) * 38;
    return `${x},${y}`;
  }).join(" ");
  return (
    <svg className="sparkline" viewBox="0 0 160 54" role="img" aria-label={`${values.length} measured samples from ${number(values[0], 2)} to ${number(values.at(-1), 2)}`}>
      <line x1="2" x2="158" y1="49" y2="49" className="spark-grid" />
      <polyline points={points} className={`spark-line spark-line--${tone}`} />
    </svg>
  );
}

function TelemetryMetric({ title, value, unit, values, baseline, tone = "critical", source }: {
  title: string;
  value: number | null;
  unit: string;
  values: number[];
  baseline?: number | null;
  tone?: "critical" | "warning" | "info";
  source: string;
}) {
  const delta = value !== null && baseline !== null && baseline !== undefined ? value - baseline : null;
  return (
    <article className="telemetry-metric">
      <div className="telemetry-metric-heading">
        <span>{title}</span>
        <strong>{value === null ? "—" : number(value, value < 10 ? 1 : 0)} <small>{unit}</small></strong>
      </div>
      <span className={`metric-delta metric-delta--${tone}`}>
        {delta === null ? source : `${delta >= 0 ? "↑" : "↓"} ${delta >= 0 ? "+" : ""}${number(delta, Math.abs(delta) < 10 ? 1 : 0)} ${unit}`}
      </span>
      <Sparkline values={values} tone={tone} />
      <small className="metric-source">{source}</small>
    </article>
  );
}

function ApplicationCard({ traffic, verified, sent, received, sender, receiver }: {
  traffic: TrafficDemo | null;
  verified: boolean;
  sent: boolean;
  received: boolean;
  sender: string;
  receiver: string;
}) {
  const size = traffic?.payload?.size_bytes ?? null;
  const receivedBytes = traffic?.payload?.bytes_received ?? null;
  const completion = size && receivedBytes !== null ? Math.min(100, Math.round((receivedBytes / size) * 100)) : 0;
  const at = time(traffic?.updated_at, true);
  return (
    <section id="application-test" className="studio-card application-card" aria-labelledby="application-title">
      <div className="studio-card-heading">
        <h2 id="application-title"><span className="heading-check" aria-hidden="true">✓</span> Application Test</h2>
        <span className={`source-pill ${verified ? "source-pill--healthy" : "source-pill--neutral"}`}>
          {verified ? "Verified safe test" : "Awaiting measurement"}
        </span>
      </div>
      {traffic?.payload?.name ? (
        <>
          <div className="file-result">
            <span className="file-symbol" aria-hidden="true">▱</span>
            <div><strong>{verified ? "File received and verified" : "File transfer measured"}</strong><span>{traffic.payload.name}</span></div>
          </div>
          <div className="file-progress"><span style={{ width: `${completion}%` }} /></div>
          <p className="file-size">{bytes(receivedBytes)} of {bytes(size)}</p>
          <div className="application-metrics">
            <div><span>Message delay</span><strong>{number(traffic.latency_ms?.p50)} ms</strong></div>
            <div><span>Upload speed</span><strong>{number(traffic.throughput_mbps)} Mbps</strong></div>
            <div><span>SHA-256 checksum</span><strong className={verified ? "healthy-text" : ""}>{verified ? "Verified" : "Pending"}</strong></div>
          </div>
          <div className="application-route">
            <div><span>Sender</span><strong>{sender}</strong></div>
            <div><span>Receiver</span><strong>{receiver}</strong></div>
            <div><span>Transport</span><strong>{traffic.transport ?? "Not available"}</strong></div>
          </div>
          <div className="application-footer">
            <ol>
              <li className={sent ? "done" : ""}>File sent <time>{at}</time></li>
              <li className={received ? "done" : ""}>File received <time>{at}</time></li>
              <li className={verified ? "done" : ""}>File checked <time>{at}</time></li>
            </ol>
            <p><strong>Privacy protected</strong> Content is discarded after verification unless a temporary download is explicitly offered. Laptop B controls this test.</p>
          </div>
        </>
      ) : (
        <div className="studio-empty"><strong>No application measurement yet</strong><span>Run the first Laptop B application test to measure delivery and checksum verification.</span></div>
      )}
    </section>
  );
}

function TopologyCard({ sender, receiver, path }: { sender: string; receiver: string; path: NetworkPath | null }) {
  return (
    <section id="topology" className="studio-card topology-card" aria-labelledby="topology-title">
      <div className="studio-card-heading">
        <h2 id="topology-title">Network Path / Topology</h2>
        <span className="source-pill source-pill--healthy">Backup path available</span>
      </div>
      <div className="topology-diagram" aria-label={`${sender} connects to ${receiver} over the primary ${path?.name ?? "network path"}. A configured backup path is available.`}>
        <div className="topology-node"><span aria-hidden="true">▥</span><strong>{sender}</strong><small>Site</small></div>
        <div className="topology-lines">
          <div className="primary-path"><span /><b aria-hidden="true">!</b><span /></div>
          <strong>Primary network path</strong><small>{path?.name ?? "Configured route"}</small>
          <div className="backup-path" />
          <em>Backup path (available)</em>
        </div>
        <div className="topology-node"><span aria-hidden="true">▤</span><strong>{receiver}</strong><small>Data centre</small></div>
      </div>
    </section>
  );
}

function InvestigationCard({ incident, detail }: { incident: IncidentCard | null; detail: IncidentDetail | null }) {
  if (!incident) {
    return <section id="incident-investigation" className="studio-card investigation-card"><div className="studio-empty">No incidents in this view.</div></section>;
  }
  const risk = incident.prediction.impact_risk;
  return (
    <section id="incident-investigation" className="studio-card investigation-card" aria-labelledby="investigation-title">
      <h2 id="investigation-title">Incident Investigation</h2>
      <div className="investigation-grid">
        <div>
          <h3>Predicted impact risk</h3>
          <div className="risk-chips">
            <span><small>30 seconds</small><strong>{percent(risk.within_30_seconds)}</strong></span>
            <span><small>60 seconds</small><strong>{percent(risk.within_60_seconds)}</strong></span>
            <span className="critical"><small>120 seconds</small><strong>{percent(risk.within_120_seconds)}</strong></span>
          </div>
          <dl className="compact-details">
            <div><dt>Likely root location</dt><dd>{locationLabel(incident.location.entity_id)}</dd></div>
            <div><dt>Affected applications</dt><dd>{incident.prediction.affected_scope.applications.map(label).join(", ") || "Not available"}</dd></div>
            <div><dt>Affected sites</dt><dd>{incident.prediction.affected_scope.sites.map(locationLabel).join(", ") || "Not available"}</dd></div>
          </dl>
        </div>
        <div className="evidence-column">
          <dl className="compact-details compact-details--model">
            <div><dt>Model version</dt><dd>{incident.prediction.model_version}</dd></div>
            <div><dt>Prediction ID</dt><dd title={incident.prediction.prediction_id}>{incident.prediction.prediction_id.slice(0, 18)}…</dd></div>
            <div><dt>Data quality</dt><dd>{label(incident.data_quality)}</dd></div>
          </dl>
          <h3>Key evidence</h3>
          <ul>{incident.prediction.evidence.slice(0, 5).map((item) => <li key={item}>{item}</li>)}</ul>
        </div>
        <div className="recommendation-column">
          <h3>Recommendation</h3>
          <p>{detail?.playbook.title ?? "Review current evidence and the controlled response playbook."}</p>
          <dl className="recommendation-status">
            <div><dt>Dry-run status</dt><dd>Available after review</dd></div>
            <div><dt>Human approval required</dt><dd>{detail?.playbook.requires_operator_approval === false ? "No" : "Yes"}</dd></div>
          </dl>
          <div className="investigation-actions">
            <a className="studio-button studio-button--primary" href="#operator-workspace">Review and simulate</a>
            <a className="studio-button studio-button--outline" href="#operator-workspace">Open approval workflow</a>
          </div>
        </div>
      </div>
    </section>
  );
}

function AuditCard({ detail, traffic, incident }: {
  detail: IncidentDetail | null;
  traffic: TrafficDemo | null;
  incident: IncidentCard | null;
}) {
  const events = [
    ...(detail?.audit_trail ?? []).map((event) => ({ at: event.occurred_at, text: label(event.event_type), tone: "info" })),
    ...(traffic?.updated_at ? [{ at: traffic.updated_at, text: `Application test ${traffic.status === "passed" ? "completed and verified" : traffic.status}`, tone: traffic.status === "passed" ? "healthy" : "warning" }] : []),
    ...(detail?.feature_window.data_quality.newest_observation_at ? [{ at: detail.feature_window.data_quality.newest_observation_at, text: `Telemetry evaluated · ${label(incident?.prediction.likely_incident_type)}`, tone: "critical" }] : [])
  ].sort((a, b) => Date.parse(b.at) - Date.parse(a.at)).slice(0, 7);
  return (
    <section className="studio-card audit-card" aria-labelledby="audit-title">
      <div className="studio-card-heading"><h2 id="audit-title">Audit History / Timeline</h2><span className="audit-filter">All events⌄</span></div>
      {events.length ? <ol>{events.map((event, index) => (
        <li key={`${event.at}-${event.text}-${index}`}><MetricIcon tone={event.tone as "healthy" | "critical" | "info" | "warning"} /><time>{time(event.at, true)}</time><span>{event.text}</span></li>
      ))}</ol> : <div className="studio-empty">No audit events recorded yet.</div>}
      <a href="#operator-workspace" className="studio-card-link">View full audit workflow →</a>
    </section>
  );
}

export function ObservabilityStudio(props: Props) {
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const sidebarRef = useRef<HTMLElement>(null);
  const incident = props.primaryIncident;
  const faultDetected = Boolean(incident && incident.severity !== "informational" && incident.prediction.likely_incident_type !== "normal");
  const detectionUnavailable = Boolean(props.detectionError);
  const risk120 = incident?.prediction.impact_risk.within_120_seconds ?? 0;
  const metricValues = (metric: string, convert = (value: number) => value) => props.metrics.filter((item) => item.metric === metric).map((item) => convert(item.value));
  const jitterValues = metricValues("jitter_ms");
  const lossValues = metricValues("packet_loss_ratio", (value) => value * 100);
  const jitterFeature = props.detail?.feature_window.metrics.jitter_ms;
  const lossFeature = props.detail?.feature_window.metrics.packet_loss_ratio;
  const latencyValues = [props.traffic?.latency_ms?.min, props.traffic?.latency_ms?.p50, props.traffic?.latency_ms?.p95, props.traffic?.latency_ms?.max]
    .filter((value): value is number => typeof value === "number" && Number.isFinite(value));
  const sourceLabel = props.simulationActive ? "Safe simulation" : props.fixtureOnly ? "Recorded replay" : "Live telemetry";

  useEffect(() => {
    if (sidebarRef.current) sidebarRef.current.inert = !sidebarOpen;
    if (!sidebarOpen) return;
    const previousOverflow = document.body.style.overflow;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setSidebarOpen(false);
    };
    document.body.style.overflow = "hidden";
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.body.style.overflow = previousOverflow;
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [sidebarOpen]);

  return (
    <div className={`studio-shell ${sidebarOpen ? "studio-shell--nav-open" : ""}`}>
      <a className="skip-link" href="#studio-main">Skip to dashboard</a>
      <aside
        ref={sidebarRef}
        id="primary-navigation"
        className="studio-sidebar"
        aria-label="Primary navigation"
        aria-hidden={!sidebarOpen}
      >
        <div className="studio-brand"><img src="/assets/prahari-logo.svg" alt="" /><span><strong>PRAHARI</strong><small>Predictive Network Assurance</small></span></div>
        <nav onClick={() => setSidebarOpen(false)}>
          <a className="active" href="#overview"><span>⌂</span>Overview</a>
          <a href="#telemetry"><span>⌁</span>Network</a>
          <a href="#application-test"><span>▦</span>Applications</a>
          <a href="#incident-investigation"><span>△</span>Incidents <b>{props.alertCount}</b></a>
          <a href="#topology"><span>⌘</span>Topology</a>
          <a href="#risk-impact"><span>⌁</span>Analytics</a>
          <a href="#case-workspace"><span>▤</span>Reports</a>
          <a href="#advanced-details"><span>⚙</span>Configuration</a>
        </nav>
        <div className="studio-sidebar-footer"><a href="#operator-workspace">? <span>Help</span></a><button type="button" onClick={() => setSidebarOpen(false)}>‹ <span>Collapse</span></button><small>PRAHARI v1.0.0<br />Smarter Networks. Safer Operations.</small></div>
      </aside>

      <div className="studio-page">
        <header className="studio-utility-header">
          <button
            className="mobile-menu"
            type="button"
            aria-label={sidebarOpen ? "Close navigation menu" : "Open navigation menu"}
            aria-controls="primary-navigation"
            aria-expanded={sidebarOpen}
            onClick={() => setSidebarOpen((open) => !open)}
          >
            <span aria-hidden="true">{sidebarOpen ? "×" : "☰"}</span>
          </button>
          <span className="environment-pill">◉ {sourceLabel}</span>
          <p>Application tests can be live. Network predictions use recorded or safely simulated telemetry.</p>
          <div className="utility-actions">
            <span className="api-indicator"><MetricIcon tone={props.apiConnected ? "healthy" : "critical"} />API {props.apiConnected ? "Connected" : "Disconnected"}</span>
            <span className="last-refresh">Last refresh: {time(props.lastUpdated, true)}</span>
            <button className={`studio-refresh ${props.refreshing ? "is-refreshing" : ""}`} type="button" onClick={props.onRefresh}>↻ <span>Refresh</span></button>
            <details className="account-menu"><summary><span className="account-avatar">○</span><span><strong>{props.actor.actor_id}</strong><small>{label(props.actor.role)}</small></span></summary>{props.onSignOut ? <button type="button" onClick={props.onSignOut}>Sign out</button> : null}</details>
          </div>
        </header>

        <main id="studio-main" className="studio-content" tabIndex={-1}>
          <section id="overview" className="studio-intro" aria-labelledby="studio-title">
            <div className="studio-title"><img src="/assets/observability-wave.svg" alt="" /><span><h1 id="studio-title">Observability Studio</h1><p>Real-time network insights, predictive intelligence and application assurance</p></span></div>
            <div className="studio-status-strip">
              <StatusTile label="Dashboard" value={props.apiConnected ? "API Connected" : "Disconnected"} tone={props.apiConnected ? "healthy" : "critical"} />
              <StatusTile label="Telemetry" value={props.telemetryReady ? "Ready" : "Unavailable"} tone={props.telemetryReady ? "healthy" : "warning"} />
              <StatusTile label="Application link" value={props.trafficVerified ? "Verified" : "Awaiting test"} tone={props.trafficVerified ? "healthy" : "warning"} />
              <StatusTile label="Active alerts" value={props.detectionError ? "Unknown" : String(props.alertCount)} tone={props.alertCount ? "critical" : "healthy"} />
              <StatusTile label="Automatic refresh" value={`every ${props.refreshSeconds} seconds`} tone="info" icon="◷" />
            </div>
          </section>

          <section className="studio-primary-grid">
            <article className={`studio-card fault-card ${faultDetected ? "fault-card--active" : "fault-card--clear"}`} aria-live="polite">
              <div className="fault-heading"><h2><span aria-hidden="true">{detectionUnavailable ? "?" : faultDetected ? "♟" : "✓"}</span>{detectionUnavailable ? "Detection unavailable" : faultDetected ? "Fault detected" : "No active fault"}</h2>{incident ? <span className={`severity-pill severity-pill--${incident.severity}`}>Severity: {label(incident.severity)}</span> : null}</div>
              {detectionUnavailable ? <div className="studio-empty"><strong>Network telemetry is unavailable.</strong><span>PRAHARI cannot determine whether a fault is present.</span></div> : incident ? <>
                <h3>{label(incident.prediction.likely_incident_type)}</h3>
                <p>Predicted impact risk within 120 seconds</p>
                <div className="fault-risk"><strong>{percent(risk120)}</strong><div role="progressbar" aria-label="Predicted impact risk within 120 seconds" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(risk120 * 100)}><span style={{ width: percent(risk120) }} /></div></div>
                <dl className="fault-details">
                  <div><dt>Time to impact</dt><dd>{incident.prediction.time_to_impact.range}</dd></div>
                  <div><dt>Likely location</dt><dd>{locationLabel(incident.location.entity_id)}</dd></div>
                  <div><dt>Data quality</dt><dd><span className="quality-pill">{label(incident.data_quality)}</span></dd></div>
                  <div><dt>Confidence</dt><dd>{percent(incident.prediction.confidence)}</dd></div>
                  <div><dt>Data source</dt><dd><span className="environment-pill">{sourceLabel}</span></dd></div>
                </dl>
                <a className="fault-evidence-link" href="#incident-investigation">▤ Review prediction evidence →</a>
              </> : <div className="studio-empty"><strong>No active prediction</strong><span>New network predictions will appear here.</span></div>}
            </article>
            <RiskChart incident={incident} />
            <ApplicationCard traffic={props.traffic} verified={props.trafficVerified} sent={props.fileSent} received={props.fileReceived} sender={props.sender} receiver={props.receiver} />
          </section>

          <section className="studio-secondary-grid">
            <section id="telemetry" className="studio-card telemetry-card" aria-labelledby="telemetry-title">
              <h2 id="telemetry-title">Key Network Telemetry <small>({props.sender} → {props.receiver})</small></h2>
              <div className="telemetry-grid">
                <TelemetryMetric title="Packet Loss" value={lossFeature ? lossFeature.latest * 100 : props.traffic?.loss_percent ?? null} unit="%" values={lossValues} baseline={lossFeature ? lossFeature.baseline_median * 100 : null} source="network telemetry" />
                <TelemetryMetric title="Jitter" value={jitterFeature?.latest ?? null} unit="ms" values={jitterValues} baseline={jitterFeature?.baseline_median} tone="warning" source="network telemetry" />
                <TelemetryMetric title="Latency (RTT)" value={props.traffic?.latency_ms?.p50 ?? null} unit="ms" values={latencyValues} baseline={props.traffic?.latency_ms?.min} source="application probe" />
                <TelemetryMetric title="Throughput" value={props.traffic?.throughput_mbps ?? null} unit="Mbps" values={props.traffic?.throughput_mbps ? [props.traffic.throughput_mbps] : []} tone="info" source="application probe" />
              </div>
            </section>
            <TopologyCard sender={props.sender} receiver={props.receiver} path={props.path} />
          </section>

          <section className="studio-bottom-grid">
            <InvestigationCard incident={incident} detail={props.detail} />
            <AuditCard detail={props.detail} traffic={props.traffic} incident={incident} />
          </section>

          {props.children}
        </main>
      </div>
      {sidebarOpen ? <button className="sidebar-backdrop" type="button" aria-label="Close navigation" onClick={() => setSidebarOpen(false)} /> : null}
    </div>
  );
}
