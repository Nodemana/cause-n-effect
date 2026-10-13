# Rubric

The scout gate reads the current fixed rubric for a lead type and applies every criterion in code. A candidate becomes a lead for significance and the Centurions only when every criterion passes. A failed candidate is stored with status `filtered` and the failing criteria. It is not queued. A missing or stale official price is stored with status `needs_price` and, for a US ticker, listed in `prices/needed.json`. The gate does not guess a price.

Macro leads are not rubric-gated. The macro detector applies its own signal rules before a macro lead is queued.

A capex chain is gated per component: capex, orders backlog, and debt raise each use that type's rubric. Smart money is gated the same way: insider buying and accumulation each use that type's rubric. A component that fails does not enter the combined lead.

Thresholds live in the `rubrics` and `rubric_criteria` tables. Rubric version 1 is fixed. Changing a threshold means inserting a new version. The gate uses the highest fixed version for that lead type.

Version 1, in plain English:

## Capex

- **min_market_cap.** Market cap is at least 2,000,000,000 USD. A US cap is SEC shares outstanding times the official price. An ASX cap is the Markit market cap in AUD, compared as a number with no FX conversion.
- **capex_growth_yoy.** Capex is up at least 10 percent versus the prior year.
- **official_source.** The figure comes from a filing or a company release.

## Debt raise

- **min_market_cap.** Market cap is at least 500,000,000 USD, measured the same way as capex.
- **raise_vs_company.** The stated raise is at least 5 percent of market cap.
- **specific_proceeds.** Proceeds name spending, an acquisition, or a project. General corporate purposes fails.

The debt scout also drops a USD raise below 50,000,000 before the rubric runs. That bar is not a rubric row.

## Insider buying

- **min_market_cap.** Market cap is at least 300,000,000 USD.
- **insider_buy_dollars.** The open-market purchase is at least 10,000 USD.
- **open_market.** The purchase is an open-market buy, not a grant, a gift, an option exercise, a director-fee or salary-sacrifice issue, or a plan issue. A notice that names more than one director is not one open-market buy.

## Orders backlog

- **min_market_cap.** Market cap is at least 1,000,000,000 USD.
- **backlog_qoq_growth.** Disclosed backlog grew at least 10 percent quarter over quarter. No stored prior is a fail.
- **disclosed_demand.** The figure is disclosed backlog or a named order.

## Accumulation

- **min_market_cap.** Market cap is at least 500,000,000 USD.
- **min_stake_pct.** The stake is at least 5 percent.
- **recent_window.** The filing date is inside 90 days.
- **not_ceasing.** The notice opens or changes a stake. A ceasing notice fails.

## Market cap

US prices come from the agent handoff (`prices/needed.json` and `prices.result.json`). The pipeline does not call the broker. ASX prices come from the Markit company header. A quote older than three trading days, or any other status, is not used. The lead waits in `needs_price` until a fresh official price is stored.
