# MVP research and implementation decisions

## Status

This is the active build decision record for the first PRAHARI vertical slice. It
does not replace the canonical requirements in the rest of `docs/`; it freezes
only the defaults needed to begin implementation.

## Implemented starting point

- Pydantic contracts validate normalized telemetry, app probes, feature windows,
  risks, incidents, and audit records.
- A versioned replay fixture drives the same deterministic feature/prediction
  path that a future collector will use.
- The FastAPI slice exposes topology, metrics, incidents, review, action
  dry-run/approval, closure, and model metadata. Its action endpoints block
  rather than simulate a device change.
- The accompanying tests cover schema validation, data quality, monotonic risk,
  topology correlation, and the blocked-action workflow.

## What we are building first

- **Scenario:** gradual packet loss on `path:branch-01-to-dc:primary`.
- **Corroborating signal:** jitter on the same tunnel/path.
- **Supported labels:** `normal` and `tunnel_degradation` only.
- **Prediction cadence:** 5-second samples, evaluated in 60-second windows, with
  30/60/120-second risk horizons.
- **Provisional impact rule:** video-probe packet loss at or above 5%, or jitter
  at or above 30 ms, sustained for 15 seconds. This is an explicit bootstrap
  assumption in `config/mvp.json`, not an evaluated production threshold.

## Research-backed choices

1. **Topology as code:** Containerlab models a lab as a topology file with named
   nodes and links, so the lab, inventory, and path IDs can be versioned together.
   The real MPLS/FRR lab remains a Linux-host task. [Containerlab quickstart](https://containerlab.dev/quickstart/)
2. **Typed local API:** FastAPI/Pydantic response models give the implementation
   a single validated source for API shapes and generated OpenAPI documentation.
   [FastAPI response models](https://fastapi.tiangolo.com/tutorial/response-model/)
3. **Baseline before trained ML:** the first implementation is a transparent
   trend baseline. A LightGBM classifier will be introduced only after complete,
   run-level labelled data exists. Its risk probabilities must be calibrated on
   data independent from model fitting. [scikit-learn calibration guidance](https://scikit-learn.org/stable/modules/calibration.html)
4. **No automated change path:** the bundled backup-path runbook is still
   `draft`; the API deliberately blocks approval/execution until a reviewer and
   allow-listed real-lab adapter exist.

## Research questions that remain open

- Is the provisional impact rule visible to users while leaving enough lead time
  on the real lab path?
- Does the baseline achieve acceptable false-alert rate and useful lead time on
  untouched runs?
- Does a calibrated LightGBM model improve those results without leakage from
  run IDs, topology IDs, or the injection schedule?
- Does the topology heuristic place the injected root in the top three candidates
  when target site and path vary?

## Build sequence

1. Implement and test shared contracts, configuration, telemetry replay, feature
   extraction, trend prediction, topology correlation, and a safe API.
2. Deploy the actual Containerlab/FRR + traffic lab on Linux/WSL2; replace replay
   fixtures with collected records and manifests.
3. Measure the baseline on grouped run splits before adding LightGBM.
4. Add the reviewed action adapter, then local retrieval; add a local LLM only
   after the structured non-LLM path works.
