import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { api } from "./api";
import type { ActorIdentity, CaseSearchFilters, ClosureCase } from "./types";

type Props = { actor: ActorIdentity; refreshToken: string | null; onChanged: () => Promise<void> };

export function ClosureCases({ actor, refreshToken, onChanged }: Props) {
  const [cases, setCases] = useState<ClosureCase[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [status, setStatus] = useState<NonNullable<CaseSearchFilters["status"]>>("");
  const [outcome, setOutcome] = useState<NonNullable<CaseSearchFilters["outcome"]>>("");
  const [incidentType, setIncidentType] = useState<
    NonNullable<CaseSearchFilters["incident_type"]>
  >("");
  const [entityId, setEntityId] = useState("");
  const [networkProfile, setNetworkProfile] = useState("");
  const [filters, setFilters] = useState<CaseSearchFilters>({});
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [decision, setDecision] = useState<"approved" | "rejected">("approved");
  const selected = cases.find((item) => item.id === selectedId) ?? null;
  const isReviewer = actor.role === "reviewer" || actor.role === "admin";
  const hasLegacyRecoveryEvidence =
    selected?.recovery_comparison?.schema_version === "recovery-comparison-v1";

  useEffect(() => {
    if (saving) return;
    let active = true;
    setLoading(true); setError(null);
    void api.cases(filters, offset).then((values) => {
      if (active) setCases(values);
    }).catch((error: unknown) => {
      if (active) { setCases([]); setError(error instanceof Error ? error.message : "Unable to load closure cases."); }
    }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [filters, offset, refreshToken, saving]);

  function applyFilters(event: FormEvent) {
    event.preventDefault();
    setFilters({
      status,
      outcome,
      incident_type: incidentType,
      entity_id: entityId.trim(),
      network_profile_sha256: networkProfile.trim()
    });
    setOffset(0);
    setSelectedId(null);
  }

  function clearFilters() {
    setStatus(""); setOutcome(""); setIncidentType(""); setEntityId(""); setNetworkProfile("");
    setFilters({}); setOffset(0); setSelectedId(null);
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!selected || saving || !isReviewer || !note.trim() || selected.review) return;
    if (decision === "approved" && hasLegacyRecoveryEvidence) return;
    setSaving(true); setError(null); setMessage(null);
    try {
      const result = await api.decideCase(selected.id, { actor_id: actor.actor_id, decision, note: note.trim() });
      setCases((values) => values.map((item) => item.id === result.id ? result : item));
      setMessage(`Closure case ${result.review?.decision}. The decision and reviewer were recorded.`);
      setNote("");
      await onChanged();
    } catch (error) { setError(error instanceof Error ? error.message : "The decision could not be saved."); }
    finally { setSaving(false); }
  }

  return <section className="case-workbench" aria-labelledby="case-workbench-title">
    <div className="workbench-heading">
      <div><p className="section-label">REVIEWED HISTORY</p><h2 id="case-workbench-title">Closure cases</h2>
        <p>Review the submitted evidence and preserve the outcome for future investigations.</p></div>
      <form className="case-search-filters" onSubmit={applyFilters}>
        <label>Case status<select value={status} disabled={saving} onChange={(event) => setStatus(event.target.value as typeof status)}>
          <option value="">All cases</option><option value="pending">Awaiting review</option>
          <option value="approved">Approved</option><option value="rejected">Rejected</option>
        </select></label>
        <label>Outcome<select value={outcome} disabled={saving} onChange={(event) => setOutcome(event.target.value as typeof outcome)}>
          <option value="">All outcomes</option><option value="resolved">Resolved</option>
          <option value="partially_resolved">Partially resolved</option><option value="failed">Failed</option>
          <option value="false_positive">False positive</option><option value="uncertain">Uncertain</option>
        </select></label>
        <label>Incident type<select value={incidentType} disabled={saving} onChange={(event) => setIncidentType(event.target.value as typeof incidentType)}>
          <option value="">All incident types</option><option value="normal">Normal</option>
          <option value="tunnel_degradation">Tunnel degradation</option>
        </select></label>
        <label>Entity ID<input value={entityId} maxLength={200} onChange={(event) => setEntityId(event.target.value)} /></label>
        <label>Profile SHA-256<input value={networkProfile} maxLength={64} pattern="[0-9a-f]{64}" onChange={(event) => setNetworkProfile(event.target.value.toLowerCase())} /></label>
        <div className="case-search-actions"><button type="submit" disabled={saving}>Search cases</button><button type="button" disabled={saving} onClick={clearFilters}>Clear filters</button></div>
      </form>
    </div>
    {loading ? <p role="status">Loading closure cases...</p> : null}
    {error ? <p className="workbench-error" role="alert">{error}</p> : null}
    {message ? <p role="status">{message}</p> : null}
    {!loading && !error && !cases.length ? <p>No closure cases match this view.</p> : null}
    <div className="case-layout">
      <nav aria-label="Closure cases">{cases.map((item) => <button key={item.id} type="button"
        aria-current={selectedId === item.id ? "true" : undefined} disabled={saving}
        onClick={() => { setSelectedId(item.id); setNote(""); setMessage(null); setDecision(item.recovery_comparison?.schema_version === "recovery-comparison-v1" ? "rejected" : "approved"); }}>
        <strong>{item.submission.outcome.replaceAll("_", " ")}</strong>
        <span>{item.submission.actor_id} · {new Date(item.created_at).toLocaleString()}</span>
        <span>{item.review?.decision ?? "Awaiting review"}</span>
      </button>)}</nav>
      {selected ? <article className="case-evidence">
        <h3>{selected.incident.summary}</h3>
        <p><strong>Submitted outcome:</strong> {selected.submission.outcome.replaceAll("_", " ")}</p>
        <p>{selected.submission.note}</p>
        <dl>
          <dt>Model</dt><dd>{selected.incident.prediction.model_version}</dd>
          <dt>Evidence captured through</dt><dd>{new Date(selected.feature_window.window_end).toLocaleString()}</dd>
          <dt>Application impact at submission</dt><dd>{selected.app_impact_started ? "Present" : "Not detected"}</dd>
        </dl>
        {selected.recovery_comparison ? <div className="recovery-comparison">
          <h4>Measured recovery comparison</h4>
          <p><strong>{selected.recovery_comparison.recovered ? "Recovery detected" : "Recovery not detected"}</strong>
            {` across ${selected.recovery_comparison.required_sustained_seconds}s windows on ${selected.recovery_comparison.path_id}.`}</p>
          {hasLegacyRecoveryEvidence ? <p role="note">This legacy comparison remains readable, but it cannot be approved as recovery evidence. Submit a new v2 comparison with captured thresholds.</p> : null}
          <dl>
            <dt>Before intervention</dt><dd>{selected.recovery_comparison.before.samples.length} samples; max loss {(selected.recovery_comparison.before.packet_loss_ratio_max * 100).toFixed(1)}%; max jitter {selected.recovery_comparison.before.jitter_ms_max.toFixed(1)} ms</dd>
            <dt>After intervention</dt><dd>{selected.recovery_comparison.after.samples.length} samples; max loss {(selected.recovery_comparison.after.packet_loss_ratio_max * 100).toFixed(1)}%; max jitter {selected.recovery_comparison.after.jitter_ms_max.toFixed(1)} ms</dd>
            <dt>Impact policy</dt><dd>{selected.recovery_comparison.impact_policy_version}{selected.recovery_comparison.impact_thresholds ? `; loss ≥ ${(selected.recovery_comparison.impact_thresholds.packet_loss_ratio * 100).toFixed(1)}% or jitter ≥ ${selected.recovery_comparison.impact_thresholds.jitter_ms.toFixed(1)} ms` : "; legacy thresholds were not captured"}</dd>
            <dt>Evidence SHA-256</dt><dd><code>{selected.recovery_comparison.evidence_sha256}</code></dd>
          </dl>
        </div> : <p>No automatic before/after comparison was captured for this case.</p>}
        <ul>{selected.incident.prediction.evidence.map((evidence, index) => <li key={index}>{evidence}</li>)}</ul>
        {selected.review ? <div role="note"><strong>{selected.review.decision} by {selected.review.actor_id}</strong><p>{selected.review.note}</p></div> :
          isReviewer && selected.submission.actor_id !== actor.actor_id ? <form onSubmit={(event) => void submit(event)}>
            <label>Review decision<select value={decision} disabled={saving} onChange={(event) => setDecision(event.target.value as "approved" | "rejected")}>
              <option value="approved" disabled={hasLegacyRecoveryEvidence}>Approve evidence and outcome</option><option value="rejected">Reject evidence or outcome</option>
            </select></label>
            <label>Independent review note<textarea required maxLength={2000} rows={4} value={note} onChange={(event) => setNote(event.target.value)} /></label>
            <p>This decision is final for this submission. A correction requires a new closure case.</p>
            <button type="submit" disabled={saving || loading || !note.trim() || (decision === "approved" && hasLegacyRecoveryEvidence)}>{saving ? "Recording decision..." : "Record review decision"}</button>
          </form> : <p>An independent reviewer must assess this submission.</p>}
      </article> : <p>Select a case to inspect its captured evidence.</p>}
    </div>
    <div className="case-pagination">
      <button disabled={saving || loading || offset === 0} onClick={() => { setOffset(Math.max(0, offset - 20)); setSelectedId(null); }}>Newer cases</button>
      <button disabled={saving || loading || cases.length < 20} onClick={() => { setOffset(offset + 20); setSelectedId(null); }}>Older cases</button>
    </div>
  </section>;
}
