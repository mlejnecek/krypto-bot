#!/usr/bin/env python3
"""
Backtest momentum strategie z bot.py na historických hodinových datech Binance.

Porovná několik variant pravidel s prostým držením BTC a výsledek zapíše
do backtest/report.md (+ backtest/results.csv). Vše v USDT.

Použití:  python3 backtest.py [počet_dní]      (výchozí 180)
"""
import csv
import os
import sys
import time

from bot import CFG, HOUR_MS, get

DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("DAYS", 180))
OUT = "backtest"

# Pevný seznam zavedených likvidních mincí (aby výběr nebyl ovlivněn tím,
# co je zrovna dnes "hot"). Chybějící/delistované páry se přeskočí.
COINS = """BTC ETH BNB SOL XRP DOGE ADA TRX AVAX LINK DOT LTC BCH NEAR UNI APT ARB
OP SUI SEI INJ TIA FET RENDER ATOM FIL ETC HBAR ICP AAVE PEPE SHIB WIF BONK
TON ENA WLD TAO JUP PENDLE ONDO STX IMX GALA SAND XLM ALGO CRV LDO""".split()

VARIANTS = {
    "A zakladni (soucasny bot)": {},
    "B + filtr trendu BTC": {"btc_filter": True},
    "C + zavreni u maxima": {"near_high": True},
    "D + posuvny stop": {"trailing": True},
    "B+C": {"btc_filter": True, "near_high": True},
    "B+C+D": {"btc_filter": True, "near_high": True, "trailing": True},
}
BTC_SMA = 50          # BTC nad 50h průměrem = trh není v sestupném trendu
NEAR_HIGH = 0.7       # zavření v horních 30 % rozpětí svíčky
TRAIL = 0.04          # posuvný stop 4 % pod maximem
TRAIL_MAX_HOLD = 48   # s posuvným stopem necháme pozici běžet až 48 h


# ---------------------------------------------------------------- data
def fetch(sym, start, end):
    out, t = [], start
    while t < end:
        kl = get("/api/v3/klines", {"symbol": sym, "interval": "1h",
                                    "startTime": t, "limit": 1000})
        if not kl:
            break
        out += kl
        t = kl[-1][0] + HOUR_MS
        if len(kl) < 1000:
            break
    return [k for k in out if k[6] < end]


def load_data():
    end = int(time.time() * 1000) // HOUR_MS * HOUR_MS
    start = end - DAYS * 24 * HOUR_MS - 60 * HOUR_MS
    raw = {}
    for c in COINS:
        sym = c + "USDT"
        try:
            kl = fetch(sym, start, end)
        except RuntimeError as e:
            print(f"  přeskakuji {sym}: {e}")
            continue
        if len(kl) > 100:
            raw[sym] = {k[0]: (float(k[1]), float(k[2]), float(k[3]),
                               float(k[4]), float(k[7])) for k in kl}
        print(f"  {sym}: {len(kl)} svíček")
    T = sorted(raw["BTCUSDT"])
    data = {s: [d.get(t) for t in T] for s, d in raw.items()}
    return T, data


def features(T, data):
    """Pro každou minci a hodinu: objem 24h, poměr objemu, pohyb, pozice zavření."""
    feat = {}
    for s, rows in data.items():
        n = len(rows)
        vol24, ratio = [None] * n, [None] * n
        for i in range(24, n):
            win = rows[i - 24:i]
            if rows[i] is None or any(r is None for r in win):
                continue
            avg = sum(r[4] for r in win) / 24
            vol24[i] = sum(r[4] for r in win[1:]) + rows[i][4]
            ratio[i] = rows[i][4] / avg if avg > 0 else None
        feat[s] = (vol24, ratio)
    btc = [r[3] if r else None for r in data["BTCUSDT"]]
    btc_ok = [False] * len(T)
    for i in range(BTC_SMA, len(T)):
        w = btc[i - BTC_SMA + 1:i + 1]
        if None not in w:
            btc_ok[i] = btc[i] > sum(w) / BTC_SMA
    return feat, btc_ok


# ---------------------------------------------------------------- simulace
def simulate(T, data, feat, btc_ok, v):
    fee, slip = CFG["fee"], CFG["slippage"]
    max_hold = TRAIL_MAX_HOLD if v.get("trailing") else CFG["max_hold_hours"]
    cash, pos, eq_curve, trades, fees = CFG["start_capital"], {}, [], [], 0.0
    last_px = {}
    start_i = max(BTC_SMA, 25)

    for i in range(start_i, len(T)):
        for s, rows in data.items():
            if rows[i]:
                last_px[s] = rows[i][3]

        # --- výstupy (svíčka i, pozice otevřené před ní)
        for s in list(pos):
            p, r = pos[s], data[s][i]
            if r is None:
                continue
            o, h, l, c, _ = r
            stop = p["entry"] * (1 - CFG["stop_loss"])
            if v.get("trailing"):
                stop = max(stop, p["peak"] * (1 - TRAIL))
            px = None
            if l <= stop:
                px = min(o, stop) * (1 - slip)
            elif not v.get("trailing") and h >= p["entry"] * (1 + CFG["take_profit"]):
                px = p["entry"] * (1 + CFG["take_profit"]) * (1 - slip)
            else:
                p["peak"] = max(p["peak"], h)
                if i - p["i"] >= max_hold:
                    px = c * (1 - slip)
            if px:
                gross = p["qty"] * px
                fees += gross * fee
                pnl = gross * (1 - fee) - p["cost"]
                cash += gross * (1 - fee)
                trades.append(pnl / p["cost"])
                del pos[s]

        # --- vstupy podle uzavřené svíčky i
        equity = cash + sum(p["qty"] * last_px.get(s, p["entry"]) for s, p in pos.items())
        if len(pos) < CFG["max_positions"] and (not v.get("btc_filter") or btc_ok[i]):
            uni = sorted(((feat[s][0][i], s) for s in data
                          if feat[s][0][i] and feat[s][0][i] >= CFG["min_quote_volume_24h"]),
                         reverse=True)[:CFG["universe_size"]]
            cands = []
            for _, s in uni:
                if s in pos or feat[s][1][i] is None:
                    continue
                o, h, l, c, _ = data[s][i]
                move = c / o - 1
                if feat[s][1][i] < CFG["vol_ratio_min"]:
                    continue
                if not CFG["min_candle_move"] <= move <= CFG["max_candle_move"]:
                    continue
                if v.get("near_high") and h > l and (c - l) / (h - l) < NEAR_HIGH:
                    continue
                cands.append((feat[s][1][i] * move, s, c))
            for _, s, c in sorted(cands, reverse=True):
                if len(pos) >= CFG["max_positions"]:
                    break
                size = min(cash, equity / CFG["max_positions"])
                if size < 10:
                    break
                px = c * (1 + slip)
                fees += size * fee
                pos[s] = {"entry": px, "qty": size * (1 - fee) / px, "cost": size,
                          "i": i, "peak": px}
                cash -= size

        eq_curve.append(cash + sum(p["qty"] * last_px.get(s, p["entry"])
                                   for s, p in pos.items()))
    return eq_curve, trades, fees, start_i


def stats(curve):
    peak, mdd = curve[0], 0.0
    for e in curve:
        peak = max(peak, e)
        mdd = max(mdd, 1 - e / peak)
    return curve[-1] / curve[0] - 1, mdd


def thirds(curve):
    n = len(curve) // 3
    parts = [curve[0:n + 1], curve[n:2 * n + 1], curve[2 * n:]]
    return [p[-1] / p[0] - 1 for p in parts]


def pct(x):
    return f"{x * 100:+.1f} %"


def main():
    os.makedirs(OUT, exist_ok=True)
    print(f"Stahuji data za {DAYS} dní…")
    T, data = load_data()
    feat, btc_ok = features(T, data)

    rows = []
    btc_curve = None
    for name, v in VARIANTS.items():
        curve, trades, fees, si = simulate(T, data, feat, btc_ok, v)
        if btc_curve is None:
            btc_curve = [data["BTCUSDT"][i][3] for i in range(si, len(T))]
        ret, mdd = stats(curve)
        wins = sum(t > 0 for t in trades)
        rows.append([name, pct(ret), pct(-mdd), len(trades),
                     f"{wins / len(trades) * 100:.0f} %" if trades else "-",
                     pct(sum(trades) / len(trades)) if trades else "-",
                     f"{fees:.0f}", *[pct(x) for x in thirds(curve)]])
        print(f"  {name}: {pct(ret)}, obchodů {len(trades)}")

    b_ret, b_mdd = stats(btc_curve)
    rows.append(["BTC koupit a drzet", pct(b_ret), pct(-b_mdd), 1, "-", "-", "1",
                 *[pct(x) for x in thirds(btc_curve)]])

    hdr = ["Varianta", "Vysledek", "Max. propad", "Obchodu", "Ziskovych",
           "Prum. obchod", "Poplatky USDT", "1. tretina", "2. tretina", "3. tretina"]
    with open(os.path.join(OUT, "results.csv"), "w", newline="") as f:
        csv.writer(f).writerows([hdr] + rows)

    def d(ms):
        return time.strftime("%Y-%m-%d", time.gmtime(ms / 1000))

    md = [f"# Backtest {d(T[BTC_SMA])} až {d(T[-1])}", "",
          f"Start 1 000 USDT, {len(data)} mincí, poplatek {CFG['fee']*100:.1f} %, "
          f"skluz {CFG['slippage']*100:.2f} %.", "",
          "| " + " | ".join(hdr) + " |", "|" + "---|" * len(hdr)]
    md += ["| " + " | ".join(str(x) for x in r) + " |" for r in rows]
    md += ["", "Třetiny = výsledek v první, druhé a třetí třetině období "
           "(ukazuje, jestli strategie funguje stabilně, nebo jen v jednom úseku)."]
    with open(os.path.join(OUT, "report.md"), "w") as f:
        f.write("\n".join(md) + "\n")
    print("\n".join(md))


if __name__ == "__main__":
    main()
