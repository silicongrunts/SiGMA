"""Model role configuration for SiGMA provider calls.

Application code should depend on logical roles, not provider names or model
IDs.  This module is the single place that maps those roles to LiteLLM-style
call parameters.
"""

from dataclasses import dataclass, field
from typing import Any, Literal

from app.core.config import settings

ModelRole = Literal["supervisor", "ra", "draw", "embedding", "vision", "rerank"]

# Roles that produce text and therefore honour sampling parameters.
_SAMPLING_ROLES = frozenset({"supervisor", "ra", "vision"})
# Roles that may pull model weights from HuggingFace / ModelScope.
_HF_ROLES = frozenset({"embedding", "rerank"})


@dataclass(frozen=True)
class ModelEndpoint:
    """Resolved model endpoint for one logical role."""

    role: ModelRole
    model: str
    provider: str = ""
    api_key: str = ""
    api_base: str = ""
    extra: dict[str, Any] = field(default_factory=dict)
    source: str = ""
    hf_endpoint: str = ""
    temperature: float | None = None
    top_p: float | None = None
    reasoning_effort: str | None = None

    @property
    def is_local(self) -> bool:
        return not (self.provider or self.api_base or self.api_key or self.extra)

    @property
    def litellm_model(self) -> str:
        """Return the model string LiteLLM should receive."""
        model = self.model.strip()
        provider = self.provider.strip()
        if not model or not provider:
            return model
        if model == provider or model.startswith(f"{provider}/"):
            return model
        return f"{provider}/{model}"

    def litellm_kwargs(self) -> dict[str, Any]:
        """Return non-secret LiteLLM call kwargs derived from this endpoint."""
        kwargs: dict[str, Any] = {}
        if self.api_key:
            kwargs["api_key"] = self.api_key
        if self.api_base:
            kwargs["api_base"] = self.api_base
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        if self.top_p is not None:
            kwargs["top_p"] = self.top_p
        if self.reasoning_effort is not None:
            kwargs["reasoning_effort"] = self.reasoning_effort
        kwargs.update(self.extra)
        return kwargs


def get_model_endpoint(role: ModelRole) -> ModelEndpoint:
    """Resolve one logical role into a model endpoint.

    All roles share a single resolution path through
    ``settings.model_settings_for_role``, which honours the ``reuse`` chain
    (e.g. ``ra`` may reuse ``supervisor``).  Only text-generation roles
    (``_SAMPLING_ROLES``) forward sampling parameters, and only the
    embedding/rerank roles (``_HF_ROLES``) forward the HuggingFace /
    ModelScope ``source`` / ``hf_endpoint`` fields — matching each role's
    provider capabilities.
    """
    normalized = role.lower()
    # ``model_settings_for_role`` raises ``ValueError("Unknown model role: …")``
    # for unrecognised roles, so no extra guard is needed here.
    ms = settings.model_settings_for_role(normalized)

    use_sampling = normalized in _SAMPLING_ROLES
    use_hf = normalized in _HF_ROLES
    return _endpoint(
        role=normalized,  # type: ignore[arg-type]
        model=ms.model,
        provider=ms.provider,
        api_key=ms.api_key,
        api_base=ms.base_url,
        extra=ms.extra,
        source=ms.source if use_hf else "",
        hf_endpoint=ms.hf_endpoint if use_hf else "",
        temperature=ms.temperature if use_sampling else None,
        top_p=ms.top_p if use_sampling else None,
        reasoning_effort=ms.reasoning_effort if use_sampling else None,
    )


def model_role_accepts_images(role: str) -> bool:
    """Return whether a logical role can receive image parts directly.

    SiGMA treats only the configured vision role and roles that vision explicitly
    reuses as multimodal. Matching provider/model strings is intentionally not
    enough because users may configure identical text-only aliases.
    """
    role = role.lower()
    if role == "vision":
        return True

    current = settings.models.vision.reuse
    seen: set[str] = set()
    while current:
        if current == role:
            return True
        if current in seen:
            return False
        seen.add(current)
        role_settings = getattr(settings.models, current, None)
        current = getattr(role_settings, "reuse", "") if role_settings else ""
    return False


def _endpoint(
    *,
    role: ModelRole,
    model: str,
    provider: str = "",
    api_key: str = "",
    api_base: str = "",
    extra: dict[str, Any] | None = None,
    source: str = "",
    hf_endpoint: str = "",
    temperature: float | None = None,
    top_p: float | None = None,
    reasoning_effort: str | None = None,
) -> ModelEndpoint:
    return ModelEndpoint(
        role=role,
        model=model.strip(),
        provider=provider.strip(),
        api_key=api_key.strip(),
        api_base=api_base.strip().rstrip("/"),
        extra=extra or {},
        source=source.strip(),
        hf_endpoint=hf_endpoint.strip(),
        temperature=temperature,
        top_p=top_p,
        reasoning_effort=reasoning_effort,
    )
