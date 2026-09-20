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
* **Operational governance** — OG stage, expected date to exit stage, overall
  status and improvement-plan progress.
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
omegro-tracker --offline build  # render from cache and seed, no network
```

`refresh` never fails on an unreachable source. It records the reason against
that source and the dashboard shows the pane as unreported — a governance
dashboard that silently drops a business unit is worse than one that admits it
could not read it. Every source's status, last-modified date and read time is
listed in the **Data sources** table at the foot of the page.

## Connecting the live data

Everything except ITDS currently renders from `data/seed/financials.json`,
which is **illustrative placeholder data, not actual results**. The page says
so in a banner until a financial source resolves. To replace it:

1. Register an Entra app with the application permissions `Sites.Read.All` and
   `Files.Read.All`, admin-consented on the `ourvolaris` tenant.
2. Set `OMEGRO_TRACKER_TENANT_ID`, `OMEGRO_TRACKER_CLIENT_ID` and
   `OMEGRO_TRACKER_CLIENT_SECRET` (or run `omegro-tracker refresh
   --device-code` against your own access).
3. `omegro-tracker build`.

For a scheduled refresh, add the same three as repository secrets; the workflow
in `.github/workflows/refresh.yml` runs on the 11th of each month, which is
after the 7-business-day submission deadline.

### Sources

All ids in `config/sources.yml` are resolved against the Omegro tenant.

| Source | What it gives | State |
|---|---|---|
| `itds_qdsr` | ITDS residual risk, posture, control effectiveness, key-area narratives, for all 14 Nelson VBUs | **Live.** Parsed and unit-tested against the published Q2-26 assessment. |
| `monthly_review_template` | The canonical P&L and WC reporting schema | Located. Drives the parser layout. |
| `bu_monthly_submissions` | Per-BU monthly Progress Reports — QTD actual vs forecast, quarter projection, next-quarter forecast, improvement plan | **Not yet populated.** The template was issued Jun-26; the submission folder is being stood up. Parser is written and tested against the template layout. |
| `og_scorecard` | OG stage, OG score, the core Volaris metric set per BU | **Needs one validation pass.** See below. |
| `qsr_submissions` | Approved quarterly forecast | Located, parser stub. |

### Validate the OG scorecard mapping before trusting it

The scorecard is a wide matrix that is rebuilt every quarter, so the parser
scans for the header row and matches BU rows by alias rather than indexing
fixed cells. Those heuristics were written against the published metric
vocabulary, not against a downloaded copy — the workbook is several megabytes
and could not be retrieved when the parser was written. Before anyone acts on
figures from it:

```bash
omegro-tracker validate --source og_scorecard
```

That prints the header row, the column-to-metric mapping and the business-unit
rows it matched, so the mapping can be checked against the real workbook.

## How numbers are treated

* **Variance is always against approved forecast**, per the Business Unit
  Progress Report. On track within ±5%, at risk to ±10%, off track beyond —
  the ±5% gate is the template's own materiality threshold for commentary.
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
