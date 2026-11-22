#!/usr/bin/env python3
"""
Týdenní e-mailový přehled papírového trendového bota (BTC + ETH × SMA 50/100/150).

Každý pátek sestaví z data/trend_* (a aktuálních denních dat Binance pro vzdálenost
od SMA průměrů) přehledný HTML + textový e-mail a odešle ho přes Gmail SMTP.

Proměnné prostředí (v GitHub Actions uložené jako Secrets):
  GMAIL_USER         – odesílací Gmail adresa (např. mlejnecek1@gmail.com)
  GMAIL_APP_PASSWORD – 16místné heslo aplikace (Google Account -> Hesla aplikací)
  REPORT_TO          – cílová adresa (nepovinné; výchozí = GMAIL_USER)
  DRY_RUN            – pokud "1", e-mail neodesílá a vypíše náhled (nebo uloží HTML)
"""
import csv
import html
import json
import os
import smtplib
import ssl
import sys
import time
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from bot_btc import COINS, DATA_DIR, SMAS, START, get

REPO_URL = "https://github.com/mlejnecek/krypto-bot"


def read_csv(name):
    p = os.path.join(DATA_DIR, name)
    if not os.path.exists(p):
        return []
    with open(p, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def fetch_market_snapshot():
    """Stáhne poslední uzavřené denní ceny a aktuální ceny pro výpočet vzdálenosti od SMA."""
    now = int(time.time() * 1000)
    closes, prices = {}, {}
    for c in COINS:
        kl = get("/api/v3/klines", {"symbol": c, "interval": "1d", "limit": max(SMAS) + 5})
        closes[c] = [float(k[4]) for k in kl if k[6] < now]
        prices[c] = float(get("/api/v3/ticker/price", {"symbol": c})["price"])
    return closes, prices


def fmt_pct(val):
    return f"{val:+.2f} %"


def build_report():
    sp = os.path.join(DATA_DIR, "trend_stav.json")
    if not os.path.exists(sp):
        raise RuntimeError(f"Chybí {sp}")
    with open(sp, encoding="utf-8") as f:
        st = json.load(f)

    ucet_rows = read_csv("trend_ucet.csv")
    obchody_rows = read_csv("trend_obchody.csv")
    if not ucet_rows:
        raise RuntimeError("Soubor trend_ucet.csv je prázdný.")

    # Pokus o stažení aktuálních hodnot SMA z Binance (při výpadku API report stále funguje bez SMA)
    closes, live_prices = {}, {}
    try:
        closes, live_prices = fetch_market_snapshot()
    except Exception as e:
        print(f"Varování: nepodařilo se stáhnout aktuální SMA z Binance ({e}), použiji jen lokální data.")

    last_row = ucet_rows[-1]
    week_rows = ucet_rows[-7:]
    ref_row = ucet_rows[-8] if len(ucet_rows) >= 8 else ucet_rows[0]

    eq_now = float(last_row["hodnota_uctu"])
    hold_now = float(last_row["drzet_50_50"])
    eq_prev = float(ref_row["hodnota_uctu"])
    hold_prev = float(ref_row["drzet_50_50"])

    eq_total_pct = (eq_now / START - 1.0) * 100
    hold_total_pct = (hold_now / START - 1.0) * 100
    diff_usdt = eq_now - hold_now
    diff_pct = eq_total_pct - hold_total_pct

    eq_week_pct = (eq_now / eq_prev - 1.0) * 100 if eq_prev > 0 else 0.0
    hold_week_pct = (hold_now / hold_prev - 1.0) * 100 if hold_prev > 0 else 0.0

    btc_px = live_prices.get("BTCUSDT", float(last_row["btc_cena"]))
    eth_px = live_prices.get("ETHUSDT", float(last_row["eth_cena"]))
    prices_map = {"BTCUSDT": btc_px, "ETHUSDT": eth_px}

    # Obchody za posledních 7 dní
    now_dt = datetime.now(timezone.utc)
    cutoff = now_dt - timedelta(days=7)
    recent_trades = []
    for r in obchody_rows:
        try:
            dt = datetime.strptime(r["cas_utc"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            if dt >= cutoff:
                recent_trades.append(r)
        except ValueError:
            pass

    # Přehled 6 dílů
    sleeve_rows = []
    v_trhu = 0
    for name, d in st["dily"].items():
        coin = d["coin"]
        n = d["sma"]
        qty = float(d["qty"])
        cash = float(d["cash"])
        px = prices_map[coin]
        val = cash + qty * px
        in_mkt = qty > 0
        if in_mkt:
            v_trhu += 1

        close_str = "-"
        sma_str = "-"
        dist_str = "-"
        if coin in closes and len(closes[coin]) >= n:
            last_c = closes[coin][-1]
            sma_val = sum(closes[coin][-n:]) / n
            dist = (last_c / sma_val - 1.0) * 100
            close_str = f"{last_c:,.2f}".replace(",", " ")
            sma_str = f"{sma_val:,.2f}".replace(",", " ")
            dist_str = f"{dist:+.1f} %"

        sleeve_rows.append({
            "dil": name,
            "stav": "Drží minci" if in_mkt else "USDT",
            "hodnota": f"{val:.2f} USDT",
            "zavreni": close_str,
            "sma": sma_str,
            "vzdalenost": dist_str,
            "in_mkt": in_mkt,
        })

    # Lidský slovní komentář
    if v_trhu == 0:
        stav_popis = "Všech 6 dílů je v bezpečí v USDT (trh je pod všemi sledovanými klouzavými průměry)."
    elif v_trhu == 6:
        stav_popis = "Všech 6 dílů je naplno v trhu (BTC i ETH jsou nad všemi sledovanými průměry)."
    else:
        stav_popis = f"V trhu je aktuálně {v_trhu} ze 6 dílů, zbylých {6 - v_trhu}/6 je zaparkováno v USDT."

    if diff_usdt >= 0:
        srovnani_popis = (
            f"Oproti prostému držení 50/50 BTC+ETH je bot v náskoku o "
            f"{diff_usdt:+.2f} USDT ({diff_pct:+.1f} p.b.)."
        )
    else:
        srovnani_popis = (
            f"Oproti prostému držení 50/50 BTC+ETH bot zatím zaostává o "
            f"{abs(diff_usdt):.2f} USDT ({diff_pct:+.1f} p.b.)."
        )

    datum = last_row["cas_utc"][:10]
    subject = (
        f"Krypto bot – páteční přehled ({datum}): "
        f"{eq_now:.2f} USDT ({eq_total_pct:+.1f} % vs. držet {hold_total_pct:+.1f} %)"
    )

    # --- Plain-text verze ---
    txt = [
        f"Krypto bot – týdenní přehled ({datum})",
        "=" * 50,
        "",
        f"Účet bota:     {eq_now:.2f} USDT ({fmt_pct(eq_total_pct)} od startu {st['start'][:10]} | {fmt_pct(eq_week_pct)} za týden)",
        f"Držet 50/50:   {hold_now:.2f} USDT ({fmt_pct(hold_total_pct)} od startu | {fmt_pct(hold_week_pct)} za týden)",
        f"Rozdíl:        {diff_usdt:+.2f} USDT ({diff_pct:+.1f} p.b.)",
        f"Aktuální ceny: BTC {btc_px:,.2f} USDT | ETH {eth_px:,.2f} USDT".replace(",", " "),
        f"Přepnutí celkem od startu: {st.get('prepnuti', 0)}",
        "",
        stav_popis,
        srovnani_popis,
        "",
        "Stav 6 dílů (vzdálenost = jak daleko je včerejší zavření od SMA):",
    ]
    for r in sleeve_rows:
        txt.append(
            f"  {r['dil']:7s} | {r['stav']:10s} | {r['hodnota']:11s} | "
            f"zavření {r['zavreni']} vs SMA {r['sma']} ({r['vzdalenost']})"
        )

    txt += ["", "Vývoj za posledních 7 dní:"]
    for r in week_rows:
        txt.append(
            f"  {r['cas_utc'][:10]} | {r['dilu_v_trhu']} | účet {r['hodnota_uctu']} USDT | "
            f"držet {r['drzet_50_50']} USDT | BTC {r['btc_cena']} | ETH {r['eth_cena']}"
        )

    txt += ["", "Obchody za posledních 7 dní:"]
    if recent_trades:
        for t in recent_trades:
            txt.append(
                f"  {t['cas_utc']} | {t['dil']} | {t['akce']} @ {t['cena']} "
                f"({t['hodnota_usdt']} USDT, poplatek {t['poplatek']})"
            )
    else:
        txt.append("  Za posledních 7 dní neproběhlo žádné přepnutí.")
    txt += ["", f"GitHub repozitář: {REPO_URL}"]
    plain_body = "\n".join(txt)

    # --- HTML verze ---
    def td(v, align="left", bold=False, color=None):
        st_css = f"padding:7px 10px;border-bottom:1px solid #e5e7eb;text-align:{align};"
        if bold:
            st_css += "font-weight:600;"
        if color:
            st_css += f"color:{color};"
        return f'<td style="{st_css}">{html.escape(str(v))}</td>'

    def th(v, align="left"):
        return (
            f'<th style="padding:8px 10px;border-bottom:2px solid #cbd5e1;'
            f'background:#f8fafc;text-align:{align};font-size:13px;color:#334155;">'
            f'{html.escape(str(v))}</th>'
        )

    sleeve_html = []
    for r in sleeve_rows:
        badge_bg = "#dcfce7" if r["in_mkt"] else "#f1f5f9"
        badge_fg = "#166534" if r["in_mkt"] else "#475569"
        badge = (
            f'<span style="background:{badge_bg};color:{badge_fg};padding:2px 8px;'
            f'border-radius:10px;font-size:12px;font-weight:600;">{html.escape(r["stav"])}</span>'
        )
        dist_color = "#15803d" if r["vzdalenost"].startswith("+") else "#b91c1c"
        sleeve_html.append(
            "<tr>"
            + td(r["dil"], bold=True)
            + f'<td style="padding:7px 10px;border-bottom:1px solid #e5e7eb;">{badge}</td>'
            + td(r["hodnota"], align="right")
            + td(r["zavreni"], align="right")
            + td(r["sma"], align="right")
            + td(r["vzdalenost"], align="right", bold=True, color=dist_color)
            + "</tr>"
        )

    week_html = []
    for r in week_rows:
        week_html.append(
            "<tr>"
            + td(r["cas_utc"][:10])
            + td(r["dilu_v_trhu"], align="center")
            + td(f"{float(r['hodnota_uctu']):.2f} USDT", align="right", bold=True)
            + td(f"{float(r['drzet_50_50']):.2f} USDT", align="right")
            + td(f"{float(r['btc_cena']):,.0f}".replace(",", " "), align="right")
            + td(f"{float(r['eth_cena']):,.0f}".replace(",", " "), align="right")
            + "</tr>"
        )

    if recent_trades:
        tr_rows = []
        for t in recent_trades:
            akce_color = "#15803d" if t["akce"] == "NAKUP" else "#b91c1c"
            tr_rows.append(
                "<tr>"
                + td(t["cas_utc"])
                + td(t["dil"], bold=True)
                + td(t["akce"], bold=True, color=akce_color)
                + td(t["cena"], align="right")
                + td(f"{t['hodnota_usdt']} USDT", align="right")
                + td(f"{t['zavreni']} vs {t['prumer']}", align="right")
                + "</tr>"
            )
        trades_section = (
            '<table style="width:100%;border-collapse:collapse;font-size:13px;">'
            "<thead><tr>"
            + th("Čas (UTC)") + th("Díl") + th("Akce")
            + th("Cena", "right") + th("Hodnota", "right") + th("Zavření vs SMA", "right")
            + "</tr></thead><tbody>"
            + "".join(tr_rows)
            + "</tbody></table>"
        )
    else:
        trades_section = (
            '<p style="color:#475569;font-size:14px;margin:6px 0;">'
            "Za posledních 7 dní neproběhlo žádné přepnutí dílu.</p>"
        )

    diff_color = "#15803d" if diff_usdt >= 0 else "#b91c1c"
    eq_color = "#15803d" if eq_total_pct >= 0 else "#b91c1c"
    btc_px_str = f"{btc_px:,.2f}".replace(",", " ")
    eth_px_str = f"{eth_px:,.2f}".replace(",", " ")

    html_body = f"""<!DOCTYPE html>
<html lang="cs">
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;background:#f8fafc;margin:0;padding:20px;color:#0f172a;">
  <div style="max-width:680px;margin:0 auto;background:#ffffff;border:1px solid #e2e8f0;border-radius:10px;padding:24px;">
    <h2 style="margin:0 0 6px 0;color:#0f172a;">Krypto bot – páteční přehled ({html.escape(datum)})</h2>
    <p style="margin:0 0 18px 0;color:#64748b;font-size:13px;">
      Papírová trendová strategie BTC + ETH × SMA 50/100/150 (start {html.escape(st['start'][:10])}, vklad {START:.0f} USDT)
    </p>

    <table style="width:100%;border-collapse:separate;border-spacing:8px 0;margin:0 -8px 18px -8px;">
      <tr>
        <td style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:12px;width:33%;">
          <div style="font-size:12px;color:#64748b;">Hodnota účtu bota</div>
          <div style="font-size:20px;font-weight:700;color:#0f172a;margin-top:4px;">{eq_now:.2f} USDT</div>
          <div style="font-size:12px;color:{eq_color};margin-top:2px;">{fmt_pct(eq_total_pct)} celkem | {fmt_pct(eq_week_pct)} / 7d</div>
        </td>
        <td style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:12px;width:33%;">
          <div style="font-size:12px;color:#64748b;">Držet 50/50 BTC+ETH</div>
          <div style="font-size:20px;font-weight:700;color:#0f172a;margin-top:4px;">{hold_now:.2f} USDT</div>
          <div style="font-size:12px;color:#475569;margin-top:2px;">{fmt_pct(hold_total_pct)} celkem | {fmt_pct(hold_week_pct)} / 7d</div>
        </td>
        <td style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:12px;width:33%;">
          <div style="font-size:12px;color:#64748b;">Náskok bota / Pozic</div>
          <div style="font-size:20px;font-weight:700;color:{diff_color};margin-top:4px;">{diff_usdt:+.2f} USDT</div>
          <div style="font-size:12px;color:#475569;margin-top:2px;">V trhu: <strong>{v_trhu}/6</strong> dílů | přepnutí: {st.get('prepnuti', 0)}</div>
        </td>
      </tr>
    </table>

    <div style="background:#f1f5f9;border-left:4px solid #3b82f6;padding:12px 14px;border-radius:4px;margin-bottom:20px;font-size:14px;line-height:1.5;">
      <div><strong>Shrnutí:</strong> {html.escape(stav_popis)}</div>
      <div style="margin-top:4px;">{html.escape(srovnani_popis)}</div>
      <div style="margin-top:4px;color:#475569;font-size:13px;">
        Aktuální ceny: BTC <strong>{btc_px_str} USDT</strong> | ETH <strong>{eth_px_str} USDT</strong>
      </div>
    </div>

    <h3 style="margin:20px 0 8px 0;font-size:15px;color:#1e293b;">1. Stav všech 6 dílů a vzdálenost od klouzavého průměru</h3>
    <p style="margin:0 0 8px 0;font-size:12px;color:#64748b;">
      Kladné % znamená zavření NAD průměrem (díl drží minci), záporné % POD průměrem (díl čeká v USDT).
    </p>
    <table style="width:100%;border-collapse:collapse;font-size:13px;">
      <thead>
        <tr>
          {th("Díl")}{th("Stav")}{th("Hodnota", "right")}{th("Zavření", "right")}{th("Průměr SMA", "right")}{th("Od SMA", "right")}
        </tr>
      </thead>
      <tbody>
        {"".join(sleeve_html)}
      </tbody>
    </table>

    <h3 style="margin:22px 0 8px 0;font-size:15px;color:#1e293b;">2. Vývoj účtu za posledních 7 dní</h3>
    <table style="width:100%;border-collapse:collapse;font-size:13px;">
      <thead>
        <tr>
          {th("Datum")}{th("V trhu", "center")}{th("Účet bota", "right")}{th("Držet 50/50", "right")}{th("BTC", "right")}{th("ETH", "right")}
        </tr>
      </thead>
      <tbody>
        {"".join(week_html)}
      </tbody>
    </table>

    <h3 style="margin:22px 0 8px 0;font-size:15px;color:#1e293b;">3. Přepnutí (obchody) za posledních 7 dní</h3>
    {trades_section}

    <hr style="border:none;border-top:1px solid #e2e8f0;margin:22px 0 12px 0;">
    <p style="margin:0;font-size:12px;color:#64748b;">
      Automatický týdenní přehled z repozitáře
      <a href="{REPO_URL}" style="color:#2563eb;text-decoration:none;">{REPO_URL}</a>.
      Pouze fiktivní paper-trading (žádné reálné obchody).
    </p>
  </div>
</body>
</html>
"""
    return subject, plain_body, html_body


def send_email(subject, plain_body, html_body):
    user = os.environ.get("GMAIL_USER", "").strip()
    password = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    recipient = os.environ.get("REPORT_TO", "").strip() or user

    if not user or not password:
        raise RuntimeError(
            "Chybí GitHub Secrets GMAIL_USER nebo GMAIL_APP_PASSWORD. "
            "Nastavte je v Settings -> Secrets and variables -> Actions."
        )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Krypto Bot <{user}>"
    msg["To"] = recipient
    msg.attach(MIMEText(plain_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context, timeout=25) as srv:
        srv.login(user, password)
        srv.sendmail(user, [recipient], msg.as_string())
    print(f"E-mail odeslán na {recipient}: {subject}")


def main():
    subject, plain_body, html_body = build_report()
    if os.environ.get("DRY_RUN") == "1":
        print(f"SUBJECT: {subject}\n")
        print(plain_body)
        preview = os.environ.get("DRY_RUN_HTML")
        if preview:
            with open(preview, "w", encoding="utf-8") as f:
                f.write(html_body)
            print(f"\nHTML náhled uložen do {preview}")
        return
    send_email(subject, plain_body, html_body)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Chyba: {e}", file=sys.stderr)
        sys.exit(1)
