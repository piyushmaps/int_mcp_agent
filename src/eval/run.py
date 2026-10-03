"""Run deterministic golden cases through the real API and local MCP protocols.

python -m src.eval.run --fail-under .8 --baseline --output data/eval/report.json

These are explicit fixture assertions, not an LLM judge or a production-quality
claim. The optional baseline searches the same document corpus without Jira or
Slack; a baseline failure identifies fixture coverage differences, not a general
proof that agent orchestration outperforms retrieval.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from src.answering.writer import extractive_draft
from src.api.main import create_app
from src.config import Settings
from src.evidence.checker import render_draft, validate_output
from src.policy.safety import UnsafeContentError, sanitize_text

GOLDEN_PATH = Path(__file__).resolve().parents[2] / "data" / "eval" / "golden.json"


def check_case(case: dict, http_status: int, body: dict) -> dict[str, bool]:
    answer = body.get("answer", "")
    serialized = json.dumps(body).lower()
    checks = {"http_status": http_status == case.get("http_status", 200)}
    if "statuses" in case:
        checks["answer_status"] = body.get("status") in case["statuses"]
    for number, group in enumerate(case.get("answer_groups", []), 1):
        checks[f"answer_fact_{number}"] = any(term.lower() in answer.lower() for term in group)
    cited_ids = {c.get("source_id") for c in body.get("citations", [])}
    for source_id in case.get("citation_ids", []):
        checks[f"citation:{source_id}"] = source_id in cited_ids
    for source in case.get("required_sources", []):
        checks[f"route:{source}"] = source in body.get("selected_sources", [])
    if case.get("no_citations"):
        checks["abstention_has_no_citations"] = body.get("citations", []) == []
    for term in ["RESTRICTED_FIXTURE_DO_NOT_RELEASE", "UNKNOWN_ELIGIBILITY_DO_NOT_RELEASE", *case.get("forbidden", [])]:
        checks[f"absent:{term}"] = term.lower() not in serialized
    return checks


def retrieval_baseline(app, question: str) -> tuple[int, dict]:
    try:
        question = sanitize_text(question)
    except UnsafeContentError:
        return 422, {"detail": "question_failed_content_check"}
    evidence = app.state.engine.retriever.retrieve(question, k=5)
    draft = extractive_draft(question, evidence, [])
    if not validate_output(draft, evidence):
        return 200, {"status": "blocked", "answer": "", "citations": [], "selected_sources": ["documents"]}
    answer, citations = render_draft(draft, evidence)
    return 200, {"status": "answered" if draft.claims else "insufficient_evidence", "answer": answer,
                 "citations": [c.model_dump(mode="json") for c in citations], "selected_sources": ["documents"]}


def evaluate(golden_path: Path = GOLDEN_PATH, *, baseline: bool = False) -> dict:
    cases = json.loads(golden_path.read_text(encoding="utf-8"))
    if not isinstance(cases, list) or not cases:
        raise ValueError("Golden evaluation must contain cases")
    rows = []
    with tempfile.TemporaryDirectory(prefix="atlas-eval-") as temporary:
        app = create_app(Settings(_env_file=None, app_env="test", model_mode="demo", auth_mode="demo",
                                  data_dir=Path(temporary), bootstrap_demo=True, mcp_transport="stdio"))
        with TestClient(app) as client:
            client.headers["Authorization"] = "Bearer local-demo-token"
            for case in cases:
                started = time.perf_counter()
                response = client.post("/api/ask", json={"question": case["question"]})
                checks = check_case(case, response.status_code, response.json())
                row = {"id": case["id"], "passed": all(checks.values()), "checks": checks,
                       "duration_ms": round((time.perf_counter() - started) * 1000),
                       "status": response.json().get("status", response.status_code)}
                if baseline:
                    http_status, body = retrieval_baseline(app, case["question"])
                    baseline_checks = check_case(case, http_status, body)
                    row["baseline"] = {"passed": all(baseline_checks.values()), "checks": baseline_checks}
                rows.append(row)
    report = {"suite": "synthetic-engineering-atlas-v1", "evaluated_at": datetime.now(timezone.utc).isoformat(),
              "case_count": len(rows), "passed": sum(row["passed"] for row in rows),
              "pass_rate": sum(row["passed"] for row in rows) / len(rows), "cases": rows,
              "limitations": "Deterministic synthetic assertions; not production quality or security certification."}
    if baseline:
        report["baseline_pass_rate"] = sum(row["baseline"]["passed"] for row in rows) / len(rows)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden", type=Path, default=GOLDEN_PATH)
    parser.add_argument("--fail-under", type=float, default=0.8)
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 0 <= args.fail_under <= 1:
        parser.error("--fail-under must be between 0 and 1")
    report = evaluate(args.golden, baseline=args.baseline)
    output = json.dumps(report, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output + "\n", encoding="utf-8")
    print(output)
    raise SystemExit(0 if report["pass_rate"] >= args.fail_under else 1)


if __name__ == "__main__":
    main()
