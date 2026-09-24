"""Print the evidence-backed incident produced from the bundled replay fixture."""

from __future__ import annotations

import json

from prahari.services.demo import DemoService


def main() -> None:
    service = DemoService()
    detail = service.incident_detail(service.incident_id)
    serialisable = {
        key: value.model_dump(mode="json") if hasattr(value, "model_dump") else value
        for key, value in detail.items()
    }
    serialisable["audit_trail"] = [
        event.model_dump(mode="json") for event in detail["audit_trail"]
    ]
    print(json.dumps(serialisable, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
