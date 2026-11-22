# krypto-bot

Papírový (fiktivní) trendový krypto bot na BTC + ETH a sada backtestů v čistém Pythonu (bez externích knihoven, bez API klíčů, využívá jen veřejná tržní data Binance).

## Aktivní strategie (`bot_btc.py`)

- **Princip:** Kapitál je rozdělený na 6 samostatných dílů – `BTC` a `ETH` × klouzavý průměr **50 / 100 / 150 dní**.
- **Pravidlo:** Každý díl se vyhodnocuje jednou denně po uzavření denní svíčky (UTC). Pokud je včerejší zavření nad průměrem, díl drží minci; jinak drží USDT.
- **Náklady v simulaci:** poplatek 0,1 % + skluz 0,05 % za stranu.
- **Automatický běh:** `.github/workflows/bot-btc.yml` (1× denně v `00:13 UTC`, záložní běh v `06:13 UTC`).
- **Výstupy:**
  - `data/trend_ucet.csv` – denní hodnota účtu vs. držení 50/50 BTC+ETH
  - `data/trend_obchody.csv` – historie všech přepnutí
  - `data/trend_stav.json` – aktuální stav 6 dílů

## Páteční e-mailový přehled (`weekly_report.py`)

- **Automatický běh:** `.github/workflows/weekly-report.yml` každý pátek v `07:00 UTC` (8:00 / 9:00 v ČR) nebo ručně tlačítkem **Run workflow**.
- **Obsah:** slovní shrnutí, stav všech 6 dílů a jejich vzdálenost od SMA, vývoj účtu za posledních 7 dní a přehled přepnutí za týden.
- **Nastavení v GitHub Secrets** (*Settings → Secrets and variables → Actions*):
  - `GMAIL_USER` – Gmail adresa (odesílatel i výchozí příjemce)
  - `GMAIL_APP_PASSWORD` – 16místné heslo aplikace z účtu Google
  - `REPORT_TO` – *(volitelné)* jiná cílová e-mailová adresa

## Přehled změn

Všechny provedené úpravy a důvody změn jsou zaznamenány v [CHANGELOG.md](CHANGELOG.md).

## Backtesty

| Skript | Výstup | Popis |
|---|---|---|
| `backtest_robust.py` | `backtest/report_robust.md` | Robustnost SMA 20–200 dní na BTC a ETH od roku 2018 |
| `backtest_potvrzeni.py` | `backtest/report_potvrzeni.md` | Test filtrů proti falešným signálům (potvrzení N dní, pásmo 1–2 %) |
| `backtest_detail.py` | `backtest/report_detail.md` | Stabilita pásem 0–3 %, rebalancování 6 dílů, zpoždění exekuce (+1h/+2h/+6h), stress test poplatků a roční obrat |
| `backtest_denni.py` | `backtest/report_denni.md` | Denní breakout svíčky (historický experiment) |
| `backtest.py` | `backtest/report.md` | Původní 1h momentum skener `bot.py` (ztrátový, odstavený) |
