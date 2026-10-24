"""Focused tests for academic environment defaults and validation."""

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from study.config import AcademicConfig, load_academic_config


def _academic_env(tmp_path: Path) -> tuple[dict[str, str], Path, Path, Path]:
    home = tmp_path / "home"
    downloads = home / "Downloads"
    library = home / "Nextcloud" / "Academic"
    downloads.mkdir(parents=True)
    library.mkdir(parents=True)
    env = {"HOME": str(home), "ACADEMIC_LIBRARY_ROOT": str(library)}
    return env, home, downloads, library


def test_library_root_is_required():
    with pytest.raises(ValueError, match="ACADEMIC_LIBRARY_ROOT is required"):
        load_academic_config({})


def test_academic_defaults_are_resolved_from_home(tmp_path):
    env, home, downloads, library = _academic_env(tmp_path)
    config = load_academic_config(env)
    assert config.library_root == str(library.resolve())
    assert config.source_root == str(home.resolve())
    assert config.start_path == str(downloads.resolve())
    assert config.state_root == str(
        (home / ".local/state/meetings-transcript/academic").resolve()
    )
    assert (config.language, config.model) == ("es", "large-v3")
    assert (config.device, config.compute_type) == ("auto", "auto")
    assert not config.show_hidden
    assert not config.include_incomplete


def test_optional_environment_settings_are_parsed_and_immutable(tmp_path):
    env, _, _, _ = _academic_env(tmp_path)
    env.update(
        {
            "ACADEMIC_LANGUAGE": "EN",
            "ACADEMIC_DEVICE": "cuda",
            "ACADEMIC_COMPUTE_TYPE": "float16",
            "ACADEMIC_WORKER_COMMAND": "python -m fake_worker --test",
            "ACADEMIC_SHOW_HIDDEN": "yes",
            "ACADEMIC_INCLUDE_INCOMPLETE": "1",
            "ACADEMIC_MODEL_CACHE": str(tmp_path / "models"),
            "HF_TOKEN": "provisioning-token",
        }
    )
    config = load_academic_config(env)
    assert config.language == "en"
    assert config.worker_command == ("python", "-m", "fake_worker", "--test")
    assert config.show_hidden and config.include_incomplete
    assert config.model_cache == str((tmp_path / "models").resolve())
    assert config.hf_token == "provisioning-token"
    with pytest.raises(FrozenInstanceError):
        config.language = "es"


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("ACADEMIC_LANGUAGE", "fr"),
        ("ACADEMIC_DEVICE", "gpu"),
        ("ACADEMIC_COMPUTE_TYPE", "half"),
        ("ACADEMIC_SHOW_HIDDEN", "sometimes"),
        ("ACADEMIC_MODEL", "  "),
    ],
)
def test_invalid_academic_environment_values_fail_fast(tmp_path, variable, value):
    env, _, _, _ = _academic_env(tmp_path)
    env[variable] = value
    with pytest.raises(ValueError):
        load_academic_config(env)


def test_start_path_must_resolve_inside_source_and_outside_library(tmp_path):
    env, _, _, library = _academic_env(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    env["ACADEMIC_START_PATH"] = str(outside)
    with pytest.raises(ValueError, match="inside ACADEMIC_SOURCE_ROOT"):
        load_academic_config(env)

    env["ACADEMIC_START_PATH"] = str(library)
    with pytest.raises(ValueError, match="cannot be inside the library"):
        load_academic_config(env)


def test_start_path_symlink_escape_is_rejected(tmp_path):
    env, home, _, _ = _academic_env(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    escape = home / "escape"
    escape.symlink_to(outside, target_is_directory=True)
    env["ACADEMIC_START_PATH"] = str(escape)
    with pytest.raises(ValueError, match="inside ACADEMIC_SOURCE_ROOT"):
        load_academic_config(env)


def test_direct_config_rejects_source_root_inside_library(tmp_path):
    library = tmp_path / "library"
    source = library / "source"
    start = source / "Downloads"
    start.mkdir(parents=True)
    with pytest.raises(ValueError, match="SOURCE_ROOT cannot be inside"):
        AcademicConfig(
            library_root=str(library),
            source_root=str(source),
            start_path=str(start),
            state_root=str(tmp_path / "state"),
        )
