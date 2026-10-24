"""Build immutable file or link plans without touching the filesystem."""

from study.application.dto import PlanAssetCommand, PlannedAssetDTO
from study.domain.errors import DomainValidationError
from study.domain.models import AssetKind, AssetPlan
from study.domain.policies import classify_asset, normalize_url, prefixed_asset_name
from study.domain.value_objects import (
    AssetFilename,
    ClassDate,
    CourseName,
    RelativeLocation,
)


class PlanAssets:
    def execute(self, command: PlanAssetCommand) -> PlannedAssetDTO:
        course = CourseName(command.course_name)
        class_date = ClassDate.parse(command.class_date)
        if command.url is not None:
            return self._plan_link(command, course, class_date)
        return self._plan_file(command, course, class_date)

    __call__ = execute

    @staticmethod
    def _override(value: str | None, detected: AssetKind) -> AssetKind:
        if value is None:
            return detected
        try:
            return AssetKind(value)
        except ValueError as error:
            raise DomainValidationError(f"unknown asset kind: {value}") from error

    def _plan_link(
        self, command: PlanAssetCommand, course: CourseName, class_date: ClassDate
    ) -> PlannedAssetDTO:
        normalized_url = normalize_url(command.url or "")
        kind = self._override(command.kind_override, AssetKind.LINK)
        if kind is not AssetKind.LINK or command.source_location is not None:
            raise DomainValidationError(
                "URL assets must remain links without a source file"
            )
        label = command.label.strip() if command.label else normalized_url
        if not label:
            raise DomainValidationError("link label cannot be empty")
        plan = AssetPlan(
            course=course,
            class_date=class_date,
            kind=kind,
            source_url=normalized_url,
            label=label,
        )
        return self._to_dto(plan)

    def _plan_file(
        self, command: PlanAssetCommand, course: CourseName, class_date: ClassDate
    ) -> PlannedAssetDTO:
        if command.source_location is None or command.original_name is None:
            raise DomainValidationError(
                "file assets require source_location and original_name"
            )
        original = AssetFilename(command.original_name)
        kind = self._override(command.kind_override, classify_asset(original.value))
        if kind is AssetKind.LINK:
            raise DomainValidationError("file assets cannot be overridden as links")
        plan = AssetPlan(
            course=course,
            class_date=class_date,
            kind=kind,
            source_location=RelativeLocation(command.source_location),
            original_filename=original,
            destination_filename=prefixed_asset_name(class_date, original),
        )
        return self._to_dto(plan)

    @staticmethod
    def _to_dto(plan: AssetPlan) -> PlannedAssetDTO:
        destination_name = (
            plan.destination_filename.value if plan.destination_filename else None
        )
        destination_location = (
            "/".join((plan.course.value, plan.class_date.iso, destination_name))
            if destination_name
            else None
        )
        return PlannedAssetDTO(
            course_name=plan.course.value,
            class_date=plan.class_date.iso,
            kind=plan.kind.value,
            source_location=(
                plan.source_location.value if plan.source_location else None
            ),
            source_url=plan.source_url,
            original_name=(
                plan.original_filename.value if plan.original_filename else None
            ),
            destination_name=destination_name,
            destination_location=destination_location,
            label=plan.label,
        )
