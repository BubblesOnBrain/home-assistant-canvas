"""DataUpdateCoordinator for Canvas LMS."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import CanvasApiClient
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN
from .exceptions import (
    CanvasAuthError,
    CanvasConnectionError,
    CanvasError,
    CanvasForbiddenError,
)
from .filtering import filter_active_courses, filter_pending_assignments
from .models import (
    CanvasAssignment,
    CanvasCourse,
    CanvasData,
    CanvasObservee,
)

_LOGGER = logging.getLogger(__name__)


class CanvasDataUpdateCoordinator(DataUpdateCoordinator[CanvasData]):
    """Coordinator to fetch data from the Canvas LMS REST API."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: CanvasApiClient,
        entry: ConfigEntry,
        update_interval: timedelta = timedelta(seconds=DEFAULT_SCAN_INTERVAL),
    ) -> None:
        """Initialize the coordinator."""
        self.client = client
        self.entry = entry
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=update_interval,
            always_update=True,
        )

    async def _async_update_data(self) -> CanvasData:
        """Fetch all data from Canvas LMS API and isolate per student."""
        try:
            user = await self.client.async_get_current_user()
            observees = await self.client.async_get_observees()

            if not observees:
                # Direct student login fallback: create self-observee
                observees = [
                    CanvasObservee(
                        id=user.id,
                        name=user.name,
                        sortable_name=user.sortable_name,
                        short_name=user.short_name,
                    )
                ]

            courses_by_student: dict[int, list[CanvasCourse]] = {}
            assignments_by_student: dict[int, list[CanvasAssignment]] = {}

            async def _fetch_student_data(
                student: CanvasObservee,
            ) -> tuple[int, list[CanvasCourse], list[CanvasAssignment]]:
                raw_courses = await self.client.async_get_student_courses(student.id)
                active_courses = filter_active_courses(raw_courses)

                async def _fetch_course_assignments(
                    course: CanvasCourse,
                ) -> list[CanvasAssignment]:
                    # A 403 (not 401) means the token is valid but this observer
                    # lacks access to one course (e.g. a club or homepage course).
                    # Skip that course instead of failing the whole entry.
                    try:
                        return await self.client.async_get_student_assignments(
                            course.id, student.id
                        )
                    except CanvasForbiddenError as err:
                        _LOGGER.warning(
                            "Skipping course %s (%s) for student %s: %s",
                            course.id,
                            course.name,
                            student.id,
                            err,
                        )
                        return []

                course_results = await asyncio.gather(
                    *(_fetch_course_assignments(course) for course in active_courses)
                )
                student_assignments: list[CanvasAssignment] = []
                for course, raw_asgs in zip(
                    active_courses, course_results, strict=True
                ):
                    student_assignments.extend(
                        filter_pending_assignments(raw_asgs, course)
                    )
                return student.id, active_courses, student_assignments

            student_results = await asyncio.gather(
                *(_fetch_student_data(student) for student in observees)
            )

            for student_id, active_courses, student_assignments in student_results:
                courses_by_student[student_id] = active_courses
                assignments_by_student[student_id] = student_assignments

            return CanvasData(
                user=user,
                observees=tuple(observees),
                courses_by_student=courses_by_student,
                assignments_by_student=assignments_by_student,
            )

        except CanvasAuthError as err:
            raise ConfigEntryAuthFailed(err) from err
        except CanvasConnectionError as err:
            raise UpdateFailed(f"Connection error connecting to Canvas: {err}") from err
        except (CanvasError, Exception) as err:
            raise UpdateFailed(f"Canvas LMS error: {err}") from err
