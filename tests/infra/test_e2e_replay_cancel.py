"""``scripts/e2e_replay.py`` cancels the investigation it created when it gives up on it (timeout,
error, Ctrl-C), so a run it no longer watches does not keep the agents (or, in live mode, the
model) busy. The stack is replaced by an ``httpx.MockTransport``."""

from __future__ import annotations

import contextlib
import importlib.util
import sys
from pathlib import Path

import httpx
import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "e2e_replay.py"
INV = "inv_01TEST"


@pytest.fixture(scope="module")
def e2e():
    spec = importlib.util.spec_from_file_location("e2e_replay_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _investigation(status: str) -> dict:
    return {
        "id": INV,
        "scenario_id": "baseline-corroboration",
        "objective": "x",
        "status": status,
        "status_reason": None,
        "created_at": "2026-11-27T00:00:00Z",
        "updated_at": "2026-11-27T00:00:00Z",
        "deadline": "2026-11-27T00:10:00Z",
        "tokens_used": 0,
        "token_budget": 60000,
        "tasks": [],
    }


class Stack:
    def __init__(self, poll: httpx.Response | Exception, cancel_status: int = 200) -> None:
        self.poll = poll
        self.cancel_status = cancel_status
        self.calls: list[tuple[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path == "/api/v1/investigations":
            return httpx.Response(201, json={"id": INV})
        if request.method == "POST" and request.url.path.endswith("/cancel"):
            return httpx.Response(self.cancel_status, json={"id": INV, "status": "CANCELLED"})
        if request.method == "GET" and request.url.path == f"/api/v1/investigations/{INV}":
            if isinstance(self.poll, Exception):
                raise self.poll
            return self.poll
        return httpx.Response(404, json={})

    def client(self) -> httpx.Client:
        return httpx.Client(
            base_url="http://stack.test", transport=httpx.MockTransport(self.handler)
        )

    def cancels(self) -> list[tuple[str, str]]:
        return [c for c in self.calls if c[1].endswith("/cancel")]


def test_timeout_cancels_the_investigation(e2e, monkeypatch) -> None:
    monkeypatch.setattr(e2e.time, "sleep", lambda s: None)
    stack = Stack(httpx.Response(200, json=_investigation("RUNNING")))
    with stack.client() as client:
        violations = e2e.run_scenario(client, "baseline-corroboration", timeout_s=0.0)
    assert violations and "not terminal" in violations[0]
    assert stack.cancels() == [("POST", f"/api/v1/investigations/{INV}/cancel")]


def test_error_while_polling_cancels_and_reraises(e2e) -> None:
    stack = Stack(httpx.ReadTimeout("boom"))
    with stack.client() as client, pytest.raises(httpx.ReadTimeout):
        e2e.run_scenario(client, "baseline-corroboration", timeout_s=60.0)
    assert stack.cancels() == [("POST", f"/api/v1/investigations/{INV}/cancel")]


def test_ctrl_c_while_polling_cancels_and_reraises(e2e, monkeypatch) -> None:
    def interrupted(_: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(e2e.time, "sleep", interrupted)  # Ctrl-C lands in the poll sleep
    stack = Stack(httpx.Response(200, json=_investigation("RUNNING")))
    with stack.client() as client, pytest.raises(KeyboardInterrupt):
        e2e.run_scenario(client, "baseline-corroboration", timeout_s=60.0)
    assert stack.cancels() == [("POST", f"/api/v1/investigations/{INV}/cancel")]


def test_cancel_refused_or_gone_is_ignored(e2e, monkeypatch) -> None:
    monkeypatch.setattr(e2e.time, "sleep", lambda s: None)
    stack = Stack(httpx.Response(200, json=_investigation("RUNNING")), cancel_status=409)
    with stack.client() as client:
        violations = e2e.run_scenario(client, "baseline-corroboration", timeout_s=0.0)
    assert len(violations) == 1 and len(stack.cancels()) == 1


def test_terminal_run_is_not_cancelled(e2e) -> None:
    stack = Stack(httpx.Response(200, json=_investigation("FAILED")))
    # the checks after the poll hit the double's 404s; only the cancel matters here
    errors = (httpx.HTTPError, ValueError, KeyError, TypeError, RuntimeError)
    with stack.client() as client, contextlib.suppress(*errors):
        e2e.run_scenario(client, "baseline-corroboration", timeout_s=60.0)
    assert stack.cancels() == []
