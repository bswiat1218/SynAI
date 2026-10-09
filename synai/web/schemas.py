from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class StrictSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ErrorDetail(StrictSchema):
    code: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=256)
    request_id: str = Field(min_length=16, max_length=64)


class ErrorResponse(StrictSchema):
    error: ErrorDetail


class HealthResponse(StrictSchema):
    status: str


class LoginRequest(StrictSchema):
    password: str = Field(min_length=1, max_length=1024)


class LoginResponse(StrictSchema):
    authenticated: bool
    csrf_token: str = Field(min_length=32, max_length=128)
    expires_at: int


class SessionResponse(StrictSchema):
    authenticated: bool
    expires_at: int


class CsrfResponse(StrictSchema):
    csrf_token: str = Field(min_length=32, max_length=128)


class PasswordChangeRequest(StrictSchema):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=12, max_length=1024)


class ModelResponse(StrictSchema):
    name: str = Field(min_length=1, max_length=256)


class ModelListResponse(StrictSchema):
    models: list[ModelResponse] = Field(max_length=4096)


class ProjectRegistrationRequest(StrictSchema):
    workspace_key: str = Field(min_length=1, max_length=32, pattern=r"^[a-z][a-z0-9_-]*$")


class ProjectResponse(StrictSchema):
    id: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    name: str = Field(min_length=1, max_length=255)
    status: str
    access: str = "read_only"


class ProjectListResponse(StrictSchema):
    projects: list[ProjectResponse] = Field(max_length=256)
