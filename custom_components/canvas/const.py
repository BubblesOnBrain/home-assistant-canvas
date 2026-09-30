"""Constants for the Canvas LMS integration."""

from __future__ import annotations

from typing import Final

# Integration Domain
DOMAIN: Final = "canvas"

# Configuration Keys
CONF_BASE_URL: Final = "base_url"
CONF_ACCESS_TOKEN: Final = "access_token"

# Timing & Intervals (in seconds)
DEFAULT_SCAN_INTERVAL: Final = 3600  # 60 minutes
DEFAULT_TIMEOUT: Final = 15  # 15 seconds per HTTP request
API_TIMEOUT: Final = 15  # Alias for DEFAULT_TIMEOUT
DEFAULT_PAGE_SIZE: Final = 100  # Canvas maximum page size
DEFAULT_PER_PAGE: Final = 100  # Alias for DEFAULT_PAGE_SIZE

# Stale Due Date Fallback Window (in days)
DEFAULT_STALE_DAYS_THRESHOLD: Final = 180
# Days finished assignments stay on the To-Do list (shown as completed)
DONE_RETENTION_DAYS: Final = 7

# HTTP Headers
HEADER_ACCEPT: Final = "Accept"
HEADER_AUTHORIZATION: Final = "Authorization"
HEADER_USER_AGENT: Final = "User-Agent"
HEADER_RATE_LIMIT_REMAINING: Final = "X-Rate-Limit-Remaining"
HEADER_REQUEST_COST: Final = "X-Request-Cost"
HEADER_LINK: Final = "Link"

# Default Header Values
DEFAULT_ACCEPT_TYPE: Final = "application/json"
DEFAULT_USER_AGENT: Final = "HomeAssistant-Canvas/1.0"

# API Endpoints
ENDPOINT_USERS_SELF: Final = "/api/v1/users/self"
ENDPOINT_USERS_OBSERVEES: Final = "/api/v1/users/self/observees"
ENDPOINT_USER_COURSES: Final = "/api/v1/users/{user_id}/courses"
ENDPOINT_COURSE_STUDENT_SUBMISSIONS: Final = (
    "/api/v1/courses/{course_id}/students/submissions"
)
ENDPOINT_USER_MISSING_SUBMISSIONS: Final = "/api/v1/users/{user_id}/missing_submissions"

# API Endpoint Aliases
API_USERS_SELF: Final = ENDPOINT_USERS_SELF
API_USERS_OBSERVEES: Final = ENDPOINT_USERS_OBSERVEES
API_USER_COURSES: Final = ENDPOINT_USER_COURSES
API_COURSE_STUDENT_SUBMISSIONS: Final = ENDPOINT_COURSE_STUDENT_SUBMISSIONS

# API Query Parameter Includes & States
COURSE_INCLUDES: Final = (
    "total_scores",
    "current_grading_period_scores",
    "term",
    "teachers",
)
COURSE_STATES: Final = ("available",)
SUBMISSION_INCLUDES: Final = ("assignment",)

# Filtering Constants
FILTER_NOT_GRADED: Final = "not_graded"
ACTIVE_ENROLLMENT_STATES: Final = ("active", "invited")
COMPLETED_TERM_STATES: Final = ("completed", "deleted")
ONLINE_SUBMISSION_TYPES: Final = (
    "online_upload",
    "online_text_entry",
    "online_url",
    "media_recording",
    "online_quiz",
    "discussion_topic",
    "external_tool",
)
IN_CLASS_KEYWORDS: Final = (
    "class week",
    "class_week",
    "do now",
    "guided notes",
    "gn -",
    "gn-",
    "workshop",
    "participation",
)
