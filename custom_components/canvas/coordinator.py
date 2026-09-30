"""DataUpdateCoordinator for Canvas LMS."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import CanvasApiClient
from .const import (
    CONF_ONLINE_GRACE_HOURS,
    CONF_PAPER_GRACE_DAYS,
    DEFAULT_ONLINE_GRACE_HOURS,
    DEFAULT_PAPER_GRACE_DAYS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
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
from .store import CanvasWorkflowStore

_LOGGER = logging.getLogger(__name__)


class CanvasDataUpdateCoordinator(DataUpdateCoordinator[CanvasData]):
    """Coordinator to fetch data from the Canvas LMS REST API."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: CanvasApiClient,
        entry: ConfigEntry,
        update_interval: timedelta = timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        workflow: CanvasWorkflowStore | None = None,
    ) -> None:
        """Initialize the coordinator."""
        self.client = client
        self.entry = entry
        # The student's own data (stages, notes, follow-ups); owned by the
        # integration and saved to .storage, never rebuilt from Canvas.
        self.workflow = (
            workflow
            if workflow is not None
            else CanvasWorkflowStore(hass, entry.unique_id or entry.entry_id)
        )
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=update_interval,
            always_update=True,
        )

    @property
    def grace_windows(self) -> tuple[timedelta, timedelta]:
        """Return the (online, paper) grace windows from the entry options."""
        options = self.entry.options
        return (
            timedelta(
                hours=options.get(CONF_ONLINE_GRACE_HOURS, DEFAULT_ONLINE_GRACE_HOURS)
            ),
            timedelta(
                days=options.get(CONF_PAPER_GRACE_DAYS, DEFAULT_PAPER_GRACE_DAYS)
            ),
        )

    def _reconcile_workflow(self, data: CanvasData) -> None:
        """Update follow-ups and snapshots from a successful fetch.

        A bug or bad stored value here must not stop Canvas data updating.
        """
        try:
            self.workflow.reconcile(data, dt_util.utcnow(), *self.grace_windows)
        except Exception:
            _LOGGER.exception("Error updating the Canvas workflow records")

    @callback
    def async_workflow_changed(self, *_: object) -> None:
        """Re-evaluate the workflow and redraw entities without a Canvas fetch.

        Called after the student edits something and on a timer, since grace
        windows run out between hourly fetches. Follow-ups are only derived
        from a fetch that succeeded, never from stale data.
        """
        if self.data is not None and self.last_update_success:
            self._reconcile_workflow(self.data)
        self.async_update_listeners()

    async def _async_update_data(self) -> CanvasData:
        """Fetch Canvas data, then reconcile the student's workflow with it."""
        data = await self._async_fetch_data()
        self._reconcile_workflow(data)
        return data

    async def _async_fetch_data(self) -> CanvasData:
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
