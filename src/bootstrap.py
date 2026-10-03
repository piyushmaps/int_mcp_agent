"""Seed synthetic reference documents only. Never ingest the project design docs as company facts."""
from datetime import datetime

from src.contracts import Document


def demo_documents():
    records = [
        ("payments-rollout", "Payments rollout guide", "devportal", "2026-09-29T09:00:00+00:00",
         "# Payments rollout guide\n\n## Release gates\n"
         "The Payments rollout requires passing idempotency regression tests, reconciliation checks, and a rollback rehearsal. "
         "A release must remain blocked until the payment idempotency test passes.\n\n"
         "## Rollout sequence\nStart the Payments rollout with a 5% canary for 30 minutes. "
         "Watch payment success rate, duplicate-charge alerts, and reconciliation lag. "
         "Expand to 25%, then 100% after the release owner approves each gate. "
         "Roll back if payment errors exceed 1% for five minutes.\n\n"
         "## Ownership\nThe money-movement team owns Payments; Alex Chen is the engineering owner. "
         "The current release work is tracked in PAY-123."),
        ("capability-onboarding", "Create a new capability", "devportal", "2026-09-28T10:00:00+00:00",
         "# Create a new capability\n\n## Checklist\n"
         "To create a new capability, register its name and owning team in the capability catalog. "
         "Create a service repository from the approved template, publish an API contract, and add a runbook. "
         "Add service-level objectives, dashboards, on-call ownership, and dependency information before requesting review. "
         "CAP-120 tracks the capability onboarding checklist.\n\n## Approval\n"
         "The platform team reviews the capability contract and operational readiness before enabling production traffic."),
        ("payments-ownership", "Payments ownership and dependencies", "city_map", "2026-09-30T14:00:00+00:00",
         "# Payments ownership and dependencies\n\n"
         "The money-movement team owns the Payments capability. Alex Chen is its engineering owner. "
         "Payments depends on Ledger for accounting entries, Identity for authentication, and Risk for transaction screening. "
         "The team discusses release readiness in the public payments channel. "
         "Payments incident response starts with the money-movement on-call rotation."),
        ("incident-response", "Engineering incident response", "devportal", "2026-09-27T11:00:00+00:00",
         "# Engineering incident response\n\n"
         "For a production incident, declare severity, assign an incident commander, and open a public incident channel. "
         "Capture the impact, timeline, mitigation, and next update time. "
         "For Payments incidents, contact the money-movement on-call rotation. "
         "Write a blameless incident review with concrete follow-up owners after recovery."),
    ]
    return [Document(id=key, source=source, title=title, body=body,
                     url=f"/api/documents/{key}/content", updated_at=datetime.fromisoformat(date),
                     revision="1", company_wide=True, metadata={"fixture": True, "format": ".md"})
            for key, title, source, date, body in records]

def seed_demo(store, embeddings=None):
    from src.ingestion.pipeline import IngestionPipeline
    # Previously revoked documents must never be recreated by restarting the app.
    candidates = [d for d in demo_documents() if store.active_state(d.id) is None]
    if candidates:
        IngestionPipeline(store, embeddings).ingest(candidates, scope="demo")

