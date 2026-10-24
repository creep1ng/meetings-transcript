"""Focused tests for study domain invariants and pure policies."""

from dataclasses import FrozenInstanceError
from datetime import datetime

import pytest

from study.domain.errors import (
    InvalidClassDateError,
    InvalidLocationError,
    InvalidNameError,
    InvalidUrlError,
)
from study.domain.models import AssetKind
from study.domain.policies import (
    classify_asset,
    is_hidden_name,
    is_incomplete_name,
    normalize_url,
    prefixed_asset_name,
    suggest_class_date,
    suggest_course_name,
)
from study.domain.value_objects import (
    AssetFilename,
    ClassDate,
    CourseName,
    RelativeLocation,
    Sha256,
)


def test_class_date_accepts_only_strict_iso_calendar_dates():
    assert ClassDate.parse("2026-08-17").iso == "2026-08-17"
    for invalid in ("20260817", "2026-8-17", "2026-02-30", "2026-W34-1"):
        with pytest.raises(InvalidClassDateError):
            ClassDate.parse(invalid)
    with pytest.raises(InvalidClassDateError):
        ClassDate(datetime(2026, 8, 17))


def test_names_are_nfc_normalized_and_collide_by_casefold():
    decomposed = CourseName("Cafe\u0301")
    assert decomposed.value == "Café"
    assert CourseName("Straße").collision_key == CourseName("STRASSE").collision_key


@pytest.mark.parametrize(
    "name",
    ["", " Math", "Math ", ".hidden", "CON", "nul.txt", "Bad:Name", "Bad/Name"],
)
def test_course_names_reject_nonportable_segments(name):
    with pytest.raises(InvalidNameError):
        CourseName(name)


@pytest.mark.parametrize("name", ["lecture.", "a?b.pdf", "COM1.mp3", "bad\x00name"])
def test_asset_filenames_reject_windows_unsafe_names(name):
    with pytest.raises(InvalidNameError):
        AssetFilename(name)


def test_relative_locations_are_root_relative_and_immutable():
    location = RelativeLocation("Downloads/classes")
    assert location.parent == RelativeLocation("Downloads")
    assert location.child("lecture.mp3").value == "Downloads/classes/lecture.mp3"
    with pytest.raises(InvalidLocationError):
        RelativeLocation("../outside")
    with pytest.raises(InvalidLocationError):
        RelativeLocation("C:\\outside")
    with pytest.raises(FrozenInstanceError):
        location.value = "other"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("class.MP3", AssetKind.AUDIO),
        ("class.mkv", AssetKind.VIDEO),
        ("notes.pdf", AssetKind.DOCUMENT),
        ("slides.pptx", AssetKind.PRESENTATION),
        ("lab.ipynb", AssetKind.NOTEBOOK),
        ("data.unknown", AssetKind.OTHER),
        ("https://github.com/example/course", AssetKind.LINK),
    ],
)
def test_asset_classifier_is_deterministic(value, expected):
    assert classify_asset(value) is expected


def test_url_normalization_is_safe_and_does_not_fetch():
    assert (
        normalize_url("HTTPS://Example.COM:443/course?q=1")
        == "https://example.com/course?q=1"
    )
    with pytest.raises(InvalidUrlError):
        normalize_url("file:///tmp/lecture")
    with pytest.raises(InvalidUrlError):
        normalize_url("https://user:secret@example.com/course")
    with pytest.raises(InvalidUrlError):
        normalize_url("https://example.com/course?access_token=secret")
    with pytest.raises(InvalidUrlError):
        normalize_url("https://example.com/course?Key-Pair-Id=secret")


def test_iso_prefix_is_added_once_for_the_assigned_date():
    class_date = ClassDate.parse("2026-08-17")
    assert prefixed_asset_name(class_date, "notes.pdf").value == (
        "2026-08-17 - notes.pdf"
    )
    already_prefixed = AssetFilename("2026-08-17 - notes.pdf")
    assert prefixed_asset_name(class_date, already_prefixed) is already_prefixed


def test_suggestions_require_one_unambiguous_domain_match():
    assert suggest_class_date("Math 2026-08-17 notes") == ClassDate.parse("2026-08-17")
    assert suggest_class_date("2026-08-17 and 2026-08-18") is None
    courses = (CourseName("Math"), CourseName("Physics"))
    assert suggest_course_name("2026 Math lecture", courses) == CourseName("Math")
    assert suggest_course_name("Math and Physics", courses) is None


def test_default_filters_and_sha256_value():
    assert is_hidden_name(".notes")
    assert is_incomplete_name("lecture.mp4.crdownload")
    assert is_incomplete_name("notes.pdf~")
    digest = Sha256("A" * 64)
    assert digest.value == "a" * 64
