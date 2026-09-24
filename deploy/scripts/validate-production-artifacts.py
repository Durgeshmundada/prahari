#!/usr/bin/env python3
"""Validate approved model and offline Ollama content without network access."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import ipaddress
import json
import re
import stat
import sys
from pathlib import Path
from typing import Any

SHA256 = re.compile(r"^[0-9a-f]{64}$")
RUNTIME_FEATURES = ["packet_loss_ratio", "jitter_ms", "rtt_avg_ms"]
OLLAMA_MANIFEST_MEDIA_TYPE = "application/vnd.docker.distribution.manifest.v2+json"
OLLAMA_CONFIG_MEDIA_TYPE = "application/vnd.docker.container.image.v1+json"
OLLAMA_MODEL_MEDIA_TYPE = "application/vnd.ollama.image.model"


def fail(message: str) -> None:
    raise SystemExit(f"ERROR: {message}")


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            fail(f"invalid environment entry at {path}:{line_number}")
        key, value = line.split("=", 1)
        if not key or key in values:
            fail(f"duplicate or empty environment key at {path}:{line_number}")
        if value[:1] in {'"', "'"}:
            fail(f"quoted environment values are not accepted: {key}")
        values[key] = value
    return values


def required(values: dict[str, str], key: str) -> str:
    value = values.get(key, "")
    if not value:
        fail(f"missing {key}")
    return value


def unresolved(deploy_dir: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else deploy_dir / path


def reject_symlink_components(path: Path, label: str) -> None:
    """Reject a symlink anywhere in an existing input path, not only at its leaf."""

    absolute = path.absolute()
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        try:
            mode = current.lstat().st_mode
        except OSError as error:
            fail(f"cannot inspect {label} path component {current}: {error}")
        if stat.S_ISLNK(mode):
            fail(f"{label} must not traverse a symbolic link: {current}")


def regular_directory(path: Path, label: str) -> Path:
    reject_symlink_components(path, label)
    if not path.is_dir():
        fail(f"{label} must be an existing directory")
    return path.resolve()


def regular_file(path: Path, label: str, *, inside: Path | None = None) -> Path:
    reject_symlink_components(path, label)
    resolved = path.resolve()
    if inside is not None and not resolved.is_relative_to(inside):
        fail(f"{label} must remain inside {inside}")
    if not resolved.is_file():
        fail(f"{label} must be an existing regular file")
    return resolved


def named_file_inside(directory_input: Path, name: str, label: str) -> Path:
    relative = Path(name)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(component in {"", ".", ".."} for component in relative.parts)
    ):
        fail(f"{label} must be a safe relative path")
    directory = directory_input.resolve()
    return regular_file(directory_input / relative, label, inside=directory)


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        fail(f"cannot hash {path}: {error}")
    return digest.hexdigest()


def validate_prediction_model(values: dict[str, str], deploy_dir: Path) -> None:
    model_dir_input = unresolved(deploy_dir, required(values, "PRAHARI_MODEL_DIR"))
    regular_directory(model_dir_input, "PRAHARI_MODEL_DIR")
    artifact = named_file_inside(
        model_dir_input,
        required(values, "PRAHARI_MODEL_ARTIFACT_NAME"),
        "legacy prediction model artifact",
    )
    manifest_path = named_file_inside(
        model_dir_input,
        required(values, "PRAHARI_MODEL_MANIFEST_NAME"),
        "legacy prediction model manifest",
    )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        fail(f"invalid prediction-model manifest: {error}")
    if manifest.get("schema_version") != "model-manifest-v1":
        fail("prediction model uses an unsupported manifest schema")
    if manifest.get("approval_status") != "approved":
        fail("prediction model manifest is not approved")
    if manifest.get("predictive_claim_approved") is not True:
        fail("prediction model manifest does not approve the predictive claim")
    if not isinstance(manifest.get("model_version"), str) or not manifest["model_version"]:
        fail("prediction model manifest has no model_version")
    if manifest.get("features") != RUNTIME_FEATURES:
        fail("prediction model features do not match the runtime feature contract")
    threshold = manifest.get("decision_threshold")
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        fail("prediction model decision_threshold must be numeric")
    if not 0 < float(threshold) < 1:
        fail("prediction model decision_threshold must be between zero and one")
    expected = manifest.get("model_sha256", "")
    if not SHA256.fullmatch(expected) or digest_file(artifact) != expected:
        fail("prediction model checksum does not match its approved manifest")
    try:
        import lightgbm as lgb
    except ImportError as error:
        fail(f"LightGBM is required to validate runtime compatibility: {error}")
    try:
        booster = lgb.Booster(model_file=str(artifact))
    except Exception as error:  # LightGBM exposes version-specific exception classes.
        fail(f"prediction model cannot be loaded by the runtime LightGBM build: {error}")
    if booster.feature_name() != RUNTIME_FEATURES:
        fail("prediction model binary feature names do not match the runtime contract")

    predictive_manifest = named_file_inside(
        model_dir_input,
        required(values, "PRAHARI_PREDICTIVE_MODEL_MANIFEST_NAME"),
        "predictive-v2 model manifest",
    )
    if predictive_manifest in {artifact, manifest_path}:
        fail("predictive-v2 manifest must be distinct from legacy model inputs")
    try:
        repository_source = Path(__file__).resolve().parents[2] / "src"
        if repository_source.is_dir():
            sys.path.insert(0, str(repository_source))
        from prahari.analytics.horizon import PredictiveHorizonClassifier

        classifier = PredictiveHorizonClassifier(predictive_manifest)
    except Exception as error:
        fail(f"predictive-v2 bundle failed runtime validation: {error}")
    if (
        not classifier.activated
        or classifier.approval_status != "approved"
        or classifier.deployment_scope != "production"
        or not classifier.predictive_claim_approved
    ):
        fail("predictive-v2 bundle is not explicitly approved for production")
    profile_path = regular_file(
        unresolved(deploy_dir, required(values, "PRAHARI_NETWORK_PROFILE_FILE")),
        "network profile",
    )
    try:
        from prahari.network_profile import load_network_profile

        profile = load_network_profile(profile_path)
    except Exception as error:
        fail(f"network profile failed validation: {error}")
    if profile.deployment_scope != "production":
        fail("network profile is not explicitly scoped for production")
    if classifier.network_profile_sha256 != profile.fingerprint:
        fail("predictive approval does not bind the configured network profile checksum")
    if (
        classifier.sampling_seconds != profile.config.sampling_seconds
        or classifier.window_seconds != profile.config.window_seconds
        or classifier.minimum_samples != profile.config.minimum_samples_per_metric
    ):
        fail("predictive model window policy does not match the network profile")
    knowledge_input = unresolved(deploy_dir, required(values, "PRAHARI_KNOWLEDGE_DIR"))
    regular_directory(knowledge_input, "PRAHARI_KNOWLEDGE_DIR")
    if not profile.approved_runbooks:
        fail("production copilot requires explicitly approved runbook references")
    for book in profile.approved_runbooks:
        document = named_file_inside(knowledge_input, book.path, "approved runbook")
        if digest_file(document) != book.sha256:
            fail("approved runbook checksum does not match the network profile")
    try:
        from prahari.copilot.retrieval import LocalRunbookIndex

        LocalRunbookIndex(
            knowledge_input,
            document_checksums={book.path: book.sha256 for book in profile.approved_runbooks},
        )
    except Exception as error:
        fail(f"approved runbook catalog cannot be loaded by the runtime: {error}")


def validate_ollama_model(values: dict[str, str], deploy_dir: Path) -> None:
    models_dir_input = unresolved(deploy_dir, required(values, "PRAHARI_OLLAMA_MODELS_DIR"))
    manifest_input = unresolved(deploy_dir, required(values, "PRAHARI_OLLAMA_MODEL_MANIFEST"))
    models_dir = regular_directory(models_dir_input, "PRAHARI_OLLAMA_MODELS_DIR")
    manifest_path = regular_file(
        manifest_input,
        "Ollama model manifest",
        inside=models_dir,
    )
    expected_manifest_hash = required(values, "PRAHARI_OLLAMA_MODEL_MANIFEST_SHA256")
    if not SHA256.fullmatch(expected_manifest_hash) or set(expected_manifest_hash) == {"0"}:
        fail("PRAHARI_OLLAMA_MODEL_MANIFEST_SHA256 must be a non-placeholder SHA-256")
    if digest_file(manifest_path) != expected_manifest_hash:
        fail("Ollama model manifest checksum does not match the release setting")

    model = required(values, "PRAHARI_OLLAMA_MODEL")
    if ":" not in model:
        fail("PRAHARI_OLLAMA_MODEL must include an explicit tag")
    name, tag = model.rsplit(":", 1)
    expected_suffix = tuple([*name.split("/"), tag])
    if tuple(manifest_path.parts[-len(expected_suffix) :]) != expected_suffix:
        fail("Ollama manifest path does not match PRAHARI_OLLAMA_MODEL")

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        fail(f"invalid Ollama model manifest: {error}")
    if manifest.get("schemaVersion") != 2:
        fail("Ollama model manifest schemaVersion must be 2")
    if manifest.get("mediaType") != OLLAMA_MANIFEST_MEDIA_TYPE:
        fail("Ollama model manifest has an unsupported mediaType")
    config = manifest.get("config")
    layers = manifest.get("layers")
    if not isinstance(config, dict) or not isinstance(layers, list) or not layers:
        fail("Ollama model manifest has invalid content descriptors")
    if config.get("mediaType") != OLLAMA_CONFIG_MEDIA_TYPE:
        fail("Ollama model config has an unsupported mediaType")
    if not all(isinstance(layer, dict) for layer in layers):
        fail("Ollama model layers must be descriptor objects")
    descriptors: list[dict[str, Any]] = [config, *layers]
    blobs_dir = models_dir / "blobs"
    reject_symlink_components(blobs_dir, "Ollama blobs directory")
    if not blobs_dir.is_dir():
        fail("Ollama blobs directory is missing")
    seen_digests: set[str] = set()
    resolved_blobs: dict[str, Path] = {}
    for descriptor in descriptors:
        digest = descriptor.get("digest", "")
        if not isinstance(digest, str) or not digest.startswith("sha256:"):
            fail("Ollama model descriptor is missing a SHA-256 digest")
        hexadecimal = digest.removeprefix("sha256:")
        if not SHA256.fullmatch(hexadecimal):
            fail("Ollama model descriptor has an invalid SHA-256 digest")
        if hexadecimal in seen_digests:
            fail("Ollama model manifest repeats a content digest")
        seen_digests.add(hexadecimal)
        blob = regular_file(
            blobs_dir / f"sha256-{hexadecimal}",
            "Ollama model blob",
            inside=models_dir,
        )
        descriptor_size = descriptor.get("size")
        if (
            isinstance(descriptor_size, bool)
            or not isinstance(descriptor_size, int)
            or descriptor_size < 1
            or blob.stat().st_size != descriptor_size
        ):
            fail(f"Ollama model blob size does not match its manifest: {blob.name}")
        if digest_file(blob) != hexadecimal:
            fail(f"Ollama model blob is missing or corrupt: {blob.name}")
        resolved_blobs[hexadecimal] = blob

    model_layers = [layer for layer in layers if layer.get("mediaType") == OLLAMA_MODEL_MEDIA_TYPE]
    if len(model_layers) != 1:
        fail("Ollama manifest must contain exactly one GGUF model layer")
    model_digest = str(model_layers[0]["digest"]).removeprefix("sha256:")
    expected_gguf_hash = required(values, "PRAHARI_OLLAMA_MODEL_GGUF_SHA256")
    if not SHA256.fullmatch(expected_gguf_hash) or set(expected_gguf_hash) == {"0"}:
        fail("PRAHARI_OLLAMA_MODEL_GGUF_SHA256 must be a non-placeholder SHA-256")
    if model_digest != expected_gguf_hash:
        fail("configured GGUF checksum does not match the exact Ollama model layer")
    gguf_blob = resolved_blobs[model_digest]
    try:
        with gguf_blob.open("rb") as handle:
            magic = handle.read(4)
    except OSError as error:
        fail(f"cannot inspect the GGUF model blob: {error}")
    if magic != b"GGUF":
        fail("the exact Ollama model layer is not a GGUF artifact")


def validate_copilot_network(values: dict[str, str]) -> None:
    try:
        subnet = ipaddress.ip_network(required(values, "PRAHARI_COPILOT_SUBNET"), strict=True)
        address = ipaddress.ip_address(required(values, "PRAHARI_OLLAMA_STATIC_IP"))
    except ValueError as error:
        fail(f"invalid copilot subnet or Ollama address: {error}")
    if subnet.version != 4 or not subnet.is_private:
        fail("PRAHARI_COPILOT_SUBNET must be a private IPv4 network")
    if address not in subnet or address in {subnet.network_address, subnet.broadcast_address}:
        fail("PRAHARI_OLLAMA_STATIC_IP must be a usable address inside the copilot subnet")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("env_file", type=Path)
    args = parser.parse_args()
    env_file = args.env_file.resolve()
    values = read_env(env_file)
    validate_copilot_network(values)
    validate_prediction_model(values, env_file.parent)
    validate_ollama_model(values, env_file.parent)
    spec = importlib.util.spec_from_file_location(
        "prahari_release_acceptance", Path(__file__).with_name("validate-release-acceptance.py")
    )
    acceptance = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(acceptance)
    try:
        acceptance.validate_release(env_file)
    except (OSError, ValueError) as error:
        fail(f"release acceptance failed: {error}")
    print("Approved legacy/predictive-v2 and exact offline Ollama GGUF artifacts are valid.")


if __name__ == "__main__":
    main()
