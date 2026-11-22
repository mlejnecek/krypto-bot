#!/usr/bin/env python3
"""
Papírový (fiktivní) trendový bot BTC + ETH – spouští se jednou denně.

Účet je rozdělený na 6 stejných dílů (každý začíná se 1/6 kapitálu):
  BTC × průměr 50 / 100 / 150 dní   a   ETH × průměr 50 / 100 / 150 dní.
Každý díl samostatně: včerejší denní zavření NAD svým průměrem -> drží minci,
POD -> drží USDT. Díly se tak přepínají postupně, ne všechno najednou.

Vše v USDT, poplatek 0,1 % + skluz 0,05 %, žádné skutečné obchody.
Samostatný skript v čistém Pythonu (bez externích knihoven a bez závislosti na bot.py).

Zápis do data/:
  trend_obchody.csv – každé přepnutí dílu
  trend_ucet.csv    – denní stav účtu vs. prosté držení 50/50 BTC+ETH
  trend_stav.json   – aktuální stav všech dílů
"""
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API = os.environ.get("BINANCE_API", "https://data-api.binance.vision")
DATA_DIR = os.environ.get("DATA_DIR", "data")
START = 1000.0
COINS = ["BTCUSDT", "ETHUSDT"]
SMAS = [50, 100, 150]
FEE, SLIP = 0.001, 0.0005


def get(path, params=None, tries=3):
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "paper-bot/1.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"API selhalo: {url} -> {last}")


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def append(name, header, row):
    p = os.path.join(DATA_DIR, name)
    new = not os.path.exists(p) or os.path.getsize(p) == 0
    with open(p, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(header)
        w.writerow(row)


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    now = int(time.time() * 1000)

    # Naplánované běhy jsou dva denně (záloha). Pokud už dnes proběhl a uložil se,
    # druhý běh nic nedělá. Ruční spuštění proběhne vždy.
    up = os.path.join(DATA_DIR, "trend_ucet.csv")
    if os.environ.get("GITHUB_EVENT_NAME") == "schedule" and os.path.exists(up):
        with open(up) as f:
            lines = [ln for ln in f.read().strip().splitlines() if ln.strip()]
        if lines and lines[-1].startswith(iso(now)[:10]):
            print("Dnes už proběhl, nic nedělám.")
            return

    closes, prices = {}, {}
    for c in COINS:
        kl = get("/api/v3/klines", {"symbol": c, "interval": "1d", "limit": max(SMAS) + 5})
        closes[c] = [float(k[4]) for k in kl if k[6] < now]
        if len(closes[c]) < max(SMAS):
            raise RuntimeError(f"málo dat pro {c}")
        prices[c] = float(get("/api/v3/ticker/price", {"symbol": c})["price"])

    sp = os.path.join(DATA_DIR, "trend_stav.json")
    if os.path.exists(sp):
        with open(sp) as f:
            st = json.load(f)
    else:
        st = {
            "start": iso(now),
            "start_ceny": prices,
            "prepnuti": 0,
            "dily": {
                f"{c[:-4]}_{n}": {"coin": c, "sma": n, "cash": START / 6, "qty": 0.0}
                for c in COINS
                for n in SMAS
            },
        }

    hdr = ["cas_utc", "dil", "akce", "cena", "mnozstvi", "hodnota_usdt", "poplatek",
           "zavreni", "prumer"]
    for name, d in st["dily"].items():
        c, n = d["coin"], d["sma"]
        last = closes[c][-1]
        sma = sum(closes[c][-n:]) / n
        px = prices[c]
        if last > sma and d["qty"] == 0:
            buy = px * (1 + SLIP)
            fee = d["cash"] * FEE
            d["qty"] = (d["cash"] - fee) / buy
            d["nakup_usdt"] = d["cash"]
            append("trend_obchody.csv", hdr, [iso(now), name, "NAKUP", f"{buy:.2f}",
                                              f"{d['qty']:.8f}", f"{d['cash']:.2f}",
                                              f"{fee:.2f}", f"{last:.2f}", f"{sma:.2f}"])
            d["cash"] = 0.0
            st["prepnuti"] += 1
            print(f"  {name}: NÁKUP @ {buy:.2f}")
        elif last <= sma and d["qty"] > 0:
            sell = px * (1 - SLIP)
            gross = d["qty"] * sell
            fee = gross * FEE
            d["cash"] = gross - fee
            cost = d.pop("nakup_usdt", None)
            pnl_txt = ""
            if cost and cost > 0:
                pnl = d["cash"] - cost
                pnl_txt = f" (zisk {pnl:+.2f} USDT / {pnl / cost * 100:+.1f} %)"
            append("trend_obchody.csv", hdr, [iso(now), name, "PRODEJ", f"{sell:.2f}",
                                              f"{d['qty']:.8f}", f"{d['cash']:.2f}",
                                              f"{fee:.2f}", f"{last:.2f}", f"{sma:.2f}"])
            d["qty"] = 0.0
            st["prepnuti"] += 1
            print(f"  {name}: PRODEJ @ {sell:.2f}{pnl_txt}")
        print(f"  {name}: zavření {last:.2f} vs průměr {sma:.2f} -> "
              f"{'drží' if d['qty'] > 0 else 'USDT'}")

    equity = sum(d["cash"] + d["qty"] * prices[d["coin"]] for d in st["dily"].values())
    hold = sum(START / 2 * prices[c] / st["start_ceny"][c] for c in COINS)
    v_trhu = sum(1 for d in st["dily"].values() if d["qty"] > 0)
    append("trend_ucet.csv",
           ["cas_utc", "dilu_v_trhu", "hodnota_uctu", "drzet_50_50", "btc_cena", "eth_cena"],
           [iso(now), f"{v_trhu}/6", f"{equity:.2f}", f"{hold:.2f}",
            f"{prices['BTCUSDT']:.2f}", f"{prices['ETHUSDT']:.2f}"])
    with open(sp, "w") as f:
        json.dump(st, f, indent=2)
    print(f"Účet {equity:.2f} USDT ({v_trhu}/6 dílů v trhu) | držet 50/50: {hold:.2f} USDT")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:
        print(f"Chyba: {e}", file=sys.stderr)
        sys.exit(1)
