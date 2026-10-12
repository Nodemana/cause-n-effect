# accumulation

Detect early M&A positioning from official ownership filings.

This scout owns its software in this folder. Fetching and parsing are deterministic. The Legate decides which flagged leads pass a threshold. Tests use fixture documents only. They do not open a network connection.

## Sources

Figures come from the filings themselves.

- SEC Schedule 13D and 13G, including amendments. EDGAR still carries the legacy HTML/ASCII submission types `SC 13D`, `SC 13D/A`, `SC 13G`, and `SC 13G/A`. The structured XML types, required for Schedule 13G from 30 September 2024 and for Schedule 13D from 18 December 2024, are `SCHEDULE 13D`, `SCHEDULE 13D/A`, `SCHEDULE 13G`, and `SCHEDULE 13G/A`. A Schedule 13D/A combined with a tender-offer form can still arrive as non-XML `SC 13D/A`. The text parser covers that case.
- ASX substantial holder notices: Form 603 (new), Form 604 (change), and Form 605 (ceasing). ASIC's consolidated Substantial holding notice replaces those three forms from 4 December 2026 and is not parsed here.

Every SEC and ASX request goes through `shared/nethttp.py`. The fetcher refuses a URL whose host is not `sec.gov` or `asx.com.au`.

## What a position records

Filer, target name, target CIK or ACN, ticker when the SEC company-ticker file or the ASX query resolves it, stake percent, share count when the form states one, event date, filing date, and, for a 13D, the Item 4 purpose text. A joint filing stays one position. Co-filers are named on that position so the same shares are not counted twice.

## Filters

`ScreenConfig` holds the thresholds. Defaults are a 5% stake, a 90-day window, two targets for a repeat filer, and two positions for sector concentration. Pass `as_of` to pin the window.

A filing is a candidate only when its filing date (or event date, if the filing date is missing) falls in the window, it is not a ceasing notice, it has no review flag, and at least one of these matches:

- stake at or above `min_stake_pct`
- the same filer on at least `repeat_filer_min_targets` distinct targets in the window
- the same filer on at least `sector_min_positions` distinct targets in one sector

Sector is not read from the filing. Pass a map keyed by 10-digit CIK, ASX ACN, ticker, or normalised target name. With `require_known_sector` left on, a filing that would otherwise be a candidate and has no sector mapping goes to review instead.

## Review, not auto-included

These break the pattern and are withheld from `candidates`:

- unparseable stake, or a date that cannot be read
- unknown sector, when a quantitative filter would otherwise have included the filing
- conflicting data (cover percent against Item 5, reporting-person stakes more than one point apart, index form type against `submissionType`)
- a 13G-to-13D switch for the same filer and target
- Item 4 language that states a control purpose (board, merger, tender, sale of the issuer, and the other phrases in `ACTIVIST_PHRASES`)

A denial such as "does not have any plans or proposals" is not treated as activist language. Form 605 and a 13G Item 5 exit (`classOwnership5PercentOrLess` = Y) are ceasing notices. A clean one is omitted. An ambiguous one is reviewed.

`analyse()` returns a JSON-ready dict with `candidates` and `review`. `to_json()` serialises it. Each review row carries `flags`, `notes`, and the position, including the filters it would have matched.

## Tests

```bash
python3 -m unittest discover -s scouts/accumulation/tests -t .
```
