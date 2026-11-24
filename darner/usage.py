# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Brad Anderson
"""A session's token usage: per model, as dejima's credential proxy counted it.

Totals are a list of {model, requests, metered, input, output, cache_read,
cache_write}. A count the provider didn't send is absent, never zero, and
metered is how many requests carried any counts. None, rather than a list,
means the session isn't metered at all, such as a self-hosted model's.
"""

from darner.job import valid_model

COUNTS = ("input", "output", "cache_read", "cache_write")
KEYS = ("requests", "metered", *COUNTS)
MAX = 10**12
# The launcher counts models past 50 unnamed, so there's one more entry at most.
MAX_MODELS = 50


def clean(value: object) -> list[dict] | None:
    """Totals as the launcher reported them, re-checked, or None if they aren't a list.

    An entry that isn't a model's totals is dropped rather than refusing the
    rest: what's left is still what the session spent, and None would say it
    wasn't metered at all.
    """
    if not isinstance(value, list):
        return None
    out = []
    for entry in value[:MAX_MODELS + 1]:
        if not isinstance(entry, dict) or not (entry.get("model") is None or valid_model(entry.get("model"))):
            continue
        counts = {k: entry[k] for k in KEYS if type(entry.get(k)) is int and 0 <= entry[k] < MAX}
        if "requests" in counts:
            out.append({"model": entry.get("model"), **counts})
    return out


def since(current: list[dict] | None, previous: list[dict] | None) -> list[dict] | None:
    """What was used between two totals: each model's requests and counts since the previous one."""
    if current is None:
        return None
    before = {t["model"]: t for t in previous or []}
    out = []
    for total in current:
        prior = before.get(total["model"], {})
        delta = {k: total[k] - prior.get(k, 0) for k in KEYS if k in total}
        if delta.get("requests", 0) > 0:
            out.append({"model": total["model"], **delta})
    return out
