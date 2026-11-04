#!/usr/bin/env python3
"""
Papírový (fiktivní) krypto bot – momentum skener.

Každou hodinu:
  1. zkontroluje otevřené pozice (stop-loss / take-profit / časový limit),
  2. projde nejlikvidnější páry XXX/USDT na Binance a hledá ty, které se
     zrovna "hýbou": hodinová svíčka s výrazně nadprůměrným objemem a růstem,
  3. do volných slotů fiktivně nakoupí nejsilnější kandidáty,
  4. zapíše vše do CSV v adresáři data/.

Žádné API klíče, žádné skutečné obchody. Jen čistý Python (bez knihoven).
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

# Veřejný endpoint Binance jen pro tržní data (bez přihlášení).
API = os.environ.get("BINANCE_API", "https://data-api.binance.vision")
DATA_DIR = os.environ.get("DATA_DIR", "data")

CFG = {
    "start_capital": 1000.0,        # fiktivní startovní kapitál v USDT
    "max_positions": 3,             # kolik pozic najednou
    "universe_size": 40,            # kolik nejlikvidnějších párů prohledat
    "min_quote_volume_24h": 20e6,   # min. 24h obrat v USDT (filtr na malé/manipulované měny)
    "vol_ratio_min": 2.5,           # objem svíčky aspoň 2,5× průměr za 24 h
    "min_candle_move": 0.01,        # svíčka musí vyrůst aspoň o 1 %
    "max_candle_move": 0.08,        # ...ale max o 8 % (nenaskakovat do extrémních pumpů)
    "stop_loss": 0.03,              # prodat při ztrátě 3 %
    "take_profit": 0.05,            # prodat při zisku 5 %
    "max_hold_hours": 24,           # nejpozději po 24 h prodat
    "fee": 0.001,                   # poplatek Binance 0,1 % za obchod
    "slippage": 0.0005,             # skluz 0,05 %
}

EXCLUDE_BASES = {
    "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "USDE", "USD1", "XUSD",
    "BFUSD", "EUR", "EURI", "AEUR", "PAXG", "WBTC", "WBETH",
}
HOUR_MS = 3_600_000


# ---------------------------------------------------------------- pomocné
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


def klines(symbol, limit=26):
    # [openTime, open, high, low, close, volume, closeTime, quoteVolume, ...]
    return get("/api/v3/klines", {"symbol": symbol, "interval": "1h", "limit": limit})


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def path(name):
    return os.path.join(DATA_DIR, name)


def append_csv(name, header, row):
    p = path(name)
    new = not os.path.exists(p)
    with open(p, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(header)
        w.writerow(row)


def load_state(btc_price, now_ms):
    p = path("state.json")
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return {
        "started": iso(now_ms),
        "cash": CFG["start_capital"],
        "btc_start": btc_price,
        "positions": {},
        "closed_trades": 0,
        "wins": 0,
    }


def save_state(state):
    with open(path("state.json"), "w") as f:
        json.dump(state, f, indent=2)


TRADE_HDR = ["cas_utc", "akce", "symbol", "cena", "mnozstvi", "hodnota_usdt",
             "poplatek", "zisk_usdt", "zisk_pct", "duvod"]


# ---------------------------------------------------------------- logika
def manage_exits(state, prices, now_ms):
    slip, fee = CFG["slippage"], CFG["fee"]
    for sym in list(state["positions"]):
        pos = state["positions"][sym]
        sl = pos["entry"] * (1 - CFG["stop_loss"])
        tp = pos["entry"] * (1 + CFG["take_profit"])
        exit_px, reason = None, None

        # projdi uzavřené svíčky od posledního checku (zachytí i pohyby mezi běhy)
        try:
            kl = klines(sym, 30)
        except RuntimeError as e:
            print(f"  ! {sym}: {e}")
            continue
        for k in kl:
            if k[0] < pos["checked_from"] or k[6] >= now_ms:
                continue
            o, h, l = float(k[1]), float(k[2]), float(k[3])
            if l <= sl:                     # konzervativně: SL má přednost
                exit_px, reason = min(o, sl) * (1 - slip), "stop-loss"
            elif h >= tp:
                exit_px, reason = tp * (1 - slip), "take-profit"
            pos["checked_from"] = k[0] + HOUR_MS
            if exit_px:
                break

        held_h = (now_ms - pos["opened_ms"]) / HOUR_MS
        if not exit_px and held_h >= CFG["max_hold_hours"] and sym in prices:
            exit_px, reason = prices[sym] * (1 - slip), "casovy limit"

        if exit_px:
            gross = pos["qty"] * exit_px
            f = gross * fee
            proceeds = gross - f
            pnl = proceeds - pos["cost"]
            state["cash"] += proceeds
            state["closed_trades"] += 1
            state["wins"] += pnl > 0
            del state["positions"][sym]
            append_csv("trades.csv", TRADE_HDR, [
                iso(now_ms), "PRODEJ", sym, f"{exit_px:.8g}", f"{pos['qty']:.8g}",
                f"{proceeds:.2f}", f"{f:.4f}", f"{pnl:.2f}",
                f"{pnl / pos['cost'] * 100:.2f}", reason])
            print(f"  PRODEJ {sym} @ {exit_px:.6g} ({reason}) zisk {pnl:+.2f} USDT")


def scan(tickers, held, now_ms):
    universe = []
    for t in tickers:
        s = t["symbol"]
        if not s.endswith("USDT"):
            continue
        base = s[:-4]
        if base in EXCLUDE_BASES or base.endswith(("UP", "DOWN", "BULL", "BEAR")):
            continue
        if float(t["quoteVolume"]) < CFG["min_quote_volume_24h"]:
            continue
        universe.append((float(t["quoteVolume"]), s))
    universe.sort(reverse=True)

    found = []
    for _, sym in universe[: CFG["universe_size"]]:
        if sym in held:
            continue
        try:
            kl = klines(sym, 26)
        except RuntimeError as e:
            print(f"  ! {sym}: {e}")
            continue
        closed = [k for k in kl if k[6] < now_ms]
        if len(closed) < 25:
            continue
        sig, prev = closed[-1], closed[-25:-1]
        avg = sum(float(k[7]) for k in prev) / len(prev)
        if avg <= 0:
            continue
        ratio = float(sig[7]) / avg
        move = float(sig[4]) / float(sig[1]) - 1
        if ratio >= CFG["vol_ratio_min"] and CFG["min_candle_move"] <= move <= CFG["max_candle_move"]:
            found.append({"symbol": sym, "ratio": ratio, "move": move,
                          "candle": iso(sig[0])})
    found.sort(key=lambda c: c["ratio"] * c["move"], reverse=True)
    return found


def open_positions(state, cands, prices, equity, now_ms):
    slip, fee = CFG["slippage"], CFG["fee"]
    for c in cands:
        if len(state["positions"]) >= CFG["max_positions"]:
            break
        sym = c["symbol"]
        if sym not in prices:
            continue
        size = min(state["cash"], equity / CFG["max_positions"])
        if size < 10:
            break
        px = prices[sym] * (1 + slip)
        f = size * fee
        qty = (size - f) / px
        state["cash"] -= size
        state["positions"][sym] = {
            "entry": px, "qty": qty, "cost": size, "opened_ms": now_ms,
            "opened": iso(now_ms), "checked_from": (now_ms // HOUR_MS) * HOUR_MS,
            "signal": f"objem {c['ratio']:.1f}x, svicka {c['move'] * 100:+.1f}%",
        }
        append_csv("trades.csv", TRADE_HDR, [
            iso(now_ms), "NAKUP", sym, f"{px:.8g}", f"{qty:.8g}", f"{size:.2f}",
            f"{f:.4f}", "", "", state["positions"][sym]["signal"]])
        print(f"  NAKUP {sym} @ {px:.6g} za {size:.2f} USDT ({state['positions'][sym]['signal']})")


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    now_ms = int(time.time() * 1000)

    tickers = get("/api/v3/ticker/24hr")
    prices = {t["symbol"]: float(t["lastPrice"]) for t in tickers}
    btc = prices["BTCUSDT"]
    state = load_state(btc, now_ms)
    print(f"Běh {iso(now_ms)} UTC, BTC {btc:.0f}")

    manage_exits(state, prices, now_ms)

    def equity():
        return state["cash"] + sum(p["qty"] * prices.get(s, p["entry"])
                                   for s, p in state["positions"].items())

    cands = scan(tickers, state["positions"], now_ms)
    for c in cands:
        append_csv("signals.csv", ["cas_utc", "symbol", "svicka_utc", "objem_nasobek", "pohyb_pct"],
                   [iso(now_ms), c["symbol"], c["candle"], f"{c['ratio']:.2f}", f"{c['move'] * 100:.2f}"])
    print(f"  Kandidátů: {len(cands)}")
    open_positions(state, cands, prices, equity(), now_ms)

    eq = equity()
    bench = CFG["start_capital"] * btc / state["btc_start"]
    append_csv("equity.csv",
               ["cas_utc", "hodnota_uctu", "hotovost", "pozic", "btc_drzet", "btc_cena"],
               [iso(now_ms), f"{eq:.2f}", f"{state['cash']:.2f}", len(state["positions"]),
                f"{bench:.2f}", f"{btc:.2f}"])
    save_state(state)
    print(f"  Účet: {eq:.2f} USDT | BTC držet: {bench:.2f} USDT | "
          f"obchodů {state['closed_trades']}, ziskových {state['wins']}")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:
        print(f"Chyba: {e}", file=sys.stderr)
        sys.exit(1)
