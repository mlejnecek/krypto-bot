#!/usr/bin/env python3
"""
Detailní analýza trendové strategie BTC + ETH × SMA 50 / 100 / 150 dní.

Testuje:
  1. Stabilitu hysterezního pásma (0 % až 3 %, včetně asymetrických variant)
  2. Vliv periodického rebalancování 6 dílů (žádné / ročně / čtvrtletně) + drift vah
  3. Vliv zpoždění exekuce po půlnoci (0h / +1h / +2h / +6h z reálných 1h dat)
  4. Stress test vyšších poplatků a skluzu (0,15 % / 0,30 % / 0,50 %)
  5. Roční obrat prodejů (násobek hodnoty účtu za rok)

Výstup: backtest/report_detail.md
"""
import concurrent.futures
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API = os.environ.get("BINANCE_API", "https://data-api.binance.vision")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest")
CACHE_DIR = os.environ.get("CACHE_DIR", "")
COINS = ["BTCUSDT", "ETHUSDT"]
SMAS = [50, 100, 150]
DAY_MS = 86_400_000
HOUR_MS = 3_600_000
FEE = 0.001
SLIP = 0.0005


def get(path, params=None, tries=4):
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "krypto-backtest/1.0"})
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"API selhalo: {url} -> {last}")


def fetch_klines(sym, interval, start_ms, end_ms):
    step = DAY_MS if interval == "1d" else HOUR_MS
    chunk = 1000 * step
    starts = list(range(start_ms, end_ms, chunk))

    def _one(t):
        return get("/api/v3/klines", {"symbol": sym, "interval": interval,
                                      "startTime": t, "limit": 1000})

    out = {}
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        for kl in ex.map(_one, starts):
            done += 1
            for k in kl:
                if k[6] < end_ms:
                    out[k[0]] = float(k[4])
            if interval == "1h" and (done % 20 == 0 or done == len(starts)):
                print(f"  {sym} {interval}: {done}/{len(starts)} dávek", flush=True)
    return out


def load_data():
    end_ms = int(time.time() * 1000) // DAY_MS * DAY_MS
    start_1d = 1_500_000_000_000  # červenec 2017

    cache_file = os.path.join(CACHE_DIR, "binance_btc_eth_cache.json") if CACHE_DIR else ""
    if cache_file and os.path.exists(cache_file):
        with open(cache_file) as f:
            cached = json.load(f)
        raw_1d = {a: {int(k): v for k, v in d.items()} for a, d in cached["1d"].items()}
        raw_1h = {a: {int(k): v for k, v in d.items()} for a, d in cached["1h"].items()}
        return raw_1d, raw_1h

    print("Stahuji denní data (1d) od 2017…", flush=True)
    raw_1d = {a: fetch_klines(a, "1d", start_1d, end_ms) for a in COINS}

    days = sorted(set(raw_1d["BTCUSDT"]) & set(raw_1d["ETHUSDT"]))
    start_1h = days[max(SMAS)]  # od začátku simulace (2018)
    print("Stahuji hodinová data (1h) pro test zpoždění exekuce…", flush=True)
    raw_1h = {a: fetch_klines(a, "1h", start_1h, end_ms) for a in COINS}

    if cache_file:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(cache_file, "w") as f:
            json.dump({"1d": raw_1d, "1h": raw_1h}, f)
    return raw_1d, raw_1h


def ym_key(ms):
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.year, (dt.month - 1) // 3


def simulate_portfolio(days, closes, start, band_in=0.0, band_out=0.0,
                       rebalance=None, lag_hours=0, raw_1h=None,
                       fee=FEE, slip=SLIP):
    """
    Simulace celého 6dílného portfolia den po dni (start = 1.0 celkem).
    Podporuje asymetrické pásmo, rebalancování ('yearly' / 'quarterly'),
    zpoždění exekuce o `lag_hours` hodin a volitelné poplatky/skluz.
    """
    sleeves = []
    for a in COINS:
        for n in SMAS:
            sleeves.append({
                "name": f"{a[:-4]}_{n}",
                "coin": a,
                "sma": n,
                "cash": 1.0 / 6.0,
                "qty": 0.0,
                "cost": 0.0,
            })

    curve = []
    in_market_frac = []
    trades = []
    switches = 0
    # Roční obrat prodejů: {rok: (suma_prodeju, soucet_dennich_equity, pocet_dni)}
    yr_sales = {}
    yr_eq_sum = {}
    yr_days = {}

    prev_year = None
    prev_q = None

    for i in range(start, len(days)):
        t = days[i]
        year, q = ym_key(t)

        # Periodické rebalancování na začátku roku / čtvrtletí (před dnešními signály)
        do_reb = False
        if rebalance == "yearly" and prev_year is not None and year != prev_year:
            do_reb = True
        elif rebalance == "quarterly" and prev_q is not None and (year, q) != prev_q:
            do_reb = True

        if do_reb:
            # Přesné účetní rebalancování: přebytky z krypto-dílů se prodají
            # s poplatkem+skluzem do společného poolu, hotovostní díly přispějí do poolu,
            # a z poolu se dorovnají podvážené krypto-díly i hotovostní díly.
            total_eq = sum(s["cash"] + s["qty"] * closes[s["coin"]][i] for s in sleeves)
            target = total_eq / 6.0
            pool = 0.0
            for s in sleeves:
                c = closes[s["coin"]][i]
                if s["qty"] == 0.0:
                    pool += s["cash"]
                    s["cash"] = 0.0
                else:
                    cur = s["qty"] * c
                    if cur > target:
                        sell_qty = (cur - target) / c
                        proceeds = sell_qty * c * (1 - slip) * (1 - fee)
                        s["qty"] -= sell_qty
                        s["cost"] *= (s["qty"] / (s["qty"] + sell_qty))
                        pool += proceeds
                        yr_sales[year] = yr_sales.get(year, 0.0) + proceeds
            # Dorovnat podvážené krypto-díly a zbytek rozdělit rovným dílem mezi hotovostní díly
            crypto_need = []
            cash_sleeves = []
            for s in sleeves:
                c = closes[s["coin"]][i]
                if s["qty"] == 0.0:
                    cash_sleeves.append(s)
                else:
                    cur = s["qty"] * c
                    if cur < target:
                        crypto_need.append((s, target - cur))
            # Přepočítat cíl po zaplacení prodejních poplatků
            rem_total = pool + sum(s["qty"] * closes[s["coin"]][i] for s in sleeves)
            adj_target = rem_total / 6.0
            for s, _ in crypto_need:
                c = closes[s["coin"]][i]
                cur = s["qty"] * c
                add = max(0.0, min(pool, adj_target - cur))
                if add > 0:
                    buy_px = c * (1 + slip)
                    s["qty"] += add * (1 - fee) / buy_px
                    s["cost"] += add
                    pool -= add
            if cash_sleeves:
                each = pool / len(cash_sleeves)
                for s in cash_sleeves:
                    s["cash"] = each
            elif pool > 0:
                # Všechny díly jsou v krypto -> zbytek poolu rovnoměrně dokoupí
                each = pool / len(sleeves)
                for s in sleeves:
                    c = closes[s["coin"]][i]
                    buy_px = c * (1 + slip)
                    s["qty"] += each * (1 - fee) / buy_px
                    s["cost"] += each

        prev_year = year
        prev_q = (year, q)

        # Signály a exekuce pro každý díl
        active_count = 0
        for s in sleeves:
            a, n = s["coin"], s["sma"]
            c = closes[a][i]
            sma = sum(closes[a][i - n + 1:i + 1]) / n

            # Cena exekuce (při lag_hours > 0 z 1h svíčky po půlnoci)
            exec_px = c
            if lag_hours > 0 and raw_1h is not None:
                # Denní svíčka t končí v t + DAY_MS.
                # Hodinová svíčka za hodinu H po půlnoci má openTime = t + DAY_MS + (H-1)*HOUR_MS
                h_ts = t + DAY_MS + (lag_hours - 1) * HOUR_MS
                if h_ts in raw_1h[a]:
                    exec_px = raw_1h[a][h_ts]

            if s["qty"] == 0.0 and c > sma * (1 + band_in):
                buy_px = exec_px * (1 + slip)
                s["cost"] = s["cash"]
                s["qty"] = s["cash"] * (1 - fee) / buy_px
                s["cash"] = 0.0
                switches += 1
            elif s["qty"] > 0.0 and c <= sma * (1 - band_out):
                sell_px = exec_px * (1 - slip)
                proceeds = s["qty"] * sell_px * (1 - fee)
                trades.append(proceeds / s["cost"] - 1.0)
                yr_sales[year] = yr_sales.get(year, 0.0) + proceeds
                s["cash"] = proceeds
                s["qty"] = 0.0
                s["cost"] = 0.0
                switches += 1

            if s["qty"] > 0.0:
                active_count += 1

        eq = sum(s["cash"] + s["qty"] * closes[s["coin"]][i] for s in sleeves)
        curve.append(eq)
        in_market_frac.append(active_count / 6.0)
        yr_eq_sum[year] = yr_eq_sum.get(year, 0.0) + eq
        yr_days[year] = yr_days.get(year, 0) + 1

    final_eq = curve[-1]
    final_weights = {
        s["name"]: (s["cash"] + s["qty"] * closes[s["coin"]][-1]) / final_eq
        for s in sleeves
    }

    # Roční obrat prodejů jako násobek průměrné hodnoty účtu v daném roce
    turnover_by_year = {}
    for y in sorted(yr_days):
        avg_eq = yr_eq_sum[y] / yr_days[y]
        turnover_by_year[y] = yr_sales.get(y, 0.0) / avg_eq if avg_eq > 0 else 0.0

    return {
        "curve": curve,
        "switches": switches,
        "trades": trades,
        "exposure": sum(in_market_frac) / len(in_market_frac),
        "final_weights": final_weights,
        "turnover_by_year": turnover_by_year,
    }


def mdd_and_duration(curve):
    peak = curve[0]
    peak_i = 0
    max_dd = 0.0
    max_dur = 0
    for i, e in enumerate(curve):
        if e >= peak:
            peak = e
            peak_i = i
        else:
            dd = 1.0 - e / peak
            if dd > max_dd:
                max_dd = dd
            if i - peak_i > max_dur:
                max_dur = i - peak_i
    return max_dd, max_dur


def yearly_returns(curve, days):
    res, prev = {}, curve[0]
    for i, t in enumerate(days):
        y = datetime.fromtimestamp(t / 1000, tz=timezone.utc).year
        nxt = days[i + 1] if i + 1 < len(days) else None
        if nxt is None or datetime.fromtimestamp(nxt / 1000, tz=timezone.utc).year != y:
            res[y] = curve[i] / prev - 1.0
            prev = curve[i]
    return res


def pct(x, dec=0):
    return f"{x * 100:+.{dec}f} %"


def fmt_row(name, res, dd, years, yrs):
    c = res["curve"]
    cagr = c[-1] ** (1.0 / years) - 1.0
    dd_val, dd_dur = mdd_and_duration(c)
    calmar = cagr / dd_val if dd_val > 0 else 0.0
    tr = res["trades"]
    losers = [x for x in tr if x <= 0]
    y = yearly_returns(c, dd)
    return [
        name,
        f"{c[-1]:.1f}×",
        pct(cagr, 1),
        pct(-dd_val, 1),
        f"{calmar:.2f}",
        f"{dd_dur} d",
        f"{res['exposure'] * 100:.0f} %",
        f"{len(tr) / years:.1f}",
        f"{len(losers) / len(tr) * 100:.0f} %" if tr else "-",
    ] + [pct(y[k], 0) for k in yrs]


def main():
    os.makedirs(OUT, exist_ok=True)
    raw_1d, raw_1h = load_data()
    days = sorted(set(raw_1d["BTCUSDT"]) & set(raw_1d["ETHUSDT"]))
    closes = {a: [raw_1d[a][d] for d in days] for a in COINS}
    start = max(SMAS) + 3
    dd = days[start:]
    years = (dd[-1] - dd[0]) / DAY_MS / 365.25

    hold_curve = [
        sum(closes[a][i] / closes[a][start] for a in COINS) / 2.0
        for i in range(start, len(days))
    ]
    yrs = sorted(yearly_returns(hold_curve, dd))
    d_str = lambda ms: datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")

    hdr = [
        "Varianta", "Násobek", "Ročně", "Max. propad", "Calmar",
        "Max. doba pod ATH", "Čas v trhu", "Obchodů/rok", "Ztrátových"
    ] + [str(y) for y in yrs]

    md = [
        f"# Detailní analýza trendové strategie ({d_str(dd[0])} až {d_str(dd[-1])})",
        "",
        f"Základ: BTC + ETH × SMA 50/100/150 dní (6 dílů). Výchozí náklady: poplatek {FEE*100:.1f} % + skluz {SLIP*100:.2f} %.",
        "Calmar = roční výnos / max. propad (vyšší = lepší poměr výnosu k riziku).",
        "",
    ]

    # --- 1. Stabilita hysterezního pásma ---
    md += ["## 1. Stabilita hysterezního pásma", "",
           "| " + " | ".join(hdr) + " |", "|" + "---|" * len(hdr)]
    band_configs = [
        ("Originál (0 %)", 0.0, 0.0),
        ("Pásmo 0,5 %", 0.005, 0.005),
        ("Pásmo 1,0 %", 0.01, 0.01),
        ("Pásmo 1,5 %", 0.015, 0.015),
        ("Pásmo 2,0 %", 0.02, 0.02),
        ("Pásmo 2,5 %", 0.025, 0.025),
        ("Pásmo 3,0 %", 0.03, 0.03),
        ("Asym: nákup +2 %, prodej 0 %", 0.02, 0.0),
        ("Asym: nákup +2 %, prodej -1 %", 0.02, 0.01),
    ]
    saved = {}
    for name, bi, bo in band_configs:
        res = simulate_portfolio(days, closes, start, band_in=bi, band_out=bo)
        saved[name] = res
        md.append("| " + " | ".join(fmt_row(name, res, dd, years, yrs)) + " |")

    # Benchmark 50/50 hold
    h_cagr = hold_curve[-1] ** (1.0 / years) - 1.0
    h_dd, h_dur = mdd_and_duration(hold_curve)
    h_y = yearly_returns(hold_curve, dd)
    md.append("| " + " | ".join([
        "Držet 50/50 BTC+ETH", f"{hold_curve[-1]:.1f}×", pct(h_cagr, 1),
        pct(-h_dd, 1), f"{h_cagr / h_dd:.2f}", f"{h_dur} d", "100 %", "-", "-"
    ] + [pct(h_y[k], 0) for k in yrs]) + " |")

    # --- 2. Vliv rebalancování 6 dílů ---
    md += ["", "## 2. Vliv rebalancování 6 dílů", "",
           "| " + " | ".join(hdr) + " |", "|" + "---|" * len(hdr)]
    reb_configs = [
        ("Originál 0 % – bez rebal.", 0.0, 0.0, None),
        ("Originál 0 % – ročně", 0.0, 0.0, "yearly"),
        ("Originál 0 % – čtvrtletně", 0.0, 0.0, "quarterly"),
        ("Pásmo 2 % – bez rebal.", 0.02, 0.02, None),
        ("Pásmo 2 % – ročně", 0.02, 0.02, "yearly"),
        ("Pásmo 2 % – čtvrtletně", 0.02, 0.02, "quarterly"),
    ]
    reb_saved = {}
    for name, bi, bo, reb in reb_configs:
        res = simulate_portfolio(days, closes, start, band_in=bi, band_out=bo, rebalance=reb)
        reb_saved[name] = res
        md.append("| " + " | ".join(fmt_row(name, res, dd, years, yrs)) + " |")

    md += ["", "### Rozjezd vah dílů na konci období (ideál = 16,7 % na díl)", "",
           "| Varianta | BTC_50 | BTC_100 | BTC_150 | ETH_50 | ETH_100 | ETH_150 |",
           "|---|---|---|---|---|---|---|"]
    for key in ["Originál 0 % – bez rebal.", "Originál 0 % – ročně",
                "Pásmo 2 % – bez rebal.", "Pásmo 2 % – ročně"]:
        w = reb_saved[key]["final_weights"]
        md.append("| " + " | ".join([key] + [
            f"{w[k] * 100:.1f} %" for k in ("BTC_50", "BTC_100", "BTC_150", "ETH_50", "ETH_100", "ETH_150")
        ]) + " |")

    # --- 3. Vliv zpoždění exekuce po půlnoci ---
    md += ["", "## 3. Vliv zpoždění exekuce po půlnoci (reálná 1h data)", "",
           "| " + " | ".join(hdr) + " |", "|" + "---|" * len(hdr)]
    for label, b in [("Originál 0 %", 0.0), ("Pásmo 2 %", 0.02)]:
        for lag in (0, 1, 2, 6):
            res = simulate_portfolio(days, closes, start, band_in=b, band_out=b,
                                     lag_hours=lag, raw_1h=raw_1h)
            md.append("| " + " | ".join(fmt_row(f"{label}, zpoždění +{lag}h", res, dd, years, yrs)) + " |")

    # --- 4. Stress test poplatků a skluzu ---
    md += ["", "## 4. Stress test nákladů (poplatek + skluz za 1 stranu)", "",
           "| Varianta | Náklady | Násobek | Ročně | Max. propad | Calmar |",
           "|---|---|---|---|---|---|"]
    for label, b in [("Originál 0 %", 0.0), ("Pásmo 2 %", 0.02)]:
        for tot in (0.0015, 0.0030, 0.0050):
            res = simulate_portfolio(days, closes, start, band_in=b, band_out=b,
                                     fee=tot, slip=0.0)
            c = res["curve"]
            cagr = c[-1] ** (1.0 / years) - 1.0
            dd_val, _ = mdd_and_duration(c)
            md.append(f"| {label} | {tot*100:.2f} % | {c[-1]:.1f}× | {pct(cagr, 1)} | {pct(-dd_val, 1)} | {cagr/dd_val:.2f} |")

    # --- 5. Roční obrat prodejů (český hodnotový test 100 000 Kč) ---
    md += ["", "## 5. Roční obrat prodejů (násobek průměrné hodnoty účtu)", "",
           "Ukazuje, kolikanásobek účtu za rok projde prodejem (směnou do stablecoinu). "
           "Pro hranici 100 000 Kč hrubých prodejů platí: `max bezpečný účet ≈ 100 000 / násobek`.", "",
           "| Varianta | Průměrný rok | Nejrušnější rok | Orientační účet pro limit 100k Kč (průměr / špička) |",
           "|---|---|---|---|"]
    for key in ["Originál (0 %)", "Pásmo 1,0 %", "Pásmo 2,0 %", "Pásmo 3,0 %"]:
        tb = saved[key]["turnover_by_year"]
        vals = list(tb.values())
        avg_t = sum(vals) / len(vals)
        max_y = max(tb, key=tb.get)
        max_t = tb[max_y]
        lim_avg = int(100_000 / avg_t) if avg_t > 0 else 0
        lim_max = int(100_000 / max_t) if max_t > 0 else 0
        s_avg = f"~{lim_avg:,} Kč".replace(",", " ")
        s_max = f"~{lim_max:,} Kč".replace(",", " ")
        md.append(f"| {key} | {avg_t:.2f}× | {max_t:.2f}× ({max_y}) | {s_avg} / {s_max} |")

    out_path = os.path.join(OUT, "report_detail.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md) + "\n")
    print(f"\nUloženo do {out_path}")


if __name__ == "__main__":
    main()
