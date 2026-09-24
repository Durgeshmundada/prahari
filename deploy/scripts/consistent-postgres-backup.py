#!/usr/bin/env python3
"""Dump and count the same exported PostgreSQL snapshot while ingestion continues."""

from __future__ import annotations

import argparse
import json
import queue
import re
import subprocess
import tempfile
import threading
from pathlib import Path

TABLES = (
    "alembic_version",
    "telemetry_records",
    "app_probe_records",
    "event_records",
    "incident_states",
    "closure_cases",
    "action_operations",
    "browser_sessions",
    "login_windows",
    "audit_events",
)
IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]{0,62}")
SNAPSHOT = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{8}-[0-9]+")


def consistent_backup(
    prefix: list[str], user: str, database: str, output: Path, timeout_seconds: int = 600
) -> dict:
    if not IDENTIFIER.fullmatch(user) or not IDENTIFIER.fullmatch(database):
        raise ValueError("invalid PostgreSQL user or database identifier")
    if not prefix or any(not isinstance(item, str) or not item for item in prefix):
        raise ValueError("database command prefix must be a nonempty argument list")
    if not 30 <= timeout_seconds <= 7200:
        raise ValueError("backup deadline must be 30..7200 seconds")
    if output.exists() or output.is_symlink():
        raise ValueError("backup archive must be a new file")
    responses: queue.Queue[str] = queue.Queue(maxsize=8)
    with tempfile.TemporaryFile(mode="w+") as errors:
        session = subprocess.Popen(
            [
                *prefix,
                "psql",
                "-X",
                "--quiet",
                "--no-align",
                "--tuples-only",
                "--set",
                "ON_ERROR_STOP=1",
                "--username",
                user,
                "--dbname",
                database,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=errors,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        assert session.stdin is not None and session.stdout is not None

        def receive() -> None:
            while True:
                line = session.stdout.readline(65537)
                responses.put(line)
                if not line:
                    return

        reader = threading.Thread(target=receive, daemon=True)
        reader.start()

        def query(sql: str) -> str:
            session.stdin.write(sql + "\n")
            session.stdin.flush()
            try:
                line = responses.get(timeout=60)
            except queue.Empty as error:
                raise TimeoutError("PostgreSQL snapshot query exceeded its deadline") from error
            if not line or len(line) > 65536:
                raise RuntimeError("PostgreSQL snapshot session returned invalid output")
            return line.strip()

        try:
            snapshot = query(
                "SET statement_timeout = '60s'; "
                f"SET idle_in_transaction_session_timeout = '{timeout_seconds + 120}s'; "
                "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY; SELECT pg_export_snapshot();"
            )
            if not SNAPSHOT.fullmatch(snapshot):
                raise RuntimeError("PostgreSQL returned an invalid exported snapshot")
            counts = ",".join(f"'{name}',(SELECT count(*) FROM {name})" for name in TABLES)
            metadata = json.loads(
                query(
                    "SELECT json_build_object('alembic_revision',"
                    "(SELECT version_num FROM alembic_version),'table_counts',"
                    f"json_build_object({counts}))::text;"
                )
            )
            if not isinstance(metadata, dict) or set(metadata) != {
                "alembic_revision",
                "table_counts",
            }:
                raise RuntimeError("invalid snapshot metadata")
            if (
                not isinstance(metadata["alembic_revision"], str)
                or not metadata["alembic_revision"]
            ):
                raise RuntimeError("missing schema revision in snapshot")
            if not isinstance(metadata["table_counts"], dict) or set(
                metadata["table_counts"]
            ) != set(TABLES):
                raise RuntimeError("incomplete table counts in snapshot")
            if any(
                type(count) is not int or count < 0 for count in metadata["table_counts"].values()
            ):
                raise RuntimeError("invalid table count in snapshot")
            with output.open("xb") as archive:
                subprocess.run(
                    [
                        *prefix,
                        "pg_dump",
                        "--username",
                        user,
                        "--dbname",
                        database,
                        "--format",
                        "custom",
                        "--compress",
                        "6",
                        "--no-owner",
                        "--no-acl",
                        "--snapshot",
                        snapshot,
                    ],
                    stdout=archive,
                    stderr=errors,
                    stdin=subprocess.DEVNULL,
                    check=True,
                    timeout=timeout_seconds,
                )
            session.stdin.write("COMMIT;\n\\q\n")
            session.stdin.flush()
            session.stdin.close()
            if session.wait(timeout=15) != 0:
                raise RuntimeError("PostgreSQL snapshot session failed to commit")
            with output.open("rb") as archive:
                if archive.read(5) != b"PGDMP":
                    raise RuntimeError("backup is not a PostgreSQL custom archive")
            return {
                **metadata,
                "snapshot": snapshot,
                "consistency": "exported-repeatable-read-snapshot",
            }
        finally:
            if session.poll() is None:
                session.terminate()
                try:
                    session.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    session.kill()
                    session.wait(timeout=5)
            if not session.stdin.closed:
                session.stdin.close()
            reader.join(timeout=5)
            session.stdout.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument("command_prefix", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    prefix = args.command_prefix
    if prefix[:1] == ["--"]:
        prefix = prefix[1:]
    print(
        json.dumps(
            consistent_backup(prefix, args.user, args.database, args.output, args.timeout_seconds),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
