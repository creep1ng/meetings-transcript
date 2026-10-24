"""Reconcile an immutable plan into one crash-safe class package commit."""

from __future__ import annotations

import unicodedata
from dataclasses import replace

from study.application.ports.commit import (
    MISSING_MANIFEST_FINGERPRINT,
    AssetCommitOutcome,
    AssetCommitStatus,
    AssetCommitter,
    ClassPackageManifest,
    CommitAssetsCommand,
    CommitAssetsResult,
    CommitCancelled,
    ConflictResolution,
    FileAsset,
    LinkAsset,
    ManifestChanged,
    ManifestStore,
    PlannedFile,
    ReconciliationRequired,
    UnresolvedConflict,
)


def _key(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


class CommitAssets:
    """Validate a planned base, reconcile assets, and delegate one safe commit."""

    def __init__(self, manifests: ManifestStore, committer: AssetCommitter) -> None:
        self._manifests = manifests
        self._committer = committer

    def execute(self, command: CommitAssetsCommand) -> CommitAssetsResult:
        if any(item.conflict == ConflictResolution.CANCEL for item in command.files):
            raise CommitCancelled("asset commit cancelled before staging")

        snapshot = self._manifests.read(command.package_dir)
        if snapshot.conflicted_copies:
            names = ", ".join(snapshot.conflicted_copies)
            raise ReconciliationRequired(f"Nextcloud conflicted copies found: {names}")

        self._validate_base(command)
        manifest, active, skipped = self._reconcile(command)
        target_fingerprint = self._manifests.fingerprint(manifest)
        if snapshot.fingerprint not in {
            command.expected_manifest_fingerprint,
            target_fingerprint,
        }:
            raise ManifestChanged("manifest changed after the commit plan was created")
        if (
            snapshot.fingerprint == command.expected_manifest_fingerprint
            and snapshot.manifest is not None
            and snapshot.manifest != command.base_manifest
        ):
            raise ManifestChanged("planned base does not match the current manifest")

        committed = self._committer.commit(command, manifest, active)
        return replace(committed, outcomes=committed.outcomes + skipped)

    def _validate_base(self, command: CommitAssetsCommand) -> None:
        base = command.base_manifest
        if base is None:
            if command.expected_manifest_fingerprint != MISSING_MANIFEST_FINGERPRINT:
                raise ManifestChanged("a missing base requires the missing fingerprint")
            return
        if command.expected_manifest_fingerprint == MISSING_MANIFEST_FINGERPRINT:
            return
        if base.course != command.course or base.class_date != command.class_date:
            raise ManifestChanged(
                "planned package identity differs from its base manifest"
            )

    def _reconcile(
        self, command: CommitAssetsCommand
    ) -> tuple[
        ClassPackageManifest,
        tuple[PlannedFile, ...],
        tuple[AssetCommitOutcome, ...],
    ]:
        assets = list(command.base_manifest.assets if command.base_manifest else ())
        for discovered in command.discovered_assets:
            if any(asset.id == discovered.id for asset in assets):
                continue
            if any(
                isinstance(asset, FileAsset)
                and _key(asset.filename) == _key(discovered.filename)
                for asset in assets
            ):
                raise UnresolvedConflict(
                    f"discovered package file collides with manifest: {discovered.filename}"
                )
            assets.append(discovered)
        active: list[PlannedFile] = []
        skipped: list[AssetCommitOutcome] = []

        for planned in command.files:
            if planned.conflict == ConflictResolution.SKIP:
                skipped.append(
                    AssetCommitOutcome(
                        planned.asset.id,
                        planned.asset.filename,
                        AssetCommitStatus.SKIPPED,
                    )
                )
                continue

            same_id = next(
                (i for i, asset in enumerate(assets) if asset.id == planned.asset.id),
                None,
            )
            if same_id is not None:
                existing_by_id = assets[same_id]
                if not isinstance(existing_by_id, FileAsset):
                    raise UnresolvedConflict("an asset id cannot change type")
                if _key(existing_by_id.filename) != _key(planned.asset.filename):
                    raise UnresolvedConflict(
                        "moving an already-managed asset is outside commit scope"
                    )
            collision = next(
                (
                    (i, asset)
                    for i, asset in enumerate(assets)
                    if isinstance(asset, FileAsset)
                    and _key(asset.filename) == _key(planned.asset.filename)
                    and asset.id != planned.asset.id
                ),
                None,
            )

            if collision is not None:
                index, existing = collision
                if existing.sha256 == planned.asset.sha256:
                    active.append(planned)
                    continue
                if planned.conflict != ConflictResolution.REPLACE:
                    raise UnresolvedConflict(
                        f"destination {planned.asset.filename!r} requires replace, "
                        "a distinct keep-both filename, skip, or cancel"
                    )
                if planned.expected_destination_sha256 is None:
                    planned = replace(
                        planned, expected_destination_sha256=existing.sha256
                    )
                assets[index] = planned.asset
            elif same_id is not None:
                existing_by_id = assets[same_id]
                if not isinstance(existing_by_id, FileAsset):
                    raise UnresolvedConflict("an asset id cannot change type")
                if existing_by_id.sha256 != planned.asset.sha256:
                    if planned.conflict != ConflictResolution.REPLACE:
                        raise UnresolvedConflict(
                            f"destination {planned.asset.filename!r} requires replace"
                        )
                    if planned.expected_destination_sha256 is None:
                        planned = replace(
                            planned,
                            expected_destination_sha256=existing_by_id.sha256,
                        )
                assets[same_id] = planned.asset
            else:
                assets.append(planned.asset)
            active.append(planned)

        for link in command.links:
            same_id = next(
                (i for i, asset in enumerate(assets) if asset.id == link.id), None
            )
            same_url = next(
                (
                    asset
                    for asset in assets
                    if isinstance(asset, LinkAsset) and asset.url == link.url
                ),
                None,
            )
            if same_id is not None:
                if not isinstance(assets[same_id], LinkAsset):
                    raise UnresolvedConflict("an asset id cannot change type")
                assets[same_id] = link
            elif same_url is None:
                assets.append(link)

        assets.sort(
            key=lambda asset: (
                asset.type,
                _key(asset.filename if isinstance(asset, FileAsset) else asset.url),
                asset.id,
            )
        )
        return (
            ClassPackageManifest(
                course=command.course,
                class_date=command.class_date,
                updated_at=command.updated_at,
                assets=tuple(assets),
            ),
            tuple(active),
            tuple(skipped),
        )
