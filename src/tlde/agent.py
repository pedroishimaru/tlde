"""Factory for building Copilot SDK sessions from AgentConfig."""

import asyncio
import re
import time
from typing import Callable

from copilot import CopilotClient
from copilot.generated.session_events import (
    AssistantMessageData,
    PermissionRequestKind,
    SessionIdleData,
)
from copilot.session import PermissionRequestResult

from tlde.config import AgentConfig
from tlde.observability import PipelineTrace, SessionObserver, SessionTrace

# mkdir with optional -p / -v flags; no shell metacharacters in the path
_ALLOWED_MKDIR = re.compile(r"^\s*mkdir(\s+-[pv]+)?\s+[^;&|$`]+$")

# Safe token: any non-whitespace character that is NOT a shell metacharacter.
# Blocks ; & | $ ` ( ) < > { }
_SAFE_TOKEN = r"[^;&|$`()<>{}\s]+"

# make -C <dir> with optional variables; renode / renode-test <args>; west build <args>
_ALLOWED_BUILD_AND_TEST = re.compile(
    rf"^\s*("
    rf"make(\s+-C\s+{_SAFE_TOKEN})?(\s+\w+={_SAFE_TOKEN})*(\s+-p\s+always)?(\s+{_SAFE_TOKEN})?"
    rf"|renode-test(\s+{_SAFE_TOKEN})+"
    rf"|renode(\s+{_SAFE_TOKEN})+"
    rf"|python\s+-m\s+robot(\s+{_SAFE_TOKEN})+"
    rf"|west\s+build(\s+{_SAFE_TOKEN})*"
    rf")\s*$"
)


def _make_shell_handler(allow_build: bool = False):
    """Return a permission handler that optionally allows build/test commands."""

    def handler(request, invocation):
        if request.kind != PermissionRequestKind.SHELL:
            return PermissionRequestResult(kind="approve-once")

        cmd = request.full_command_text or ""

        if _ALLOWED_MKDIR.match(cmd):
            return PermissionRequestResult(kind="approve-once")

        if allow_build and _ALLOWED_BUILD_AND_TEST.match(cmd):
            return PermissionRequestResult(kind="approve-once")

        print(f"[BLOCKED] shell: {cmd[:80]}")
        return PermissionRequestResult(kind="deny")

    return handler


def _approve_all_handler(request, invocation):
    """Approve every permission request, including shell commands."""
    return PermissionRequestResult(kind="approve-once")


class ModelUnavailableError(RuntimeError):
    """Raised when a configured model isn't available for the chosen provider."""


def _translate_session_error(e: Exception, config: AgentConfig) -> Exception:
    """Turn an opaque 'model not available' JSON-RPC error into actionable guidance."""
    msg = str(e)
    if "not available" in msg.lower() or "unknown model" in msg.lower():
        return ModelUnavailableError(
            f"Model '{config.model}' for agent '{config.name}' "
            f"(provider '{config.provider or 'github'}') is not available.\n"
            f"  Fix: set a valid slug for this role in your config's [models] "
            f"section.\n"
            f"  For OpenRouter, list current IDs with:\n"
            f"    curl -s https://openrouter.ai/api/v1/models | python -m json.tool | grep '\"id\"'\n"
            f"  (model IDs change as the catalog updates — e.g. glm-5 → glm-5.1).\n"
            f"  Original error: {msg}"
        )
    return e


# Default handler — approve everything.
_permission_handler = _approve_all_handler

# Extended handler for the test aggregator — also allows make / renode-test / west.
_test_permission_handler = _make_shell_handler(allow_build=True)


async def run_agent(
    config: AgentConfig,
    prompt: str,
    pipeline_trace: PipelineTrace | None = None,
    permission_handler=None,
) -> str:
    """Run a single-turn Copilot agent session.

    Args:
        config: Agent configuration.
        prompt: The user prompt to send.
        pipeline_trace: If provided, the session trace is added to it.
        permission_handler: Override the default shell permission handler.
            Use ``_test_permission_handler`` for agents that need to run
            builds and test commands.

Returns:
        The agent's final text response.
    """
    handler = permission_handler or _permission_handler
    provider = config.get_provider_dict()

    async with CopilotClient() as client:
        try:
            session_ctx = await client.create_session(
                on_permission_request=handler,
                model=config.model,
                mcp_servers=config.mcp_servers or None,
                skill_directories=["~/.copilot/skills"],
                custom_agents=[_agent_dict(config)],
                agent=config.name,
                provider=provider,
            )
        except Exception as e:
            translated = _translate_session_error(e, config)
            if translated is e:
                raise
            raise translated from e
        async with session_ctx as session:
            observer = SessionObserver(config.name)
            observer.attach(session)

            response = await _send_and_wait(session, prompt, label=config.name)

            trace = observer.finish()
            if pipeline_trace is not None:
                pipeline_trace.add(trace)

            return response


async def run_agent_interactive(
    config: AgentConfig,
    initial_prompt: str,
    get_feedback: Callable[[str], str | None],
    pipeline_trace: PipelineTrace | None = None,
    permission_handler=None,
) -> str:
    """Run a multi-turn Copilot agent session with a user feedback loop.

    The agent sends its initial response, then `get_feedback` is called
    with that response. If it returns a string, that feedback is sent back
    to the agent for another turn. If it returns None, the loop ends.

    Args:
        config: Agent configuration.
        initial_prompt: The first prompt to send.
        get_feedback: Called with the agent's response each turn.
            Return a string to continue iterating, or None to accept.
        pipeline_trace: If provided, the session trace is added to it.
        permission_handler: Override the default shell permission handler.
            Use ``_test_permission_handler`` for agents that need to run
            builds and test commands.

Returns:
        The agent's final accepted response.
    """
    handler = permission_handler or _permission_handler
    provider = config.get_provider_dict()

    async with CopilotClient() as client:
        try:
            session_ctx = await client.create_session(
                on_permission_request=handler,
                model=config.model,
                mcp_servers=config.mcp_servers or None,
                skill_directories=["~/.copilot/skills"],
                custom_agents=[_agent_dict(config)],
                agent=config.name,
                provider=provider,
            )
        except Exception as e:
            translated = _translate_session_error(e, config)
            if translated is e:
                raise
            raise translated from e
        async with session_ctx as session:
            observer = SessionObserver(config.name)
            observer.attach(session)

            response = await _send_and_wait(session, initial_prompt, label=config.name)

            while True:
                feedback = get_feedback(response)
                if feedback is None:
                    break
                response = await _send_and_wait(session, feedback, label=config.name)

            trace = observer.finish()
            if pipeline_trace is not None:
                pipeline_trace.add(trace)

            return response


async def _send_and_wait(session, prompt: str, label: str = "agent",
                         heartbeat_s: float = 20.0) -> str:
    """Send a prompt and wait for the agent to finish, returning its response.

    Emits a periodic heartbeat while waiting so a long (non-streaming) model
    call is visibly *working* rather than appearing frozen.
    """
    done = asyncio.Event()
    response_parts: list[str] = []

    def on_event(event):
        match event.data:
            case AssistantMessageData() as data:
                response_parts.append(data.content)
            case SessionIdleData():
                done.set()

    async def _heartbeat():
        from tlde.progress import write
        t0 = time.monotonic()
        try:
            while True:
                await asyncio.sleep(heartbeat_s)
                write(f"  … {label} working ({int(time.monotonic() - t0)}s elapsed)")
        except asyncio.CancelledError:
            pass

    unsubscribe = session.on(on_event)
    hb = asyncio.create_task(_heartbeat())
    try:
        await session.send(prompt)
        await done.wait()
    finally:
        hb.cancel()
        unsubscribe()

    return "\n".join(response_parts)


def _agent_dict(config: AgentConfig) -> dict:
    """Convert an AgentConfig to the dict format expected by the SDK."""
    d: dict = {
        "name": config.name,
        "description": config.description,
        "prompt": config.prompt,
    }
    if config.tools is not None:
        d["tools"] = config.tools
    if config.mcp_servers:
        d["mcp_servers"] = config.mcp_servers
    if config.skills:
        d["skills"] = config.skills
    return d
