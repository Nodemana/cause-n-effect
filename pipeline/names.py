"""Normalised counterparty names for the reject log."""

import re
import unicodedata


def normalise_name(value):
    """Fold a party name so a later search does not depend on case or punctuation."""
    text = unicodedata.normalize("NFKC", value or "")
    text = text.casefold()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())
