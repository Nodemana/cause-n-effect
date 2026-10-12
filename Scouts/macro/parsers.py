"""Turn official payloads into observation rows. No network."""

import calendar
import csv
import io
import json
import re
import zipfile
import xml.etree.ElementTree as ET

_ISO = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_QUARTER = re.compile(r"(\d{4})-Q([1-4])\Z")
_YEAR_MONTH = re.compile(r"(\d{4})-(\d{2})\Z")
_QUARTER_LABEL = re.compile(r"([A-Za-z]{3}) Qtr (\d{4})\Z")
_DMY_SLASH = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})\Z")
_MISSING = {"", ".", "na", "n/a", "nan", "null", "none", "-"}
_QUARTER_END = {"1": (3, 31), "2": (6, 30), "3": (9, 30), "4": (12, 31)}
_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

_SS_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def parse(spec, payload):
    provider = spec["provider"]
    if provider == "fred":
        rows = parse_fred(payload)
    elif provider == "bls":
        rows = parse_bls(payload)
    elif provider == "abs":
        rows = parse_abs(payload)
    elif provider == "rba":
        rows = parse_rba(payload, spec["remote_id"])
    elif provider == "nyfed":
        rows = parse_gscpi(payload)
    elif provider == "eia":
        rows = parse_eia(payload)
    elif provider == "gfs":
        rows = parse_gfs(payload, spec["label"])
    else:
        raise ValueError(f"unknown provider {provider}")
    return _collapse(rows)


def parse_fred(payload):
    data = _json(payload)
    if data.get("error_message"):
        raise ValueError(str(data["error_message"]))
    observations = data.get("observations")
    if observations is None:
        raise ValueError("FRED payload has no observations")
    rows = []
    for item in observations:
        obs_date = _require_iso(str(item.get("date") or ""))
        value = parse_number(item.get("value"))
        if obs_date and value is not None:
            rows.append({"obs_date": obs_date, "value": value})
    count = data.get("count")
    if count is not None and int(count) > len(observations):
        raise ValueError("FRED payload is truncated")
    return rows


def parse_bls(payload):
    data = _json(payload)
    status = data.get("status") or ""
    if status and status != "REQUEST_SUCCEEDED":
        message = data.get("message") or status
        if isinstance(message, list):
            message = "; ".join(str(part) for part in message)
        raise ValueError(f"BLS {message}")
    series = ((data.get("Results") or {}).get("series")) or []
    if not series:
        raise ValueError("BLS payload has no series")
    rows = []
    for item in series[0].get("data") or []:
        period = str(item.get("period") or "")
        if not re.fullmatch(r"M(0[1-9]|1[0-2])", period):
            continue
        year = int(item["year"])
        month = int(period[1:])
        value = parse_number(item.get("value"))
        if value is None:
            continue
        rows.append({"obs_date": _month_end(year, month), "value": value})
    return rows


def parse_abs(payload):
    rows = []
    for record in _csv_dicts(payload):
        period = (record.get("TIME_PERIOD") or "").strip()
        value = parse_number(record.get("OBS_VALUE"))
        if not period or value is None:
            continue
        rows.append({"obs_date": period_end(period), "value": value})
    return rows


def parse_rba(payload, remote_id):
    table = list(csv.reader(io.StringIO(_text(payload))))
    header = None
    for index, row in enumerate(table):
        if row and row[0].strip() == "Series ID":
            header = (index, row)
            break
    if header is None:
        raise ValueError("RBA table has no Series ID row")
    start, ids = header
    try:
        column = ids.index(remote_id)
    except ValueError:
        raise ValueError(f"RBA table has no series {remote_id}") from None
    rows = []
    for row in table[start + 1 :]:
        if not row or column >= len(row):
            continue
        obs_date = _rba_date(row[0].strip())
        if not obs_date:
            continue
        value = parse_number(row[column])
        if value is None:
            continue
        rows.append({"obs_date": obs_date, "value": value})
    return rows


def parse_gscpi(payload):
    table = list(csv.reader(io.StringIO(_text(payload))))
    if not table:
        raise ValueError("GSCPI payload is empty")
    rows = []
    for row in table[1:]:
        if not row or not row[0].strip():
            continue
        obs_date = _rba_date(row[0].strip()) or _require_iso(row[0].strip())
        if not obs_date:
            continue
        value = None
        for cell in reversed(row[1:]):
            value = parse_number(cell)
            if value is not None:
                break
        if value is None:
            continue
        rows.append({"obs_date": obs_date, "value": value})
    return rows


def parse_eia(payload):
    data = _json(payload)
    response = data.get("response") or {}
    points = response.get("data")
    if points is None:
        message = data.get("error") or response.get("error") or "EIA payload has no data"
        raise ValueError(str(message))
    rows = []
    for item in points:
        obs_date = _require_iso(str(item.get("period") or "")[:10])
        value = parse_number(item.get("value"))
        if obs_date and value is not None:
            rows.append({"obs_date": obs_date, "value": value})
    return rows


def parse_gfs(payload, label):
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    try:
        book = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile:
        raise ValueError("GFS workbook is not an xlsx file") from None
    try:
        sheet_name = _gfs_sheet_name(book)
        shared = _shared_strings(book)
        root = ET.fromstring(book.read(sheet_name))
    except (ET.ParseError, KeyError, IndexError):
        raise ValueError("GFS workbook could not be read") from None
    grid = {}
    for cell in root.findall(".//m:c", _SS_NS):
        ref = cell.attrib.get("r")
        if not ref:
            continue
        column, row = _cell_ref(ref)
        grid.setdefault(row, {})[column] = _cell_text(cell, shared)
    header_row = None
    columns = {}
    for row, cells in grid.items():
        found = {}
        for column, text in cells.items():
            obs_date = _quarter_label(text)
            if obs_date:
                found[column] = obs_date
        if found:
            header_row = row
            columns = found
            break
    if header_row is None:
        raise ValueError("GFS workbook has no quarter headers")
    target = None
    for row, cells in grid.items():
        if (cells.get(1) or "").strip() == label:
            target = cells
            break
    if target is None:
        raise ValueError(f"GFS workbook has no row {label}")
    rows = []
    for column, obs_date in columns.items():
        value = parse_number(target.get(column))
        if value is None:
            continue
        rows.append({"obs_date": obs_date, "value": value})
    return rows


def gfs_xlsx_url(html, origin="https://www.abs.gov.au"):
    hrefs = re.findall(r'href="([^"]+\.xlsx)"', html or "", flags=re.I)
    if not hrefs:
        raise ValueError("GFS release page has no xlsx link")
    href = hrefs[0]
    for candidate in hrefs:
        if "government-finance-statistics" in candidate.lower():
            href = candidate
            break
    if href.startswith("http://") or href.startswith("https://"):
        return href
    return origin.rstrip("/") + "/" + href.lstrip("/")


def period_end(token):
    token = (token or "").strip()
    iso = _require_iso(token)
    if iso:
        return iso
    quarter = _QUARTER.fullmatch(token)
    if quarter:
        year = quarter.group(1)
        month, day = _QUARTER_END[quarter.group(2)]
        return f"{year}-{month:02d}-{day:02d}"
    year_month = _YEAR_MONTH.fullmatch(token)
    if year_month:
        return _month_end(int(year_month.group(1)), int(year_month.group(2)))
    labeled = _quarter_label(token)
    if labeled:
        return labeled
    raise ValueError(f"unrecognised period {token}")


def parse_number(raw):
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip().replace(",", "")
    if text.casefold() in _MISSING:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _collapse(rows):
    ordered = {}
    for row in rows:
        ordered[row["obs_date"]] = row["value"]
    return [{"obs_date": key, "value": ordered[key]} for key in ordered]


def _json(payload):
    if isinstance(payload, dict):
        return payload
    return json.loads(_text(payload))


def _text(payload):
    if isinstance(payload, str):
        return payload.lstrip("\ufeff")
    return payload.decode("utf-8-sig")


def _csv_dicts(payload):
    return list(csv.DictReader(io.StringIO(_text(payload))))


def _require_iso(token):
    token = (token or "").strip()
    if _ISO.fullmatch(token):
        return token
    return None


def _rba_date(token):
    if not token:
        return None
    if _ISO.fullmatch(token):
        return token
    named = re.fullmatch(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})", token)
    if named:
        month = _MONTHS.get(named.group(2).casefold())
        if month:
            return f"{int(named.group(3)):04d}-{month:02d}-{int(named.group(1)):02d}"
    slash = _DMY_SLASH.fullmatch(token)
    if slash:
        day, month, year = (int(part) for part in slash.groups())
        return f"{year:04d}-{month:02d}-{day:02d}"
    return None


def _quarter_label(token):
    match = _QUARTER_LABEL.fullmatch((token or "").strip())
    if not match:
        return None
    month = _MONTHS.get(match.group(1).casefold())
    if not month:
        return None
    year = int(match.group(2))
    end_month = ((month - 1) // 3 + 1) * 3
    return _month_end(year, end_month)


def _month_end(year, month):
    day = calendar.monthrange(year, month)[1]
    return f"{year:04d}-{month:02d}-{day:02d}"


def _gfs_sheet_name(book):
    root = ET.fromstring(book.read("xl/workbook.xml"))
    sheets = root.findall("m:sheets/m:sheet", _SS_NS)
    target = None
    rel = None
    for sheet in sheets:
        name = sheet.attrib.get("name") or ""
        if name.strip().lower() == "table 1":
            target = sheet
            break
    if target is None and sheets:
        target = sheets[0]
    if target is None:
        raise ValueError("GFS workbook has no worksheets")
    rel = target.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
    rels = ET.fromstring(book.read("xl/_rels/workbook.xml.rels"))
    path = None
    for item in rels:
        if item.attrib.get("Id") == rel:
            path = item.attrib.get("Target")
            break
    if not path:
        raise ValueError("GFS workbook sheet is missing")
    path = path.lstrip("/")
    if not path.startswith("xl/"):
        path = "xl/" + path
    return path


def _shared_strings(book):
    if "xl/sharedStrings.xml" not in book.namelist():
        return []
    root = ET.fromstring(book.read("xl/sharedStrings.xml"))
    strings = []
    for item in root.findall("m:si", _SS_NS):
        strings.append("".join(node.text or "" for node in item.findall(".//m:t", _SS_NS)))
    return strings


def _cell_text(cell, shared):
    kind = cell.attrib.get("t")
    if kind == "inlineStr":
        return "".join(node.text or "" for node in cell.findall(".//m:t", _SS_NS))
    node = cell.find("m:v", _SS_NS)
    if node is None or node.text is None:
        return ""
    if kind == "s":
        index = int(node.text)
        if index < 0 or index >= len(shared):
            return ""
        return shared[index]
    return node.text


def _cell_ref(ref):
    column = 0
    row = ""
    for char in ref:
        if char.isalpha():
            column = column * 26 + ord(char.upper()) - 64
        else:
            row += char
    return column, int(row)
