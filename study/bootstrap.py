"""Sole composition root for the local-first study workflow."""

from __future__ import annotations

from study.adapters.filesystem.library_repository import FilesystemLibraryRepository
from study.adapters.filesystem.manifest_repository import ManifestRepository
from study.adapters.filesystem.safe_committer import SafeCommitter
from study.adapters.filesystem.source_repository import FilesystemSourceRepository
from study.adapters.persistence.job_store import AcademicJobStore
from study.adapters.system import LocalFileOpener
from study.adapters.textual_facade import TextualStudyFacade
from study.adapters.transcription.supervisor import WorkerSupervisor
from study.application.use_cases.browse_sources import BrowseSources
from study.application.use_cases.commit_assets import CommitAssets
from study.application.use_cases.manage_courses import ManageCourses
from study.application.use_cases.transcribe_media import TranscribeMedia
from study.config import AcademicConfig, load_academic_config_file
from study.presentation.textual import OrganizerApp, OrganizerConfig
from study.worker.pipeline import PipelineConfig, WhisperXPipeline


def build_study_app(config: AcademicConfig) -> OrganizerApp:
    source_repository = FilesystemSourceRepository(
        config.source_root,
        config.library_root,
        start_path=config.start_path,
        show_hidden=config.show_hidden,
        include_incomplete=config.include_incomplete,
    )
    library_repository = FilesystemLibraryRepository(
        config.library_root,
        show_hidden=config.show_hidden,
        include_incomplete=config.include_incomplete,
    )
    manifests = ManifestRepository()
    committer = SafeCommitter(
        manifests,
        source_root=config.source_root,
        library_root=config.library_root,
        staging_root=config.state_root,
    )
    jobs = AcademicJobStore(config.state_root, library_root=config.library_root)
    pipeline_config = PipelineConfig(
        model=config.model,
        device=config.device,
        compute_type=config.compute_type,
        default_language=config.language,
        model_cache=config.model_cache,
        local_files_only=config.local_files_only,
    )
    pipeline_signature = WhisperXPipeline(pipeline_config).signature
    worker_environment = {
        "ACADEMIC_MODEL": config.model,
        "ACADEMIC_DEVICE": config.device,
        "ACADEMIC_COMPUTE_TYPE": config.compute_type,
        "ACADEMIC_LANGUAGE": config.language,
        "ACADEMIC_LOCAL_FILES_ONLY": "1" if config.local_files_only else "0",
        "ACADEMIC_PIPELINE_SIGNATURE": pipeline_signature,
    }
    if config.model_cache:
        worker_environment["ACADEMIC_MODEL_CACHE"] = config.model_cache
    if config.hf_token and not config.local_files_only:
        worker_environment["HF_TOKEN"] = config.hf_token
    supervisor = WorkerSupervisor(
        pipeline_signature,
        command=config.worker_command,
        environment=worker_environment,
    )
    transcriptions = TranscribeMedia(jobs, supervisor)
    opener = LocalFileOpener()
    facade = TextualStudyFacade(
        source_root=config.source_root,
        library_root=config.library_root,
        source_browser=BrowseSources(source_repository),
        courses=ManageCourses(library_repository),
        manifests=manifests,
        commits=CommitAssets(manifests, committer),
        jobs=jobs,
        transcriptions=transcriptions,
        transcriber=supervisor,
        pipeline_signature=pipeline_signature,
        language=config.language,
        opener=opener,
    )
    return OrganizerApp(
        facade,
        OrganizerConfig(
            source_root=config.source_root,
            library_root=config.library_root,
            start_path=source_repository.start_location,
            language=config.language,
            os_open_available=opener.available,
        ),
    )


def run_study(env_path: str = ".env") -> int:
    app = build_study_app(load_academic_config_file(env_path))
    app.run()
    return 0
