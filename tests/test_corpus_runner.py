"""M3 corpus runner: explicit selection, sequential processing, resume, isolation, budget gate,
call limits and aggregation. Offline: a scripted provider stands in for the API."""

from __future__ import annotations

import json
import re
from collections import Counter

import pytest

from conftest import make_candidate, provision
from navigator import starter_pack as sp
from navigator.extraction.cache import ResponseCache
from navigator.extraction.config import GENERATION_SETTINGS
from navigator.extraction.corpus import (IntegrityViolation, MeteredProvider, SpendLedger, estimate_cost,
                                         run_stage, summary_path, supplied_documents)
from navigator.extraction.prompt import REPAIR_SYSTEM_INSTRUCTION
from navigator.extraction.provider import ProviderResult

USAGE = {"input_tokens": 1000, "output_tokens": 2000, "thinking_tokens": 3000}
CALL_COST = (1000 * 0.75 + 5000 * 3.75) / 1e6          # thinking billed as output

EMPTY = {"provisions": [], "global_scope": [], "rules": []}                     # complete, nothing in scope
COMPLETE_D052 = {"provisions": [provision("§ 15B(1)(b)", rule_indices=[0])], "global_scope": [],
                 "rules": [make_candidate()]}
NO_INVENTORY = {"rules": [make_candidate()]}                                       # review_required
RETURN_RULE = make_candidate(citation="M.G.L. c. 186, § 15B(4)", title="Return", key_value=None,
                             quoted_span="The lessor shall, within thirty days after the termination of occupancy",
                             requirement="The lessor must return the deposit within thirty days.")
NEEDS_REPAIR_D052 = ({"provisions": [provision("§ 15B(1)(b)", rule_indices=[0]), provision("§ 15B(4)")],
                      "global_scope": [], "rules": [make_candidate()]},
                     {"target_resolutions": [{"ref": "§ 15B(4)", "scope": "in_scope", "reason": None,
                                              "evidence_parts": []}], "rules": [RETURN_RULE]})


class ScriptedProvider:
    """Answers per document (read from the prompt) and pass; records every request in order."""

    name, model = "fake", "fake-model"

    def __init__(self, script: dict[str, tuple]) -> None:
        self.script = script
        self.calls: list[tuple[str, str]] = []

    def generate(self, *, system_instruction, prompt, response_json_schema, settings) -> ProviderResult:
        doc_id = re.search(r"doc_id: (D\d+)", prompt).group(1)
        kind = "repair" if system_instruction == REPAIR_SYSTEM_INSTRUCTION else "primary"
        self.calls.append((doc_id, kind))
        primary, *repair = self.script[doc_id]
        response = primary if kind == "primary" else repair[0]
        return ProviderResult(text=json.dumps(response), metadata={"model": self.model, "usage": dict(USAGE)})


class NoCalls(ScriptedProvider):
    def generate(self, **kwargs):
        raise AssertionError("no provider request was expected")


@pytest.fixture
def stage(tmp_path):
    cache = ResponseCache(tmp_path / "cache")

    def _run(doc_ids, provider, settings=GENERATION_SETTINGS, budget=1.0, **kwargs):
        return run_stage(doc_ids, out_dir=tmp_path / "stage1", cache=cache, provider_name="fake",
                         model="fake-model", provider=provider, settings=settings, budget_usd=budget, **kwargs)

    return _run


def by_id(summary):
    return {r["doc_id"]: r for r in summary["documents"]}


def test_discovery_reads_the_manifest_without_assuming_a_corpus_size():
    _, rows = sp.read_csv(sp.REPO_ROOT / sp.MANIFEST_PATH)
    docs = supplied_documents()
    assert len(docs) == sum(sp.classify_source(r) == sp.SUPPLIED_TEXT for r in rows)
    assert {"D052", "D073"} <= {d["doc_id"] for d in docs} and "D002" not in {d["doc_id"] for d in docs}


def test_only_the_explicit_selection_is_processed_in_order(stage, tmp_path):
    provider = ScriptedProvider({"D081": (EMPTY,), "D052": (COMPLETE_D052,), "D080": (EMPTY,)})
    summary = stage(["D081", "D052", "D080"], provider)
    assert provider.calls == [("D081", "primary"), ("D052", "primary"), ("D080", "primary")]
    assert sorted(p.name for p in (tmp_path / "stage1" / "documents").iterdir()) == \
        ["D052_extraction.json", "D080_extraction.json", "D081_extraction.json"]
    assert [r["run_mode"] for r in summary["documents"]] == ["processed"] * 3
    assert json.loads(summary_path(tmp_path / "stage1").read_text(encoding="utf-8"))["selection"] == \
        ["D081", "D052", "D080"]


def test_resume_reuses_artifacts_and_cached_responses_without_new_requests(stage, tmp_path):
    first = stage(["D052", "D081"], ScriptedProvider({"D052": (COMPLETE_D052,), "D081": (EMPTY,)}))
    (tmp_path / "stage1" / "documents" / "D081_extraction.json").unlink()   # interrupted before the write
    again = stage(["D052", "D081"], NoCalls({}))
    rows = by_id(again)
    assert (rows["D052"]["run_mode"], rows["D081"]["run_mode"]) == ("resumed", "processed")
    assert rows["D081"]["primary_cache_hit"] and rows["D081"]["primary_provider_calls"] == 0
    assert again["estimated_new_spend_usd"] == {"this_invocation": 0.0,
                                                "stage_ledger_total": first["estimated_new_spend_usd"]
                                                ["stage_ledger_total"]}


def test_an_artifact_from_other_settings_is_never_overwritten_silently(stage, tmp_path):
    stage(["D081"], ScriptedProvider({"D081": (EMPTY,)}))
    path = tmp_path / "stage1" / "documents" / "D081_extraction.json"
    before = path.read_text(encoding="utf-8")
    low = {**GENERATION_SETTINGS, "thinking_level": "low"}
    refused = stage(["D081"], ScriptedProvider({"D081": (EMPTY,)}), settings=low)
    assert by_id(refused)["D081"]["run_mode"] == "error" and path.read_text(encoding="utf-8") == before
    replaced = stage(["D081"], ScriptedProvider({"D081": (EMPTY,)}), settings=low, replace=frozenset({"D081"}))
    assert by_id(replaced)["D081"]["run_mode"] == "processed"
    assert [p.read_text(encoding="utf-8") for p in path.parent.glob("D081_extraction.superseded-*.json")] == [before]


def test_document_failures_are_isolated_and_review_required_does_not_abort(stage):
    summary = stage(["D002", "D052", "D081"], ScriptedProvider({"D052": (NO_INVENTORY,), "D081": (EMPTY,)}))
    rows = by_id(summary)
    assert rows["D002"]["run_mode"] == "error" and "no supplied local text" in rows["D002"]["detail"]
    assert rows["D052"]["document_status"] == "review_required"
    assert rows["D081"]["run_mode"] == "processed" and summary["stop_reason"] is None


def test_budget_gate_stops_before_a_new_primary_request(stage):
    provider = ScriptedProvider({"D052": (COMPLETE_D052,), "D081": (EMPTY,)})
    summary = stage(["D052", "D081", "D080"], provider, budget=0.01)
    assert provider.calls == [("D052", "primary")]                 # started below the gate; nothing after
    rows = by_id(summary)
    assert rows["D081"]["run_mode"] == "not_run" and "budget gate" in rows["D081"]["detail"]
    assert rows["D080"]["run_mode"] == "not_run" and summary["stop_reason"] == "budget gate reached"
    assert summary["estimated_new_spend_usd"]["stage_ledger_total"] == pytest.approx(CALL_COST)


def test_budget_gate_also_blocks_the_repair_request(stage):
    provider = ScriptedProvider({"D052": NEEDS_REPAIR_D052})
    summary = stage(["D052"], provider, budget=0.01)
    assert provider.calls == [("D052", "primary")]
    row = by_id(summary)["D052"]
    assert row["document_status"] == "review_required" and row["unresolved_targets"] == ["§ 15B(4)"]
    assert summary["stop_reason"] == "budget gate reached"


def test_at_most_one_primary_and_one_repair_request_per_document(stage, tmp_path):
    provider = ScriptedProvider({"D052": NEEDS_REPAIR_D052})
    row = by_id(stage(["D052"], provider))["D052"]
    assert provider.calls == [("D052", "primary"), ("D052", "repair")]
    assert (row["primary_provider_calls"], row["repair_provider_calls"], row["document_status"]) == (1, 1, "complete")
    metered = MeteredProvider(ScriptedProvider({"D052": (COMPLETE_D052,)}), SpendLedger(tmp_path / "l.json"), 1.0)
    metered.doc_id = "D052"
    request = {"system_instruction": "primary", "prompt": "doc_id: D052", "response_json_schema": {}, "settings": {}}
    metered.generate(**request)
    with pytest.raises(IntegrityViolation):
        metered.generate(**request)
    assert metered.inner.calls == [("D052", "primary")]            # the second request never started


def test_summary_aggregates_outcomes_tokens_and_cost(stage):
    summary = stage(["D052", "D081"], ScriptedProvider({"D052": NEEDS_REPAIR_D052, "D081": (EMPTY,)}))
    t = summary["totals"]
    assert (t["processed"], t["complete"], t["accepted_rules"], t["documents_requiring_repair"],
            t["repair_resolved_by_rule"], t["primary_provider_calls"], t["repair_provider_calls"]) == (2, 2, 2, 1, 1, 2, 1)
    assert t["new_tokens"]["primary"] == {"input_tokens": 2000, "output_tokens": 4000, "thinking_tokens": 6000}
    assert t["new_tokens"]["repair"] == {"input_tokens": 1000, "output_tokens": 2000, "thinking_tokens": 3000}
    assert t["estimated_new_cost_usd"] == pytest.approx(3 * CALL_COST)
    assert by_id(summary)["D052"]["estimated_new_cost_usd"]["total"] == pytest.approx(2 * CALL_COST)
    assert t["exact_citation_rate"] == 1.0
    assert estimate_cost(USAGE) == pytest.approx(CALL_COST) and Counter(summary["selection"]) == Counter(["D052", "D081"])
