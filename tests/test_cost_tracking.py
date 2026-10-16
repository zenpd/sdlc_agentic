"""Tests for converting OpenHands LLM metrics into per-run usage updates."""

from types import SimpleNamespace

from backend.jira_bot import _report_usage


def _metrics(
    *,
    prompt_tokens=0,
    completion_tokens=0,
    cost=0.0,
    requests=0,
    priced_requests=0,
):
    return SimpleNamespace(
        accumulated_token_usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        ),
        accumulated_cost=cost,
        response_latencies=[object() for _ in range(requests)],
        costs=[object() for _ in range(priced_requests)],
    )


def test_report_usage_forwards_tokens_cost_and_pricing_coverage():
    captured = []

    _report_usage(
        lambda *values: captured.append(values),
        "agent",
        _metrics(
            prompt_tokens=1200,
            completion_tokens=300,
            cost=0.025,
            requests=4,
            priced_requests=3,
        ),
    )

    assert captured == [("agent", 1200, 300, 0.025, 4, 3)]


def test_report_usage_marks_unpriced_cost_as_unavailable_not_zero():
    captured = []

    _report_usage(
        lambda *values: captured.append(values),
        "gate",
        _metrics(prompt_tokens=80, completion_tokens=20, requests=1),
    )

    assert captured == [("gate", 80, 20, None, 1, 0)]


def test_report_usage_handles_empty_metrics():
    captured = []

    _report_usage(
        lambda *values: captured.append(values),
        "verify",
        _metrics(),
    )

    assert captured == [("verify", 0, 0, None, 0, 0)]
