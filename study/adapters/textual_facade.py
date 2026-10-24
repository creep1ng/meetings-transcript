"""Concrete async bridge from Textual intents to academic use cases."""

from __future__ import annotations

import asyncio
import json
import mimetypes
from pathlib import Path
from typing import Mapping

from study.adapters.filesystem.manifest_repository import ManifestRepository
from study.adapters.persistence.job_store import AcademicJobStore
from study.adapters.system import LocalClock, LocalFileHasher, LocalFileOpener
from study.adapters.transcription.artifacts import (
    atomic_write_bytes,
    render_markdown,
    write_json_artifact,
)
from study.application.dto import (
    BrowseSourcesQuery,
    CreateCourseCommand,
    EnsurePackageCommand,
    ListPackagesQuery,
    PackageDTO,
)
from study.application.ports.commit import (
    ClassPackageManifest,
    CommitAssetsCommand,
    ConflictResolution,
    FileAsset,
    LinkAsset,
    PlannedFile,
    normalize_link_url,
)
from study.application.ports.jobs import AcademicJob, JobCheckpoint, JobStatus
from study.application.use_cases.browse_sources import BrowseSources
from study.application.use_cases.commit_assets import CommitAssets
from study.application.use_cases.manage_courses import ManageCourses
from study.application.use_cases.rename_speakers import rename_speakers
from study.application.use_cases.transcribe_media import (
    TranscribeMedia,
    TranscribeMediaCommand,
)
from study.domain.policies import classify_asset, prefixed_asset_name
from study.domain.value_objects import ClassDate, CourseName
from study.presentation.textual.ports import MoveConflict, MoveConflictError, MoveResult
from study.presentation.textual.state import (
    CourseItem,
    JobItem,
    PackageItem,
    SourceCatalog,
    SourceItem,
    SpeakerItem,
)


class TextualStudyFacade:
    """Translate presentation records while keeping I/O off Textual's loop."""

    def __init__(
        self,
        *,
        source_root: str,
        library_root: str,
        source_browser: BrowseSources,
        courses: ManageCourses,
        manifests: ManifestRepository,
        commits: CommitAssets,
        jobs: AcademicJobStore,
        transcriptions: TranscribeMedia,
        transcriber: object,
        pipeline_signature: str,
        language: str,
        opener: LocalFileOpener | None = None,
        hasher: LocalFileHasher | None = None,
        clock: LocalClock | None = None,
    ) -> None:
        self.source_root = Path(source_root).resolve(strict=True)
        self.library_root = Path(library_root).resolve(strict=True)
        self.source_browser = source_browser
        self.courses = courses
        self.manifests = manifests
        self.commits = commits
        self.jobs = jobs
        self.transcriptions = transcriptions
        self.transcriber = transcriber
        self.pipeline_signature = pipeline_signature
        self.language = language
        self.opener = opener or LocalFileOpener()
        self.hasher = hasher or LocalFileHasher()
        self.clock = clock or LocalClock()
        self._assignments: dict[str, tuple[str, str]] = {}
        self._closed = False

    async def browse_source(self, location: str | None) -> SourceCatalog:
        return await asyncio.to_thread(self._browse_source, location)

    def _browse_source(self, location: str | None) -> SourceCatalog:
        directory = self.source_browser.execute(BrowseSourcesQuery(location))
        return SourceCatalog(
            directory.location,
            directory.parent_location,
            tuple(
                SourceItem(
                    entry.location,
                    entry.name,
                    is_directory=entry.is_directory,
                    kind=entry.asset_kind,
                    size_bytes=entry.size_bytes,
                )
                for entry in directory.entries
            ),
        )

    async def list_courses(self) -> tuple[CourseItem, ...]:
        return await asyncio.to_thread(self._list_courses)

    def _list_courses(self) -> tuple[CourseItem, ...]:
        return tuple(
            CourseItem(
                course.name,
                course.package_count,
                str(self.library_root / course.name),
            )
            for course in self.courses.list_courses()
        )

    async def list_packages(self, course_name: str) -> tuple[PackageItem, ...]:
        return await asyncio.to_thread(self._list_packages, course_name)

    def _list_packages(self, course_name: str) -> tuple[PackageItem, ...]:
        return tuple(
            self._package_item(package)
            for package in self.courses.list_packages(ListPackagesQuery(course_name))
        )

    async def list_jobs(self) -> tuple[JobItem, ...]:
        return await asyncio.to_thread(self._list_jobs)

    def _list_jobs(self) -> tuple[JobItem, ...]:
        jobs = self.jobs.list_jobs(include_invalidated=True)
        for job in jobs:
            if job.assigned and job.course and job.class_date:
                self._sync_transcript_assignment(job)
        jobs = self.jobs.list_jobs(include_invalidated=True)
        total = max(1, len(jobs))
        return tuple(
            self._job_item(job, position, total)
            for position, job in enumerate(jobs, start=1)
        )

    def _job_item(self, job: AcademicJob, position: int, total: int) -> JobItem:
        active = job.status in {JobStatus.PENDING, JobStatus.RUNNING}
        retained = self.jobs.staging_usage(job.id) > 0
        speakers: tuple[SpeakerItem, ...] = ()
        transcript_json = self._checkpoint(job, "transcript_json")
        if transcript_json is not None:
            try:
                payload = json.loads(Path(transcript_json.artifact_path).read_text())
                anonymous = payload["speakers"]["anonymous_ids"]
                names = payload["speakers"]["display_names"]
                speakers = tuple(
                    SpeakerItem(identifier, names.get(identifier, identifier))
                    for identifier in anonymous
                )
            except (OSError, KeyError, TypeError, json.JSONDecodeError):
                speakers = ()
        return JobItem(
            job.id,
            Path(job.source_path).name,
            job.current_phase or job.status.value,
            position,
            total,
            job.status.value,
            active=active,
            recoverable=job.is_recoverable or retained,
            speakers=speakers,
            source_location=self._source_location(job.source_path),
            generated_assets=self._generated_assets(job),
        )

    async def create_course(self, name: str) -> CourseItem:
        return await asyncio.to_thread(self._create_course, name)

    def _create_course(self, name: str) -> CourseItem:
        course = self.courses.create_course(CreateCourseCommand(name))
        return CourseItem(course.name, location=str(self.library_root / course.name))

    async def ensure_package(self, course_name: str, class_date: str) -> PackageItem:
        return await asyncio.to_thread(self._ensure_package, course_name, class_date)

    def _ensure_package(self, course_name: str, class_date: str) -> PackageItem:
        package = self.courses.ensure_package(
            EnsurePackageCommand(course_name, class_date)
        )
        return self._package_item(package)

    def _package_item(self, package: PackageDTO) -> PackageItem:
        package_path = self.library_root / package.course_name / package.class_date
        asset_names = [asset.name for asset in package.assets]
        if package.has_manifest:
            snapshot = self.manifests.read(str(package_path))
            if snapshot.manifest is not None:
                asset_names.extend(
                    f"{asset.label} [link] {asset.url}"
                    for asset in snapshot.manifest.assets
                    if isinstance(asset, LinkAsset)
                )
        return PackageItem(
            package.course_name,
            package.class_date,
            str(package_path),
            tuple(asset_names),
            package.has_manifest,
            package.adopted,
        )

    async def dispatch_transcriptions(
        self, locations: tuple[str, ...], language: str
    ) -> tuple[JobItem, ...]:
        await asyncio.to_thread(self._dispatch_transcriptions, locations, language)
        return await self.list_jobs()

    def _dispatch_transcriptions(
        self, locations: tuple[str, ...], language: str
    ) -> None:
        for location in locations:
            source = self._source_path(location)
            kind = classify_asset(source.name).value
            if kind not in {"audio", "video"}:
                raise ValueError(f"Only audio/video can be transcribed: {source.name}")
            source_hash = self._stable_hash(source)
            assignment = self._assignment_for(source)
            self.transcriptions.execute(
                TranscribeMediaCommand(
                    source_path=str(source),
                    source_sha256=source_hash,
                    pipeline_signature=self.pipeline_signature,
                    assigned=assignment is not None,
                    requested_language=language,
                    media_kind=kind,
                    assignment=(
                        {"course": assignment[0], "class_date": assignment[1]}
                        if assignment
                        else None
                    ),
                )
            )

    async def assign_sources(
        self, locations: tuple[str, ...], course_name: str, class_date: str
    ) -> None:
        await asyncio.to_thread(
            self._assign_sources, locations, course_name, class_date
        )

    def _assign_sources(
        self, locations: tuple[str, ...], course_name: str, class_date: str
    ) -> None:
        course = CourseName(course_name).value
        class_day = ClassDate.parse(class_date).iso
        jobs = self.jobs.list_jobs(include_invalidated=True)
        for location in locations:
            source = self._source_path(location)
            self._assignments[str(source)] = (course, class_day)
            for job in jobs:
                if Path(job.source_path) == source and not job.invalidated:
                    assigned = self.jobs.set_assignment(job.id, course, class_day)
                    self._sync_transcript_assignment(assigned)

    async def move_sources(
        self,
        locations: tuple[str, ...],
        course_name: str,
        class_date: str,
        resolutions: Mapping[str, str] | None = None,
        expected_generated_assets: Mapping[str, tuple[str, ...]] | None = None,
    ) -> MoveResult:
        return await asyncio.to_thread(
            self._move_sources,
            locations,
            course_name,
            class_date,
            dict(resolutions or {}),
            dict(expected_generated_assets or {}),
        )

    def _move_sources(
        self,
        locations: tuple[str, ...],
        course_name: str,
        class_date: str,
        resolutions: Mapping[str, str],
        expected_generated_assets: Mapping[str, tuple[str, ...]],
    ) -> MoveResult:
        course = CourseName(course_name).value
        class_day = ClassDate.parse(class_date).iso
        package = self.library_root / course / class_day
        self.courses.ensure_package(EnsurePackageCommand(course, class_day))
        snapshot = self.manifests.read(str(package))
        base = snapshot.manifest
        discovered = self._discover_unindexed(package, base)
        existing_names = {
            child.name.casefold(): child
            for child in package.iterdir()
            if child.is_file()
        }
        reserved_names: dict[str, tuple[str, int, str]] = {}
        planned: list[PlannedFile] = []
        moved_jobs: list[tuple[AcademicJob, str]] = []
        source_paths: dict[str, str] = {}

        for location in locations:
            source = self._source_path(location)
            source_paths[location] = str(source)
            active_job = self._job_for_source(source)
            actual_generated_assets = (
                self._generated_assets(active_job) if active_job is not None else ()
            )
            if (
                location in expected_generated_assets
                and actual_generated_assets != expected_generated_assets[location]
            ):
                raise RuntimeError(
                    f"Generated assets changed after review: {source.name}"
                )
            if active_job and active_job.status in {
                JobStatus.PENDING,
                JobStatus.RUNNING,
            }:
                raise RuntimeError(
                    f"Cannot move {source.name} while transcription is active"
                )
            source_hash = self._stable_hash(source)
            if active_job and active_job.source_sha256 != source_hash:
                raise RuntimeError(
                    f"Cannot publish stale transcript artifacts for {source.name}"
                )
            original_name = prefixed_asset_name(
                ClassDate.parse(class_day), source.name
            ).value
            original_name, conflict, expected = self._resolve_destination(
                package,
                original_name,
                source_hash,
                source.stat().st_size,
                resolutions,
                location,
                existing_names,
                reserved_names,
            )
            original_asset = self._file_asset(
                source,
                source_hash,
                original_name,
                status="ready",
            )
            planned.append(
                PlannedFile(
                    original_asset,
                    str(source),
                    conflict=conflict,
                    expected_destination_sha256=expected,
                    delete_source=True,
                )
            )
            if conflict == ConflictResolution.SKIP:
                continue

            if active_job:
                if active_job.course != course or active_job.class_date != class_day:
                    active_job = self.jobs.set_assignment(
                        active_job.id, course, class_day
                    )
                self._sync_transcript_assignment(active_job)
                derivative_status = (
                    "partial" if active_job.status is JobStatus.PARTIAL else "ready"
                )
                stem = Path(original_name).stem
                for checkpoint_name, suffix, media_type in (
                    ("transcript_json", ".transcript.json", "application/json"),
                    ("transcript_markdown", ".transcript.md", "text/markdown"),
                ):
                    checkpoint = self._checkpoint(active_job, checkpoint_name)
                    if checkpoint is None:
                        continue
                    derivative = Path(checkpoint.artifact_path)
                    derivative_name = f"{stem}{suffix}"
                    derivative_name, derivative_conflict, derivative_expected = (
                        self._resolve_destination(
                            package,
                            derivative_name,
                            checkpoint.artifact_sha256,
                            derivative.stat().st_size,
                            resolutions,
                            location,
                            existing_names,
                            reserved_names,
                        )
                    )
                    asset = self._file_asset(
                        derivative,
                        checkpoint.artifact_sha256,
                        derivative_name,
                        status=derivative_status,
                        media_type=media_type,
                        derived_from=original_asset.id,
                        original_filename=derivative.name,
                    )
                    planned.append(
                        PlannedFile(
                            asset,
                            str(derivative),
                            conflict=derivative_conflict,
                            expected_destination_sha256=derivative_expected,
                            delete_source=False,
                        )
                    )
                moved_jobs.append((active_job, str(source)))

        command = CommitAssetsCommand(
            package_dir=str(package),
            course=course,
            class_date=class_day,
            updated_at=self.clock.now_utc().isoformat().replace("+00:00", "Z"),
            expected_manifest_fingerprint=snapshot.fingerprint,
            base_manifest=base,
            discovered_assets=discovered,
            files=tuple(planned),
        )
        result = self.commits.execute(command)
        move_sources = {item.source_path for item in planned if item.delete_source}
        deleted = set(result.deleted_sources) & move_sources
        skipped = {
            item.source_path
            for item in planned
            if item.delete_source and item.conflict is ConflictResolution.SKIP
        }
        retained = (move_sources - deleted - skipped) | (
            set(result.retained_sources) & move_sources
        )
        for job, source_path in moved_jobs:
            if source_path in deleted and job.status is JobStatus.READY_TO_COMMIT:
                self.jobs.complete(job.id)
        return MoveResult(
            moved_locations=self._locations_for_paths(locations, source_paths, deleted),
            retained_locations=self._locations_for_paths(
                locations, source_paths, retained
            ),
            skipped_locations=self._locations_for_paths(
                locations, source_paths, skipped
            ),
        )

    def _discover_unindexed(
        self, package: Path, base: ClassPackageManifest | None
    ) -> tuple[FileAsset, ...]:
        indexed = {
            asset.filename.casefold()
            for asset in (base.assets if base else ())
            if isinstance(asset, FileAsset)
        }
        discovered: list[FileAsset] = []
        for child in package.iterdir():
            if (
                not child.is_file()
                or child.is_symlink()
                or child.name == "class-package.json"
                or "conflicted copy" in child.name.casefold()
                or child.name.startswith(".study-")
                or child.name.casefold() in indexed
            ):
                continue
            digest = self._stable_hash(child)
            discovered.append(
                self._file_asset(
                    child,
                    digest,
                    child.name,
                    status="adopted",
                    original_filename=child.name,
                )
            )
        return tuple(discovered)

    def _resolve_destination(
        self,
        package: Path,
        filename: str,
        incoming_sha256: str,
        incoming_size: int,
        resolutions: Mapping[str, str],
        location: str,
        existing_names: dict[str, Path],
        reserved_names: dict[str, tuple[str, int, str]],
    ) -> tuple[str, ConflictResolution | None, str | None]:
        destination = existing_names.get(filename.casefold())
        if destination is None:
            reserved = reserved_names.get(filename.casefold())
            if reserved is None:
                reserved_names[filename.casefold()] = (
                    incoming_sha256,
                    incoming_size,
                    filename,
                )
                return filename, None, None
            reserved_hash, reserved_size, reserved_name = reserved
            if reserved_hash == incoming_sha256 and reserved_size == incoming_size:
                return reserved_name, None, None
            resolution_key = self._resolution_key(
                location, filename, reserved_hash, incoming_sha256
            )
            decision = resolutions.get(resolution_key)
            if decision is None:
                raise MoveConflictError(
                    MoveConflict(
                        resolution_key, filename, reserved_hash, incoming_sha256
                    )
                )
            resolution = ConflictResolution(decision)
            if resolution is not ConflictResolution.KEEP_BOTH:
                if resolution in {ConflictResolution.SKIP, ConflictResolution.CANCEL}:
                    return filename, resolution, reserved_hash
                raise ValueError(
                    "An intra-batch collision must use keep-both, skip, or cancel"
                )
            original = Path(filename)
            number = 2
            while True:
                candidate = f"{original.stem} ({number}){original.suffix}"
                key = candidate.casefold()
                if key not in existing_names and key not in reserved_names:
                    reserved_names[key] = (incoming_sha256, incoming_size, candidate)
                    return candidate, resolution, None
                number += 1
        existing_hash = self._stable_hash(destination)
        if (
            destination.stat().st_size == incoming_size
            and existing_hash == incoming_sha256
        ):
            return destination.name, None, None
        resolution_key = self._resolution_key(
            location, filename, existing_hash, incoming_sha256
        )
        decision = resolutions.get(resolution_key)
        if decision is None:
            raise MoveConflictError(
                MoveConflict(resolution_key, filename, existing_hash, incoming_sha256)
            )
        try:
            resolution = ConflictResolution(decision)
        except ValueError as error:
            raise ValueError(f"Unsupported conflict resolution: {decision}") from error
        if resolution is ConflictResolution.KEEP_BOTH:
            original = Path(filename)
            number = 2
            while True:
                candidate = f"{original.stem} ({number}){original.suffix}"
                if (
                    candidate.casefold() not in existing_names
                    and candidate.casefold() not in reserved_names
                ):
                    reserved_names[candidate.casefold()] = (
                        incoming_sha256,
                        incoming_size,
                        candidate,
                    )
                    return candidate, resolution, None
                number += 1
        if resolution is ConflictResolution.REPLACE:
            return destination.name, resolution, existing_hash
        return destination.name, resolution, existing_hash

    def _file_asset(
        self,
        source: Path,
        digest: str,
        filename: str,
        *,
        status: str,
        media_type: str | None = None,
        derived_from: str | None = None,
        original_filename: str | None = None,
    ) -> FileAsset:
        identifier = f"file-{self._id_digest(filename, digest)}"
        return FileAsset(
            id=identifier,
            filename=filename,
            kind=classify_asset(filename).value,
            media_type=media_type
            or mimetypes.guess_type(filename)[0]
            or "application/octet-stream",
            size=source.stat().st_size,
            sha256=digest,
            original_filename=original_filename or source.name,
            status=status,
            derived_from=derived_from,
        )

    @staticmethod
    def _id_digest(filename: str, digest: str) -> str:
        import hashlib

        return hashlib.sha256(f"{filename.casefold()}\0{digest}".encode()).hexdigest()[
            :24
        ]

    @staticmethod
    def _resolution_key(
        location: str,
        filename: str,
        existing_sha256: str,
        incoming_sha256: str,
    ) -> str:
        return json.dumps(
            (location, filename, existing_sha256, incoming_sha256),
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @staticmethod
    def _locations_for_paths(
        locations: tuple[str, ...],
        source_paths: Mapping[str, str],
        paths: set[str],
    ) -> tuple[str, ...]:
        return tuple(
            location for location in locations if source_paths.get(location) in paths
        )

    def _generated_assets(self, job: AcademicJob) -> tuple[str, ...]:
        if job.invalidated:
            return ()
        return tuple(
            label
            for phase, label in (
                ("transcript_json", "transcript JSON"),
                ("transcript_markdown", "transcript Markdown"),
            )
            if self._checkpoint(job, phase) is not None
        )

    async def rename_speakers(self, job_id: str, names: Mapping[str, str]) -> None:
        await asyncio.to_thread(self._rename_speakers, job_id, dict(names))

    def _rename_speakers(self, job_id: str, names: Mapping[str, str]) -> None:
        job = self.jobs.get_job(job_id)
        if job is None:
            raise LookupError(job_id)
        checkpoint = self._checkpoint(job, "transcript_json")
        markdown_checkpoint = self._checkpoint(job, "transcript_markdown")
        if checkpoint is None or markdown_checkpoint is None:
            raise RuntimeError("Transcript artifacts are not available")
        payload = json.loads(Path(checkpoint.artifact_path).read_text(encoding="utf-8"))
        renamed = rename_speakers(payload, names, render_markdown=render_markdown)
        json_ref = write_json_artifact(checkpoint.artifact_path, renamed.transcript)
        markdown_ref = atomic_write_bytes(
            markdown_checkpoint.artifact_path, renamed.markdown.encode("utf-8")
        )
        for phase, reference in (
            ("transcript_json", json_ref),
            ("transcript_markdown", markdown_ref),
        ):
            self.jobs.record_checkpoint(
                job.id, JobCheckpoint(phase, reference.path, reference.sha256)
            )

    async def open_path(self, location: str) -> None:
        await asyncio.to_thread(self.opener.open, str(self._runtime_path(location)))

    async def add_link(
        self, url: str, label: str, course_name: str, class_date: str
    ) -> None:
        await asyncio.to_thread(self._add_link, url, label, course_name, class_date)

    def _add_link(
        self, url: str, label: str, course_name: str, class_date: str
    ) -> None:
        course = CourseName(course_name).value
        class_day = ClassDate.parse(class_date).iso
        package = self.library_root / course / class_day
        self.courses.ensure_package(EnsurePackageCommand(course, class_day))
        snapshot = self.manifests.read(str(package))
        normalized_url = normalize_link_url(url)
        link = LinkAsset(
            id=f"link-{self._id_digest(normalized_url, normalized_url)}",
            url=normalized_url,
            label=label.strip(),
        )
        self.commits.execute(
            CommitAssetsCommand(
                package_dir=str(package),
                course=course,
                class_date=class_day,
                updated_at=self.clock.now_utc().isoformat().replace("+00:00", "Z"),
                expected_manifest_fingerprint=snapshot.fingerprint,
                base_manifest=snapshot.manifest,
                discovered_assets=self._discover_unindexed(package, snapshot.manifest),
                links=(link,),
            )
        )

    async def retry_job(self, job_id: str) -> None:
        await asyncio.to_thread(self._retry_job, job_id)

    def _retry_job(self, job_id: str) -> None:
        job = self.jobs.get_job(job_id)
        if job is None:
            raise LookupError(job_id)
        self.transcriptions.execute(
            TranscribeMediaCommand(
                source_path=job.source_path,
                source_sha256=job.source_sha256,
                pipeline_signature=job.pipeline_signature,
                assigned=job.assigned,
                requested_language=self.language,
                media_kind=classify_asset(job.source_path).value,
                assignment=(
                    {"course": job.course, "class_date": job.class_date}
                    if job.course and job.class_date
                    else None
                ),
            )
        )

    async def discard_job(self, job_id: str) -> None:
        await asyncio.to_thread(self.jobs.discard, job_id)

    async def close(self, *, cancel_active: bool) -> None:
        if self._closed:
            return
        self._closed = True
        if cancel_active:
            for job in self.jobs.list_jobs(
                {JobStatus.PENDING, JobStatus.RUNNING}, include_invalidated=True
            ):
                self.transcriptions.cancel(job.id)
        await asyncio.to_thread(self.transcriber.close)
        await asyncio.to_thread(self.jobs.close)

    def _source_path(self, location: str) -> Path:
        lexical = self.source_root / location
        if lexical.is_symlink():
            raise ValueError("Symlinked files are not valid move/transcription sources")
        candidate = lexical.resolve(strict=True)
        if not candidate.is_file() or not candidate.is_relative_to(self.source_root):
            raise ValueError("Source is not a root-confined regular file")
        if candidate.is_relative_to(self.library_root):
            raise ValueError("Managed library files cannot be move sources")
        return candidate

    def _source_location(self, source_path: str) -> str:
        try:
            return Path(source_path).relative_to(self.source_root).as_posix()
        except ValueError:
            return source_path

    def _runtime_path(self, location: str) -> Path:
        candidate = Path(location)
        if candidate.is_absolute():
            resolved = candidate.resolve(strict=True)
            if not (
                resolved.is_relative_to(self.source_root)
                or resolved.is_relative_to(self.library_root)
            ):
                raise ValueError("Location is outside configured roots")
            return resolved
        resolved = (self.source_root / location).resolve(strict=True)
        if not resolved.is_relative_to(self.source_root) or resolved.is_relative_to(
            self.library_root
        ):
            raise ValueError("Location is outside configured roots")
        return resolved

    def _stable_hash(self, path: Path) -> str:
        before = path.stat()
        digest = self.hasher.sha256(str(path))
        after = path.stat()
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )
        identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if identity_before != identity_after:
            raise RuntimeError(f"File changed while hashing: {path.name}")
        return digest

    def _assignment_for(self, source: Path) -> tuple[str, str] | None:
        assignment = self._assignments.get(str(source))
        if assignment:
            return assignment
        job = self._job_for_source(source)
        if job and job.course and job.class_date:
            return job.course, job.class_date
        return None

    def _job_for_source(self, source: Path) -> AcademicJob | None:
        matches = [
            job
            for job in self.jobs.list_jobs(include_invalidated=True)
            if not job.invalidated and Path(job.source_path) == source
        ]
        return max(matches, key=lambda item: item.updated_at, default=None)

    def _sync_transcript_assignment(self, job: AcademicJob) -> None:
        if not job.course or not job.class_date:
            return
        json_checkpoint = self._checkpoint(job, "transcript_json")
        markdown_checkpoint = self._checkpoint(job, "transcript_markdown")
        if json_checkpoint is None or markdown_checkpoint is None:
            return
        try:
            payload = json.loads(
                Path(json_checkpoint.artifact_path).read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError):
            return
        assignment = {"course": job.course, "class_date": job.class_date}
        if payload.get("assignment") == assignment:
            return
        payload["assignment"] = assignment
        json_ref = write_json_artifact(json_checkpoint.artifact_path, payload)
        markdown_ref = atomic_write_bytes(
            markdown_checkpoint.artifact_path, render_markdown(payload).encode("utf-8")
        )
        self.jobs.record_checkpoint(
            job.id,
            JobCheckpoint("transcript_json", json_ref.path, json_ref.sha256),
        )
        self.jobs.record_checkpoint(
            job.id,
            JobCheckpoint(
                "transcript_markdown", markdown_ref.path, markdown_ref.sha256
            ),
        )

    @staticmethod
    def _checkpoint(job: AcademicJob, phase: str) -> JobCheckpoint | None:
        return next(
            (checkpoint for checkpoint in job.checkpoints if checkpoint.phase == phase),
            None,
        )
