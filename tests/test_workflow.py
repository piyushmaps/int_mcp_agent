"""Complete API/graph/source workflows using real local MCP processes."""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from src.api.main import create_app
from src.config import Settings
from src.contracts import Actor
from src.eval.run import GOLDEN_PATH, check_case
from src.policy.auth import Authenticator


@pytest.fixture
def application(tmp_path):
    return create_app(Settings(_env_file=None, app_env="test", data_dir=tmp_path,
                               model_mode="demo", auth_mode="demo", mcp_transport="stdio"))


@pytest.fixture
def client(application):
    with TestClient(application) as result:
        result.headers["Authorization"] = "Bearer local-demo-token"
        yield result


GOLDEN_CASES = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", GOLDEN_CASES, ids=[case["id"] for case in GOLDEN_CASES])
def test_golden_answers_have_supported_facts_citations_and_abstentions(client, case):
    response = client.post("/api/ask", json={"question": case["question"]})
    checks = check_case(case, response.status_code, response.json())
    assert all(checks.values()), [name for name, passed in checks.items() if not passed]


def test_followup_resolves_entities_but_reads_sources_again(client, application):
    first = client.post("/api/ask", json={"question": "Who owns Payments?"}).json()
    calls = []
    retrieve = application.state.engine.retriever.retrieve

    def observed_retrieve(query, **kwargs):
        calls.append(query)
        return retrieve(query, **kwargs)

    application.state.engine.retriever.retrieve = observed_retrieve
    second = client.post("/api/ask", json={"question": "Where is its rollout guide?", "session_id": first["session_id"]})
    assert second.status_code == 200
    body = second.json()
    assert body["session_id"] == first["session_id"]
    assert any("Payments" in question for question in calls)
    assert any(c["source_id"] == "payments-rollout" for c in body["citations"])


def parse_sse(text):
    events = []
    for block in text.strip().split("\n\n"):
        lines = block.splitlines()
        event = next((line[7:] for line in lines if line.startswith("event: ")), None)
        data = next((line[6:] for line in lines if line.startswith("data: ")), None)
        if event and data:
            events.append((event, json.loads(data)))
    return events


def test_sse_emits_only_safe_progress_then_one_checked_answer(client):
    question = "Why is PAY-123 delayed, and where is its rollout guide?"
    response = client.post("/api/chat", json={"question": question})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(response.text)
    assert events[0][0] == "progress" and events[0][1]["stage"] == "started"
    assert events[-1][0] == "done"
    answers = [data for event, data in events if event == "answer"]
    assert len(answers) == 1 and answers[0]["citations"]
    stages = []
    for event, data in events:
        assert event in {"progress", "answer", "done", "error"}
        if event == "progress":
            assert set(data) == {"stage", "message", "request_id"}
            assert question not in json.dumps(data)
            assert "PAY-123" not in json.dumps(data)
            stages.append(data["stage"])
    assert "checking" in stages
    checking_index = next(i for i, (event, data) in enumerate(events) if event == "progress" and data["stage"] == "checking")
    answer_index = next(i for i, (event, _) in enumerate(events) if event == "answer")
    assert checking_index < answer_index


def test_sse_error_never_echoes_provider_exception_or_leaks_admission(client, application):
    async def fail(*args):
        raise RuntimeError("RAW_PROVIDER_SECRET=do-not-release")
    application.state.engine.run = fail
    response = client.post("/api/chat", json={"question": "Find rollout guide"})
    events = parse_sse(response.text)
    assert [event for event, _ in events] == ["progress", "error", "done"]
    assert "RAW_PROVIDER_SECRET" not in response.text
    assert not application.state.admission.active


def test_document_revoke_hides_content_search_and_saved_history(client, application):
    body = client.post("/api/ask", json={"question": "How do I create a new capability?"}).json()
    assert any(c["source_id"] == "capability-onboarding" for c in body["citations"])
    deletion = client.delete("/api/documents/capability-onboarding")
    assert deletion.status_code == 200
    assert client.get("/api/documents/capability-onboarding/content").status_code == 404
    assert client.get(f"/api/sessions/{body['session_id']}").status_code == 404
    assert not any(e.source_id == "capability-onboarding" for e in application.state.engine.retriever.retrieve("capability onboarding"))
    assert not any(d["id"] == "capability-onboarding" for d in client.get("/api/documents").json()["documents"])


def test_owner_binding_applies_to_history_followups_and_deletion(application):
    actor = {"id": "owner-a"}
    authenticator = next(dependency.call for route in application.routes if hasattr(route, "dependant")
                         for dependency in route.dependant.dependencies if isinstance(dependency.call, Authenticator))
    async def identity():
        return Actor(id=actor["id"], admin=False)
    application.dependency_overrides[authenticator] = identity
    with TestClient(application) as client:
        original = client.post("/api/ask", json={"question": "How do I create a new capability?"})
        sid = original.json()["session_id"]
        assert client.get(f"/api/sessions/{sid}").status_code == 200
        actor["id"] = "owner-b"
        assert client.get("/api/sessions").json() == {"sessions": []}
        assert client.get(f"/api/sessions/{sid}").status_code == 404
        assert client.delete(f"/api/sessions/{sid}").status_code == 404
        assert client.post("/api/ask", json={"question": "Who owns it?", "session_id": sid}).status_code == 404
        actor["id"] = "owner-a"
        assert client.get(f"/api/sessions/{sid}").status_code == 200


def test_readiness_detects_http_source_failure_and_recovery(tmp_path):
    sockets = [socket.socket(), socket.socket()]
    try:
        for sock in sockets:
            sock.bind(("127.0.0.1", 0))
        ports = [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()
    endpoints = [f"http://127.0.0.1:{port}/mcp" for port in ports]
    app = create_app(Settings(_env_file=None, app_env="test", data_dir=tmp_path, mcp_transport="http",
                              jira_mcp_url=endpoints[0], slack_mcp_url=endpoints[1]))
    processes = []
    try:
        with TestClient(app) as client:
            client.headers["Authorization"] = "Bearer local-demo-token"
            unavailable = client.get("/health/ready")
            assert unavailable.status_code == 503
            assert unavailable.json()["services"]["jira"] == "unavailable"
            assert unavailable.json()["services"]["slack"] == "unavailable"
            assert unavailable.json()["services"]["documents"] == "ready"
            for source, port in zip(["jira", "slack"], ports):
                processes.append(subprocess.Popen(
                    [sys.executable, "-m", "src.integrations.mcp.server", source, "--transport", "streamable-http", "--port", str(port)],
                    cwd=Path(__file__).resolve().parents[1], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    env=dict(os.environ, APP_ENV="test", MCP_TOKEN="local-mcp-token"),
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                ))
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                try:
                    if all(httpx.get(endpoint, timeout=0.5).status_code == 401 for endpoint in endpoints):
                        break
                except httpx.TransportError:
                    pass
                time.sleep(0.05)
            else:
                pytest.fail("Local HTTP MCP servers failed to start")
            app.state.readiness.invalidate()
            recovered = client.get("/health/ready")
            assert recovered.status_code == 200
            assert recovered.json()["status"] == "ok"
            assert all(value == "ready" for value in recovered.json()["services"].values())
    finally:
        for process in processes:
            process.terminate()
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
