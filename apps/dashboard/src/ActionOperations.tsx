import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { api } from "./api";
import type { ActionOperation, ActorIdentity } from "./types";

export function ActionOperations({ actor, refreshToken, onChanged }: {
  actor: ActorIdentity;
  refreshToken: string | null;
  onChanged: () => Promise<void>;
}) {
  const [operations, setOperations] = useState<ActionOperation[]>([]);
  const [dispatchEnabled, setDispatchEnabled] = useState(true);
  const [hasLoaded, setHasLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    let active = true;
    void api.actionOperations().then((value) => {
      if (active) { setOperations(value.operations); setDispatchEnabled(value.dispatch_enabled); setHasLoaded(true); setError(null); }
    }).catch((failure: unknown) => {
      if (active) setError(failure instanceof Error ? failure.message : "Action records unavailable.");
    });
    return () => { active = false; };
  }, [refreshToken, refresh]);
  useEffect(() => {
    const timer = window.setInterval(() => setRefresh((value) => value + 1), 10000);
    return () => window.clearInterval(timer);
  }, []);
  const unresolved = operations.filter((item) => item.status !== "finished");
  return (
    <section className="action-operations" aria-label="Durable action records">
      <h2>Action records</h2>
      {error ? <p role="alert">{error} Previously loaded records may be stale.</p> : null}
      {unresolved.length ? <p className="workbench-error" role="alert">
        {unresolved.length} unconfirmed {unresolved.length === 1 ? "action" : "actions"}. Further dispatch and incident closure are blocked.
        Verify route state independently; a timeout does not establish that no change occurred.
      </p> : error ? <p>Current action status is unavailable.</p> : hasLoaded
        ? <p>No unconfirmed actions in the loaded records.</p> : <p>Loading action records...</p>}
      <button type="button" onClick={() => setRefresh((value) => value + 1)}>Refresh action records</button>
      {unresolved.length && dispatchEnabled ? <p>Disable device dispatch and restart in monitoring mode before recording reconciliation.</p> : null}
      {operations.map((item) => <details key={item.id} open={item.status !== "finished"}>
        <summary>{item.kind === "apply" ? "Traffic shift" : "Rollback"}: {item.status === "finished"
          ? item.reconciliation ? "state reconciled" : item.result_status : "outcome unconfirmed"}</summary>
        <p><code>{item.id}</code> · Requested by {item.actor_id}</p>
        <p>{item.message ?? "Dispatch intent recorded; no outcome has been confirmed."}</p>
        <p>Request note: {item.note}</p>
        {item.agent_receipt ? <p>Original adapter reply: {item.agent_receipt.status.replaceAll("_", " ")}.
          Route reported: {item.agent_receipt.observed_route} at {new Date(item.agent_receipt.checked_at).toLocaleString()}.</p> : null}
        <p>Evidence: <code>{item.prediction_id}</code></p>
        {item.reconciliation ? <p>Observed {item.observed_route} route verified by {item.reconciliation.actor_id}.
          This records route state; it does not establish application recovery.</p> : null}
        {item.status !== "finished" && (actor.role === "reviewer" || actor.role === "admin") ?
          <ReconciliationForm key={`${item.id}-${item.updated_at}`} operation={item} actor={actor}
            disabled={Boolean(error) || dispatchEnabled} onSaved={async () => {
              setRefresh((value) => value + 1); await onChanged();
            }} /> : null}
      </details>)}
    </section>
  );
}

function ReconciliationForm({ operation, actor, disabled, onSaved }: {
  operation: ActionOperation; actor: ActorIdentity; disabled: boolean; onSaved: () => Promise<void>;
}) {
  const [route, setRoute] = useState<"primary" | "backup">("primary");
  const [reference, setReference] = useState("");
  const [checksum, setChecksum] = useState("");
  const [note, setNote] = useState("");
  const [quiesced, setQuiesced] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const canSave = !disabled && !busy && actor.actor_id !== operation.actor_id
    && quiesced && reference.trim().length >= 3 && /^[0-9a-f]{64}$/.test(checksum)
    && checksum !== "0".repeat(64) && note.trim().length >= 20;
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!canSave) return;
    setBusy(true); setError(null);
    try {
      await api.reconcileAction(operation.id, {
        actor_id: actor.actor_id, observed_route: route, evidence_reference: reference.trim(),
        evidence_sha256: checksum, dispatch_quiesced: true, note: note.trim()
      });
      await onSaved();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Reconciliation failed.");
    } finally { setBusy(false); }
  }
  return <form className="closure-form" onSubmit={(event) => void submit(event)}>
    <p>Independent reconciliation requires stopping all dispatch workers and the device agent,
      then verifying the actual route. This form records your attestation and evidence.</p>
    {actor.actor_id === operation.actor_id ? <p role="alert">The requesting operator cannot reconcile their own action.</p> : null}
    <label>Verified route<select value={route} onChange={(event) => setRoute(event.target.value as "primary" | "backup")}>
      <option value="primary">Primary</option><option value="backup">Backup</option>
    </select></label>
    <label>Evidence reference<input required maxLength={1000} value={reference} onChange={(event) => setReference(event.target.value)} /></label>
    <label>Evidence SHA-256<input required maxLength={64} value={checksum} onChange={(event) => setChecksum(event.target.value)} /></label>
    <label>Verification note<textarea required minLength={20} maxLength={2000} value={note} onChange={(event) => setNote(event.target.value)} /></label>
    <label><input type="checkbox" checked={quiesced} onChange={(event) => setQuiesced(event.target.checked)} />
      All dispatch workers and the device agent are stopped; route state was independently verified.
    </label>
    <button type="submit" disabled={!canSave}>{busy ? "Recording..." : "Record verified state"}</button>
    {error ? <p role="alert">{error}</p> : null}
  </form>;
}
