# Changelog

Newest entries are first. Times are AEST (Australian Eastern Standard Time, UTC+10).

Every pull request adds an entry at the top of this file before it merges. Each entry records the date and time, what changed, why, and the pull request number. Leave out secrets, contact addresses, and personal data.

### 2026-10-13 14:04 AEST — [PR #34](https://github.com/Nodemana/cause-n-effect/pull/34)

**What changed.** An Appendix 3Y that issues shares as director fees, as salary sacrifice, or under a plan is not an open-market buy. A notice that names more than one director is not stored as one director's open-market purchase. Each selected thesis file lists tickers only, once each. The thesis is paragraphs, followed by the recommendation, the expected return, the risks as bullets, the timeframe, and the assumptions as bullets. The macro section on that file keeps the series that match the lead.

**Why.** A Mineral Resources fee-plan notice for six non-executive directors was read as one A$240,000 open-market buy by one director. Those shares are director pay, not a discretionary purchase. The report was putting the recommendation on the ticker line, running the other sections into the thesis paragraph, repeating a ticker, and printing the whole macro catalog.

### 2026-10-13 09:15 AEST — [PR #33](https://github.com/Nodemana/cause-n-effect/pull/33)

**What changed.** Scouts test every rubric criterion in code before a candidate is handed to significance or the Centurions. The checks are market cap, capex growth, an official source, backlog growth, disclosed demand, the raise versus the company, specific proceeds, insider purchase dollars, an open-market purchase, stake percent, the recent filing window, and whether a notice is ceasing. A candidate proceeds only when every criterion for its lead type passes. Each pass or fail is stored with the measured value and the rubric id and version. A failed candidate is stored with status filtered and the failing criteria, and it is not queued. If the official price is missing or stale, the candidate waits with status needs_price. A US name in that state is listed in prices/needed.json. The gate does not guess a price. A capex chain and a smart-money lead are gated on each component. Macro leads are unchanged. Centurion tasks no longer ask for a grade against the rubric. The task includes the gate results. The Centurion writes the judgement: what the spend is on, suppliers and customers, whether a move is priced in, the thesis, the ROI, the risks, the timeframe, the assumptions, and a recommendation. `docs/rubric.md` lists the version 1 thresholds. Those numbers live in the rubrics tables. A change needs a new version.

**Why.** Those checks are numbers and pass/fail facts. Running them in the scout keeps that work off the Centurions.

### 2026-10-13 08:30 AEST — [PR #31](https://github.com/Nodemana/cause-n-effect/pull/31)

**What changed.** After the Legate selects, ingest writes one markdown file per selected thesis at `data/reports/<run>/<rank>-<ticker>.md`. The rank is the selection order. The file has the thesis: the cause to effect chain, the tickers, evidence links from official sources, the macro context, and the price-run classification. The full Praetorian critique follows, with every flaw, its severity, and its evidence. The combined final report is gone. The sweep no longer writes a decision row, and the pipeline has no broker order call.

**Why.** The reader should open one thesis at a time, with the objections attached to that thesis. A decision row read like an instruction to act.

### 2026-10-13 08:24 AEST — [PR #32](https://github.com/Nodemana/cause-n-effect/pull/32)

**What changed.** ASX quotes come from the Markit company header. The last price and the market cap are in Australian dollars. The pipeline does not call Interactive Brokers. For a US ticker that needs a quote, the sweep writes `data/agent_queue/<run>/prices/needed.json`. The agent returns the last price, contract id, status, and time. A quote is stored when the status is realtime or delayed, the price is positive, and the quote is no older than three trading days. Any other result leaves that ticker unverified. A US market cap is SEC shares outstanding times that price. An ASX market cap is the Markit figure. `python -m pipeline cleanup` prints what it would remove and writes nothing. `--apply` copies the database first, then in one transaction removes unofficial sources, prior-year comparatives, implausible figures, duplicates, superseded figures, and orphan leads and theses. Each removal is written to `cleanup_log`. Running it again removes nothing new.

**Why.** Price and market-cap checks need an official quote, and a bad row needs a cleanup that can be reviewed before it writes and that is safe to run twice.

### 2026-10-13 08:22 AEST — [PR #31](https://github.com/Nodemana/cause-n-effect/pull/31)

**What changed.** Ranking writes every candidate thesis to `data/reports/<run>-candidates.md` and one Legate task. Each section includes that thesis's full Praetorian flaw list. The Legate selects at most five of those leads and writes a reason for each pick. `pipeline ingest` checks that the list has at most five ids and that each id is a candidate in this run, then writes `data/reports/<run>-final.md` with only the selected theses.

**Why.** The operator chooses the shortlist. The ranking score stays a sort order for the candidates.

### 2026-10-13 08:05 AEST — [PR #31](https://github.com/Nodemana/cause-n-effect/pull/31)

**What changed.** Praetorians always run. For each thesis they write every flaw they see: the flaw, a severity of low, moderate, or high, and the evidence. Leads continue into ranking. The report shows the list. The score does not use it.

**Why.** The critique is a record that travels with the thesis. It does not decide which leads survive.

### 2026-10-13 07:54 AEST — [PR #31](https://github.com/Nodemana/cause-n-effect/pull/31)

**What changed.** Linkable capex, orders backlog, and debt raises become one capex-chain lead before the Centurion. Insider buying and accumulation on the same ticker, inside 90 days, become one smart-money lead. Each combined lead gets one Centurion task.

**Why.** A buyer and its supplier, or two smart-money signals on one company, are one story. One task keeps that story in one thesis.

### 2026-10-13 07:54 AEST — [PR #31](https://github.com/Nodemana/cause-n-effect/pull/31)

**What changed.** A large macro move becomes its own lead: a 1-month move of at least 10 percent, a 3-month move of at least 20 percent, a z-score of at least 2 against the prior year, or a Brent minus WTI spread outside the prior-year range.

**Why.** Those moves are research leads, not only background for other theses.

### 2026-10-13 07:54 AEST — [PR #31](https://github.com/Nodemana/cause-n-effect/pull/31)

**What changed.** Henry Hub natural gas (series `RNGWHHD`) is read from EIA's natural-gas futures route, `natural-gas/pri/fut`. WTI and Brent stay on the petroleum spot route.

**Why.** That series is published on the futures route. The catalog was pointing at the natural-gas spot route.

### 2026-10-13 06:11 AEST — [PR #31](https://github.com/Nodemana/cause-n-effect/pull/31)

**What changed.** The live sweep does not call a model and does not read an API key. After the deterministic gates it writes task files. An external agent writes the results. `pipeline ingest` checks each result and advances the run.

**Why.** Judgement belongs with the operator's agent. The pipeline keeps the gates, the queue, and the database.

### 2026-10-13 02:01 AEST — [PR #30](https://github.com/Nodemana/cause-n-effect/pull/30)

**What changed.** A counterparty's role comes from the filing sentence. Customer language stays a customer, supplier language stays a supplier, and acquire, merger, or separation language is a deal party. A customer named in a capex filing stays a customer, so that name does not merge with a backlog lead for the same company. Names that share the issuer's own root are dropped, including a former parent or spin-off when the sentence says so. Registrant matches are whole words. Exhibit-index lines and spin-off boilerplate are skipped. Full-text downloads are capped at 5 per company and 40 per sweep.

**Why.** Role used to follow the lead type, so a customer named in a spending filing could be stored as a supplier and linked to the wrong backlog.

### 2026-10-13 01:35 AEST — [PR #29](https://github.com/Nodemana/cause-n-effect/pull/29)

**What changed.** Each full-text search hit is downloaded and read as the filing. A hit that names the other company is the link by itself. Suffixes fold before the registrant lookup, so Incorporated matches Inc, Corporation matches Corp, Company matches Co, and Limited matches Ltd.

**Why.** Live hits carried no highlighted snippet, and a suffix variant missed the registrant title.

### 2026-10-13 01:23 AEST — [PR #28](https://github.com/Nodemana/cause-n-effect/pull/28)

**What changed.** A counterparty name is stored only when it matches a cached SEC registrant or ends in a company suffix, and it is not a heading. A registrant match keeps that title and ticker. Supplier and customer lines list those names, or say none found. Each side also searches the other company's name in the filings.

**Why.** Extraction was keeping report headings and missing real customers.

### 2026-10-13 00:59 AEST — [PR #27](https://github.com/Nodemana/cause-n-effect/pull/27)

**What changed.** Every orders-backlog figure ignores an on-time timing rating. A late figure still rejects. Capex ignores on-time only for a quarter-end print. When the SEC contact address is set, the sweep reads official filings for named suppliers and customers and writes those names on the thesis. A source error drops that company's names. The address is not logged.

**Why.** Ordinary quarterly timing was rejecting supplier orders, and the merge had no official names to link a capex buyer with a backlog supplier.

### 2026-10-13 00:39 AEST — [PR #26](https://github.com/Nodemana/cause-n-effect/pull/26)

**What changed.** Stored capex and backlog growth is quoted in the thesis, with both periods and the percent change. For a quarter-end print, on-time is recorded and is not an issue, and novelty follows the growth rate: 50 percent or more is a sharp acceleration. Rank uses expected return, capped growth, and size, with eight points off for each surviving issue. An unknown market cap stays a surviving issue. A known cap below the threshold still rejects. Same-day insider lots name the currency in the total.

**Why.** The thesis was silent on the growth the ranking uses, and a missing market cap needed to stay distinct from a small known cap.

### 2026-10-13 00:24 AEST — [PR #25](https://github.com/Nodemana/cause-n-effect/pull/25)

**What changed.** Cash-flow history keeps every span in the window, at least five quarters, so the year-ago quarter is stored. When that quarter is still missing, the comparison uses trailing-twelve-month totals about a year apart. One soft rating, either routine novelty or on-time timing, is a surviving issue. Two failed ratings reject the lead. Late still rejects on its own. Lots by the same insider, ticker, and day are summed into one figure. A different insider on that day stays a separate lead.

**Why.** The history cap was filling with the newest year-to-date rows and dropping the year-ago quarter. Quarterly prints were rejected for being on time. Split same-day lots were marking each other routine.

### 2026-10-13 00:00 AEST — [PR #24](https://github.com/Nodemana/cause-n-effect/pull/24)

**What changed.** Capex growth matches any capex figure with the same span whose period ends about a year earlier. The instrument string does not have to match. Backlog growth can use a prior quarter-end or a year-ago end, including history stored under another instrument or tag. A large order with no stored prior stays in the sweep, and the missing comparison is a surviving issue. A smaller order with no prior is still rejected. ASX announcement pages and Markit links count as official sources. A missing companyfacts response is logged as "no companyfacts".

**Why.** Year-ago comparisons were missing stored history, ASX links were treated as unofficial, and a large backlog with no prior was being dropped.
