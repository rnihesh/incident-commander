from ic.agent import payloads
from ic.agent.payloads import content_hash, iso

ONSET = 1_700_003_600
WINDOW_MIN = 60


def _cert() -> dict:
    return {
        "incident_id": "inc-1",
        "suspect_id": "chg-root",
        "window": {"from": ONSET - WINDOW_MIN * 60, "to": ONSET + 600, "minutes": WINDOW_MIN},
        "checked_services": ["svc-a", "svc-b"],
        "checked": [
            {
                "id": "chg-root",
                "service_id": "svc-a",
                "event_time": ONSET - 300,
                "ok": True,
                "clause": None,
            },
            {
                "id": "chg-late",
                "service_id": "svc-b",
                "event_time": ONSET + 120,
                "ok": False,
                "clause": "after_onset",
            },
            {
                "id": "chg-cov",
                "service_id": "svc-b",
                "event_time": None,
                "ok": False,
                "clause": "coverage",
            },
        ],
        "qualifying": [],
        "clean": True,
    }


def test_certificate_adds_event_time_and_onset():
    out = payloads.certificate(_cert())
    assert out["onset"] == iso(ONSET)
    by_id = {c["change"]: c for c in out["checked"]}
    assert by_id["chg-late"]["event_time"] == iso(ONSET + 120)
    # an unknown change time stays absent rather than being invented
    assert "event_time" not in by_id["chg-cov"]
    assert "chg-root" not in by_id


def test_certificate_carries_the_suspects_own_failing_clause():
    cert = _cert()
    assert payloads.certificate(cert)["suspect_clause"] is None  # no suspect verdict
    cert["suspect"] = {"id": "chg-root", "event_time": ONSET - 300, "ok": True, "clause": None}
    assert payloads.certificate(cert)["suspect_clause"] is None
    cert["suspect"] = {"id": "chg-root", "event_time": ONSET - 300, "ok": False, "clause": "origin"}
    before = content_hash(cert)
    out = payloads.certificate(cert)
    assert out["suspect_clause"] == "origin"
    assert out["hash"] == before == content_hash(cert)  # UI-only: never hashed


def test_certificate_ui_fields_do_not_touch_the_hash():
    cert = _cert()
    before = content_hash(cert)
    out = payloads.certificate(cert)
    assert out["hash"] == before
    assert content_hash(cert) == before
