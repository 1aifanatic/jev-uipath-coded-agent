# STATE — jev-uipath-codedapp

Last updated: 2026-09-20 (v0.2.0 - switchable decider)

## What this is
FINS demo: UiPath coded agent for AML alert triage, decisions made by TypeSafe Jev.
Full design rationale in README.md.

## What works (verified live, not assumed)
- Jev API from local dev — all three primitives (Noul / Score / Choice), one call.
- Jev API **from a UiPath serverless run** — 200 OK. Egress is not blocked.
- Orchestrator asset `JevApiKey` (Id 684201) read from inside a serverless run.
- Agent published and run on Orchestrator as PythonCodedAgent — Successful, 44s.
- Eval: schema 12/12, disposition 10/12 (83.3%), evidence grounding 50/50.
- Head-to-head: Jev median 429ms vs gateway LLM 3052ms (7.1x), $0.000427 for 12 alerts.

## v0.2.0 - switchable decider
- `decider` input argument: "jev" (default) or "llm". Both arms answer the SAME seven
  questions against the same digest, so the comparison is like-for-like.
- `expected_disposition` input turns on the CORRECT/WRONG line in the log.
- `benchmark: true` runs the other arm too and logs speedup / cost / agreement.
- Every run emits a summary block with decider, disposition, accuracy, decision time,
  decision cost and total run time.
- evaluate.py gains --decider and --compare (side-by-side table for the whole set).
- Cost units are honest: Jev in dollars (documented $0.042/M input, output free), gateway
  LLM in platform units (documented 0.2/call) unless USD_PER_PLATFORM_UNIT is set.

## Correction to the earlier 7x claim
The original benchmark asked the LLM for ONE word while Jev answered SEVEN questions -
unfair to the LLM on work, but it still lost on time. With both doing equal work the gap
is LARGER: roughly 15-16x per decision. Also, on the original 12-alert set the LLM was
MORE accurate than Jev (11/12 vs 10/12). Jev's case is speed and cost at comparable
accuracy, plus calibrated confidence - not better answers. Do not overclaim this.

## What is NOT done (deliberately, per scope)
- v2: Data Fabric entity customer lookup as an agent tool. Cut from v1 on purpose —
  it demonstrates UiPath plumbing, not Jev.
- v3: HITL escalation to Action Center.
- Studio Web link (`UIPATH_PROJECT_ID` + `uipath push`). Not needed for the demo;
  the Coded agent type in Studio Web is still Preview.
- `uipath eval` (the platform eval surface). evaluate.py covers the same three metrics
  locally and is what produced the numbers above.

## Known gotchas discovered here
- There are TWO CLIs: `uip` (Node, low-code/Maestro) and `uipath` (Python SDK, coded
  agents). Separate credential stores. Do not conflate them.
- Tenant is on **staging.uipath.com**, not cloud.uipath.com.
- `UIPATH_URL` must include org and tenant: `https://staging.uipath.com/aifanatic/DefaultTenant`.
  Without the path the CLI parses the service name as the org.
- `.env` does NOT reach the serverless runtime. Use an Orchestrator asset.
- Serverless runs Python 3.14 and installs deps from pyproject.toml on each cold start.
- Coded agent output does not appear in the job's OutputArguments; read it from RobotLogs.

## Next
1. Decide whether to push to Studio Web for the visual trace surface.
2. v2 tool call if the demo needs a UiPath-platform beat.
3. The 2 ambiguous misses are both low-confidence — a confidence gate at 0.50 is the
   obvious next experiment and is nearly free to test against the existing eval set.
