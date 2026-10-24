"""Direct-child course and package catalog port."""

from typing import Protocol

from study.application.dto import CourseDTO, PackageDTO


class LibraryRepository(Protocol):
    def list_courses(self) -> tuple[CourseDTO, ...]: ...

    def create_course(self, name: str) -> CourseDTO: ...

    def list_packages(self, course_name: str) -> tuple[PackageDTO, ...]: ...

    def ensure_package(self, course_name: str, class_date: str) -> PackageDTO: ...
