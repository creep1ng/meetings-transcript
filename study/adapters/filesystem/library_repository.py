"""Local direct-child course and class-package catalog."""

from pathlib import Path
from typing import Any

from study.application.dto import CourseDTO, PackageAssetDTO, PackageDTO
from study.domain.errors import (
    CourseNotFoundError,
    InvalidNameError,
    LocationNotDirectoryError,
    LocationNotFoundError,
    NameCollisionError,
    RootEscapeError,
)
from study.domain.policies import (
    classify_asset,
    is_hidden_name,
    is_incomplete_name,
)
from study.domain.value_objects import ClassDate, CourseName, name_collision_key

PACKAGE_MANIFEST = "class-package.json"


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


class FilesystemLibraryRepository:
    """Catalog only courses and packages that are direct root children."""

    def __init__(
        self,
        library_root: Any,
        *,
        show_hidden: bool = False,
        include_incomplete: bool = False,
    ) -> None:
        try:
            root = Path(library_root).expanduser().resolve(strict=True)
        except (FileNotFoundError, RuntimeError) as error:
            raise LocationNotFoundError(
                f"library root does not exist: {library_root}"
            ) from error
        if not root.is_dir():
            raise LocationNotDirectoryError(
                f"library root is not a directory: {library_root}"
            )
        self._root = root
        self._show_hidden = show_hidden
        self._include_incomplete = include_incomplete

    def list_courses(self) -> tuple[CourseDTO, ...]:
        courses: dict[str, tuple[CourseName, Path]] = {}
        for entry in self._root.iterdir():
            if not self._visible(entry.name):
                continue
            if entry.is_symlink():
                continue
            try:
                resolved = entry.resolve(strict=True)
                course = CourseName(entry.name)
            except (FileNotFoundError, RuntimeError, InvalidNameError):
                continue
            if not resolved.is_dir() or not _is_within(resolved, self._root):
                continue
            previous = courses.get(course.collision_key)
            if previous is not None:
                raise NameCollisionError(
                    "ambiguous course names: "
                    f"{previous[0].value!r} and {course.value!r}"
                )
            courses[course.collision_key] = (course, resolved)

        result = [
            CourseDTO(name=course.value, package_count=self._package_count(path))
            for course, path in courses.values()
        ]
        result.sort(key=lambda item: (name_collision_key(item.name), item.name))
        return tuple(result)

    def create_course(self, name: str) -> CourseDTO:
        course = CourseName(name)
        collisions = [
            entry.name
            for entry in self._root.iterdir()
            if name_collision_key(entry.name) == course.collision_key
        ]
        if collisions:
            raise NameCollisionError(
                f"course collides with existing entry: {collisions[0]!r}"
            )
        destination = self._root / course.value
        try:
            destination.mkdir()
        except FileExistsError as error:
            raise NameCollisionError(
                f"course already exists: {course.value!r}"
            ) from error
        return CourseDTO(name=course.value)

    def list_packages(self, course_name: str) -> tuple[PackageDTO, ...]:
        course, path = self._find_course(course_name)
        packages: list[PackageDTO] = []
        for entry in path.iterdir():
            if not self._visible(entry.name):
                continue
            if entry.is_symlink():
                continue
            try:
                class_date = ClassDate.parse(entry.name)
                resolved = entry.resolve(strict=True)
            except (ValueError, FileNotFoundError, RuntimeError):
                continue
            if resolved.is_dir() and _is_within(resolved, path):
                packages.append(self._package_dto(course, class_date, resolved))
        packages.sort(key=lambda item: item.class_date, reverse=True)
        return tuple(packages)

    def ensure_package(self, course_name: str, class_date: str) -> PackageDTO:
        course, course_path = self._find_course(course_name)
        parsed_date = ClassDate.parse(class_date)
        package_path = course_path / parsed_date.iso
        if package_path.is_symlink():
            raise RootEscapeError("package directories cannot be symlinks")
        try:
            package_path.mkdir()
        except FileExistsError:
            pass
        try:
            resolved = package_path.resolve(strict=True)
        except (FileNotFoundError, RuntimeError) as error:
            raise LocationNotFoundError(
                f"package could not be created: {parsed_date.iso}"
            ) from error
        if not resolved.is_dir():
            raise LocationNotDirectoryError(
                f"package path is not a directory: {parsed_date.iso}"
            )
        if not _is_within(resolved, course_path):
            raise RootEscapeError("package resolves outside its direct-child course")
        return self._package_dto(course, parsed_date, resolved)

    def _find_course(self, requested_name: str) -> tuple[CourseName, Path]:
        requested = CourseName(requested_name)
        matches: list[tuple[CourseName, Path]] = []
        for entry in self._root.iterdir():
            if name_collision_key(entry.name) != requested.collision_key:
                continue
            if entry.is_symlink():
                continue
            try:
                actual = CourseName(entry.name)
                resolved = entry.resolve(strict=True)
            except (InvalidNameError, FileNotFoundError, RuntimeError):
                continue
            if resolved.is_dir() and _is_within(resolved, self._root):
                matches.append((actual, resolved))
        if len(matches) > 1:
            raise NameCollisionError(f"course name is ambiguous: {requested_name!r}")
        if not matches:
            raise CourseNotFoundError(f"course does not exist: {requested_name!r}")
        return matches[0]

    def _package_count(self, course_path: Path) -> int:
        count = 0
        for entry in course_path.iterdir():
            if entry.is_symlink():
                continue
            try:
                ClassDate.parse(entry.name)
                resolved = entry.resolve(strict=True)
            except (ValueError, FileNotFoundError, RuntimeError):
                continue
            if resolved.is_dir() and _is_within(resolved, course_path):
                count += 1
        return count

    def _package_dto(
        self, course: CourseName, class_date: ClassDate, package_path: Path
    ) -> PackageDTO:
        assets: list[PackageAssetDTO] = []
        has_manifest = False
        for entry in package_path.iterdir():
            if entry.name == PACKAGE_MANIFEST:
                try:
                    manifest = entry.resolve(strict=True)
                except (FileNotFoundError, RuntimeError):
                    continue
                has_manifest = manifest.is_file() and _is_within(manifest, package_path)
                continue
            if not self._visible(entry.name):
                continue
            if entry.is_symlink():
                continue
            try:
                resolved = entry.resolve(strict=True)
            except (FileNotFoundError, RuntimeError):
                continue
            if not resolved.is_file() or not _is_within(resolved, package_path):
                continue
            assets.append(
                PackageAssetDTO(
                    name=entry.name,
                    kind=classify_asset(entry.name).value,
                    size_bytes=resolved.stat().st_size,
                )
            )
        assets.sort(key=lambda item: (name_collision_key(item.name), item.name))
        return PackageDTO(
            course_name=course.value,
            class_date=class_date.iso,
            assets=tuple(assets),
            has_manifest=has_manifest,
        )

    def _visible(self, name: str) -> bool:
        if not self._show_hidden and is_hidden_name(name):
            return False
        if not self._include_incomplete and is_incomplete_name(name):
            return False
        return True
