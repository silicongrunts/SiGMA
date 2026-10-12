"""ChatGPT provider policy for the LiteLLM chatgpt transport.

The Codex subscription endpoint accepts an arbitrary ``instructions`` value.
SiGMA requires that the model see *exactly* SiGMA's system prompt —
identical to every other provider — for the supervisor, every subagent, and
the annotation loop.

LiteLLM's chatgpt transformation prepends its built-in copy of the official
Codex instructions (``get_chatgpt_default_instructions``) to whatever the
caller supplied. That behaviour is defeated here by replacing the function
with one returning an empty string: the prepend branch checks
``base not in existing``, and the empty string is a substring of everything,
so the caller's instructions pass through byte-for-byte; requests without a
system message send ``instructions: ""``.

Two seams must be patched because the transformation module binds the
function by value at import time. The behavioral asserts at the end make any
litellm upgrade that renames the function, drops the seam, or changes the
prepend logic fail loudly at startup instead of silently re-introducing the
official prompt.

The same policy layer also restores prompt-cache affinity: the Codex backend
routes requests carrying the same body ``prompt_cache_key`` to the same
cache machines (best effort), and litellm's field allowlist strips that key
even when the caller supplies it. The stable per-conversation session id is
therefore injected into the body — mirroring the ``Session-Id`` request
header — so consecutive turns of one conversation stay cache-affine. The
transform is wrapped (not replaced) so litellm's own logic keeps running and
the session id is read with litellm's own extractor.
"""

from litellm.llms.chatgpt import common_utils as _chatgpt_common_utils
from litellm.llms.chatgpt.responses import transformation as _chatgpt_transformation
from litellm.types.router import GenericLiteLLMParams

from app.core.config import CHATGPT_TOKEN_DIR
from app.core.logging import get_logger

logger = get_logger(__name__)


def _no_default_instructions() -> str:
    return ""


_original_transform_responses_api_request = (
    _chatgpt_transformation.ChatGPTResponsesAPIConfig.transform_responses_api_request
)


def _transform_with_cache_key(
    config, model, input, response_api_optional_request_params, litellm_params, headers,
):
    request = _original_transform_responses_api_request(
        config, model, input, response_api_optional_request_params, litellm_params, headers,
    )
    session_id = _chatgpt_common_utils.get_chatgpt_session_id(litellm_params)
    if session_id and "prompt_cache_key" not in request:
        request["prompt_cache_key"] = session_id
    return request


_transform_with_cache_key._sigma_cache_key_patch = True


def apply_chatgpt_provider_policy() -> None:
    """Install the empty-instructions and cache-affinity policies.

    Raises (loudly, at startup) when the litellm version in the environment
    no longer matches the seams these policies depend on.
    """
    _chatgpt_common_utils.get_chatgpt_default_instructions = _no_default_instructions
    _chatgpt_transformation.get_chatgpt_default_instructions = _no_default_instructions

    config_cls = _chatgpt_transformation.ChatGPTResponsesAPIConfig
    if not getattr(config_cls.transform_responses_api_request, "_sigma_cache_key_patch", False):
        config_cls.transform_responses_api_request = _transform_with_cache_key

    config = config_cls()
    if config.authenticator.token_dir != str(CHATGPT_TOKEN_DIR):
        raise RuntimeError(
            "ChatGPT token mirror is misconfigured: litellm's Authenticator "
            f"reads '{config.authenticator.token_dir}' but SiGMA maintains "
            f"'{CHATGPT_TOKEN_DIR}'. CHATGPT_TOKEN_DIR must be set before "
            "litellm is first imported."
        )

    system_prompt = "You are SiGMA's supervisor. Use the tools provided."
    request = config.transform_responses_api_request(
        model="gpt-5.3-codex",
        input=[{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
        response_api_optional_request_params={"instructions": system_prompt},
        litellm_params=GenericLiteLLMParams(metadata={"session_id": "sigma-cache-canary"}),
        headers={},
    )
    if request.get("instructions") != system_prompt:
        raise RuntimeError(
            "ChatGPT instructions policy failed: the outgoing instructions no "
            "longer equal SiGMA's system prompt verbatim. The litellm chatgpt "
            "transformation likely changed — re-check the prepend seam before "
            "using the chatgpt provider."
        )
    if request.get("prompt_cache_key") != "sigma-cache-canary":
        raise RuntimeError(
            "ChatGPT cache-affinity policy failed: the outgoing request no "
            "longer carries prompt_cache_key from the session id. The litellm "
            "chatgpt transformation likely changed — re-check the transform "
            "wrap before using the chatgpt provider."
        )
    logger.info("ChatGPT provider policy installed (instructions passthrough and "
                "prompt_cache_key affinity verified)")
