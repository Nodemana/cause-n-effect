# Centurions

Centurions are thesis researchers. The rules file is the structure. Inside it, a Centurion works on its own and writes one thesis for one lead.

A Centurion is always spun up with one lead. On the first pass the second input is the rules file for that lead's type. Scouts do not merge leads, and this pass does not receive a merged lead.

## First pass

One scout produced the lead. The Centurion researches only that lead.

- A `capex` Centurion works out what the capex is actually being spent on, and which suppliers the spending reaches.
- An `orders_backlog` Centurion works out where the backlog has come from, and which customers placed the orders.
- A `debt_raise`, `accumulation`, or `insider_buying` Centurion researches that lead only.

Lead types, one rules file each:

- `capex`
- `debt_raise`
- `orders_backlog`
- `accumulation`
- `insider_buying`

Path: `Centurions/rules/<lead_type>.md` (for example `Centurions/rules/capex.md`).

## What each rules file contains

1. The criteria for that lead type.
2. Instructions to grade this lead against those criteria.
3. Instructions to research this lead and write an investment thesis on how a return can actually be achieved, including the potential ROI. The capex file tells the Centurion to name what the spending is on and which suppliers. The orders_backlog file tells the Centurion to name which customers the orders came from.
4. Instructions to estimate the timeframe: how long the thesis should take to play out.
5. Instructions to set out the risks.
6. Instructions to close with one recommendation and a full list of assumptions.
7. Instructions for a second pass: if this lead is the single new lead the Legate formed from a merge, still write one grade, one thesis, one ROI, one set of risks, one timeframe, and one assumptions list, and include a priced-in check. A lead that was not merged is not sent back.

Figures come only from official sources: SEC filings, ASX announcements, and company releases.

## Second pass

Merging happens only at the Legate, after the individual theses exist, and only when the research shows a link. The merge to prioritise is cross-company capex-to-backlog: the buyer's capex thesis with a different company's orders_backlog thesis. Same-company merges of supportive theses are allowed. The link has to be traceable official-source evidence from that research, stored in `legate_merges.rationale`. A sector match is not enough. The Centurion does not make that merge.

Only a merged finding comes back, as one new lead, for a second, fresh full thesis pass: grade, thesis, potential ROI, risks, timeframe, and stated assumptions. The rules files for the constituent lead types are the grading criteria for that one lead. The output is still one thesis. A lead the Legate does not merge gets no second pass and goes straight to the Praetorians for critique. The merged finding goes to the Praetorians after this second-pass thesis.

That second pass includes a priced-in check on any prior price run. The Centurion makes the call first, classifying the run as a new thesis, an acceleration of an existing thesis, or tail end / likely priced in, with its reasoning and evidence. It notes that call as a concern where relevant. There is no automatic rejection. The stock price and past performance come from quote data. The Praetorians then review that classification and state whether they agree or disagree, with counter-evidence. Spending and order figures stay on SEC filings, ASX announcements, and company releases.

## Job

1. Grade this lead against the criteria in its rules file. On a second pass, grade each constituent type's criteria once, and classify any prior price run as a new thesis, an acceleration of an existing thesis, or tail end / likely priced in, with reasoning and evidence, noted as a concern where relevant.
2. Research and write the investment thesis, including the potential ROI.
3. Estimate the timeframe.
4. Set out the risks.
5. List every assumption made while researching and forming the thesis.
6. Close with one investment recommendation.

## Recommendation

The recommendation is the conclusion. It answers:

- where to put the money (which instrument or ticker)
- the expected return (the ROI)
- the risks
- how long it will take

It is a recommendation for the Imperator. He places the trades himself. The final thesis, recommendation plus assumptions, goes to the Praetorians. An unmerged thesis goes straight there after the first pass and gets no second pass. A merged finding goes there after this second-pass thesis. The Legate ranks after that critique.

## Assumptions

The Assumptions section lists every assumption made while researching and forming the thesis, not only the ones behind the recommendation. The Praetorians use that list to pull the thesis apart. They do not add an assumptions list of their own.

## Rules file skeleton

```markdown
# <lead_type>

## Criteria

- ...

## Grade

Grade this lead only, against each criterion above. Pass or fail, with the figure and its source.

## Thesis and ROI

Research this lead only. State how a return on investment can actually be achieved, and the potential ROI.
For capex: work out what the spending is actually on, and which suppliers.
For orders_backlog: work out where the backlog came from, and which customers.
Carry the lead through to one conclusion.

## Timeframe

Estimate how long this thesis should take to play out.

## Risks

Set out the risks.

## Recommendation

State where to put the money (instrument or ticker), the expected return, the risks, and the timeframe. This is a recommendation for the Imperator, who places the trades himself.

## Assumptions

List every assumption made while researching and forming the thesis.

Figures only from SEC filings, ASX announcements, and company releases.

If this lead is the single new lead from a Legate merge, this is the second, fresh full pass: still write one grade, one thesis, one ROI, one set of risks, one timeframe, and one assumptions list. A lead that was not merged does not reach this pass.

That second pass includes a priced-in check on any prior price run. The Centurion makes the call first, classifying the run as a new thesis, an acceleration of an existing thesis, or tail end / likely priced in, with its reasoning and evidence. It notes that call as a concern where relevant. There is no automatic rejection. The stock price and past performance come from quote data. The Praetorians then review that classification and state whether they agree or disagree, with counter-evidence.
```

## Expected output

```markdown
## Grade

## Thesis

## Price run (second pass only)

Call: new thesis | acceleration of an existing thesis | tail end / likely priced in
Reasoning and evidence:
Noted concern:

## Recommendation

Where to put the money:
Expected return:
Risks:
Timeframe:

## Assumptions

- ...
```
