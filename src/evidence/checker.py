import re
from datetime import datetime, timezone

from src.contracts import AnswerDraft, Citation, Evidence
from src.policy.safety import UnsafeContentError, safe_url, sanitize_text


def merge_evidence(results) -> list[Evidence]:
    evidence = {}
    for branch in sorted(results, key=lambda b: b.task_id):
        for item in branch.evidence:
            # Final boundary is defense in depth; adapters must already screen before graph state.
            if sanitize_text(item.text) != item.text or sanitize_text(item.title) != item.title:
                continue
            try:
                safe_url(item.url)
            except UnsafeContentError:
                continue
            evidence[item.id] = item
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    def timestamp(e):
        value = e.updated_at or epoch
        return value.replace(tzinfo=timezone.utc).timestamp() if value.tzinfo is None else value.timestamp()
    return sorted(evidence.values(), key=lambda e: (-timestamp(e), e.source, e.id))[:12]

def validate_output(draft: AnswerDraft, evidence: list[Evidence]) -> bool:
    """Conservative extractive claim gate: factual text must occur in its cited evidence.

    This deliberately avoids pretending that citation membership proves arbitrary paraphrases.
    """
    registry = {e.id: e for e in evidence}
    def normalize(s):
        return re.sub(r"\s+", " ", s).strip()
    try:
        for claim in draft.claims:
            if not claim.text.strip() or sanitize_text(claim.text) != claim.text:
                return False
            if any(eid not in registry for eid in claim.evidence_ids):
                return False
            if not any(normalize(claim.text) in normalize(registry[eid].text) for eid in claim.evidence_ids):
                return False
            if re.search(r"<[^>]+>|https?://|\[[^\]]*\]\(", claim.text):
                return False
        return all(sanitize_text(x) == x for x in draft.limitations)
    except UnsafeContentError:
        return False

def render_draft(draft: AnswerDraft, evidence: list[Evidence]) -> tuple[str, list[Citation]]:
    registry = {e.id: e for e in evidence}
    cited = {}
    paragraphs = []
    for claim in draft.claims:
        numbers = []
        for eid in claim.evidence_ids:
            if eid not in cited:
                e = registry[eid]
                cited[eid] = Citation(id=str(len(cited) + 1), evidence_id=e.id, source=e.source,
                                     source_id=e.source_id, title=e.title, url=safe_url(e.url),
                                     location=e.location, updated_at=e.updated_at, version=e.version)
            numbers.append(f"[{cited[eid].id}]")
        paragraphs.append(f"{claim.text} {' '.join(numbers)}")
    return "\n\n".join(paragraphs), list(cited.values())

