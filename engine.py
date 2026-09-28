#!/usr/bin/env python3
"""
MARKET ENGINE v2 -- Ved's personal finance analyst.

Modes:
  python engine.py --mode publish  # fetch everything -> writes docs/data.json (dashboard reads this)
  python engine.py --mode brief     # morning brief  -> ntfy push (optional)
  python engine.py --mode scan      # intraday alerts on active names
  python engine.py --mode wrap      # evening wrap

Two buckets in watchlist.json:
  active   -> traded names: movers, buy zones, targets, 200W MA, scans
  longterm -> income names: dividend yield, ex-div, safety, growth streak

Env vars:
  NTFY_TOPIC          optional ntfy.sh topic for phone pushes
  ANTHROPIC_API_KEY   optional -- the senior-PM analyst voice
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
DATA_OUT = os.path.join(ROOT, "docs", "data.json")

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
CT = ZoneInfo("America/Chicago")


def now_ct():
    return dt.datetime.now(CT)

def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)

def fmt(x, nd=2):
    return "n/a" if x is None else f"{x:,.{nd}f}"


def day_move(close):
    price = float(close.iloc[-1])
    prev = float(close.iloc[-2]) if len(close) > 1 else price
    return price, prev, ((price / prev - 1.0) * 100 if prev else 0.0)

def next_earnings(t):
    try:
        cal = t.calendar
        if isinstance(cal, dict):
            d = cal.get("Earnings Date")
            if d:
                d0 = d[0] if isinstance(d, (list, tuple)) else d
                return str(d0)[:10]
    except Exception:
        pass
    return None

def news_titles(t, n=2):
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


def fetch_active(sym):
    t = yf.Ticker(sym)
    daily = t.history(period="1y", auto_adjust=True)
    if daily is None or daily.empty:
        return None
    close = daily["Close"].dropna()
    price, prev, pct = day_move(close)
    ma200w, weeks = None, 0
    try:
        weekly = t.history(period="5y", interval="1wk", auto_adjust=True)
        wclose = weekly["Close"].dropna()
        weeks = len(wclose)
        if weeks >= 30:
            ma200w = float(wclose.tail(200).mean())
    except Exception:
        pass
    return {
        "sym": sym, "price": price, "prev": prev, "pct": pct,
        "hi52": float(close.max()), "lo52": float(close.min()),
        "ma200": round(((price / ma200w) - 1.0) * 100) if ma200w else None,
        "ma200w_val": ma200w, "weeks": weeks,
        "er": next_earnings(t), "news": (news_titles(t, 1) or [""])[0],
    }


FREQ = {12: "monthly", 4: "quarterly", 2: "semi-annual", 1: "annual"}

def div_growth_streak(t):
    try:
        divs = t.dividends
        if divs is None or divs.empty:
            return None
        by_year = divs.groupby(divs.index.year).sum()
        cur = now_ct().year
        yrs = sorted(y for y in by_year.index if y < cur)
        if len(yrs) < 2:
            return None
        streak = 0
        for i in range(len(yrs) - 1, 0, -1):
            if by_year[yrs[i]] > by_year[yrs[i - 1]] * 1.001:
                streak += 1
            else:
                break
        return streak
    except Exception:
        return None

def fetch_longterm(sym, cfg):
    t = yf.Ticker(sym)
    daily = t.history(period="6mo", auto_adjust=True)
    if daily is None or daily.empty:
        return None
    close = daily["Close"].dropna()
    price, prev, pct = day_move(close)
    info = {}
    try:
        info = t.info or {}
    except Exception:
        pass
    dy = info.get("dividendYield")
    if dy is not None and dy <= 1.5:
        dy = dy * 100
    rate = info.get("dividendRate") or info.get("trailingAnnualDividendRate")
    if not dy and rate and price:
        dy = (rate / price) * 100
    freq = None
    try:
        recent = t.dividends.tail(14)
        if recent is not None and not recent.empty:
            yr_ago = now_ct().replace(tzinfo=None) - dt.timedelta(days=365)
            cnt = int((recent.index.tz_localize(None) >= yr_ago).sum())
            freq = FREQ.get(cnt)
    except Exception:
        pass
    ex = info.get("exDividendDate")
    ex_str = None
    if ex:
        try:
            ex_str = dt.datetime.utcfromtimestamp(int(ex)).strftime("%Y-%m-%d")
        except Exception:
            ex_str = str(ex)[:10]
    payout = info.get("payoutRatio")
    typ = cfg.get("type", "stock")
    return {
        "sym": sym, "type": typ, "price": round(price, 2), "pct": round(pct, 1),
        "yield": round(dy, 2) if dy else None,
        "annual_div": round(rate, 2) if rate else None,
        "frequency": freq or cfg.get("frequency"),
        "ex_div": ex_str,
        "payout_ratio": round(payout, 3) if payout else None,
        "growth_streak": div_growth_streak(t),
        "news": (news_titles(t, 1) or [""])[0],
    }


PERSONA = (
    "You are a sharp, plain-spoken senior portfolio manager writing Ved's {kind}. "
    "He runs two books: an ACTIVE trading watchlist (tech/space/semis) and a LONG-TERM INCOME "
    "sleeve (SCHD, O, PEP). No hype, no long disclaimers, no price predictions -- use scenarios "
    "with concrete levels (support/resistance, his buy zones, 200-week MA, earnings dates) for "
    "the active book, and yield/coverage/ex-div context for the income sleeve. Explain WHY things "
    "moved and connect macro (rates, CPI, Fed) to both books. Under 230 words, punchy, "
    "phone-readable. End with a line starting with the exact token WATCH: then 2-3 short "
    "catalysts/levels for next session."
)

def analyst_take(kind, context):
    if not ANTHROPIC_KEY:
        return None
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-sonnet-4-6", "max_tokens": 900,
                  "system": PERSONA.format(kind=kind),
                  "messages": [{"role": "user", "content": context}]},
            timeout=90,
        )
        r.raise_for_status()
        parts = r.json().get("content", [])
        txt = "\n".join(p.get("text", "") for p in parts if p.get("type") == "text").strip()
        return txt or None
    except Exception as e:
        print(f"[analyst] fallback, no take: {e}")
        return None

def split_take(text):
    if not text:
        return None
    watch = ""
    body = text
    for marker in ["WATCH:", "Watch:"]:
        if marker in text:
            body, watch = text.split(marker, 1)
            break
    paras = [p.strip() for p in body.strip().split("\n") if p.strip()]
    return {"body": paras, "watch": watch.strip()}


def push(title, body, priority="default", tags="chart_with_upwards_trend"):
    print(f"\n=== {title} ===\n{body}\n")
    if not NTFY_TOPIC:
        print("[ntfy] NTFY_TOPIC not set -- printed only.")
        return
    try:
        requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=body[:3800].encode("utf-8"),
                      headers={"Title": title, "Priority": priority, "Tags": tags}, timeout=20)
    except Exception as e:
        print(f"[ntfy] push failed: {e}")


def crossed_down(prev, cur, level):
    return prev is not None and level is not None and prev > level >= cur

def crossed_up(prev, cur, level):
    return prev is not None and level is not None and prev < level <= cur

def run_rules(cfg, d, rules_cfg):
    out = []
    sym, p, prev = d["sym"], d["price"], d["prev"]
    lo, hi = (list(cfg.get("buy_zone") or [None, None]) + [None, None])[:2]
    tgt = cfg.get("target")
    if hi and crossed_down(prev, p, hi):
        out.append((f"{sym}:zone", f"[BUY] {sym} entered your buy zone",
                    f"{sym} at ${fmt(p)} -- inside your ${fmt(lo)}-${fmt(hi)} zone.", "high"))
    if tgt and crossed_up(prev, p, tgt):
        out.append((f"{sym}:target", f"[TGT] {sym} hit your target",
                    f"{sym} at ${fmt(p)} -- through your ${fmt(tgt)} target.", "high"))
    big = rules_cfg.get("big_move_pct", 4.0)
    if abs(d["pct"]) >= big:
        arrow = "UP" if d["pct"] > 0 else "DOWN"
        out.append((f"{sym}:bigmove", f"[{arrow}] {sym} {d['pct']:+.1f}% today",
                    f"{sym} ${fmt(p)} ({d['pct']:+.1f}%). {d.get('news') or 'no headline'}", "high"))
    ma = d.get("ma200w_val")
    if ma and (crossed_down(prev, p, ma) or crossed_up(prev, p, ma)):
        side = "reclaimed" if p >= ma else "lost"
        out.append((f"{sym}:ma200w", f"[MA] {sym} {side} its 200-week MA",
                    f"{sym} ${fmt(p)} vs 200W MA ${fmt(ma)}.", "default"))
    return out


def fetch_bucket(names, fn):
    data, dead = {}, []
    for s in names:
        try:
            row = fn(s)
        except Exception as e:
            print(f"[fetch] {s} failed: {e}")
            row = None
        if row:
            data[s] = row
        else:
            dead.append(s)
    return data, dead


def do_publish():
    wl = load_json(WATCHLIST_FILE, {})
    active_cfg = wl.get("active", {})
    long_cfg = wl.get("longterm", {})
    pulse_syms = wl.get("market_pulse", ["SPY", "QQQ"])

    active, dead_a = fetch_bucket(list(active_cfg), fetch_active)
    longt, dead_l = fetch_bucket(list(long_cfg), lambda s: fetch_longterm(s, long_cfg.get(s, {})))
    pulse_data, _ = fetch_bucket(pulse_syms, fetch_active)

    pulse = [{"sym": s, "pct": round(pulse_data[s]["pct"], 1)} for s in pulse_syms if s in pulse_data]
    avg = sum(p["pct"] for p in pulse) / len(pulse) if pulse else 0
    regime = "risk-off" if avg < -0.3 else ("risk-on" if avg > 0.3 else "mixed")

    active_rows = []
    for sym, cfg in active_cfg.items():
        if sym not in active:
            continue
        d = active[sym]
        active_rows.append({
            "sym": sym, "price": round(d["price"], 2), "pct": round(d["pct"], 1),
            "ma200": d["ma200"], "er": d["er"],
            "zone": cfg.get("buy_zone"), "target": cfg.get("target"),
            "lo52": round(d["lo52"], 2), "hi52": round(d["hi52"], 2),
            "news": d["news"],
        })

    long_rows = [longt[s] for s in long_cfg if s in longt]

    ctx = build_analyst_context(pulse, regime, active_rows, long_rows)
    take = split_take(analyst_take("evening wrap" if now_ct().hour >= 15 else "morning brief", ctx))
    if not take:
        take = {"body": ["Live data below. Add ANTHROPIC_API_KEY in repo secrets to turn on the "
                         "analyst voice that reads the tape and connects the dots."], "watch": ""}

    payload = {
        "generated": now_ct().strftime("%a %b %d, %Y - %I:%M %p CT"),
        "pulse": pulse, "regime": regime, "take": take,
        "active": active_rows, "longterm": long_rows,
        "dead": dead_a + dead_l,
    }
    save_json(DATA_OUT, payload)
    print(f"[publish] wrote {DATA_OUT} -- {len(active_rows)} active, {len(long_rows)} long-term.")
    if payload["dead"]:
        print(f"[publish] no data for: {', '.join(payload['dead'])}")

def build_analyst_context(pulse, regime, active_rows, long_rows):
    lines = ["Market: " + ", ".join(f"{p['sym']} {p['pct']:+.1f}%" for p in pulse) + f" -- regime {regime}.",
             "", "ACTIVE BOOK:"]
    for r in active_rows:
        z = f" zone {r['zone']}" if r.get("zone") else ""
        t = f" tgt {r['target']}" if r.get("target") else ""
        ma = f" 200W {r['ma200']:+d}%" if r["ma200"] is not None else ""
        er = f" ER {r['er']}" if r["er"] else ""
        lines.append(f"  {r['sym']} ${r['price']} ({r['pct']:+.1f}%){ma}{z}{t}{er} -- {r['news']}")
    lines += ["", "INCOME SLEEVE:"]
    for r in long_rows:
        y = f"{r['yield']}% yield" if r.get("yield") else ""
        lines.append(f"  {r['sym']} ${r['price']} ({r['pct']:+.1f}%) {y}, {r.get('frequency','')} -- {r['news']}")
    return "\n".join(lines)


def do_report(kind):
    wl = load_json(WATCHLIST_FILE, {})
    active_cfg = wl.get("active", {})
    active, dead = fetch_bucket(list(active_cfg), fetch_active)
    ts = now_ct().strftime("%a %b %d, %I:%M %p CT")
    head = "MORNING BRIEF" if kind == "morning brief" else "EVENING WRAP"
    lines = [f"{head} -- {ts}", ""]
    for sym, cfg in active_cfg.items():
        if sym not in active:
            continue
        d = active[sym]
        bits = [f"{sym} ${fmt(d['price'])} ({d['pct']:+.1f}%)"]
        if d["ma200"] is not None:
            bits.append(f"200W {d['ma200']:+d}%")
        if d["er"]:
            bits.append(f"ER {d['er']}")
        lines.append(" | ".join(bits))
    push("Morning Brief" if kind == "morning brief" else "Evening Wrap", "\n".join(lines))

def do_scan():
    wl = load_json(WATCHLIST_FILE, {})
    active_cfg = wl.get("active", {})
    state = load_json(STATE_FILE, {})
    today = now_ct().strftime("%Y-%m-%d")
    active, _ = fetch_bucket(list(active_cfg), fetch_active)
    fired = 0
    for sym, cfg in active_cfg.items():
        if sym not in active:
            continue
        for key, title, msg, prio in run_rules(cfg, active[sym], wl.get("alert_rules", {})):
            skey = f"{today}:{key}"
            if state.get(skey):
                continue
            push(title, msg, priority=prio, tags="rotating_light")
            state[skey] = True
            fired += 1
    save_json(STATE_FILE, {k: v for k, v in state.items() if k.startswith(today)})
    print(f"[scan] {fired} alert(s) fired.")


def main():
    ap = argparse.ArgumentParser(description="Market Engine v2")
    ap.add_argument("--mode", choices=["publish", "brief", "scan", "wrap"], required=True)
    m = ap.parse_args().mode
    if m == "publish":
        do_publish()
    elif m == "brief":
        do_report("morning brief")
    elif m == "wrap":
        do_report("evening wrap")
    else:
        do_scan()


if __name__ == "__main__":
    main()
