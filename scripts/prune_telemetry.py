"""Preview by default; explicit bounded pruning under a controlled maintenance identity."""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from prahari.persistence import PersistenceStore
from prahari.retention import preview_retention, prune_retention_batch


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url-file",
        type=Path,
        default=os.environ.get("PRAHARI_MAINTENANCE_DATABASE_URL_FILE"),
    )
    parser.add_argument("--retain-days", type=int, default=30)
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--max-batches", type=int, default=1)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--actor-id")
    args = parser.parse_args()
    if (
        args.database_url_file is None
        or not 1 <= args.retain_days <= 3650
        or not 1 <= args.batch_size <= 10000
        or not 1 <= args.max_batches <= 100
        or (args.execute and (not args.actor_id or len(args.actor_id) > 100))
    ):
        parser.error("supply a protected URL file, bounded policy and an actor for execution")
    store = None
    try:
        url = args.database_url_file.read_text(encoding="utf-8").strip()
        store = PersistenceStore(url)
        if not store.schema_is_current():
            raise ValueError("apply reviewed database migrations before maintenance")
        cutoff = datetime.now(UTC) - timedelta(days=args.retain_days)
        result = {"preview": preview_retention(store, cutoff), "batches": []}
        if args.execute:
            for _ in range(args.max_batches):
                batch = prune_retention_batch(
                    store, cutoff, actor_id=args.actor_id, limit=args.batch_size
                )
                result["batches"].append(batch)
                if not any(batch["deleted_rows"].values()):
                    break
        print(json.dumps(result, indent=2))
        return 0
    except ValueError as error:
        # Never echo a connection exception or its URL/credentials.
        message = (
            str(error)
            if str(error).startswith(("retention ", "apply reviewed"))
            else "invalid maintenance configuration"
        )
        print(json.dumps({"error": message}))
        return 1
    except Exception:
        print(json.dumps({"error": "maintenance failed; inspect protected operator logs"}))
        return 1
    finally:
        if store is not None:
            store.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
