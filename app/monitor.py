"""Alerts and observability: e-mail the owner about serious problems and unusual traffic.

- Serious problems e-mail at once (budget thresholds, the ledger down, the model API rejecting the key or failing
  repeatedly, Tavily's quota, unexpected server errors), each kind at most once per cooldown.
- A daily check e-mails only when papers, questions or spend jump to 3x the 7-day average (with a floor).

Counters and "already sent" markers live in Upstash Redis (the same durable store as the spending ledger), so
restarts neither lose counts nor repeat e-mails. Without Upstash they fall back to this process's memory.
E-mails go through Resend (RESEND_API_KEY, ALERT_EMAIL); without a key, alerts are only logged.
Checks run on incoming requests, throttled: a sleeping server has no traffic, so nothing to report.
"""
from __future__ import annotations

import datetime as dt
import threading
import time
from typing import Dict, Optional

import httpx

from . import budget, config

RESEND_API_KEY = config._str("RESEND_API_KEY")
ALERT_EMAIL = config._str("ALERT_EMAIL")
ALERT_FROM = config._str("ALERT_FROM", "OpenDAG alerts <onboarding@resend.dev>")
SPIKE_FACTOR = 3.0
SPIKE_FLOOR = {"papers": 10, "asks": 60, "spend_usd": 1.0}   # below these a "spike" isn't worth an e-mail
BUDGET_STEPS = (0.25, 0.5, 0.8, 1.0)

_mem: Dict[str, float] = {}           # fallback store when Upstash isn't configured
_mem_exp: Dict[str, float] = {}
_lock = threading.Lock()
_last_check = {"t": 0.0}


# ---------- tiny store: Upstash if available, memory otherwise ----------

def _redis(cmd):
    return budget._redis(cmd) if budget.durable() else None


def _incr(key: str, by: float = 1.0, ttl_days: int = 40) -> None:
    try:
        if budget.durable():
            _redis(["INCRBYFLOAT", key, f"{by:.8f}"])
            _redis(["EXPIRE", key, str(ttl_days * 86400)])
            return
    except Exception:
        pass
    with _lock:
        _mem[key] = _mem.get(key, 0.0) + by


def _get(key: str) -> float:
    try:
        if budget.durable():
            return float(_redis(["GET", key]) or 0)
    except Exception:
        pass
    return _mem.get(key, 0.0)


def _once(key: str, seconds: int) -> bool:
    """True the first time within `seconds` (a SET NX with expiry): the cooldown for each kind of e-mail."""
    try:
        if budget.durable():
            return _redis(["SET", key, "1", "NX", "EX", str(seconds)]) == "OK"
    except Exception:
        pass
    with _lock:
        now = time.time()
        if _mem_exp.get(key, 0) > now:
            return False
        _mem_exp[key] = now + seconds
        return True


def _day(offset: int = 0) -> str:
    return (dt.datetime.utcnow().date() - dt.timedelta(days=offset)).isoformat()


# ---------- metrics ----------

def count(metric: str, by: float = 1.0) -> None:
    """Daily counters: papers, asks, errors, model_errors, spend_usd."""
    _incr(f"opendag:m:{_day()}:{metric}", by)


def day_total(metric: str, offset: int = 0) -> float:
    return _get(f"opendag:m:{_day(offset)}:{metric}")


# ---------- e-mail ----------

def send(subject: str, text: str) -> bool:
    print(f"[alert] {subject}\n{text}")
    if not (RESEND_API_KEY and ALERT_EMAIL):
        return False
    try:
        r = httpx.post("https://api.resend.com/emails", timeout=15,
                       headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
                       json={"from": ALERT_FROM, "to": [ALERT_EMAIL], "subject": f"[OpenDAG] {subject}", "text": text})
        return r.status_code < 300
    except httpx.HTTPError:
        return False


def alert(kind: str, subject: str, text: str, cooldown_s: int = 6 * 3600) -> bool:
    """Send at most one e-mail of this kind per cooldown. Never raises: alerting must not break the app."""
    try:
        if _once(f"opendag:alert:{kind}", cooldown_s):
            return send(subject, text + f"\n\nTime (UTC): {dt.datetime.utcnow():%Y-%m-%d %H:%M}\nBudget: {budget.status()}")
    except Exception as e:
        print(f"[alert] failed to send {kind}: {e}")
    return False


# ---------- what counts as serious ----------

def model_error(err: Exception) -> None:
    """Called when a Nemotron call ultimately fails."""
    count("model_errors")
    msg = str(err)
    if any(s in msg for s in ("401", "403", "Unauthorized", "invalid api key", "Invalid API key")):
        alert("model_auth", "Nemotron is rejecting the API key",
              f"Token Factory refused the API key, so papers and questions are failing.\nCheck NEBIUS_API_KEY and your "
              f"credits in the Nebius console.\n\nError: {msg[:500]}", cooldown_s=3 * 3600)
    elif day_total("model_errors") >= 5:
        alert("model_errors", "Nemotron calls are failing repeatedly",
              f"{int(day_total('model_errors'))} model calls have failed today (after retries).\nLast error: {msg[:500]}",
              cooldown_s=6 * 3600)


def tavily_limit(status: int) -> None:
    alert("tavily_limit", "Tavily's search limit is reached",
          f"Tavily answered HTTP {status} (credit or pay-as-you-go limit). Papers still process, but without evidence "
          f"links until the limit resets or is raised in the Tavily dashboard.", cooldown_s=24 * 3600)


def server_error(where: str, err: Exception) -> None:
    count("errors")
    alert(f"server_error:{where}", f"Unexpected error in {where}",
          f"{type(err).__name__}: {str(err)[:800]}\n\nErrors today: {int(day_total('errors'))}", cooldown_s=3 * 3600)


def budget_paused(reason: str) -> None:
    kind = "ledger_down" if "ledger" in reason.lower() else "budget_100"
    subject = ("The spending ledger is unavailable: AI features paused" if kind == "ledger_down"
               else "Spending cap reached: AI features paused")
    alert(kind, subject, reason, cooldown_s=6 * 3600 if kind == "ledger_down" else 40 * 86400)


# ---------- periodic checks (run from requests, at most every 10 minutes) ----------

def check_budget() -> None:
    st = budget.status()
    if st.get("ledger") == "unavailable":
        budget_paused(st.get("reason", "ledger unavailable"))
        return
    used = st.get("spent_usd", 0) / max(st.get("budget_usd", 1), 1e-9)
    for step in BUDGET_STEPS:
        if used >= step:
            alert(f"budget_{int(step * 100)}", f"{int(step * 100)}% of the AI budget used",
                  f"OpenDAG has spent ${st['spent_usd']:.2f} of its ${st['budget_usd']:.0f} cap "
                  f"(Nemotron ${st.get('nemotron_usd', 0):.2f}, Tavily ${st.get('tavily_usd', 0):.2f}).",
                  cooldown_s=40 * 86400)


def check_spike() -> Optional[str]:
    """Once a day, compare yesterday with the 7 days before it; e-mail only on a real jump."""
    if not _once(f"opendag:digest:{_day()}", 26 * 3600):
        return None
    lines, spiking = [], []
    for metric, floor in SPIKE_FLOOR.items():
        y = day_total(metric, 1)
        avg = sum(day_total(metric, d) for d in range(2, 9)) / 7
        lines.append(f"{metric}: yesterday {y:g}, 7-day average {avg:.1f}")
        if y >= floor and y >= SPIKE_FACTOR * max(avg, 1e-9):
            spiking.append(metric)
    if spiking:
        send(f"Traffic spike: {', '.join(spiking)}", "Yesterday was unusually busy.\n\n" + "\n".join(lines)
             + f"\n\nErrors yesterday: {int(day_total('errors', 1))}, model errors: {int(day_total('model_errors', 1))}"
             + f"\nBudget: {budget.status()}")
        return ", ".join(spiking)
    return None


def tick() -> None:
    """Called on requests; does real work at most every 10 minutes, in the background."""
    now = time.time()
    if now - _last_check["t"] < 600:
        return
    _last_check["t"] = now

    def run():
        try:
            check_budget()
            check_spike()
        except Exception as e:
            print(f"[monitor] check failed: {e}")
    threading.Thread(target=run, daemon=True).start()
