# AML Alert Triage — UiPath coded agent with TypeSafe Jev

A FINS demo: a UiPath **coded agent** that triages anti-money-laundering alerts, where
every *decision* is made by TypeSafe's **Jev** model rather than by an LLM.

## The idea

Jev is not a generative model. It cannot write a sentence. What it does is return **typed,
calibrated decisions** — and it does so in ~400ms for a fraction of a cent. So the two jobs
are split:

| Layer | Responsibility |
|---|---|
| UiPath LLM Gateway (`gpt-5-mini`) | Read the alert, extract parties, do all arithmetic, and afterwards write the rationale |
| **Jev** (`jev-latest`) | Every decision — red flags, risk level, disposition |

One Jev call answers seven questions in parallel against the same state:

- **Noul** ×5 → `structuring`, `rapid_pass_through`, `shell_company_indicators`,
  `high_risk_jurisdiction`, `purpose_mismatch`
- **Score** ×1 → risk level on the ordered rubric (Low / Medium / High / Critical)
- **Choice** ×1 → `escalate` / `close` / `need_info`

## Measured results (12-alert eval set)

```
schema validity    12/12  100.0%   bar 100%   PASS
disposition match  10/12   83.3%   bar  80%   PASS
evidence grounded  50/50  100.0%
  clear      8/8
  ambiguous  2/4

head to head
  jev   median  429 ms   total cost $0.000427 for 12 alerts
  llm   median 3052 ms   2.4 platform units
  jev is 7.1x faster
  agreement 9/12
```

**The calibration is the interesting part.** Both misses arrived with low confidence
(0.75 and 0.31); every correct call was 0.89–1.00. Routing anything below ~0.50 to a human
would have caught one of the two misses without touching a single correct decision — which
is the actual operating model for an L1 triage queue.

## Layout

| File | Purpose |
|---|---|
| `main.py` | The LangGraph agent: `extract` → `decide` (Jev) → `explain` |
| `rubric.py` | The policy artifact. Red flags, risk levels, dispositions — this *is* the Jev question set |
| `evals/alerts.json` | 12 synthetic alerts with hand-authored ground truth (4 deliberately ambiguous) |
| `evaluate.py` | Schema gate, disposition accuracy, evidence grounding, head-to-head |

Change policy in `rubric.py`, not in the agent. `RUBRIC_VERSION` is stamped into every result.

## Design constraints

`rubric.py` is written around Jev's documented weaknesses (`model-jaggedness/jev-1.13`):
it is unreliable at arithmetic, cannot order dates, is poor at multi-step indirection, and
loses accuracy when the state carries irrelevant context. So the LLM does every calculation
*before* Jev sees anything, the state Jev receives is a tight derived digest, and every
question is atomic. Asking Jev to count would be asking it to fail.

## Running it

```bash
uv venv --python 3.12 && uv pip install -e .
cp .env.local .env            # plus UIPATH_URL / UIPATH_ACCESS_TOKEN
uipath init
uipath run agent -f evals/one.json
python evaluate.py
```

Deploy:

```bash
uipath pack && uipath publish --my-workspace
uipath invoke agent -f evals/one.json
```

## Secrets

Locally the Jev key comes from `.env` (`JEV_API_KEY` or `TYPESAFE_API_KEY`).
**`.env` does not propagate to the serverless runtime** — in the cloud the agent reads the
Orchestrator asset `JevApiKey`. That fallback is in `main.py:_jev_key()`.

## Verified

- Egress from a UiPath serverless run to `api.typesafe.ai` — **permitted** (200 OK).
- Orchestrator asset read from inside a serverless coded-agent run — **works**.
- Published and run as a `PythonCodedAgent` on serverless — **Successful**.
