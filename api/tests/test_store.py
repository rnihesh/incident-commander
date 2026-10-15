import sqlite3

from ic.actions import pr_store
from ic.store import Store


def test_put_get_round_trip(tmp_path):
    store = Store(tmp_path / "nested" / "ic.sqlite3")
    store.put_answer(
        fault_id="f1",
        session_id="s1",
        incident_id="inc-001",
        fault_class="bad_deploy",
        root_id="chg-00001",
        nonce="abc",
        created_at=5,
    )
    row = store.get_answer("inc-001")
    assert row == {
        "fault_id": "f1",
        "session_id": "s1",
        "incident_id": "inc-001",
        "fault_class": "bad_deploy",
        "root_id": "chg-00001",
        "nonce": "abc",
        "created_at": 5,
        "commitment": None,  # judge commit-reveal hash (U14), optional
    }
    assert store.get_answer("inc-999") is None
    store.put_answer(
        fault_id="f2",
        session_id="s1",
        incident_id="inc-002",
        fault_class="bad_deploy",
        root_id="chg-00002",
        nonce="def",
        commitment="c" * 64,
    )
    assert store.get_answer("inc-002", "s1")["commitment"] == "c" * 64


def test_session_scoping_and_latest_wins(tmp_path):
    store = Store(tmp_path / "ic.sqlite3")
    for fid, sid, root, at in (("a", "s1", "chg-1", 1), ("b", "s2", "chg-2", 2)):
        store.put_answer(
            fault_id=fid,
            session_id=sid,
            incident_id="inc-001",
            fault_class="flag_flip",
            root_id=root,
            nonce="n",
            created_at=at,
        )
    assert store.get_answer("inc-001", "s1")["root_id"] == "chg-1"
    assert store.get_answer("inc-001")["root_id"] == "chg-2"


def test_init_is_idempotent_and_keeps_data(tmp_path):
    path = tmp_path / "ic.sqlite3"
    store = Store(path)
    store.put_answer(
        fault_id="f1",
        session_id="s",
        incident_id="i",
        fault_class="c",
        root_id="r",
        nonce="n",
    )
    store.init()
    Store(path).init()
    assert store.get_answer("i")["fault_id"] == "f1"


def test_chain_anchor_round_trip_and_reanchor(tmp_path):
    store = Store(tmp_path / "ic.sqlite3")
    assert store.get_anchor("s1", "inc-001") is None
    store.put_anchor(session_id="s1", incident_id="inc-001", head_hash="aa", anchored_at=1)
    store.put_anchor(session_id="s2", incident_id="inc-001", head_hash="bb", anchored_at=1)
    assert store.get_anchor("s1", "inc-001") == {
        "session_id": "s1",
        "incident_id": "inc-001",
        "head_hash": "aa",
        "anchored_at": 1,
        "t": None,
        "world": None,
    }
    store.put_anchor(session_id="s1", incident_id="inc-001", head_hash="cc")
    assert store.get_anchor("s1", "inc-001")["head_hash"] == "cc"
    assert store.get_anchor("s2", "inc-001")["head_hash"] == "bb"
    store.put_answer(
        fault_id="f",
        session_id="s1",
        incident_id="inc-001",
        fault_class="c",
        root_id="r",
        nonce="n",
    )
    assert store.get_answer("inc-001", "s1")["root_id"] == "r"  # answer key API unchanged


def test_chain_anchor_binds_alert_time_and_world(tmp_path):
    store = Store(tmp_path / "ic.sqlite3")
    store.put_anchor(
        session_id="s1", incident_id="inc-001", head_hash="aa", t=1700, world="s:s1:world"
    )
    row = store.get_anchor("s1", "inc-001")
    assert (row["head_hash"], row["t"], row["world"]) == ("aa", 1700, "s:s1:world")


def test_chain_anchor_migration_adds_columns_and_keeps_old_rows(tmp_path):
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as c:  # the pre-migration schema, with one anchored row
        c.execute(
            "CREATE TABLE chain_anchor (session_id TEXT NOT NULL, incident_id TEXT NOT NULL, "
            "head_hash TEXT NOT NULL, anchored_at INTEGER NOT NULL, "
            "PRIMARY KEY (session_id, incident_id))"
        )
        c.execute("INSERT INTO chain_anchor VALUES ('s1', 'inc-001', 'aa', 1)")
    c.close()
    store = Store(path)
    Store(path)  # migrating twice is a no-op
    assert store.get_anchor("s1", "inc-001") == {
        "session_id": "s1",
        "incident_id": "inc-001",
        "head_hash": "aa",
        "anchored_at": 1,
        "t": None,
        "world": None,
    }
    store.put_anchor(session_id="s1", incident_id="inc-002", head_hash="bb", t=5, world="w")
    assert store.get_anchor("s1", "inc-002")["t"] == 5
    with sqlite3.connect(path) as c:
        cols = [r[1] for r in c.execute("PRAGMA table_info(chain_anchor)")]
    c.close()
    assert cols.count("t") == 1 and cols.count("world") == 1


def _tables(path) -> set[str]:
    with sqlite3.connect(path) as c:
        names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    c.close()
    return names


def test_init_creates_the_pr_tables_up_front(tmp_path):
    path = tmp_path / "ic.sqlite3"
    Store(path)
    assert {"pr_graphs", "pr_anchor", "rehearsal_seal"} <= _tables(path)


def test_pr_graphs_migration_adds_columns_and_keeps_old_rows(tmp_path):
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as c:  # pr_graphs as first deployed, with one tracked copy
        c.execute(
            "CREATE TABLE pr_graphs (graph_name TEXT PRIMARY KEY, incident_id TEXT NOT NULL, "
            "sid TEXT NOT NULL, pr_url TEXT, created_at REAL NOT NULL, proof_ref TEXT NOT NULL)"
        )
        c.execute(
            "INSERT INTO pr_graphs VALUES ('pr:s1:inc-001:world', 'inc-001', 's1', "
            "'https://github.com/o/r/pull/1', 1.0, ?)",
            ("a" * 24,),
        )
    c.close()
    store = Store(path)
    Store(path)  # migrating twice is a no-op
    with sqlite3.connect(path) as c:
        cols = [r[1] for r in c.execute("PRAGMA table_info(pr_graphs)")]
    c.close()
    assert cols.count("head_sha") == 1 and cols.count("simulated") == 1
    assert {"pr_anchor", "rehearsal_seal"} <= _tables(path)
    # pr_store reads and writes it without creating or migrating anything itself.
    assert pr_store.resolve_proof_ref(store, "a" * 24) == ("s1", "inc-001")
    assert pr_store.pr_record(store, "s1", "inc-001") == {
        "pr_url": "https://github.com/o/r/pull/1",
        "head_sha": None,
        "simulated": None,
        "created_at": 1.0,
    }
    pr_store.set_pr_url(store, "s1", "inc-001", "https://github.com/o/r/pull/1", "abc", True)
    rec = pr_store.pr_record(store, "s1", "inc-001")
    assert (rec["head_sha"], rec["simulated"]) == ("abc", True)
