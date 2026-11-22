# Přehled změn (Changelog)

Chronologický záznam úprav v repozitáři `krypto-bot`, co se změnilo a proč.

## 2026-11-23 – Úklid repozitáře a detailní analýza strategie

### Změněno
- **`bot_btc.py`**:
  - Odstraněna závislost na nepoužívaném `bot.py` (`from bot import get`) – funkce `get()` přesunuta přímo do `bot_btc.py`, skript je nyní plně samostatný.
  - Ošetřen okrajový případ prázdného souboru `data/trend_ucet.csv` při kontrole duplicitního denního spuštění.
  - Do logu při prodeji přidán výpis realizovaného zisku/ztráty (v USDT i v %).
  - *Pravidla strategie (SMA 50/100/150 bez pásma) ani formát souborů `data/trend_*` se neměnily.*
- **`.github/workflows/bot.yml`**:
  - Vypnut automatický hodinový cron pro odstavený 1h momentum skener `bot.py` (ponecháno jen ruční spuštění přes `workflow_dispatch`), aby neběžel zbytečně a nekolidoval v concurrency skupině s `bot-btc.yml`.
- **`README.md`**:
  - Doplněn přehled aktivního trendového bota, jeho výstupů a všech backtestů.

### Přidáno
- **`backtest_detail.py`** a **`backtest/report_detail.md`**:
  - Rozšířená analýza trendové strategie BTC + ETH × SMA 50/100/150 na datech 2018–2026 (stabilita hysterezního pásma 0–3 %, roční a čtvrtletní rebalancování 6 dílů, vliv zpoždění exekuce po půlnoci +1h/+2h/+6h z 1h dat, stress test poplatků a orientační roční obrat prodejů).
- **`CHANGELOG.md`**:
  - Založen tento přehled změn pro sledování všech budoucích úprav přímo na GitHubu.

### Odstraněno
- **`data/state.json`**, **`data/equity.csv`**, **`data/btc_stav.json`**, **`data/btc_ucet.csv`**:
  - Smazány jednorázové pozůstatky ze starších testů (ze 4. 11. 2026). V adresáři `data/` zůstávají čistě živá data běžícího bota (`trend_stav.json`, `trend_ucet.csv`, `trend_obchody.csv`).

---

## 2026-11-04 až 2026-11-18 – Vývoj a spuštění trendového bota (shrnutí historie)
- Spuštěn denní paper-trading trendového bota `bot_btc.py` (6 dílů: BTC + ETH × SMA 50/100/150) na GitHub Actions (`.github/workflows/bot-btc.yml`).
- Přidány backtesty `backtest.py` (1h momentum – vyřazeno jako ztrátové), `backtest_denni.py` (denní svíčky), `backtest_robust.py` (robustnost SMA 20–200 od 2018) a `backtest_potvrzeni.py` (filtry proti falešným signálům).
