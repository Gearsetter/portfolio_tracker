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
* a local **archive** (SQLite) of every statement, downloaded price/FX series and your settings, so history
  survives restarts and old statements can be combined with new ones.

Everything runs on your own computer. The only network access is the (optional) download of daily closing prices
from Yahoo Finance.

---------------------------------------------------------------------------------------------------------------

## 1. Quick start

Requirements: **Python 3.10 – 3.13**. LibreOffice is optional (see §8).

| System | Start |
|---|---|
| Windows | double‑click `run_app.bat` |
| macOS / Linux | `./run_app.sh` |
| any | `pip install -r requirements.txt` then `streamlit run app.py` (or `python app.py`) |
| VS Code + Docker | *Dev Containers: Reopen in Container*, then **F5** – nothing else to install, see §11 |

The launch scripts create a private virtual environment in `.venv` on the first start, install
`requirements.txt` and open the app in your browser (`http://localhost:8501`; set `IBKR_TRACKER_PORT` for another
port). The server listens on this computer only (`.streamlit/config.toml`). With `streamlit run app.py` open the
address it prints yourself.

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
average years since the purchases); in the consolidated view an **Accounts** table; then positions, cash balances,
allocation and the value history (with the view filter on, closed positions and dust balances are not listed there –
a one‑line note says what was left out).

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

Other sheets: **Prices** (latest prices, FX, the settings that all formulas read, yellow *Override* columns),
**FX** and **Benchmark** (daily history used for purchase‑date conversions), **Checks** (reconciliation with the
statement: cash per currency, share counts, NAV), **About** (index with links, conventions, warnings, statements
included) and a hidden `_state` sheet (layout version, archive signature, snapshot of your overrides).

In the **consolidated workbook** (`IBKR_Portfolio_ALL.xlsx`, see §6) the records of Sections 1–4 carry a ninth
column, **Account** (`I`), saying which account each line comes from; everything else is laid out identically.

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
   recalculates. If the statement archive changed (new transactions, other layout version) or you switched the view
   filter (§12) the workbook is regenerated and the previous file is kept as `…previous.xlsx`. The file is written atomically; if Excel has it open you get a
   clear message and nothing is changed. The consolidated workbook is refreshed the same way.
4. A value snapshot is stored at every refresh (Dashboard → *Value history*), separately for every account and for
   the consolidated view.

**Overrides.** Type a price / FX rate / benchmark price into a yellow cell of the `Prices` sheet *or* into the app's
*Market data* tab. Edits made in Excel are adopted by the app at the next refresh (three‑way merge with the
snapshot in `_state`; an edit in the file wins, clearing a cell removes the override).

**Settings** (export toggle, view filter, price source, benchmark symbol, days per year, base currency of the
consolidated view, CSV format, output folder, overrides, symbol mapping, last account) are stored in `data/tracker.db` and restored at
the next start.

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
    `checks.csv`. UTF‑8 with byte order mark. **CSV format**: *International* (comma separated, decimal point – pandas,
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
  reconcile.py             cash / share counts / NAV versus the statement(s)
  market/                  Yahoo provider, SQLite cache, statement-derived fallback, symbol mapping
  excel/                   builder (workbook), refresh (in-place update / rebuild), overrides, recalc (cached values)
  exports.py, tables.py    CSV export (international / European dialect) and the tables behind it and the GUI
  storage.py, tracker.py   archive + settings (SQLite) and the service layer used by the GUI
scripts/make_sample_workbook.py   regenerates examples/output/*.xlsx offline from examples/statements
tests/                     281 tests (pytest)
.devcontainer/             VS Code dev container: Dockerfile, devcontainer.json, post-create.sh (see §11)
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
  open *Market data* – you can change the Yahoo symbol of a stock, or type a manual price.
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

## 10. Tests

```
pip install -r requirements-dev.txt
python -m pytest            # about a minute; browser test is skipped unless Playwright + Chromium are installed
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

## 11. Development container (VS Code)

`.devcontainer/` is a ready‑made development environment: **Ubuntu 24.04, Python 3.12, LibreOffice Calc 24.2** and all
Python packages (runtime and `pytest`) – the stack the project is tested with. Nothing has to be installed on your
computer except Docker and VS Code.

1. Install **Docker** (Docker Desktop on Windows / macOS, Docker Engine on Linux) and the VS Code extension
   **Dev Containers** (`ms-vscode-remote.remote-containers`; VS Code suggests it when it opens the folder).
2. Open the project folder in VS Code and run **Dev Containers: Reopen in Container** (Command Palette). The first
   build creates an image of about 1.5 GB and takes a few minutes; later starts take seconds.
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
  0.5 GB: cached values in the workbook and the tests that recalculate it) and `INSTALL_PLAYWRIGHT` (default `"false"`;
  `"true"` adds Chromium, about 0.7 GB, for the real‑browser smoke test). Change the value, then *Rebuild Container*.
* **Windows:** for fast file access keep the project in the WSL 2 file system (for example `\\wsl$\Ubuntu\home\you\…`)
  and open it from there. `.gitattributes` keeps the shell scripts at LF line endings – a CRLF checkout would break them.
* The container needs internet access for the Yahoo Finance download (and for the first build); behind a company proxy
  configure it in Docker Desktop.

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

## Changelog

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
