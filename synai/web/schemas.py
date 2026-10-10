from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt


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


class ChatConversationCreateRequest(StrictSchema):
    model: str | None = Field(default=None, min_length=1, max_length=256)
    project_id: str | None = Field(default=None, min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")


class ChatTurnRequest(StrictSchema):
    prompt: str = Field(min_length=1, max_length=32_768)
    model: str = Field(min_length=1, max_length=256)


class ChatMessageResponse(StrictSchema):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=1_048_576)
    thinking: str = Field(max_length=1_048_576)
    status: str = Field(max_length=32)
    created_at: str = Field(max_length=64)


class ChatConversationResponse(StrictSchema):
    schema_version: Literal[1] = 1
    id: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    project_id: str | None
    title: str = Field(max_length=255)
    model: str = Field(max_length=256)
    state: Literal["idle", "running", "cancelled", "error", "interrupted", "stopped"]
    created_at: str = Field(max_length=64)
    updated_at: str = Field(max_length=64)
    messages: list[ChatMessageResponse] = Field(default_factory=list, max_length=256)


class ChatConversationListResponse(StrictSchema):
    conversations: list[ChatConversationResponse] = Field(max_length=256)


class ChatTurnResponse(StrictSchema):
    conversation_id: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    model: str = Field(min_length=1, max_length=256)
    state: Literal["running"]


class ChatCancelResponse(StrictSchema):
    conversation_id: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    state: Literal["cancelling", "idle", "cancelled", "error", "interrupted", "stopped", "running"]
    cancelled: bool


class ChatEventEnvelope(StrictSchema):
    schema_version: Literal[1] = 1
    event_id: StrictInt = Field(ge=0)
    conversation_id: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    type: Literal[
        "session_snapshot", "turn_started", "content_delta", "thinking_delta",
        "turn_completed", "turn_cancelled", "turn_interrupted", "turn_failed",
        "provider_unavailable", "resynchronization_required",
    ]
    created_at: StrictInt = Field(ge=0)
    payload: dict[str, str | int | bool | None] = Field(max_length=8)


class ProjectActivityEventResponse(StrictSchema):
    schema_version: Literal[1] = 1
    event_id: StrictInt = Field(ge=0)
    project_id: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    type: Literal[
        "project_snapshot", "resynchronization_required", "project_created",
        "workspace_binding_created", "workspace_binding_revoked", "device_revoked",
        "snapshot_committed", "snapshot_expired",
    ]
    created_at: StrictInt = Field(ge=0)
    payload: dict[str, str | int | bool | None] = Field(max_length=8)


class ProjectActivityListResponse(StrictSchema):
    schema_version: Literal[1] = 1
    project_id: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    cursor: StrictInt = Field(ge=0)
    events: list[ProjectActivityEventResponse] = Field(max_length=256)


class ProjectRegistrationRequest(StrictSchema):
    workspace_key: str = Field(min_length=1, max_length=32, pattern=r"^[a-z][a-z0-9_-]*$")


class ProjectResponse(StrictSchema):
    id: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    name: str = Field(min_length=1, max_length=255)
    status: str
    access: str = "read_only"
    compatibility_state: Literal["legacy_host_path"] = "legacy_host_path"


class ProjectListResponse(StrictSchema):
    projects: list[ProjectResponse] = Field(max_length=256)


class LogicalProjectCreateRequest(StrictSchema):
    name: str = Field(min_length=1, max_length=128)
    registration_key: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


class LogicalProjectResponse(StrictSchema):
    id: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    schema_version: int = 1
    name: str = Field(min_length=1, max_length=128)
    status: str
    created_at: int


class LogicalProjectListResponse(StrictSchema):
    projects: list[LogicalProjectResponse] = Field(max_length=256)


class PairingChallengeResponse(StrictSchema):
    challenge_id: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    challenge_secret: str = Field(min_length=32, max_length=128)
    expires_at: int
    protocol_versions: list[int] = Field(min_length=1, max_length=8)


class DeviceEnrollmentRequest(StrictSchema):
    challenge_id: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    challenge_secret: str = Field(min_length=32, max_length=128)
    public_key: str = Field(min_length=40, max_length=64)
    protocol_version: StrictInt = Field(ge=1, le=32)
    capabilities: dict[str, object]
    signature: str = Field(min_length=80, max_length=128)


class DeviceCredentialResponse(StrictSchema):
    device_id: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    credential: str = Field(min_length=32, max_length=128)
    credential_expires_at: int
    state: str


class DeviceMetadataResponse(StrictSchema):
    id: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    state: str
    protocol_version: int
    capabilities: dict[str, object]
    key_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    created_at: int
    authorized_at: int | None
    revoked_at: int | None
    last_seen_at: int | None
    last_authenticated_activity_at: int | None
    credential_expires_at: int
    recently_active: bool
    connection_state: Literal["not_supported"]
    connected: bool


class DeviceListResponse(StrictSchema):
    devices: list[DeviceMetadataResponse] = Field(max_length=256)


class WorkspaceBindingCreateRequest(StrictSchema):
    device_id: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    name: str = Field(min_length=1, max_length=128)
    expires_at: StrictInt | None = None


class WorkspaceBindingResponse(StrictSchema):
    id: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    schema_version: int = 1
    project_id: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    device_id: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    name: str = Field(min_length=1, max_length=128)
    status: str
    created_at: int
    expires_at: int | None


class WorkspaceBindingListResponse(StrictSchema):
    bindings: list[WorkspaceBindingResponse] = Field(max_length=512)


class SnapshotFileManifest(StrictSchema):
    path: str = Field(min_length=1, max_length=512)
    file_type: str = Field(min_length=1, max_length=32)
    size_bytes: StrictInt = Field(ge=0, le=1_048_576)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class SnapshotUploadBeginRequest(StrictSchema):
    project_id: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    binding_id: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    idempotency_key: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    files: list[SnapshotFileManifest] = Field(min_length=1, max_length=500)


class SnapshotUploadResponse(StrictSchema):
    upload_id: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    state: str
    chunk_bytes: int
    deadline: int
    files: int
    total_bytes: int
    reused: bool


class SnapshotUploadStatusResponse(StrictSchema):
    upload_id: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    state: str
    deadline: int
    files: int
    total_bytes: int
    snapshot_id: str | None


class SnapshotChunkResponse(StrictSchema):
    accepted: bool
    duplicate: bool


class SnapshotResponse(StrictSchema):
    snapshot_id: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    project_id: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    source_device_id: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    workspace_binding_id: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    state: str
    created_at: int
    expires_at: int
    total_bytes: int
    manifest_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    manifest: dict[str, object]


class SnapshotListResponse(StrictSchema):
    snapshots: list[SnapshotResponse] = Field(max_length=256)


class DeviceCredentialRotationResponse(StrictSchema):
    credential: str = Field(min_length=32, max_length=128)
    credential_expires_at: int


class TaskContractResponse(StrictSchema):
    schema_version: int = 1
    task_id: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    project_id: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    source_snapshot_id: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    source_device_id: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    workspace_binding_id: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    selected_execution_target: str | None
    required_capabilities: list[str] = Field(max_length=32)
    state: str
    execution_claim: str | None
    lease_generation: StrictInt = Field(ge=0)
    approval_reference: str | None
    result_reference: str | None
    error_reference: str | None


class TaskListResponse(StrictSchema):
    tasks: list[TaskContractResponse] = Field(max_length=256)
    execution_available: bool = False


class ExecutionTargetResponse(StrictSchema):
    id: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    schema_version: int = 1
    target_type: str
    state: str
    capabilities: list[str] = Field(max_length=32)
    broker_allocation_id: str | None


class ExecutionTargetStatusResponse(StrictSchema):
    schema_version: int = 1
    targets: list[ExecutionTargetResponse] = Field(max_length=64)
    execution_available: bool = False
    broker_available: bool = False


class MemoryAssociationPreviewRequest(StrictSchema):
    legacy_identity: str = Field(pattern=r"^[a-f0-9]{64}$")
    provenance: dict[str, str | int | bool | None] = Field(max_length=8)


class MemoryAssociationPreviewResponse(StrictSchema):
    association_id: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    schema_version: int = 1
    legacy_identity: str = Field(pattern=r"^[a-f0-9]{64}$")
    project_id: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    status: str = "preview_only"
    provenance_validated: bool = True
    record_count: int | None
    migration_enabled: bool = False
