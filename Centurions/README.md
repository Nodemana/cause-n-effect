# Centurions

Centurions are thesis researchers. The rules file is the structure. Inside it, a Centurion works on its own: it researches one lead, individual or merged, from the sources through to a final investment recommendation.

## How a lead arrives

**Individual.** One scout produced the lead. The Centurion is given two things: the rules file for that lead's type, and the lead.

**Merged.** The merge to prioritise is cross-company capex-to-backlog. A capex lead on the buyer (the company spending the money) and an orders_backlog lead on a different company (the supplier receiving those orders) are the same money in two scouts. When that pairing is detected, the Legate merges the two into one combined lead for a single Centurion.

Any merge, and that pairing especially, needs traceable evidence that links the two leads. Enough evidence is the buyer's capex filing naming the supplier, the supplier's filing naming the customer or contract, or another official-source link between the two companies' filings. A bare sector match is not enough. The linking evidence is stored on the `legate_merges` row (`rationale`). `survivor_lead_id` is the combined lead; the constituent lead ids are in `legate_merge_leads`.

Same-company merges of supportive leads stay allowed, on that same evidence rule. They are not the merge to prioritise.

The Centurion is given the rules file for each constituent lead type, plus the merged lead. A merged lead is still one lead. Grading, the thesis, the ROI, the risks, the timeframe, the recommendation, and the assumptions are each produced once.

Lead types, one rules file each:

- `capex`
- `debt_raise`
- `orders_backlog`
- `accumulation`
- `insider_buying`

Path: `Centurions/rules/<lead_type>.md` (for example `Centurions/rules/capex.md`).

## What each rules file contains

1. The criteria for that lead type.
2. Instructions to grade the lead against those criteria.
3. Instructions to research and write an investment thesis on how a return can actually be achieved, including the potential ROI.
4. Instructions to estimate the timeframe: how long the thesis should take to play out.
5. Instructions to set out the risks.
6. Instructions to close with one recommendation and a full list of assumptions.

Figures come only from official sources: SEC filings, ASX announcements, and company releases.

## Job

1. Grade the lead against the criteria. On a merged lead, grade each constituent type's criteria.
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

It is a recommendation for the Imperator. He places the trades himself. The write-up, recommendation plus assumptions, goes to the Praetorians for critique before the Legate ranks it.

## Assumptions

The Assumptions section lists every assumption made while researching and forming the thesis, not only the ones behind the recommendation. The Praetorians use that list to pull the thesis apart.

## Rules file skeleton

```markdown
# <lead_type>

## Criteria

- ...

## Grade

Grade this lead against each criterion above. Pass or fail, with the figure and its source. If this lead was merged with other types, grade this file's criteria only. The thesis, ROI, risks, timeframe, recommendation, and assumptions are still written once for the merged lead.

## Thesis and ROI

Research how a return on investment can actually be achieved. State the potential ROI. Carry the lead through to one conclusion.

## Timeframe

Estimate how long this thesis should take to play out.

## Risks

Set out the risks.

## Recommendation

State where to put the money (instrument or ticker), the expected return, the risks, and the timeframe. This is a recommendation for the Imperator, who places the trades himself.

## Assumptions

List every assumption made while researching and forming the thesis.

Figures only from SEC filings, ASX announcements, and company releases.
```

## Expected output

```markdown
## Grade

## Thesis

## Recommendation

Where to put the money:
Expected return:
Risks:
Timeframe:

## Assumptions

- ...
```
