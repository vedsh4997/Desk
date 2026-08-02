#!/usr/bin/env python3
"""
MARKET ENGINE v1 — Ved's personal finance analyst.

Modes:
  python engine.py --mode brief   # morning brief  -> ntfy push
  python engine.py --mode scan    # intraday check -> alerts only when YOUR rules fire
  python engine.py --mode wrap    # evening wrap + Tomorrow's Watch

Env vars:
  NTFY_TOPIC          your private ntfy.sh topic (required for phone pushes)
  ANTHROPIC_API_KEY   optional — turns raw numbers into the senior-PM voice
"""

import argparse
import datetime as dt
import json
import os
from zoneinfo import ZoneInfo

import requests
import yfinance as yf

ROOT = os.path.dirname(os.path.abspath(__file__))
WATCHLIST_FILE = os.path.join(ROOT, "watchlist.json")
STATE_FILE = os.path.join(ROOT, "state.json")

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
CT = ZoneInfo("America/Chicago")


# ---------------- helpers ----------------

def now_ct():
    return dt.datetime.now(CT)


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)


def fmt(x, nd=2):
    return "n/a" if x is None else f"{x:,.{nd}f}"


# ---------------- 1 · data layer ----------------

def fetch_one(sym):
    """Price, day move, 52w range, 200-week MA, next earnings, top headlines."""
    t = yf.Ticker(sym)
    daily = t.history(period="1y", auto_adjust=True)
    if daily is None or daily.empty:
        return None
    close = daily["Close"].dropna()
    price = float(close.iloc[-1])
    prev = float(close.iloc[-2]) if len(close) > 1 else price
    day_pct = (price / prev - 1.0) * 100 if prev else 0.0

    ma200w, weeks = None, 0
    try:
        weekly = t.history(period="5y", interval="1wk", auto_adjust=True)
        wclose = weekly["Close"].dropna()
        weeks = len(wclose)
        if weeks >= 30:  # young listings get a partial MA, flagged in output
            ma200w = float(wclose.tail(200).mean())
    except Exception:
        pass

    return {
        "sym": sym,
        "price": price,
        "prev": prev,
        "day_pct": day_pct,
        "hi52": float(close.max()),
        "lo52": float(close.min()),
        "ma200w": ma200w,
        "ma_dist_pct": ((price / ma200w) - 1.0) * 100 if ma200w else None,
        "weeks": weeks,
        "earnings": next_earnings(t),
        "news": news_titles(t),
    }


def next_earnings(t):
    try:
        cal = t.calendar
        if isinstance(cal, dict):
            d = cal.get("Earnings Date")
            if d:
                d0 = d[0] if isinstance(d, (list, tuple)) else d
                return str(d0)
    except Exception:
        pass
    return None


def news_titles(t, n=2):
    """yfinance news schema changed across versions — handle both shapes."""
    out = []
    try:
        for item in (t.news or [])[:8]:
            c = item.get("content", item) or {}
            title = c.get("title")
            if title and title.strip() not in out:
                out.append(title.strip())
            if len(out) >= n:
                break
    except Exception:
        pass
    return out


def fetch_all(symbols):
    data, dead = {}, []
    for s in symbols:
        try:
            row = fetch_one(s)
        except Exception as e:
            print(f"[fetch] {s} failed: {e}")
            row = None
        if row:
            data[s] = row
        else:
            dead.append(s)
    return data, dead


# ---------------- 2 · rules engine ----------------

def crossed_down(prev, cur, level):
    return prev is not None and level is not None and prev > level >= cur


def crossed_up(prev, cur, level):
    return prev is not None and level is not None and prev < level <= cur


def run_rules(cfg, d, rules_cfg):
    """Returns list of (dedupe_key, title, message, priority)."""
    out = []
    sym, p, prev = d["sym"], d["price"], d["prev"]
    bz = (cfg.get("buy_zone") or [None, None])
    lo, hi = (list(bz) + [None, None])[:2]
    tgt = cfg.get("target")

    if hi and crossed_down(prev, p, hi):
        out.append((
            f"{sym}:zone",
            f"🟢 {sym} entered your buy zone",
            f"{sym} at ${fmt(p)} — inside your ${fmt(lo)}–${fmt(hi)} zone. Your move.",
            "high",
        ))

    if tgt and crossed_up(prev, p, tgt):
        out.append((
            f"{sym}:target",
            f"🎯 {sym} hit your target",
            f"{sym} at ${fmt(p)} — through your ${fmt(tgt)} target. Trim/hold decision time.",
            "high",
        ))

    big = rules_cfg.get("big_move_pct", 4.0)
    if abs(d["day_pct"]) >= big:
        arrow = "🔺" if d["day_pct"] > 0 else "🔻"
        heads = "; ".join(d["news"]) or "no headlines yet"
        out.append((
            f"{sym}:bigmove",
            f"{arrow} {sym} {d['day_pct']:+.1f}% today",
            f"{sym} ${fmt(p)} ({d['day_pct']:+.1f}%). 📰 {heads}",
            "high",
        ))

    ma = d["ma200w"]
    if ma and (crossed_down(prev, p, ma) or crossed_up(prev, p, ma)):
        side = "reclaimed" if p >= ma else "lost"
        out.append((
            f"{sym}:ma200w",
            f"⚠️ {sym} {side} its 200-week MA",
            f"{sym} ${fmt(p)} vs 200W MA ${fmt(ma)}. Long-term trend line in play.",
            "default",
        ))

    return out


# ---------------- 3 · analyst brain (optional) ----------------

PERSONA = (
    "You are a sharp, plain-spoken senior portfolio manager writing Ved's {kind}. "
    "No hype, no long disclaimers, no predictions — use scenarios with concrete levels "
    "(support/resistance, his buy zones, 200-week MA, earnings dates). Explain WHY things "
    "moved and connect macro to his tickers. Under 220 words, punchy, phone-readable. "
    "End with '📌 Watch:' — 2-3 bullets of catalysts and levels for the next session."
)


def analyst_rewrite(kind, context):
    if not ANTHROPIC_KEY:
        return None
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 800,
                "system": PERSONA.format(kind=kind),
                "messages": [{"role": "user", "content": context}],
            },
            timeout=90,
        )
        r.raise_for_status()
        parts = r.json().get("content", [])
        text = "\n".join(p.get("text", "") for p in parts if p.get("type") == "text").strip()
        return text or None
    except Exception as e:
        print(f"[analyst] falling back to raw numbers: {e}")
        return None


# ---------------- 4 · delivery ----------------

def push(title, body, priority="default", tags="chart_with_upwards_trend"):
    print(f"\n=== {title} ===\n{body}\n")  # full text always lands in the Actions log
    if not NTFY_TOPIC:
        print("[ntfy] NTFY_TOPIC not set — printed only.")
        return
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=body[:3800].encode("utf-8"),
            headers={"Title": title, "Priority": priority, "Tags": tags},
            timeout=20,
        )
    except Exception as e:
        print(f"[ntfy] push failed: {e}")


# ---------------- report builders ----------------

def ticker_line(cfg, d):
    bits = [f"{d['sym']} ${fmt(d['price'])} ({d['day_pct']:+.1f}%)"]
    if d["ma_dist_pct"] is not None:
        young = "" if d["weeks"] >= 200 else f" ({d['weeks']}w avg)"
        bits.append(f"200W {d['ma_dist_pct']:+.0f}%{young}")
    bz = (cfg.get("buy_zone") or [None, None])
    if bz[0] and bz[1]:
        if bz[0] <= d["price"] <= bz[1]:
            bits.append("IN BUY ZONE ✅")
        else:
            bits.append(f"zone {fmt(bz[0], 0)}–{fmt(bz[1], 0)}")
    if cfg.get("target"):
        bits.append(f"tgt {fmt(cfg['target'], 0)}")
    if d["earnings"]:
        bits.append(f"ER {str(d['earnings'])[:10]}")
    line = " | ".join(bits)
    if d["news"]:
        line += f"\n   📰 {d['news'][0]}"
    return line


def build_report(kind, wl, data, dead):
    ts = now_ct().strftime("%a %b %d, %I:%M %p CT")
    head = "🌅 MORNING BRIEF" if kind == "morning brief" else "🌙 EVENING WRAP"
    lines = [f"{head} — {ts}", ""]
    pulse = [data[s] for s in wl.get("market_pulse", []) if s in data]
    if pulse:
        lines.append("Market: " + "   ".join(f"{d['sym']} {d['day_pct']:+.1f}%" for d in pulse))
        lines.append("")
    for sym, cfg in wl.get("tickers", {}).items():
        if sym in data:
            lines.append(ticker_line(cfg, data[sym]))
    if dead:
        lines.append(f"\n⚠️ no data: {', '.join(dead)}")
    return "\n".join(lines)


# ---------------- modes ----------------

def do_report(kind):
    wl = load_json(WATCHLIST_FILE, {})
    tickers = list(wl.get("tickers", {}))
    syms = tickers + [s for s in wl.get("market_pulse", []) if s not in tickers]
    data, dead = fetch_all(syms)
    raw = build_report(kind, wl, data, dead)
    smart = analyst_rewrite(kind, "Today's data for the client's watchlist:\n\n" + raw)
    body = raw if not smart else raw + "\n\n———— ANALYST TAKE ————\n" + smart
    title = "Morning Brief ☕" if kind == "morning brief" else "Evening Wrap 🌙"
    push(title, body)


def do_scan():
    wl = load_json(WATCHLIST_FILE, {})
    state = load_json(STATE_FILE, {})
    today = now_ct().strftime("%Y-%m-%d")
    data, dead = fetch_all(list(wl.get("tickers", {})))
    if dead:
        print(f"[scan] no data: {', '.join(dead)}")
    fired = 0
    for sym, cfg in wl.get("tickers", {}).items():
        if sym not in data:
            continue
        for key, title, msg, prio in run_rules(cfg, data[sym], wl.get("alert_rules", {})):
            skey = f"{today}:{key}"
            if state.get(skey):
                continue  # already alerted today — no spam
            push(title, msg, priority=prio, tags="rotating_light")
            state[skey] = True
            fired += 1
    state = {k: v for k, v in state.items() if k.startswith(today)}  # keep state tiny
    save_json(STATE_FILE, state)
    print(f"[scan] {fired} alert(s) fired.")


def main():
    ap = argparse.ArgumentParser(description="Market Engine v1")
    ap.add_argument("--mode", choices=["brief", "scan", "wrap"], required=True)
    m = ap.parse_args().mode
    if m == "brief":
        do_report("morning brief")
    elif m == "wrap":
        do_report("evening wrap")
    else:
        do_scan()


if __name__ == "__main__":
    main()
