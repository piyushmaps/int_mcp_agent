# 10 — Proving it works, and seeing inside it

**Files:** [`src/eval/harness.py`](../../src/eval/harness.py),
[`data/eval/golden.json`](../../data/eval/golden.json),
[`src/observability/tracing.py`](../../src/observability/tracing.py).

Everything up to here builds the system. This chapter is about knowing whether it
is any good, and finding out why when it isn't. Most hobby RAG projects stop
before this point — having it is a large part of what makes this one look
professional.

## 10.1 Why "it seems to work" is not enough

You change the chunk size from 800 to 600. Did answers get better or worse?
Without a measurement you are guessing, and you will keep guessing forever,
because LLM output *always* looks plausible.

So: a fixed set of questions with known-correct properties, and a score.

```json
{
  "id": "q2",
  "question": "Who owns the Payments capability?",
  "expected_sources": ["city_map", "slack"],
  "must_mention": ["money-movement", "alex.chen"]
}
```

Five questions today (`q1`–`q5`). Small — say so honestly — but the *mechanism*
is what matters, and adding a sixth is three lines of JSON.

Each entry encodes two different things, which is the key idea of the harness.

## 10.2 Two axes, deliberately kept apart

> * **Routing** — did the router select the sources the question needs?
> * **Grounding** — did the answer state the facts it must mention?

Keeping them separate is what makes a failure *diagnosable*. If grounding drops,
these two numbers tell you which half broke:

- Routing good, grounding bad → retrieval or synthesis is at fault.
- Routing bad → the right source was never searched; fix the prompt.

One blended "accuracy" number would tell you something is wrong and nothing else.

### Routing is scored as recall and precision, not exact match

```python
@property
def routing_recall(self) -> float:
    return len(self.expected_sources & self.routed_sources) / len(self.expected_sources)

@property
def routing_precision(self) -> float:
    return len(self.expected_sources & self.routed_sources) / len(self.routed_sources)
```

(`&` is set intersection — what both sets contain.)

- **Recall**: of the sources you *needed*, how many did you get? Missing one means
  a fact is unreachable. This is the number that matters.
- **Precision**: of the sources you *picked*, how many were needed? Low precision
  is waste, not failure.

The docstring explains why exact match was rejected:

> A router that picks `devportal + city_map` when only `devportal` was needed
> still surfaces every fact; exact-match would score that a failure and report
> ~30% accuracy for a system answering everything correctly.

That is an important instinct: **a metric that punishes harmless behaviour will
push you to "fix" a system that is not broken.** `routing_exact` is still
reported, as information, but nothing is judged on it.

### Grounding is scored by normalised substring match

```python
_NON_ALNUM = re.compile(r"[^a-z0-9]+")

def normalize(text: str) -> str:
    return _NON_ALNUM.sub(" ", text.casefold()).strip()

def mentions(answer: str, fact: str) -> bool:
    return normalize(fact) in normalize(answer)
```

Casefold, then collapse every run of non-alphanumeric characters to a single
space — applied to **both** sides. Golden facts are written as raw identifiers
(`alex.chen`, `CAP-120`, `money-movement`) while a model naturally writes
"Alex Chen", "CAP 120", "money movement". Without normalising, correct answers
score as misses and you would waste a day chasing a retrieval bug that does not
exist.

`casefold()` rather than `lower()` is the more aggressive Unicode-aware
lowercasing — the right default for text comparison.

This is a deliberately cheap metric. It checks that a required string is
*present*; it cannot tell "Alex Chen owns Payments" from "Alex Chen does not own
Payments". The alternative — an LLM judging answers — is slower, costs money, and
is itself unreliable. For a regression check that runs in CI, substring matching
on carefully chosen facts is the right trade, and you should be able to state
both its cost and its justification.

## 10.3 The baseline — the part that turns a claim into evidence

```python
def evaluate_baseline(golden, retriever):
    """Plain RAG: one unfiltered search over every source, no routing, no
    decomposition. This is the control that tells you whether the agent layer
    is actually earning its latency and token cost."""
    for item in golden:
        chunks = retriever.retrieve(item["question"])          # no source filter
        state = synthesize_answer({"question": ..., "retrieved": chunks, ...})
```

This is the strongest idea in the harness. The agent layer costs an extra LLM
call on every request. Does it *earn* that?

The baseline is a **control**: same corpus, same retriever, same synthesis
prompt, with the routing and decomposition steps removed. Run both, compare fact
recall, and `main()` prints the difference:

```python
delta = fact_recall(results) - fact_recall(base)
print(f"\n  fact-recall lift from the agent layer: {delta:+.2f}")
```

That single number is the difference between "we built an agent because agents
are good" and "the agent layer adds N points of fact recall over plain RAG on our
golden set". Only one of those is an engineering statement.

Note the detail:

```python
# Plain RAG effectively 'selects' every source: perfect routing
# recall, worst-case precision. That contrast is the point.
routed_sources=set(VALID_SOURCES),
```

The baseline scores perfect routing recall by definition — it searches
everything. So the comparison surfaces exactly what routing buys: precision, and
whatever fact recall follows from cleaner evidence.

## 10.4 Running it in CI

```python
parser.add_argument("--fail-under", type=float, default=None)
...
if args.fail_under is not None and fact_recall(results) < args.fail_under:
    raise SystemExit(f"fact recall {fact_recall(results):.2f} < {args.fail_under}")
```

`python -m src.eval.harness --fail-under 0.8` exits non-zero when quality drops
below a threshold. That is a **quality gate**: wire it into CI and a prompt
change that quietly breaks answers fails the build instead of shipping. This is
the difference between having an eval and having it protect you.

`report()` prints a per-question table — routing recall/precision, exact, facts
hit, citation count, and *which facts were missed*. That last column is what you
actually debug from.

## 10.5 Tracing — seeing inside one request

[`tracing.py`](../../src/observability/tracing.py). The eval tells you *that*
something is wrong across the set; tracing tells you *what happened* on one
request.

**It is opt-in and inert by default.** Every function starts with a guard:

```python
if not _enabled or _client is None:
    yield          # or return
    return
```

No Langfuse keys → every function here is a no-op and the project behaves exactly
as if the module did not exist. The docstring states the principle:

> Observability should never be a dependency of the thing it observes.

Every call site is also wrapped so tracing failures cannot break a request:

```python
except Exception as exc:  # noqa: BLE001 - never let a trace break the request
    logger.debug("span %r failed: %s", name, exc)
    yield
```

Note it still `yield`s in the failure path — the traced block runs normally even
if the span could not be opened. Monitoring that can take down production is a
real and embarrassing failure mode; this avoids it by construction.

**What LangChain gives you free, and what it cannot.**

```python
result = agent.invoke({"question": question},
                      config={"callbacks": langfuse_callbacks(), "run_name": "ask"})
```

Passing callbacks into the graph makes the routing and synthesis **LLM calls**
appear as spans with prompts, token counts and latency. But retrieval is *our*
code, not a LangChain component, so it is invisible to those callbacks — and
retrieval is exactly the part you need when an answer is wrong:

```python
def record_retrieval(query, sources, chunks, *, min_similarity=None):
    """Attach what retrieval returned to the current span.

    Scores are the point. An answer that went wrong is either bad retrieval or
    bad generation, and the scores are what separate the two: high scores with a
    wrong answer means the model ignored good evidence; low scores mean nothing
    relevant was found and it should have refused.
    """
```

That docstring is the whole reason the function exists. It logs each chunk's
source, title, score, url and a 300-character preview (`PREVIEW_CHARS` — enough
to recognise the chunk without copying the corpus into every trace), plus
metadata including `top_score` and:

```python
# No results is the single most useful thing to alert on: it is
# either an off-corpus question or retrieval quietly breaking.
"empty": not chunks,
```

A rising rate of `empty: true` is your early warning that ingestion broke or
users are asking about something you do not cover. That is an *operational*
insight, not a debugging one.

One implementation detail from chapter 9 closes the loop:

```python
# Recorded here rather than inside the worker threads: tracing context
# does not cross a thread boundary, and the data is what matters, not
# which thread produced it.
```

Langfuse tracks the "current span" in a context variable, which does not
propagate into `ThreadPoolExecutor` workers. Calling `record_retrieval` inside
the worker would attach to nothing. So retrieval is recorded on the main thread
after the pool joins. This is the kind of bug that is very hard to find later and
trivial to avoid once known.

## Say this out loud

> "Evaluation scores two things separately — routing as recall and precision over
> the expected sources, and grounding as whether required facts appear in the
> answer — because a blended number tells you something broke but not which half.
> Routing deliberately isn't exact-match, since over-selecting still surfaces
> every fact. There's a plain-RAG baseline as a control, so the agent layer's
> lift is measured rather than assumed, and `--fail-under` turns it into a CI
> gate. Tracing is opt-in, inert without keys, and can't break a request;
> retrieval is logged explicitly with scores because that's what separates bad
> retrieval from bad generation."

## Prove it to yourself

```bash
python -m src.eval.harness --baseline
```

Read the table. Find a question where the agent and the baseline differ and ask
why. Then add a sixth question to `golden.json` about something you know is in
the corpus, and one about something you know is *not* — the second should return
zero citations, and you can score that by expecting no facts.

Then break something on purpose: set `MIN_SIMILARITY=0.9` in `.env` and re-run.
Fact recall should collapse, because the cutoff now rejects good evidence. That
is what a regression looks like on the dashboard you just built.
