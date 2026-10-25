from meeseeks.store import Store


def test_claim_is_idempotent_per_source(tmp_path):
    store = Store(str(tmp_path / "db"))
    task, new = store.claim("ms-aaaaa", "matrix", "$e1", "@u:example.org", "{}")
    assert new and task.status == "pending"
    store.mark("ms-aaaaa", "launched", target="meeseeks:ms-aaaaa")

    again, new = store.claim("ms-bbbbb", "matrix", "$e1", "@u:example.org", "{}")
    assert not new and again.id == "ms-aaaaa" and again.target == "meeseeks:ms-aaaaa"

    _, new = store.claim("ms-ccccc", "matrix", "$e2", "@u:example.org", "{}")
    assert new


def test_failed_task_is_claimed_again(tmp_path):
    store = Store(str(tmp_path / "db"))
    store.claim("ms-aaaaa", "matrix", "$e1", "@u:example.org", "{}")
    store.mark("ms-aaaaa", "failed", error="boom")
    task, new = store.claim("ms-bbbbb", "matrix", "$e1", "@u:example.org", "{}")
    assert new and task.id == "ms-bbbbb"


def test_pending_task_is_not_relaunched(tmp_path):
    store = Store(str(tmp_path / "db"))
    store.claim("ms-aaaaa", "matrix", "$e1", "@u:example.org", "{}")
    task, new = store.claim("ms-bbbbb", "matrix", "$e1", "@u:example.org", "{}")
    assert not new and task.status == "pending"
