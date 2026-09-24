# Local development

## Bootstrap slice

The API runs locally on Windows with Python through `uv`. It replays a
checked-in telemetry fixture by default and never contacts a network device.

```powershell
uv sync --all-groups
uv run ruff check .
uv run pytest
uv run python scripts/replay_demo.py
uv run uvicorn prahari.main:app --app-dir src --reload
```

The FastAPI documentation is available at `http://127.0.0.1:8000/docs` while
the server is running.

The optional React dashboard needs Node.js 22 or newer:

```powershell
Set-Location apps/dashboard
npm ci
npm run dev
```

It opens at `http://127.0.0.1:5173` and proxies the API locally.

## What the bootstrap proves

- normalized metric records validate before feature generation;
- a 60-second replay window creates deterministic slope/EWMA/robust-z features;
- bad sample coverage and stale live data withhold prediction;
- risk horizons are constrained to be monotonic;
- topology produces ranked candidates and an affected scope; and
- action approval is blocked when the playbook is draft or no real-lab adapter
  exists.

## What still needs a Linux host

The checked-in [network-lab scaffold](../network-lab/README.md) contains a
Containerlab/FRRouting topology and guarded `tc netem` scripts, but it has not
been deployed on this Windows host. Real Containerlab/FRRouting/MPLS lab
validation, fault injection, live collection, real traffic probes, and a
controlled backup-path action require a Linux-capable host with Docker/container
support.

Do not treat the replay fixture as a measured lab run or a trained-model result.
