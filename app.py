"""LeaseLens: Streamlit demo of the Rental Housing Law Navigator.

    uv run streamlit run app.py

Offline. Reads the frozen M3-M6 outputs through navigator.demo; no Gemini, Census or database
calls. Informational prototype for hackathon purposes. Not legal advice.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from navigator.demo import (CATEGORY_LABELS, DEFAULT_AS_OF, DEMO_EXAMPLES, RESULT_LABELS, address_label,  # noqa: E402
                            change_status, load_bundle, matches_submission, rule_results)

DISCLAIMER = "Informational prototype for hackathon purposes. Not legal advice."
UNKNOWN_NOTE = ("**Unknown** means the supplied property data does not contain a fact required to determine "
                "applicability. It is an honest answer, not a failure.")
BADGE = {"applies": ":green-badge[Applies]", "unknown": ":orange-badge[Unknown]", "pending": ":blue-badge[Pending]",
         "not_yet_effective": ":violet-badge[Not yet effective]", "superseded": ":gray-badge[Superseded]"}
STATUS_ORDER = ["applies", "not_yet_effective", "pending", "unknown", "superseded"]
BASIS_TEXT = {
    "verified_held_records": "Rule records extracted from supplied text with exact quotes (held in M3 only for a "
                             "missing effective date; the scenario supplies it).",
    "published_records": "Published rule records with exact quotes.",
    "supplied_change_fact_plus_verified_effective_date": "Organizer-supplied scenario facts, with the effective "
                                                         "date verified from exact spans of the source text.",
    "supplied_change_fact_only": "Organizer-supplied scenario facts only. The source is link-only in the supplied "
                                 "corpus, so there is no citable text and no quote is shown.",
}
_MD_SPECIAL = str.maketrans({c: "\\" + c for c in "\\`*_[]<>#|$~"})


def md(text: object) -> str:
    """Escape source text for st.markdown ('$' would otherwise start LaTeX)."""
    return str(text).translate(_MD_SPECIAL)


def units_text(facts) -> str:
    if facts.units_source == "conflict":
        return "_conflicting values in the data (treated as unknown)_"
    if not facts.units_known:
        return "_not in the data_"
    n = (str(facts.units_min) if facts.units_min == facts.units_max
         else f"{facts.units_min}–{facts.units_max}" if facts.units_max else f"{facts.units_min}+")
    return n + (" (range stated in the use description)" if facts.units_source == "use_description" else "")


st.set_page_config(page_title="LeaseLens", page_icon="⚖️", layout="wide")


@st.cache_resource(show_spinner="Loading frozen outputs…")
def bundle():
    return load_bundle()


@st.cache_data(show_spinner=False)
def results_for(address_id: str, as_of: date):
    return rule_results(bundle(), address_id, as_of)


b = bundle()

# ---------------------------------------------------------------- header
st.title("LeaseLens")
st.markdown("#### Rental Housing Law Navigator")
st.caption("Auditable rental-law guidance by property, jurisdiction, and date.")
st.warning(DISCLAIMER, icon="⚠️")

# ---------------------------------------------------------------- sidebar inputs
with st.sidebar:
    st.header("Lookup")
    source = st.radio("Choose from", ["Demo examples", "All 500 challenge addresses"], horizontal=False)
    options = list(DEMO_EXAMPLES) if source == "Demo examples" else sorted(b.rows)
    aid = st.selectbox("Address", options, format_func=lambda a: (
        f"{address_label(b, a)} — {DEMO_EXAMPLES[a]}" if source == "Demo examples" else address_label(b, a)))
    as_of = st.date_input("As-of date", value=DEFAULT_AS_OF, min_value=date(2026, 1, 1),
                          max_value=date(2027, 12, 31), format="YYYY-MM-DD")
    st.caption("The deterministic pipeline is built and validated for the 500 supplied sample addresses. "
               "Lookups use the committed outputs only; no network calls.")
    st.divider()
    st.caption(DISCLAIMER)

tab_lookup, tab_changes, tab_about = st.tabs(["Address lookup", "Change scenarios (T1–T5)", "How it works"])

# ---------------------------------------------------------------- address lookup
with tab_lookup:
    row, facts, res = b.rows[aid], b.facts[aid], b.resolutions[aid]
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("A. Property")
        st.markdown(f"**{md(row['street_address'])}, {md(row['postal_city'])}, {row['state']} {row['zip']}**")
        st.markdown(f"- Address ID: `{aid}`\n"
                    f"- Year built: {facts.year_built if facts.year_built is not None else '_not in the data_'}\n"
                    f"- Units: {units_text(facts)}\n"
                    f"- Use: {md(row['use_description'] or '—')}\n"
                    f"- Source dataset: {md(row['source_dataset'])}")
    with c2:
        st.subheader("B. Jurisdiction")
        status = res["resolution_status"]
        if status == "unresolved":
            st.error("Jurisdiction unresolved: Census evidence did not identify a city or state, so nothing is "
                     "guessed and every rule is reported as unknown.")
        else:
            st.markdown(f"- State: **{res['state_jurisdiction']}**\n"
                        f"- Local jurisdiction: **{res['local_jurisdiction']}**\n"
                        f"- Census match: {md(res['matched_address'] or '—')} ({res['match_type'] or 'override'})\n"
                        f"- Census place: {md(res['census_place_name'] or '—')}"
                        + (f", {md(res['county']['name'])}" if res.get("county") else ""))
            postal = row["postal_city"].strip().lower()
            if res["local_jurisdiction"] and postal != res["local_jurisdiction"].split(",")[0].lower():
                st.info(f"Postal city **{md(row['postal_city'])}** is not the legal city: Census places this "
                        f"address in **{res['local_jurisdiction']}**.")
            if status == "review_required":
                st.warning("Census match flagged for review: " + md("; ".join(res["review_reasons"]))
                           + ". Results use this jurisdiction with a warning.")
            if res.get("override"):
                st.info("Reviewed override: " + md(res["override"].get("authoritative_reason", ""))[:400] + "…")
            for w in res.get("warnings") or []:
                st.caption("Geography note: " + md(w))

    st.subheader("C. Rules for this property")
    results = results_for(aid, as_of)
    counts = {k: sum(r["result"] == k for r in results) for k in STATUS_ORDER}
    m = st.columns(5)
    for col, k in zip(m, STATUS_ORDER):
        col.metric(RESULT_LABELS[k], counts[k])
    if as_of == DEFAULT_AS_OF:
        if matches_submission(b, aid, results):
            st.success("Identical to the submitted `lookups.json` row for this address (as of 2026-10-01).", icon="✅")
        else:
            st.error("Differs from the submitted lookups.json row.")
    else:
        st.info(f"Re-evaluated for {as_of.isoformat()} with the same deterministic engine. Rule statuses were "
                f"established for 2026-10-01; other dates re-check only recorded effective dates (sunsets and "
                f"pre-2026 history are not modelled). The submitted lookups.json is for 2026-10-01.")
    st.markdown(UNKNOWN_NOTE)
    shown = st.multiselect("Show", [k for k in STATUS_ORDER if counts[k]],
                           default=[k for k in STATUS_ORDER if counts[k]], format_func=RESULT_LABELS.get)
    if not results:
        st.write("No rule in the published set covers this address on this date.")
    for cat, label in CATEGORY_LABELS.items():
        group = [r for r in results if r["rule"]["category"] == cat and r["result"] in shown]
        if not group:
            continue
        group.sort(key=lambda r: STATUS_ORDER.index(r["result"]))
        st.markdown(f"##### {label} ({len(group)})")
        for r in group:
            rule = r["rule"]
            with st.expander(f"{BADGE[r['result']]} {md(rule['title'])} · {md(rule['jurisdiction'])}"):
                st.markdown(f"**Requirement.** {md(rule['requirement'])}")
                if rule.get("key_value"):
                    st.markdown(f"**Key value.** {md(rule['key_value'])}")
                st.markdown(f"**Why ({RESULT_LABELS[r['result']].lower()}).** {md(r['explanation'])}")
                if r["missing"]:
                    st.markdown("**Missing facts:** " + "; ".join(md(x) for x in r["missing"]))
                    with st.popover("Conditions the data does not settle"):
                        for c in r["causes"]:
                            st.markdown(f"- _{c['role']}_: {md(c['clause'])}")
                if rule.get("effective_date"):
                    st.markdown(f"**Effective date.** {rule['effective_date']}")
                if rule.get("conflict_flag"):
                    st.warning("Conflict flag: " + md(rule.get("conflict_note") or "possible conflict with "
                                                      "another level of law; human review."))
                st.markdown(f"**Citation.** {md(rule['citation'])} · source `{rule['source_doc_id']}`"
                            + (f" · [open source]({rule['source_url']})" if rule.get("source_url") else ""))
                st.caption("Quoted source text (verified verbatim against the supplied corpus text)")
                st.code(rule["quoted_span"], language=None, wrap_lines=True)
                st.caption(f"Rule ID `{r['team_rule_id']}` · record status `{rule['status']}`")

    st.subheader("Change scenarios for this address")
    cs = change_status(b, aid)
    st.dataframe([{"Test": t["test_id"], "Scenario": t["title"], "This address": cs[t["test_id"]]}
                  for t in b.change_tests], hide_index=True, width="stretch")

# ---------------------------------------------------------------- change scenarios
with tab_changes:
    st.subheader("D. Change scenarios (official tests T1–T5)")
    st.caption("From the submitted outputs/m6/changes.json. Deterministic: no LLM in the change engine. "
               "Addresses without a resolved jurisdiction are never placed in an affected set.")
    summary = b.change_summary["tests"]
    for t in b.change_tests:
        tid, e = t["test_id"], b.changes[t["test_id"]]
        with st.container(border=True):
            st.markdown(f"#### {tid} · {md(t['title'])}")
            a, c, d = st.columns([1, 1, 3])
            a.metric("Affected addresses", len(e["affected_address_ids"]))
            c.metric("Conflict flags", len(e["conflict_flag_address_ids"]))
            by_city = summary[tid]["affected_by_local_jurisdiction"]
            d.markdown("**Affected by jurisdiction:** " + (", ".join(f"{k} {v}" for k, v in by_city.items())
                                                           or "none"))
            if t["type"] == "as_of":
                d.markdown(f"**Before → after:** {t['as_of_before']}: not yet effective → "
                           f"{t['as_of_after']}: applies ({', '.join(t['states'])} addresses)")
            elif t["type"] == "boundary":
                d.markdown("**Boundary:** " + "; ".join(f"{o} only in {b.change_map[o]['jurisdiction']}"
                                                        for o in t["rule_ids"]) + "; never Newark.")
            elif t["type"] == "pending":
                d.markdown(f"**On {t['as_of']}:** pending bills, not law — reported as *pending*, never *applies*.")
            else:
                d.markdown(f"**On {t['as_of']}:** measure failed — nothing affected, no rent cap shown.")
            st.markdown(f"**Engine notes.** {md(e['notes'])}")
            for o in t["rule_ids"]:
                mp = b.change_map[o]
                st.markdown(f"- `{o}` = {md(mp['label'])} ({mp['jurisdiction']}). "
                            f"_{BASIS_TEXT.get(mp['basis'], mp['basis'])}_ Sources: {', '.join(mp['source_doc_ids'])}.")
                for rid in mp.get("team_rule_ids") or []:
                    rule = b.rules.get(rid)
                    if rule:
                        st.markdown(f"  - {md(rule['citation'])} · `{rid}` · status `{rule['status']}`")
            if e["conflict_flag_address_ids"]:
                st.warning(f"{len(e['conflict_flag_address_ids'])} addresses carry a conflict flag for human review "
                           f"(possible preemption of local ordinances).")
            with st.expander("Affected address IDs"):
                st.write(", ".join(e["affected_address_ids"]) or "none")

# ---------------------------------------------------------------- how it works
with tab_about:
    st.subheader("How LeaseLens works")
    st.markdown(
        "1. **Extraction (LLM):** Gemini reads each supplied legal text and returns structured rule records.\n"
        "2. **Verification:** every published quote is matched against the raw source text; records whose quote "
        "or date cannot be verified are held or rejected, never published.\n"
        "3. **Jurisdiction:** each address is resolved with the U.S. Census Geocoder (postal city is never "
        "trusted as the legal city); unclear matches are flagged for review, unresolved ones stay unresolved.\n"
        "4. **Applicability (deterministic Python):** each rule's coverage conditions and exemptions are tested "
        "against the property facts with true / false / unknown logic.\n"
        "5. **Change tests:** T1–T5 are run by a deterministic engine over the same records and dates.")
    st.markdown("**Known limitations**\n"
                "- Some explanatory-page rules are held, not published, because their operative dates could not "
                "be established safely (e.g. most New Jersey just-cause and deposit guidance).\n"
                "- Hoboken and Jersey City ordinance texts are link-only in the corpus; T2 uses organizer-supplied "
                "facts and Census geography, with no quote.\n"
                "- The NJ FAIR Act record stays rejected (its model quote inserted brackets not in the source); T3 "
                "uses the verified effective date and the supplied scenario facts.\n"
                "- Owner occupancy, subsidy status, owner type, certificate-of-occupancy dates and other facts are "
                "absent from the challenge data, so many results are honestly unknown.\n"
                "- No local rules are published for Cambridge, Hoboken, Jersey City or Newark.")
    st.caption(DISCLAIMER)
