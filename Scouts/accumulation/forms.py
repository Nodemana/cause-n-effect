"""Official form types this scout knows how to read.

SEC submission types come from the EDGAR Filer Manual index. SC 13D, SC 13D/A,
SC 13G, and SC 13G/A are the legacy HTML/ASCII types. SCHEDULE 13D, SCHEDULE 13D/A,
SCHEDULE 13G, and SCHEDULE 13G/A are the structured XML types filed through EDGAR
Online Forms. XML became mandatory for Schedule 13G on 30 September 2024 and for
Schedule 13D on 18 December 2024. A Schedule 13D/A combined with a tender-offer
form (SC TO-I, SC TO-I/A, SC TO-T, SC TO-T/A) can still arrive as non-XML SC 13D/A.

ASX substantial holder notices are ASIC Form 603 (new), Form 604 (change), and
Form 605 (ceasing). From 4 December 2026 ASIC's consolidated Substantial holding
notice replaces those three forms; this scout does not parse that later form.
"""

SEC_13D = ("SC 13D", "SC 13D/A", "SCHEDULE 13D", "SCHEDULE 13D/A")
SEC_13G = ("SC 13G", "SC 13G/A", "SCHEDULE 13G", "SCHEDULE 13G/A")
SEC_FORM_TYPES = SEC_13D + SEC_13G

ASX_FORMS = ("603", "604", "605")

_13D = {item.upper() for item in SEC_13D}
_13G = {item.upper() for item in SEC_13G}


def canonical_form(form_type):
    """Collapse internal whitespace and case so 'sc  13d/a' matches the manual."""
    if not form_type:
        return ""
    return " ".join(str(form_type).upper().split())


def sec_family(form_type):
    """Return ('13D'|'13G', amendment) or (None, False) when the type is unknown."""
    key = canonical_form(form_type)
    if key in _13D:
        return "13D", key.endswith("/A")
    if key in _13G:
        return "13G", key.endswith("/A")
    return None, False
