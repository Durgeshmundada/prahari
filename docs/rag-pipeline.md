# Local retrieval and evidence-bound copilot

The predictor estimates network risk. Retrieval provides registered local
procedures and compatible, independently reviewed historical outcomes. The
language model selects statements; the server renders the explanation.

## Implemented flow

1. Capture the typed incident and compute its canonical SHA-256. Optional client
   prediction preconditions reject a request for evidence already superseded.
2. Construct a statement catalog from typed current state, risk status,
   probabilities, quality, confidence and configured scope. Free-form incident
   summaries, evidence prose and operator notes cannot establish a claim.
3. Retrieve exact checksum-bound catalog documents and at most two compatible
   approved cases. Case applicability includes entity/type/model/scope and
   profile/model-bundle hashes. Pending, rejected and incompatible cases stay out.
4. Historical statements describe only the typed approved outcome. Runbook
   statements identify the registered procedure as advisory. Neither establishes
   current root cause, recovery, applicability beyond the supported scope or
   authority to run a command.
5. Constrain the local model's output to incident identity/hash, statement IDs
   from the supplied catalog and an eligible registered playbook ID or null.
   Arbitrary narrative, fabricated citations and model-written confidence fail.
6. Validate selection, support paths, content-derived IDs and source hashes.
   Render exact server statements and citations, retaining mandatory quality,
   heuristic-versus-calibrated and unconfirmed-root-cause limitations.
7. Return the immutable incident snapshot with the brief. Live collection can
   advance during generation; the server annotates supersession and the UI shows
   the captured prediction ID and an earlier-state warning.

## Knowledge provenance

External deployment profiles register exact relative document paths, byte
checksums, reviewer identities and approval references. Traversal, symlinks,
missing or changed documents fail validation. Retrieval is a bounded local
keyword implementation; vector retrieval remains future work.

Case notes remain stored for human reviewers but are excluded from model context.
Approval of a case reporting failure or uncertainty preserves that outcome.
Similarity is historical context; it cannot prove a present diagnosis or authorize
a device operation. Catalog checks enforce declared provenance, not reviewer
authority, signatures or the factual truth of arbitrary procedural prose.

## Failure and operating behaviour

The local Ollama client disables thinking for this selection task, constrains
generation/context sizes, bounds prompt and streamed response bytes and allows
two bounded attempts. Invalid selections, missing support and unavailable models
produce a visible unavailable response. The structured predictor remains usable;
there is no invented language-model fallback.

Evidence selection does not add forecasting ability to the experimental
degradation classifier or heuristic baseline. Production forecasting still
requires measured training/held-out data, calibration, target-scope validation
and independent approval. Device operations retain their separate current-review,
dry-run, durable-intent and reconciliation guards.

The response fields and live supersession behaviour are documented in
[api-and-ui-contract.md](api-and-ui-contract.md). The next evaluation environment
is the [owned server pilot](owned-pilot.md).
