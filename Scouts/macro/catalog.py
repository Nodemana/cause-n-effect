"""Official series the macro scout caches. Numeric facts only.

Geopolitics, elections, and regulatory risk are not series. That placeholder
lives in geopolitics.py.
"""

# How long a successful fetch stays fresh. Daily market series refresh after a
# day, weekly balance-sheet series after a week, monthly series after 20 days,
# and quarterly series after 45 days.
_DAILY = 24
_WEEKLY = 24 * 7
_MONTHLY = 24 * 20
_QUARTERLY = 24 * 45

SERIES = (
    {
        "series_id": "DFF",
        "name": "Effective federal funds rate",
        "source": "FRED",
        "source_url": "https://fred.stlouisfed.org/series/DFF",
        "unit": "percent",
        "frequency": "daily",
        "stale_after_hours": _DAILY,
        "provider": "fred",
        "remote_id": "DFF",
        "fixture": "fred_observations.json",
    },
    {
        "series_id": "DGS2",
        "name": "US Treasury constant maturity, 2-year",
        "source": "FRED",
        "source_url": "https://fred.stlouisfed.org/series/DGS2",
        "unit": "percent",
        "frequency": "daily",
        "stale_after_hours": _DAILY,
        "provider": "fred",
        "remote_id": "DGS2",
        "fixture": "fred_observations.json",
    },
    {
        "series_id": "DGS10",
        "name": "US Treasury constant maturity, 10-year",
        "source": "FRED",
        "source_url": "https://fred.stlouisfed.org/series/DGS10",
        "unit": "percent",
        "frequency": "daily",
        "stale_after_hours": _DAILY,
        "provider": "fred",
        "remote_id": "DGS10",
        "fixture": "fred_observations.json",
    },
    {
        "series_id": "RBA_CASH",
        "name": "RBA cash rate target",
        "source": "RBA",
        "source_url": "https://www.rba.gov.au/statistics/tables/csv/f1-data.csv",
        "unit": "percent",
        "frequency": "daily",
        "stale_after_hours": _DAILY,
        "provider": "rba",
        "table": "f1",
        "remote_id": "FIRMMCRTD",
        "fixture": "rba_f1.csv",
    },
    {
        "series_id": "AU10Y",
        "name": "Australian government bond yield, 10-year",
        "source": "RBA",
        "source_url": "https://www.rba.gov.au/statistics/tables/csv/f2-data.csv",
        "unit": "percent per annum",
        "frequency": "daily",
        "stale_after_hours": _DAILY,
        "provider": "rba",
        "table": "f2",
        "remote_id": "FCMYGBAG10D",
        "fixture": "rba_f1.csv",
    },
    {
        "series_id": "US_CPI",
        "name": "US CPI-U all items, not seasonally adjusted",
        "source": "BLS",
        "source_url": "https://data.bls.gov/timeseries/CUUR0000SA0",
        "unit": "index 1982-84=100",
        "frequency": "monthly",
        "stale_after_hours": _MONTHLY,
        "provider": "bls",
        "remote_id": "CUUR0000SA0",
        "fixture": "bls_cpi.json",
    },
    {
        "series_id": "AU_CPI",
        "name": "Australia CPI all groups, eight capital cities, original, 17th series",
        "source": "ABS",
        "source_url": "https://www.abs.gov.au/statistics/economy/price-indexes-and-inflation/consumer-price-index-australia",
        "unit": "index 2011-12=100",
        "frequency": "quarterly",
        "stale_after_hours": _QUARTERLY,
        "provider": "abs",
        "dataflow": "ABS,CPI,1.1.0",
        "data_key": "1.10001.10.50.Q",
        "start": "2000-Q1",
        "fixture": "abs_cpi.csv",
    },
    {
        "series_id": "WALCL",
        "name": "Federal Reserve total assets, Wednesday level",
        "source": "FRED",
        "source_url": "https://fred.stlouisfed.org/series/WALCL",
        "unit": "millions of USD",
        "frequency": "weekly",
        "stale_after_hours": _WEEKLY,
        "provider": "fred",
        "remote_id": "WALCL",
        "fixture": "fred_observations.json",
    },
    {
        "series_id": "RBA_ASSETS",
        "name": "RBA total assets",
        "source": "RBA",
        "source_url": "https://www.rba.gov.au/statistics/tables/csv/a1-data.csv",
        "unit": "millions of AUD",
        "frequency": "weekly",
        "stale_after_hours": _WEEKLY,
        "provider": "rba",
        "table": "a1",
        "remote_id": "ARBAATAW",
        "fixture": "rba_f1.csv",
    },
    {
        "series_id": "IG_OAS",
        "name": "ICE BofA US corporate index option-adjusted spread",
        "source": "FRED",
        "source_url": "https://fred.stlouisfed.org/series/BAMLC0A0CM",
        "unit": "percent",
        "frequency": "daily",
        "stale_after_hours": _DAILY,
        "provider": "fred",
        "remote_id": "BAMLC0A0CM",
        "fixture": "fred_observations.json",
    },
    {
        "series_id": "HY_OAS",
        "name": "ICE BofA US high yield index option-adjusted spread",
        "source": "FRED",
        "source_url": "https://fred.stlouisfed.org/series/BAMLH0A0HYM2",
        "unit": "percent",
        "frequency": "daily",
        "stale_after_hours": _DAILY,
        "provider": "fred",
        "remote_id": "BAMLH0A0HYM2",
        "fixture": "fred_observations.json",
    },
    {
        "series_id": "USD_BROAD",
        "name": "Trade-weighted US dollar index, broad, goods and services",
        "source": "FRED",
        "source_url": "https://fred.stlouisfed.org/series/DTWEXBGS",
        "unit": "index Jan 2006=100",
        "frequency": "daily",
        "stale_after_hours": _DAILY,
        "provider": "fred",
        "remote_id": "DTWEXBGS",
        "fixture": "fred_observations.json",
    },
    {
        "series_id": "AUDUSD",
        "name": "Australian dollar (USD per 1 AUD)",
        "source": "RBA",
        "source_url": "https://www.rba.gov.au/statistics/tables/csv/f11.1-data.csv",
        "unit": "USD per AUD",
        "frequency": "daily",
        "stale_after_hours": _DAILY,
        "provider": "rba",
        "table": "f11.1",
        "remote_id": "FXRUSD",
        "fixture": "rba_f1.csv",
    },
    {
        "series_id": "WTI",
        "name": "WTI crude oil spot, Cushing",
        "source": "EIA",
        "source_url": "https://www.eia.gov/dnav/pet/pet_pri_spt_s1_d.htm",
        "unit": "USD per barrel",
        "frequency": "daily",
        "stale_after_hours": _DAILY,
        "provider": "eia",
        "route": "petroleum/pri/spt",
        "remote_id": "RWTC",
        "fixture": "eia_spot.json",
    },
    {
        "series_id": "BRENT",
        "name": "Brent crude oil spot, Europe",
        "source": "EIA",
        "source_url": "https://www.eia.gov/dnav/pet/pet_pri_spt_s1_d.htm",
        "unit": "USD per barrel",
        "frequency": "daily",
        "stale_after_hours": _DAILY,
        "provider": "eia",
        "route": "petroleum/pri/spt",
        "remote_id": "RBRTE",
        "fixture": "eia_spot.json",
    },
    {
        "series_id": "HENRY_HUB",
        "name": "Henry Hub natural gas spot",
        "source": "EIA",
        "source_url": "https://www.eia.gov/dnav/ng/hist/rngwhhdd.htm",
        "unit": "USD per million Btu",
        "frequency": "daily",
        "stale_after_hours": _DAILY,
        "provider": "eia",
        "route": "natural-gas/pri/spt",
        "remote_id": "RNGWHHD",
        "fixture": "eia_spot.json",
    },
    {
        "series_id": "US_FED_OUTLAYS",
        "name": "US federal government outlays",
        "source": "FRED",
        "source_url": "https://fred.stlouisfed.org/series/MTSO133FMS",
        "unit": "millions of USD",
        "frequency": "monthly",
        "stale_after_hours": _MONTHLY,
        "provider": "fred",
        "remote_id": "MTSO133FMS",
        "fixture": "fred_observations.json",
    },
    {
        "series_id": "US_FED_SURPLUS",
        "name": "US federal surplus or deficit (negative is a deficit)",
        "source": "FRED",
        "source_url": "https://fred.stlouisfed.org/series/MTSDS133FMS",
        "unit": "millions of USD",
        "frequency": "monthly",
        "stale_after_hours": _MONTHLY,
        "provider": "fred",
        "remote_id": "MTSDS133FMS",
        "fixture": "fred_observations.json",
    },
    {
        "series_id": "AU_GOV_SPENDING",
        "name": "Australia general government final consumption expenditure, current prices, seasonally adjusted",
        "source": "ABS",
        "source_url": "https://www.abs.gov.au/statistics/economy/national-accounts/australian-national-accounts-national-income-expenditure-and-product",
        "unit": "millions of AUD",
        "frequency": "quarterly",
        "stale_after_hours": _QUARTERLY,
        "provider": "abs",
        "dataflow": "ABS,ANA_EXP,1.0.0",
        "data_key": "C.FCE.GGS.20.AUS.Q",
        "start": "2000-Q1",
        "fixture": "abs_gov.csv",
    },
    {
        "series_id": "AU_GFS_BALANCE",
        "name": "Australia general government GFS net operating balance (negative is a deficit)",
        "source": "ABS",
        "source_url": "https://www.abs.gov.au/statistics/economy/government/government-finance-statistics-australia/latest-release",
        "unit": "millions of AUD",
        "frequency": "quarterly",
        "stale_after_hours": _QUARTERLY,
        "provider": "gfs",
        "label": "GFS NET OPERATING BALANCE",
        "fixture": "gfs_table1.xlsx",
    },
    {
        "series_id": "GSCPI",
        "name": "NY Fed global supply chain pressure index",
        "source": "NY Fed",
        "source_url": "https://www.newyorkfed.org/research/policy/gscpi",
        "unit": "standard deviations",
        "frequency": "monthly",
        "stale_after_hours": _MONTHLY,
        "provider": "nyfed",
        "fixture": "gscpi.csv",
    },
)


def by_id():
    return {row["series_id"]: row for row in SERIES}


def select(series_ids):
    """Catalog rows in request order. Unknown ids raise ValueError."""
    if series_ids is None:
        return list(SERIES)
    chosen = []
    seen = set()
    unknown = []
    lookup = by_id()
    for raw in series_ids:
        key = (raw or "").strip()
        if not key or key in seen:
            continue
        row = lookup.get(key)
        if row is None:
            unknown.append(key)
            continue
        seen.add(key)
        chosen.append(row)
    if unknown:
        raise ValueError("unknown series: " + ", ".join(unknown))
    if not chosen:
        raise ValueError("no series selected")
    return chosen
