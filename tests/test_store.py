# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Brad Anderson
from darner.store import Store

E1 = {"kind": "matrix", "ref": "$e1", "channel": "!room:example.org"}
E2 = {"kind": "matrix", "ref": "$e2"}


def claim(store, task_id, source=E1, client="maubot"):
    return store.claim(task_id, client, source, "@u:example.org", "{}")


def test_claim_is_idempotent_per_source(tmp_path):
    store = Store(str(tmp_path / "db"))
    task, new = claim(store, "dn-aaaaa")
    assert new and task.status == "pending"
    store.mark("dn-aaaaa", "launched", target="darner:dn-aaaaa")

    again, new = claim(store, "dn-bbbbb")
    assert not new and again.id == "dn-aaaaa" and again.target == "darner:dn-aaaaa"

    _, new = claim(store, "dn-ccccc", E2)
    assert new


def test_failed_task_is_claimed_again(tmp_path):
    store = Store(str(tmp_path / "db"))
    claim(store, "dn-aaaaa")
    store.mark("dn-aaaaa", "failed", error="boom")
    task, new = claim(store, "dn-bbbbb")
    assert new and task.id == "dn-bbbbb"


def test_pending_task_is_not_relaunched(tmp_path):
    store = Store(str(tmp_path / "db"))
    claim(store, "dn-aaaaa")
    task, new = claim(store, "dn-bbbbb")
    assert not new and task.status == "pending"


def test_launched_and_ended(tmp_path):
    store = Store(str(tmp_path / "db"))
    claim(store, "dn-aaaaa")
    claim(store, "dn-bbbbb", E2)
    store.mark("dn-aaaaa", "launched", target="darner:dn-aaaaa")
    store.mark("dn-bbbbb", "launched", target="darner:dn-bbbbb")
    store.mark("dn-aaaaa", "ended")
    assert store.launched() == ["dn-bbbbb"]


def test_events_are_scoped_to_client_and_ordered(tmp_path):
    store = Store(str(tmp_path / "db"))
    claim(store, "dn-aaaaa")
    claim(store, "dn-bbbbb", E2, client="other")
    first = store.add_event("dn-aaaaa", "output", "summary", 10)
    theirs = store.add_event("dn-bbbbb", "output")
    second = store.add_event("dn-aaaaa", "ended")

    events = store.events("maubot")
    assert [(e.seq, e.type) for e in events] == [(first, "output"), (second, "ended")]
    assert events[0].source == E1 and events[0].summary == "summary" and events[0].size == 10
    assert store.event("maubot", theirs) is None
    assert not store.ack("maubot", theirs)
    assert [e.seq for e in store.events("other")] == [theirs]


def test_ack_hides_event_and_is_idempotent(tmp_path):
    store = Store(str(tmp_path / "db"))
    claim(store, "dn-aaaaa")
    seq = store.add_event("dn-aaaaa", "ended")
    assert store.ack("maubot", seq) and store.ack("maubot", seq)
    assert store.events("maubot") == []
    assert store.event("maubot", seq).type == "ended"


def test_same_source_from_different_clients_is_separate(tmp_path):
    store = Store(str(tmp_path / "db"))
    claim(store, "dn-aaaaa")
    store.mark("dn-aaaaa", "launched")
    task, new = claim(store, "dn-bbbbb", client="other")
    assert new and task.id == "dn-bbbbb"
