#!/usr/bin/env python3
"""
Test robustnosti trendové strategie "držet, když je cena nad N-denním průměrem".

Testuje N = 20 … 200 dní na BTC a ETH od roku 2017/2018, plus kombinaci
50 % BTC + 50 % ETH. Výsledek po letech ukáže, jestli strategie funguje
stabilně, nebo jen díky jednomu šťastnému období / číslu.

Výstup: backtest/report_robust.md
"""
import os
import time

from bot import CFG, get

DAY_MS = 86_400_000
OUT = "backtest"
SMAS = [20, 30, 50, 75, 100, 150, 200]
ASSETS = ["BTCUSDT", "ETHUSDT"]
FEE, SLIP = CFG["fee"], CFG["slippage"]


def fetch(sym):
    end = int(time.time() * 1000) // DAY_MS * DAY_MS
    t, out = 1_500_000_000_000, []          # červenec 2017
    while t < end:
        kl = get("/api/v3/klines", {"symbol": sym, "interval": "1d", "startTime": t, "limit": 1000})
        if not kl:
            break
        out += kl
        t = kl[-1][0] + DAY_MS
        if len(kl) < 1000:
            break
    return {k[0]: float(k[4]) for k in out if k[6] < end}


def trend(closes, n, start):
    """Equity křivka (start = 1.0) a počet přepnutí."""
    cash, qty, curve, sw = 1.0, 0.0, [], 0
    for i in range(start, len(closes)):
        c = closes[i]
        above = c > sum(closes[i - n + 1:i + 1]) / n
        if above and qty == 0:
            qty = cash * (1 - FEE) / (c * (1 + SLIP)); cash = 0; sw += 1
        elif not above and qty > 0:
            cash = qty * c * (1 - SLIP) * (1 - FEE); qty = 0; sw += 1
        curve.append(cash + qty * c)
    return curve, sw


def hold(closes, start):
    return [c / closes[start] for c in closes[start:]]


def mdd(curve):
    peak, m = curve[0], 0.0
    for e in curve:
        peak = max(peak, e); m = max(m, 1 - e / peak)
    return m


def yearly(curve, days):
    res, prev = {}, curve[0]
    for i, t in enumerate(days):
        y = time.gmtime(t / 1000).tm_year
        nxt = days[i + 1] if i + 1 < len(days) else None
        if nxt is None or time.gmtime(nxt / 1000).tm_year != y:
            res[y] = curve[i] / prev - 1
            prev = curve[i]
    return res


def pct(x):
    return f"{x * 100:+.0f} %"


def main():
    os.makedirs(OUT, exist_ok=True)
    raw = {a: fetch(a) for a in ASSETS}
    days = sorted(set(raw["BTCUSDT"]) & set(raw["ETHUSDT"]))
    closes = {a: [raw[a][d] for d in days] for a in ASSETS}
    start = max(SMAS) - 1
    dd = days[start:]
    years = (dd[-1] - dd[0]) / DAY_MS / 365.25

    rows, curves = [], {}
    for a in ASSETS:
        name = a[:-4]
        h = hold(closes[a], start)
        curves[f"{name} drzet"] = (h, 1)
        for n in SMAS:
            c, sw = trend(closes[a], n, start)
            curves[f"{name} nad {n}d"] = (c, sw)
    for n in SMAS:
        b, sb = curves[f"BTC nad {n}d"]
        e, se = curves[f"ETH nad {n}d"]
        curves[f"50/50 nad {n}d"] = ([(x + y) / 2 for x, y in zip(b, e)], sb + se)
    hb, he = curves["BTC drzet"][0], curves["ETH drzet"][0]
    curves["50/50 drzet"] = ([(x + y) / 2 for x, y in zip(hb, he)], 2)

    yrs = sorted(yearly(hb, dd))
    for name, (c, sw) in curves.items():
        cagr = c[-1] ** (1 / years) - 1
        y = yearly(c, dd)
        rows.append([name, f"{c[-1]:.1f}×", pct(cagr), pct(-mdd(c)), sw] + [pct(y[k]) for k in yrs])
        print(name, f"{c[-1]:.2f}x", pct(-mdd(c)))

    d = lambda ms: time.strftime("%Y-%m-%d", time.gmtime(ms / 1000))
    hdr = ["Varianta", "Nasobek", "Rocne", "Max. propad", "Prepnuti"] + [str(y) for y in yrs]
    md = [f"# Robustnost trendové strategie {d(dd[0])} až {d(dd[-1])}", "",
          f"Poplatek {FEE*100:.1f} % + skluz {SLIP*100:.2f} % při každém přepnutí. "
          "„Nasobek“ = kolikrát se zvětšil vklad, „Rocne“ = průměrný roční výnos.", "",
          "| " + " | ".join(hdr) + " |", "|" + "---|" * len(hdr)]
    md += ["| " + " | ".join(str(x) for x in r) + " |" for r in rows]
    with open(os.path.join(OUT, "report_robust.md"), "w") as f:
        f.write("\n".join(md) + "\n")
    print("\n".join(md))


if __name__ == "__main__":
    main()
