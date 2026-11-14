# IBKR Portfolio Tracker

Upload your **Interactive Brokers Activity Statements** (CSV and/or HTML, any number of files, any mix of
periods) and get

* a dashboard in the browser (Streamlit) with positions, cash, deposits, time‑weighted **CAGR** in USD and in your
  base currency, and a "what if I had bought the S&P 500 (VUAA) instead" simulation;
* a professionally formatted, **formula‑driven Excel workbook** (`IBKR_Portfolio_<account>.xlsx`) that the
  **Refresh Prices** button keeps up to date *in place* – without regenerating the file;
* a local **archive** (SQLite) of every statement, downloaded price/FX series and your settings, so history
  survives restarts and old statements can be combined with new ones.

Everything runs on your own computer. The only network access is the (optional) download of daily closing prices
from Yahoo Finance.

---------------------------------------------------------------------------------------------------------------

## 1. Quick start

Requirements: **Python 3.10 – 3.13**. LibreOffice is optional (see §6).

| System | Start |
|---|---|
| Windows | double‑click `run_app.bat` |
| macOS / Linux | `./run_app.sh` |
| any | `pip install -r requirements.txt` then `streamlit run app.py` (or `python app.py`) |

The launch scripts create a private virtual environment in `.venv` on the first start, install
`requirements.txt` and open the app in your browser (`http://localhost:8501`; set `IBKR_TRACKER_PORT` for another
port). The server listens on this computer only (`.streamlit/config.toml`). With `streamlit run app.py` open the
address it prints yourself.

1. **Import Activity Statements** in the sidebar (CSV and/or HTML; IBKR Client Portal → *Performance & Reports →
   Statements → Activity*, custom date range). Drop several files at once; re‑uploading a file changes nothing.
2. Press **🔄 Refresh Prices** – downloads only the missing prices / FX rates, recalculates everything and (when the
   toggle is on) updates the workbook.
3. **Export results as Excel file** (sidebar toggle, on by default) – the workbook is written to
   `data/output/IBKR_Portfolio_<account>.xlsx`. Change the folder under *Settings → Output folder*; the *Excel
   export* tab has a *Download* button.

A ready‑made example built from the two statements you supplied is in
`examples/output/IBKR_Portfolio_U24013787_sample.xlsx` (see the limitation about prices in §7).

## 2. What the workbook contains

Sheet **Portfolio** (all sections are real cell ranges, headers bold with borders, statement data blue,
formulas black, cross‑sheet look‑ups green, yellow = cells you may overwrite):

| § | Where | Content |
|---|---|---|
| 1 | `A:H` from row 1 | **Stock transactions** – Name, Ticker, Quantity, Price, Total, Fee, Currency, Date. Chronological; sells have negative quantity/total; currency from the statement sub‑header; `Total = Quantity × Price` |
| 2 | `A:H`, 5 empty rows below §1 | **Dividend payouts** – Name, Ticker, Quantity, Dividend, Total, Withholding Tax, Currency, Date. Dividend, tax and fee lines of one payment are merged by date; fees are added to the tax cell |
| 3 | `A:E`, 5 empty rows below §2 | **Deposits & withdrawals** – Description (deposit / withdrawal / monthly fee / interest), Amount, USD.HUF (closing rate of the day), USD Amount, Date (+ helper columns `F:G`) |
| 4 | `A:G`, 5 empty rows below §3 | **Forex** – Currency Bought, Quantity, Price, Total, Fee, Currency Paid, Date (+ `H` fee currency) |
| 5 | `J:P`, rows of §1 | Gross (`=Total+Fee`), Current Price, Current Total, USD Total, USD Gross, USD Current Total, **VUAA Quantity** (`=USD Total / VUAA close on the purchase date`) (+ helpers `R:S`) |
| 6 | `J:Q`, 5 empty rows below §5 | **Positions** – Ticker, Quantity, Current Price, Value, Return (`=Value/Gross`), USD Value, USD Return, CAGR (left empty as specified) |
| 7 | `J:O`, directly below §6 | **Cash balances** – base currency, USD, EUR and every traded currency; ties to IBKR's *Ending Cash* |
| 8 | `J:Q`, 1 empty row below §7 | **Summary (USD)** – `O` Σ USD values, `P` = `O / Σ USD Amount (§3)`, `Q` time‑weighted CAGR |
| 9 | `J:Q`, 1 empty row below §8 | **Summary (base currency)** – `M` = §8 `O` × latest USD/base rate, `N` return, `Q` CAGR |
| 10 | below §9 | *Extra:* benchmark block – the same USD amounts invested in VUAA, value today, excess of your stocks |

Other sheets: **Prices** (latest prices, FX, the settings that all formulas read, yellow *Override* columns),
**FX** and **Benchmark** (daily history used for purchase‑date conversions), **Checks** (reconciliation with the
statement: cash per currency, open positions, NAV), **About** (index with links, conventions, warnings, statements
included) and a hidden `_state` sheet (layout version, archive signature, snapshot of your overrides).

Named ranges (`Trade_Qty`, `Dep_USDAmt`, `BaseCcy`, `AsOfDate`, `Px_Used`, `FX_Table` …) keep the formulas readable.
Only classic functions are used (`SUMIFS`, `INDEX/MATCH`, `IFERROR`, `SUMPRODUCT` …) – no dynamic arrays or
`XLOOKUP`, so the file is not tied to Microsoft 365 and also opens in LibreOffice.

## 3. Refresh Prices – what happens

1. New daily closes are requested **incrementally** (from a week before the last cached day; the first run goes back
   to two weeks before your first transaction). Today's unfinished bar is dropped. Pence quotes (GBp) are converted.
2. Priority of the numbers: **your override > Yahoo Finance (cache) > closing price printed in the statement**.
   A failing symbol never stops the others; after three consecutive failures without a single success the app gives
   up quickly (offline) and keeps working with the cached / statement data.
3. The existing workbook is updated **in place**: only `Prices`, `FX`, `Benchmark`, `About` (and the hidden state)
   are rewritten – every formula, format and comment of `Portfolio` and `Checks` stays exactly as it is and
   recalculates. If the statement archive changed (new transactions, other layout version) the workbook is regenerated
   and the previous file is kept as `…previous.xlsx`. The file is written atomically; if Excel has it open you get a
   clear message and nothing is changed.
4. A value snapshot is stored at every refresh (Dashboard → *Value history*).

**Overrides.** Type a price / FX rate / benchmark price into a yellow cell of the `Prices` sheet *or* into the app's
*Market data* tab. Edits made in Excel are adopted by the app at the next refresh (three‑way merge with the
snapshot in `_state`; an edit in the file wins, clearing a cell removes the override).

**Settings** (export toggle, price source, benchmark symbol, days per year, output folder, overrides, symbol
mapping, last account) are stored in `data/tracker.db` and restored at the next start.

## 4. Conventions and interpretation choices

* **FX quotes** are "units of currency per 1 USD" (`USD.HUF ≈ 320`, Yahoo `HUF=X`). Converting to USD therefore
  **divides**: `USD Amount = Amount / USD.HUF`. (Multiplying a HUF amount by ~320 would be meaningless; the About sheet
  says the same.) The rate of a date is the last close **on or before** that date (Excel `MATCH(…,1)`).
* **Fees and taxes** are stored as **positive costs** (IBKR prints them negative): `Gross = Total + Fee`. For a sell,
  Total is negative, so Gross is the negative of the net proceeds. Dividend fees (e.g. ADR fees) are added to the
  *Withholding Tax* cell. Forex commission is shown in the account's base currency (`Fee Currency`).
* **§5 column O** (USD Current Total) converts at the **purchase‑date** rate, exactly as specified – it isolates the
  price performance from currency moves. §6/§8/§9 use the **latest** rate.
* **CAGR** = `Return ^ (1 / years) − 1`, *years* = deposit‑weighted average time since the deposits (weights = USD
  amounts) measured to the **valuation date** (`Prices!B6`, the date of the newest price – not `TODAY()`, so numbers
  do not drift) divided by `Prices!B7` (365 days). **Interest** rows are listed in §3 but are not contributions.
* **Merging statements**: transactions are de‑duplicated across overlapping files (multiplicity aware – two genuine
  identical fills are not collapsed). IBKR's **HTML** statements round amounts to cents, **CSV** keeps full
  precision; if both cover the same transactions, the CSV values are kept. A mixed archive is reconciled against the
  newest statement (Checks sheet; cash tolerance 0.01 + 0.0051 per rounded amount).
* **Dividend quantity** comes from IBKR's dividend‑accrual table, else from the position on that day.
* Only **stocks/ETFs** (IBKR asset category *Stocks*) and forex conversions are processed. Other asset classes
  (options, futures, bonds, …) are listed as warnings and ignored – cash and positions may then differ from IBKR.

## 5. Project layout

```
app.py                     Streamlit GUI (sidebar, dashboard, transactions, reconciliation, market data, archive, export)
run_app.bat / run_app.sh   launchers (create .venv, install requirements, start the app)
ibkr_tracker/
  parsing/                 CSV + HTML readers -> one common record model (csv_reader, html_reader, extract …)
  portfolio.py             merges any number of statements chronologically, de-duplicates
  calc.py                  Python implementation of every number the workbook computes (oracle + dashboard)
  reconcile.py             cash / positions / NAV versus the statement
  market/                  Yahoo provider, SQLite cache, statement-derived fallback, symbol mapping
  excel/                   builder (workbook), refresh (in-place update / rebuild), overrides, recalc (cached values)
  storage.py, tracker.py   archive + settings (SQLite) and the service layer used by the GUI
scripts/make_sample_workbook.py   regenerates examples/output/*.xlsx offline from examples/statements
tests/                     165 tests (pytest)
```

Data lives in `./data` (`tracker.db`, `output/`); set `IBKR_TRACKER_HOME` to use another folder.

## 6. LibreOffice (optional)

Excel always recalculates the workbook when it is opened. If **LibreOffice** is installed, the app additionally
calculates the file once and stores the results as *cached values*, so previewers that do not calculate (mail
previews, Quick Look, pandas, Excel's *Protected View*) show numbers too. Without LibreOffice the formulas are still
there – in *Protected View* click **Enable Editing** once and Excel fills in the numbers. The checkbox is under
*Settings → Embed calculated values*.

## 7. Known limitations – please read

* **Live Yahoo Finance access was not testable while this was developed** (the build environment has no access to
  Yahoo). The download code is covered by tests with a deterministic fake provider and synthetic Yahoo‑style responses,
  and falls back gracefully, but the first *Refresh Prices* on your computer is the real test. If it reports problems,
  open *Market data* – you can change the Yahoo symbol of a stock, or type a manual price.
* The example workbook in `examples/output` was therefore generated **offline**: prices/FX come from the statements
  (period‑end close), the VUAA columns are blank until a refresh has downloaded benchmark prices.
* The Excel formulas were verified by recalculating the workbook with **LibreOffice** (cell by cell against the
  Python implementation), not with Microsoft Excel itself.
* Everything is derived from the statements you import: import every statement from your first deposit onwards,
  otherwise deposits, cash and the CAGR are incomplete (the *Reconciliation* tab shows when cash or positions
  no longer match IBKR).
* Positions in currencies other than your base currency need an FX rate: the app takes it from the statements
  (forex trades / closing rates) or from Yahoo; otherwise it asks for a manual rate.

## 8. Tests

```
pip install -r requirements-dev.txt
python -m pytest            # ~1 minute; browser test is skipped unless Playwright + Chromium are installed
```

Highlights: parsing of both formats against the same statement, merge / de‑duplication, a synthetic account with
hand‑computed ground truth (sells, withdrawal, fee, interest, EUR stock, EUR base, forex commission, USD base),
cell‑by‑cell comparison of the LibreOffice‑recalculated workbook with the Python numbers, layout/spacing rules of
the specification, in‑place refresh and override merging, locked files, the GUI (Streamlit `AppTest` and an optional
real‑browser smoke test) and a check of the totals against IBKR's own *Open Positions*, *Cash Report* and NAV.
