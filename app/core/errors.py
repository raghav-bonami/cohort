"""Application errors and the handlers that give every failure the same JSON shape.

Every error response looks like:

    {"error": {"code": "not_found", "message": "...", "request_id": "...", "details": [...]}}

One shape means a client writes one error parser, and request_id lets anyone match a
response to its log lines. Services raise AppError subclasses; they never build HTTP
responses themselves, so business code stays independent of FastAPI.
"""

from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = structlog.get_logger()


class AppError(Exception):
    """Base class for expected, client-facing failures."""

    status_code = 500
    code = "internal_error"

    def __init__(self, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class UnauthorizedError(AppError):
    status_code = 401  # caller did not identify itself (missing X-User-Id)
    code = "unauthorized"


class ForbiddenError(AppError):
    status_code = 403  # caller identified, but is acting as someone else
    code = "forbidden"


class NotFoundError(AppError):
    status_code = 404  # also used for "exists but not yours" (no existence leak)
    code = "not_found"


class ConflictError(AppError):
    status_code = (
        409  # request clashes with existing state client_doc_ref used for other content/owner
    )
    code = "conflict"


class BadRequestError(AppError):
    status_code = 400  # a header we parse ourselves is malformed (e.g. If-Match)
    code = "bad_request"


class PreconditionFailedError(AppError):
    status_code = 412  # If-Match named a content_version that is no longer current
    code = "precondition_failed"


def _error_body(code: str, message: str, details: Any = None) -> dict[str, Any]:
    # request_id was bound by request_id_middleware for this request's context.
    request_id = structlog.contextvars.get_contextvars().get("request_id")
    error: dict[str, Any] = {"code": code, "message": message, "request_id": request_id}
    if details is not None:
        error["details"] = details
    return {"error": error}


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    # 4xx are normal outcomes (bad input, not found), so INFO, not ERROR:
    # error-level logs should mean "someone needs to look at this".
    log.info("request_rejected", code=exc.code, status=exc.status_code, reason=exc.message)
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_body(exc.code, exc.message, exc.details),
    )


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    # Keep only where and why. Pydantic's raw error also contains "input", which
    # would echo the rejected value back (up to 100k chars of content) and copy
    # user text into logs.
    details = [
        {
            "field": ".".join(str(part) for part in err["loc"]),
            "message": err["msg"],
            "type": err["type"],
        }
        for err in exc.errors()
    ]
    log.info("request_invalid", fields=[d["field"] for d in details])
    return JSONResponse(
        status_code=422,
        content=_error_body("validation_error", "Request validation failed", details),
    )


async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    # Framework-raised errors (unknown route → 404, wrong method → 405) get the
    # same shape as our own, so clients never see two formats.
    code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_body(code, str(exc.detail)),
        headers=getattr(exc, "headers", None),
    )


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    # Last resort for bugs. The full traceback goes to the logs; the caller gets a
    # generic message plus the request_id to quote. Internals (stack traces,
    # connection strings) are never sent to the client.
    log.error("unhandled_error", path=request.url.path, exc_info=exc)
    return JSONResponse(
        status_code=500,
        content=_error_body("internal_error", "Internal server error"),
    )


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)
