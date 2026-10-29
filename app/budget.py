"""Hard spending cap for every paid call (Nemotron on Nebius Token Factory, Tavily).

Before a call, its worst-case cost (all allowed output tokens used) is checked against the budget; the call
is refused if it could push total spend past BUDGET_USD. After the call, the real cost (from the token usage
the API reports) is recorded. If the ledger can't be read, paid calls are refused (fail closed).

Nebius budgets only send e-mail alerts, hours late, and don't stop usage, so this is the hard stop.

The ledger lives in SQLite by default. On a host whose disk is wiped on restart (Render's free plan), set
UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN so the total survives restarts; with
REQUIRE_DURABLE_BUDGET=1 paid calls are refused until that durable ledger is configured.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Optional

import httpx

from . import config, db

_lock = threading.Lock()


class BudgetExceeded(Exception):
    """Shown to users as-is."""


def _f(name: str, default: float) -> float:
    raw = config._str(name)
    return float(raw) if raw else default


# USD per million tokens (input, output). Defaults are Nebius Token Factory list prices; an unknown model is
# priced high on purpose so a typo in MODEL_* can only make the cap stricter.
PRICES = {
    "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B": (_f("PRICE_NANO_IN", 0.06), _f("PRICE_NANO_OUT", 0.24)),
    "nvidia/nemotron-3-super-120b-a12b": (_f("PRICE_SUPER_IN", 0.30), _f("PRICE_SUPER_OUT", 0.90)),
}
UNKNOWN_MODEL_PRICE = (_f("PRICE_UNKNOWN_IN", 2.0), _f("PRICE_UNKNOWN_OUT", 8.0))
TAVILY_PER_SEARCH = _f("PRICE_TAVILY_SEARCH", 0.008)  # pay-as-you-go price; basic search = 1 credit

BUDGET_USD = _f("BUDGET_USD", 100.0)              # stop here: $20 under a $120 limit, for price drift
SPENT_BEFORE_LEDGER_USD = _f("SPENT_BEFORE_LEDGER_USD", 0.0)  # spend from before this ledger existed
REDIS_URL = config._str("UPSTASH_REDIS_REST_URL").rstrip("/")
REDIS_TOKEN = config._str("UPSTASH_REDIS_REST_TOKEN")
REQUIRE_DURABLE = config._str("REQUIRE_DURABLE_BUDGET").lower() in ("1", "true", "yes")
REDIS_KEY = "opendag:spent_usd"

EXHAUSTED_MSG = ("OpenDAG's AI budget for this demo is used up, so adding papers and asking questions are paused. "
                 "Browsing the dataset, the charts and the network still works.")


# ---------- ledger storage ----------

def _redis(cmd: list) -> Optional[str]:
    r = httpx.post(REDIS_URL, headers={"Authorization": f"Bearer {REDIS_TOKEN}"}, json=cmd, timeout=10)
    r.raise_for_status()
    return r.json().get("result")


def _sqlite_spent() -> float:
    db.conn().execute("CREATE TABLE IF NOT EXISTS spend (ts REAL, kind TEXT, usd REAL)")
    return db.conn().execute("SELECT COALESCE(SUM(usd), 0) FROM spend WHERE kind='llm'").fetchone()[0]


def durable() -> bool:
    return bool(REDIS_URL and REDIS_TOKEN)


_tavily_cache = {"usd": None, "at": 0.0}


def tavily_spent() -> float:
    """Real Tavily spend: pay-as-you-go credits used this billing cycle (Tavily's /usage API) x price.

    Searches inside the plan's free credits cost nothing. Cached for 5 minutes. If /usage can't be reached,
    every search this server recorded is counted at the pay-as-you-go price (an over-estimate, on purpose).
    """
    now = time.time()
    if _tavily_cache["usd"] is not None and now - _tavily_cache["at"] < 300:
        return _tavily_cache["usd"]
    try:
        r = httpx.get("https://api.tavily.com/usage", headers={"Authorization": f"Bearer {config.TAVILY_API_KEY}"}, timeout=10)
        r.raise_for_status()
        usd = float(r.json().get("account", {}).get("paygo_usage") or 0) * TAVILY_PER_SEARCH
    except Exception:
        n = db.conn().execute("SELECT COUNT(*) FROM spend WHERE kind='tavily'").fetchone()[0] if _has_spend() else 0
        usd = n * TAVILY_PER_SEARCH
    _tavily_cache.update(usd=usd, at=now)
    return usd


def _has_spend() -> bool:
    db.conn().execute("CREATE TABLE IF NOT EXISTS spend (ts REAL, kind TEXT, usd REAL)")
    return True


def llm_spent() -> float:
    if REQUIRE_DURABLE and not durable():
        raise BudgetExceeded("Paid AI features are off: the spending ledger isn't configured on this server.")
    try:
        if durable():
            return float(_redis(["GET", REDIS_KEY]) or 0.0)
        return float(_sqlite_spent())
    except BudgetExceeded:
        raise
    except Exception as e:
        raise BudgetExceeded(f"Paid AI features are paused: the spending ledger is unavailable ({type(e).__name__}).")


def spent() -> float:
    """Total spend: Nemotron (from the ledger) + Tavily (from Tavily). Fails closed if the ledger is unreadable."""
    return SPENT_BEFORE_LEDGER_USD + llm_spent() + (tavily_spent() if config.TAVILY_API_KEY else 0.0)


def _add(kind: str, usd: float) -> None:
    if usd <= 0:
        return
    if durable() and kind == "llm":  # Tavily keeps its own durable count (/usage)
        _redis(["INCRBYFLOAT", REDIS_KEY, f"{usd:.8f}"])
    if kind == "llm":
        from . import monitor
        monitor.count("spend_usd", usd)
    with db._lock:
        db.conn().execute("CREATE TABLE IF NOT EXISTS spend (ts REAL, kind TEXT, usd REAL)")
        db.conn().execute("INSERT INTO spend (ts, kind, usd) VALUES (?, ?, ?)", (time.time(), kind, usd))
        db.conn().commit()


# ---------- the guard ----------

def price(model: str):
    return PRICES.get(model, UNKNOWN_MODEL_PRICE)


def llm_cost(model: str, prompt_tokens: float, completion_tokens: float) -> float:
    pin, pout = price(model)
    return (prompt_tokens * pin + completion_tokens * pout) / 1e6


def check(worst_case_usd: float) -> None:
    """Refuse a call whose worst case could push total spend past the budget."""
    if spent() + worst_case_usd > BUDGET_USD:
        raise BudgetExceeded(EXHAUSTED_MSG)


def guard_llm(model: str, messages: list, max_tokens: int) -> None:
    chars = sum(len(str(m.get("content", ""))) for m in messages)
    worst_in = chars / 3 + 50  # ~4 characters per token; /3 over-estimates on purpose
    check(llm_cost(model, worst_in, max_tokens))


def record_llm(model: str, usage, max_tokens: int, messages: list) -> None:
    """Record the real cost; if the API didn't report usage, record the worst case."""
    with _lock:
        if usage and getattr(usage, "prompt_tokens", None) is not None:
            _add("llm", llm_cost(model, usage.prompt_tokens or 0, usage.completion_tokens or 0))
        else:
            chars = sum(len(str(m.get("content", ""))) for m in messages)
            _add("llm", llm_cost(model, chars / 3 + 50, max_tokens))


def guard_tavily() -> None:
    check(TAVILY_PER_SEARCH)


def record_tavily() -> None:
    """Counted locally only as a fallback; the real Tavily spend comes from Tavily's /usage."""
    with _lock:
        _add("tavily", TAVILY_PER_SEARCH)
    _tavily_cache["at"] = 0.0  # re-read Tavily's usage on the next check


def status() -> dict:
    try:
        llm = llm_spent()
        tav = tavily_spent() if config.TAVILY_API_KEY else 0.0
        s = SPENT_BEFORE_LEDGER_USD + llm + tav
        return {"budget_usd": BUDGET_USD, "spent_usd": round(s, 4), "nemotron_usd": round(llm, 4),
                "tavily_usd": round(tav, 4), "remaining_usd": round(max(0.0, BUDGET_USD - s), 4),
                "paused": s >= BUDGET_USD, "ledger": "durable" if durable() else "local"}
    except BudgetExceeded as e:
        return {"budget_usd": BUDGET_USD, "paused": True, "reason": str(e), "ledger": "unavailable"}
