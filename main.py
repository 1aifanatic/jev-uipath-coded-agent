"""AML alert triage — a UiPath coded agent whose decisions are made by TypeSafe's Jev.

Split of responsibilities:
  UiPath LLM Gateway  -> language work: read the alert, extract entities, do the arithmetic,
                         and afterwards write the rationale prose.
  Jev (System One)    -> every decision: red flags (Noul), risk level (Score),
                         disposition (Choice). One API call, all questions in parallel.

Jev returns typed, calibrated values and cannot emit free text, which is precisely why the
generation and the judgement are separated rather than asked of one model.
"""

import json
import logging
import os
import time
from typing import Literal, Optional

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field
from typesafe_sdk import TypeSafeClient
from uipath_langchain.chat.models import UiPathChat

from rubric import DISPOSITIONS, RED_FLAGS, RISK_LEVELS, RUBRIC_VERSION, build_questions

log = logging.getLogger("aml_triage")
log.setLevel(logging.INFO)

GATEWAY_MODEL = "gpt-5-mini-2025-08-07"
JEV_MODEL = "jev-latest"
JEV_USD_PER_M_INPUT = 0.042  # documented; output tokens are free
PLATFORM_UNITS_PER_LLM_CALL = 0.2  # documented Standard tier, per LLM call


# --------------------------------------------------------------------------- I/O

class Input(BaseModel):
    alert_id: str
    customer: str
    narrative: str
    account_age_days: Optional[int] = None
    prior_alerts: Optional[int] = None
    benchmark: bool = Field(False, description="Also run an LLM-only arm for comparison.")


class Benchmark(BaseModel):
    jev_latency_ms: int
    jev_input_tokens: int
    jev_cost_usd: float
    llm_latency_ms: Optional[int] = None
    llm_disposition: Optional[str] = None
    llm_platform_units: Optional[float] = None
    agreement: Optional[bool] = None


class Output(BaseModel):
    alert_id: str
    disposition: Literal["escalate", "close", "need_info"]
    disposition_confidence: float
    risk_level: str
    risk_score: float
    red_flags: dict[str, float]
    parties_extracted: list[str]
    rationale: str
    evidence: list[str]
    rubric_version: str
    metrics: Optional[Benchmark] = None


class State(BaseModel):
    """Input fields + node-derived work + the Output fields the final node fills in.

    Output fields live at the top level because LangGraph's output_schema selects by
    key from the state, not from a nested object.
    """
    # input
    alert_id: str = ""
    customer: str = ""
    narrative: str = ""
    account_age_days: Optional[int] = None
    prior_alerts: Optional[int] = None
    benchmark: bool = False
    # intermediate
    digest: dict = Field(default_factory=dict)
    parties: list[str] = Field(default_factory=list)
    answers: dict = Field(default_factory=dict)
    timings: dict = Field(default_factory=dict)
    # output
    disposition: str = ""
    disposition_confidence: float = 0.0
    risk_level: str = ""
    risk_score: float = 0.0
    red_flags: dict[str, float] = Field(default_factory=dict)
    parties_extracted: list[str] = Field(default_factory=list)
    rationale: str = ""
    evidence: list[str] = Field(default_factory=list)
    rubric_version: str = RUBRIC_VERSION
    metrics: Optional[Benchmark] = None


# ----------------------------------------------------------------------- helpers

def _jev_key() -> str:
    """Local dev reads .env; the serverless run reads the Orchestrator asset."""
    key = os.getenv("JEV_API_KEY") or os.getenv("TYPESAFE_API_KEY")
    if key:
        return key
    from uipath.platform import UiPath  # imported lazily: only needed in the cloud
    asset = UiPath().assets.retrieve(name="JevApiKey")
    return getattr(asset, "value", None) or getattr(asset, "string_value", "")


def _llm(model: str = GATEWAY_MODEL) -> UiPathChat:
    return UiPathChat(model=model)


def _json_from(text: str) -> dict:
    """Gateway models occasionally fence their JSON; tolerate it."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```")[1]
        t = t[4:] if t.lower().startswith("json") else t
    return json.loads(t.strip())


# ------------------------------------------------------------------------- nodes

EXTRACT_PROMPT = """You are preparing an AML alert for a decision model that is poor at \
arithmetic and cannot compare dates. Do ALL counting and arithmetic yourself.

Return ONLY JSON with these keys:
  parties            list of named legal/natural persons in the narrative
  transaction_count  integer, or null if not determinable
  total_amount       number, or null
  largest_amount     number, or null
  smallest_amount    number, or null
  span_days          integer number of days the activity covers, or null
  amounts_cluster_just_below  one of "yes" | "no" | "unknown" - whether the individual \
amounts sit consistently just under a round reporting threshold such as 10,000
  outflow_within_days integer days between credit and onward transfer, or null
  stated_purpose     the customer's stated business purpose, or null
  documentation      what supporting documentation exists, or "none on file"

ALERT:
{narrative}

CUSTOMER: {customer}
ACCOUNT AGE (days): {account_age_days}
PRIOR ALERTS: {prior_alerts}"""


def extract(state: State) -> dict:
    t0 = time.perf_counter()
    raw = _llm().invoke(EXTRACT_PROMPT.format(
        narrative=state.narrative, customer=state.customer,
        account_age_days=state.account_age_days, prior_alerts=state.prior_alerts,
    )).content
    facts = _json_from(raw)
    dt = int((time.perf_counter() - t0) * 1000)

    # The tight digest Jev sees - derived facts only, no raw prose padding.
    digest = {
        "customer": state.customer,
        "account_age_days": state.account_age_days,
        "prior_alerts": state.prior_alerts,
        "narrative": state.narrative,
        **{k: v for k, v in facts.items() if k != "parties"},
    }
    log.info("EXTRACT ok in %sms parties=%s", dt, facts.get("parties"))
    return {"digest": digest, "parties": facts.get("parties") or [],
            "timings": {**state.timings, "extract_ms": dt}}


def decide(state: State) -> dict:
    """The Jev call: every decision in this agent happens here, in one request."""
    client = TypeSafeClient(api_key=_jev_key())
    t0 = time.perf_counter()
    r = client.system_one(state=state.digest, questions=build_questions(), model=JEV_MODEL)
    dt = int((time.perf_counter() - t0) * 1000)

    answers = {}
    for name, a in r.answers.items():
        if a.type == "noul":
            answers[name] = {"type": "noul", "value": a.noul}
        elif a.type == "score":
            answers[name] = {"type": "score", "value": a.score,
                             "confidence": a.confidence, "legend": a.legend}
        else:
            answers[name] = {"type": "choice", "value": a.choice,
                             "confidence": a.confidence, "probabilities": a.probabilities}

    log.info("JEV ok in %sms disposition=%s tokens_in=%s", dt,
             answers["disposition"]["value"], r.usage.input_tokens)
    return {"answers": answers,
            "timings": {**state.timings, "jev_ms": dt,
                        "jev_input_tokens": r.usage.input_tokens}}


EXPLAIN_PROMPT = """Write the analyst-facing justification for a triage decision that has \
already been made by a decision model. Do not second-guess it; explain it.

DECISION: {disposition}
RISK LEVEL: {risk_level}
RED FLAG SCORES (0-1): {flags}

ALERT NARRATIVE:
{narrative}

Return ONLY JSON:
  rationale  2-4 sentences explaining why this disposition follows from the flags above
  evidence   list of 2-5 SHORT VERBATIM quotes from the narrative that support it"""


def explain(state: State) -> dict:
    disp = state.answers["disposition"]
    score = state.answers["risk_level"]
    level = score["legend"].get(int(score["value"]), RISK_LEVELS[int(score["value"])])
    flags = {k: round(v["value"], 3) for k, v in state.answers.items() if v["type"] == "noul"}

    t0 = time.perf_counter()
    raw = _llm().invoke(EXPLAIN_PROMPT.format(
        disposition=disp["value"], risk_level=level, flags=flags, narrative=state.narrative,
    )).content
    parsed = _json_from(raw)
    dt = int((time.perf_counter() - t0) * 1000)

    bench = Benchmark(
        jev_latency_ms=state.timings.get("jev_ms", -1),
        jev_input_tokens=state.timings.get("jev_input_tokens", 0),
        jev_cost_usd=round(state.timings.get("jev_input_tokens", 0) / 1e6 * JEV_USD_PER_M_INPUT, 8),
    )
    if state.benchmark:
        t1 = time.perf_counter()
        llm_disp = _llm().invoke(
            "Triage this AML alert. Reply with exactly one word - escalate, close, or need_info.\n"
            f"Options mean: {json.dumps(DISPOSITIONS)}\n\nALERT:\n{state.narrative}"
        ).content.strip().lower()
        bench.llm_latency_ms = int((time.perf_counter() - t1) * 1000)
        bench.llm_disposition = next((d for d in DISPOSITIONS if d in llm_disp), llm_disp[:20])
        bench.llm_platform_units = PLATFORM_UNITS_PER_LLM_CALL
        bench.agreement = bench.llm_disposition == disp["value"]

    result = Output(
        alert_id=state.alert_id,
        disposition=disp["value"],
        disposition_confidence=round(disp["confidence"], 4),
        risk_level=level.split(":")[0],
        risk_score=round(score["value"], 3),
        red_flags=flags,
        parties_extracted=state.parties,
        rationale=parsed.get("rationale", ""),
        evidence=parsed.get("evidence", []) or [],
        rubric_version=RUBRIC_VERSION,
        metrics=bench,
    )
    log.info("RESULT %s", result.model_dump_json()[:600])
    return {**result.model_dump(), "timings": {**state.timings, "explain_ms": dt}}


# ------------------------------------------------------------------------- graph

builder = StateGraph(State, input_schema=Input, output_schema=Output)
builder.add_node("extract", extract)
builder.add_node("decide", decide)
builder.add_node("explain", explain)
builder.add_edge(START, "extract")
builder.add_edge("extract", "decide")
builder.add_edge("decide", "explain")
builder.add_edge("explain", END)
graph = builder.compile()
