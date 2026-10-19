#!/usr/bin/env python3
"""
qa_cost.py - LLM token usage and cost report for the post-merge QA agent
========================================================================

The agent records the token counts that every LLM response reports (input / output tokens)
and this file turns them into money, priced at Claude API list prices from pricing.json.

Why "Claude prices" even when Gemini / GitHub Models answered?  The pipeline can run on free
tiers, but the real question for a roll-out is "what would this cost on a paid API?". So the
report shows:
  * tokens per agent stage (Agent 1, Agent 2 write, Agent 2 self-heal)
  * what the run actually cost (free tiers = $0.00, Anthropic = priced from pricing.json)
  * what the same token volume would cost on each reference Claude model
  * a monthly projection for N merged PRs

Standalone use (also the demo entry point):
    python qa-agent/qa_cost.py                         # reads qa-results/llm-usage.json
    python qa-agent/qa_cost.py path/to/llm-usage.json --prs-per-month 200

Outputs (next to the usage file): qa-cost-report.md, qa-cost-report.html

Caveat shown in every report: token counts come from the provider that answered (Gemini and
OpenAI tokenizers differ from Claude's), so cost figures for other models are estimates.
Standard library only.
"""

import html
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PRICING_FILE = os.path.join(HERE, "pricing.json")
DEFAULT_REFERENCE_MODELS = ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"]


# --------------------------------------------------------------------------- #
# Pricing
# --------------------------------------------------------------------------- #
def load_pricing(path=None):
    path = path or DEFAULT_PRICING_FILE
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        print(f"⚠️  Cannot read pricing file {path}: {e}")
        return {"models": {}}


def rates_for(model_id, pricing, prompt_tokens=0):
    """(input_rate, output_rate) in USD per million tokens, or None if the model is unknown."""
    m = pricing.get("models", {}).get(model_id)
    if not m:
        return None
    threshold = m.get("long_context_threshold")
    if threshold and prompt_tokens > threshold:
        return m["long_input"], m["long_output"]
    return m["input"], m["output"]


def cost_of(rates, input_tokens, output_tokens):
    return (input_tokens * rates[0] + output_tokens * rates[1]) / 1_000_000


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #
def stage_of(purpose):
    p = purpose or ""
    if p.startswith("Agent 2 heal"):
        return "Agent 2 - self-heal"
    if p.startswith("Agent 2"):
        return "Agent 2 - write tests"
    if p.startswith("Agent 1"):
        return "Agent 1 - scenarios"
    return p or "other"


def summarize(calls, pricing, free_providers=None, reference_models=None, prs_per_month=50):
    free = set(free_providers or ["github", "gemini"])
    refs = [m for m in (reference_models or DEFAULT_REFERENCE_MODELS) if m in pricing.get("models", {})]
    stages = {}
    actual, actual_known = 0.0, True
    for c in calls:
        s = stages.setdefault(stage_of(c.get("purpose")), {"calls": 0, "input": 0, "output": 0})
        s["calls"] += 1
        s["input"] += c.get("input_tokens", 0)
        s["output"] += c.get("output_tokens", 0)
        if c.get("provider") in free:
            pass  # free tier: $0
        else:
            r = rates_for(c.get("model"), pricing, c.get("input_tokens", 0))
            if r:
                actual += cost_of(r, c.get("input_tokens", 0), c.get("output_tokens", 0))
            else:
                actual_known = False  # paid provider we have no price for

    total_in = sum(c.get("input_tokens", 0) for c in calls)
    total_out = sum(c.get("output_tokens", 0) for c in calls)
    equivalents = []
    for m in refs:
        # priced call by call, so long-context tiers apply per request
        total = 0.0
        for c in calls:
            r = rates_for(m, pricing, c.get("input_tokens", 0))
            total += cost_of(r, c.get("input_tokens", 0), c.get("output_tokens", 0))
        equivalents.append({"model": m, "label": pricing["models"][m].get("label", m), "cost": total,
                            "per_month": total * prs_per_month})
    return {
        "calls": len(calls),
        "estimated_calls": sum(1 for c in calls if c.get("estimated")),
        "input_tokens": total_in,
        "output_tokens": total_out,
        "stages": stages,
        "providers": sorted({f"{c.get('provider')}/{c.get('model')}" for c in calls}),
        "actual_cost": actual if actual_known else None,
        "equivalents": equivalents,
        "prs_per_month": prs_per_month,
        "pricing_checked_on": pricing.get("checked_on", "?"),
    }


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def usd(x):
    if x is None:
        return "n/a"
    if x == 0:
        return "$0.00"
    return f"${x:,.4f}" if x < 1 else f"${x:,.2f}"


def render_markdown(s, heading="## 💰 LLM cost"):
    if not s["calls"]:
        return f"{heading}\nNo LLM calls were made in this run, so it cost nothing.\n"
    lines = [heading, "",
             f"**{s['calls']} LLM call(s), {s['input_tokens']:,} input + {s['output_tokens']:,} output tokens**  "
             f"(answered by: {', '.join(s['providers'])})", "",
             "| Stage | Calls | Input tokens | Output tokens |", "|---|---:|---:|---:|"]
    for name, st in s["stages"].items():
        lines.append(f"| {name} | {st['calls']} | {st['input']:,} | {st['output']:,} |")
    lines += ["", f"**Actually billed for this run: {usd(s['actual_cost'])}**"
              + (" (free-tier providers)" if s["actual_cost"] == 0 else ""), "",
              "| If this run used... | Cost per run | Per month at "
              f"{s['prs_per_month']} merged PRs |", "|---|---:|---:|"]
    for e in s["equivalents"]:
        lines.append(f"| {e['label']} | {usd(e['cost'])} | {usd(e['per_month'])} |")
    note = (f"_Claude API list prices checked {s['pricing_checked_on']} (pricing.json). Token counts come from the "
            "provider that answered; other tokenizers differ from Claude's, so treat other-model figures as estimates")
    if s["estimated_calls"]:
        note += f" ({s['estimated_calls']} call(s) had no usage data and were estimated at ~4 characters per token)"
    lines += ["", note + "._", ""]
    return "\n".join(lines)


def render_html(s):
    esc = html.escape
    max_cost = max([e["cost"] for e in s["equivalents"]] or [0]) or 1
    rows = "".join(
        f"<tr><td>{esc(n)}</td><td class=n>{st['calls']}</td><td class=n>{st['input']:,}</td>"
        f"<td class=n>{st['output']:,}</td></tr>" for n, st in s["stages"].items())
    eq = "".join(
        f"<tr><td>{esc(e['label'])}</td><td class=n>{usd(e['cost'])}</td><td class=n>{usd(e['per_month'])}</td>"
        f"<td><div class=bar style='width:{max(2, int(100 * e['cost'] / max_cost))}%'></div></td></tr>"
        for e in s["equivalents"])
    billed = usd(s["actual_cost"])
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>QA agent LLM cost</title><style>
:root{{--bg:#fff;--fg:#1b1f24;--mut:#59636e;--line:#d8dee4;--acc:#0b6bcb;--card:#f6f8fa}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0d1117;--fg:#e6edf3;--mut:#9198a1;--line:#30363d;--acc:#4493f8;--card:#161b22}}}}
body{{margin:0;padding:24px 16px;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif}}
main{{max-width:860px;margin:auto}}h1{{font-size:1.5rem;margin:0 0 4px}}h2{{font-size:1.05rem;margin:28px 0 8px}}
.mut{{color:var(--mut);font-size:.9rem}}.cards{{display:flex;gap:12px;flex-wrap:wrap;margin:16px 0}}
.card{{flex:1 1 150px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}}
.card b{{display:block;font-size:1.5rem}}table{{width:100%;border-collapse:collapse}}
th,td{{padding:8px 6px;border-bottom:1px solid var(--line);text-align:left}}th{{font-size:.8rem;color:var(--mut);text-transform:uppercase}}
.n{{text-align:right;font-variant-numeric:tabular-nums}}.bar{{height:10px;background:var(--acc);border-radius:5px}}
</style></head><body><main>
<h1>QA agent - LLM cost for this run</h1>
<div class=mut>Answered by: {esc(', '.join(s['providers']) or '-')}</div>
<div class=cards>
<div class=card><span class=mut>LLM calls</span><b>{s['calls']}</b></div>
<div class=card><span class=mut>Input tokens</span><b>{s['input_tokens']:,}</b></div>
<div class=card><span class=mut>Output tokens</span><b>{s['output_tokens']:,}</b></div>
<div class=card><span class=mut>Actually billed</span><b>{billed}</b></div></div>
<h2>Tokens per stage</h2>
<table><tr><th>Stage</th><th class=n>Calls</th><th class=n>Input</th><th class=n>Output</th></tr>{rows}</table>
<h2>The same run on Claude (list prices)</h2>
<table><tr><th>Model</th><th class=n>Per run</th><th class=n>Per month ({s['prs_per_month']} PRs)</th><th></th></tr>{eq}</table>
<p class=mut>Claude API list prices checked {esc(str(s['pricing_checked_on']))} (qa-agent/pricing.json). Token counts come from the provider that
answered; other tokenizers differ from Claude's, so figures for other models are estimates.</p>
</main></body></html>"""


def write_reports(calls, out_dir, pricing_file=None, cost_cfg=None):
    """Write llm-usage.json, qa-cost-report.md and qa-cost-report.html into out_dir; return the summary."""
    cost_cfg = cost_cfg or {}
    os.makedirs(out_dir, exist_ok=True)
    pricing = load_pricing(pricing_file)
    s = summarize(calls, pricing, cost_cfg.get("free_providers"), cost_cfg.get("reference_models"),
                  cost_cfg.get("prs_per_month", 50))
    with open(os.path.join(out_dir, "llm-usage.json"), "w", encoding="utf-8") as f:
        json.dump({"calls": calls}, f, indent=2)
    with open(os.path.join(out_dir, "qa-cost-report.md"), "w", encoding="utf-8") as f:
        f.write(render_markdown(s, "# 💰 QA agent LLM cost"))
    with open(os.path.join(out_dir, "qa-cost-report.html"), "w", encoding="utf-8") as f:
        f.write(render_html(s))
    return s


def main(argv):
    args = [a for a in argv[1:] if not a.startswith("--")]
    prs = 50
    if "--prs-per-month" in argv:
        prs = int(argv[argv.index("--prs-per-month") + 1])
        args = [a for a in args if a != str(prs)]
    usage_file = args[0] if args else os.path.join("qa-results", "llm-usage.json")
    try:
        with open(usage_file, encoding="utf-8") as f:
            calls = json.load(f)["calls"]
    except (OSError, ValueError, KeyError) as e:
        print(f"Cannot read {usage_file}: {e}\nRun the agent once first (it writes qa-results/llm-usage.json).")
        return 1
    out_dir = os.path.dirname(os.path.abspath(usage_file))
    s = write_reports(calls, out_dir, cost_cfg={"prs_per_month": prs})
    print(render_markdown(s))
    print(f"Written: {os.path.join(out_dir, 'qa-cost-report.md')} and qa-cost-report.html")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
