#!/usr/bin/env python3
"""
Backtest na DENNÍCH svíčkách: nakoupit jen po silné (dlouhé) zelené svíčce.

Signál (den i):
  - svíčka vyrostla aspoň o PRAH % (5 / 8 / 12 %),
  - objem aspoň 2× průměr předchozích 20 dní,
  - zavřela v horních 30 % svého rozpětí (žádný dlouhý horní knot),
  - volitelně: BTC je nad 50denním průměrem (trh roste).
Nákup na zavření dne i, stop-loss na minimu signální svíčky,
prodej po DRZET dnech (1 / 3 / 7).

Navíc změří "jistotu": jak často po signálu další den cena opravdu vzrostla.
A pro srovnání: BTC držet, a BTC jen nad 50denním průměrem (jinak USDT).

Použití:  python3 backtest_denni.py [počet_dní]     (výchozí 1095 = 3 roky)
"""
import csv
import os
import sys
import time

from bot import CFG, get
from backtest import COINS

DAY_MS = 86_400_000
DAYS = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1] else 1095
OUT = "backtest"
VOL_RATIO, VOL_WIN, NEAR_HIGH, SMA = 2.0, 20, 0.7, 50
PRAHY, DRZET = [0.05, 0.08, 0.12], [1, 3, 7]
MAX_POS = 3


def fetch(sym, start, end):
    out, t = [], start
    while t < end:
        kl = get("/api/v3/klines", {"symbol": sym, "interval": "1d",
                                    "startTime": t, "limit": 1000})
        if not kl:
            break
        out += kl
        t = kl[-1][0] + DAY_MS
        if len(kl) < 1000:
            break
    return [k for k in out if k[6] < end]


def load():
    end = int(time.time() * 1000) // DAY_MS * DAY_MS
    start = end - (DAYS + SMA + VOL_WIN) * DAY_MS
    raw = {}
    for c in COINS:
        s = c + "USDT"
        try:
            kl = fetch(s, start, end)
        except RuntimeError as e:
            print(f"  přeskakuji {s}: {e}")
            continue
        if len(kl) > VOL_WIN + 5:
            raw[s] = {k[0]: tuple(float(x) for x in (k[1], k[2], k[3], k[4], k[7])) for k in kl}
        print(f"  {s}: {len(kl)} dní")
    T = sorted(raw["BTCUSDT"])
    return T, {s: [d.get(t) for t in T] for s, d in raw.items()}


def signals(T, data, prah):
    """{i: [(síla, symbol, close, low)]} – signály na zavření dne i."""
    sig = {}
    for s, rows in data.items():
        for i in range(VOL_WIN, len(T)):
            r, win = rows[i], rows[i - VOL_WIN:i]
            if r is None or None in win:
                continue
            o, h, l, c, qv = r
            avg = sum(w[4] for w in win) / VOL_WIN
            move = c / o - 1
            if (avg > 0 and qv / avg >= VOL_RATIO and move >= prah
                    and h > l and (c - l) / (h - l) >= NEAR_HIGH):
                sig.setdefault(i, []).append((move * qv / avg, s, c, l))
    return sig


def btc_trend(T, data):
    btc = [r[3] if r else None for r in data["BTCUSDT"]]
    ok = [False] * len(T)
    for i in range(SMA, len(T)):
        w = btc[i - SMA + 1:i + 1]
        if None not in w:
            ok[i] = btc[i] > sum(w) / SMA
    return ok


def simulate(T, data, sig, hold, ok, use_filter, start_i):
    fee, slip = CFG["fee"], CFG["slippage"]
    cash, pos, curve, trades, fees, last = CFG["start_capital"], {}, [], [], 0.0, {}
    for i in range(start_i, len(T)):
        for s, rows in data.items():
            if rows[i]:
                last[s] = rows[i][3]
        for s in list(pos):
            p, r = pos[s], data[s][i]
            if r is None:
                continue
            o, h, l, c, _ = r
            px = None
            if l <= p["stop"]:
                px = min(o, p["stop"]) * (1 - slip)
            elif i - p["i"] >= hold:
                px = c * (1 - slip)
            if px:
                g = p["qty"] * px
                fees += g * fee
                cash += g * (1 - fee)
                trades.append(g * (1 - fee) / p["cost"] - 1)
                del pos[s]
        eq = cash + sum(p["qty"] * last.get(s, p["entry"]) for s, p in pos.items())
        if (not use_filter or ok[i]) and i in sig:
            for _, s, c, l in sorted(sig[i], reverse=True):
                if len(pos) >= MAX_POS or s in pos:
                    continue
                size = min(cash, eq / MAX_POS)
                if size < 10:
                    break
                px = c * (1 + slip)
                fees += size * fee
                pos[s] = {"entry": px, "qty": size * (1 - fee) / px, "cost": size,
                          "i": i, "stop": l}
                cash -= size
        curve.append(cash + sum(p["qty"] * last.get(s, p["entry"]) for s, p in pos.items()))
    return curve, trades, fees


def btc_sma_strategy(T, data, ok, start_i):
    fee, slip = CFG["fee"], CFG["slippage"]
    cash, qty, curve, switches = CFG["start_capital"], 0.0, [], 0
    for i in range(start_i, len(T)):
        c = data["BTCUSDT"][i][3]
        if ok[i] and qty == 0:
            qty = cash * (1 - fee) / (c * (1 + slip)); cash = 0; switches += 1
        elif not ok[i] and qty > 0:
            cash = qty * c * (1 - slip) * (1 - fee); qty = 0; switches += 1
        curve.append(cash + qty * c)
    return curve, switches


def stats(curve):
    peak, mdd = curve[0], 0.0
    for e in curve:
        peak = max(peak, e); mdd = max(mdd, 1 - e / peak)
    n = len(curve) // 3
    th = [p[-1] / p[0] - 1 for p in (curve[:n + 1], curve[n:2 * n + 1], curve[2 * n:])]
    return curve[-1] / curve[0] - 1, mdd, th


def pct(x):
    return f"{x * 100:+.1f} %"


def main():
    os.makedirs(OUT, exist_ok=True)
    print(f"Stahuji denní data za {DAYS} dní…")
    T, data = load()
    ok = btc_trend(T, data)
    start_i = len(T) - DAYS if len(T) > DAYS else SMA

    # --- "jistota": co se stalo DALŠÍ den po signálu
    jist = []
    for prah in PRAHY:
        sig = signals(T, data, prah)
        nxt = [data[s][i + 1][3] / c - 1 for i, lst in sig.items() if i >= start_i
               for _, s, c, _ in lst if i + 1 < len(T) and data[s][i + 1]]
        if nxt:
            jist.append([f"{prah*100:.0f} %", len(nxt),
                         f"{sum(x > 0 for x in nxt) / len(nxt) * 100:.0f} %",
                         pct(sum(nxt) / len(nxt))])

    rows = []
    for prah in PRAHY:
        sig = signals(T, data, prah)
        for hold in DRZET:
            for flt in (False, True):
                curve, tr, fees = simulate(T, data, sig, hold, ok, flt, start_i)
                ret, mdd, th = stats(curve)
                name = f"svicka {prah*100:.0f} %, drzet {hold} d" + (", BTC trend" if flt else "")
                rows.append([name, pct(ret), pct(-mdd), len(tr),
                             f"{sum(t > 0 for t in tr) / len(tr) * 100:.0f} %" if tr else "-",
                             pct(sum(tr) / len(tr)) if tr else "-", f"{fees:.0f}",
                             *[pct(x) for x in th]])
                print(f"  {name}: {pct(ret)} ({len(tr)} obchodů)")

    btc_curve = [data["BTCUSDT"][i][3] for i in range(start_i, len(T))]
    ret, mdd, th = stats(btc_curve)
    rows.append(["BTC koupit a drzet", pct(ret), pct(-mdd), 1, "-", "-", "1", *[pct(x) for x in th]])
    c2, sw = btc_sma_strategy(T, data, ok, start_i)
    ret, mdd, th = stats(c2)
    rows.append(["BTC jen nad 50d prumerem", pct(ret), pct(-mdd), sw, "-", "-", "-", *[pct(x) for x in th]])

    hdr = ["Varianta", "Vysledek", "Max. propad", "Obchodu", "Ziskovych",
           "Prum. obchod", "Poplatky USDT", "1. tretina", "2. tretina", "3. tretina"]
    with open(os.path.join(OUT, "results_denni.csv"), "w", newline="") as f:
        csv.writer(f).writerows([hdr] + rows)

    d = lambda ms: time.strftime("%Y-%m-%d", time.gmtime(ms / 1000))
    md = [f"# Denní backtest {d(T[start_i])} až {d(T[-1])}", "",
          f"Start 1 000 USDT, {len(data)} mincí, max {MAX_POS} pozice, poplatek "
          f"{CFG['fee']*100:.1f} %, skluz {CFG['slippage']*100:.2f} %.", "",
          "## Jistota: co udělala cena DALŠÍ den po signálu", "",
          "| Min. svíčka | Signálů | Další den nahoru | Prům. pohyb další den |",
          "|---|---|---|---|"]
    md += ["| " + " | ".join(str(x) for x in r) + " |" for r in jist]
    md += ["", "## Varianty", "", "| " + " | ".join(hdr) + " |", "|" + "---|" * len(hdr)]
    md += ["| " + " | ".join(str(x) for x in r) + " |" for r in rows]
    with open(os.path.join(OUT, "report_denni.md"), "w") as f:
        f.write("\n".join(md) + "\n")
    print("\n".join(md))


if __name__ == "__main__":
    main()
