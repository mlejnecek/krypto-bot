#!/usr/bin/env python3
"""
Papírový (fiktivní) BTC trendový bot – spouští se jednou denně.

Pravidlo: když BTC zavřel včerejší den NAD průměrem posledních 50 denních
zavření -> držet BTC. Když POD -> držet USDT.
Vše v USDT, poplatek 0,1 % + skluz 0,05 %, žádné skutečné obchody.

Zápis do data/:
  btc_obchody.csv  – každé přepnutí BTC <-> USDT
  btc_ucet.csv     – denní stav účtu vs. prosté držení BTC
  btc_stav.json    – aktuální stav (hotovost, množství BTC)
"""
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone

from bot import get

DATA_DIR = os.environ.get("DATA_DIR", "data")
START = 1000.0
SMA = 50
FEE, SLIP = 0.001, 0.0005


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def append(name, header, row):
    p = os.path.join(DATA_DIR, name)
    new = not os.path.exists(p)
    with open(p, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(header)
        w.writerow(row)


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    now = int(time.time() * 1000)

    kl = get("/api/v3/klines", {"symbol": "BTCUSDT", "interval": "1d", "limit": SMA + 5})
    closed = [k for k in kl if k[6] < now]
    closes = [float(k[4]) for k in closed[-SMA:]]
    if len(closes) < SMA:
        raise RuntimeError("málo dat pro průměr")
    sma = sum(closes) / SMA
    last_close = closes[-1]
    signal = last_close > sma
    price = float(get("/api/v3/ticker/price", {"symbol": "BTCUSDT"})["price"])

    sp = os.path.join(DATA_DIR, "btc_stav.json")
    if os.path.exists(sp):
        with open(sp) as f:
            st = json.load(f)
    else:
        st = {"start": iso(now), "btc_start": price, "cash": START, "qty": 0.0,
              "prepnuti": 0}

    hdr = ["cas_utc", "akce", "cena", "mnozstvi_btc", "hodnota_usdt", "poplatek",
           "btc_zavreni", "prumer_50d"]
    if signal and st["qty"] == 0:
        px = price * (1 + SLIP)
        fee = st["cash"] * FEE
        st["qty"] = (st["cash"] - fee) / px
        append("btc_obchody.csv", hdr, [iso(now), "NAKUP BTC", f"{px:.2f}", f"{st['qty']:.8f}",
                                        f"{st['cash']:.2f}", f"{fee:.2f}",
                                        f"{last_close:.2f}", f"{sma:.2f}"])
        st["cash"] = 0.0
        st["prepnuti"] += 1
        print(f"NÁKUP BTC @ {px:.2f}")
    elif not signal and st["qty"] > 0:
        px = price * (1 - SLIP)
        gross = st["qty"] * px
        fee = gross * FEE
        st["cash"] = gross - fee
        append("btc_obchody.csv", hdr, [iso(now), "PRODEJ BTC", f"{px:.2f}", f"{st['qty']:.8f}",
                                        f"{st['cash']:.2f}", f"{fee:.2f}",
                                        f"{last_close:.2f}", f"{sma:.2f}"])
        st["qty"] = 0.0
        st["prepnuti"] += 1
        print(f"PRODEJ BTC @ {px:.2f}")
    else:
        print("Beze změny")

    equity = st["cash"] + st["qty"] * price
    hold = START * price / st["btc_start"]
    append("btc_ucet.csv",
           ["cas_utc", "drzi", "hodnota_uctu", "btc_drzet", "btc_cena", "prumer_50d"],
           [iso(now), "BTC" if st["qty"] > 0 else "USDT", f"{equity:.2f}", f"{hold:.2f}",
            f"{price:.2f}", f"{sma:.2f}"])
    with open(sp, "w") as f:
        json.dump(st, f, indent=2)
    print(f"BTC {price:.0f}, průměr 50d {sma:.0f} -> {'BTC' if signal else 'USDT'} | "
          f"účet {equity:.2f} USDT, BTC držet {hold:.2f} USDT")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:
        print(f"Chyba: {e}", file=sys.stderr)
        sys.exit(1)
