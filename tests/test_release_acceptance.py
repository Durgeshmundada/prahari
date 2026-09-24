from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest

from prahari.release_acceptance import BASELINE_REQUIREMENTS, ReleaseAcceptance, validate_acceptance


@pytest.fixture
def acceptance(tmp_path):
    content = b"Test fixture evidence only, not a customer acceptance record."
    (tmp_path / "test-only.txt").write_bytes(content)
    now = datetime.now(UTC)
    report = ReleaseAcceptance(
        deployment_mode="monitoring_only",
        release_sha256="e" * 64,
        customer_requirement_references=["test-only-requirements"],
        prepared_by="test-preparer",
        approved_by="test-reviewer",
        approval_reference="unit-test-fixture",
        approved_at=now - timedelta(days=1),
        expires_at=now + timedelta(days=1),
        requirements=[
            {
                "requirement_id": requirement,
                "status": "passed",
                "note": "Synthetic evidence used to verify validator behavior only.",
                "evidence": [{"path": "test-only.txt", "sha256": sha256(content).hexdigest()}],
            }
            for requirement in sorted(BASELINE_REQUIREMENTS)
        ],
    )
    return report, tmp_path


def test_complete_release_bound_evidence_validates(acceptance):
    report, root = acceptance
    validate_acceptance(report, root, release_sha256="e" * 64)


@pytest.mark.parametrize("change", ["release", "expiry", "pending", "artifact"])
def test_acceptance_rejects_stale_incomplete_or_changed_evidence(acceptance, change):
    report, root = acceptance
    if change == "expiry":
        report.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    if change == "pending":
        report.requirements[0].status = "pending"
    if change == "artifact":
        (root / "test-only.txt").write_text("Changed after review.")
    with pytest.raises(ValueError):
        validate_acceptance(report, root, release_sha256=("f" if change == "release" else "e") * 64)


@pytest.mark.parametrize("change", ["independence", "coverage", "traversal", "placeholder"])
def test_acceptance_contract_rejects_unsafe_or_incomplete_records(acceptance, change):
    report, _ = acceptance
    value = report.model_dump(mode="json")
    if change == "independence":
        value["approved_by"] = " TEST-PREPARER "
    if change == "coverage":
        value["requirements"][-1]["requirement_id"] = value["requirements"][0]["requirement_id"]
    if change == "traversal":
        value["requirements"][0]["evidence"][0]["path"] = "../outside.txt"
    if change == "placeholder":
        value["requirements"][0]["evidence"][0]["sha256"] = "0" * 64
    with pytest.raises(ValueError):
        ReleaseAcceptance.model_validate(value)
