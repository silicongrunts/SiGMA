"""Regression tests for tiktoken counting over literal special tokens.

Ingested LLM-related documents (datasets, chat logs, model output) routinely
contain literal special tokens such as "<|endoftext|>". tiktoken refuses to
encode those by default, which crashed RAG indexing before the fix.
"""

import pytest

from app.services.chunker import count_content_units, custom_tokenizer


@pytest.mark.regression
@pytest.mark.parametrize("special", ["<|endoftext|>", "<|endofprompt|>"])
def test_count_content_units_encodes_special_tokens(special):
    """Special tokens in document text are counted as plain text."""
    assert count_content_units(f"before {special} after") > 0


@pytest.mark.regression
@pytest.mark.parametrize("special", ["<|endoftext|>", "<|endofprompt|>"])
def test_custom_tokenizer_encodes_special_tokens(special):
    """SentenceSplitter tokenizer path survives special tokens."""
    tokens = custom_tokenizer(f"done {special} next")
    assert len(tokens) > 0
