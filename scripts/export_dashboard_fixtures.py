"""Generate dashboard contract fixtures from the actual replay API, without a server."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from prahari.api import app_for_root
from prahari.copilot.grounding import BriefSelection
from prahari.copilot.service import IncidentCopilot
from prahari.services.demo import DemoService


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    endpoints = {
        "health": "/health",
        "readiness": "/readyz",
        "topology": "/api/topology",
        "actions": "/api/action-operations",
        "incidents": "/api/incidents",
        "detail": f"/api/incidents/{DemoService.incident_id}",
        "models": "/api/models",
        "metrics": "/api/metrics?limit=2",
        "traffic": "/api/demo/traffic",
    }
    with TestClient(app_for_root(root)) as client:
        fixtures = {}
        for name, endpoint in endpoints.items():
            response = client.get(endpoint)
            response.raise_for_status()
            fixtures[name] = response.json()
        class FixtureSelector:
            def structured_chat(self, prompt, model, *, json_schema):
                properties = json_schema["properties"]
                return BriefSelection(
                    incident_id=properties["incident_id"]["const"],
                    incident_sha256=properties["incident_sha256"]["const"],
                    situation_claim_ids=properties["situation_claim_ids"]["items"]["enum"][:1],
                    recommended_playbook_id=None,
                )

        # A contract fixture uses the real renderer with a deterministic selector.
        # It is not a local-model evaluation or device recovery record.
        fixtures["brief"] = IncidentCopilot(root, FixtureSelector(), allowed_playbooks=set()).brief(
            client.app.state.service.incident()
        ).model_dump(mode="json")
    target = root / "apps/dashboard/src/__fixtures__/replay.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(fixtures, indent=2) + "\n", encoding="utf-8")
    print(f"Generated {len(fixtures)} replay API fixtures: {target}")


if __name__ == "__main__":
    main()
