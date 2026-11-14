# IBKR Portfolio Tracker

Upload your **Interactive Brokers Activity Statements** (CSV and/or HTML, any number of files, any mix of
periods, any number of accounts) and get

* a dashboard in the browser (Streamlit) with positions, cash, deposits, time‑weighted **CAGR** in USD and in your
  base currency, and a "what if I had bought the S&P 500 (VUAA) instead" simulation whose headline figure is the
  **VUAA CAGR (USD)**;
* every account **on its own, or all accounts merged into one consolidated portfolio** – positions, cash,
  performance and transaction histories – exportable as Excel and as CSV;
* a professionally formatted, **formula‑driven Excel workbook** (`IBKR_Portfolio_<account>.xlsx`, or
  `IBKR_Portfolio_ALL.xlsx` for the consolidated portfolio) that the **Refresh Prices** button keeps up to date
  *in place* – without regenerating the file;
* an optional **view filter** that hides fully closed positions and dust cash balances (< 0.5) from the dashboard
  lists, the workbook and the CSV files – without changing a single total (§12);
* the **performance of the whole lifespan, day by day** – value, deposits, gain, return, CAGR, allocation and the
  VUAA comparison from the first transaction to today, rebuilt from the transactions and the daily prices instead
  of from the moments you pressed *Refresh* (§15);
* a **portfolio analysis** below the performance charts: a **donut chart** of the allocation by asset class, stock type,
  industry sector, currency or single holding, the concentration of the portfolio and your own classification of every
  holding (§16), and the **risk and risk‑adjusted return metrics** that were missing – annualised volatility, maximum
  drawdown, Sharpe, Sortino and Calmar ratios, Value at Risk, beta and alpha against the benchmark – with drawdown,
  rolling‑volatility and return‑distribution charts (§17); both are also in the workbook (sheets `Allocation`, `Risk`,
  `RiskData`) and in the CSV files;
* a **ticker mapping** (IBKR ticker → Yahoo Finance symbol, saved permanently, §13) and a **manual price history**
  (paste, upload or type `Date, Price`) for a stock Yahoo does not know, which flows into every valuation next to the
  downloaded prices (§14);
* a local **archive** (SQLite) of every statement, downloaded price/FX series, your manual prices and your settings, so
  history survives restarts and old statements can be combined with new ones.

Everything runs on your own computer. The only network access is the (optional) download of daily closing prices
from Yahoo Finance – and, when you press the button, the lookup of the sector of your stocks (§16).

---------------------------------------------------------------------------------------------------------------

## 1. Quick start

Requirements: **Python 3.10 – 3.13**. LibreOffice is optional (see §8).

| System | Start |
|---|---|
| Windows | double‑click `run_app.bat` |
| macOS / Linux | `./run_app.sh` |
| any | `pip install -r requirements.txt` then `streamlit run app.py` (or `python app.py`) |
| VS Code + Docker | *Dev Containers: Reopen in Container*, then **F5** – nothing else to install, see §11.1 |
| Docker (no VS Code) | `docker compose up --build`, then open `http://localhost:8501` – nothing else to install, see §11.2 |

The launch scripts create a private virtual environment in `.venv` on the first start, install
`requirements.txt` and open the app in your browser (`http://localhost:8501`; set `IBKR_TRACKER_PORT` for another
port). The server listens on this computer only (`.streamlit/config.toml`). With `streamlit run app.py` open the
address it prints yourself. All start options use the same data folder, `./data` (§7), so you can switch between
them at any time.

1. **Import Activity Statements** in the sidebar (CSV and/or HTML; IBKR Client Portal → *Performance & Reports →
   Statements → Activity*, custom date range). Drop several files at once – also from several accounts;
   re‑uploading a file changes nothing.
2. Choose the **Account / view**: one account, or **∑ All accounts (consolidated)** (see §6). The choice is
   remembered.
3. Press **🔄 Refresh Prices** – downloads only the missing prices / FX rates, recalculates everything and (when the
   toggle is on) updates the workbook.
4. **Export results as Excel file** (sidebar toggle, on by default) – the workbook is written to
   `data/output/IBKR_Portfolio_<account>.xlsx` (`IBKR_Portfolio_ALL.xlsx` for the consolidated view). Change the
   folder under *Settings → Output folder*. The *Excel export* tab has the *Download* button for the workbook and the
   CSV files of the same tables.
5. **Hide closed positions & dust balances** (sidebar toggle below the export toggle, off by default) – leaves
   positions with zero shares and cash balances below 0.5 out of the dashboard tables and chart, the workbook and the
   CSV files; totals, returns and the reconciliation are not affected (§12).

A ready‑made example built from the two statements you supplied is in
`examples/output/IBKR_Portfolio_U24013787_sample.xlsx` (see the limitation about prices in §9).

**The dashboard** shows, from top to bottom: total value, net deposits, return and CAGR in USD; the same in your
base currency; stocks, cash, "what the same purchases would be worth in VUAA" and the **VUAA CAGR (USD)**; a small
*Benchmark comparison · details* card (**Stocks minus VUAA (USD)**, stocks CAGR on the same basis, VUAA return,
average years since the purchases); in the consolidated view an **Accounts** table; then positions, cash balances and
the allocation bar chart (with the view filter on, closed positions and dust balances are not listed there – a one‑line
note says what was left out); then **Performance since the first transaction** (the day‑by‑day charts of §15) and, at
the bottom, **Portfolio analysis** with the two tabs *Allocation breakdown* (donut chart, §16) and *Risk & return*
(§17). Problems with a ticker or a missing price are solved in the *Market data* tab (§13, §14).

## 2. What the workbook contains

Sheet **Portfolio** (all sections are real cell ranges, headers bold with borders, statement data blue,
formulas black, cross‑sheet look‑ups green, yellow = cells you may overwrite):

| § | Where | Content |
|---|---|---|
| 1 | `A:H` from row 1 | **Stock transactions** – Name, Ticker, Quantity, Price, Total, Fee, Currency, Date. Chronological; sells have negative quantity/total; currency from the statement sub‑header; `Total = Quantity × Price` |
| 2 | `A:H`, 5 empty rows below §1 | **Dividend payouts** – Name, Ticker, Quantity, Dividend, Total, Withholding Tax, Currency, Date. Dividend, tax and fee lines of one payment are merged by date; fees are added to the tax cell |
| 3 | `A:E`, 5 empty rows below §2 | **Deposits & withdrawals** – Description (deposit / withdrawal / monthly fee / interest), Amount, USD.HUF (closing rate of the day), USD Amount, Date (+ helper columns `F:G`) |
| 4 | `A:G`, 5 empty rows below §3 | **Forex** – Currency Bought, Quantity, Price, Total, Fee, Currency Paid, Date (+ `H` fee currency) |
| 5 | `J:P`, rows of §1 | Gross (`=Total+Fee`), Current Price, Current Total (`=Quantity × Current Price × share factor`, see §5), USD Total, USD Gross, USD Current Total, **VUAA Quantity** (`=USD Total / VUAA close on the purchase date`) (+ helpers `R:S`) |
| 6 | `J:Q`, 5 empty rows below §5 | **Positions** – Ticker, **Quantity (shares according to the Open Positions section of the newest statement)**, Current Price, Value, Return (`=Value/Gross`), USD Value, USD Return, CAGR (left empty as specified) (+ helpers `R:T`: currency, sum of the transactions, share factor) |
| 7 | `J:O`, directly below §6 | **Cash balances** – base currency, USD, EUR and every traded currency; ties to IBKR's *Ending Cash*. With the view filter on (§12) Sections 6 and 7 leave out closed positions and balances below 0.5 |
| 8 | `J:Q`, 1 empty row below §7 | **Summary (USD)** – `O` Σ USD values, `P` = `O / Σ USD Amount (§3)`, `Q` time‑weighted CAGR |
| 9 | `J:Q`, 1 empty row below §8 | **Summary (base currency)** – `M` = §8 `O` × latest USD/base rate, `N` return, `Q` CAGR |
| 10 | below §9 | *Extra:* **benchmark block** – the same USD amounts invested in VUAA, value today, **Stocks minus VUAA (USD)**, average years since the purchases, **VUAA CAGR (USD)** and the stocks' CAGR on the same basis |

Other sheets: **Prices** (latest prices with the Yahoo symbol and the source of each price – including your
aliases and manual prices –, FX, the settings that all formulas read, yellow *Override* columns),
**FX** and **Benchmark** (daily history used for purchase‑date conversions), **History** (one row per day of the whole
lifespan with two charts – §15; values, not formulas), **Allocation** (donut charts by asset class, stock type, industry
sector and currency, the concentration figures and the classification of every holding – §16; the USD values are
formulas that read Sections 6 and 7), **Risk** and **RiskData** (volatility, drawdown, Sharpe / Sortino / Calmar, Value at
Risk and the figures against the benchmark as formulas over the daily observations, with two charts – §17), **Checks**
(reconciliation with the statement: cash per currency, share counts, NAV), **About** (index with links, conventions,
warnings, data‑coverage notes, statements included) and a hidden `_state` sheet (layout version, archive signature,
snapshot of your overrides).

In the **consolidated workbook** (`IBKR_Portfolio_ALL.xlsx`, see §6) the records of Sections 1–4 carry a ninth
column, **Account** (`I`), saying which account each line comes from; everything else is laid out identically.

Named ranges (`Trade_Qty`, `Dep_USDAmt`, `BaseCcy`, `AsOfDate`, `Px_Used`, `FX_Table` …) keep the formulas readable.
Only classic functions are used (`SUMIFS`, `INDEX/MATCH`, `IFERROR`, `SUMPRODUCT` …) – no dynamic arrays or
`XLOOKUP`, so the file is not tied to Microsoft 365 and also opens in LibreOffice.

## 3. Refresh Prices – what happens

1. New daily closes are requested **incrementally** (from a week before the last cached day; the first run goes back
   to two weeks before your first transaction). Today's unfinished bar is dropped. Pence quotes (GBp) are converted.
2. Priority of the numbers: **your single‑price override > your manual price history (§14) > Yahoo Finance (cache) >
   closing price printed in the statement** – day by day for the history, and the newest point of the merged series is
   the current price. The FX rates and the benchmark are requested **first**, then the stocks one by one; a failing
   symbol never stops the others, and after three consecutive failures without a single success the app gives up
   quickly (offline) and keeps working with the cached / statement / manual data. Stocks set to *manual prices only*
   (§13) are never requested.
3. The existing workbook is updated **in place**: only `Prices`, `FX`, `Benchmark`, `History`, `Allocation`, `Risk`,
   `RiskData`, `About` (and the hidden state) are rewritten – every formula, format and comment of `Portfolio` and
   `Checks` stays exactly as it is and recalculates. If the statement archive changed (new transactions, other layout version) or you switched the view
   filter (§12) the workbook is regenerated and the previous file is kept as `…previous.xlsx`. The file is written atomically; if Excel has it open you get a
   clear message and nothing is changed. The consolidated workbook is refreshed the same way.
4. A small value snapshot is still stored at every refresh, separately for every account and for the consolidated
   view (kept for the archive). The charts no longer depend on it: since 1.3.0 the history is rebuilt for every day
   from the first transaction (§15).

**Overrides.** Type a price / FX rate / benchmark price into a yellow cell of the `Prices` sheet *or* into the app's
*Market data* tab. Edits made in Excel are adopted by the app at the next refresh (three‑way merge with the
snapshot in `_state`; an edit in the file wins, clearing a cell removes the override).

**Settings** (export toggle, view filter, price source, benchmark symbol, days per year, base currency of the
consolidated view, CSV format, output folder, overrides, ticker mapping, your asset class / sector classification, the
risk‑free rates, last account) and your **manual prices** are stored in `data/tracker.db` and restored at the next
start. The sectors looked up at Yahoo Finance are cached there as well.

## 4. Conventions and interpretation choices

* **FX quotes** are "units of currency per 1 USD" (`USD.HUF ≈ 320`, Yahoo `HUF=X`). Converting to USD therefore
  **divides**: `USD Amount = Amount / USD.HUF`. (Multiplying a HUF amount by ~320 would be meaningless; the About sheet
  says the same.) The rate of a date is the last close **on or before** that date (Excel `MATCH(…,1)`).
* **Fees and taxes** are stored as **positive costs** (IBKR prints them negative): `Gross = Total + Fee`. For a sell,
  Total is negative, so Gross is the negative of the net proceeds. Dividend fees (e.g. ADR fees) are added to the
  *Withholding Tax* cell. Forex commission is shown in the account's base currency (`Fee Currency`).
* **Section 5, column O** (USD Current Total) converts at the **purchase‑date** rate, exactly as specified – it
  isolates the price performance from currency moves. Sections 6, 8 and 9 use the **latest** rate.
* **CAGR** = `Return ^ (1 / years) − 1`, *years* = deposit‑weighted average time since the deposits (weights = USD
  amounts) measured to the **valuation date** (`Prices!B6`, the date of the newest price – not `TODAY()`, so numbers
  do not drift) divided by `Prices!B7` (365 days). **Interest** rows are listed in Section 3 but are not
  contributions.
* **VUAA CAGR (USD)** – every stock transaction is replayed into the benchmark ETF at that day's close (Section 5,
  column P), so the benchmark position is worth `VUAA value` today for the same `invested` USD. `VUAA CAGR =
  (VUAA value / invested) ^ (1 / years) − 1`, where *years* is the **purchase‑weighted** average time from each stock
  transaction (weight = its USD Total, sells count negatively) to the valuation date ÷ 365. **Stocks CAGR (USD, same
  basis)** uses the same *years* and the same invested amount, so the two percentages are directly comparable;
  **Stocks minus VUAA (USD)** is the difference of the two USD values.
* **Merging statements**: transactions of one account are de‑duplicated across overlapping files (multiplicity aware –
  two genuine identical fills are not collapsed). IBKR's **HTML** statements round amounts to cents, **CSV** keeps full
  precision; if both cover the same transactions, the CSV values are kept. A mixed archive is reconciled against the
  newest statement (Checks sheet; cash tolerance 0.01 + 0.0051 per rounded amount).
* **Dividend quantity** comes from IBKR's dividend‑accrual table, else from the position on that day.
* Only **stocks/ETFs** (IBKR asset category *Stocks*) and forex conversions are processed. Other asset classes
  (options, futures, bonds, …) are listed as warnings and ignored – cash and positions may then differ from IBKR.

## 5. Share counts: the Open Positions rule

Stock splits, reverse splits, spin‑offs, mergers and transfers change the number of shares you hold (and the price per
share) without a trade, and the "Corporate Actions" section of a statement describes them in many different shapes.
Adding up the buys and sells therefore gives wrong share counts as soon as such an event happened. The app does
**not** use the *Corporate Actions* section or the transaction sums for the number of shares. Instead:

* **The *Open Positions* section of the statement is the source of truth.** The share count of every stock is the
  quantity printed there; the transactions only supply prices, dates, costs and the performance history.
* **Several statements of one account → the chronologically latest one counts** (newest period end; on a tie the later
  period start, then the CSV – full precision – over the HTML, then the file name). Its *Open Positions* section is
  applied; the older statements still contribute their transactions, deposits, dividends and cash.
* **Fallbacks** (each one is reported as a warning in the app and on the About sheet):
  1. the latest statement has no *Open Positions* section but its *Net Asset Value* shows no stocks → no holdings;
  2. otherwise the newest older statement that has the section is used, plus the trades dated after its period end;
  3. no statement has the section → the sum of the transactions (the old behaviour).
* **A difference between the statement and the transaction sums is information, not an error.** The Checks sheet and
  the app's *Reconciliation* tab get one row per stock, *Share count: statement vs transactions* (status **OK** when
  they agree, **INFO** when they differ), and a note – shown in the *Reconciliation* tab and on the About sheet – says
  e.g. "AAPL (USD): 12 in the statement, 6 from the transactions – a stock split, transfer or history before the first
  imported statement. The statement count is used." Section 6 of the workbook and `positions.csv` show both numbers
  (*Quantity (transactions)* / column `S`, and the *Share factor* / column `T`).
* **Valuing the individual transactions after a split.** Column *Current Total* of Section 5 multiplies each lot by
  the **share factor** of its stock (statement shares ÷ sum of the transactions; 1 when they agree), so the lots add
  up to the value of the position. The total per stock is exact; the split of it among the lots is proportional.
* A stock that appears in *Open Positions* but has no transaction in the archive (shares transferred in, earlier
  history not imported) still gets its Section 6 row and is valued.

## 6. All accounts: the consolidated view

The sidebar selector **Account / view** keeps listing every account on its own (nothing changed there) and offers
**∑ All accounts (consolidated)** on top. That view merges all accounts into **one unified portfolio**:

* **Positions** – the share counts of all accounts are added up per stock (Open Positions rule of §5 applied per
  account first); one row per stock and currency.
* **Cash** – the balances per currency are added up. **Transactions** – stock trades, dividends, deposits &
  withdrawals and forex of all accounts form one chronological history; every record keeps its account (`Account`
  column in the app's tables, in column `I` of the workbook and as the first column of the CSV files). Nothing is
  de‑duplicated *between* accounts: two accounts that bought the same shares on the same day each own them.
* **Performance** – value, net deposits, return, CAGR (USD and base currency) and the VUAA simulation are computed
  from the merged records exactly like for a single account, so every figure is additive across accounts where it can
  be (total value, deposits, stocks, cash).
* **Base currency of the consolidated view** – the common base currency of the accounts, or **USD** when they differ.
  Change it under *Settings → Base currency of the consolidated view* (a 3‑letter code; it needs an FX rate – the
  app asks you to press *Refresh Prices* or to type a rate when it is missing).
* **Accounts table** on the dashboard – per account: base currency, statement date, stocks, cash, total value (USD)
  and its share of the total, net deposits, return, CAGR, total value in the account's own base currency and the number
  of trades. All accounts are valued with the same prices and FX rates.
* **Reconciliation** – the cash balances are compared with the sum of the accounts' *Ending Cash*; in addition every
  account is reconciled on its own against its own newest statement (*Reconciliation* tab). The NAV is compared only
  when all accounts have the same base currency. A warning says when the newest statements of the accounts end on
  different days.
* **Export** – *Excel export* tab:
  * **Excel**: `IBKR_Portfolio_ALL.xlsx`, the same formula‑driven workbook as for one account (Sections 1–10, Prices,
    FX, Benchmark, Checks, About) plus the *Account* column, refreshed in place by *Refresh Prices*;
  * **CSV**: *Download all tables (.zip of CSV files)*, single files, or *Save the CSV files to the output folder*
    (`IBKR_Portfolio_ALL_csv/`): `summary.csv`, `positions.csv`, `cash.csv`, `stock_transactions.csv`,
    `dividends.csv`, `deposits_withdrawals.csv`, `forex.csv`, `accounts.csv` (per‑account breakdown) and
    `checks.csv`, plus – when there is something to analyse – `allocation.csv`, `holdings.csv`, `risk_metrics.csv` and
    `risk_daily.csv` (§16, §17; the consolidated portfolio is analysed as one). UTF‑8 with byte order mark. **CSV format**: *International* (comma separated, decimal point – pandas,
    R, Google Sheets) or *European Excel* (semicolon separated, decimal comma – what a double click in Excel needs
    under a Hungarian, German, French … regional setting). The single‑account views export the same files.

## 7. Project layout

```
app.py                     Streamlit GUI (sidebar, dashboard, transactions, reconciliation, market data, archive, export)
run_app.bat / run_app.sh   launchers (create .venv, install requirements, start the app)
ibkr_tracker/
  parsing/                 CSV + HTML readers -> one common record model (csv_reader, html_reader, extract …)
  portfolio.py             merges statements chronologically per account, de-duplicates, Open Positions share counts,
                           consolidate() = all accounts as one portfolio
  calc.py                  Python implementation of every number the workbook computes (oracle + dashboard)
  history.py               day-by-day reconstruction of the whole lifespan (value, deposits, return, CAGR, benchmark)
  allocation.py            classification of the holdings (asset class, stock type, sector) and the weights per grouping
  analytics.py             time-weighted daily returns -> volatility, drawdown, Sharpe / Sortino / Calmar, VaR, beta, alpha
  reconcile.py             cash / share counts / NAV versus the statement(s)
  market/                  Yahoo provider (prices, sector profiles), SQLite cache, statement-derived fallback, symbol rules,
                           merged price series, mapping.py (ticker aliases), manual.py (the Date, Price reader),
                           service.py (refresh / build / sector lookup)
  excel/                   builder (workbook), refresh (in-place update / rebuild), overrides, recalc (cached values),
                           analysis_sheets.py (Allocation, Risk, RiskData sheets with their donut / line charts)
  exports.py, tables.py    CSV export (international / European dialect) and the tables behind it and the GUI
  storage.py, tracker.py   archive + settings (SQLite) and the service layer used by the GUI
scripts/make_sample_workbook.py   regenerates examples/output/*.xlsx offline from examples/statements
tests/                     632 tests (pytest)
.devcontainer/             VS Code dev container: Dockerfile, devcontainer.json, post-create.sh (see §11.1)
Dockerfile, docker-compose.yml, docker/entrypoint.sh, .env.example
                           plain Docker container: runs the app without VS Code or Python (see §11.2)
.dockerignore              allow-list for both images: statements, data and .git never enter an image
.vscode/                   F5 debug configuration, tasks (run the app, tests), recommended extensions
```

Data lives in `./data` (`tracker.db`, `output/`); set `IBKR_TRACKER_HOME` to use another folder.

## 8. LibreOffice (optional)

Excel always recalculates the workbook when it is opened. If **LibreOffice** is installed, the app additionally
calculates the file once and stores the results as *cached values*, so previewers that do not calculate (mail
previews, Quick Look, pandas, Excel's *Protected View*) show numbers too. Without LibreOffice the formulas are still
there – in *Protected View* click **Enable Editing** once and Excel fills in the numbers. The checkbox is under
*Settings → Embed calculated values*.

## 9. Known limitations – please read

* **Live Yahoo Finance access was not testable while this was developed** (the build environment has no access to
  Yahoo). The download code is covered by tests with a deterministic fake provider and synthetic Yahoo‑style responses,
  and falls back gracefully, but the first *Refresh Prices* on your computer is the real test. If it reports problems,
  open *Market data* – you can give a stock its own Yahoo symbol (§13) or add its prices yourself (§14).
* The example workbook in `examples/output` was therefore generated **offline**: prices/FX come from the statements
  (period‑end close), the VUAA columns – and with them the VUAA CAGR – are blank until a refresh has downloaded
  benchmark prices.
* The Excel formulas were verified by recalculating the workbook with **LibreOffice** (cell by cell against the
  Python implementation), not with Microsoft Excel itself.
* Everything is derived from the statements you import: import every statement from your first deposit onwards,
  otherwise deposits, cash and the CAGR are incomplete (the *Reconciliation* tab shows when cash or positions
  no longer match IBKR).
* The share counts are as current as the **newest statement** of each account: trades after its period end appear
  only when you import a newer statement. Section 5 describes the fallbacks for statements without an *Open
  Positions* section.
* In the consolidated view the accounts are valued with the same prices and rates and combined through USD; accounts
  with different base currencies are shown in USD (or the base currency you choose) – there is no per‑account
  base‑currency performance in the merged totals (the Accounts table lists each account's total in its own base
  currency).
* Positions in currencies other than your base currency need an FX rate: the app takes it from the statements
  (forex trades / closing rates) or from Yahoo; otherwise it asks for a manual rate.
* The **daily history** (§15) is a reconstruction. It values every day with today's share counts (corrected by the
  Open Positions rule) and the split‑adjusted closes of that day – exactly right for splits, but it assumes that the
  prices you enter by hand are split‑adjusted as well. Days without a price are valued at the stock's last purchase /
  sale price and flagged; a stock a provider has never heard of needs a symbol (§13) or manual prices (§14) for a
  meaningful curve. Intraday timing, accrued interest and a position that was transferred out are not modelled.
* The **risk metrics** (§17) are computed from that history, so they need **daily prices**: the example workbook was
  built offline (statement closes only), its *Risk* sheet therefore says that prices are missing – after the first
  *Refresh Prices* on your computer it fills in. Short windows give unstable figures (fewer than 20 observation days: no
  ratios; fewer than 60: indicative), and a historical Value at Risk is not a forecast. The sector names come from Yahoo
  Finance (looked up on request, untested against the live service like the price download); IBKR itself provides none,
  so until you look them up or type them in, a stock is *Unclassified* in the sector view.

## 10. Tests

```
pip install -r requirements-dev.txt
python -m pytest            # about a minute; browser test is skipped unless Playwright + Chromium are installed
RUN_DOCKER_TESTS=1 python -m pytest tests/test_docker.py   # optional: also builds the image (§11.2) and starts it
```

Highlights: parsing of both formats against the same statement, merge / de‑duplication, a synthetic account with
hand‑computed ground truth (sells, withdrawal, fee, interest, EUR stock, EUR base, forex commission, USD base, a
2:1 stock split), the Open Positions rule (latest statement wins, every fallback, split scenario in Python, workbook
and checks), the consolidated view (merged records, summed share counts and cash, additivity of the metrics,
base‑currency choice, per‑account reconciliation, workbook with the Account column, CSV in both dialects, GUI),
the view filter (the two rules and their boundaries, display‑only totals, tables / CSV / workbook with a fully closed
position and a dust balance, empty sections, rebuild when the setting is switched, consolidated view, GUI),
cell‑by‑cell comparison of the LibreOffice‑recalculated workbook with the Python numbers, layout/spacing rules of
the specification, in‑place refresh and override merging, locked files, the GUI (Streamlit `AppTest` and an optional
real‑browser smoke test), a check of the totals against IBKR's own *Open Positions*, *Cash Report* and NAV, and
consistency checks of the dev container configuration (port, build context, line endings, …).

The three 1.3.0 features have their own test files: `test_history.py` replays the synthetic account day by day against
a ledger written out **by hand** (every one of its 356 days: stocks, cash, total, deposits at the rate of their day,
return, CAGR, the benchmark replay, splits, an opening balance, missing / stale / manual prices, several accounts,
independence from refreshes), `test_history_exports.py` (Excel *History* sheet cell by cell, charts, in‑place refresh,
a workbook of version 1.2 gaining the sheet, `history.csv`, a LibreOffice cross‑check of the last day against the
`Portfolio` formulas), `test_manual_prices.py` (every date and number format, decoding, error reporting, storage,
persistence, priority and merging in the valuation, the benchmark, the workbook) and `test_mapping.py` (symbol rules,
edits, the mapping file, symbol test, persistence, targeted download, wrong‑currency protection, the workbook); the
screens are exercised with Streamlit's `AppTest`.

The 1.4.0 features (§16, §17) have five more: `test_analytics.py` checks every risk figure against an independent
hand / numpy calculation on a hand‑built day‑by‑day history (observation days, flow‑adjusted returns, volatility,
downside deviation, maximum drawdown with its dates, Sharpe / Sortino / Calmar, VaR / expected shortfall, beta, alpha,
tracking error, the windows and the cases in which the figures are withheld); `test_allocation.py` (the classification
rules and their priority, the weights per grouping, negative balances, the *Other* merge, the concentration, the sector
names, the Yahoo profile lookup and its cache, failures included); `test_analysis_tracker.py` (the same through the
Tracker: the saved classification and risk‑free rates, the CSV files, the consolidated view); `test_gui_analysis.py`
(the section on the dashboard with `AppTest`) and `test_excel_analysis.py` (the *Allocation*, *Risk* and *RiskData*
sheets recalculated by LibreOffice and compared with the Python engine cell by cell, both currencies, with and without
risk‑free rates, a USD‑only and a consolidated account, a workbook without prices, in‑place refresh of a 1.3 workbook).

The plain container (1.4.1, §11.2) has `test_docker.py`: it cross‑checks `Dockerfile`, `docker-compose.yml`,
`docker/entrypoint.sh`, `.env.example` and `.dockerignore` with each other and with the app (port, bind address, data
folder, build arguments and their defaults, what the build context lets through – and keeps out –, capabilities, line
endings, the README), validates the compose file with `docker compose config` where the Docker CLI is installed and –
opt‑in with `RUN_DOCKER_TESTS=1`, because it builds the image – starts the container and checks that it becomes healthy,
does not run as root and writes the imported archive as the host user.

## 11. Docker: development container and plain container

Two Docker setups are included. Both use the same stack – **Ubuntu 24.04, Python 3.12, LibreOffice Calc 24.2**, the one
the project is tested with – and neither needs anything on your computer except Docker.

| | For | Start | Files |
|---|---|---|---|
| **§11.1 Development container** | working on the code in VS Code: debugger (F5), tests, tasks | *Dev Containers: Reopen in Container* | `.devcontainer/` |
| **§11.2 Plain container** | just *using* the app – no VS Code, no Python | `docker compose up --build` | `Dockerfile`, `docker-compose.yml`, `docker/` |

Both keep the data in `./data` – the folder `run_app.sh` / `run_app.bat` use as well – so you can switch between them
at any time (just not run two of them on the same folder at the same time).

### 11.1 Development container (VS Code)

`.devcontainer/` is a ready‑made development environment: **Ubuntu 24.04, Python 3.12, LibreOffice Calc 24.2** and all
Python packages (runtime and `pytest`) – the stack the project is tested with. Nothing has to be installed on your
computer except Docker and VS Code.

1. Install **Docker** (Docker Desktop on Windows / macOS, Docker Engine on Linux) and the VS Code extension
   **Dev Containers** (`ms-vscode-remote.remote-containers`; VS Code suggests it when it opens the folder).
2. Open the project folder in VS Code and run **Dev Containers: Reopen in Container** (Command Palette). The first
   build creates an image of about 2 GB and takes a few minutes; later starts take seconds.
3. Press **F5** (*Run and Debug → Streamlit: IBKR Portfolio Tracker*): the app starts under the debugger and the
   browser opens at `http://localhost:8501`. Or type `./run_app.sh` in the VS Code terminal.
4. **Terminal → Run Task → Tests: all** (or `python -m pytest`) runs the test suite; the *Testing* view lists the tests
   too. More tasks: *Tests: fast (without the browser test)*, *Sample workbook: rebuild (offline)*.

Good to know

* **Your data stays on your computer.** The project folder is mounted into the container, so `data/` (statement archive,
  settings, workbooks) is an ordinary folder next to `app.py` and survives *Rebuild Container*. The app listens on
  `127.0.0.1` inside the container and VS Code forwards port 8501 to *your* computer only – nothing is published on the
  network. Do not run it in GitHub Codespaces with real statements: that would put your account data on a cloud machine.
* The Python packages live in `/opt/venv` inside the image, not in the folder, so a `.venv` that `run_app.bat` /
  `run_app.sh` created next to the project (on Windows or macOS) never gets in the way. In the container `run_app.sh`
  uses that environment (`IBKR_TRACKER_VENV`) instead of creating `.venv`. After changing `requirements*.txt` run
  *Dev Containers: Rebuild Container*.
* **Build options** – `.devcontainer/devcontainer.json` → `build.args`: `INSTALL_LIBREOFFICE` (default `"true"`, about
  0.8 GB: cached values in the workbook and the tests that recalculate it) and `INSTALL_PLAYWRIGHT` (default `"false"`;
  `"true"` adds Chromium, about 0.7 GB, for the real‑browser smoke test). Change the value, then *Rebuild Container*.
* **Windows:** for fast file access keep the project in the WSL 2 file system (for example `\\wsl$\Ubuntu\home\you\…`)
  and open it from there. `.gitattributes` keeps the shell scripts at LF line endings – a CRLF checkout would break them.
* The container needs internet access for the Yahoo Finance download (and for the first build); behind a company proxy
  configure it in Docker Desktop.

### 11.2 Plain container (docker compose)

Runs the finished app in a container – for using the tracker without installing Python, LibreOffice or VS Code. The
image holds the program only; the statement archive and the workbooks stay in `./data` on your computer. Statements are
imported the usual way in the browser (the browser uploads them – there is nothing to mount).

1. Install **Docker** – Docker Desktop on Windows / macOS; on Linux Docker Engine with the Compose plugin
   (`docker compose version` has to work).
2. Open a terminal in the project folder (the one with `docker-compose.yml`) and run

   ```
   docker compose up --build
   ```

   The first build creates an image of about 2 GB (Ubuntu, the Python packages, LibreOffice) and takes a few minutes;
   later starts take seconds. When the log shows `URL: http://localhost:8501`, open that address in your browser.
   (The log always names the port *inside* the container; if you changed `IBKR_TRACKER_PORT`, use that one.)
3. **Ctrl+C** stops it. In the background: `docker compose up -d`, then `docker compose logs -f` follows the log and
   `docker compose ps` shows `healthy` as soon as the app answers. `docker compose down` removes the container – your
   data stays – and `docker compose down --rmi all` removes the image as well. After replacing the project folder by a
   newer version run `docker compose up --build` again, so that the image is rebuilt.

Without compose, the same in two commands (PowerShell: `-v "${PWD}/data:/data"`):

```
docker build -t ibkr-portfolio-tracker .
docker run --rm -p 127.0.0.1:8501:8501 -v "$(pwd)/data:/data" ibkr-portfolio-tracker
```

**Settings** (all optional) live in a file named `.env` next to `docker-compose.yml` – copy `.env.example` and remove
the `#` in front of a line:

| Variable | Default | Meaning |
|---|---|---|
| `IBKR_TRACKER_PORT` | `8501` | port of your computer the app opens on, e.g. `8502` while the dev container uses 8501 |
| `IBKR_TRACKER_BIND` | `127.0.0.1` | address the port is published on – see *Privacy* below |
| `TZ` | `UTC` | time zone of the container; the workbook's *generated* / *last refresh* times are local times – use your zone, e.g. `Europe/Budapest` |
| `INSTALL_LIBREOFFICE` | `true` | build option: `false` makes the image about 1 GB smaller; Excel then calculates the workbook when you open it (§8) |
| `APP_UID`, `APP_GID` | `1000` | build option: user / group id of the app user in the image – rarely needed, see *File ownership* below |

A new port, address or time zone applies with `docker compose up -d`; the build options with `docker compose up -d --build`.

Good to know

* **Your data stays on your computer, in `./data`**: `tracker.db` and `output/` with the workbooks (the *Download* button
  of the *Excel export* tab works as usual). It is the folder `run_app.sh` / `run_app.bat` and the dev container use,
  too. The image contains no statements and no data; `docker compose down` and rebuilds never touch the folder – back it
  up by copying it. Leave **Settings → Output folder** empty: a path of your computer does not exist inside the
  container, and the workbooks belong in `./data/output`.
* **Privacy: the port is published on this computer only** (`127.0.0.1`). The app has no login and shows your whole
  portfolio – change `IBKR_TRACKER_BIND` (to `0.0.0.0`, say) only if you really want everybody in your network to see
  it. Inside the container the app runs as an unprivileged user with all Linux capabilities dropped and
  `no-new-privileges`; `.dockerignore` is an allow‑list, so no statement, database, `.env` or `.git` folder can enter an
  image. (`.streamlit/config.toml` keeps the app on `127.0.0.1` for the other start options; in the container
  environment variables let it listen on all interfaces of the *container* – otherwise the published port could not
  reach it – and the port mapping decides who may connect.)
* **It comes back with Docker.** `docker-compose.yml` says `restart: unless-stopped`: a container started with
  `docker compose up -d` is started again whenever Docker starts (after a reboot, say) until you run
  `docker compose stop` or `docker compose down`. Delete that line if you do not want this.
* **File ownership (Linux).** `docker/entrypoint.sh` starts as root for a moment, makes `./data` usable and then runs the
  app as an unprivileged user – as the *owner of the folder*, so the files the app writes stay yours. Create the folder
  first (`mkdir data`); if Docker creates it, it belongs to root and is handed over to user 1000. Docker Desktop
  (Windows, macOS), rootless Docker and Podman need nothing. If the log shows `WARNING: … not writable`, follow its
  advice; on SELinux hosts (Fedora, RHEL) write `./data:/data:z` in `docker-compose.yml`.
* **Dev container and plain container** both want port 8501: while the dev container is running put
  `IBKR_TRACKER_PORT=8502` into `.env` (or stop one of them). Do not let both work on the same `./data` at the same time –
  two programs writing one SQLite file through different file‑sharing layers.
* **Internet access** is needed for the build and for the Yahoo Finance download – normal Docker networking is enough;
  behind a company proxy configure it in Docker Desktop. Without it the app falls back to the statement prices (§3).
* **Windows:** `.gitattributes` keeps the Docker files at LF line endings (a CRLF checkout would break
  `docker/entrypoint.sh`; the build repairs that, too). As for the dev container, a project in the WSL 2 file system is
  faster than one on a Windows drive.

## 12. Hiding closed positions and dust balances

One switch – the sidebar toggle **Hide closed positions & dust balances** (off by default, remembered like the other
settings) – removes the clutter from every *list* of the app. It is a **view filter**: nothing is recalculated and no
figure changes.

| Rule | What is hidden |
|---|---|
| **Closed position** | a stock whose current share count is zero (Open Positions rule of §5) – it was sold completely |
| **Dust balance** | a currency whose cash balance has an **absolute amount below 0.5**, counted in that currency's own units (strictly below: exactly 0.50 stays visible). This includes the rows that are always listed – USD and EUR with 0.00 – and rounding residue such as 0.0000000005 |

**Where it applies**

* **Dashboard** – the *Positions* table, the *Cash balances* table and the *Allocation (USD)* chart. A small caption
  under the tables says how many items were hidden and what the hidden dust is worth in USD.
* **Excel workbook** – Sections 6 and 7 of the `Portfolio` sheet list only the remaining rows. The layout depends on
  the setting, so the workbook is **regenerated once when you switch it** (the previous file is kept as
  `…previous.xlsx`); *Refresh Prices* keeps updating it in place afterwards. A workbook made without the filter stays
  up to date as long as the filter is off. The *About* sheet says when the filter is on.
* **CSV files** – `positions.csv` and `cash.csv` without the hidden rows; `summary.csv` gets two extra rows
  (*Hidden closed positions*, *Hidden dust cash balances (included in the totals)*).
* **Consolidated view** – the filter looks at the merged figures: a stock that is closed in every account, a currency
  whose balance summed over the accounts is below 0.5.

**What is never hidden – on purpose**

* **Totals, returns, CAGR, the net asset value.** A closed position is worth nothing and a dust balance is small but
  real, so both stay in the KPI cards and in Sections 8–10. In the workbook the hidden balances are added up by a
  formula‑driven block on the `Checks` sheet (columns `I:K`, total named `Hidden_USD`); Section 8 is
  `=SUM(…)+Hidden_USD`, so the total is the same with and without the filter, and still live.
* **The transaction history** – Sections 1–5, the *Transactions* tab, `stock_transactions.csv`, `forex.csv` …: the
  trades of a closed position are history. Their *Current Total* in Section 5 is still computed at today's price
  (the share factor of such a lot is 1 because the trades add up to zero shares).
* **The reconciliation** – the `Checks` sheet, the *Reconciliation* tab and `checks.csv` keep a row for every currency
  and every position: a discrepancy must never disappear from view.
* **Market data** – the *Market data* tab and the `Prices` / `FX` sheets list every stock and currency (a closed
  position without a price is marked *not needed (position closed)*).

## 13. Ticker mapping – IBKR ticker → Yahoo Finance symbol

Prices are downloaded under a **symbol**. The automatic rule turns the IBKR ticker and listing exchange into the Yahoo
symbol (`NOVOB` on CPH → `NOVOB.CO`, `SAP` on IBIS → `SAP.DE`, `BRK B` → `BRK-B`) and fits most stocks. When Yahoo
Finance calls a stock differently – or the rule guesses the wrong listing – the download fails or returns nothing.
*Market data → **Ticker mapping*** fixes that permanently.

* The table lists every stock of the portfolio: ticker, currency, whether it is **held now**, the **automatic symbol**,
  **your symbol (alias)**, **manual prices only**, the **symbol used** and a **status** (not downloaded yet · ok, N
  prices, newest … · last download failed: … · quoted in another currency …), followed by the stock's name and the IBKR
  exchange the automatic symbol is derived from. Problems are marked `[!]`.
* Type the correct Yahoo symbol into **Your symbol (alias)** and press **Save mapping**. The changed symbols – and only
  those, not the whole refresh – are downloaded right away (untick *Download the prices of the changed symbols right
  away* to postpone it). Clearing the cell returns to the automatic symbol; typing the automatic symbol is no override.
* **Saved permanently.** The mapping is part of the settings in `data/tracker.db`: it survives restarts, applies to
  every account and the consolidated view, to every download and to the `Prices` sheet of the workbook (*Yahoo symbol*
  column), and it is never touched by *Clear the downloaded price cache*. Saving it while one account is open keeps the
  entries of the stocks that account does not hold.
* **Test a symbol, then use it.** Pick the stock, type a symbol and press *Test symbol*: the app asks Yahoo Finance for
  the last two weeks and tells you whether the symbol exists, its last close and the currency it is quoted in – with a
  warning when that is not the currency IBKR trades the stock in. *Use this symbol* saves it.
* **Protection against the wrong listing.** If the cached prices of a symbol are quoted in another currency than the
  one the stock trades in at IBKR (the Frankfurt listing of a US stock, pence versus pounds …), they are **ignored** and
  a note names the stock and the symbol – the stock keeps its statement price until you choose a better symbol.
* **Manual prices only.** Tick it for a stock Yahoo does not know at all: the stock is never requested, and its prices
  come from the *Manual price history* (§14). Until you enter some, the status says so.
* **Mapping file.** *Mapping file – export / import* saves the mapping as a small CSV (`Ticker, Currency, Symbol,
  Manual only`; comma, semicolon or tab separated; a byte order mark is fine) – a backup, something to edit in a
  spreadsheet, or a way to move to another computer. An imported file is **merged** into the saved mapping line by line;
  unusable lines are listed with their line number and the rest is applied. A line without *Currency* applies to the
  ticker in every currency (a line for `SAP, EUR` wins over one for `SAP`).
* A symbol may contain letters, digits and `. - = ^ _ &` (no blanks – Yahoo writes `BRK-B` for `BRK B`).

## 14. Manual price history – `Date, Price`

For a stock Yahoo Finance does not know – or only partly – *Market data → **Manual price history*** takes the closing
prices from you. It works for every stock of the portfolio and, with its own entry, for the **benchmark** (in the
benchmark's quote currency, USD for VUAA.L).

* **Three ways in**: *Paste text*, *Upload a file* (CSV / text / TSV) or *Type rows* in a small table. Every way ends in
  the same preview – how many prices, the first and last day, the first and last rows – before anything is saved.
* **Formats.** Two columns, `Date, Price`, in any of: `2026-01-02,125.40` · `02.01.2026;125,40` · `Jan 2, 2026<TAB>1,234.50`
  · a table copied from Excel · a Yahoo‑style download (`Date, Open, High, Low, Close, Adj Close, Volume` – the *Close*
  column is used) · a header in English or Hungarian, or none. Separator (comma, semicolon, tab, blank), decimal mark
  and date order are detected; *Options for unusual data* overrides them. Dates may be `2026-01-02`, `2026/01/02`,
  `02.01.2026`, `2 Jan 2026`, `January 2nd, 2026`, `20260102`, with a time, or an Excel serial number; `13/01/2026`
  proves day‑first for the whole file, and a file whose order is ambiguous says so.
* **Line by line diagnostics.** Unusable lines are listed with their line number and the reason (not a date, not a
  price, zero or negative, in the future, before 1970); you must tick *Skip the N unusable line(s) and save the rest*
  before the rest can be saved. A date that occurs twice keeps its last value; a **jump of more than ×1.5 between two
  consecutive prices** is flagged as a probable typing error or an unadjusted stock split.
* **Prices are in the stock's trading currency and split‑adjusted** (like Yahoo's *Close*) – the history multiplies them
  with today's share counts (§15).
* **Add or replace.** By default new data are added and a day that already exists is overwritten; *Replace all manual
  prices of this stock* starts over. The saved prices can be listed, downloaded as CSV and deleted (with a confirmation).
* **How they enter the valuation.** Per stock, the daily series is merged from the sources by priority – **manual >
  Yahoo Finance > the closing price printed in the statement** – so manual prices fill the days Yahoo lacks and **win on
  a day that both have**. The **newest point of the merged series is the current price**: a manual price newer than
  anything downloaded becomes the current price (source *Manual price history* in the `Prices` sheet and the *Market
  data* table), an older one only values the past. A single‑price **override** (the yellow cells, §3) still beats
  everything for the *current* value, but leaves the history untouched. Everything is used at once – dashboard,
  positions, performance history (§15), workbook, CSV – without a refresh.
* **Stored in the database**, so they survive restarts, *Refresh Prices* and *Clear the downloaded price cache*.
* A stock whose newest price is more than 10 days older than the valuation date gets a note (dashboard, *Reconciliation*
  tab, About sheet) – a manual series that was never extended shows up there.

## 15. Performance history since the first transaction

The dashboard section **Performance since the first transaction** (tabs *Value & deposits · Gain · Return · CAGR ·
vs. VUAA · Allocation*) covers the **entire lifespan** of the portfolio, day by day. Earlier versions could only chart
the moments at which *Refresh Prices* had been pressed; now the history is **rebuilt from the transactions and the
daily price series**, so it is complete from the first refresh on – and from the first transaction backwards in time.

* **Range.** Every calendar day from the first activity of the portfolio (the first transaction, deposit or dividend;
  the statement period when there is none) to the valuation date. *Currency* (base currency / USD) and *Period* (All,
  1Y, YTD, 6M, 3M, 1M) only choose what the charts show – the figures always start at the first transaction.
* **What one day is made of.**
  * **Cash** – the ledger of every cash movement per currency (trades, fees, dividends and taxes, deposits and
    withdrawals, interest, forex), up to and including the day – the same ledger that gives the cash balances –
    converted at that day's FX rate;
  * **Stocks** – the transactions up to the day, corrected by the Open Positions rule of §5 (splits, transfers, shares
    nobody bought in the archive count from the first day), times the day's close;
  * **Net deposits** – every deposit and withdrawal converted at the rate **of its own day**; *return* = value / net
    deposits, *CAGR* = return ^ (1 / years) − 1 with the deposit‑weighted average age **up to that day** (§4);
  * **Benchmark** – every purchase and sale replayed into VUAA at that day's close (the simulation of §4, day by day).
* **The last day is the dashboard.** Its value, deposits, return, CAGR and benchmark figures equal the KPI cards; the app
  checks it and shows a warning when they differ (only possible when a held stock has no price at all).
* **Annualising a young portfolio exaggerates**, so the CAGR charts and the CAGR columns start once the average
  deposit is three months old (the Excel and CSV files keep the raw numbers).
* **Missing data are handled in the open.** Weekends and holidays carry the last close forward. A day with no price
  at all is valued at the stock's last purchase / sale price; a price older than 10 days is carried forward and
  reported; a currency without FX rates is left out and reported; without benchmark prices the benchmark curve is
  omitted. *Data coverage of the history* (below the charts) and *Market data → **Price coverage*** list, per stock, the
  days held, the number of prices from Yahoo / your manual history / the statement, the first and last price date and
  a status – and point to the cure: a symbol (§13) or manual prices (§14).
* **Exports.** The workbook has a **History** sheet – one row per day with every figure above (and the value of every
  position in USD), two charts (portfolio value against net deposits; stocks against the VUAA replay) and the coverage
  notes on the *About* sheet; it is rewritten by every *Refresh Prices* and added to a workbook made by an earlier
  version at its next refresh. `history.csv` is part of the CSV files, and the dashboard has its own download button.
  The History sheet holds values, not formulas – it is data for charts and further analysis; the formula‑driven
  `Portfolio` sheet is unchanged.
* It does not depend on the view filter of §12 and is computed from the archive, the price cache and your manual
  prices only – no network access.

## 16. Allocation and donut charts

The dashboard section **Portfolio analysis** (below *Performance since the first transaction*) has the tab
**Allocation breakdown**: a **donut chart** of how the portfolio is split, with the same numbers in a table next to it.

* **Five groupings** (the *Group by* switch): **Asset class** (Stocks · ETFs & funds · Cash – or your own classes),
  **Stock type** (the instrument type of the statement: Common stock, ADR, ETF, REIT, Preferred stock …), **Industry
  sector**, **Currency** (the currency a stock is quoted in, and every cash balance) and **Holding** (every stock and
  every cash balance on its own).
* **What a slice is.** The share of the **positive USD values** of the stocks held now and the cash balances – the same
  prices, FX rates and view filter (§12) as the rest of the dashboard. The legend carries the weights, the hole the
  total, the tooltip the USD value. A **negative** balance (a margin loan) cannot be a slice: it is listed under the
  chart and left out of the 100 %. More than ten groups: the smallest are merged into *Other (n)* in the chart (the
  table lists every group with its number of holdings; `allocation.csv` names the holdings, largest first). The colours
  work in the light and the dark theme.
* **Concentration** (cards below the chart): number of stock positions, the largest position, the top 3 and top 5
  (shares of the whole portfolio, stocks + cash) and the **effective number of positions** `1 / Σ w²` of the stock
  weights (10 equally large positions = 10.0; one dominant position → close to 1).
* **Where the classes come from – and what they cannot know.** IBKR statements say whether an instrument is an ETF, an
  ADR, a REIT … but **nothing about its industry**. Therefore (highest priority first):
  1. **your own entry** (below);
  2. **asset class**: the statement's *Type* – an ETF / ETN / fund is *ETFs & funds*, every other instrument
     *Stocks*, a cash balance *Cash*; **sector**: the sector **Yahoo Finance** reports for the stock (Technology,
     Healthcare, Financial Services, Consumer Cyclical, Consumer Defensive, Industrials, Energy, Utilities, Real Estate,
     Basic Materials, Communication Services), *ETFs & funds* for a fund without a sector, *Unclassified* for a stock
     nobody classified yet – the *Industry sector* view says how large that share is.
* **Sector lookup is a button, not part of *Refresh Prices*.** *Classify your holdings → **Look up sectors (Yahoo
  Finance)*** asks Yahoo for the sector of every stock you hold and remembers the answer in the archive
  (`instrument_profiles`). Funds (they have no sector), stocks set to *manual prices only* (§13) and stocks to which you
  gave a sector yourself are not asked; a stock whose sector is already known is not asked again unless you tick *Ask
  Yahoo again for stocks that already have a sector*. The Yahoo symbol is the one of the ticker mapping (§13); a failing
  symbol is reported by name and never stops the others (three failures in a row without any answer end the lookup – the
  connection is probably down). It needs the *Yahoo Finance* price source (not the offline *statements only* mode) and
  internet access.
* **Your own classification.** In the same expander, the table lists every stock with its *Asset class* (a drop‑down of
  Stocks · ETFs & funds · Bonds · Commodities · Real estate · Crypto · Other – or type your own) and *Sector* (a text
  cell; the usual GICS names such as "Information Technology" or "Health Care" are matched to Yahoo's). **Save
  classification** stores the entries permanently in the settings (key `TICKER|CCY`); an entry equal to the automatic
  value is not kept, and clearing a cell returns to the automatic value. The workbook is updated right away when the
  Excel export is on.
* **Excel** – sheet **Allocation**: one block per grouping (asset class, stock type, sector, currency) with a table and a
  **native Excel donut chart**, the concentration figures and the **Holdings and their classification** table. The USD
  values are **formulas** that read the *USD Value* cells of Sections 6 and 7, the weights, group totals (`SUMIFS`) and
  the concentration are formulas too – so the charts follow every price refresh and the workbook's own total (a check
  line says when a block does not add up). Only the *labels* (asset class, stock type, sector) are values written by
  the app; change them in the app. **CSV** – `allocation.csv` (every grouping stacked: group, USD value, weight,
  holdings, members) and `holdings.csv` (the classification of every holding).

## 17. Risk and risk‑adjusted return metrics

The tab **Risk & return** of *Portfolio analysis* answers *"how bumpy was the road?"* – the CAGR of §4 says what the
deposits earned, but says nothing about the swings, and it depends on **when** money was paid in. The metrics below
therefore use the **time‑weighted daily returns** of the day‑by‑day history of §15, in which deposits and withdrawals
are taken out on the day they happen.

* **Observation days.** Monday–Friday days of the history plus the valuation date; a day on which nothing moved (a
  market holiday, a carried‑forward price, a day with only a deposit) is not an observation – its flow is added to the
  next one. The return of day *t* is `r_t = V_t / (V_prev + F_t) − 1` (`V` = total value of stocks + cash, `F` = net
  deposits since the previous observation, assumed to be at work all day). The wealth index is `Π(1 + r)`; the
  time‑weighted return is its last value minus 1. Everything is **annualised with 252 trading days**.
* **Controls.** *Currency* – the base currency (includes the currency effect: a falling dollar is a loss for a forint or
  euro investor) or USD; *Period* – All, 1Y, YTD, 6M, 3M, 1M (the volatility of the last three months is not the
  volatility of the lifespan); *Risk‑free rate (% p.a.)* – per currency, remembered, **0 by default**, so that the Sharpe
  family is then the plain ratio of return to risk. The benchmark (VUAA by default) is converted the same way.

| Metric | Definition |
|---|---|
| **Annualised volatility** | sample standard deviation of the daily returns × √252 |
| **Downside deviation** | √(mean of min(r − m, 0)²) × √252 over all observations, `m` = daily risk‑free rate `(1 + rf)^(1/252) − 1` |
| **Maximum drawdown** | the largest fall of the wealth index from a previous peak – with the peak, trough and recovery date (or *not recovered yet*); the **current drawdown** is today's distance from the peak |
| **Sharpe ratio** | `(mean(r) − m) / stdev(r) × √252` |
| **Sortino ratio** | `(mean(r) − m) / downside deviation × √252` |
| **Calmar ratio** | annualised return / \|maximum drawdown\| |
| **Annualised return** | `(1 + time‑weighted return)^(1 / years) − 1`, years = calendar days / days per year (§4); shown once the window is three months old |
| **Daily Value at Risk (95 %)** | the 5th percentile of the daily returns, as a positive loss: on 95 % of the days the loss was smaller (historical, 1 day) |
| **Daily expected shortfall (95 %)** | the average loss on the worst 5 % of the days |
| **Best / worst day, days with a gain** | extremes and share of positive days |
| **Beta · correlation** | `cov(r, r_b) / var(r_b)` and the correlation of the daily returns with the benchmark's on the same days |
| **Alpha (Jensen)** | `(mean(r − m) − β · mean(r_b − m)) × 252` |
| **Tracking error · information ratio** | `stdev(r − r_b) × √252` and `mean(r − r_b) × 252 / tracking error` |
| **Treynor ratio** | `(mean(r) − m) × 252 / β` |

* **On screen**: eight cards (volatility, maximum drawdown, Sharpe, Sortino, time‑weighted return, Calmar, beta, Value at
  Risk), the table of all metrics in four groups (*Return · Risk · Risk‑adjusted return · Against the benchmark*) with
  the benchmark in a second column and a one‑line explanation per metric, and three charts: **Drawdown**, **Rolling
  volatility** (30 trading days) and **Return distribution** (histogram with the Value at Risk line). A download button
  saves the table as CSV.
* **When the figures are withheld – in the open.** *No daily prices*: when less than 50 % of the stock value has a quote
  of the last few days (the portfolio is valued at carried‑forward / purchase prices, e.g. only statement closes), no
  metric is shown – volatility and drawdown of such a curve would be fiction – and the message says what to do (*Refresh
  Prices*, a symbol §13, manual prices §14). 50–90 %: shown with a warning (the swings are understated). *Fewer than 20
  observation days* in the window: only the return and the drawdown; fewer than 60: a note that the figures are
  indicative.
* **Excel** – sheets **Risk** and **RiskData**. `RiskData` is the observation table (date, value, net deposits and the
  benchmark price are values written by the app; the returns, wealth index, peak, drawdown, rolling volatility and the
  benchmark columns are formulas). `Risk` computes **every metric above with a formula over those columns** – only
  classic functions (`STDEV`, `PERCENTILE`, `SLOPE`, `CORREL`, `SUMPRODUCT` …), so the numbers can be followed cell by cell
  and checked against the app – plus two charts (drawdown, rolling volatility) per currency. The risk‑free rate cell is
  an input: type another rate to see its effect (the app writes the saved one again at the next update). Both sheets are
  rewritten by every *Refresh Prices*; a workbook made by an earlier version gains the three analysis sheets then.
  **CSV** – `risk_metrics.csv` (all metrics as numbers, per currency) and `risk_daily.csv` (the observation table).
* These are **historical** measures of the reconstructed history of §15 and share its assumptions (§9: share counts
  from the Open Positions rule, carried‑forward prices on days without a quote, no intraday timing). They describe the
  past and say nothing about the future.

## Changelog

**1.4.1**

* **Plain Docker container** (§11.2): `docker compose up --build` runs the app in a container – no VS Code, no Python and
  no LibreOffice on your computer needed – at `http://localhost:8501`. New files: `Dockerfile`, `docker-compose.yml`,
  `docker/entrypoint.sh`, `.env.example`; `.dockerignore` is now an allow‑list for both images. The data stays in
  `./data` (the folder the launch scripts and the dev container use), the port is published on this computer only, the
  app runs as an unprivileged user (on Linux it adopts the owner of `./data`, so the files stay yours) and the settings –
  port, time zone, build options – come from an optional `.env` file. A health check, a clean stop and a start‑up that
  does not wait for the internet are part of the image.
* **The VS Code dev container is unchanged** (`.devcontainer/`, `.vscode/` and the launch scripts are byte for byte the
  same as in 1.4.0); both ways of running the app in Docker are available side by side (§11).
* The application itself did not change – `app.py` and `ibkr_tracker/` differ from 1.4.0 only in the version number;
  every figure, setting and workbook sheet is as before (the sample workbooks were regenerated for the version stamp
  only). README: the image size figures of the dev container corrected. 42 new tests (632 in all).

**1.4.0**

* **Allocation and sector donut chart** (§16): new dashboard section *Portfolio analysis → Allocation breakdown* with a
  donut chart of the portfolio by **asset class, stock type, industry sector, currency or single holding**, the table
  behind it, the concentration of the portfolio (largest / top 3 / top 5 / effective number of positions) and a
  **classification editor** for your own asset classes and sectors. Sectors come from Yahoo Finance on request (button,
  cached in the archive – *Refresh Prices* is unchanged), because IBKR statements carry none.
* **Risk and risk‑adjusted return metrics** (§17): *Portfolio analysis → Risk & return* – annualised volatility,
  downside deviation, maximum drawdown (with peak, trough and recovery date), Sharpe, Sortino and Calmar ratios,
  annualised return, daily Value at Risk and expected shortfall, best / worst day, and – against the benchmark – beta,
  correlation, alpha, tracking error, information ratio and Treynor ratio. Computed from the **time‑weighted daily
  returns** of the full‑lifespan history (deposits and withdrawals taken out), in the base currency or in USD, for a
  period of your choice and with a risk‑free rate per currency; with drawdown, rolling‑volatility and return‑distribution
  charts. The figures are withheld, with the reason, when there are no daily prices or too few observation days.
* **Excel**: three new sheets – **Allocation** (native donut charts, formulas that read Sections 6 and 7), **Risk** (every
  metric as a formula, two charts per currency) and **RiskData** (the observation days) – written by the first
  *Refresh Prices* / export and kept up to date in place. **CSV**: `allocation.csv`, `holdings.csv`,
  `risk_metrics.csv`, `risk_daily.csv`; the risk table can also be downloaded from the dashboard.
* Nothing that existed changed: the `Portfolio` sheet (Sections 1–10), `Prices`, `FX`, `Benchmark`, `History`, `Checks`,
  every figure and every setting are exactly as in 1.3.0 – all 486 existing tests pass, three of them extended only to
  know the new sheets and the new tab. Workbooks of version 1.3 stay valid and gain the three sheets at their next
  refresh; the archive database is upgraded automatically (new table `instrument_profiles`, two new optional settings:
  the classification and the risk‑free rates). The *Risk* sheet of the example workbook is built from statement prices
  only – see §9 for what that means for the risk metrics. 104 new tests (590 in all).

**1.3.0**

* **Ticker mapping** (§13): *Market data → Ticker mapping* maps an IBKR ticker to the Yahoo Finance symbol that works,
  saved permanently in the settings and used by every download, account and workbook. Test a symbol before using it;
  targeted download of only the changed symbols; wrong‑currency protection; *manual prices only* for stocks Yahoo does
  not know; export / import of the mapping as a CSV file. The refresh now requests the FX rates and the benchmark
  first and the stocks afterwards, so a failing stock symbol can never abort a refresh.
* **Manual price history** (§14): paste, upload or type `Date, Price` for any stock or the benchmark – every common
  date and number format, line‑by‑line diagnostics, saved in the database. The manual prices are merged with the
  downloaded ones (manual > Yahoo > statement per day), set the current price when they are the newest, and value the
  stock on every day of the history. They survive *Clear the downloaded price cache*.
* **Full‑lifespan performance history** (§15): value, net deposits, gain, return, CAGR, allocation and the VUAA
  comparison for every day from the first transaction, rebuilt from the transactions, the daily prices and the daily
  FX rates (no longer from the refresh timestamps). New dashboard section with period and currency selectors, a data
  coverage report, the **History** sheet of the workbook (values + two charts, kept up to date in place), `history.csv`.
* The Excel `Prices` sheet says *MISSING - refresh, add manual prices or enter an override* for a stock without any price.
* Fixed: a pasted price line such as `13/01/2026,10` is no longer mistaken for a header line.
* Compatibility: workbooks of version 1.2 stay valid and gain the *History* sheet at their next refresh; the archive
  database is upgraded automatically (new table `manual_prices`, two new optional settings for the mapping); all
  existing settings keep their meaning.

**1.2.0**

* **View filter – hide closed positions and dust balances** (§12). One sidebar toggle hides stocks with zero shares and
  cash balances with an absolute amount below 0.5 from the dashboard (Positions table, Cash balances table, allocation
  chart), the Excel workbook (Sections 6 and 7) and the CSV files (`positions.csv`, `cash.csv`). It is display only:
  totals, returns, CAGR, the net asset value, the transaction history, the reconciliation and the market‑data tables
  are complete either way. The workbook keeps its totals through a formula‑driven helper (`Checks!I:K`,
  `Hidden_USD`) and is regenerated once when the setting is switched; workbooks made without the filter stay valid.
* A closed position without a price is no longer reported as missing (*Market data* tab: "not needed (position
  closed)"; no "No price available" warning), and neither is a stock whose price you typed in.
* The message after a workbook rebuild now names the real reason (statement archive changed, layout version, filter).
* Fixed: with no position left to list, *Stocks now – latest FX* in Section 10 of the workbook is 0 (it used to read the
  first cash row).

**1.1.0**

* **Open Positions is the source of truth for share counts** (§5): the *Corporate Actions* section and the raw
  transaction sums are no longer used for the holdings; with several statements of one account the chronologically
  latest *Open Positions* section is applied. Differences to the transaction sums are reported (Checks sheet, About
  sheet, dashboard) instead of silently changing the numbers. Workbook layout version 2: Section 6 gets the
  statement shares (values) plus helper columns `S` (sum of the transactions) and `T` (share factor); *Current Total*
  in Section 5 uses the factor. Existing workbooks are rebuilt once on the next export (the old file is kept as
  `…previous.xlsx`).
* **Consolidated all‑accounts view** (§6): merged positions, cash, performance and transaction histories, an Accounts
  table, per‑account reconciliation, the consolidated workbook with an *Account* column and CSV export (international
  or European dialect). The single‑account views are unchanged.
* **Dashboard**: the primary slot that showed *Stocks minus VUAA (USD)* now shows the **VUAA CAGR (USD)**; *Stocks
  minus VUAA (USD)* moved to the small *Benchmark comparison · details* card. Section 10 of the workbook gained the
  rows *Avg. years since the purchases*, *VUAA CAGR (USD)* and *Stocks CAGR (USD, same basis)*.

**1.0.1** – VS Code development container. **1.0.0** – first release.
