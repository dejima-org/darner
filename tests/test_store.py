from meeseeks.store import Store

E1 = {"kind": "matrix", "ref": "$e1", "channel": "!room:example.org"}
E2 = {"kind": "matrix", "ref": "$e2"}


def claim(store, task_id, source=E1, client="maubot"):
    return store.claim(task_id, client, source, "@u:example.org", "{}")


def test_claim_is_idempotent_per_source(tmp_path):
    store = Store(str(tmp_path / "db"))
    task, new = claim(store, "ms-aaaaa")
    assert new and task.status == "pending"
    store.mark("ms-aaaaa", "launched", target="meeseeks:ms-aaaaa")

    again, new = claim(store, "ms-bbbbb")
    assert not new and again.id == "ms-aaaaa" and again.target == "meeseeks:ms-aaaaa"

    _, new = claim(store, "ms-ccccc", E2)
    assert new


def test_failed_task_is_claimed_again(tmp_path):
    store = Store(str(tmp_path / "db"))
    claim(store, "ms-aaaaa")
    store.mark("ms-aaaaa", "failed", error="boom")
    task, new = claim(store, "ms-bbbbb")
    assert new and task.id == "ms-bbbbb"


def test_pending_task_is_not_relaunched(tmp_path):
    store = Store(str(tmp_path / "db"))
    claim(store, "ms-aaaaa")
    task, new = claim(store, "ms-bbbbb")
    assert not new and task.status == "pending"


def test_launched_and_ended(tmp_path):
    store = Store(str(tmp_path / "db"))
    claim(store, "ms-aaaaa")
    claim(store, "ms-bbbbb", E2)
    store.mark("ms-aaaaa", "launched", target="meeseeks:ms-aaaaa")
    store.mark("ms-bbbbb", "launched", target="meeseeks:ms-bbbbb")
    store.mark("ms-aaaaa", "ended")
    assert store.launched() == ["ms-bbbbb"]


def test_events_are_scoped_to_client_and_ordered(tmp_path):
    store = Store(str(tmp_path / "db"))
    claim(store, "ms-aaaaa")
    claim(store, "ms-bbbbb", E2, client="other")
    first = store.add_event("ms-aaaaa", "output", "summary", 10)
    theirs = store.add_event("ms-bbbbb", "output")
    second = store.add_event("ms-aaaaa", "ended")

    events = store.events("maubot")
    assert [(e.seq, e.type) for e in events] == [(first, "output"), (second, "ended")]
    assert events[0].source == E1 and events[0].summary == "summary" and events[0].size == 10
    assert store.event("maubot", theirs) is None
    assert not store.ack("maubot", theirs)
    assert [e.seq for e in store.events("other")] == [theirs]


def test_ack_hides_event_and_is_idempotent(tmp_path):
    store = Store(str(tmp_path / "db"))
    claim(store, "ms-aaaaa")
    seq = store.add_event("ms-aaaaa", "ended")
    assert store.ack("maubot", seq) and store.ack("maubot", seq)
    assert store.events("maubot") == []
    assert store.event("maubot", seq).type == "ended"


def test_same_source_from_different_clients_is_separate(tmp_path):
    store = Store(str(tmp_path / "db"))
    claim(store, "ms-aaaaa")
    store.mark("ms-aaaaa", "launched")
    task, new = claim(store, "ms-bbbbb", client="other")
    assert new and task.id == "ms-bbbbb"
