"""Run the eval set against the agent and report the three metrics that matter.

  1. Schema validity   - HARD GATE. A well-reasoned answer in the wrong shape is an outage.
  2. Disposition match - against hand-authored ground truth.
  3. Evidence grounding - are the quoted spans actually in the narrative, or invented?
  Plus the head-to-head: Jev vs a gateway LLM asked for the same disposition.

Usage:  python evaluate.py [--no-benchmark] [--limit N]
"""

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from pydantic import ValidationError

load_dotenv(".env")

from main import Output, graph  # noqa: E402  (import after env is loaded)

BAR_SCHEMA = 1.00
BAR_DISPOSITION = 0.80


def normalise(s: str) -> str:
    return " ".join(s.lower().replace("’", "'").split())


def grounded(evidence: list[str], narrative: str) -> tuple[int, int]:
    """How many evidence quotes actually appear in the source text."""
    hay = normalise(narrative)
    hits = sum(1 for e in evidence if normalise(e) and normalise(e) in hay)
    return hits, len(evidence)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-benchmark", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    alerts = json.loads(Path("evals/alerts.json").read_text(encoding="utf-8"))
    if args.limit:
        alerts = alerts[: args.limit]

    rows, schema_ok, disp_ok = [], 0, 0
    ev_hits = ev_total = 0
    jev_ms, llm_ms, jev_cost, agree = [], [], 0.0, []

    print(f"Running {len(alerts)} alerts\n")
    for a in alerts:
        payload = {k: a[k] for k in
                   ("alert_id", "customer", "narrative", "account_age_days", "prior_alerts")}
        payload["benchmark"] = not args.no_benchmark
        t0 = time.perf_counter()
        try:
            raw = graph.invoke(payload)
        except Exception as exc:  # a crash is a failed row, not a failed run
            print(f"  {a['alert_id']}  EXCEPTION  {type(exc).__name__}: {exc}")
            rows.append({"alert_id": a["alert_id"], "error": str(exc)[:200]})
            continue
        wall = int((time.perf_counter() - t0) * 1000)

        # 1. schema gate
        try:
            out = Output.model_validate(raw)
            schema_ok += 1
        except ValidationError as exc:
            print(f"  {a['alert_id']}  SCHEMA FAIL  {exc.error_count()} errors")
            rows.append({"alert_id": a["alert_id"], "schema": False})
            continue

        # 2. disposition
        hit = out.disposition == a["expected_disposition"]
        disp_ok += hit

        # 3. evidence grounding
        h, t = grounded(out.evidence, a["narrative"])
        ev_hits += h
        ev_total += t

        m = out.metrics
        if m:
            jev_ms.append(m.jev_latency_ms)
            jev_cost += m.jev_cost_usd
            if m.llm_latency_ms:
                llm_ms.append(m.llm_latency_ms)
            if m.agreement is not None:
                agree.append(m.agreement)

        mark = "OK " if hit else "MISS"
        print(f"  {a['alert_id']}  {mark}  got={out.disposition:<9} want={a['expected_disposition']:<9}"
              f" risk={out.risk_level:<8} conf={out.disposition_confidence:.2f}"
              f" ev={h}/{t} ({a['difficulty']}, {wall}ms)")
        rows.append({
            "alert_id": a["alert_id"], "schema": True, "hit": hit,
            "got": out.disposition, "want": a["expected_disposition"],
            "difficulty": a["difficulty"], "risk_level": out.risk_level,
            "confidence": out.disposition_confidence, "evidence_grounded": [h, t],
            "red_flags": out.red_flags, "rationale": out.rationale,
            "metrics": m.model_dump() if m else None,
        })

    n = len(alerts)
    s_rate, d_rate = schema_ok / n, disp_ok / n
    print("\n" + "=" * 68)
    print(f"  schema validity    {schema_ok}/{n}  {s_rate:6.1%}   bar {BAR_SCHEMA:.0%}   "
          f"{'PASS' if s_rate >= BAR_SCHEMA else 'FAIL'}")
    print(f"  disposition match  {disp_ok}/{n}  {d_rate:6.1%}   bar {BAR_DISPOSITION:.0%}   "
          f"{'PASS' if d_rate >= BAR_DISPOSITION else 'FAIL'}")
    if ev_total:
        print(f"  evidence grounded  {ev_hits}/{ev_total}  {ev_hits/ev_total:6.1%}")

    by_diff = {}
    for r in rows:
        if "hit" in r:
            by_diff.setdefault(r["difficulty"], []).append(r["hit"])
    for d, hits in sorted(by_diff.items()):
        print(f"    {d:<10} {sum(hits)}/{len(hits)}")

    if jev_ms:
        print("\n  head to head")
        print(f"    jev   median {statistics.median(jev_ms):6.0f} ms   "
              f"total cost ${jev_cost:.6f}")
        if llm_ms:
            print(f"    llm   median {statistics.median(llm_ms):6.0f} ms   "
                  f"{len(llm_ms) * 0.2:.1f} platform units")
            print(f"    jev is {statistics.median(llm_ms)/statistics.median(jev_ms):.1f}x faster")
        if agree:
            print(f"    agreement {sum(agree)}/{len(agree)}")

    Path("evals/results.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print("\n  wrote evals/results.json")
    print("=" * 68)

    return 0 if (s_rate >= BAR_SCHEMA and d_rate >= BAR_DISPOSITION) else 1


if __name__ == "__main__":
    sys.exit(main())
