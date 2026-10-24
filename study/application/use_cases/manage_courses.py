"""Manage direct-child courses and their date packages."""

from study.application.dto import (
    CourseDTO,
    CreateCourseCommand,
    EnsurePackageCommand,
    ListPackagesQuery,
    PackageDTO,
)
from study.application.ports.library import LibraryRepository


class ManageCourses:
    def __init__(self, repository: LibraryRepository) -> None:
        self._repository = repository

    def list_courses(self) -> tuple[CourseDTO, ...]:
        return self._repository.list_courses()

    def create_course(self, command: CreateCourseCommand) -> CourseDTO:
        return self._repository.create_course(command.name)

    def list_packages(self, query: ListPackagesQuery) -> tuple[PackageDTO, ...]:
        return self._repository.list_packages(query.course_name)

    def ensure_package(self, command: EnsurePackageCommand) -> PackageDTO:
        return self._repository.ensure_package(command.course_name, command.class_date)
