"""LLM providers.

The live client speaks the OpenAI chat-completions API. It reads
LLM_API_KEY, LLM_BASE_URL, and LLM_MODEL from the environment. The key is
never written to a log line, a repr, or an exception message.

When LLM_API_KEY is unset, resolve_provider returns the stub. The stub is
deterministic and does not open a socket. Tests use it the same way.
"""

import json
import os
import re
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"


class LLMError(RuntimeError):
    pass


class Completion:
    def __init__(self, text, prompt_tokens, completion_tokens):
        self.text = text
        self.prompt_tokens = int(prompt_tokens)
        self.completion_tokens = int(completion_tokens)

    @property
    def total_tokens(self):
        return self.prompt_tokens + self.completion_tokens


class OpenAICompatibleProvider:
    """HTTP client for an OpenAI-compatible /chat/completions endpoint."""

    name = "openai-compatible"

    def __init__(self, api_key, base_url, model, opener=None):
        self._api_key = api_key
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.model = model or DEFAULT_MODEL
        self._opener = opener or urllib.request.urlopen

    def __repr__(self):
        return f"OpenAICompatibleProvider(model={self.model!r})"

    def complete(self, prompt, *, max_tokens):
        url = self.base_url + "/chat/completions"
        body = json.dumps(
            {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": int(max_tokens),
            }
        ).encode("utf-8")
        request = urllib.request.Request(url, data=body, method="POST")
        request.add_header("Content-Type", "application/json")
        request.add_header("Authorization", "Bearer " + self._api_key)
        try:
            with self._opener(request, timeout=60) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise LLMError(_redact(f"LLM request failed with HTTP {exc.code}: {detail}", self._api_key)) from None
        except urllib.error.URLError as exc:
            raise LLMError(_redact(f"LLM request failed: {exc.reason}", self._api_key)) from None
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise LLMError("LLM response was not JSON") from exc
        try:
            text = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("LLM response had no message content") from exc
        usage = payload.get("usage") or {}
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        if prompt_tokens is None:
            prompt_tokens = max(1, (len(prompt) + 3) // 4)
        if completion_tokens is None:
            completion_tokens = max(1, (len(text) + 3) // 4)
        return Completion(text, prompt_tokens, completion_tokens)


class StubProvider:
    """Deterministic offline stand-in. No socket, no key, stable JSON."""

    name = "stub"

    def __repr__(self):
        return "StubProvider()"

    def complete(self, prompt, *, max_tokens):
        text = _stub_text(prompt)
        return Completion(text, _count(prompt), _count(text))


def resolve_provider(environ=None):
    """Stub when LLM_API_KEY is missing or blank. Otherwise the HTTP client.

    The key is read here and stored only on the provider instance.
    """
    env = os.environ if environ is None else environ
    key = (env.get("LLM_API_KEY") or "").strip()
    if not key:
        return StubProvider()
    base = (env.get("LLM_BASE_URL") or DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL
    model = (env.get("LLM_MODEL") or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    return OpenAICompatibleProvider(key, base, model)


def parse_json_object(text):
    """Parse a JSON object from a model reply, including a fenced block."""
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
        if raw.endswith("```"):
            raw = raw[: raw.rfind("```")]
        raw = raw.strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            raise LLMError("model reply was not a JSON object")
        try:
            data = json.loads(raw[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMError("model reply was not a JSON object") from exc
    if not isinstance(data, dict):
        raise LLMError("model reply was not a JSON object")
    return data


def _redact(message, secret):
    if secret and secret in message:
        return message.replace(secret, "[redacted]")
    return message


def _count(text):
    return max(1, (len(text or "") + 3) // 4)


def _field(prompt, key):
    prefix = key + ": "
    for line in (prompt or "").splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :]
    return ""


def _block(prompt, name):
    start = name + "_BEGIN"
    end = name + "_END"
    collecting = False
    lines = []
    for line in (prompt or "").splitlines():
        marker = line.strip()
        if marker == start:
            collecting = True
            continue
        if marker == end and collecting:
            break
        if collecting:
            lines.append(line)
    return "\n".join(lines)


def _stub_text(prompt):
    task = _field(prompt, "TASK")
    if task == "significance":
        return json.dumps(_significance(prompt), sort_keys=True)
    if task == "centurion":
        return json.dumps(_centurion(prompt), sort_keys=True)
    if task == "praetorian":
        return json.dumps(_praetorian(prompt), sort_keys=True)
    raise LLMError(f"stub has no response for task {task!r}")


_LARGE = {
    "capex": 1_000_000_000,
    "debt_raise": 200_000_000,
    "orders_backlog": 100_000_000,
    "insider_buying": 100_000,
    "accumulation": 10,
}


def _significance(prompt):
    lead_type = _field(prompt, "LEAD_TYPE") or "capex"
    text = _field(prompt, "VALUE_TEXT").casefold()
    try:
        value = float(_field(prompt, "VALUE") or "0")
    except ValueError:
        value = 0
    if "small-print" in text:
        magnitude = "small"
        magnitude_rationale = "The stored text marks this print as small."
    elif value >= _LARGE.get(lead_type, 1):
        magnitude = "large"
        magnitude_rationale = f"The stored figure {value:g} clears the large bar for {lead_type}."
    else:
        magnitude = "moderate"
        magnitude_rationale = f"The stored figure {value:g} clears the threshold and sits under the large bar."
    if "low-signal" in text or "routine" in text:
        novelty = "routine"
        novelty_rationale = "The stored text marks this as routine rather than a first occurrence."
    else:
        novelty = "first"
        novelty_rationale = "The stored text does not mark this as routine."
    if "already noticed" in text:
        attention = "noticed"
        attention_rationale = "The stored text says this item was already noticed."
    else:
        attention = "unnoticed"
        attention_rationale = "No media note is stored on the figure, so attention stays unnoticed."
    if "after earnings" in text:
        timing = "late"
        timing_rationale = "The stored text places this after earnings."
    else:
        timing = "early"
        timing_rationale = "No earnings-date note is stored, so timing stays early."
    return {
        "magnitude": magnitude,
        "magnitude_rationale": magnitude_rationale,
        "novelty": novelty,
        "novelty_rationale": novelty_rationale,
        "attention": attention,
        "attention_rationale": attention_rationale,
        "timing": timing,
        "timing_rationale": timing_rationale,
    }


_ROI = {
    "capex": 18,
    "orders_backlog": 14,
    "debt_raise": 11,
    "insider_buying": 9,
    "accumulation": 10,
}
_TIMEFRAME = {
    "capex": 365,
    "orders_backlog": 180,
    "debt_raise": 270,
    "insider_buying": 90,
    "accumulation": 120,
}


def _centurion(prompt):
    lead_type = _field(prompt, "LEAD_TYPE") or "capex"
    ticker = _field(prompt, "TICKER")
    pass_number = _field(prompt, "PASS") or "1"
    constituents = _constituents(prompt)
    suppliers, customers, buyers = _parties(prompt, constituents)
    grades = _grades(prompt)
    if pass_number == "2" and constituents:
        roi = max(_ROI.get(item.get("lead_type"), 8) for item in constituents) + 3
        timeframe = min(_TIMEFRAME.get(item.get("lead_type"), 365) for item in constituents)
        price_run = _price_run(constituents)
    else:
        roi = _ROI.get(lead_type, 8)
        timeframe = _TIMEFRAME.get(lead_type, 180)
        price_run = None
    risks = _risks(lead_type, price_run)
    assumptions = [
        "Market cap is not in the stored figure.",
        "The figures used here are the ones stored from the filing or the announcement.",
    ]
    macro = _field(prompt, "MACRO")
    if pass_number == "2" and macro and macro != "none":
        assumptions.append(f"Macro context used: {macro}")
    where = ticker
    if pass_number == "2" and suppliers:
        named = suppliers[0]
        where = named.get("ticker") or named.get("name") or ticker
    thesis = (
        f"Thesis for {ticker} {lead_type}. "
        f"A return depends on the official-source figure stored for this lead."
    )
    if suppliers:
        thesis += " Suppliers named: " + ", ".join(item["name"] for item in suppliers) + "."
    if customers:
        thesis += " Customers named: " + ", ".join(item["name"] for item in customers) + "."
    if buyers:
        thesis += " Buyers named: " + ", ".join(item["name"] for item in buyers) + "."
    return {
        "thesis": thesis,
        "roi_pct": roi,
        "roi": f"{roi}%",
        "timeframe_days": timeframe,
        "risks": risks,
        "recommendation": where,
        "assumptions": assumptions,
        "grades": grades,
        "suppliers": suppliers,
        "customers": customers,
        "buyers": buyers,
        "price_run": price_run,
    }


def _grades(prompt):
    grades = []
    for line in _block(prompt, "CRITERIA").splitlines():
        line = line.strip()
        if not line.startswith("- "):
            continue
        name, _, _description = line[2:].partition(" | ")
        name = name.strip()
        if not name:
            continue
        if "market_cap" in name or name == "raise_vs_company":
            passed = False
            evidence = "market cap is not in the stored figure"
        elif "growth" in name:
            passed = "PRIOR_COMPARISON: yes" in prompt
            evidence = "prior comparison in the figures table" if passed else "no prior comparison in the figures table"
        else:
            passed = True
            evidence = "the stored figure is an official-source row"
        grades.append({"name": name, "passed": passed, "evidence": evidence})
    return grades


def _parties(prompt, constituents):
    if _field(prompt, "PASS") == "2":
        suppliers, customers, buyers = [], [], []
        for item in constituents:
            suppliers.extend(item.get("suppliers") or [])
            customers.extend(item.get("customers") or [])
            buyers.extend(item.get("buyers") or [])
        return _unique_parties(suppliers), _unique_parties(customers), _unique_parties(buyers)
    value_text = _field(prompt, "VALUE_TEXT")
    figure_type = _field(prompt, "FIGURE_TYPE")
    instrument = _field(prompt, "INSTRUMENT")
    suppliers = [{"name": name, "ticker": ""} for name in _labeled(value_text, "supplier")]
    customers = [{"name": name, "ticker": ""} for name in _labeled(value_text, "customer")]
    buyers = [{"name": name, "ticker": ""} for name in _labeled(value_text, "buyer")]
    if figure_type == "supplier_order" and _is_party(instrument):
        buyers.append({"name": instrument.strip(), "ticker": ""})
    if figure_type in {"customer_backlog", "backlog"} and _is_party(instrument):
        customers.append({"name": instrument.strip(), "ticker": ""})
    return _unique_parties(suppliers), _unique_parties(customers), _unique_parties(buyers)


def _constituents(prompt):
    items = []
    for line in _block(prompt, "CONSTITUENTS").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            items.append(item)
    return items


def _price_run(constituents):
    blob = " ".join(str(item.get("value_text") or "") for item in constituents).casefold()
    if "priced-in" in blob:
        return "priced-in"
    kinds = {item.get("lead_type") for item in constituents if item.get("lead_type")}
    if len(kinds) > 1:
        return "acceleration"
    return "new"


def _risks(lead_type, price_run):
    risks = [
        f"The {lead_type} figure can be revised in a later filing.",
        "The path from this figure to a return is an assumption, not a booked gain.",
    ]
    if price_run == "priced-in":
        risks.append("Price run: priced-in. This is a concern because the move may already be in the price.")
    elif price_run:
        risks.append(f"Price run: {price_run}. A priced-in move would be a concern.")
    return risks


_STOP_PARTIES = {
    "quarter",
    "ytd",
    "annual",
    "ttm",
    "instant",
    "ceo",
    "bond",
    "facility",
    "loan",
    "filing",
}


def _is_party(text):
    cleaned = (text or "").strip()
    if len(cleaned) < 2 or cleaned.casefold() in _STOP_PARTIES:
        return False
    if any(char.isalpha() for char in cleaned):
        if any(char.islower() for char in cleaned) and any(char.isupper() for char in cleaned[1:]):
            # CamelCase disclosure tags are not party names.
            if " " not in cleaned:
                return False
        return True
    return False


def _labeled(text, role):
    found = []
    for match in re.finditer(rf"(?i)\b{role}s?\s*:\s*([^;\n]+)", text or ""):
        name = match.group(1).strip(" .")
        if _is_party(name):
            found.append(name)
    return found


def _unique_parties(items):
    seen = set()
    kept = []
    for item in items:
        name = (item.get("name") or "").strip()
        if not name:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        kept.append({"name": name, "ticker": (item.get("ticker") or "").strip()})
    return kept


def _praetorian(prompt):
    price_run = _field(prompt, "PRICE_RUN")
    thesis = _block(prompt, "THESIS")
    if price_run == "priced-in":
        assumptions = (
            "The price-run classification is priced-in, so the claim that this is still early does not hold."
        )
        contradicting = "A priced-in classification is counter-evidence to treating the finding as an early lead."
        issues = 1
    else:
        assumptions = ""
        contradicting = ""
        issues = 0
    if "sec.gov" in thesis or "asx.com.au" in thesis or "company release" in thesis.casefold():
        bad_sources = ""
    else:
        bad_sources = "The thesis does not cite an SEC filing, an ASX announcement, or a company release."
        issues += 1
    return {
        "assumptions_that_fail": assumptions,
        "bad_sources": bad_sources,
        "contradicting_research": contradicting,
        "other_side": (
            "The return fails if the named spending or order is cancelled before it reaches a filing the thesis can cite."
        ),
        "surviving_issues": issues,
    }
