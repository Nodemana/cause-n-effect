"""Task files for an external agent. Live runs write these and stop.

An agent reads `<queue>/<run_id>/<stage>/<lead_id>.task.json` and writes
`<lead_id>.result.json` beside it. This module does not call a model.
"""

import json
from pathlib import Path

DEFAULT_QUEUE_DIR = Path("data") / "agent_queue"

SIGNIFICANCE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "magnitude",
        "magnitude_rationale",
        "novelty",
        "novelty_rationale",
        "attention",
        "attention_rationale",
        "timing",
        "timing_rationale",
    ],
    "properties": {
        "magnitude": {"enum": ["large", "moderate", "small"]},
        "magnitude_rationale": {"type": "string", "minLength": 1},
        "novelty": {"enum": ["first", "routine"]},
        "novelty_rationale": {"type": "string", "minLength": 1},
        "attention": {"enum": ["unnoticed", "noticed"]},
        "attention_rationale": {"type": "string", "minLength": 1},
        "timing": {"enum": ["early", "on_time", "late"]},
        "timing_rationale": {"type": "string", "minLength": 1},
    },
}

CENTURION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "thesis",
        "roi",
        "roi_pct",
        "timeframe_days",
        "risks",
        "recommendation",
        "assumptions",
        "suppliers",
        "customers",
        "buyers",
        "price_run",
    ],
    "properties": {
        "thesis": {"type": "string"},
        "roi": {"type": "string"},
        "roi_pct": {"type": "number"},
        "timeframe_days": {"type": "integer"},
        "risks": {"type": "array", "items": {"type": "string"}},
        "recommendation": {"type": "string"},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "suppliers": {"$ref": "#/definitions/party_list"},
        "customers": {"$ref": "#/definitions/party_list"},
        "buyers": {"$ref": "#/definitions/party_list"},
        "price_run": {"enum": ["new", "acceleration", "priced-in", None]},
    },
    "definitions": {
        "party": {
            "type": "object",
            "additionalProperties": False,
            "required": ["name", "ticker"],
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "ticker": {"type": "string"},
            },
        },
        "party_list": {"type": "array", "items": {"$ref": "#/definitions/party"}},
    },
}

_FLAW_SEVERITIES = ("low", "moderate", "high")

PRAETORIAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["flaws"],
    "properties": {
        "flaws": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["flaw", "severity", "evidence"],
                "properties": {
                    "flaw": {"type": "string", "minLength": 1},
                    "severity": {"enum": list(_FLAW_SEVERITIES)},
                    "evidence": {"type": "string", "minLength": 1},
                },
            },
        },
    },
}

SELECTION_MAX = 5
LEGATE_TASK_ID = "legate_selection"

LEGATE_SELECTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["lead_ids", "rationale"],
    "properties": {
        "lead_ids": {
            "type": "array",
            "maxItems": SELECTION_MAX,
            "items": {"type": "integer"},
        },
        "rationale": {"type": "object"},
    },
}

_SCHEMAS = {
    "significance": SIGNIFICANCE_SCHEMA,
    "centurion_1": CENTURION_SCHEMA,
    "centurion_2": CENTURION_SCHEMA,
    "praetorian": PRAETORIAN_SCHEMA,
    "legate_selection": LEGATE_SELECTION_SCHEMA,
}

_SIGNIFICANCE_ENUMS = {
    "magnitude": ("large", "moderate", "small"),
    "novelty": ("first", "routine"),
    "attention": ("unnoticed", "noticed"),
    "timing": ("early", "on_time", "late"),
}

_PRICE_RUNS = ("new", "acceleration", "priced-in")
_PARTY_KEYS = {"name", "ticker"}


class AgentResultError(RuntimeError):
    """One or more result files are missing or do not match the schema."""


def figure_task_id(figure_id):
    """Significance has no lead row yet. The task id is the figure id."""
    return f"figure-{figure_id}"


def lead_task_id(synthetic_id):
    """Later stages use the sweep lead key. The database id is assigned at the end."""
    return str(synthetic_id)


def schema_for(stage):
    try:
        return _SCHEMAS[stage]
    except KeyError:
        raise AgentResultError(f"unknown stage {stage}") from None


def stage_dir(queue_dir, run_id, stage):
    return Path(queue_dir) / run_id / stage


def write_task(queue_dir, run_id, stage, lead_id, payload):
    folder = stage_dir(queue_dir, run_id, stage)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{lead_id}.task.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def task_files(queue_dir, run_id, stage):
    folder = stage_dir(queue_dir, run_id, stage)
    if not folder.is_dir():
        return []
    return sorted(folder.glob("*.task.json"))


def result_path(task_path):
    name = task_path.name
    if not name.endswith(".task.json"):
        raise AgentResultError(f"not a task file: {task_path}")
    return task_path.with_name(name[: -len(".task.json")] + ".result.json")


def read_tasks(queue_dir, run_id, stage):
    """Return (payload, path) for every task in the stage, path order."""
    found = []
    for path in task_files(queue_dir, run_id, stage):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise AgentResultError(f"{path.name}: task file is not JSON ({exc})") from None
        if not isinstance(payload, dict):
            raise AgentResultError(f"{path.name}: task file is not a JSON object")
        found.append((payload, path))
    return found


def outstanding_tasks(queue_dir, run_id, stage):
    """Task paths that do not yet have a sibling result file."""
    missing = []
    for path in task_files(queue_dir, run_id, stage):
        if not result_path(path).is_file():
            missing.append(path)
    return missing


def load_results(queue_dir, run_id, stage):
    """Validate every result for this stage. Raise before any result is returned.

    A missing file, malformed JSON, or a schema miss rejects the whole stage
    so a later ingest can be run again safely.
    """
    errors = []
    found = {}
    files = task_files(queue_dir, run_id, stage)
    for path in files:
        lead_id = path.name[: -len(".task.json")]
        answer = result_path(path)
        if not answer.is_file():
            errors.append(f"{lead_id}: missing result file {answer}")
            continue
        try:
            data = json.loads(answer.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            errors.append(f"{lead_id}: result is not JSON ({exc})")
            continue
        if not isinstance(data, dict):
            errors.append(f"{lead_id}: result is not a JSON object")
            continue
        errors.extend(validate(stage, lead_id, data))
        found[lead_id] = data
    if errors:
        raise AgentResultError("\n".join(errors))
    return found


def validate(stage, lead_id, data):
    if stage == "significance":
        return _validate_significance(lead_id, data)
    if stage in {"centurion_1", "centurion_2"}:
        return _validate_centurion(lead_id, data)
    if stage == "praetorian":
        return _validate_praetorian(lead_id, data)
    if stage == "legate_selection":
        return _validate_legate_selection(lead_id, data)
    return [f"{lead_id}: unknown stage {stage}"]


def _validate_significance(lead_id, data):
    allowed = set()
    for key in _SIGNIFICANCE_ENUMS:
        allowed.add(key)
        allowed.add(f"{key}_rationale")
    errors = _unexpected(lead_id, data, allowed)
    for key, choices in _SIGNIFICANCE_ENUMS.items():
        if data.get(key) not in choices:
            errors.append(f"{lead_id}: {key} must be {_listed(choices)}")
        rationale = data.get(f"{key}_rationale")
        if not isinstance(rationale, str) or not rationale.strip():
            errors.append(f"{lead_id}: {key}_rationale must be a non-empty string")
    return errors


def _validate_centurion(lead_id, data):
    required = set(CENTURION_SCHEMA["required"])
    errors = _unexpected(lead_id, data, required)
    errors.extend(_missing(lead_id, data, required))
    _string(errors, lead_id, data, "thesis")
    _string(errors, lead_id, data, "roi")
    _string(errors, lead_id, data, "recommendation")
    if "roi_pct" in data and not _is_number(data.get("roi_pct")):
        errors.append(f"{lead_id}: roi_pct must be a number")
    if "timeframe_days" in data and not _is_int(data.get("timeframe_days")):
        errors.append(f"{lead_id}: timeframe_days must be an integer")
    elif "timeframe_days" in data and data["timeframe_days"] < 0:
        errors.append(f"{lead_id}: timeframe_days must be zero or positive")
    _string_list(errors, lead_id, data, "risks")
    _string_list(errors, lead_id, data, "assumptions")
    for role in ("suppliers", "customers", "buyers"):
        _party_list(errors, lead_id, data, role)
    if "price_run" in data and data["price_run"] is not None and data["price_run"] not in _PRICE_RUNS:
        errors.append(f"{lead_id}: price_run must be new, acceleration, priced-in, or null")
    return errors


def _validate_praetorian(lead_id, data):
    errors = _unexpected(lead_id, data, {"flaws"})
    errors.extend(_missing(lead_id, data, {"flaws"}))
    flaws = data.get("flaws")
    if "flaws" not in data:
        return errors
    if not isinstance(flaws, list):
        errors.append(f"{lead_id}: flaws must be a list")
        return errors
    for index, item in enumerate(flaws):
        prefix = f"{lead_id}: flaws[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix} must be an object")
            continue
        errors.extend(_unexpected(prefix, item, {"flaw", "severity", "evidence"}))
        errors.extend(_missing(prefix, item, {"flaw", "severity", "evidence"}))
        if "flaw" in item and (not isinstance(item.get("flaw"), str) or not item["flaw"].strip()):
            errors.append(f"{prefix}: flaw must be a non-empty string")
        if "severity" in item and item.get("severity") not in _FLAW_SEVERITIES:
            errors.append(f"{prefix}: severity must be {_listed(_FLAW_SEVERITIES)}")
        if "evidence" in item and (not isinstance(item.get("evidence"), str) or not item["evidence"].strip()):
            errors.append(f"{prefix}: evidence must be a non-empty string")
    return errors


def _validate_legate_selection(lead_id, data):
    errors = _unexpected(lead_id, data, {"lead_ids", "rationale"})
    errors.extend(_missing(lead_id, data, {"lead_ids", "rationale"}))
    lead_ids = data.get("lead_ids")
    chosen = []
    if "lead_ids" not in data:
        pass
    elif not isinstance(lead_ids, list):
        errors.append(f"{lead_id}: lead_ids must be a list")
    elif len(lead_ids) > SELECTION_MAX:
        errors.append(f"{lead_id}: lead_ids must contain at most {SELECTION_MAX} ids")
    else:
        seen = set()
        for index, item in enumerate(lead_ids):
            if isinstance(item, bool) or not isinstance(item, int):
                errors.append(f"{lead_id}: lead_ids[{index}] must be an integer")
                continue
            if item in seen:
                errors.append(f"{lead_id}: lead_ids contains duplicate {item}")
            seen.add(item)
            chosen.append(item)
    rationale = data.get("rationale")
    if "rationale" not in data:
        return errors
    if not isinstance(rationale, dict):
        errors.append(f"{lead_id}: rationale must be an object")
        return errors
    expected = {str(item) for item in chosen}
    if set(rationale) != expected:
        errors.append(f"{lead_id}: rationale must have one entry for each lead id")
    for key, value in rationale.items():
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{lead_id}: rationale[{key}] must be a non-empty string")
    return errors


def _party_list(errors, lead_id, data, role):
    if role not in data:
        return
    items = data[role]
    if not isinstance(items, list):
        errors.append(f"{lead_id}: {role} must be a list")
        return
    for index, item in enumerate(items):
        prefix = f"{lead_id}: {role}[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix} must be an object")
            continue
        errors.extend(_unexpected(prefix, item, _PARTY_KEYS))
        errors.extend(_missing(prefix, item, _PARTY_KEYS))
        if "name" in item and (not isinstance(item.get("name"), str) or not item["name"].strip()):
            errors.append(f"{prefix}: name must be a non-empty string")
        if "ticker" in item and not isinstance(item.get("ticker"), str):
            errors.append(f"{prefix}: ticker must be a string")


def _string_list(errors, lead_id, data, key):
    if key not in data:
        return
    items = data[key]
    if not isinstance(items, list):
        errors.append(f"{lead_id}: {key} must be a list of strings")
        return
    for index, item in enumerate(items):
        if not isinstance(item, str):
            errors.append(f"{lead_id}: {key}[{index}] must be a string")


def _string(errors, lead_id, data, key):
    if key in data and not isinstance(data.get(key), str):
        errors.append(f"{lead_id}: {key} must be a string")


def _unexpected(lead_id, data, allowed):
    return [f"{lead_id}: unexpected key {key}" for key in sorted(set(data) - set(allowed))]


def _missing(lead_id, data, required):
    return [f"{lead_id}: missing key {key}" for key in sorted(set(required) - set(data))]


def _listed(choices):
    items = list(choices)
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} or {items[1]}"
    return ", ".join(items[:-1]) + ", or " + items[-1]


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)
