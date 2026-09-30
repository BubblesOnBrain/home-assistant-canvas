"""Asynchronous API client for Canvas LMS."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import logging
import re
from typing import Any
from urllib.parse import urljoin

import aiohttp

from .const import (
    API_TIMEOUT,
    COURSE_INCLUDES,
    COURSE_STATES,
    DEFAULT_ACCEPT_TYPE,
    DEFAULT_PAGE_SIZE,
    DEFAULT_USER_AGENT,
    ENDPOINT_COURSE_ASSIGNMENT_GROUPS,
    ENDPOINT_COURSE_STUDENT_SUBMISSIONS,
    ENDPOINT_USER_COURSES,
    ENDPOINT_USERS_OBSERVEES,
    ENDPOINT_USERS_SELF,
    HEADER_ACCEPT,
    HEADER_AUTHORIZATION,
    HEADER_LINK,
    HEADER_RATE_LIMIT_REMAINING,
    HEADER_USER_AGENT,
    SUBMISSION_INCLUDES,
)
from .exceptions import (
    CanvasAuthError,
    CanvasForbiddenError,
    CanvasConnectionError,
    CanvasError,
    CanvasRateLimitError,
    CanvasResponseError,
)
from .models import (
    CanvasAssignment,
    CanvasCourse,
    CanvasObservee,
    CanvasSubmission,
    CanvasUser,
)

_LOGGER = logging.getLogger(__name__)

# RFC 5988 Link header parser
_LINK_REGEX = re.compile(r'<([^>]+)>;\s*rel=([a-zA-Z0-9_-]+|"[^"]+"|\'[^\']+\')')
MAX_PAGES = 50


def _parse_next_link(link_header: str | None) -> str | None:
    """Extract next page URL from RFC 5988 Link header."""
    if not link_header:
        return None
    for part in link_header.split(","):
        sections = part.split(";")
        if len(sections) >= 2:
            url_part = sections[0].strip()
            rel_parts = sections[1:]
            for rel in rel_parts:
                cleaned_rel = rel.strip().replace(" ", "")
                if (
                    'rel="next"' in cleaned_rel
                    or "rel='next'" in cleaned_rel
                    or "rel=next" in cleaned_rel
                ):
                    if url_part.startswith("<") and url_part.endswith(">"):
                        return url_part[1:-1]
    return None


class CanvasApiClient:
    """Asynchronous client for interacting with the Canvas LMS REST API."""

    def __init__(
        self,
        base_url: str,
        access_token: str,
        session: aiohttp.ClientSession,
        timeout: int = API_TIMEOUT,
    ) -> None:
        """Initialize the Canvas API client."""
        url = base_url.strip().rstrip("/")
        if not url.lower().startswith(("http://", "https://")):
            url = f"https://{url}"
        if url.lower().startswith("https://"):
            url = f"https://{url[8:]}"
        else:
            url = f"http://{url[7:]}"
        self._base_url = url
        self._access_token = access_token.strip()
        self._session = session
        self._timeout = timeout
        self._rate_limit_remaining: float | None = None

    @property
    def base_url(self) -> str:
        """Return the normalized base URL."""
        return self._base_url

    @property
    def access_token(self) -> str:
        """Return the access token."""
        return self._access_token

    @property
    def rate_limit_remaining(self) -> float | None:
        """Return the last recorded rate limit remaining value."""
        return self._rate_limit_remaining

    @property
    def _headers(self) -> dict[str, str]:
        """Return standardized request headers."""
        return {
            HEADER_AUTHORIZATION: f"Bearer {self._access_token}",
            HEADER_ACCEPT: DEFAULT_ACCEPT_TYPE,
            HEADER_USER_AGENT: DEFAULT_USER_AGENT,
        }

    def _parse_rate_limit_headers(self, headers: Mapping[str, Any]) -> None:
        """Extract and check rate limit headers."""
        rate_remaining = headers.get(HEADER_RATE_LIMIT_REMAINING)
        if rate_remaining is not None:
            try:
                val = float(rate_remaining)
                self._rate_limit_remaining = val
                if val < 50.0:
                    _LOGGER.warning("Canvas API rate limit remaining is low: %.1f", val)
                if val <= 0.0:
                    raise CanvasRateLimitError("Canvas API rate limit quota exhausted.")
            except ValueError:
                pass

    async def _async_request(
        self,
        method: str,
        path_or_url: str,
        params: dict[str, Any] | list[tuple[str, str]] | None = None,
    ) -> tuple[Any, aiohttp.ClientResponse]:
        """Execute a single HTTP request with timeout, error wrapping, and header parsing."""
        if path_or_url.startswith(("http://", "https://")):
            url = path_or_url
        else:
            url = urljoin(f"{self._base_url}/", path_or_url.lstrip("/"))

        try:
            async with asyncio.timeout(self._timeout):
                response = await self._session.request(
                    method=method,
                    url=url,
                    headers=self._headers,
                    params=params,
                )

                self._parse_rate_limit_headers(response.headers)

                if response.status in (401, 403):
                    body_text = await response.text()
                    if (
                        response.status == 429
                        or "Rate Limit Exceeded" in body_text
                        or (
                            self._rate_limit_remaining is not None
                            and self._rate_limit_remaining <= 0.0
                        )
                    ):
                        raise CanvasRateLimitError("Canvas API rate limit exceeded.")
                    error_cls = (
                        CanvasForbiddenError
                        if response.status == 403
                        else CanvasAuthError
                    )
                    raise error_cls(
                        f"Authentication failed (HTTP {response.status}): {body_text}"
                    )

                if response.status == 429:
                    raise CanvasRateLimitError("Canvas API rate limit exceeded.")

                if response.status >= 500:
                    body_text = await response.text()
                    raise CanvasConnectionError(
                        f"Canvas LMS server error (HTTP {response.status}): {body_text}"
                    )

                if response.status >= 400:
                    body_text = await response.text()
                    raise CanvasResponseError(
                        f"Canvas API error (HTTP {response.status}): {body_text}"
                    )

                try:
                    payload = await response.json()
                except (aiohttp.ContentTypeError, ValueError) as err:
                    raise CanvasResponseError(
                        f"Invalid JSON received from Canvas: {err}"
                    ) from err

                return payload, response

        except (TimeoutError, asyncio.TimeoutError) as err:
            raise CanvasConnectionError(
                f"Timeout connecting to Canvas LMS: {err}"
            ) from err
        except aiohttp.ClientError as err:
            raise CanvasConnectionError(
                f"Network error connecting to Canvas LMS: {err}"
            ) from err
        except CanvasError:
            raise
        except Exception as err:
            raise CanvasError(
                f"Unexpected error during Canvas API request: {err}"
            ) from err

    async def _request_paginated(
        self,
        endpoint: str,
        params: list[tuple[str, str]] | None = None,
        max_pages: int = MAX_PAGES,
    ) -> list[dict[str, Any]]:
        """Fetch all pages for a collection endpoint using RFC 5988 Link headers."""
        items: list[dict[str, Any]] = []
        next_url: str | None = endpoint
        current_params: list[tuple[str, str]] | None = params
        visited_urls: set[str] = set()
        pages_fetched = 0

        while next_url and pages_fetched < max_pages:
            canonical_url = (
                next_url
                if next_url.startswith(("http://", "https://"))
                else urljoin(f"{self._base_url}/", next_url.lstrip("/"))
            )
            if canonical_url in visited_urls:
                _LOGGER.warning("Pagination cycle detected for URL: %s", canonical_url)
                break
            visited_urls.add(canonical_url)

            payload, response = await self._async_request(
                "GET",
                next_url,
                params=current_params,
            )
            visited_urls.add(str(response.url))

            if isinstance(payload, list):
                if not payload:
                    break
                items.extend(payload)
            else:
                _LOGGER.warning(
                    "Expected list payload from %s, received: %s",
                    next_url,
                    type(payload),
                )
                break

            link_header = response.headers.get(HEADER_LINK)
            next_url = _parse_next_link(link_header)
            # URL extracted from Link header already includes query parameters
            current_params = None
            pages_fetched += 1

        return items

    async def async_get_current_user(self) -> CanvasUser:
        """Retrieve the authenticated user profile."""
        payload, _ = await self._async_request("GET", ENDPOINT_USERS_SELF)
        if not isinstance(payload, dict):
            raise CanvasResponseError("Expected user profile object from Canvas API")
        try:
            return CanvasUser.from_dict(payload)
        except (KeyError, TypeError, ValueError) as err:
            raise CanvasResponseError(
                f"Malformed user profile object from Canvas API: {err}"
            ) from err

    async def async_get_observees(self) -> list[CanvasObservee]:
        """Retrieve all linked students (observees) for the parent account."""
        params: list[tuple[str, str]] = [("per_page", str(DEFAULT_PAGE_SIZE))]
        items = await self._request_paginated(ENDPOINT_USERS_OBSERVEES, params=params)
        return [
            CanvasObservee.from_dict(item)
            for item in items
            if isinstance(item, dict) and "id" in item
        ]

    async def async_get_student_courses(self, student_id: int) -> list[CanvasCourse]:
        """Retrieve active enrolled courses with grades for a specific student."""
        endpoint = ENDPOINT_USER_COURSES.format(user_id=student_id)
        params: list[tuple[str, str]] = [("per_page", str(DEFAULT_PAGE_SIZE))]
        for inc in COURSE_INCLUDES:
            params.append(("include[]", inc))
        for st in COURSE_STATES:
            params.append(("state[]", st))

        items = await self._request_paginated(endpoint, params=params)
        return [
            CanvasCourse.from_dict(item)
            for item in items
            if isinstance(item, dict) and "id" in item
        ]

    async def async_get_student_submissions(
        self,
        course_id: int,
        student_id: int,
    ) -> list[CanvasSubmission]:
        """Retrieve student submissions for a course."""
        endpoint = ENDPOINT_COURSE_STUDENT_SUBMISSIONS.format(course_id=course_id)
        params: list[tuple[str, str]] = [
            ("per_page", str(DEFAULT_PAGE_SIZE)),
            ("student_ids[]", str(student_id)),
        ]
        for inc in SUBMISSION_INCLUDES:
            params.append(("include[]", inc))

        items = await self._request_paginated(endpoint, params=params)
        submissions: list[CanvasSubmission] = []
        for item in items:
            if isinstance(item, dict):
                sub = CanvasSubmission.from_dict(item)
                if sub is not None:
                    submissions.append(sub)
        return submissions

    async def async_get_student_assignments(
        self,
        course_id: int,
        student_id: int,
    ) -> list[CanvasAssignment]:
        """Retrieve assignments with embedded student submissions for a course."""
        endpoint = ENDPOINT_COURSE_STUDENT_SUBMISSIONS.format(course_id=course_id)
        params: list[tuple[str, str]] = [
            ("per_page", str(DEFAULT_PAGE_SIZE)),
            ("student_ids[]", str(student_id)),
        ]
        for inc in SUBMISSION_INCLUDES:
            params.append(("include[]", inc))

        items = await self._request_paginated(endpoint, params=params)
        assignments: list[CanvasAssignment] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            asg_data = item.get("assignment")
            if asg_data and isinstance(asg_data, dict) and "id" in asg_data:
                sub = CanvasSubmission.from_dict(item)
                assignment = CanvasAssignment.from_dict(asg_data, submission=sub)
                assignments.append(assignment)
        return assignments

    async def async_get_assignment_groups(self, course_id: int) -> dict[int, str]:
        """Return a course's assignment groups as {group id: name}."""
        endpoint = ENDPOINT_COURSE_ASSIGNMENT_GROUPS.format(course_id=course_id)
        params: list[tuple[str, str]] = [("per_page", str(DEFAULT_PAGE_SIZE))]
        items = await self._request_paginated(endpoint, params=params)
        return {
            int(item["id"]): str(item.get("name") or "")
            for item in items
            if isinstance(item, dict) and "id" in item
        }
