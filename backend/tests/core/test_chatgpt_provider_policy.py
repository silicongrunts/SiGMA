"""Regression tests for the ChatGPT provider policy layer.

Two policies ride on the same transform seam, both verified through the real
litellm transformation so an upgrade that breaks it fails here (and at
startup via the policy's own asserts) instead of misbehaving silently:

* instructions passthrough — the Codex endpoint must receive exactly SiGMA's
  system prompt, never litellm's built-in copy of the official instructions;
* prompt-cache affinity — the stable per-conversation session id must ride in
  the body as ``prompt_cache_key`` (litellm's field allowlist strips it),
  the cache-affinity routing key the subscription backend honours.
"""

import pytest

from app.core.chatgpt_provider import apply_chatgpt_provider_policy


def _transform(instructions, session_id=None):
    from litellm.llms.chatgpt.responses.transformation import ChatGPTResponsesAPIConfig
    from litellm.types.router import GenericLiteLLMParams

    config = ChatGPTResponsesAPIConfig()
    metadata = {"session_id": session_id} if session_id else None
    return config.transform_responses_api_request(
        model="gpt-5.3-codex",
        input=[{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
        response_api_optional_request_params=(
            {"instructions": instructions} if instructions is not None else {}
        ),
        litellm_params=GenericLiteLLMParams(metadata=metadata),
        headers={},
    )


@pytest.mark.unit
@pytest.mark.regression
def test_instructions_passthrough_with_system_prompt():
    apply_chatgpt_provider_policy()
    system_prompt = "You are SiGMA's supervisor. Use the provided tools."
    request = _transform(system_prompt)
    assert request["instructions"] == system_prompt


@pytest.mark.unit
@pytest.mark.regression
def test_instructions_empty_without_system_prompt():
    """A call with no system message sends instructions="" — the wire shape
    the subscription endpoint is known to accept."""
    apply_chatgpt_provider_policy()
    request = _transform(None)
    assert request["instructions"] == ""


@pytest.mark.unit
def test_policy_is_idempotent():
    apply_chatgpt_provider_policy()
    apply_chatgpt_provider_policy()
    request = _transform("prompt", session_id="sess-1")
    assert request["instructions"] == "prompt"
    assert request["prompt_cache_key"] == "sess-1"


@pytest.mark.unit
@pytest.mark.regression
def test_prompt_cache_key_injected_from_session_id():
    """The stable session id rides in the body as prompt_cache_key (the
    cache-affinity routing key the Codex backend honours), and repeated
    turns of one session keep the same value."""
    apply_chatgpt_provider_policy()
    first = _transform("prompt", session_id="sess-abc")
    second = _transform("prompt", session_id="sess-abc")
    assert first["prompt_cache_key"] == "sess-abc"
    assert second["prompt_cache_key"] == "sess-abc"


@pytest.mark.unit
def test_prompt_cache_key_absent_without_session():
    """One-shot calls without a session stay untagged rather than getting a
    random per-request key (which would be churn without any affinity)."""
    apply_chatgpt_provider_policy()
    request = _transform("prompt", session_id=None)
    assert "prompt_cache_key" not in request


@pytest.mark.unit
@pytest.mark.regression
def test_builtin_instructions_would_be_prepended_without_policy():
    """Contrast guard: with litellm's shipped default (the built-in official
    text, still available as the CHATGPT_DEFAULT_INSTRUCTIONS constant) the
    transformation prepends it — proving the passthrough above really comes
    from the policy and not from a litellm behaviour change."""
    import litellm.llms.chatgpt.responses.transformation as rt
    from litellm.llms.chatgpt import common_utils

    def _shipped_default() -> str:
        return common_utils.CHATGPT_DEFAULT_INSTRUCTIONS

    try:
        rt.get_chatgpt_default_instructions = _shipped_default
        request = _transform("SiGMA prompt")
        assert request["instructions"] != "SiGMA prompt"
        assert request["instructions"].endswith("SiGMA prompt")
        assert request["instructions"].startswith(common_utils.CHATGPT_DEFAULT_INSTRUCTIONS)
    finally:
        apply_chatgpt_provider_policy()
