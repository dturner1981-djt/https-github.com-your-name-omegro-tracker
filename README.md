# Turner Group Tracker

Performance dashboard for the three business units in the Turner Group
(Nelson Portfolio, Omegro): **Technology Blueprint**, **tlmNexus** and
**Grosvenor Systems**.

Pulls from SharePoint on the Omegro tenant, normalises everything into one fact
table, and renders a single self-contained HTML page covering:

* **P&L** — Net Revenue, EBITA, EBITA margin, Organic Growth and BQR, each for
  the closed month, quarter to date, year to date, the full-quarter outturn
  (projection) and next quarter's forecast, all against approved forecast.
* **Working capital** — WC%, WC% excluding cash, overdue AR and WIP over 30
  days, previous month / current month / next month target.
* **Variance commentary** — the preparer's own explanation of each material
  variance, quoted from the reporting pack.
* **Group summary** — group totals by metric across three horizons: the closed
  month, quarter to date, and the expected full year, each against plan.
* **Operational governance** — OG stage, overall status and improvement-plan
  progress.
* **ITDS** — Portfolio Security Assessment residual risk out of 400, posture,
  movement against baseline, control effectiveness and the three key areas.

```
config/          portfolio definition, metric thresholds, SharePoint source registry
src/omegro_tracker/
  model.py       canonical fact table and period arithmetic
  graph.py       Microsoft Graph client (app-only or device-code)
  build.py       refresh orchestration
  derive.py      variance, RAG, QTD/YTD/outturn roll-ups
  render.py      view model and chart geometry
  parsers/       one module per source shape
templates/       the dashboard template
data/snapshot.json   the committed result of the last refresh
dist/index.html      the rendered dashboard
```

## Running it

```bash
pip install -r requirements-dev.txt
pip install -e .

omegro-tracker build            # refresh from SharePoint, then render
omegro-tracker refresh          # sources -> data/snapshot.json
omegro-tracker render           # snapshot.json -> dist/index.html
omegro-tracker --offline build  # render from the committed extract, no network
```

`refresh` never fails on an unreachable source. It records the reason against
that source and the dashboard shows the pane as unreported — a governance
dashboard that silently drops a business unit is worse than one that admits it
could not read it. Every source's status, last-modified date and read time is
listed in the **Data sources** table at the foot of the page.

## Connecting the live data

P&L and ITDS are live. Working capital and OG stage are not yet connected, and
the page says which is which in the coverage strip at the top.

The financials come from the **Omegro monthly reporting pack** — not from the
BU Progress Report folder, which is not yet populated. Group Finance produces
one workbook per period covering every Omegro VBU; our units sit in the row
band **"David Turner Group"**. `data/extracted/2026-08.json` is a committed
extract of the P8 FY26 pack carrying its own provenance; an authenticated
refresh reproduces it from the source and supersedes it.

To run the refresh live:

1. Register an Entra app with the application permissions `Sites.Read.All` and
   `Files.Read.All`, admin-consented on the `ourvolaris` tenant.
2. Set `OMEGRO_TRACKER_TENANT_ID`, `OMEGRO_TRACKER_CLIENT_ID` and
   `OMEGRO_TRACKER_CLIENT_SECRET` (or run `omegro-tracker refresh
   --device-code` against your own access).
3. `omegro-tracker build`.

For a scheduled refresh, add the same three as repository secrets; the workflow
in `.github/workflows/refresh.yml` runs on the 11th of each month, which is
after the 7-business-day submission deadline.

## The Monday scan

Every Monday morning the tracker checks whether the workbook behind each
dashboard section has been republished in SharePoint, refreshes the dashboard
if so, and emails a summary.

```bash
omegro-tracker scan                # what changed, print only
omegro-tracker scan --dry-run      # ... without advancing the watermarks
omegro-tracker alert --dry-run     # compose the email without sending it
omegro-tracker alert               # scan, refresh, send
```

Change detection compares against `data/seen.json`, a per-source watermark of
the last filename and `lastModifiedDateTime` seen — deliberately not against
the snapshot, because a workbook can be updated in a week when the refresh did
not run and we still want to say so. A dry run never advances a watermark: if
it did, the change would be consumed by the preview and lost to the real run.

Two things the scan has to get right, both of which cost a week of silence if
wrong:

* **Dated subfolders.** Finance files the monthly pack under
  `Monthly Reporting / <year> / <n>. <Mon>`. A scan that stops at the folder a
  source names finds only directories and reports the source missing every
  week, so it substitutes the period into the path and, failing that, descends
  into the most recently modified subfolder.
* **First appearance counts as a change.** `bu_monthly_submissions` turning up
  for the first time is the event that finally fills the working-capital pane;
  treated as "no change" it would pass unremarked.

A source that cannot be watched at all — `itds_assessments` is registered by
site id with no drive — is left out of the scan rather than given a section,
so a known gap does not raise the same alert every Monday.

### Two ways it runs

| | Runs | Email | Needs |
|---|---|---|---|
| `.github/workflows/weekly-scan.yml` | Monday 07:00 UTC | The styled HTML from `notify.py`, via Graph `sendMail` | The Entra app, plus **Mail.Send** and a `OMEGRO_TRACKER_MAIL_SENDER` mailbox it may send as |
| Claude Routine "Turner Group tracker — Monday source scan" | Monday 07:00 UTC | Claude's run summary to the account owner | The **Microsoft 365 connector attached to the Routine** — this cannot be set from a session and must be added in the claude.ai Routines UI |

07:00 UTC is 08:00 London in summer and 07:00 in winter. Cron is UTC and does
not track BST; both are Monday morning, so it is left alone.

`Mail.Send` is deliberately **not** in the read-only scope set the rest of the
client uses — granting it widens what the app can do, so it is requested only
for the alert path. App-only tokens have no mailbox of their own, which is why
`--mail-sender` is required there and not for delegated sign-in.

### Sources

All ids in `config/sources.yml` are resolved against the Omegro tenant.

| Source | What it gives | State |
|---|---|---|
| `omegro_monthly` | **Net Revenue, OPEX, EBITA** — QTD actual vs approved QSR forecast, full-quarter projection (outturn), and the preparer's variance commentary | **Live.** P8 FY26 (Aug close), unit-tested against the published figures. |
| `itds_qdsr` | ITDS residual risk, posture, control effectiveness, key-area narratives, for all 14 Nelson VBUs | **Live.** Parsed and unit-tested against the published Q2-26 assessment. |
| `monthly_review_template` | The canonical P&L and WC reporting schema | Located. Drives the parser layout. |
| `bu_monthly_submissions` | Working capital, improvement-plan initiatives, OG stage per BU | **Not yet populated.** The template was issued Jun-26; the submission folder is being stood up. This is the missing piece for the working-capital pane. Parser written and tested against the template layout. |
| `og_scorecard` | OG stage, OG score, the core Volaris metric set per BU. Read from the **Nelson leadership area**, resolved as a folder so each new quarter is picked up automatically | **Blocked by a sensitivity label.** See below. |
| `og_scorecard_portfolio_copy` | The same scorecards on the portfolio site — the folder circulated as "the link for the operational governance reports" | Registered. Same label, same block. |
| *(derived)* `og_framework` | The OG framework itself, as published by the CFO: five dimensions, the QDSR bands, the escalation rules. Lets the tracker score the **IT & Data Security** dimension from the QDSR data it already holds | **Live.** Unit-tested against the circulated bands. |
| `qsr_submissions` | Per-BU QSR workbooks — the only source with a **full-year** view. Full Yr yr2026 at columns 23 (current forecast) / 28 (prior iteration) / 29 (var) on the confirmed TBL workbook | Located, layout confirmed, parser stub. |
| `qsr_baseline` | The start-of-year QSR (Q4 2025 for FY26), whose Full Yr column **is** the baseline | Registered, parser stub. |
| `vbu_qsrs` | The portfolio's own per-quarter copy of each VBU QSR; fallback where a workbook is missing from Leaders Shared, as Grosvenor's Q2-26 is | Located, parser stub. |

### The OG scorecard is locked, and credentials will not unlock it

The scorecard exists in two places, both registered:

    GRPNelsonPortfolioFinanceRenukaSimpsonGroup-Leadership
      Shared Documents / Leadership / 09. Operational Governance   (_VALUES snapshots)

    OmegroNelsonPortfolio
      Portfolio Documents / Operational Governance / Scoring Assessment   (formula masters)

Both were located and both are current. Neither can be read. Microsoft Graph
answers a content request with **`notSupported`**, which is what it returns for
a file carrying an **encrypting sensitivity label**: Microsoft blocks conversion
for any application that cannot decrypt the file.

This matters for how it gets fixed. It is *not* a size or timeout problem, and
**adding app-only Graph credentials will not resolve it** — a service principal
without rights to the label reads ciphertext. (An earlier version of this file
said the opposite. It was wrong.) The routes that do work:

* Group Finance publishes a values copy **without** the label; or
* someone whose own permissions open the workbook in the browser saves the
  three Turner Group rows out, and they are committed under
  `data/extracted/` like the monthly pack already is.

Until then the dashboard scores the one dimension it can source by itself —
IT & Data Security, from the QDSR — and shows the other four and the stage as
outstanding rather than estimating them.

The parser is written and waiting. The scorecard is a wide matrix rebuilt every
quarter, so it scans for the header row and matches BU rows by alias rather
than indexing fixed cells, and those heuristics have never been run against a
real copy. Whenever a readable copy does arrive, before anyone acts on figures
from it:

```bash
omegro-tracker validate --source og_scorecard
```

That prints the header row, the column-to-metric mapping and the business-unit
rows it matched, so the mapping can be checked against the real workbook.

### The OG framework (and the one dimension we can score)

From **Q3-26** the framework gains a fifth dimension, IT & Data Security,
scored from the QDSR security maturity score. Published by Katie Mansell (CFO)
to the Senior Leadership Team on 14 Sep 2026, and held in `config/portfolio.yml`
under `og_framework`:

| QDSR score | Band | Points |
|---|---|---|
| ≤ 80 | Green | 6 |
| 81–160 | Amber | 4 |
| 161–200 | Red | 2 |
| 200+ | Black | 0 |

* **Q3-26**: published for visibility only — it does not move the stage.
* **Q4-26**: it counts. Maximum total rises 36 → 42, thresholds adjusted
  proportionally.
* **Auto-escalation from Q3-26**, regardless of overall score: QDSR ≥ 160, or a
  critical control failure in EDR coverage or MFA enforcement.
* Target for every business: **80 or below**.

Because the QDSR scores are already parsed from the Q2-26 Portfolio Security
Assessment, this dimension is computed rather than waited for. Two deliberate
restraints in `governance.py`:

* The framework names EDR coverage and MFA enforcement but defines no test for
  a "failure". A `critical` QDSR key area is therefore raised as a **question to
  confirm with IT & DS**, never asserted as an escalation on its own.
* Red reads `161–200` and Black reads `200+`, so both claim exactly 200 —
  which is precisely where Grosvenor sits. It is shown as Red, the reading that
  favours the business, and the page says so rather than quietly picking.

## How numbers are treated

* **Variance bands**: on track within ±5%, at risk to ±10%, off track beyond.
  The ±5% gate is the Business Unit Progress Report's own materiality
  threshold for requiring commentary.
* **Percentage-point metrics move in points, not percent.** A margin going from
  23.1% to 20.4% has fallen 2.7 points, not 12%. Those metrics use ±1pt and
  ±3pt bands.
* **Direction is per metric.** EBITA under forecast is adverse; working capital
  under target is favourable. The page colours on status, never on raw sign.
* **Outturn** is the BU's submitted full-quarter projection where given,
  otherwise QTD actual plus the remaining months at forecast.
* **Missing is not zero.** An unreported figure renders as "—" and as "not
  reported", and never counts as green in a roll-up.
* **ITDS risk is out of a fixed 400 and lower is better**, so the risk rails are
  directly comparable between units.
* **Ratios are recomputed, not averaged.** Group EBITA margin is summed EBITA
  over summed Net Revenue.
* **Baseline and forecast are different things.** The *baseline* is the plan
  set at the start of the year and is fixed for the year. The *forecast* is
  the most recent QSR iteration of the expected position and is re-cut
  regularly. So the month and quarter are measured against the current
  forecast — "against what we last agreed" — and the full year is measured
  against the baseline. Measuring the year against forecast would compare the
  latest estimate with itself.
* **A group total needs every unit.** If one business has not reported a
  metric for a horizon, no total is shown for it — a sum over a partial set
  reads as the group's number while silently omitting a business.
* **The month split is exact, not apportioned.** The pack reports quarter to
  date, so August is P8 QTD less P7 QTD. July, being month one of the quarter,
  is its own QTD.
* **The group does not sell**, so no stage-exit or exit date is tracked.

## Scope

BPC's **"David Turner Group"** row band still carries a fourth unit, **AgentOS**,
which is divesting. It is marked `in_scope: false` in `config/portfolio.yml`
and dropped on load: no panel, no total, and no facts in the snapshot. The
entry is kept only so its alias keeps matching — otherwise its rows would read
as an unrecognised unit for as long as BPC leaves the band in place.

Business unit leads: Colin Ma (Technology Blueprint), David Appleton
(tlmNexus), Paula McQuilan (Grosvenor Systems). Stephen Craig, Renuka Simpson
and Jean Triquet are the finance contacts who produce and sign off the numbers
this tool reads.

The group reports in **USD** even though all three units have GBP as their
functional currency; the dashboard follows the group reporting currency so the
units add up.

## Current position — Q3-26 quarter to date (USD'000)

| | Net revenue | vs fcst | EBITA | vs fcst | Q3 outturn EBITA | vs fcst |
|---|---|---|---|---|---|---|
| Technology Blueprint | 582 | −10.1% | 192 | −20.4% | 339 | −14.7% |
| tlmNexus | 1,524 | −3.3% | 259 | −31.0% | 235 | −62.4% |
| Grosvenor Systems | 1,016 | +1.3% | 319 | +12.1% | 444 | +6.3% |
| **Group** | **3,122** | **−3.2%** | **771** | **−14.6%** | **1,018** | **−22.5%** |

Source: Omegro NR/EBITA pack, period 8 FY26.

August alone is the sharper read — Technology Blueprint's net revenue came in
21% under plan in the month, and both it and tlmNexus lost about 42% of
month EBITA against plan.

**FY26 is not yet connected.** The monthly pack carries the quarter only. The
full-year column needs two figures, from two different QSRs:

* the FY26 **baseline** — the Full Yr yr2026 column of the **Q4 2025** QSR,
  which is the plan set at the start of the year (`qsr_baseline`);
* the FY26 **forecast** — the Full Yr yr2026 column of the latest QSR, the
  most recent iteration of the expected position (`qsr_submissions`).

The QSR's own "Prior Fcst" column is last quarter's iteration, not the
baseline, so it is not a substitute.

## Current position (Q2-26 ITDS, live)

| | Posture | Residual risk | vs baseline | Controls at 7+ | Below 7 | Avg KRI |
|---|---|---|---|---|---|---|
| tlmNexus | Strong | 22 / 400 | unchanged | 47 | 3 | 9.4 |
| Technology Blueprint | Moderate | 126 / 400 | +108 | 28 | 22 | 6.8 |
| Grosvenor Systems | Needs Improvement | 200 / 400 | +103 | 15 | 35 | 5.1 |

tlmNexus holds the strongest position in the Nelson Portfolio. Grosvenor is
8th of 14 and its residual risk has more than doubled since baseline.

## Tests

```bash
pytest -q
```

The ITDS tests assert the published Q2-26 figures, so a parser regression
surfaces as a wrong number rather than an empty pane.
