import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { api, ApiError } from "./api";
import type {
  ActionResult,
  ActorIdentity,
  ClosureOutcome,
  IncidentBrief,
  IncidentCard,
  IncidentDetail
} from "./types";

type Props = {
  actor: ActorIdentity;
  incidents: IncidentCard[];
  loadError: string | null;
  refreshToken: string | null;
  onChanged: () => Promise<void>;
};

type Operation = "review" | "brief" | "dry-run" | "approve" | "rollback" | "close";

function percent(value: number): string {
  return `${Math.round(value * 100)}%`;
}

function timestamp(value: string | null): string {
  if (!value) return "Not available";
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? value : parsed.toLocaleString();
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  return error instanceof Error ? error.message : "The request could not be completed.";
}

export function IncidentWorkbench({ actor, incidents, loadError, refreshToken, onChanged }: Props) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<IncidentDetail | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const actorId = actor.actor_id;
  const canOperate = actor.role === "operator" || actor.role === "admin";
  const [note, setNote] = useState("");
  const [operation, setOperation] = useState<Operation | null>(null);
  const [result, setResult] = useState<ActionResult | null>(null);
  const [resultPredictionId, setResultPredictionId] = useState<string | null>(null);
  const [resultActorId, setResultActorId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [brief, setBrief] = useState<IncidentBrief | null>(null);
  const [briefPredictionId, setBriefPredictionId] = useState<string | null>(null);
  const [closureOutcome, setClosureOutcome] = useState<ClosureOutcome>("resolved");
  const [closureNote, setClosureNote] = useState("");
  const [closureIntervention, setClosureIntervention] = useState("");
  const [closureMessage, setClosureMessage] = useState<string | null>(null);
  const selectedPredictionId = incidents.find((incident) => incident.id === selectedId)
    ?.prediction.prediction_id;
  const evidenceCurrent = detail?.incident.prediction.prediction_id === selectedPredictionId;
  const inputsBlocked = !canOperate || Boolean(operation) || loading || !evidenceCurrent
    || !actorId.trim() || Boolean(detail?.workflow.unconfirmed_action);
  const deviceInputsBlocked = inputsBlocked || !detail?.device_dispatch_enabled;
  const canApprove = result?.status === "dry_run_passed"
    && resultPredictionId === selectedPredictionId
    && resultActorId === actorId.trim();

  useEffect(() => {
    if (!incidents.length) {
      setSelectedId(null);
      setDetail(null);
      return;
    }
    if (!selectedId || !incidents.some((incident) => incident.id === selectedId)) {
      setSelectedId(incidents[0].id);
    }
  }, [incidents, selectedId]);

  useEffect(() => {
    setDetail(null);
    setBrief(null);
    setResult(null);
    setActionError(null);
    setClosureMessage(null);
  }, [selectedId]);

  useEffect(() => {
    if (!selectedId || operation) return;
    let active = true;
    setLoading(true);
    setDetailError(null);
    void api
      .incidentDetail(selectedId)
      .then((value) => {
        if (active) setDetail(value);
      })
      .catch((error: unknown) => {
        if (active) {
          setDetail(null);
          setDetailError(errorMessage(error));
        }
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [selectedId, refreshToken, operation]);

  async function generateBrief() {
    if (!canOperate || !detail || operation || loading || !evidenceCurrent) return;
    setOperation("brief");
    setBrief(null);
    setActionError(null);
    try {
      const result = await api.incidentBrief(detail.incident.id, detail.incident.prediction.prediction_id);
      if (result.grounding.incident_snapshot.prediction.prediction_id !== detail.incident.prediction.prediction_id) {
        throw new Error("Incident evidence changed. Refresh before generating another brief.");
      }
      setBrief(result);
      setBriefPredictionId(detail.incident.prediction.prediction_id);
    } catch (error) {
      setActionError(errorMessage(error));
    } finally {
      setOperation(null);
    }
  }

  async function submit(event: FormEvent, nextOperation: Operation) {
    event.preventDefault();
    if (!detail || inputsBlocked || !note.trim()) return;
    if (nextOperation === "approve" && !canApprove) return;
    setOperation(nextOperation);
    setResult(null);
    setActionError(null);
    const payload = {
      actor_id: actorId.trim(), note: note.trim(),
      prediction_id: detail.incident.prediction.prediction_id
    };
    try {
      if (nextOperation === "review") {
        await api.review(detail.incident.id, payload);
        setNote("");
        await onChanged();
        setDetail(await api.incidentDetail(detail.incident.id));
      } else {
        const response =
          nextOperation === "dry-run"
            ? await api.dryRun(detail.incident.id, detail.playbook.id, payload)
            : nextOperation === "rollback"
              ? await api.rollback(detail.incident.id, detail.playbook.id, payload)
              : await api.approve(detail.incident.id, detail.playbook.id, payload);
        setResult(response.result);
        setResultPredictionId(detail.incident.prediction.prediction_id);
        setResultActorId(payload.actor_id);
        setDetail(await api.incidentDetail(detail.incident.id));
      }
    } catch (error) {
      setActionError(errorMessage(error));
    } finally {
      setOperation(null);
    }
  }

  async function submitClosure(event: FormEvent) {
    event.preventDefault();
    if (!detail || inputsBlocked || !closureNote.trim()) return;
    if (!detail.fixture_only && closureOutcome === "resolved" && !closureIntervention) return;
    setOperation("close");
    setActionError(null);
    setClosureMessage(null);
    try {
      const incident = await api.close(detail.incident.id, {
        actor_id: actorId.trim(),
        prediction_id: detail.incident.prediction.prediction_id,
        outcome: closureOutcome,
        note: closureNote.trim(),
        intervention_at: closureIntervention
          ? new Date(closureIntervention).toISOString()
          : null
      });
      setClosureMessage(
        incident.state === "resolved"
          ? "Closure approved. The reviewed outcome is eligible for incident memory."
          : incident.state === "validating"
            ? "Closure submitted. An independent reviewer must approve it before PRAHARI marks the incident resolved."
            : "Outcome recorded and escalated. PRAHARI has not claimed that service recovered."
      );
      setClosureNote("");
      setClosureIntervention("");
      await onChanged();
      setDetail(await api.incidentDetail(detail.incident.id));
    } catch (error) {
      setActionError(errorMessage(error));
    } finally {
      setOperation(null);
    }
  }

  return (
    <section className="incident-workbench" aria-labelledby="incident-workbench-title">
      <div className="workbench-heading">
        <div>
          <p className="section-label">OPERATOR WORKSPACE</p>
          <h2 id="incident-workbench-title">Network incidents</h2>
          <p>Review evidence and validate a playbook. Device changes remain safety-gated.</p>
        </div>
        <span className="incident-count">{incidents.length} monitored</span>
      </div>

      {loadError ? <p className="workbench-error" role="alert">{loadError}</p> : null}
      {!loadError && !incidents.length ? (
        <div className="workbench-empty"><strong>No active incidents</strong><span>New predictions will appear here.</span></div>
      ) : null}

      {incidents.length ? (
        <div className="workbench-layout">
          <nav className="incident-selector" aria-label="Active incidents">
            {incidents.map((incident) => (
              <button
                key={incident.id}
                type="button"
                className={incident.id === selectedId ? "incident-option incident-option--active" : "incident-option"}
                aria-current={incident.id === selectedId ? "true" : undefined}
                disabled={Boolean(operation)}
                onClick={() => setSelectedId(incident.id)}
              >
                <span className={`severity-dot severity-dot--${incident.severity}`} aria-hidden="true" />
                <span><strong>{incident.summary}</strong><small>{incident.location.site_id} · {incident.state}</small></span>
              </button>
            ))}
          </nav>

          <div className="incident-panel" aria-live="polite">
            {loading ? <p className="panel-state">{detail ? "Refreshing incident evidence..." : "Loading incident evidence..."}</p> : null}
            {detailError ? <p className="workbench-error" role="alert">{detailError}</p> : null}
            {detail && detail.incident.id === selectedId ? (
              <>
                <div className="incident-title-row">
                  <div><span className={`status-tag status-tag--${detail.incident.severity}`}>{detail.incident.severity}</span><span className="status-tag">{detail.incident.state}</span></div>
                  <span className="model-label">{detail.incident.prediction.risk_status} model</span>
                </div>

                <h3>{detail.incident.summary}</h3>
                <p className="incident-id">Incident {detail.incident.id}</p>

                <div className="risk-grid">
                  <div><span>Impact within 30s</span><strong>{percent(detail.incident.prediction.impact_risk.within_30_seconds)}</strong></div>
                  <div><span>Impact within 60s</span><strong>{percent(detail.incident.prediction.impact_risk.within_60_seconds)}</strong></div>
                  <div><span>Impact within 120s</span><strong>{percent(detail.incident.prediction.impact_risk.within_120_seconds)}</strong></div>
                  <div><span>Prediction confidence</span><strong>{percent(detail.incident.prediction.confidence)}</strong></div>
                </div>

                <dl className="prediction-context" aria-label="Prediction provenance">
                  <div>
                    <dt>Estimated time to impact</dt>
                    <dd>{detail.incident.prediction.time_to_impact.range}</dd>
                  </div>
                  <div>
                    <dt>Model</dt>
                    <dd>{detail.incident.prediction.model_version}</dd>
                  </div>
                  <div>
                    <dt>Evidence window</dt>
                    <dd>{timestamp(detail.feature_window.window_start)} to {timestamp(detail.feature_window.window_end)}</dd>
                  </div>
                  <div>
                    <dt>Newest observation</dt>
                    <dd>{timestamp(detail.feature_window.data_quality.newest_observation_at)}</dd>
                  </div>
                </dl>
                <p className="prediction-basis">
                  <strong>ETA basis:</strong> {detail.incident.prediction.time_to_impact.basis}
                </p>
                {detail.feature_window.data_quality.reasons.length ? (
                  <div className="quality-reasons" role="note">
                    <strong>Data-quality limitations</strong>
                    <ul>
                      {detail.feature_window.data_quality.reasons.map((reason) => (
                        <li key={reason}>{reason}</li>
                      ))}
                    </ul>
                  </div>
                ) : null}

                <div className="evidence-grid">
                  <section>
                    <h4>Why PRAHARI raised this</h4>
                    <ul>{detail.incident.prediction.evidence.map((item) => <li key={item}>{item}</li>)}</ul>
                  </section>
                  <section>
                    <h4>Likely location</h4>
                    <p><strong>{detail.incident.location.site_id}</strong><br />{detail.incident.location.rationale}</p>
                    <p className="quality-line">Data quality: <strong>{detail.incident.data_quality}</strong></p>
                  </section>
                </div>

                <section className="copilot-brief" aria-label="Local copilot incident brief">
                  <div className="copilot-brief-heading">
                    <div><span>LOCAL COPILOT</span><h4>Cited incident brief</h4></div>
                    <button type="button" disabled={!canOperate || Boolean(operation) || loading || !evidenceCurrent} onClick={() => void generateBrief()}>
                      {operation === "brief" ? "Generating locally..." : "Generate brief"}
                    </button>
                  </div>
                  {brief ? (
                    <div className="copilot-brief-body">
                      {!brief.evidence_current_at_completion || briefPredictionId !== selectedPredictionId ? (
                        <p role="status">This brief describes captured evidence from an earlier network state. Check the current incident before taking action.</p>
                      ) : null}
                      <small>Evidence window: <code>{briefPredictionId}</code></small>
                      <p><strong>{brief.summary}</strong></p>
                      <p>{brief.root_cause_hypothesis}</p>
                      <p>Impact: {brief.impact}</p>
                      <ul>{brief.citations.map((citation) => <li key={`${citation.source_id}-${citation.claim}`}><code>{citation.source_id}</code> — {citation.claim}</li>)}</ul>
                      <small>Confidence {percent(brief.confidence)} · Advisory only · {brief.limitations.join(" · ")}</small>
                    </div>
                  ) : <p className="copilot-brief-empty">The local model selects evidence-bound statements. The server checks their support and renders an advisory brief.</p>}
                </section>

                <div className="playbook-card">
                  <div><span>Recommended playbook</span><h4>{detail.playbook.title}</h4><p>{detail.playbook.safety_note}</p></div>
                  <span className="playbook-status">{detail.playbook.status.replaceAll("_", " ")}</span>
                </div>

                <form className="operator-form" onSubmit={(event) => void submit(event, "review")}>
                  <div className="operator-fields">
                    <label>Signed-in actor<input value={actorId} readOnly /></label>
                    <label>Review note<textarea value={note} maxLength={1000} required rows={3} placeholder="Record what you checked and why..." onChange={(event) => setNote(event.target.value)} /></label>
                  </div>
                  <div className="operator-actions">
                    <button type="submit" disabled={inputsBlocked || !note.trim()}>{operation === "review" ? "Saving..." : "Mark reviewed"}</button>
                    <button type="button" disabled={deviceInputsBlocked || !note.trim()} onClick={(event) => void submit(event, "dry-run")}>{operation === "dry-run" ? "Checking..." : "Run safety check"}</button>
                    <button className="approve-button" type="button" disabled={deviceInputsBlocked || !note.trim() || !canApprove} onClick={(event) => void submit(event, "approve")}>{operation === "approve" ? "Applying..." : "Approve & shift traffic"}</button>
                    <button type="button" disabled={deviceInputsBlocked || !note.trim()} onClick={(event) => void submit(event, "rollback")}>{operation === "rollback" ? "Restoring..." : "Restore primary path"}</button>
                  </div>
                  <p>{detail.device_dispatch_enabled ? "Review the current evidence, then run a safety check. Traffic-shift approval requires a successful check for this evidence and operator." : "Device changes are disabled for this deployment. Incident review and evidence capture remain available."}</p>
                </form>

                {result && resultPredictionId !== selectedPredictionId ? <p role="status">This action result belongs to an earlier evidence window. Review the current evidence before requesting another action.</p> : null}
                {result ? <p className={`action-result action-result--${result.status}`} role="status"><strong>{result.status.replaceAll("_", " ")}</strong> — {result.message}</p> : null}
                {actionError ? <p className="workbench-error" role="alert">{actionError}</p> : null}

                <details className="closure-details">
                  <summary>Close or escalate incident</summary>
                  {detail.workflow.closure ? <p>Closure review: <strong>{detail.workflow.closure_review?.decision ?? "awaiting independent review"}</strong>
                    {detail.workflow.closure_prediction_id !== selectedPredictionId ? " (earlier evidence window)" : ""}
                  </p> : null}
                  <form className="closure-form" onSubmit={(event) => void submitClosure(event)}>
                    <p>Record the observed outcome. Failed, partial, or uncertain results are escalated and never presented as recovery.</p>
                    <div className="closure-fields">
                      <label>
                        Outcome
                        <select value={closureOutcome} onChange={(event) => setClosureOutcome(event.target.value as ClosureOutcome)}>
                          <option value="resolved">Resolved and validated</option>
                          <option value="partially_resolved">Partially resolved</option>
                          <option value="failed">Mitigation failed</option>
                          <option value="false_positive">False positive</option>
                          <option value="uncertain">Uncertain — escalate</option>
                        </select>
                      </label>
                      <label>
                        Validation evidence
                        <textarea value={closureNote} maxLength={2000} required rows={4} placeholder="Describe application checks, before/after metrics, remaining symptoms, and any rollback..." onChange={(event) => setClosureNote(event.target.value)} />
                      </label>
                      {closureOutcome === "resolved" ? <label>
                        Intervention time {detail.fixture_only ? "(optional in replay)" : "(required)"}
                        <input type="datetime-local" value={closureIntervention}
                          required={!detail.fixture_only}
                          onChange={(event) => setClosureIntervention(event.target.value)} />
                      </label> : null}
                    </div>
                    <button type="submit" disabled={inputsBlocked || !closureNote.trim()
                      || (!detail.fixture_only && closureOutcome === "resolved" && !closureIntervention)}>{operation === "close" ? "Recording outcome..." : "Record outcome"}</button>
                  </form>
                </details>
                {closureMessage ? <p className="closure-message" role="status">{closureMessage}</p> : null}

                <details className="audit-details">
                  <summary>Audit trail ({detail.audit_trail.length})</summary>
                  <ol>{detail.audit_trail.map((event, index) => <li key={`${event.occurred_at}-${index}`}><strong>{event.event_type.replaceAll("_", " ")}</strong><span>{event.actor_id} · {new Date(event.occurred_at).toLocaleString()}</span><p>{event.detail}</p></li>)}</ol>
                </details>
              </>
            ) : null}
          </div>
        </div>
      ) : null}
    </section>
  );
}
