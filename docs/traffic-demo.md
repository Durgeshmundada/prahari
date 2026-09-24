# Two-server application traffic demo

This module turns a real, bounded application exchange into PRAHARI evidence. A
Branch probe sends chat messages and one binary file to a DC receiver over a
WebSocket. The receiver returns chat echoes and a byte-count/SHA-256 receipt.
The Branch probe verifies that receipt, calculates application delivery and
round-trip measurements, and posts normalized app-probe and event records to
the existing PRAHARI collector API.

It is a **local/demo workload**, not a network-control service. It never logs
chat content, persists uploaded content, executes a command, or changes a
network device.

## What the dashboard can show

The sender publishes to these existing collector endpoints:

- `POST /api/telemetry/app-probes` — `chat` and `file-upload` app probes;
- `POST /api/telemetry/events` — start and completed/degraded lifecycle events.

The dashboard reads its safe summary from `GET /api/demo/traffic` (optionally
with `?path_id=path:branch-01-to-dc:primary`). The file body and chat text are
never included in API or dashboard data.

The transfer record contains only:

- a generated transfer ID, safe filename, size, content kind/type, and byte
  counts;
- a SHA-256 checksum and the receiver's integrity acknowledgement;
- upload elapsed time and throughput; and
- chat-message measurements: attempted/received counts, RTT min/p50/p95/max,
  and jitter.

`packet_loss_ratio` means **application chat-echo message loss**, not IP packet
loss. RTT is an end-to-end WebSocket application round trip, not one-way or
router latency. Jitter is the mean absolute difference between consecutive
chat RTT samples. A completed upload means the DC receipt returned the exact
expected byte count and checksum; it does not prove a physical network path.

## Local demo on one machine

Install Python dependencies once:

```powershell
uv sync --all-groups
```

In terminal 1, start the PRAHARI API:

```powershell
uv run uvicorn prahari.main:app --app-dir src --reload
```

In terminal 2, start the DC receiver. It binds only to loopback by default:

```powershell
uv run python scripts/traffic_demo_receiver.py
```

In terminal 3, run the Branch probe. The default sends five chat messages and
a generated 128 KiB binary file every five seconds for 12 cycles (about one
minute), keeping live app-probe evidence fresh while the demo runs:

```powershell
uv run python scripts/traffic_demo_probe.py
```

Open the dashboard and use its traffic-demo card. A one-shot troubleshooting
run is available with `--repeat 1`; `--repeat 0` continues until `Ctrl+C` and
prints each completed run immediately without retaining an unbounded history.

## Two-laptop browser demo

Use this mode when a person on Laptop B should use a small chat/file page while
the presenter watches the PRAHARI dashboard on Laptop A. Laptop B does not need
Python, Node.js, the repository, or a collector credential. Its browser talks
only to the bounded user-demo service on Laptop A.

### One-click Windows launcher

For the normal presentation path, connect both laptops to the same phone
hotspot and double-click `start-two-laptop-demo.bat` in the repository root.
It detects Laptop A's active IPv4 address, creates a fresh access code, prepares
the database, starts the API and dashboard in separate windows, opens the
operator dashboard, and runs the Laptop B service in the main window. Copy the
displayed Laptop B URL and access code to Laptop B.

From Command Prompt, the same launcher is:

```bat
cd /d D:\hack2skill
start-two-laptop-demo.bat
```

If automatic address detection selects the wrong adapter, provide the exact
hotspot address shown by `ipconfig`:

```bat
start-two-laptop-demo.bat 10.149.135.1
```

For the first presentation, connect both laptops to your own phone hotspot,
travel router, or isolated lab switch. Do not use the college production Wi-Fi
until its administrator has approved the listener and firewall rule. This demo
does not modify a router, change routes, shape traffic, scan devices, or inject
a fault. It adds only normal browser traffic between the two laptops.

### 1. Prepare Laptop A once

From the repository root:

```powershell
uv sync --all-groups
uv run alembic upgrade head
Set-Location apps/dashboard
npm ci
Set-Location ../..
```

Find Laptop A's private IPv4 address after both laptops join the same hotspot:

```powershell
Get-NetIPAddress -AddressFamily IPv4 |
  Where-Object { $_.IPAddress -like "192.168.*" -or $_.IPAddress -like "10.*" -or $_.IPAddress -like "172.*" } |
  Select-Object InterfaceAlias, IPAddress
```

Choose the address belonging to that hotspot. The examples below use
`192.168.137.1`; replace it with the exact address displayed on Laptop A.

Create a fresh access code and keep it only in Laptop A's terminal:

```powershell
$env:PRAHARI_USER_DEMO_TOKEN = uv run python -c "import secrets; print(secrets.token_hex(24))"
$env:PRAHARI_USER_DEMO_TOKEN
```

Read the code to the Laptop B user or paste it directly into that laptop. Do
not put the code in source control, a screenshot, or the presentation deck.

### 2. Start Laptop A for the presentation

Open three PowerShell terminals in the repository root.

Terminal 1 starts the local collector API. Keep it on loopback; Laptop B never
needs direct access to it:

```powershell
uv run uvicorn prahari.main:app --app-dir src --host 127.0.0.1 --port 8000
```

Terminal 2 starts the operator dashboard:

```powershell
Set-Location apps/dashboard
npm run dev -- --host 127.0.0.1
```

If the terminal starts with a prompt such as `D:\hack2skill>` rather than
`PS D:\hack2skill>`, it is Windows Command Prompt. Use `cd` instead of the
PowerShell `Set-Location` command:

```bat
cd /d D:\hack2skill\apps\dashboard
npm run dev -- --host 127.0.0.1
```

Open `http://127.0.0.1:5173` on Laptop A.

Terminal 3 starts the page used by Laptop B. Use the same private address found
in step 1:

```powershell
$env:PRAHARI_USER_DEMO_TOKEN = "paste-the-code-created-in-step-1"
uv run python scripts/traffic_demo_user_service.py `
  --host 192.168.137.1 `
  --allow-non-loopback
```

The equivalent commands in Windows Command Prompt are:

```bat
cd /d D:\hack2skill
for /f %i in ('uv run python -c "import secrets; print(secrets.token_hex(24))"') do set PRAHARI_USER_DEMO_TOKEN=%i
echo %PRAHARI_USER_DEMO_TOKEN%
uv run python scripts\traffic_demo_user_service.py --host 192.168.137.1 --allow-non-loopback
```

The single `%i` form is for commands typed directly into Command Prompt. Use
`%%i` only if these lines are copied into a `.bat` file.

The listener refuses `0.0.0.0`, public IP addresses, hostnames, missing access
codes, and access codes shorter than 16 characters. The collector stays at
`127.0.0.1:8000` on Laptop A.

If Windows Firewall asks, allow Python only on **Private networks**. A stricter
administrator-managed rule should allow inbound TCP port `8780` only from
Laptop B's private IP. No router port forwarding is needed.

### 3. Use Laptop B

On Laptop B, open this URL in a modern browser, replacing the address:

```text
http://192.168.137.1:8780
```

Then:

1. Enter the access code from Laptop A and select **Use code**.
2. Send two or three harmless test messages. The page displays the real
   browser-to-service round-trip time.
3. Choose a small sample image, video, or document and select
   **Run transfer test**.
4. The service counts and hashes the bytes in memory, discards them, and sends
   only the measurement record to the collector on Laptop A.
5. On Laptop A, show the traffic card in the PRAHARI dashboard. It reports chat
   RTT/jitter, verified byte count, upload throughput, checksum integrity, and
   whether the complete evidence bundle was saved.

The connection check creates the first RTT sample. A later file transfer uses
up to the 20 most recent acknowledged chat samples. Refreshing the page clears
the access code from the browser because the page keeps it only in memory.

### 4. Show one safe error

Use one of these application-level failures; neither affects the network:

- enter an incorrect access code to show an immediate `401` rejection;
- select a file larger than the displayed 5 MiB limit to show a local limit
  error; or
- briefly stop Terminal 1, transfer a harmless small file, and show that the
  user service still verifies the transfer but clearly reports that dashboard
  evidence was not saved. Restart Terminal 1 before continuing.

Do not disconnect college equipment or run packet loss, bandwidth exhaustion,
ARP, DNS, routing, firewall, or denial-of-service experiments on a college
network. Use the project's isolated Containerlab environment when the
presentation specifically needs a gradual network fault and prediction.

### What this proves

This browser demo proves that two real laptops can exchange application data
and that PRAHARI can collect and display real service measurements without
storing user content. It monitors the service from Laptop A.

It does not by itself prove an outage prediction. Prediction needs time-series
router/interface telemetry from the same path and a model accepted on unseen
fault runs. Use the isolated network lab for that part of the presentation.

### Safe dashboard fault simulations

The files under `fault-simulations` exercise prediction and dashboard behavior
without changing Wi-Fi, routing, firewall rules, latency, or packet loss. Run
`01-healthy-network.bat` first, refresh the dashboard, then run one of the fault
files and refresh again. `06-recovery-to-healthy.bat` demonstrates recovery.

These files are labelled synthetic local simulations. They prove that PRAHARI
responds to controlled metric patterns; they are not measurements of the current
network and cannot be presented as a real live fault. Use
`check-fault-detection.bat` to print the current mode and result explicitly.

### Laptops on different internet connections

Do not expose port `8780` with router port forwarding. Put both laptops on a
private encrypted VPN that assigns Laptop A a literal `10.x`, `172.16-31.x`,
`192.168.x`, or ULA IPv6 address. Bind the user-demo service to that VPN address
with the same `--allow-non-loopback` command, and open that address from Laptop
B. The existing access code is still required. The VPN setup and its firewall
policy remain separate infrastructure and should be approved by whoever owns
the two networks.

### Development dashboard upload control

The browser-triggered control is disabled by default and is never registered
outside the `development` environment. Enable it on the API/Branch sender only
when the dashboard and receiver are ready:

```powershell
$env:PRAHARI_TRAFFIC_DEMO_RUN_ENABLED = "true"
$env:PRAHARI_TRAFFIC_DEMO_RECEIVER_URL = "ws://127.0.0.1:8765/ws/traffic"
uv run uvicorn prahari.main:app --app-dir src --reload
```

The dashboard sends one raw `application/octet-stream` body to
`POST /api/demo/traffic/run`, with a safe basename in
`X-Prahari-File-Name` and the explicit
`X-Prahari-Demo-Intent: run-traffic-test` header. In authentication-disabled
development, the request must also carry an `Origin` that exactly matches one
of `PRAHARI_CORS_ORIGINS`. Operator/admin authorization is required when API
key authentication is enabled.

The API accepts only one dashboard run at a time, streams and counts the body
against `PRAHARI_TRAFFIC_DEMO_MAX_FILE_BYTES` (5 MiB by default), and applies a
whole-run timeout. Receiver URLs and shared tokens are server configuration;
the request cannot select an outbound destination or filesystem path. Uploaded
bytes stay in memory and are discarded after the receiver verifies them.

The POST response contains `run_id`, `transfer_id`, `evidence_saved`, and a
`traffic` summary. `GET /api/demo/traffic` exposes `can_run` and
`max_file_bytes` so the dashboard can present the control accurately. A
verified transfer and successfully saved evidence are separate outcomes.

The normal local API starts in **fixture-only replay mode**. The traffic card
can still prove the chat/file exchange and its stored evidence, but that does
not turn the rest of PRAHARI into a live ready NOC or train its heuristic
predictor. A genuine live-mode demonstration also needs current, valid router
metric telemetry at the configured cadence; the traffic probe alone cannot
satisfy the model's feature-window readiness checks.

To test the receiver without publishing evidence, add `--no-publish`. Do not
use that option for a dashboard demonstration.

## Send a photo, video, or other sample file

Photo and video inputs are sent as a **bounded binary file**, not as a video
player or live stream. The receiver only hashes the bytes and returns a
receipt. It never stores or serves the media.

```powershell
uv run python scripts/traffic_demo_probe.py --file .\sample-photo.jpg --repeat 12
uv run python scripts/traffic_demo_probe.py --file .\short-demo-video.mp4 --repeat 12
```

The default upload cap is 5 MiB. It is configurable with `--max-file-mib`, but
the code enforces a hard 20 MiB demo cap. Binary frames default to 64 KiB. Chat
is capped at 100 messages/run and 4,096 characters/message. These limits make
the workload suitable for a controlled demo, not a general file-sharing
service.

## Two private lab servers

For two machines, run the receiver on a literal RFC1918/ULA private-LAN
address. The receiver refuses `0.0.0.0`, public addresses, and hostnames. It
also requires both explicit opt-in and a shared token of at least 16 characters.
Keep the token in an environment variable rather than a shell argument:

On the DC receiver machine:

```powershell
$env:PRAHARI_TRAFFIC_DEMO_TOKEN = "replace-with-a-long-random-private-token"
uv run python scripts/traffic_demo_receiver.py `
  --host 192.168.1.20 `
  --allow-non-loopback
```

On the Branch sender machine:

```powershell
$env:PRAHARI_TRAFFIC_DEMO_TOKEN = "replace-with-the-same-private-token"
uv run python scripts/traffic_demo_probe.py `
  --receiver-url ws://192.168.1.20:8765/ws/traffic `
  --allow-non-loopback-receiver `
  --allow-insecure-remote-ws
```

`ws://` sends the handshake token in plaintext. The final flag is intentionally
awkward because it is acceptable only for an isolated private lab/VLAN with
host firewalls. For any shared, routed, or production-like environment, put the
receiver behind a TLS reverse proxy or VPN and use an exact `wss://.../ws/traffic`
URL instead. This module is not an internet-facing upload endpoint.

If PRAHARI uses API-key collector authentication, put the collector token in an
environment variable; the CLI does not accept it as a command-line argument:

```powershell
$env:PRAHARI_COLLECTOR_API_KEY = "collector-api-token"
uv run python scripts/traffic_demo_probe.py --repeat 12
```

With an API key, a remote collector must use `https://` (and ideally managed
identity/mTLS in a real deployment). A plaintext `http://` private-LAN
collector requires the deliberately explicit
`--allow-insecure-private-collector-http` acknowledgement and is only for an
isolated lab. Never send a collector key over a public or shared HTTP network.

The sender uses `X-Actor-Role: collector` automatically in local development
when no API key is configured.

## Safety controls

- Receiver default: `127.0.0.1`; non-loopback requires a literal private-LAN
  address, an explicit flag, and a shared token.
- Receiver accepts eight active WebSocket sessions at most, rejects oversized
  frames before processing, requires `hello` within five seconds, and enforces
  upload progress and total-duration deadlines.
- Receiver WebSockets reject browser `Origin` headers; browsers must use the
  development API control rather than connecting to the receiver directly.
- One upload is active per session. Upload bytes are hashed incrementally in
  memory and are never written to arbitrary paths or retained after the receipt.
- The sender checks receiver byte count, completion state, integrity state, and
  SHA-256 against its own payload before reporting a verified upload.
- The sender never calls router, shell, traffic-shaping, or device APIs.
- A five-second repeat cadence is intentional: live PRAHARI considers old app
  probes stale after roughly two sampling intervals.

## Useful commands

```powershell
# See every option without exposing any secrets.
uv run python scripts/traffic_demo_receiver.py --help
uv run python scripts/traffic_demo_probe.py --help

# Run source tests, including a real loopback WebSocket smoke test.
uv run pytest -q tests/test_traffic_demo.py
```

The traffic demo is evidence for a lab presentation. It is not evidence that a
predictive model is calibrated, that a network is production-ready, or that a
safe failover action has been validated.
