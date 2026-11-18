#!/usr/bin/env python3
"""
Test filtrů proti falešným signálům pro trendovou kombinaci
BTC + ETH × průměr 50 / 100 / 150 dní (stejná jako živý bot).

Varianty:
  - originál (jako živý bot): nákup/prodej hned po zavření nad/pod průměrem
  - potvrzení N dní: nákup až po N zavřeních nad průměrem po sobě, prodej po N pod ním
  - potvrzení jen nákupu: nákup po 2 dnech nad, prodej hned (ochrana zůstává rychlá)
  - pásmo X %: nákup až nad průměrem + X %, prodej až pod průměrem − X %

Nic v botovi nemění. Výstup: backtest/report_potvrzeni.md
"""
import os
import time

from backtest_robust import DAY_MS, FEE, SLIP, fetch, mdd, pct, yearly

OUT = "backtest"
COINS = ["BTCUSDT", "ETHUSDT"]
SMAS = [50, 100, 150]

VARIANTS = {
    "Original (zivy bot)":          dict(cin=1, cout=1, band=0.0),
    "Potvrzeni 2 dny":              dict(cin=2, cout=2, band=0.0),
    "Potvrzeni 3 dny":              dict(cin=3, cout=3, band=0.0),
    "Potvrzeni jen nakupu 2 dny":   dict(cin=2, cout=1, band=0.0),
    "Pasmo 1 %":                    dict(cin=1, cout=1, band=0.01),
    "Pasmo 2 %":                    dict(cin=1, cout=1, band=0.02),
}


def sleeve(closes, n, start, cin, cout, band):
    """Jeden díl: equity křivka (start 1.0), počet přepnutí, výsledky obchodů."""
    cash, qty, curve, sw, trades = 1.0, 0.0, [], 0, []
    up = down = 0
    cost = 0.0
    for i in range(start, len(closes)):
        c = closes[i]
        sma = sum(closes[i - n + 1:i + 1]) / n
        up = up + 1 if c > sma * (1 + band) else 0
        down = down + 1 if c <= sma * (1 - band) else 0
        if qty == 0 and up >= cin:
            cost = cash
            qty = cash * (1 - FEE) / (c * (1 + SLIP)); cash = 0; sw += 1
        elif qty > 0 and down >= cout:
            cash = qty * c * (1 - SLIP) * (1 - FEE); qty = 0; sw += 1
            trades.append(cash / cost - 1)
        curve.append(cash + qty * c)
    return curve, sw, trades


def main():
    os.makedirs(OUT, exist_ok=True)
    raw = {a: fetch(a) for a in COINS}
    days = sorted(set(raw["BTCUSDT"]) & set(raw["ETHUSDT"]))
    closes = {a: [raw[a][d] for d in days] for a in COINS}
    start = max(SMAS) + 3
    dd = days[start:]
    years = (dd[-1] - dd[0]) / DAY_MS / 365.25

    curves = {}
    for name, v in VARIANTS.items():
        parts, sw, tr = [], 0, []
        for a in COINS:
            for n in SMAS:
                c, s, t = sleeve(closes[a], n, start, **v)
                parts.append(c); sw += s; tr += t
        curves[name] = ([sum(p[i] for p in parts) / 6 for i in range(len(parts[0]))], sw, tr)
    hold = [sum(closes[a][i] / closes[a][start] for a in COINS) / 2 for i in range(start, len(days))]
    curves["Drzet 50/50 BTC+ETH"] = (hold, 2, [])

    yrs = sorted(yearly(hold, dd))
    hdr = ["Varianta", "Nasobek", "Rocne", "Max. propad", "Prepnuti", "Obchodu",
           "Ztratovych", "Prum. ztrata"] + [str(y) for y in yrs]
    rows = []
    for name, (c, sw, tr) in curves.items():
        losers = [t for t in tr if t <= 0]
        y = yearly(c, dd)
        rows.append([name, f"{c[-1]:.1f}×", pct(c[-1] ** (1 / years) - 1), pct(-mdd(c)), sw,
                     len(tr) or "-",
                     f"{len(losers) / len(tr) * 100:.0f} %" if tr else "-",
                     pct(sum(losers) / len(losers)) if losers else "-"]
                    + [pct(y[k]) for k in yrs])
        print(name, f"{c[-1]:.2f}x", pct(-mdd(c)), sw)

    d = lambda ms: time.strftime("%Y-%m-%d", time.gmtime(ms / 1000))
    md = [f"# Filtry proti falešným signálům {d(dd[0])} až {d(dd[-1])}", "",
          "Kombinace BTC + ETH × průměr 50/100/150 dní (6 dílů, jako živý bot). "
          f"Poplatek {FEE*100:.1f} % + skluz {SLIP*100:.2f} % při každém přepnutí.", "",
          "„Obchodu“ = uzavřené nákupy+prodeje všech 6 dílů, „Ztratovych“ = kolik z nich skončilo "
          "ztrátou (falešné signály), „Prum. ztrata“ = průměrná ztráta takového obchodu.", "",
          "| " + " | ".join(hdr) + " |", "|" + "---|" * len(hdr)]
    md += ["| " + " | ".join(str(x) for x in r) + " |" for r in rows]
    with open(os.path.join(OUT, "report_potvrzeni.md"), "w") as f:
        f.write("\n".join(md) + "\n")
    print("\n".join(md))


if __name__ == "__main__":
    main()
