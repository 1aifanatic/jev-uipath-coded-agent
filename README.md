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
uipath run agent -f evals/sample-escalate.json
python evaluate.py
```

Deploy:

```bash
uipath pack && uipath publish --my-workspace
uipath invoke agent -f evals/sample-escalate.json
```

## Input arguments

| Argument | Type | Required | Notes |
|---|---|---|---|
| `alert_id` | string | **yes** | Free-form identifier, echoed into the result |
| `customer` | string | **yes** | Name plus whatever profile context you have (jurisdiction, incorporation date) |
| `narrative` | string | **yes** | The alert text. This is what the evidence quotes are checked against |
| `account_age_days` | integer | no | Feeds the shell-company judgement |
| `prior_alerts` | integer | no | Alert history on this customer |
| `benchmark` | boolean | no, default `false` | Also runs an LLM-only arm and fills `metrics` for the head-to-head |

Three ready-to-run inputs are in `evals/`: `sample-escalate.json`, `sample-close.json`,
`sample-ambiguous.json`.

## What you get back

```json
{
  "alert_id": "AML-2026-0144",
  "disposition": "escalate",
  "disposition_confidence": 1.0,
  "risk_level": "Critical",
  "risk_score": 3.0,
  "red_flags": { "structuring": 0.72, "rapid_pass_through": 0.97,
                 "shell_company_indicators": 0.95, "high_risk_jurisdiction": 0.82,
                 "purpose_mismatch": 0.81 },
  "parties_extracted": ["Aurelia Holdings SA (Panama, nominee directors)", "Latvian bank"],
  "rationale": "...",
  "evidence": ["Single inbound transfer of USD 2,400,000 from a Latvian bank", "..."],
  "rubric_version": "1.0.0",
  "metrics": { "jev_latency_ms": 402, "jev_cost_usd": 3.5e-05,
               "llm_latency_ms": 2420, "llm_disposition": "escalate", "agreement": true }
}
```

**Where to look in UiPath:** the result does **not** appear in the job's Output Arguments
panel — that field comes back empty for coded agents. Open the job's **Traces** view instead.
The root `LangGraph` span carries the final output; `extract`, `decide` and `explain` appear
as child spans, so you can see the digest handed to Jev and Jev's raw typed answers
separately. The `assets_retrieve` span shows as redacted, which is the platform refusing to
log the API key. Each LLM call also fires twelve ISO 42001 governance guardrail spans.

## Secrets

Locally the Jev key comes from `.env` (`JEV_API_KEY` or `TYPESAFE_API_KEY`).
**`.env` does not propagate to the serverless runtime** — in the cloud the agent reads the
Orchestrator asset `JevApiKey`. That fallback is in `main.py:_jev_key()`.

## Verified

- Egress from a UiPath serverless run to `api.typesafe.ai` — **permitted** (200 OK).
- Orchestrator asset read from inside a serverless coded-agent run — **works**.
- Published and run as a `PythonCodedAgent` on serverless — **Successful**.
