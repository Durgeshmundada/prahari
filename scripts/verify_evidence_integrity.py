"""Read and verify every content-addressed PRAHARI evidence row."""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from prahari.persistence import PersistenceStore


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url-file",
        type=Path,
        default=os.environ.get("PRAHARI_DATABASE_URL_FILE"),
    )
    parser.add_argument("--batch-size", type=int, default=1000)
    args = parser.parse_args()
    if args.database_url_file is None or not 1 <= args.batch_size <= 10_000:
        parser.error("supply a protected database URL file and batch size of 1-10000")

    store = None
    try:
        database_url = args.database_url_file.read_text(encoding="utf-8").strip()
        if not database_url:
            raise ValueError("database URL file is empty")
        store = PersistenceStore(database_url)
        if not store.schema_is_current():
            raise ValueError("database schema is not at the reviewed migration head")
        checked_at = datetime.now(UTC).isoformat()
        counts = store.verify_evidence_integrity(batch_size=args.batch_size)
        print(
            json.dumps(
                {
                    "schema_version": "evidence-integrity-v2",
                    "checked_at": checked_at,
                    "status": "verified",
                    "row_counts": counts,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except ValueError as error:
        message = str(error)
        if not message.startswith(
            (
                "database URL file",
                "database schema",
                "metric metadata",
                "application probe metadata",
                "event metadata",
                "audit event metadata",
                "closure case normalized metadata",
                "action operation normalized metadata",
                "incident state metadata",
                "incident state relationship metadata",
                "action operation relationship metadata",
            )
        ):
            message = "evidence integrity verification failed"
        print(json.dumps({"status": "failed", "error": message}))
        return 1
    except Exception:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": "evidence integrity verification failed; inspect protected logs",
                }
            )
        )
        return 1
    finally:
        if store is not None:
            store.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
