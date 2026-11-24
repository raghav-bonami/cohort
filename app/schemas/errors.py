"""Error response models. Used only to document error shapes in OpenAPI (/docs)."""

from typing import Any

from pydantic import BaseModel


class ErrorDetail(BaseModel):
    field: str
    message: str
    type: str


class ErrorBody(BaseModel):
    code: str
    message: str
    request_id: str | None
    details: list[ErrorDetail] | dict[str, Any] | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


def error_responses(*status_codes: int) -> dict[int, dict]:
    """`responses=` argument for a route: lists its possible errors in /docs."""
    return {code: {"model": ErrorResponse} for code in status_codes}
