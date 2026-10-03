# Understand the project: workflow, HLD and LLD

Design date: September 29, 2026. Status: **proposed design for review**.
These documents do not indicate that the new services or controls are implemented.

The assistant answers internal engineering questions using live Jira and Slack
retrieval through MCP and required document retrieval over web pages, Google
Drive, Markdown, PDF, and Word documents. Initial MCP servers will implement the
real protocol over synthetic source data. All source operations are read-only.

## Start with these three pages

1. [Workflow](../workflow.md): what the product does, explained through one employee question.
2. [High-level design](high-level-design.md): the main parts and how they connect.
3. [Low-level design](low-level-design.md): open each part and follow the ordered steps inside its methods, with example inputs, returns and failures.

The LLD now includes a newcomer introduction, a worked PAY-123 example, shared
data contracts, explicit session-memory methods and a diagram inside each A–G
component. Two connected deep dives explain the largest flows:

- [Document ingestion LLD](ingestion-low-level-design.md): current incremental
  batch ingestion versus proposed continuous jobs; creation of Documents,
  extraction, chunking, embeddings, FAISS/OpenSearch and query-time synthesis.
- [MCP integration LLD](mcp-low-level-design.md): host/client/server roles,
  connection and tool-call sequences, initial tool contracts, client wrappers,
  SDK compatibility, fake-server internals and failure behavior.

These additions refine the LLD; both existing HLD presentations remain available.

For comparison, [High-level design 2](high-level-design-2.md) keeps the A–G flow
and adds explicit session-memory steps and agent/code labels. The original HLD
is preserved; we have not selected one version as its replacement.

To see what "how a method works" means, start with the
[request-validation walkthrough](low-level-design.md#validate_request). Every
function row links to its own walkthrough on that same page.

The same letters and names appear on all three pages:

```text
Load balancer -> A. Request entrance -> B. Coordinator -> C. Source readers
    -> D. Evidence checker -> E. Answer writer -> F. Response checker

G. Document preparation -> the document index searched by C
```

**HLD and LLD are two views of the same system.** For example, HLD says that
C. Source readers gets a Jira ticket. LLD opens C and explains how its specialist
chooses a tool, checks permission, calls the MCP server and screens the reply.
There is no separate HLD program and LLD program to connect.

## Detailed references for later

| Reference | Use it when you need to understand... |
|---|---|
| [LangGraph nodes](langgraph-nodes.md) | Agent loops, ordinary nodes, parallel results and working state |
| [Expanded workflow](../workflow-reference.md) | Every control, failure route and concrete gap ID |
| [Production architecture](production-reference.md) | Deployment, storage, scale, recovery and operations |
| [Service contracts](service-reference.md) | Full data fields, budgets, method contracts and failure rules |
| [Ingestion LLD](ingestion-low-level-design.md) | Follow a document from source bytes through its published chunks to a cited answer |
| [MCP LLD](mcp-low-level-design.md) | Follow a specialist decision through client authorization, server execution and safe evidence |
| [Gap register](gap-register.md) | What is missing from the code and how each fix will be verified |

The detailed controls have been preserved. The first reading path focuses on
understanding the product and following a question from beginning to end.

## Decisions and assumptions

Confirmed: LangGraph; Jira and Slack MCP; realistic fake sources first; document
retrieval is required; company-wide eligible information only; sanitization;
source links; newest relevant evidence first with older conflicts explained;
learning in small steps with short method explanations.

Proposed engineering choices: one modular query application initially, separate
ingestion workers, two specialist tool-using agents, a single-call planner and
answer writer, explicit policy gates, bounded refinement, and checked answer release.
The checked-release proposal changes raw token streaming: progress streams immediately,
but answer text is withheld until its complete draft passes checks. This behavior
needs agreement before implementing the new response path.

Still unresolved: million requests per day/month versus peak concurrency; budget;
production identity/provider approvals; exact freshness and request deadlines;
session expiration; latency percentile; OCR scope; sanitized-viewer linking.
These do not block local fake-server exercises. They do block production sizing
and final data-handling configuration. Handoff retention/recovery defaults remain
starting points, not verified corporate policy.

## Implementation sequence

1. Shared contracts and one real local MCP `get_issue` exchange over fake data.
2. Identity/policy boundary, restricted-ticket fixture, sanitized evidence contract.
3. Bounded LangGraph Jira subgraph, checked response path, and failure tests.
4. Slack subgraph and incremental document pipeline, with citation locations.
5. Cross-source evaluation, session isolation, deletion and cache behavior.
6. Shared limits, hardened deployment, measured load and recovery exercises.

Before each implementation step, explain the methods and offer a small skeleton
or TODO exercise so the user can implement first. Do not generate the entire
production system as a single teaching step.
