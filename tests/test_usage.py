# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Brad Anderson
import pytest

from darner import usage


@pytest.mark.parametrize("value", [None, "x", {}, 1])
def test_what_isnt_a_list_is_refused(value):
    assert usage.clean(value) is None


def test_bad_entries_are_dropped_not_the_rest():
    good = {"model": "m", "requests": 1}
    assert usage.clean([1, {"model": "m"}, {"model": "<i>", "requests": 1}, good]) == [good]
    # At most one more than the launcher's cap on named models: the unnamed rest.
    assert len(usage.clean([good] * 80)) == usage.MAX_MODELS + 1


def test_totals_keep_only_valid_counts():
    assert usage.clean([{"model": None, "requests": 2, "metered": 1, "input": -1, "output": 3, "extra": "x"}]) == [
        {"model": None, "requests": 2, "metered": 1, "output": 3},
    ]


def test_since_is_per_model_and_skips_models_with_no_new_requests():
    before = [{"model": "m", "requests": 1, "metered": 1, "input": 5}]
    now = [{"model": "m", "requests": 1, "metered": 1, "input": 5}, {"model": "n", "requests": 2, "metered": 2, "output": 7}]
    assert usage.since(now, before) == [{"model": "n", "requests": 2, "metered": 2, "output": 7}]
    assert usage.since(now, None) == now
    assert usage.since(None, before) is None
