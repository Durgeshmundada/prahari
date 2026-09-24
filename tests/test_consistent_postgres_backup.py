import importlib.util
import io
import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

spec = importlib.util.spec_from_file_location(
    "consistent_backup", Path(__file__).parents[1] / "deploy/scripts/consistent-postgres-backup.py"
)
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


def session_fixture(snapshot="00000003-0000001B-1", invalid_counts=False):
    counts = dict.fromkeys(backup.TABLES, 2)
    if invalid_counts:
        counts["audit_events"] = True
    metadata = {"alembic_revision": "synthetic-test-revision", "table_counts": counts}
    session = Mock()
    session.stdout = io.StringIO(snapshot + "\n" + json.dumps(metadata) + "\n")
    session.stdin.closed = False
    session.poll.return_value = None
    session.wait.return_value = 0
    return session, metadata


def test_counts_and_dump_share_snapshot_before_transaction_commit(tmp_path, monkeypatch):
    session, metadata = session_fixture()
    monkeypatch.setattr(backup.subprocess, "Popen", Mock(return_value=session))

    def dump(command, **kwargs):
        assert command[command.index("--snapshot") + 1] == "00000003-0000001B-1"
        assert "--file" not in command
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert not any("COMMIT" in call.args[0] for call in session.stdin.write.call_args_list)
        kwargs["stdout"].write(b"PGDMPsynthetic-unit-test-archive")
        return Mock(returncode=0)

    monkeypatch.setattr(backup.subprocess, "run", dump)
    result = backup.consistent_backup(
        ["synthetic-client"], "test_user", "test_db", tmp_path / "dump"
    )
    assert result["table_counts"] == metadata["table_counts"]
    assert result["consistency"] == "exported-repeatable-read-snapshot"
    assert any(
        "REPEATABLE READ READ ONLY" in call.args[0] for call in session.stdin.write.call_args_list
    )
    assert any("COMMIT" in call.args[0] for call in session.stdin.write.call_args_list)


@pytest.mark.parametrize("invalid_snapshot,invalid_counts", [(True, False), (False, True)])
def test_invalid_snapshot_or_counts_never_start_dump(
    tmp_path, monkeypatch, invalid_snapshot, invalid_counts
):
    session, _ = session_fixture(
        "bad-snapshot" if invalid_snapshot else "00000003-0000001B-1", invalid_counts
    )
    monkeypatch.setattr(backup.subprocess, "Popen", Mock(return_value=session))
    dump = Mock()
    monkeypatch.setattr(backup.subprocess, "run", dump)
    with pytest.raises(RuntimeError):
        backup.consistent_backup(["synthetic-client"], "test_user", "test_db", tmp_path / "dump")
    dump.assert_not_called()
    session.terminate.assert_called_once()


def test_dump_timeout_closes_exporting_transaction(tmp_path, monkeypatch):
    session, _ = session_fixture()
    monkeypatch.setattr(backup.subprocess, "Popen", Mock(return_value=session))
    monkeypatch.setattr(
        backup.subprocess, "run", Mock(side_effect=subprocess.TimeoutExpired("test", 30))
    )
    with pytest.raises(subprocess.TimeoutExpired):
        backup.consistent_backup(
            ["synthetic-client"], "test_user", "test_db", tmp_path / "dump", 30
        )
    session.terminate.assert_called_once()
    assert not any("COMMIT" in call.args[0] for call in session.stdin.write.call_args_list)


def test_existing_archive_cannot_be_overwritten(tmp_path):
    archive = tmp_path / "dump"
    archive.write_bytes(b"preserve-existing-backup")
    with pytest.raises(ValueError, match="new file"):
        backup.consistent_backup(["synthetic-client"], "test_user", "test_db", archive)
    assert archive.read_bytes() == b"preserve-existing-backup"
