"""Validated environment configuration for the academic workflow."""

import os
import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

LANGUAGES = frozenset({"auto", "en", "es"})
DEVICES = frozenset({"auto", "cpu", "cuda"})
COMPUTE_TYPES = frozenset(
    {
        "auto",
        "default",
        "float16",
        "float32",
        "int8",
        "int8_float16",
        "int8_float32",
    }
)


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _directory(value: str, variable: str) -> str:
    try:
        path = Path(value).expanduser().resolve(strict=True)
    except (FileNotFoundError, RuntimeError) as error:
        raise ValueError(f"{variable} must point to an existing directory") from error
    if not path.is_dir():
        raise ValueError(f"{variable} must point to a directory")
    return str(path)


def _optional_path(value: str | None) -> str | None:
    if not value:
        return None
    return str(Path(value).expanduser().resolve(strict=False))


def _boolean(value: str | None, variable: str, default: bool = False) -> bool:
    if value is None:
        return default
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{variable} must be a boolean")


@dataclass(frozen=True, slots=True)
class AcademicConfig:
    library_root: str
    source_root: str
    start_path: str
    state_root: str
    language: str = "es"
    model: str = "large-v3"
    device: str = "auto"
    compute_type: str = "auto"
    worker_command: tuple[str, ...] | None = None
    show_hidden: bool = False
    include_incomplete: bool = False
    local_files_only: bool = True
    model_cache: str | None = None
    hf_token: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        library_root = _directory(self.library_root, "ACADEMIC_LIBRARY_ROOT")
        source_root = _directory(self.source_root, "ACADEMIC_SOURCE_ROOT")
        start_path = _directory(self.start_path, "ACADEMIC_START_PATH")
        state_root = str(Path(self.state_root).expanduser().resolve(strict=False))
        library = Path(library_root)
        source = Path(source_root)
        start = Path(start_path)
        if _is_within(source, library):
            raise ValueError("ACADEMIC_SOURCE_ROOT cannot be inside the library")
        if not _is_within(start, source):
            raise ValueError("ACADEMIC_START_PATH must be inside ACADEMIC_SOURCE_ROOT")
        if _is_within(start, library):
            raise ValueError("ACADEMIC_START_PATH cannot be inside the library")

        language = self.language.strip().casefold()
        device = self.device.strip().casefold()
        compute_type = self.compute_type.strip().casefold()
        model = self.model.strip()
        if language not in LANGUAGES:
            raise ValueError("ACADEMIC_LANGUAGE must be one of: auto, en, es")
        if device not in DEVICES:
            raise ValueError("ACADEMIC_DEVICE must be one of: auto, cpu, cuda")
        if compute_type not in COMPUTE_TYPES:
            raise ValueError(
                "ACADEMIC_COMPUTE_TYPE must be a supported WhisperX compute type"
            )
        if not model:
            raise ValueError("ACADEMIC_MODEL cannot be empty")
        if self.worker_command is not None and not self.worker_command:
            raise ValueError("ACADEMIC_WORKER_COMMAND cannot be empty")
        if (
            not isinstance(self.show_hidden, bool)
            or not isinstance(self.include_incomplete, bool)
            or not isinstance(self.local_files_only, bool)
        ):
            raise ValueError("academic file filter options must be booleans")

        object.__setattr__(self, "library_root", library_root)
        object.__setattr__(self, "source_root", source_root)
        object.__setattr__(self, "start_path", start_path)
        object.__setattr__(self, "state_root", state_root)
        object.__setattr__(self, "language", language)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "device", device)
        object.__setattr__(self, "compute_type", compute_type)
        object.__setattr__(self, "model_cache", _optional_path(self.model_cache))

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "AcademicConfig":
        env = os.environ if environ is None else environ
        home = Path(env.get("HOME", str(Path.home()))).expanduser()
        library_root = env.get("ACADEMIC_LIBRARY_ROOT", "").strip()
        if not library_root:
            raise ValueError("ACADEMIC_LIBRARY_ROOT is required")
        source_root = env.get("ACADEMIC_SOURCE_ROOT", str(home))
        start_path = env.get("ACADEMIC_START_PATH", str(home / "Downloads"))
        xdg_state = env.get("XDG_STATE_HOME")
        default_state = (
            (Path(xdg_state) if xdg_state else home / ".local" / "state")
            / "meetings-transcript"
            / "academic"
        )
        worker_value = env.get("ACADEMIC_WORKER_COMMAND")
        worker_command = tuple(shlex.split(worker_value)) if worker_value else None
        return cls(
            library_root=library_root,
            source_root=source_root,
            start_path=start_path,
            state_root=env.get("ACADEMIC_STATE_ROOT", str(default_state)),
            language=env.get("ACADEMIC_LANGUAGE", "es"),
            model=env.get("ACADEMIC_MODEL", "large-v3"),
            device=env.get("ACADEMIC_DEVICE", "auto"),
            compute_type=env.get("ACADEMIC_COMPUTE_TYPE", "auto"),
            worker_command=worker_command,
            show_hidden=_boolean(
                env.get("ACADEMIC_SHOW_HIDDEN"), "ACADEMIC_SHOW_HIDDEN"
            ),
            include_incomplete=_boolean(
                env.get("ACADEMIC_INCLUDE_INCOMPLETE"),
                "ACADEMIC_INCLUDE_INCOMPLETE",
            ),
            local_files_only=_boolean(
                env.get("ACADEMIC_LOCAL_FILES_ONLY"),
                "ACADEMIC_LOCAL_FILES_ONLY",
                default=True,
            ),
            model_cache=env.get("ACADEMIC_MODEL_CACHE"),
            hf_token=env.get("HF_TOKEN") or None,
        )


def load_academic_config(
    environ: Mapping[str, str] | None = None,
) -> AcademicConfig:
    return AcademicConfig.from_env(environ)


def load_academic_config_file(
    env_path: str,
    environ: Mapping[str, str] | None = None,
) -> AcademicConfig:
    """Load academic values with the legacy env-file precedence and syntax."""

    values = dict(os.environ if environ is None else environ)
    path = Path(env_path)
    if path.exists():
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return AcademicConfig.from_env(values)
