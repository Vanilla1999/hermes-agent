"""Isolated provider boundary for the bounded one-card planner."""
from __future__ import annotations

from dataclasses import dataclass

MODEL = "gpt-5.6-sol"
REASONING_EFFORT = "low"


class PlannerRuntimeError(RuntimeError):
    """The pinned Hermes provider boundary could not produce a response."""


@dataclass(frozen=True)
class PlannerRuntimeResult:
    text: str
    api_requests: int
    input_tokens: int | None
    output_tokens: int | None
    missing_metrics_reasons: dict[str, str]


def _metric(agent, *names: str) -> int | None:
    for name in names:
        value = getattr(agent, name, None)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


def run_isolated_planner(prompt: str) -> PlannerRuntimeResult:
    """Run one provider request with no tools, memory, rules, fallback, or session DB."""
    from hermes_cli.models import detect_provider_for_model
    from hermes_cli.runtime_provider import resolve_runtime_provider
    from run_agent import AIAgent

    detected = detect_provider_for_model(MODEL, "auto")
    requested_provider, effective_model = detected or (None, MODEL)
    runtime = resolve_runtime_provider(
        requested=requested_provider, target_model=effective_model
    )
    agent = AIAgent(
        api_key=runtime.get("api_key"), base_url=runtime.get("base_url"),
        provider=runtime.get("provider"), api_mode=runtime.get("api_mode"),
        credential_pool=runtime.get("credential_pool"), model=effective_model,
        enabled_toolsets=[], max_iterations=1, quiet_mode=True,
        reasoning_config={"enabled": True, "effort": REASONING_EFFORT},
        max_tokens=2048, skip_context_files=True, load_soul_identity=False,
        skip_memory=True, session_db=None, fallback_model=None, platform="tool",
    )
    response = agent.chat(prompt)
    if not isinstance(response, str) or not response.strip():
        raise PlannerRuntimeError("planner produced no final response")
    api_requests = _metric(agent, "_api_call_count")
    if api_requests != 1:
        raise PlannerRuntimeError(f"planner_request_cardinality: expected=1 actual={api_requests}")
    input_tokens = _metric(agent, "total_input_tokens", "input_tokens")
    output_tokens = _metric(agent, "total_output_tokens", "output_tokens")
    missing = {}
    if input_tokens is None:
        missing["input_tokens"] = "pinned_hermes_did_not_expose_metric"
    if output_tokens is None:
        missing["output_tokens"] = "pinned_hermes_did_not_expose_metric"
    return PlannerRuntimeResult(response, api_requests, input_tokens, output_tokens, missing)
