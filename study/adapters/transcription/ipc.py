"""Strict newline-delimited JSON contract for the local study worker."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from study.application.ports.transcriber import (
    PROTOCOL_VERSION,
    ArtifactReference,
    TranscriptionRequest,
    WorkerEvent,
)

MAX_MESSAGE_BYTES = 64 * 1024
MESSAGE_TYPES = frozenset({"hello", "request", "event", "response"})


class IPCProtocolError(RuntimeError):
    """Base error for an unusable worker protocol stream."""


class MalformedMessageError(IPCProtocolError):
    """The stdout line was not one complete contract message."""


class HandshakeError(IPCProtocolError):
    """The child did not complete a valid handshake."""


class ProtocolVersionError(HandshakeError):
    """Parent and worker use incompatible IPC versions."""


class PipelineSignatureError(HandshakeError):
    """The running worker does not match the requested model pipeline."""


class WorkerDiedError(IPCProtocolError):
    """The worker exited or closed stdout before a terminal response."""


class ArtifactValidationError(IPCProtocolError):
    """A referenced worker artifact is unsafe, missing, or corrupt."""


def encode_message(message: Mapping[str, Any]) -> str:
    """Encode exactly one compact JSON object terminated by a newline."""

    validated = validate_message(dict(message))
    encoded = json.dumps(
        validated,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    if len(encoded.encode("utf-8")) + 1 > MAX_MESSAGE_BYTES:
        raise MalformedMessageError("IPC message exceeds the 64 KiB limit")
    return encoded + "\n"


def decode_message(line: str | bytes) -> dict[str, Any]:
    """Decode one stdout line; arbitrary worker output is never tolerated."""

    if isinstance(line, bytes):
        if len(line) > MAX_MESSAGE_BYTES:
            raise MalformedMessageError("IPC message exceeds the 64 KiB limit")
        try:
            line = line.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise MalformedMessageError("IPC message is not UTF-8") from exc
    elif len(line.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise MalformedMessageError("IPC message exceeds the 64 KiB limit")
    if not line.endswith("\n"):
        raise MalformedMessageError("IPC message is not newline terminated")
    if not line.strip():
        raise MalformedMessageError("IPC message is empty")
    try:
        message = json.loads(line)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise MalformedMessageError("worker stdout is not structured JSON") from exc
    if not isinstance(message, dict):
        raise MalformedMessageError("IPC message must be a JSON object")
    return validate_message(message)


def validate_message(message: dict[str, Any]) -> dict[str, Any]:
    message_type = message.get("type")
    if message_type not in MESSAGE_TYPES:
        raise MalformedMessageError(f"unknown IPC message type: {message_type!r}")
    if message_type == "hello":
        _validate_hello(message)
    elif message_type == "request":
        _validate_request(message)
    elif message_type == "event":
        _validate_event(message)
    else:
        _validate_response(message)
    return message


def validate_handshake(
    message: Mapping[str, Any],
    expected_pipeline_signature: str,
    *,
    protocol_version: int = PROTOCOL_VERSION,
) -> None:
    if message.get("type") != "hello":
        raise HandshakeError("worker did not emit a startup hello")
    if message.get("protocol_version") != protocol_version:
        raise ProtocolVersionError(
            f"worker protocol {message.get('protocol_version')!r} does not match "
            f"{protocol_version}"
        )
    if message.get("pipeline_signature") != expected_pipeline_signature:
        raise PipelineSignatureError("worker pipeline signature does not match")
    requests = message.get("requests")
    if not isinstance(requests, list) or "transcribe" not in requests:
        raise HandshakeError("worker does not advertise transcribe support")


def request_message(
    request: TranscriptionRequest, *, protocol_version: int = PROTOCOL_VERSION
) -> dict[str, Any]:
    options = dict(request.options)
    parameters: dict[str, Any] = {
        "source_path": request.source_path,
        "source_sha256": request.source_sha256,
        "staging_dir": request.staging_path,
        "media_kind": options.pop("media_kind", "audio"),
        "language": options.pop("language", "es"),
        "start_phase": request.start_phase,
        "checkpoints": [
            {
                "phase": checkpoint.phase,
                "path": checkpoint.artifact_path,
                "sha256": checkpoint.artifact_sha256,
            }
            for checkpoint in request.checkpoints
        ],
    }
    for key in ("assignment", "min_speakers", "max_speakers"):
        if key in options:
            value = options.pop(key)
            if value is not None:
                parameters[key] = dict(value) if key == "assignment" else value
    if options:
        raise MalformedMessageError(
            f"unsupported transcribe option: {sorted(options)[0]}"
        )
    return {
        "type": "request",
        "protocol_version": protocol_version,
        "id": request.job_id,
        "method": "transcribe",
        "params": parameters,
    }


def event_from_message(message: Mapping[str, Any]) -> WorkerEvent:
    return WorkerEvent(
        kind="progress",
        job_id=str(message["id"]),
        phase=str(message["phase"]),
        message=str(message["state"]),
    )


def artifacts_from_response(
    message: Mapping[str, Any],
) -> tuple[tuple[str, ArtifactReference], ...]:
    container = message.get("result") if message.get("ok") else message.get("error")
    if not isinstance(container, Mapping):
        return ()
    artifacts = container.get("artifacts", {})
    if not isinstance(artifacts, Mapping):
        return ()
    return tuple(
        (
            str(name),
            ArtifactReference(
                path=str(reference["path"]), sha256=str(reference["sha256"])
            ),
        )
        for name, reference in sorted(artifacts.items())
    )


def _validate_hello(message: Mapping[str, Any]) -> None:
    _strict_keys(
        message,
        required={
            "type",
            "protocol_version",
            "pipeline_revision",
            "pipeline_signature",
            "requests",
        },
    )
    _integer(message, "protocol_version")
    _string(message, "pipeline_revision")
    _sha256(message, "pipeline_signature")
    requests = message["requests"]
    if not isinstance(requests, list) or not all(
        isinstance(method, str) and method for method in requests
    ):
        raise MalformedMessageError("requests must contain method names")


def _validate_request(message: Mapping[str, Any]) -> None:
    _strict_keys(
        message,
        required={"type", "protocol_version", "id", "method", "params"},
    )
    _integer(message, "protocol_version")
    _string(message, "id")
    if len(message["id"]) > 128:
        raise MalformedMessageError("request id exceeds 128 characters")
    if message["method"] not in {"ping", "transcribe", "shutdown"}:
        raise MalformedMessageError("request method is invalid")
    params = message["params"]
    if not isinstance(params, dict):
        raise MalformedMessageError("request params must be an object")
    if message["method"] == "transcribe":
        _strict_keys(
            params,
            required={
                "source_path",
                "source_sha256",
                "staging_dir",
                "media_kind",
                "language",
                "start_phase",
                "checkpoints",
            },
            optional={"assignment", "min_speakers", "max_speakers"},
        )
        for key in ("source_path", "staging_dir"):
            _string(params, key)
        _sha256(params, "source_sha256")
        if params["media_kind"] not in {"audio", "video"}:
            raise MalformedMessageError("media_kind must be audio or video")
        if params["language"] not in {"auto", "en", "es"}:
            raise MalformedMessageError("language must be auto, en, or es")
        if params["start_phase"] not in {"asr", "alignment", "diarization"}:
            raise MalformedMessageError("start_phase is invalid")
        checkpoints = params["checkpoints"]
        if not isinstance(checkpoints, list):
            raise MalformedMessageError("checkpoints must be an array")
        seen_phases: set[str] = set()
        for checkpoint in checkpoints:
            if not isinstance(checkpoint, dict):
                raise MalformedMessageError("checkpoint must be an object")
            _strict_keys(checkpoint, required={"phase", "path", "sha256"})
            if checkpoint["phase"] not in {"asr", "alignment", "diarization"}:
                raise MalformedMessageError("checkpoint phase is invalid")
            if checkpoint["phase"] in seen_phases:
                raise MalformedMessageError("checkpoint phases must be unique")
            seen_phases.add(checkpoint["phase"])
            _string(checkpoint, "path")
            _sha256(checkpoint, "sha256")
        assignment = params.get("assignment")
        if assignment is not None and not isinstance(assignment, dict):
            raise MalformedMessageError("assignment must be an object")
        for key in ("min_speakers", "max_speakers"):
            value = params.get(key)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 1
            ):
                raise MalformedMessageError(f"{key} must be a positive integer")
    elif params:
        raise MalformedMessageError(f"{message['method']} does not accept params")


def _validate_event(message: Mapping[str, Any]) -> None:
    _strict_keys(
        message,
        required={"type", "id", "event", "phase", "state"},
    )
    _string(message, "id")
    if message["event"] != "phase":
        raise MalformedMessageError("worker event is invalid")
    _string(message, "phase")
    if message["state"] not in {"started", "completed", "failed"}:
        raise MalformedMessageError("worker phase state is invalid")


def _validate_response(message: Mapping[str, Any]) -> None:
    _strict_keys(
        message,
        required={"type", "id", "ok"},
        optional={"result", "error"},
    )
    if message["id"] is not None:
        _string(message, "id")
    if not isinstance(message["ok"], bool):
        raise MalformedMessageError("response ok must be boolean")
    payload_key = "result" if message["ok"] else "error"
    other_key = "error" if message["ok"] else "result"
    if payload_key not in message or other_key in message:
        raise MalformedMessageError("response payload does not match ok")
    payload = message[payload_key]
    if not isinstance(payload, dict):
        raise MalformedMessageError(f"response {payload_key} must be an object")
    if message["ok"]:
        if "status" in payload:
            _strict_keys(
                payload,
                required={
                    "status",
                    "assigned",
                    "completed_phases",
                    "failed_phases",
                    "error",
                    "artifacts",
                },
                optional={"pipeline_signature", "profile"},
            )
            if payload["status"] not in {"complete", "partial"}:
                raise MalformedMessageError("transcription status is invalid")
            if not isinstance(payload["assigned"], bool):
                raise MalformedMessageError("assigned must be boolean")
            _phase_list(payload, "completed_phases")
            _phase_list(payload, "failed_phases")
            if payload["error"] is not None and not isinstance(payload["error"], str):
                raise MalformedMessageError("error must be a string or null")
            if "pipeline_signature" in payload:
                _sha256(payload, "pipeline_signature")
            if "profile" in payload:
                profile = payload["profile"]
                if not isinstance(profile, dict):
                    raise MalformedMessageError("worker profile must be an object")
                _strict_keys(profile, required={"device", "compute_type"})
                if profile["device"] not in {"cpu", "cuda"}:
                    raise MalformedMessageError("worker profile device is invalid")
                _string(profile, "compute_type")
            _validate_artifacts(payload["artifacts"])
        elif "state" in payload:
            _strict_keys(payload, required={"state"}, optional={"pipeline_signature"})
            if payload["state"] not in {"ready", "stopping"}:
                raise MalformedMessageError("worker state is invalid")
            if "pipeline_signature" in payload:
                _sha256(payload, "pipeline_signature")
        else:
            raise MalformedMessageError("success response result is invalid")
    else:
        _strict_keys(payload, required={"code", "message"}, optional={"artifacts"})
        _string(payload, "code")
        _string(payload, "message")
        if "artifacts" in payload:
            _validate_artifacts(payload["artifacts"])


def _validate_artifacts(artifacts: object) -> None:
    if not isinstance(artifacts, dict):
        raise MalformedMessageError("artifacts must be an object")
    for name, artifact in artifacts.items():
        if not isinstance(name, str) or not name:
            raise MalformedMessageError("artifact name must be a non-empty string")
        if not isinstance(artifact, dict):
            raise MalformedMessageError("artifact reference must be an object")
        _strict_keys(artifact, required={"path", "sha256"})
        _string(artifact, "path")
        _sha256(artifact, "sha256")


def _phase_list(message: Mapping[str, Any], key: str) -> None:
    phases = message[key]
    if not isinstance(phases, list) or not all(
        isinstance(phase, str) and phase for phase in phases
    ):
        raise MalformedMessageError(f"{key} must contain phase names")
    if len(phases) != len(set(phases)):
        raise MalformedMessageError(f"{key} must not contain duplicates")


def _strict_keys(
    message: Mapping[str, Any],
    *,
    required: set[str],
    optional: set[str] | None = None,
) -> None:
    keys = set(message)
    missing = required - keys
    unexpected = keys - required - (optional or set())
    if missing:
        raise MalformedMessageError(f"IPC message is missing {sorted(missing)!r}")
    if unexpected:
        raise MalformedMessageError(
            f"IPC message has unexpected fields {sorted(unexpected)!r}"
        )


def _string(message: Mapping[str, Any], key: str) -> None:
    if not isinstance(message.get(key), str) or not message[key]:
        raise MalformedMessageError(f"{key} must be a non-empty string")


def _integer(message: Mapping[str, Any], key: str) -> None:
    if not isinstance(message.get(key), int) or isinstance(message[key], bool):
        raise MalformedMessageError(f"{key} must be an integer")


def _sha256(message: Mapping[str, Any], key: str) -> None:
    _string(message, key)
    value = message[key]
    if len(value) != 64 or any(
        character not in "0123456789abcdefABCDEF" for character in value
    ):
        raise MalformedMessageError(f"{key} must be a hexadecimal SHA-256 digest")
