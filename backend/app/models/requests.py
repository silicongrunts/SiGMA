"""
Pydantic request schemas for all SiGMA endpoints.

Every POST/PUT/PATCH endpoint MUST use a schema from this file.
Never use Dict = Body(...) — always use a typed request model.
"""

from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Optional, List, Dict, Any, Literal


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------

class StreamChatRequest(BaseModel):
    """Request body for /chat/stream/{project_id}."""
    message: str = Field(default="", max_length=100000)
    session_id: Optional[str] = None
    file: Optional[str] = None
    resume: bool = False  # set true to continue a crashed task from checkpoint
    interaction_response: Optional[Dict[str, Any]] = None  # user response to interactive tool
    user_state: Optional[Dict[str, Any]] = None  # frontend context (active tab, cursor, citation, etc.)
    attachments: List[Dict[str, Any]] = Field(default_factory=list)
    token_budget: Optional[int] = Field(None, ge=1)


class EditChatMessageRequest(BaseModel):
    """Replace a user message and delete that message plus all later messages."""
    message_id: str = Field(..., min_length=1)
    message: str = Field(..., min_length=1, max_length=100000)
    user_state: Optional[Dict[str, Any]] = None
    attachments: List[Dict[str, Any]] = Field(default_factory=list)
    token_budget: Optional[int] = Field(None, ge=1)


class UpdateSessionRequest(BaseModel):
    """Request body for PATCH /chat/sessions/{project_id}/{session_id}."""
    title: Optional[str] = Field(None, max_length=500)
    is_archived: Optional[bool] = None


# ---------------------------------------------------------------------------
# Library
# ---------------------------------------------------------------------------

class CreateDocumentRequest(BaseModel):
    """Request body for POST /library/{project_id}/documents."""
    title: str = Field(..., min_length=1, max_length=500)
    description: Optional[str] = Field("", max_length=5000)
    content: str = Field("")
    source: Optional[str] = None
    doc_type: str = Field("text", max_length=50)
    keywords: Optional[List[str]] = None


class UpdateDocumentRequest(BaseModel):
    """Request body for PUT /library/{project_id}/documents/{doc_id}."""
    title: Optional[str] = Field(None, max_length=500)
    description: Optional[str] = None
    content: Optional[str] = None
    source: Optional[str] = None
    doc_type: Optional[str] = None
    keywords: Optional[List[str]] = None


class SearchRequest(BaseModel):
    """Request body for /library/{project_id}/search and rag-search."""
    query: str = Field(..., min_length=1, max_length=1000)
    parent_id: Optional[str] = None
    limit: int = Field(100, ge=1, le=500)
    offset: int = Field(0, ge=0)


class CreateFolderRequest(BaseModel):
    """Request body for POST /library/{project_id}/folders."""
    name: str = Field(..., min_length=1, max_length=255)
    parent_id: Optional[str] = None


class MoveItemsRequest(BaseModel):
    """Request body for POST /library/{project_id}/move."""
    ids: List[str] = Field(..., min_length=1)
    target_folder_id: Optional[str] = None


class BatchDeleteRequest(BaseModel):
    """Request body for POST /library/{project_id}/batch-delete."""
    ids: List[str] = Field(..., min_length=1)


# ---------------------------------------------------------------------------
# Annotations
# ---------------------------------------------------------------------------

class CreateAnnotationRequest(BaseModel):
    """Request body for POST /annotations/{project_id}/create."""
    file_path: Optional[str] = Field(None, alias="filePath")
    from_pos: int = Field(..., alias="from", ge=0)
    to_pos: int = Field(..., alias="to", ge=0)
    original_text: Optional[str] = Field(None, alias="originalText")

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def validate_range(self):
        if self.to_pos <= self.from_pos:
            raise ValueError("Annotation range must be non-empty (to > from)")
        return self


class SaveAnnotationsRequest(BaseModel):
    """Request body for POST /annotations/{project_id}."""
    file_path: Optional[str] = Field(None, alias="filePath")
    annotations: List[Dict[str, Any]]

    model_config = {"populate_by_name": True}


class AnnotationStreamRequest(BaseModel):
    """Request body for POST /annotations/stream/{project_id}."""
    file_path: str = Field(..., alias="filePath")
    annotation_id: str = Field(..., alias="annotationId")

    model_config = {"populate_by_name": True}


class AnnotationReplyRequest(BaseModel):
    """Request body for POST /annotations/{project_id}/reply — append a user message."""
    annotation_id: str = Field(..., alias="annotationId")
    content: str = Field(..., min_length=1, max_length=100000)

    model_config = {"populate_by_name": True}

    @field_validator("content")
    @classmethod
    def validate_content_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Annotation reply content cannot be empty")
        return value


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

class ProjectCreate(BaseModel):
    """Request body for POST /projects."""
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = Field(None, max_length=500)
    template: Optional[str] = Field("latex", max_length=50)


class ProjectRegister(BaseModel):
    """Request body for POST /projects/register."""
    directory: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = Field(None, max_length=500)


class ProjectUpdate(BaseModel):
    """Request body for PATCH /projects/{project_id}."""
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    description: Optional[str] = Field(None, max_length=500)
    main_file: Optional[str] = None
    engine: Optional[str] = None


class ProjectConfigUpdate(BaseModel):
    """Request body for PATCH /projects/{project_id}/config."""
    snapshot_enabled: Optional[bool] = None
    snapshot_interval_minutes: Optional[int] = Field(None, ge=1)
    tips: Optional[str] = Field(None, max_length=6000)


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

class FileCreate(BaseModel):
    """Request body for POST /files/{project_id}/create."""
    path: str = Field(..., min_length=1)
    type: str  # "file" or "directory"


class FileRename(BaseModel):
    """Request body for POST /files/{project_id}/rename."""
    path: str = Field(..., min_length=1)
    new_name: str = Field(..., min_length=1, max_length=255)


class FileMove(BaseModel):
    """Request body for POST /files/{project_id}/move."""
    source: str = Field(..., min_length=1)
    destination: str = Field("", min_length=0)


class FileExtractRequest(BaseModel):
    """Request body for POST /files/{project_id}/extract."""
    path: str = Field(..., min_length=1)
    overwrite: bool = False
    skip_conflicts: bool = False


class FileBatchDownloadRequest(BaseModel):
    """Request body for POST /files/{project_id}/batch-download."""
    paths: List[str] = Field(..., min_length=1)


class FileContent(BaseModel):
    """Request body for POST /files/{project_id}/content."""
    content: str
    path: str
    force: bool = False
    hash: Optional[str] = None


# ---------------------------------------------------------------------------
# Compile
# ---------------------------------------------------------------------------

class CompileRequest(BaseModel):
    """Request body for POST /compile/{project_id}."""
    engine: Optional[str] = None
    main_file: Optional[str] = None


class SyncTeXRequest(BaseModel):
    """Request body for POST /compile/{project_id}/synctex."""
    type: Literal["forward", "backward"]
    file: Optional[str] = None
    line: Optional[int] = None
    column: Optional[int] = 0
    page: Optional[int] = None
    x: Optional[float] = None
    y: Optional[float] = None


# ---------------------------------------------------------------------------
# Permission
# ---------------------------------------------------------------------------

class PermissionRespondRequest(BaseModel):
    """Request body for POST /permissions/{project_id}/{task_id}/respond."""
    request_id: str = Field(..., min_length=1)
    approved: bool
    reason: Optional[str] = Field(default="", max_length=2000)


# ---------------------------------------------------------------------------
# Notebooks
# ---------------------------------------------------------------------------

class NotebookWriteRequest(BaseModel):
    """Request body for POST /notebooks/{project_id}."""
    path: str
    notebook: Dict[str, Any]


class CreateNotebookRequest(BaseModel):
    """Request body for POST /notebooks/{project_id}/create."""
    path: str


# ---------------------------------------------------------------------------
# Skills — file management
# ---------------------------------------------------------------------------

class SkillFileContentRequest(BaseModel):
    """Request body for PUT /skills/{skill_id}/files/content."""
    file_path: str = Field(..., min_length=1)
    content: str
    hash: Optional[str] = None


class SkillFileCreateRequest(BaseModel):
    """Request body for POST /skills/{skill_id}/files/create."""
    path: str = Field(..., min_length=1)
    type: str  # "file" or "directory"


class SkillFileRenameRequest(BaseModel):
    """Request body for POST /skills/{skill_id}/files/rename."""
    path: str = Field(..., min_length=1)
    new_name: str = Field(..., min_length=1, max_length=255)


class SkillImportGitRequest(BaseModel):
    """Request body for POST /skills/import/git."""
    url: str = Field(..., min_length=1, max_length=2000)


class SkillLoadRequest(BaseModel):
    """Request body for POST /chat/sessions/.../load-skill."""
    skill_id: str = Field(..., min_length=1, max_length=200)


# ---------------------------------------------------------------------------
# System / settings
# ---------------------------------------------------------------------------

class SettingsYamlUpdate(BaseModel):
    """Request body for POST /system/settings/validate-yaml — raw YAML string."""
    content: str


class SettingsDataUpdate(BaseModel):
    """Request body for POST /system/settings/yaml — structured config draft."""
    config: Dict[str, Any]


class SettingsUpdate(BaseModel):
    """Request body for PUT /system/settings and POST /system/settings/check."""
    content: Optional[str] = None
    config: Optional[Dict[str, Any]] = None


class ModelListRequest(BaseModel):
    """Request body for POST /system/litellm/models."""
    provider: str = ""
    base_url: str = ""
    api_key: str = ""


class TeXOperationRequest(BaseModel):
    """Request body for POST /system/tex/run."""
    operation: str
    repository: Optional[str] = None
    package: Optional[str] = None
    query: Optional[str] = None
    target_year: Optional[str] = None
