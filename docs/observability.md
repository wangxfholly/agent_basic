# Observability & Loop Guard — Operator's Manual

> Companion to the [Observability & loop guard](../README.md#-observability--loop-guard--可观测与防循环)
> section in the main README. This file is the deep-dive: full Grafana
> dashboard recipe, alert rules, and the rationale behind every metric.

---

## 1. Why agent monitoring ≠ service monitoring

| Dimension | Classical service | LLM agent | Monitoring implication |
|---|---|---|---|
| Determinism | same input → same output | same input → variable output | 200 OK ≠ correct; need **eval** layer |
| Request shape | one RPC = one call | one task = N×LLM + M×tool + nested sub-agents | observe at **task** level, not request |
| Failure modes | exception or timeout | hallucination, oscillation, context bloat — all silent | classical `error_rate` is necessary but not sufficient |
| Cost model | CPU/RAM, ~constant | tokens, can grow exponentially per turn | budget guardrails are mandatory |

**One-liner:** classical monitoring catches "the box is down"; agent monitoring also has to catch "the box went insane".

---

## 2. The four signals classical APMs miss

| New signal | Why traditional metrics don't cover it |
|---|---|
| **Iter depth** (`agent.iter.depth`) | RPC graphs aren't recursive; agents are |
| **Tokens / turn** (`agent.llm.tokens`) | Cost isn't proportional to wall-clock |
| **Tool error rate / retry** (`agent.tool.errors`, `agent.tool.retries`) | Downstream services are static; tool selection is LLM-chosen |
| **Stop-reason mix** (`agent.turn.stop_reason`) | `end_turn / tool_use / max_tokens / max_iter / cancelled / error` shape shifts before regressions show in latency |

Add **eval pass-rate** (`agent.eval.pass_rate`) to catch silent quality regressions a deploy can otherwise hide.

---

## 3. Data plane

```
┌──────────────────────────────────────────────────────────┐
│  mega_agent runtime                                      │
│   events.emit(...)        hooks.on("tool.before", ...)   │
│        │                          │                      │
│        ▼                          ▼                      │
│  events.jsonl                OTel SDK                    │
└────────┬───────────────────────┬─────────────────────────┘
         │                       │ OTLP gRPC
         ▼                       ▼
   tail / Vector /        OTel Collector
   Loki / ClickHouse      (sample · scrub PII · route)
                          │       │       │
                          ▼       ▼       ▼
                       Prom    Tempo    Loki / Langfuse
                       (M)     (T)      (L + LLM full-text)
                          │       │       │
                          └───────┴───────┘
                                  │
                                  ▼
                              Grafana
                          (dashboards + alerts)
```

**Hard rule:** never send full prompts/responses through trace attributes — they will blow up your trace backend. Send hashes through OTLP, full text through Langfuse / object storage.

---

## 4. Reference exporter

```python
# mega_observe.py — drop into your driver
from opentelemetry import trace, metrics
from mega_agent.hooks import hooks
from mega_agent.events import events

tracer = trace.get_tracer("mega_agent")
meter  = metrics.get_meter("mega_agent")

m_tool = meter.create_counter  ("agent.tool.calls")
m_terr = meter.create_counter  ("agent.tool.errors")
m_iter = meter.create_histogram("agent.iter.depth")
m_loop = meter.create_counter  ("agent.loop.detected")

_spans = {}

def _pre(p):
    name   = p.get("name")
    intent = p.get("intent") or ""
    _spans[name] = tracer.start_span(
        f"tool.{name}", attributes={"agent.tool.intent": intent}
    )
    m_tool.add(1, {"tool": name})

def _post(p):
    sp = _spans.pop(p.get("name"), None)
    if sp: sp.end()

def _err(p):
    name = p.get("name")
    m_terr.add(1, {"tool": name})
    sp = _spans.pop(name, None)
    if sp:
        sp.set_attribute("error", str(p.get("error", ""))[:200]); sp.end()

def _iter(p):
    m_iter.record(p.get("iter", 0), {"backend": p.get("backend", "unknown")})

hooks.on("tool.before", _pre)
hooks.on("tool.after",  _post)
hooks.on("tool.error",  _err)
hooks.on("loop.iter",   _iter)
```

For LLM-side telemetry (tokens, cost, stop-reason, model name) wrap your
adapter — `LLMResponse` already exposes `.usage` and `.stop_reason`:

```python
# llm/observed_adapter.py
class ObservedLLM:
    def __init__(self, inner, model):
        self.inner, self.model = inner, model
    def complete(self, *a, **kw):
        with tracer.start_as_current_span("llm.complete") as sp:
            resp = self.inner.complete(*a, **kw)
            if getattr(resp, "usage", None):
                sp.set_attributes({
                    "gen_ai.system":              "anthropic",
                    "gen_ai.request.model":       self.model,
                    "gen_ai.usage.input_tokens":  resp.usage.input_tokens,
                    "gen_ai.usage.output_tokens": resp.usage.output_tokens,
                    "gen_ai.response.stop_reason": getattr(resp, "stop_reason", ""),
                })
            return resp
    def stream(self, *a, **kw):
        yield from self.inner.stream(*a, **kw)
```

> Hook handlers in `mega_agent` are **advisory** — the kernel catches any
> exception they raise and records `hook.failed`, so they never crash a turn.
> If you need to *force* a turn to abort (e.g. on `loop.detected`), flip the
> `cancel_event` you passed into `agent_loop` from outside; the kernel
> unwinds cleanly and preserves the tool_use ↔ tool_result contract.

---

## 5. Loop guard recipes

### 5.1 Repeat-call detector (oscillation + same-args replay)

```python
import hashlib, json, collections
from mega_agent.hooks import hooks
from mega_agent.events import events

_recent = collections.deque(maxlen=8)

def _loop_guard(p):
    name = p.get("name")
    args = p.get("input") or {}
    sig  = hashlib.md5(f"{name}:{json.dumps(args, sort_keys=True)}".encode()).hexdigest()[:10]
    _recent.append(sig)
    if _recent.count(sig) >= 3:
        events.emit("loop.detected", tool=name, sig=sig)
        # NOTE: hooks are advisory — to actually abort the turn,
        # flip the cancel_event passed into agent_loop from outside.

hooks.on("tool.before", _loop_guard)
```

### 5.2 Context-bloat alarm

Wrap your LLM adapter (same `ObservedLLM` pattern as §4) and emit on
`prompt_tokens > 0.8 × ctx_window`:

```python
if resp.usage.input_tokens > 0.8 * ctx_window:
    events.emit("context.bloat", tokens=resp.usage.input_tokens)
    # optionally: trigger summarisation / drop oldest tool_results
```

### 5.3 Iteration checkpoint

```python
def _progress(p):
    n = p.get("iter", 0)
    if n and n % 5 == 0:
        events.emit("progress.checkpoint", iter=n)

hooks.on("loop.iter", _progress)
```

---

## 6. Grafana dashboards

Six panels cover ~95% of incidents.

### Panel 1 · Realtime health

```promql
sum(rate(agent_turn_total[1m]))                       # turns/min
histogram_quantile(0.95, agent_turn_duration_bucket)  # P95 latency
sum(rate(agent_cost_usd_total[1m])) * 60              # $/min
```

### Panel 2 · Loop watch

```promql
# % of turns hitting max_iter
sum(rate(agent_iter_depth_count{stop="max_iter"}[5m]))
  /
sum(rate(agent_iter_depth_count[5m]))

# top repeated calls
topk(10,
  sum by (tool, args_hash) (rate(agent_tool_call_total[5m]))
)

# loop guard fires
sum(rate(agent_loop_detected_total[5m])) by (tool)
```

### Panel 3 · Tokens & cost

```promql
sum by (model) (rate(agent_llm_tokens_total{kind="in"}[1m]))
sum by (model) (rate(agent_llm_tokens_total{kind="out"}[1m]))
sum by (user)  (increase(agent_cost_usd_total[1d]))   # daily burn per user
```

### Panel 4 · Tool topology

```promql
sum by (tool) (rate(agent_tool_call_total[1m]))           # QPS
sum by (tool) (rate(agent_tool_errors_total[5m]))         # err
histogram_quantile(0.99, agent_tool_duration_bucket)      # P99
```

### Panel 5 · Stop-reason mix

Stacked area of:
`end_turn`, `tool_use`, `max_tokens`, `max_iter`, `cancelled`, `error`.

```promql
sum by (stop_reason) (rate(agent_turn_total[5m]))
```

### Panel 6 · Eval trend

```promql
agent_eval_pass_rate{set="golden"}              # CI gate
avg_over_time(agent_eval_judge_score[1h])       # online LLM-as-judge
```

---

## 7. Alert rules

| Severity | Trigger | Expression sketch | Runbook |
|---|---|---|---|
| **P0** | Safety event | prompt-injection / unauthorised tool / PII leak | `runbooks/safety.md` |
| **P1** | Loop outbreak | `max_iter_rate > 5%` for 5m | `runbooks/loop.md` |
| **P1** | Cost anomaly | per-user 5m cost > 3σ | `runbooks/cost.md` |
| **P1** | Eval regression | `pass_rate@golden` drop > 5% vs prev release | block deploy |
| **P1** | LLM throttling | 429 ratio > 1% for 3m | `runbooks/throttle.md` |
| **P2** | Tool flake | per-tool err rate > 20% for 10m | `runbooks/tool.md` |
| **P2** | Context bloat | avg prompt_tokens > 0.8·ctx_window | summarise/clip |

Every alert *must* carry `runbook_url` — pageable alerts without an SOP are a 3 a.m. tax on your team.

---

## 8. Eval pipeline

```
release -> CI runs golden_set (≥100 tasks)
            ├── pass_rate < threshold → block merge
            └── pass_rate ≥ threshold → deploy
deploy  -> sample 1–5% live traffic
            └── LLM-as-judge → metric agent_eval_judge_score
nightly -> low-score samples → human label
            └── feed back into golden_set
```

Tooling: **Langfuse Datasets**, **Phoenix Evals**, **Promptfoo** all consume OTel traces directly.

---

## 9. Minimum viable setup (1 day)

1. Pipe `events.jsonl` through Vector → Loki.
2. Run Prometheus + Grafana via `docker-compose`.
3. Wire the exporter from §4 in your `agent_loop` driver.
4. Import the panels from §6 (JSON pack TBD).
5. Connect Langfuse Cloud free tier for prompt full-text.
6. Add the loop-guard hook from §5.1.
7. Add a 100-task golden set; gate CI on it.

That's enough to spot a runaway agent within minutes instead of after the bill.

---

## 10. Bytedance internal stack mapping (optional)

| Capability | Internal pick |
|---|---|
| Metrics | Argos / ByteMonitor |
| Tracing | ByteTrace (OTel-native) |
| Logs | ByteLog / TLB |
| LLM-specific obs | self-hosted Langfuse, or AML inference monitoring |
| Dashboards | Argos Dashboard / Grafana |
| Alerting | Bytedance Alarm + Lark bot |
| Eval | reuse AML / Mira Eval Hub |

Keep instrumentation in vanilla **OTel GenAI semconv** — backend swaps stay zero-cost.
