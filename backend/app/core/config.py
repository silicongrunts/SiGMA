"""
SiGMA configuration.

The single source of truth is ``settings.yaml`` under ``userdata``. This module
owns parsing, validation, atomic writes, and the process-wide settings object
used by the rest of the backend.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# Canonical project root — the single source of truth for path resolution.
# Other core modules reuse settings.ROOT_DIR instead of recomputing the path
# from __file__.
ROOT_DIR = Path(__file__).resolve().parent.parent.parent.parent
# userdata_dir is resolved from the SIGMA_USERDATA_DIR env var only — it is no
# longer a settings.yaml field, so the same userdata tree is portable between
# host dev and the Docker image (/app/userdata) without rewriting settings.yaml.
_env_userdata = Path(os.getenv("SIGMA_USERDATA_DIR", str(ROOT_DIR / "userdata"))).expanduser()
USERDATA_DIR = _env_userdata if _env_userdata.is_absolute() else (ROOT_DIR / _env_userdata).resolve()
SIGMA_DIR = USERDATA_DIR / ".SiGMA"
SETTINGS_FILE = USERDATA_DIR / "settings.yaml"
PROJECT_NAME = "SiGMA"
# Override the Jupyter executable path (e.g. for venv/conda installs).
JUPYTER_BIN = os.getenv("SIGMA_JUPYTER_BIN", "jupyter")
TEXLIVE_ROOT = os.getenv("SIGMA_TEXLIVE_ROOT", "/usr/local/texlive")
DEFAULT_TEXLIVE_YEAR = os.getenv("SIGMA_TEXLIVE_YEAR", "2025")
UPDATE_TLMGR_BIN = os.getenv("SIGMA_UPDATE_TLMGR_BIN", "")
DEFAULT_MAX_CONTEXT_LENGTH = 200_000
NORMAL_RESPONSE_MAX_TOKENS = 32_000
COMPACT_RESPONSE_MAX_TOKENS = 20_000


class AppSettings(BaseModel):
    api_prefix: str = "/api/v1"


class ModelSettings(BaseModel):
    model: str = ""
    provider: str = ""
    base_url: str = ""
    api_key: str = ""
    reuse: str = ""
    extra: dict[str, Any] = Field(default_factory=dict)
    max_context_length: int | None = None
    compress_threshold: int | None = None
    source: str = ""
    hf_endpoint: str = ""
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh", "default"] | None = None

    @field_validator("model", "provider", "base_url", "api_key", "reuse", "source", "hf_endpoint", mode="before")
    @classmethod
    def _string_or_empty(cls, value: Any) -> str:
        return "" if value is None else str(value).strip()

    @field_validator("temperature", "top_p", "reasoning_effort", mode="before")
    @classmethod
    def _coerce_optional_value(cls, value: Any) -> Any:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return value

    @field_validator("source")
    @classmethod
    def _validate_source(cls, value: str) -> str:
        allowed = {"", "huggingface", "modelscope"}
        if value not in allowed:
            raise ValueError(f"source must be one of {sorted(allowed - {''})!r}")
        return value

    @field_validator("base_url")
    @classmethod
    def _trim_base_url(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("max_context_length", "compress_threshold")
    @classmethod
    def _positive_optional_int(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("must be a positive integer")
        return value

    @model_validator(mode="after")
    def _validate_context_budget(self) -> "ModelSettings":
        if self.compress_threshold is None:
            return self
        if self.max_context_length is None:
            raise ValueError("max_context_length is required when compress_threshold is set")
        max_allowed = self.max_context_length - 20_000
        if max_allowed < 30_000:
            raise ValueError("max_context_length must be at least 50000 when compress_threshold is set")
        if not 30_000 <= self.compress_threshold <= max_allowed:
            raise ValueError("compress_threshold must be between 30000 and max_context_length - 20000")
        return self


class ModelRoleSettings(BaseModel):
    supervisor: ModelSettings = Field(default_factory=ModelSettings)
    ra: ModelSettings = Field(default_factory=ModelSettings)
    draw: ModelSettings = Field(default_factory=ModelSettings)
    embedding: ModelSettings = Field(default_factory=ModelSettings)
    vision: ModelSettings = Field(default_factory=ModelSettings)
    rerank: ModelSettings = Field(default_factory=ModelSettings)

    @model_validator(mode="after")
    def _validate_role_reuse(self) -> "ModelRoleSettings":
        allowed = {
            "supervisor": set(),
            "ra": {"supervisor"},
            "vision": {"supervisor", "ra"},
            "draw": set(),
            "embedding": set(),
            "rerank": set(),
        }
        for role, sources in allowed.items():
            reuse = getattr(self, role).reuse
            if reuse and reuse not in sources:
                if sources:
                    allowed_text = ", ".join(sorted(sources))
                    raise ValueError(f"models.{role}.reuse must be one of: {allowed_text}")
                raise ValueError(f"models.{role}.reuse is not supported")
        return self


class RetrySettings(BaseModel):
    max_retries: int = Field(default=10, ge=1)
    delay: float = Field(default=2.0, gt=0)
    backoff: float = Field(default=2.0, gt=1)
    max_delay: float = Field(default=64.0, gt=0)


class LoggingSettings(BaseModel):
    level: str = "INFO"
    retention_days: int = 14

    @field_validator("level", mode="before")
    @classmethod
    def _normalize_level(cls, value: Any) -> str:
        level = str(value or "").strip().upper()
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if level not in allowed:
            raise ValueError(f"logging.level must be one of: {', '.join(sorted(allowed))}")
        return level

    @field_validator("retention_days")
    @classmethod
    def _positive_retention(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("logging.retention_days must be a positive integer")
        return value


class LatexSettings(BaseModel):
    engines: list[str] = Field(default_factory=lambda: ["pdflatex", "xelatex", "lualatex", "latex"])

    @field_validator("engines")
    @classmethod
    def _non_empty_engines(cls, value: list[str]) -> list[str]:
        engines = [engine.strip() for engine in value if engine and engine.strip()]
        if not engines:
            raise ValueError("latex.engines must contain at least one engine")
        return engines


class BrowserSettings(BaseModel):
    chrome_bin: str = "chromium-browser"
    dom_max_chars: int = 32000
    fold_threshold: int = 5
    console_buffer_size: int = 200
    tool_timeout: int = 30
    tab_idle_timeout: int = 60
    search_engine_url: str = "https://www.google.com/search?q="


class TerminalSettings(BaseModel):
    max_sessions: int = 10
    grace_period: int = 600


class WorkerSettings(BaseModel):
    library_workers: int = 1
    library_queue_batch_size: int = 20
    task_lease_seconds: int = 600
    task_cleanup_hours: int = 24
    library_scan_project_timeout_seconds: int = 30
    library_scan_total_timeout_seconds: int = 120

    @field_validator(
        "library_workers",
        "library_queue_batch_size",
        "task_lease_seconds",
        "task_cleanup_hours",
        "library_scan_project_timeout_seconds",
        "library_scan_total_timeout_seconds",
    )
    @classmethod
    def _positive_int(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("must be a positive integer")
        return value


class LibrarySettings(BaseModel):
    top_k: int = 5
    candidate_pool_size: int = 15
    chunk_max_units: int = 512
    chunk_min_units: int = 100
    chunk_overlap_units: int = 55
    reranker_enabled: bool = True
    max_matches_per_doc: int = 3
    query_instruction: str = ""
    auto_ai_metadata_enabled: bool = True
    ai_metadata_max_input_tokens: int = 40_000
    ai_metadata_output_tokens: int = 20_000

    @field_validator(
        "top_k",
        "candidate_pool_size",
        "chunk_max_units",
        "chunk_min_units",
        "chunk_overlap_units",
        "max_matches_per_doc",
        "ai_metadata_max_input_tokens",
        "ai_metadata_output_tokens",
    )
    @classmethod
    def _positive_int(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("must be a positive integer")
        return value


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    app: AppSettings = Field(default_factory=AppSettings)
    models: ModelRoleSettings = Field(default_factory=ModelRoleSettings)
    retry: RetrySettings = Field(default_factory=RetrySettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    latex: LatexSettings = Field(default_factory=LatexSettings)
    browser: BrowserSettings = Field(default_factory=BrowserSettings)
    terminal: TerminalSettings = Field(default_factory=TerminalSettings)
    workers: WorkerSettings = Field(default_factory=WorkerSettings)
    library: LibrarySettings = Field(default_factory=LibrarySettings)

    # NOTE: keep this model side-effect-free. Directories are created by
    # ``lifecycle.startup_event`` and by ``write_settings_file``; validating
    # user-supplied YAML through ``Settings.model_validate`` must stay pure.

    @property
    def PROJECT_NAME(self) -> str:
        return PROJECT_NAME

    @property
    def API_PREFIX(self) -> str:
        return self.app.api_prefix

    @property
    def ROOT_DIR(self) -> Path:
        return ROOT_DIR

    @property
    def USERDATA_DIR(self) -> Path:
        return USERDATA_DIR

    @property
    def SIGMA_DIR(self) -> Path:
        return SIGMA_DIR

    @property
    def JUPYTER_BIN(self) -> str:
        return JUPYTER_BIN

    @property
    def SUPERVISOR_MODEL(self) -> str:
        return self.model_settings_for_role("supervisor").model

    @property
    def SUPERVISOR_PROVIDER(self) -> str:
        return self.model_settings_for_role("supervisor").provider

    @property
    def SUPERVISOR_BASE_URL(self) -> str:
        return self.model_settings_for_role("supervisor").base_url

    @property
    def SUPERVISOR_API_KEY(self) -> str:
        return self.model_settings_for_role("supervisor").api_key

    @property
    def SUPERVISOR_EXTRA_JSON(self) -> dict[str, Any]:
        return self.model_settings_for_role("supervisor").extra

    @property
    def RA_MODEL(self) -> str:
        return self.model_settings_for_role("ra").model

    @property
    def RA_PROVIDER(self) -> str:
        return self.model_settings_for_role("ra").provider

    @property
    def RA_BASE_URL(self) -> str:
        return self.model_settings_for_role("ra").base_url

    @property
    def RA_API_KEY(self) -> str:
        return self.model_settings_for_role("ra").api_key

    @property
    def RA_EXTRA_JSON(self) -> dict[str, Any]:
        return self.model_settings_for_role("ra").extra

    @property
    def DRAW_MODEL(self) -> str:
        return self.model_settings_for_role("draw").model

    @property
    def DRAW_PROVIDER(self) -> str:
        return self.model_settings_for_role("draw").provider

    @property
    def DRAW_BASE_URL(self) -> str:
        return self.model_settings_for_role("draw").base_url

    @property
    def DRAW_API_KEY(self) -> str:
        return self.model_settings_for_role("draw").api_key

    @property
    def DRAW_EXTRA_JSON(self) -> dict[str, Any]:
        return self.model_settings_for_role("draw").extra

    @property
    def VISION_MODEL(self) -> str:
        return self.model_settings_for_role("vision").model

    @property
    def VISION_PROVIDER(self) -> str:
        return self.model_settings_for_role("vision").provider

    @property
    def VISION_BASE_URL(self) -> str:
        return self.model_settings_for_role("vision").base_url

    @property
    def VISION_API_KEY(self) -> str:
        return self.model_settings_for_role("vision").api_key

    @property
    def VISION_EXTRA_JSON(self) -> dict[str, Any]:
        return self.model_settings_for_role("vision").extra

    @property
    def EMBEDDING_MODEL(self) -> str:
        return self.models.embedding.model

    @property
    def EMBEDDING_PROVIDER(self) -> str:
        return self.models.embedding.provider

    @property
    def EMBEDDING_BASE_URL(self) -> str:
        return self.models.embedding.base_url

    @property
    def EMBEDDING_API_KEY(self) -> str:
        return self.models.embedding.api_key

    @property
    def EMBEDDING_EXTRA_JSON(self) -> dict[str, Any]:
        return self.models.embedding.extra

    @property
    def RERANKER_MODEL(self) -> str:
        return self.models.rerank.model

    @property
    def RERANKER_PROVIDER(self) -> str:
        return self.models.rerank.provider

    @property
    def RERANKER_BASE_URL(self) -> str:
        return self.models.rerank.base_url

    @property
    def RERANKER_API_KEY(self) -> str:
        return self.models.rerank.api_key

    @property
    def RERANKER_EXTRA_JSON(self) -> dict[str, Any]:
        return self.models.rerank.extra

    @property
    def EMBEDDING_SOURCE(self) -> str:
        return self.models.embedding.source

    @property
    def EMBEDDING_HF_ENDPOINT(self) -> str:
        return self.models.embedding.hf_endpoint

    @property
    def RERANKER_SOURCE(self) -> str:
        return self.models.rerank.source

    @property
    def RERANKER_HF_ENDPOINT(self) -> str:
        return self.models.rerank.hf_endpoint

    @property
    def COMPRESSION_THRESHOLD(self) -> int:
        return self.compact_threshold_for_role("supervisor")

    def model_settings_for_role(self, role: str, seen: set[str] | None = None) -> ModelSettings:
        seen = seen or set()
        if role in seen:
            raise ValueError(f"Model reuse cycle detected at role: {role}")
        role_settings = getattr(self.models, role, None)
        if role_settings is None:
            raise ValueError(f"Unknown model role: {role}")
        if not role_settings.reuse:
            return role_settings
        return self.model_settings_for_role(role_settings.reuse, seen | {role})

    def max_context_length_for_role(self, role: str) -> int:
        role_settings = self.model_settings_for_role(role)
        configured = getattr(role_settings, "max_context_length", None)
        return configured or DEFAULT_MAX_CONTEXT_LENGTH

    def compact_threshold_for_role(self, role: str) -> int:
        role_settings = self.model_settings_for_role(role)
        configured = getattr(role_settings, "compress_threshold", None)
        if configured:
            return configured
        max_context = self.max_context_length_for_role(role)
        return min(
            DEFAULT_MAX_CONTEXT_LENGTH,
            int(max_context * 0.8),
            max_context - COMPACT_RESPONSE_MAX_TOKENS,
        )

    @property
    def NORMAL_RESPONSE_MAX_TOKENS(self) -> int:
        return NORMAL_RESPONSE_MAX_TOKENS

    @property
    def COMPACT_RESPONSE_MAX_TOKENS(self) -> int:
        return COMPACT_RESPONSE_MAX_TOKENS

    @property
    def MAX_RETRIES(self) -> int:
        return self.retry.max_retries

    @property
    def RETRY_DELAY(self) -> float:
        return self.retry.delay

    @property
    def RETRY_BACKOFF(self) -> float:
        return self.retry.backoff

    @property
    def RETRY_MAX_DELAY(self) -> float:
        return self.retry.max_delay

    @property
    def LOG_LEVEL(self) -> str:
        return self.logging.level

    @property
    def LOG_RETENTION_DAYS(self) -> int:
        return self.logging.retention_days

    @property
    def LATEX_ENGINES(self) -> list[str]:
        return self.latex.engines

    @property
    def DEFAULT_LATEX_ENGINE(self) -> str:
        return self.latex.engines[0] if self.latex.engines else "pdflatex"

    @property
    def CHROME_BIN(self) -> str:
        return self.browser.chrome_bin

    @property
    def BROWSER_DOM_MAX_CHARS(self) -> int:
        return self.browser.dom_max_chars

    @property
    def BROWSER_FOLD_THRESHOLD(self) -> int:
        return self.browser.fold_threshold

    @property
    def BROWSER_CONSOLE_BUFFER_SIZE(self) -> int:
        return self.browser.console_buffer_size

    @property
    def BROWSER_TOOL_TIMEOUT(self) -> int:
        return self.browser.tool_timeout

    @property
    def BROWSER_TAB_IDLE_TIMEOUT(self) -> int:
        return self.browser.tab_idle_timeout

    @property
    def BROWSER_SEARCH_ENGINE_URL(self) -> str:
        return self.browser.search_engine_url

    @property
    def TERMINAL_MAX_SESSIONS(self) -> int:
        return self.terminal.max_sessions

    @property
    def TERMINAL_GRACE_PERIOD(self) -> int:
        return self.terminal.grace_period

    @property
    def LIBRARY_WORKERS(self) -> int:
        return self.workers.library_workers

    @property
    def LIBRARY_QUEUE_BATCH_SIZE(self) -> int:
        return self.workers.library_queue_batch_size

    @property
    def BACKGROUND_TASK_LEASE_SECONDS(self) -> int:
        return self.workers.task_lease_seconds

    @property
    def BACKGROUND_TASK_CLEANUP_HOURS(self) -> int:
        return self.workers.task_cleanup_hours

    @property
    def LIBRARY_SCAN_PROJECT_TIMEOUT_SECONDS(self) -> int:
        return self.workers.library_scan_project_timeout_seconds

    @property
    def LIBRARY_SCAN_TOTAL_TIMEOUT_SECONDS(self) -> int:
        return self.workers.library_scan_total_timeout_seconds

    @property
    def RAG_TOP_K(self) -> int:
        return self.library.top_k

    @property
    def RAG_CANDIDATE_POOL_SIZE(self) -> int:
        return self.library.candidate_pool_size

    @property
    def RAG_CHUNK_MAX_UNITS(self) -> int:
        return self.library.chunk_max_units

    @property
    def RAG_CHUNK_MIN_UNITS(self) -> int:
        return self.library.chunk_min_units

    @property
    def RAG_CHUNK_OVERLAP_UNITS(self) -> int:
        return self.library.chunk_overlap_units

    @property
    def RAG_RERANKER_ENABLED(self) -> bool:
        return self.library.reranker_enabled

    @property
    def RAG_MAX_MATCHES_PER_DOC(self) -> int:
        return self.library.max_matches_per_doc

    @property
    def RAG_QUERY_INSTRUCTION(self) -> str:
        return self.library.query_instruction

    @property
    def AUTO_AI_METADATA_ENABLED(self) -> bool:
        return self.library.auto_ai_metadata_enabled

    @property
    def AI_METADATA_MAX_INPUT_TOKENS(self) -> int:
        return self.library.ai_metadata_max_input_tokens

    @property
    def AI_METADATA_OUTPUT_TOKENS(self) -> int:
        return self.library.ai_metadata_output_tokens

    def get_project_path(self, project_id: str) -> Path:
        return (self.USERDATA_DIR / project_id).resolve()

    def get_sigma_path(self, project_id: str) -> Path:
        return self.get_project_path(project_id) / ".SiGMA"

    def validate_path(self, path: str, project_id: str) -> bool:
        try:
            from app.core.utils import is_within
            project_path = self.get_project_path(project_id)
            full_path = (project_path / path).resolve()
            return is_within(full_path, project_path.resolve())
        except (OSError, ValueError):
            return False

    def is_safe_filename(self, filename: str) -> bool:
        if not filename or filename in {".", ".."}:
            return False
        if "/" in filename or "\\" in filename:
            return False
        if filename.startswith("."):
            return False
        if any(c in filename for c in ["\x00", "\n", "\r"]):
            return False
        return True


def load_settings_file(path: Path = SETTINGS_FILE) -> Settings:
    if not path.exists():
        config = Settings()
        write_settings_file(config, path)
        return config

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML in {path}") from exc

    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return Settings.model_validate(raw)


def dump_settings_yaml(config: Settings) -> str:
    return yaml.safe_dump(settings_to_dict(config), allow_unicode=False, sort_keys=False)


def settings_to_dict(config: Settings) -> dict[str, Any]:
    # model_dump emits pydantic fields only; derived @property values such as
    # COMPRESSION_THRESHOLD are never included, so no exclude set is needed.
    return config.model_dump(mode="json")


def validate_settings_yaml(content: str) -> Settings:
    try:
        raw = yaml.safe_load(content) or {}
    except yaml.YAMLError as exc:
        raise ValueError("Invalid YAML") from exc
    if not isinstance(raw, dict):
        raise ValueError("settings.yaml must contain a YAML mapping")
    return Settings.model_validate(raw)


def write_settings_file(config: Settings, path: Path = SETTINGS_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = dump_settings_yaml(config)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, path)
    finally:
        tmp_path = Path(tmp_name)
        if tmp_path.exists():
            tmp_path.unlink()


def save_settings_yaml(content: str, path: Path = SETTINGS_FILE) -> Settings:
    config = validate_settings_yaml(content)
    write_settings_file(config, path)
    return reload_settings(config)


def save_settings_data(data: dict[str, Any], path: Path = SETTINGS_FILE) -> Settings:
    config = Settings.model_validate(data)
    write_settings_file(config, path)
    return reload_settings(config)


def reload_settings(config: Settings | None = None) -> Settings:
    """Replace the global settings' fields in place.

    Field-by-field ``setattr`` (instead of poking ``__dict__``) keeps
    ``validate_assignment`` active, so field validators and cross-field
    ``model_validator(mode="after")`` checks re-run on every reload.
    """
    new_settings = config or load_settings_file()
    for field_name in Settings.model_fields:
        setattr(settings, field_name, getattr(new_settings, field_name))
    return settings


settings = load_settings_file()
