"""Small deterministic worker used by ordinary supervisor tests."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

SIGNATURE = os.environ.get("ACADEMIC_PIPELINE_SIGNATURE", "a" * 64)


def send(message: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def receive() -> dict[str, object] | None:
    line = sys.stdin.readline()
    return json.loads(line) if line else None


def handshake(mode: str) -> bool:
    if mode == "malformed":
        sys.stdout.write("worker banner that violates the protocol\n")
        sys.stdout.flush()
        return False
    if mode == "eof":
        return False
    signature = SIGNATURE if mode != "bad-signature" else "b" * 64
    version = 2 if mode == "bad-version" else 1
    send(
        {
            "type": "hello",
            "protocol_version": version,
            "pipeline_revision": "fake-worker-v1",
            "pipeline_signature": signature,
            "requests": ["ping", "shutdown", "transcribe"],
        }
    )
    return mode not in {"bad-version", "bad-signature"}


def artifact(
    job_id: str, phase: str, staging: Path, *, corrupt: bool
) -> dict[str, str]:
    path = staging / f"{phase}.json"
    content = json.dumps({"job_id": job_id, "phase": phase}).encode()
    path.write_bytes(content)
    digest = "0" * 64 if corrupt else hashlib.sha256(content).hexdigest()
    return {"path": str(path), "sha256": digest}


def phase_event(job_id: str, phase: str, state: str) -> None:
    send(
        {
            "type": "event",
            "id": job_id,
            "event": "phase",
            "phase": phase,
            "state": state,
        }
    )


def transcript_artifacts(
    job_id: str, staging: Path, params: dict[str, object]
) -> dict[str, dict[str, str]]:
    assignment = params.get("assignment")
    if not isinstance(assignment, dict):
        assignment = {}
    payload = {
        "version": 1,
        "status": "complete",
        "source": {
            "sha256": params["source_sha256"],
            "duration_ms": 1000,
        },
        "assignment": {
            "course": assignment.get("course"),
            "class_date": assignment.get("class_date"),
        },
        "language": {"requested": params.get("language", "es"), "detected": "es"},
        "revisions": {
            "pipeline": "fake-worker-v1",
            "asr": "fake",
            "alignment": "fake",
            "diarization": "fake",
        },
        "phases": {
            "asr": "completed",
            "alignment": "completed",
            "diarization": "completed",
        },
        "errors": [],
        "speakers": {
            "anonymous_ids": ["SPEAKER_00"],
            "display_names": {"SPEAKER_00": "SPEAKER_00"},
        },
        "segments": [
            {
                "start_ms": 0,
                "end_ms": 1000,
                "text": "Fake class transcript",
                "speaker": "SPEAKER_00",
                "words": [],
            }
        ],
    }
    json_path = staging / "transcript.json"
    json_path.write_text(json.dumps(payload, sort_keys=True) + "\n")
    markdown_path = staging / "transcript.md"
    markdown_path.write_text(
        "[00:00:00–00:00:01] **SPEAKER_00:** Fake class transcript.\n"
    )
    return {
        "transcript_json": {
            "path": str(json_path),
            "sha256": hashlib.sha256(json_path.read_bytes()).hexdigest(),
        },
        "transcript_markdown": {
            "path": str(markdown_path),
            "sha256": hashlib.sha256(markdown_path.read_bytes()).hexdigest(),
        },
    }


def run_job(message: dict[str, object]) -> bool:
    job_id = str(message["id"])
    params = message.get("params", {})
    if not isinstance(params, dict):
        return False
    mode = Path(str(params["source_path"])).stem
    staging = Path(str(params["staging_dir"]))
    staging.mkdir(parents=True, exist_ok=True)
    sys.stderr.write(f"fake worker log for {job_id}\n")
    sys.stderr.flush()

    if mode == "stdout-noise":
        sys.stdout.write("loading models...\n")
        sys.stdout.flush()
        return True
    if mode == "malformed":
        sys.stdout.write('{"type":"event"}\n')
        sys.stdout.flush()
        return True
    if mode == "eof":
        return False
    if mode == "die":
        os._exit(23)
    if mode == "error":
        phase_event(job_id, "asr", "failed")
        send(
            {
                "type": "response",
                "id": job_id,
                "ok": False,
                "error": {"code": "asr_failed", "message": "boom"},
            }
        )
        return True
    if mode in {"hang", "hang-ignore-term", "hang-with-child"}:
        if mode == "hang-ignore-term":
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        if mode == "hang-with-child":
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)",
                ]
            )
            (staging / "child.pid").write_text(str(child.pid))
        phase_event(job_id, "asr", "started")
        while True:
            time.sleep(1)

    artifacts: dict[str, dict[str, str]] = {}
    phases = ["asr", "alignment", "diarization"]
    for phase in phases:
        if mode == "partial" and phase == "alignment":
            phase_event(job_id, phase, "failed")
            send(
                {
                    "type": "response",
                    "id": job_id,
                    "ok": True,
                    "result": {
                        "status": "partial",
                        "assigned": False,
                        "completed_phases": list(artifacts),
                        "failed_phases": [phase],
                        "error": "alignment failed",
                        "pipeline_signature": SIGNATURE,
                        "profile": {"device": "cpu", "compute_type": "int8"},
                        "artifacts": artifacts,
                    },
                }
            )
            return True
        phase_event(job_id, phase, "started")
        artifacts[phase] = artifact(
            job_id,
            phase,
            staging,
            corrupt=mode == "corrupt-artifact" and phase == phases[0],
        )
        phase_event(job_id, phase, "completed")
        if mode == "die-after-asr" and phase == "asr":
            os._exit(24)
    artifacts.update(transcript_artifacts(job_id, staging, params))
    send(
        {
            "type": "response",
            "id": job_id,
            "ok": True,
            "result": {
                "status": "complete",
                "assigned": False,
                "completed_phases": phases,
                "failed_phases": [],
                "error": None,
                "pipeline_signature": SIGNATURE,
                "profile": {"device": "cpu", "compute_type": "int8"},
                "artifacts": artifacts,
            },
        }
    )
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--handshake", default="normal")
    args = parser.parse_args()
    if not handshake(args.handshake):
        return 0
    while message := receive():
        if message.get("method") == "shutdown":
            return 0
        if not run_job(message):
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
