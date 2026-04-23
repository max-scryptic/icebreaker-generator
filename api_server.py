#!/usr/bin/env python3
"""
Production-friendly API wrapper around the lead icebreaker generator.

Local run:
    export OPENAI_API_KEY="..."
    export ICEBREAKER_API_TOKEN="choose-a-long-random-token"
    uvicorn api_server:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import os
import secrets
import uuid
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from lead_icebreakers import DEFAULT_MODEL, EXPECTED_HEADERS, generate_single_lead


APP_TITLE = "Lead Icebreaker API"
APP_VERSION = "1.0.0"


class ErrorDetail(BaseModel):
    code: str
    message: str
    request_id: str
    details: Optional[Dict[str, Any]] = None


class ErrorResponse(BaseModel):
    error: ErrorDetail


class IcebreakerRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    business_name: str = Field(default="", alias="Business Name")
    niche: str = Field(default="", alias="Niche")
    city: str = Field(default="", alias="City")
    website: str = Field(default="", alias="Website")
    domain: str = Field(default="", alias="Domain")
    name: str = Field(default="", alias="Name")
    first_name: str = Field(default="", alias="First Name")
    surname: str = Field(default="", alias="Surname")
    email: str = Field(default="", alias="Email")
    model: str = DEFAULT_MODEL
    timeout: float = 20.0
    search_linkedin: bool = True
    debug: bool = False

    def to_row(self) -> Dict[str, str]:
        payload = self.model_dump(by_alias=True)
        return {header: str(payload.get(header, "") or "") for header in EXPECTED_HEADERS}


class IcebreakerResponse(BaseModel):
    company_size: str
    linkedin_url: str
    reasoning: str
    icebreaker_1: str
    icebreaker_2: str
    confidence: str


class HealthResponse(BaseModel):
    status: str
    version: str


app = FastAPI(title=APP_TITLE, version=APP_VERSION)


def get_request_id(request: Request) -> str:
    request_id = getattr(request.state, "request_id", "")
    if request_id:
        return request_id
    return str(uuid.uuid4())


def error_response(
    *,
    request: Request,
    status_code: int,
    code: str,
    message: str,
    details: Optional[Dict[str, Any]] = None,
) -> JSONResponse:
    payload = ErrorResponse(
        error=ErrorDetail(
            code=code,
            message=message,
            request_id=get_request_id(request),
            details=details,
        )
    )
    return JSONResponse(status_code=status_code, content=payload.model_dump())


@app.middleware("http")
async def attach_request_id(request: Request, call_next):  # type: ignore[no-untyped-def]
    request.state.request_id = str(uuid.uuid4())
    response = await call_next(request)
    response.headers["X-Request-ID"] = request.state.request_id
    return response


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return error_response(
        request=request,
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        code="validation_error",
        message="Request body validation failed",
        details={"issues": exc.errors()},
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    message = str(exc.detail)
    if isinstance(exc.detail, dict):
        message = str(exc.detail.get("message", exc.detail))
    code = "http_error"
    if exc.status_code == status.HTTP_401_UNAUTHORIZED:
        code = "unauthorized"
    elif exc.status_code == status.HTTP_404_NOT_FOUND:
        code = "not_found"
    elif exc.status_code == status.HTTP_400_BAD_REQUEST:
        code = "bad_request"
    return error_response(
        request=request,
        status_code=exc.status_code,
        code=code,
        message=message,
    )


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    return error_response(
        request=request,
        status_code=status.HTTP_502_BAD_GATEWAY,
        code="generation_failed",
        message=str(exc),
    )


def require_bearer_token(authorization: Optional[str] = Header(default=None)) -> None:
    expected_token = os.getenv("ICEBREAKER_API_TOKEN", "").strip()
    if not expected_token:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="ICEBREAKER_API_TOKEN is not set",
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
        )
    supplied_token = authorization.split(" ", 1)[1].strip()
    if not supplied_token or not secrets.compare_digest(supplied_token, expected_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid bearer token",
        )


@app.get("/health", response_model=HealthResponse)
async def healthcheck() -> HealthResponse:
    return HealthResponse(status="ok", version=APP_VERSION)


@app.post(
    "/icebreaker",
    response_model=IcebreakerResponse,
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
    },
)
async def create_icebreaker(
    payload: IcebreakerRequest,
    request: Request,
    _: None = Depends(require_bearer_token),
) -> IcebreakerResponse:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="OPENAI_API_KEY is not set",
        )

    row = payload.to_row()
    if not any(row.values()):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Request body must include at least one supported lead field",
        )

    result = generate_single_lead(
        row=row,
        model=payload.model,
        api_key=api_key,
        timeout=payload.timeout,
        search_linkedin=payload.search_linkedin,
        debug_logs=payload.debug,
    )
    return IcebreakerResponse(**result)
