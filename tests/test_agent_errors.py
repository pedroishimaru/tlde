"""Agent error-translation tests: a 'model not available' provider error becomes
actionable guidance (and a clean exit) instead of a raw JSON-RPC traceback."""

from __future__ import annotations

from tlde.agent import ModelUnavailableError, _translate_session_error
from tlde.config import AgentConfig


def _cfg():
    return AgentConfig(name="firmware_emulation_manager", model="z-ai/glm-5",
                       provider="openrouter")


def test_model_unavailable_is_translated():
    raw = Exception('session.create failed: Model "z-ai/glm-5" is not available')
    err = _translate_session_error(raw, _cfg())
    assert isinstance(err, ModelUnavailableError)
    msg = str(err)
    assert "z-ai/glm-5" in msg and "firmware_emulation_manager" in msg
    assert "openrouter" in msg and "openrouter.ai/api/v1/models" in msg


def test_unknown_model_phrasing_is_translated():
    raw = Exception("unknown model foo/bar")
    assert isinstance(_translate_session_error(raw, _cfg()), ModelUnavailableError)


def test_unrelated_error_passes_through():
    raw = ValueError("network timeout")
    assert _translate_session_error(raw, _cfg()) is raw  # not translated
