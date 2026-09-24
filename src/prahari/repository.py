"""Small local-file repository used until live storage is introduced."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


def load_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_jsonl(path: Path, model: type[T]) -> list[T]:
    records: list[T] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            records.append(model.model_validate_json(line))
        except ValueError as error:
            message = f"invalid {path.name} record at line {line_number}: {error}"
            raise ValueError(message) from error
    return records
