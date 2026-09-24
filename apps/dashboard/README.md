# PRAHARI dashboard

This is the local browser dashboard for the replay-backed PRAHARI API. It is deliberately an **operator interface**, not a network automation client: the current API is fixture-only and every action result is surfaced as an auditable review, dry-run, or block state.

## Run locally

Start the API from the repository root in one terminal:

```powershell
uv run uvicorn prahari.main:app --app-dir src --reload
```

Then start the dashboard here:

```powershell
cd apps/dashboard
npm install
npm run dev
```

Open `http://127.0.0.1:5173`. The Vite development server proxies `/api`, `/health`, and `/readyz` to `http://127.0.0.1:8000` by default.

## Configuration

For local development, no environment file is required: Vite proxies requests to the API on port
8000. Copy `.env.example` to `.env.local` only when configuring a different development proxy or
a separately deployed API.

- `VITE_API_BASE_URL` is the browser-visible API origin for a deployed dashboard. It should be the same origin where possible.
- `VITE_API_PROXY_TARGET` is development-server-only and does not ship in the static build.
- `VITE_DASHBOARD_REFRESH_MS` controls dashboard polling. It defaults to 5000 ms and is constrained to 1000-300000 ms.

The API must allow the dashboard origin through CORS if it is deployed on a different origin. Use TLS, an authenticated reverse proxy, and server-side identity propagation in a real deployment. The actor ID entered in this MVP UI is an audit field; it is **not authentication**.

## Commands

```powershell
npm run check
npm run build
npm run preview
```

## Safety behavior

- A persistent banner names fixture-only mode whenever `/health` or the loaded resources report it.
- The screen separates observations, predictions, playbooks, dry-runs, approvals, and confirmed recovery.
- "Review incident" records an operator acknowledgement.
- "Run dry-run" sends the current actor and records the returned validation/audit result.
- "Request approval" is intentionally labelled as expected to be blocked in this MVP. It cannot send device commands and renders the API response, including HTTP 409 blocks.
- No router credentials, executable CLI commands, or autonomous actions are stored in or emitted by this application.

## API surface used

`GET /health`, `GET /readyz`, `GET /api/topology`, `GET /api/incidents`, `GET /api/incidents/{id}`, `GET /api/models`, `POST /api/incidents/{id}/review`, `POST /api/incidents/{id}/actions/{playbookId}/dry-run`, and `POST /api/incidents/{id}/actions/{playbookId}/approve`.

### Optional two-server traffic-demo monitor

The dashboard also makes an optional `GET /api/demo/traffic` request. A current
PRAHARI API returns `200` with `status: "idle"` until it has received transfer
evidence; that is an explicit "no transfer measured yet" state, not a successful
transfer. A `404` from an older API is rendered as "monitor not connected" and
never appears as a successful transfer.

When that response has `can_run: true`, the dashboard also shows a bounded file
test control. It posts the selected file as `application/octet-stream` to
`POST /api/demo/traffic/run`; the receiver URL and credentials remain on the
server. The percentage shown while uploading measures browser-to-PRAHARI upload
progress only. The following sender-to-receiver and checksum phase is deliberately
indeterminate because the browser has no trustworthy byte progress for that hop.

When available, the endpoint should return JSON shaped like this:

```json
{
  "status": "running",
  "can_run": true,
  "max_file_bytes": 5242880,
  "updated_at": "2026-08-20T12:00:00Z",
  "source": { "id": "server:branch", "name": "Branch sender" },
  "destination": { "id": "server:dc", "name": "DC receiver" },
  "transport": "websocket",
  "payload": {
    "name": "demo-video.mp4",
    "kind": "video",
    "size_bytes": 52428800,
    "bytes_sent": 31457280,
    "bytes_received": 31457280,
    "integrity": "pending"
  },
  "latency_ms": { "min": 12.4, "p50": 17.8, "p95": 28.1, "max": 42.0 },
  "throughput_mbps": 18.2,
  "loss_percent": 0,
  "trace": [
    { "at": "2026-08-20T12:00:00Z", "stage": "receiver_connected", "status": "passed" },
    { "at": "2026-08-20T12:00:03Z", "stage": "payload_streaming", "status": "active" }
  ]
}
```

`status` can be `idle`, `running`, `passed`, `failed`, or `unavailable`. The response includes
`path_id`, so the UI labels the actual recorded primary or backup path rather than assuming one.
`latency_ms` represents application WebSocket chat round-trip time and `loss_percent` represents
application-message delivery loss; neither is raw IP packet loss. A real collector should only mark
a payload `integrity: "verified"` after comparing its received checksum with the sender checksum.

The run endpoint returns `{ run_id, transfer_id, evidence_saved, traffic }` and may additionally
return `downloadable`, `download_url`, and `download_expires_at`. The UI only labels a run verified
when the response is correlated to the selected file, the byte counts match exactly, the SHA-256
receipt is valid, and `evidence_saved` is true. A measured transfer failure is displayed as a test
result; an HTTP/network failure leaves the previous measured result unchanged.

The download control appears only for that correlated POST response when `downloadable` is exactly
`true` and the API supplies a valid, unexpired opaque URL. It fetches the receiver-side retained
copy with the required download-intent header, checks its byte length before saving, and uses the
safe filename from the correlated transfer rather than trusting `Content-Disposition`. An expired,
deleted, mismatched, or unavailable copy is reported inline and is never presented as downloaded.
The ordinary `GET /api/demo/traffic` response never implies that a file remains downloadable.
Browser redemption uses an empty-body `POST` to the returned URL; the receiver’s internal download
request is server-to-server and is not exposed to browser code.
