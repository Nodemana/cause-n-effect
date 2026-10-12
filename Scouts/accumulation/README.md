# accumulation

## What it does

Finds early stake building in official ownership filings. The scout fetches, parses, and screens. Pattern breaks are flagged for the Legate instead of being treated as clean leads.

## Data sources

SEC Schedules 13D and 13G, including amendments, in both the legacy `SC 13D` / `SC 13G` types and the XML `SCHEDULE 13D` / `SCHEDULE 13G` types. ASX substantial holder notices are Form 603 (new), Form 604 (change), and Form 605 (ceasing). Every SEC and ASX request goes through `shared/nethttp.py`.

## What it captures

Each position records the filer, the target, the stake percent, the share count when the form states one, the event date, and the filing date. A 13D also keeps the Item 4 purpose text. A joint filing stays one position so the same shares are not counted twice.

## Filters/rules

A filing is a candidate only inside a recent window (default 90 days), only when it is not a ceasing notice, and only when at least one screen matches: a stake at or above the threshold (default 5%), a repeat filer across more than one target, or sector concentration by the same filer. Unparseable stakes and dates, conflicting figures, a 13G-to-13D switch, and control-purpose language are pattern breaks and go to review.

## Constraints and limitations

The ASX announcements feed is undocumented, returns only the latest five announcements per company (enough for a weekly run), and is health-checked. Sector is not on the filing; the screen uses a map supplied by the caller. The Legate decides which flagged leads pass a threshold.

## Status

Structure and tests only, no live runs.
