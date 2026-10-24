"""NDJSON entry point for the supervised WhisperX worker.

Protocol v1 writes exactly one JSON object per stdout line. The worker emits a
startup ``hello`` and accepts request envelopes shaped as::

    {"type":"request","protocol_version":1,"id":"job-1",
     "method":"ping|transcribe|shutdown","params":{...}}

``transcribe`` params are limited to ``source_path``, ``source_sha256``,
``staging_dir``, ``media_kind`` (``audio`` or ``video``), optional ``language``,
``assignment``, ``min_speakers``, and ``max_speakers``. Progress uses ``event``
objects. Responses never contain transcript payloads, only artifact path/hash
references. Operational and third-party output is redirected to stderr.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from typing import Any, Mapping, TextIO

from study.adapters.transcription.artifacts import sanitize_error
from study.worker.pipeline import PIPELINE_REVISION, PipelineError, WhisperXPipeline

PROTOCOL_VERSION = 1
_METHODS = {"ping", "transcribe", "shutdown"}


def run(
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
    error_stream: TextIO | None = None,
    *,
    pipeline: WhisperXPipeline | None = None,
) -> int:
    input_stream = input_stream or sys.stdin
    output_stream = output_stream or sys.stdout
    error_stream = error_stream or sys.stderr
    logging.basicConfig(
        level=os.environ.get("ACADEMIC_WORKER_LOG_LEVEL", "INFO"),
        stream=error_stream,
        format="%(levelname)s %(message)s",
        force=True,
    )
    logger = logging.getLogger("study.worker")
    pipeline = pipeline or WhisperXPipeline()
    _emit(
        output_stream,
        {
            "type": "hello",
            "protocol_version": PROTOCOL_VERSION,
            "pipeline_revision": PIPELINE_REVISION,
            "pipeline_signature": pipeline.signature,
            "requests": sorted(_METHODS),
        },
    )

    for line in input_stream:
        if not line.strip():
            continue
        request_id: str | None = None
        try:
            request = json.loads(line)
            request_id, method, params = _validate_request(request)
            if method == "ping":
                result = {"state": "ready", "pipeline_signature": pipeline.signature}
            elif method == "shutdown":
                _emit(output_stream, _success(request_id, {"state": "stopping"}))
                return 0
            else:

                def progress(event: Mapping[str, str]) -> None:
                    _emit(
                        output_stream,
                        {
                            "type": "event",
                            "id": request_id,
                            "event": "phase",
                            **event,
                        },
                    )

                with redirect_stdout(error_stream), redirect_stderr(error_stream):
                    result = pipeline.transcribe(params, progress)
            _emit(output_stream, _success(request_id, result))
        except PipelineError as error:
            logger.error("%s: %s", error.code, sanitize_error(error))
            payload: dict[str, Any] = {
                "code": error.code,
                "message": sanitize_error(error),
            }
            if error.artifacts:
                payload["artifacts"] = {
                    name: reference.to_wire()
                    for name, reference in sorted(error.artifacts.items())
                }
            _emit(output_stream, _failure(request_id, payload))
        except (RequestError, json.JSONDecodeError) as error:
            logger.error("invalid request: %s", sanitize_error(error))
            _emit(
                output_stream,
                _failure(
                    request_id,
                    {"code": "invalid_request", "message": sanitize_error(error)},
                ),
            )
        except Exception as error:
            logger.error("worker failure: %s", sanitize_error(error))
            _emit(
                output_stream,
                _failure(
                    request_id,
                    {"code": "worker_failed", "message": sanitize_error(error)},
                ),
            )
    return 0


class RequestError(ValueError):
    pass


def _validate_request(
    request: Any,
) -> tuple[str, str, Mapping[str, Any]]:
    if not isinstance(request, dict):
        raise RequestError("request must be an object")
    if request.get("type") != "request":
        raise RequestError("type must be request")
    if request.get("protocol_version") != PROTOCOL_VERSION:
        raise RequestError("unsupported protocol_version")
    request_id = request.get("id")
    if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
        raise RequestError("id must be a non-empty string up to 128 characters")
    method = request.get("method")
    if method not in _METHODS:
        raise RequestError("unsupported method")
    params = request.get("params", {})
    if not isinstance(params, dict):
        raise RequestError("params must be an object")
    unexpected = set(request) - {"type", "protocol_version", "id", "method", "params"}
    if unexpected:
        raise RequestError(f"unsupported envelope field: {sorted(unexpected)[0]}")
    if method != "transcribe" and params:
        raise RequestError(f"{method} does not accept params")
    return request_id, method, params


def _success(request_id: str, result: Mapping[str, Any]) -> dict[str, Any]:
    return {"type": "response", "id": request_id, "ok": True, "result": result}


def _failure(request_id: str | None, error: Mapping[str, Any]) -> dict[str, Any]:
    return {"type": "response", "id": request_id, "ok": False, "error": error}


def _emit(stream: TextIO, message: Mapping[str, Any]) -> None:
    stream.write(
        json.dumps(
            message,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    )
    stream.flush()


if __name__ == "__main__":
    raise SystemExit(run())
