"""Placeholder for geopolitics, election, and regulatory risk notes.

Official numeric feeds do not cover these. Centurions call get_notes and
receive an empty list with status placeholder, so silence is not an all-clear.
Nothing here contacts the network.
"""

TOPICS = ("geopolitics", "election", "regulatory")


def list_topics():
    return [get_notes(topic) for topic in TOPICS]


def get_notes(topic=None):
    """Sourced notes for one topic, or every topic when topic is omitted.

    The result is always status placeholder and an empty notes list.
    """
    if topic is None:
        return list_topics()
    if topic not in TOPICS:
        raise ValueError(f"unknown risk topic {topic!r}")
    return {"topic": topic, "status": "placeholder", "notes": []}
