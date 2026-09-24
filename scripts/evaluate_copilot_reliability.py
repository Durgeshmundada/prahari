"""Measure schema and grounding reliability of the live local copilot endpoint."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import math
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse

import httpx

_API_TOKEN: str | None = None


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = math.ceil(quantile * len(ordered)) - 1
    return ordered[max(0, min(index, len(ordered) - 1))]


def validate_origin(api_url: str, allowed_private_ip: str | None) -> str:
    parsed = urlparse(api_url)
    if (
        parsed.scheme != "http"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("API URL must be a plain local HTTP origin")
    try:
        address = ipaddress.ip_address(parsed.hostname or "")
    except ValueError as error:
        raise ValueError("API URL must use an explicit IP address") from error
    private_allowed = allowed_private_ip is not None and str(address) == allowed_private_ip
    if not address.is_loopback and not (address.is_private and private_allowed):
        raise ValueError("API URL must use loopback or the exact allowed private IP")
    return api_url.rstrip("/")


def fetch_json(url: str, *, method: str, timeout_seconds: float) -> tuple[dict[str, Any], bytes]:
    import httpx

    headers = {"Accept": "application/json"}
    if _API_TOKEN:
        headers["Authorization"] = "Bearer " + _API_TOKEN
    with httpx.Client(timeout=timeout_seconds, trust_env=False, follow_redirects=False) as client:
        response = client.request(method, url, headers=headers)
        response.raise_for_status()
        body = response.content
    value = json.loads(body)
    if not isinstance(value, (dict, list)):
        raise ValueError("response must be a JSON object or array")
    return value, body


def validate_brief(value: dict[str, Any], incident, extra_supported=()) -> tuple[bool, bool]:
    """Reconstruct claim support from trusted evidence, never trust self-reported hashes."""
    from prahari.contracts import IncidentCard
    from prahari.copilot.grounding import evidence_statements, snapshot_digest
    from prahari.copilot.service import IncidentBrief

    card = incident if isinstance(incident, IncidentCard) else IncidentCard.model_validate(incident)
    brief = IncidentBrief.model_validate(value)
    if brief.incident_id != card.id or brief.grounding.incident_sha256 != snapshot_digest(
        card.model_dump(mode="json")
    ):
        raise ValueError("brief is not bound to the evaluated incident snapshot")
    if brief.confidence != card.prediction.confidence:
        raise ValueError("brief confidence does not match measured evidence")
    if brief.recommended_playbook_id is not None and (
        card.playbook_status != "available_pending_review"
        or card.prediction.likely_incident_type == "normal"
        or card.prediction.affected_scope.backup_path_id is None
        or card.data_quality != "good"
        or card.prediction.data_quality != "good"
    ):
        raise ValueError("playbook is not eligible for this incident")
    catalog = {claim.claim_id: claim for claim in [*evidence_statements(card), *extra_supported]}
    for claim in brief.grounding.claims:
        if catalog.get(claim.claim_id) != claim:
            raise ValueError("claim is not grounded in the evaluated evidence and approved corpus")
    return True, True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--incident-id", required=True)
    parser.add_argument("--knowledge-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--network-profile", type=Path)
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--api-url", default="http://127.0.0.1:8080")
    parser.add_argument("--allowed-private-ip")
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument("--minimum-success-rate", type=float, default=1.0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.iterations <= 100:
        parser.error("--iterations must be between 1 and 100")
    if not 5 <= args.timeout_seconds <= 300:
        parser.error("--timeout-seconds must be between 5 and 300")
    if not 0 <= args.minimum_success_rate <= 1:
        parser.error("--minimum-success-rate must be between zero and one")
    api_url = validate_origin(args.api_url, args.allowed_private_ip)
    from prahari.contracts import ClosureCase, IncidentCard
    from prahari.copilot.grounding import historical_statement, runbook_statement
    from prahari.copilot.retrieval import LocalRunbookIndex, approved_case_context
    from prahari.network_profile import load_network_profile

    checksums = None
    if args.network_profile:
        profile = load_network_profile(args.network_profile)
        checksums = {book.path: book.sha256 for book in profile.approved_runbooks}
    index = LocalRunbookIndex(args.knowledge_root, document_checksums=checksums)
    global _API_TOKEN
    _API_TOKEN = args.token_file.read_text(encoding="utf-8").strip() if args.token_file else None
    started_at = utc_now()
    model_info, _ = fetch_json(
        f"{api_url}/api/models", method="GET", timeout_seconds=args.timeout_seconds
    )
    calls: list[dict[str, Any]] = []
    for number in range(1, args.iterations + 1):
        call_started_at = utc_now()
        started = time.monotonic()
        result: dict[str, Any] = {
            "iteration": number,
            "started_at": call_started_at,
            "http_status": None,
            "schema_valid": False,
            "grounding_valid": False,
            "response_sha256": None,
            "error": None,
        }
        try:
            brief, body = fetch_json(
                f"{api_url}/api/incidents/{args.incident_id}/brief",
                method="POST",
                timeout_seconds=args.timeout_seconds,
            )
            detail, _ = fetch_json(
                f"{api_url}/api/incidents/{args.incident_id}",
                method="GET",
                timeout_seconds=args.timeout_seconds,
            )
            card = IncidentCard.model_validate(brief["grounding"]["incident_snapshot"])
            query = " ".join(
                [card.summary, card.prediction.likely_incident_type, *card.prediction.evidence]
            )
            chunks = index.search(query)
            extra = [
                runbook_statement(chunk, detail["playbook"]["id"])
                for chunk in chunks
                if chunk.source_path == detail["playbook"]["source"]
            ]
            cases, _ = fetch_json(
                f"{api_url}/api/cases?status=approved&limit=100",
                method="GET",
                timeout_seconds=args.timeout_seconds,
            )
            extra.extend(
                historical_statement(chunk)
                for chunk in approved_case_context(
                    card, [ClosureCase.model_validate(case) for case in cases]
                )
            )
            schema_valid, grounding_valid = validate_brief(brief, card, extra)
            result.update(
                {
                    "http_status": 200,
                    "schema_valid": schema_valid,
                    "grounding_valid": grounding_valid,
                    "response_sha256": hashlib.sha256(body).hexdigest(),
                    "recommended_playbook_id": brief["recommended_playbook_id"],
                    "citation_ids": [item["source_id"] for item in brief["citations"]],
                }
            )
        except HTTPError as error:
            result["http_status"] = error.code
            result["error"] = f"HTTP {error.code}: {error.read(1000).decode(errors='replace')}"
        except (OSError, URLError, ValueError, httpx.HTTPError) as error:
            result["error"] = f"{type(error).__name__}: {error}"
        result["duration_seconds"] = round(time.monotonic() - started, 3)
        calls.append(result)
        print(
            f"[{number:02}/{args.iterations}] "
            f"schema={result['schema_valid']} grounding={result['grounding_valid']} "
            f"duration={result['duration_seconds']:.3f}s",
            flush=True,
        )
    successes = [call for call in calls if call["schema_valid"] and call["grounding_valid"]]
    durations = [float(call["duration_seconds"]) for call in calls]
    success_rate = len(successes) / len(calls)
    report = {
        "schema_version": "copilot-reliability-report-v1",
        "started_at": started_at,
        "completed_at": utc_now(),
        "api_origin": api_url,
        "incident_id": args.incident_id,
        "iterations": len(calls),
        "successful_calls": len(successes),
        "success_rate": success_rate,
        "required_success_rate": args.minimum_success_rate,
        "passed": success_rate >= args.minimum_success_rate,
        "latency_seconds": {
            "minimum": min(durations),
            "median": statistics.median(durations),
            "p95": percentile(durations, 0.95),
            "maximum": max(durations),
        },
        "model_info": model_info,
        "calls": calls,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(args.output)
    summary_fields = (
        "iterations",
        "successful_calls",
        "success_rate",
        "passed",
        "latency_seconds",
    )
    print(json.dumps({key: report[key] for key in summary_fields}, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
