# Understanding this project, one layer at a time

You built this with a lot of AI help. That is fine — but right now you cannot
open a file and say *why* the code is shaped that way. This guide fixes that.

It walks the whole project in the order the **data** moves, not in the order the
folders are listed. Every chapter reads real lines from real files and explains
them in plain English, including the background ideas (generators, protocols,
embeddings, cosine similarity) the first time they show up.

## How to use it

1. Open the chapter on the left, open the file it talks about on the right.
2. Read a numbered block in the chapter, then read those exact lines in the file.
3. At the end of each chapter there is **"Say this out loud"** — a short summary
   in your own voice, and **"Prove it to yourself"** — a small thing to run or
   change so the idea sticks. Do them. Reading alone will not hold.

One chapter per sitting is a reasonable pace. Chapters 1–3 are the foundation;
if you only ever finish those, you still understand the most important decision
in the project.

## The chapters

| # | File | What you learn |
|---|------|----------------|
| 1 | [01-the-shape-of-the-system.md](01-the-shape-of-the-system.md) | What the system does, and why it is really **two** programs |
| 2 | [02-the-document-schema.md](02-the-document-schema.md) | The keystone decision: one shape for all knowledge |
| 3 | [03-connectors.md](03-connectors.md) | How each source becomes Documents (start here — you had this file open) |
| 4 | [04-messy-files.md](04-messy-files.md) | PDFs, HTML, email, and the 900k-row spreadsheet problem |
| 5 | [05-chunking.md](05-chunking.md) | Why documents get cut up, and where to cut |
| 6 | [06-vector-stores.md](06-vector-stores.md) | Embeddings, cosine similarity, FAISS vs OpenSearch |
| 7 | [07-ingestion-pipeline.md](07-ingestion-pipeline.md) | The job that survives a real corpus: incremental, resumable |
| 8 | [08-retrieval.md](08-retrieval.md) | Finding evidence — and knowing when there is none |
| 9 | [09-the-agent.md](09-the-agent.md) | Route → retrieve → synthesize, and the HTTP layer |
| 10 | [10-eval-and-tracing.md](10-eval-and-tracing.md) | Proving it works, and seeing inside it when it doesn't |
| 11 | [11-what-you-got-right.md](11-what-you-got-right.md) | The defensible decisions — and the honest gaps |
| 12 | [12-telling-the-story.md](12-telling-the-story.md) | How to narrate the whole thing in an interview |

## The whole thing in one paragraph

Four sources of company knowledge (Jira tickets, Slack threads, DevPortal docs,
a "city map" of capability ownership) are read by **connectors**, which convert
everything into one common `Document` shape. An **ingestion** job cuts those
documents into **chunks**, turns each chunk into a list of numbers (an
**embedding**) that captures its meaning, and stores them in a **vector index**.
When a question arrives, an **agent** asks an LLM which sources are relevant,
searches only those, throws away results that are not actually close in meaning,
and asks the LLM to write an answer using *only* the retrieved text, with
citations. An **eval harness** scores the whole thing against a golden set of
questions, and **tracing** records what each request actually read.

Line numbers in this guide were correct on 2026-09-01. If code moves, the
surrounding names still find it.
