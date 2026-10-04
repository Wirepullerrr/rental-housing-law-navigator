"""LeaseLens demo (app.py) and the submission package. Offline (conftest blocks the network)."""

from __future__ import annotations

import sys
from datetime import date

import pytest

from navigator.demo import (DEFAULT_AS_OF, DEMO_EXAMPLES, change_status, load_bundle, map_points, matches_submission,
                            rule_results)
from navigator.starter_pack import REPO_ROOT


@pytest.fixture(scope="module")
def b():
    return load_bundle()


def test_engine_rerun_reproduces_every_submitted_lookup_row(b):
    assert len(b.rows) == 500
    mismatched = [a for a in b.rows if not matches_submission(b, a, rule_results(b, a, DEFAULT_AS_OF))]
    assert mismatched == []


def test_demo_examples_are_resolved_and_show_rules(b):
    for aid in DEMO_EXAMPLES:
        assert b.resolutions[aid]["resolution_status"] == "resolved"
        assert rule_results(b, aid, DEFAULT_AS_OF)


def test_change_status_reads_the_submitted_changes(b):
    for aid in b.rows:
        st = change_status(b, aid)
        assert list(st) == ["T1", "T2", "T3", "T4", "T5"]
        assert st["T5"] == "not affected"
        assert (st["T3"] == "affected + conflict flag") == (aid in b.changes["T3"]["conflict_flag_address_ids"])


def test_unresolved_address_is_all_unknown_never_guessed(b):
    aid = next(a for a, r in b.resolutions.items() if r["resolution_status"] == "unresolved")
    assert {r["result"] for r in rule_results(b, aid, DEFAULT_AS_OF)} == {"unknown"}


def test_app_runs_offline_for_each_demo_example_and_another_date():
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(REPO_ROOT / "app.py"), default_timeout=120).run()
    assert not at.exception
    assert at.title[0].value == "LeaseLens"
    assert any("Not legal advice" in w.value for w in at.warning)
    for aid in DEMO_EXAMPLES:
        at.selectbox[0].set_value(aid).run()
        assert not at.exception
        assert any("Same answers as our submitted lookups.json" in c.value for c in at.caption)
        assert len(at.get("deck_gl_json_chart")) == 2          # property map + one scenario map
    at.date_input[0].set_value(date(2027, 7, 2)).run()
    assert not at.exception


def test_app_opens_on_the_boston_example_with_applies_only_and_t2():
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(REPO_ROOT / "app.py"), default_timeout=120).run()
    assert at.selectbox[0].value == "A0134" == next(iter(DEMO_EXAMPLES))
    groups = {g.proto.label: g for g in at.get("button_group")}
    assert groups["Show"].value == ["applies"]
    groups["Show"].select("pending").run()                      # other statuses are one click away
    assert not at.exception
    assert {g.proto.label: g for g in at.get("button_group")}["Show"].value == ["applies", "pending"]
    assert groups["Scenario to explore"].value == "T2"


def test_map_points_use_m4_coordinates_only(b):
    pts = map_points(b)
    with_coords = {a for a, r in b.resolutions.items() if r["latitude"] is not None and r["longitude"] is not None}
    assert {p["address_id"] for p in pts} == with_coords
    assert all(b.resolutions[p["address_id"]]["resolution_status"] != "unresolved" for p in pts)
    t3 = b.changes["T3"]
    flagged = {p["address_id"] for p in map_points(b, t3["affected_address_ids"], t3["conflict_flag_address_ids"])
               if p["conflict_flag"] == "yes"}
    assert flagged and flagged <= set(t3["conflict_flag_address_ids"])


def test_app_shows_a_note_when_an_address_has_no_coordinates(b):
    from streamlit.testing.v1 import AppTest
    aid = next(a for a, r in b.resolutions.items() if r["latitude"] is None)
    at = AppTest.from_file(str(REPO_ROOT / "app.py"), default_timeout=120).run()
    at.radio[0].set_value("All 500 challenge addresses").run()
    at.selectbox[0].set_value(aid).run()
    assert not at.exception
    assert any("Map unavailable for this address" in c.value for c in at.caption)
    assert len(at.get("deck_gl_json_chart")) == 1          # only the scenario map


def test_submission_package_is_identical_and_valid():
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import check_submission
    problems, counts = check_submission.check()
    assert problems == []
    assert counts["rules"] == 228 and counts["lookup_addresses"] == 500
    assert list(counts["change_tests"]) == ["T1", "T2", "T3", "T4", "T5"]
