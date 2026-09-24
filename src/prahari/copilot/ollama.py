"""Restricted client for a local Ollama-compatible chat endpoint."""

from __future__ import annotations

import ipaddress
import json
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ValidationError


class OllamaError(RuntimeError):
    pass


class OllamaClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        timeout_seconds: float = 45,
        transport: httpx.BaseTransport | None = None,
        allowed_private_ip: str | None = None,
    ) -> None:
        parsed = urlparse(base_url)
        if (
            parsed.scheme != "http"
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Ollama URL must be a plain local HTTP origin")
        try:
            address = ipaddress.ip_address(parsed.hostname or "")
        except ValueError as error:
            raise ValueError("Ollama URL must use an explicit loopback IP address") from error
        private_allowed = allowed_private_ip is not None and str(address) == allowed_private_ip
        if (not address.is_loopback and not private_allowed) or parsed.path not in {"", "/"}:
            raise ValueError(
                "Ollama URL must use loopback or the one explicitly allowed private IP, and no path"
            )
        if not model.strip():
            raise ValueError("Ollama model must not be empty")
        self._model = model
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            transport=transport,
            trust_env=False,
        )

    def structured_chat(
        self,
        prompt: str,
        response_model: type[BaseModel],
        *,
        json_schema: dict[str, object] | None = None,
    ) -> BaseModel:
        schema = json_schema or response_model.model_json_schema()
        if len(prompt.encode("utf-8")) > 65536:
            raise OllamaError("local model context exceeds the request limit")
        last_error = "invalid response"
        messages = [{"role": "user", "content": prompt}]
        for attempt in range(2):
            content = ""
            try:
                payload = self._bounded_chat(
                    {
                        "model": self._model,
                        "stream": False,
                        "format": schema,
                        "think": False,
                        "options": {"temperature": 0, "num_predict": 1024, "num_ctx": 8192},
                        "messages": messages,
                    }
                )
                content = payload["message"]["content"]
                return response_model.model_validate_json(content)
            except (httpx.HTTPError, KeyError, TypeError, ValueError, ValidationError) as error:
                last_error = str(error)
                if attempt == 0:
                    previous = content[:4000]
                    messages.append({"role": "assistant", "content": previous})
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "The previous JSON failed validation. Return only corrected JSON "
                                "matching the supplied schema. Do not use bare category labels as "
                                "narrative fields. Validation detail: " + last_error[:1000]
                            ),
                        }
                    )
        raise OllamaError(f"local model did not return a valid structured response: {last_error}")

    def _bounded_chat(self, payload: dict) -> dict:
        with self._client.stream("POST", "/api/chat", json=payload) as response:
            response.raise_for_status()
            body = bytearray()
            for chunk in response.iter_bytes(chunk_size=65536):
                if len(body) + len(chunk) > 1024 * 1024:
                    raise ValueError("local model response exceeds the 1 MiB limit")
                body.extend(chunk)
            return json.loads(body)

    def close(self) -> None:
        self._client.close()
