"""LeaseLens: Streamlit demo of the Rental Housing Law Navigator.

    uv run streamlit run app.py

Offline. Reads the frozen M3-M6 outputs through navigator.demo; no Gemini, Census or database
calls. Maps use the coordinates M4 already recorded; the basemap tiles load in the browser.
Informational prototype for hackathon purposes. Not legal advice.
"""

from __future__ import annotations

import html
import sys
from collections import Counter
from datetime import date
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from navigator.demo import (CATEGORY_LABELS, DEFAULT_AS_OF, DEMO_EXAMPLES, RESULT_LABELS, address_label,  # noqa: E402
                            change_status, load_bundle, map_points, matches_submission, rule_results)

DISCLAIMER = "Informational prototype for hackathon purposes. Not legal advice."
STATUS_ORDER = ["applies", "unknown", "pending", "not_yet_effective", "superseded"]
STATUS_COLOR = {"applies": "#21a366", "unknown": "#e08a00", "pending": "#3b82f6", "not_yet_effective": "#8b5cf6",
                "superseded": "#8a8f98"}
STATUS_HELP = {"applies": "In force and covers this property", "unknown": "Depends on a fact the data doesn't have",
               "pending": "A bill or proposal, not law yet", "not_yet_effective": "Enacted, starts after this date",
               "superseded": "A stricter rule governs instead"}
BADGE = {"applies": ":green-badge[Applies]", "unknown": ":orange-badge[Unknown]", "pending": ":blue-badge[Pending]",
         "not_yet_effective": ":violet-badge[Not yet effective]", "superseded": ":gray-badge[Superseded]"}
SUMMARY_VERB = {"applies": ("applies", "apply"), "unknown": ("can't be decided from the data",) * 2,
                "pending": ("is pending", "are pending"), "not_yet_effective": ("isn't in force yet", "aren't in force yet"),
                "superseded": ("is superseded", "are superseded")}          # (singular, plural)
CITY_COLORS = [[59, 130, 246], [16, 185, 129]]                  # blue, green (scenario maps)
DEFAULT_SCENARIO = "T2"                                         # tight Hoboken / Jersey City cluster on the map
CHANGE_LABEL = {"affected": "Affected", "affected + conflict flag": "Affected, with a conflict flag",
                "not affected": "Not affected"}
FRIENDLY_REASON = {"jurisdiction_unresolved": "which city the property is in (Census couldn't place it)"}
BASIS_TEXT = {
    "verified_held_records": "Based on rule text quoted from the supplied source. The scenario supplies the "
                             "effective date.",
    "published_records": "Based on published rule records with quotes from the source.",
    "supplied_change_fact_plus_verified_effective_date": "Based on the organizers' scenario facts, with the "
                                                         "effective date checked against the bill text.",
    "supplied_change_fact_only": "Based on the organizers' scenario facts only. The source was link-only in the "
                                 "supplied corpus, so there is no text to quote.",
}
CSS = """
<style>
[data-testid="stMainBlockContainer"] {padding-top:2.5rem}
.ll-addr {font-size:1.5rem;font-weight:700;line-height:1.25;margin-top:.2rem}
.ll-sub {opacity:.7;margin-bottom:.6rem}
.ll-card-title {font-size:.78rem;font-weight:700;text-transform:uppercase;letter-spacing:.07em;opacity:.6;
  margin-bottom:.5rem}
.ll-grid {display:grid;grid-template-columns:max-content 1fr;gap:.35rem 1rem;font-size:.95rem}
.ll-k {opacity:.65}
.ll-v {font-weight:500;overflow-wrap:anywhere}
.ll-muted {opacity:.55;font-style:italic;font-weight:400}
.ll-stat {border:1px solid rgba(128,128,128,.25);border-left:6px solid var(--c);border-radius:10px;
  padding:.7rem .9rem;background:color-mix(in srgb, var(--c) 9%, transparent);margin-bottom:.5rem}
.ll-stat .n {font-size:2.1rem;font-weight:700;line-height:1.1;color:var(--c)}
.ll-stat .l {font-weight:600}
.ll-stat .h {font-size:.78rem;opacity:.7;margin-top:.1rem}
.ll-stat.zero {opacity:.45}
.ll-chip {display:inline-block;padding:.05rem .6rem;border-radius:999px;font-size:.8rem;font-weight:600;
  border:1px solid var(--c);color:var(--c);background:color-mix(in srgb, var(--c) 10%, transparent);
  margin:0 .3rem .5rem 0}
.ll-tile {border:1px solid rgba(128,128,128,.25);border-radius:10px;padding:.7rem .85rem;min-height:9.5rem;
  margin-bottom:.5rem}
.ll-tile .t {font-weight:700;font-size:1.05rem}
.ll-tile .s {font-size:.82rem;opacity:.75;min-height:2.6rem;margin:.15rem 0 .3rem}
.ll-tile .n {font-size:1.8rem;font-weight:700;line-height:1.1}
.ll-tile .h {font-size:.78rem;opacity:.7}
.ll-step {font-size:1.9rem;font-weight:800;opacity:.3;line-height:1}
.ll-dot {display:inline-block;width:.75rem;height:.75rem;border-radius:50%;margin:0 .35rem 0 .1rem;
  vertical-align:-.05rem}
.ll-intro {display:flex;flex-wrap:wrap;gap:.6rem 2rem;align-items:center;border:1px solid rgba(128,128,128,.25);
  border-radius:10px;padding:.8rem 1rem;margin:.2rem 0 .9rem;background:rgba(128,128,128,.06)}
.ll-intro .txt {flex:3 1 26rem;font-size:.95rem;line-height:1.5}
.ll-intro .why {font-size:.85rem;opacity:.7;margin-top:.3rem}
.ll-intro .steps {flex:1 1 12rem;display:flex;flex-direction:column;gap:.3rem;font-size:.9rem;font-weight:600}
.ll-intro .steps span {display:inline-flex;align-items:center;justify-content:center;width:1.45rem;height:1.45rem;
  border-radius:50%;margin-right:.5rem;font-size:.8rem;border:1px solid rgba(128,128,128,.45)}
</style>
"""
_MD_SPECIAL = str.maketrans({c: "\\" + c for c in "\\`*_[]<>#|$~"})


def md(text: object) -> str:
    """Escape source text for st.markdown ('$' would otherwise start LaTeX)."""
    return str(text).translate(_MD_SPECIAL)


def esc(text: object) -> str:
    return html.escape(str(text))


def missing_html(text: str = "not in the data") -> str:
    return f'<span class="ll-muted">{esc(text)}</span>'


def units_html(facts) -> str:
    if facts.units_source == "conflict":
        return missing_html("conflicting values in the data")
    if not facts.units_known:
        return missing_html()
    n = (str(facts.units_min) if facts.units_min == facts.units_max
         else f"{facts.units_min}–{facts.units_max}" if facts.units_max else f"{facts.units_min}+")
    return esc(n) + (' <span class="ll-muted">(from the use description)</span>'
                     if facts.units_source == "use_description" else "")


def card(title: str, pairs: list[tuple[str, str]], chips: str = "") -> None:
    rows = "".join(f'<div class="ll-k">{esc(k)}</div><div class="ll-v">{v}</div>' for k, v in pairs)
    st.markdown(f'<div class="ll-card-title">{esc(title)}</div>{chips}<div class="ll-grid">{rows}</div>',
                unsafe_allow_html=True)


def chip(text: str, color: str) -> str:
    return f'<span class="ll-chip" style="--c:{color}">{esc(text)}</span>'


def friendly(reason: str) -> str:
    text = FRIENDLY_REASON.get(reason, reason)
    return text[:1].upper() + text[1:]


st.set_page_config(page_title="LeaseLens", page_icon="⚖️", layout="wide")
st.markdown(CSS, unsafe_allow_html=True)


@st.cache_resource(show_spinner="Loading saved results…")
def bundle():
    return load_bundle()


@st.cache_data(show_spinner=False)
def results_for(address_id: str, as_of: date):
    return rule_results(bundle(), address_id, as_of)


@st.cache_data(show_spinner=False)
def all_points():
    return [{**p, "address": esc(p["address"]), "jurisdiction": esc(p["jurisdiction"])} for p in map_points(bundle())]


def draw_map(layers, view, tooltip: str, height: int) -> None:
    """One PyDeck chart. Any failure leaves a note instead of breaking the page."""
    try:
        import pydeck as pdk
        deck = pdk.Deck(layers=[pdk.Layer("ScatterplotLayer", **spec) for spec in layers], initial_view_state=view,
                        map_style=None, tooltip={"html": tooltip})        # None: Streamlit's theme-aware basemap
        st.pydeck_chart(deck, height=height)
    except Exception:  # noqa: BLE001 - the map is decoration; the page must still render
        st.caption("Map unavailable right now.")


def point_layer(data, color, pixels, outline=False) -> dict:
    """Points about `pixels` px across at any zoom (radius in metres, clamped in pixels; pydeck would turn a
    string radius_units into an expression)."""
    spec = {"data": data, "get_position": "[lon, lat]", "get_fill_color": color, "get_radius": 50,
            "radius_min_pixels": pixels, "radius_max_pixels": pixels, "pickable": True}
    if outline:
        spec |= {"stroked": True, "get_line_color": [255, 255, 255, 255], "line_width_min_pixels": 2}
    return spec


def property_map(address_id: str) -> None:
    pts = all_points()
    sel = [p for p in pts if p["address_id"] == address_id]
    if not sel:
        st.caption("Map unavailable for this address: the Census match recorded no coordinates.")
        return
    import pydeck as pdk
    others = [p for p in pts if p["address_id"] != address_id]
    draw_map([point_layer(others, [140, 140, 140, 150], 5), point_layer(sel, [230, 57, 70, 240], 11, outline=True)],
             pdk.ViewState(latitude=sel[0]["lat"], longitude=sel[0]["lon"], zoom=14),
             "<b>{address_id}</b><br/>{address}<br/>{jurisdiction}", height=260)
    st.caption("Red: this property. Grey dots: other challenge properties.")


def scenario_map(test_id: str) -> None:
    e = b.changes[test_id]
    ids, flagged = e["affected_address_ids"], set(e["conflict_flag_address_ids"])
    if not ids:
        st.caption("Nothing to map: no address is affected in this scenario.")
        return
    by_id = {p["address_id"]: p for p in all_points()}
    pts = [{**by_id[a], "conflict_flag": "yes" if a in flagged else "no"} for a in ids if a in by_id]
    if not pts:
        st.caption("Map unavailable: none of the affected addresses has coordinates.")
        return
    import pydeck as pdk
    view = pdk.data_utils.compute_view([[p["lon"], p["lat"]] for p in pts], view_proportion=0.95)
    plain = [p for p in pts if p["conflict_flag"] == "no"]
    test = next(x for x in b.change_tests if x["test_id"] == test_id)
    if test["type"] == "boundary":                           # one colour per city, so the boundary is visible
        cities = [b.change_map[o]["jurisdiction"] for o in test["rule_ids"]]
        groups = [(c, [p for p in plain if p["jurisdiction"] == esc(c)], CITY_COLORS[i % len(CITY_COLORS)])
                  for i, c in enumerate(cities)]
    else:
        groups = [("Affected", plain, CITY_COLORS[0])]
    draw_map([point_layer(g, rgb + [210], 6) for _, g, rgb in groups]
             + [point_layer([p for p in pts if p["conflict_flag"] == "yes"], [234, 88, 12, 235], 7, outline=True)],
             view, "<b>{address_id}</b><br/>{address}<br/>{jurisdiction}<br/>Conflict flag: {conflict_flag}",
             height=360)
    legend = " &nbsp; ".join(f'<span class="ll-dot" style="background:rgb({rgb[0]},{rgb[1]},{rgb[2]})"></span>'
                             f'{esc(name)}' for name, g, rgb in groups if g)
    if flagged:
        legend += ' &nbsp; <span class="ll-dot" style="background:#ea580c"></span>Affected, with a conflict flag'
    st.markdown(legend, unsafe_allow_html=True)
    if len(pts) < len(ids):
        st.caption(f"{len(ids) - len(pts)} affected addresses have no recorded coordinates and are not shown.")


b = bundle()

# ---------------------------------------------------------------- header
st.title("LeaseLens")
st.markdown("**Rental Housing Law Navigator** · Auditable rental-law guidance by property, jurisdiction, and date.")
st.markdown(
    '<div class="ll-intro"><div class="txt"><b>What LeaseLens does.</b> Pick one of the supplied rental properties '
    "and an as-of date in the sidebar. LeaseLens resolves the property's legal jurisdiction, checks which state and "
    "city housing rules may apply, and shows the source behind each answer. If the data is missing "
    "something important, it says <b>Unknown</b> instead of guessing."
    "<div class=\"why\">Why it matters: the mailing city isn't always the legal city, and housing rules change with "
    "place, property facts, and date.</div></div>"
    '<div class="steps"><div><span>1</span>Pick a property</div><div><span>2</span>See what applies</div>'
    "<div><span>3</span>Check the source</div></div></div>", unsafe_allow_html=True)
st.warning(DISCLAIMER, icon="⚠️")

# ---------------------------------------------------------------- sidebar inputs
with st.sidebar:
    st.header("Look up a property")
    source = st.radio("Choose from", ["Demo examples", "All 500 challenge addresses"])
    options = list(DEMO_EXAMPLES) if source == "Demo examples" else sorted(b.rows)
    aid = st.selectbox("Address", options, format_func=lambda a: (
        f"{b.rows[a]['street_address']}, {b.rows[a]['postal_city']} — {DEMO_EXAMPLES[a]}"
        if source == "Demo examples" else address_label(b, a)))
    as_of = st.date_input("As-of date", value=DEFAULT_AS_OF, min_value=date(2026, 1, 1),
                          max_value=date(2027, 12, 31), format="YYYY-MM-DD",
                          help="The submitted answers are for 2026-10-01.")
    st.caption("LeaseLens covers the 500 sample addresses supplied with the challenge. Everything runs from "
               "saved results, with no live API calls.")
    st.divider()
    st.caption(DISCLAIMER)

tab_lookup, tab_changes, tab_about = st.tabs(["Address lookup", "Change scenarios (T1–T5)", "How it works"])

# ---------------------------------------------------------------- address lookup
with tab_lookup:
    row, facts, res = b.rows[aid], b.facts[aid], b.resolutions[aid]
    status = res["resolution_status"]
    st.markdown(f'<div class="ll-addr">{esc(row["street_address"])}</div>'
                f'<div class="ll-sub">{esc(row["postal_city"])}, {esc(row["state"])} {esc(row["zip"])} · '
                f'{esc(aid)}</div>', unsafe_allow_html=True)

    c1, c2, c3 = st.columns([1, 1, 1.25], gap="medium")
    with c1, st.container(border=True):
        card("Property", [
            ("Year built", esc(facts.year_built) if facts.year_built is not None else missing_html()),
            ("Units", units_html(facts)),
            ("Use", esc(row["use_description"]) if row["use_description"] else missing_html()),
            ("Data source", esc(row["source_dataset"])),
        ])
    with c2, st.container(border=True):
        chips = {"resolved": chip("Resolved by Census", "#21a366"),
                 "review_required": chip("Census match needs review", "#e08a00"),
                 "unresolved": chip("Unresolved", "#8a8f98")}[status]
        if res.get("override"):
            chips += chip("Manually reviewed", "#8b5cf6")
        if status == "unresolved":
            card("Jurisdiction", [("State", missing_html("unknown")), ("City", missing_html("unknown"))], chips)
            st.caption("Census couldn't place this address, so LeaseLens doesn't guess a city. Every rule is "
                       "reported as Unknown.")
        else:
            match = (f"{esc(res['matched_address'])} ({esc(res['match_type'])})" if res["matched_address"]
                     else missing_html("set by manual review"))
            card("Jurisdiction", [
                ("State", esc(res["state_jurisdiction"])),
                ("City", f"<b>{esc(res['local_jurisdiction'])}</b>"),
                ("Census match", match),
                ("County", esc(res["county"]["name"]) if res.get("county") else missing_html("not recorded")),
            ], chips)
            if res["local_jurisdiction"] and \
                    row["postal_city"].strip().lower() != res["local_jurisdiction"].split(",")[0].lower():
                st.info(f"The postal city is **{md(row['postal_city'])}**, but the property is legally in "
                        f"**{res['local_jurisdiction']}**.")
            if status == "review_required":
                st.warning("The Census match needs review (" + md("; ".join(res["review_reasons"])) + "). "
                           "Results use this jurisdiction.")
            if res.get("override"):
                reason = res["override"].get("authoritative_reason", "")
                st.caption("Review note: " + md(reason[:300]) + ("…" if len(reason) > 300 else ""))
            for w in res.get("warnings") or []:
                st.caption("Geography note: " + md(w))
    with c3, st.container(border=True):
        st.markdown('<div class="ll-card-title">Where it is</div>', unsafe_allow_html=True)
        property_map(aid)

    # ---- status summary
    results = results_for(aid, as_of)
    counts = {k: sum(r["result"] == k for r in results) for k in STATUS_ORDER}
    for col, k in zip(st.columns(5), STATUS_ORDER):
        col.markdown(f'<div class="ll-stat{" zero" if not counts[k] else ""}" style="--c:{STATUS_COLOR[k]}">'
                     f'<div class="n">{counts[k]}</div><div class="l">{RESULT_LABELS[k]}</div>'
                     f'<div class="h">{STATUS_HELP[k]}</div></div>', unsafe_allow_html=True)

    if as_of != DEFAULT_AS_OF:
        st.info(f"Showing {as_of.isoformat()}. LeaseLens re-checked each rule's recorded effective date for this "
                f"day. Rule statuses were set for 2026-10-01, so sunsets and earlier history aren't modelled. "
                f"The submitted answers are for 2026-10-01.")

    left, right = st.columns([2, 1], gap="medium")
    with left, st.container(border=True):
        st.markdown("#### Quick summary")
        if not results:
            st.markdown(f"No published rule covers this property on {as_of.isoformat()}.")
        else:
            parts = [f"**{counts[k]}** {SUMMARY_VERB[k][counts[k] != 1]}" for k in STATUS_ORDER if counts[k]]
            st.markdown(f"Of the **{len(results)}** rules that could cover this property on **{as_of.isoformat()}**: "
                        + ", ".join(parts) + ".")
            reasons = Counter(m for r in results if r["result"] == "unknown" for m in r["missing"])
            if reasons:
                st.markdown("Most unknowns depend on facts the sample data doesn't include:\n"
                            + "\n".join(f"- {md(friendly(x))} · {n} rule{'s' if n != 1 else ''}"
                                        for x, n in reasons.most_common(3)))
        cs = change_status(b, aid)
        touched = [f"{t} ({'conflict flag' if s.endswith('flag') else 'affected'})" for t, s in cs.items()
                   if s != "not affected"]
        if touched:
            st.markdown("Change scenarios that touch this property: " + ", ".join(touched) + ".")
        if as_of == DEFAULT_AS_OF:
            if matches_submission(b, aid, results):
                st.caption(":green[✓ Same answers as our submitted lookups.json for this property "
                           "(as of 2026-10-01).]")
            else:
                st.error("These results differ from the submitted lookups.json row.")
    with right:
        st.info("**What “Unknown” means**\n\nThe property data is missing a fact the rule depends on, such as "
                "whether the owner lives there. LeaseLens says Unknown instead of guessing.")

    # ---- rules
    st.markdown("### Rules for this property")
    if results:
        available = [k for k in STATUS_ORDER if counts[k]]
        # Confirmed results first; the other statuses are one click away.
        shown = st.pills("Show", available, selection_mode="multi",
                         default=["applies"] if counts["applies"] else available,
                         format_func=lambda k: f"{RESULT_LABELS[k]} ({counts[k]})") or []
        hidden = sum(counts[k] for k in available if k not in shown)
        st.caption("Click a rule to see why, what's missing, and the quoted source text."
                   + (f" {hidden} more are hidden: turn on the other statuses above to see them." if hidden else ""))
        for cat, label in CATEGORY_LABELS.items():
            group = [r for r in results if r["rule"]["category"] == cat and r["result"] in shown]
            if not group:
                continue
            group.sort(key=lambda r: STATUS_ORDER.index(r["result"]))
            tally = Counter(r["result"] for r in group)
            st.markdown(f"#### {label}")
            st.caption(" · ".join(f"{tally[k]} {RESULT_LABELS[k].lower()}" for k in STATUS_ORDER if tally[k]))
            for r in group:
                rule = r["rule"]
                with st.expander(f"{BADGE[r['result']]} **{md(rule['title'])}** · {md(rule['jurisdiction'])}"):
                    st.markdown(md(rule["requirement"]))
                    if rule.get("key_value"):
                        st.markdown(f"**Key figure:** {md(rule['key_value'])}")
                    st.markdown(f"**Why it's {RESULT_LABELS[r['result']].lower()}:** {md(r['explanation'])}")
                    if r["missing"]:
                        st.markdown("**Missing from the property data:**\n"
                                    + "\n".join(f"- {md(friendly(x))}" for x in r["missing"]))
                        with st.popover("See the exact conditions"):
                            for c in r["causes"]:
                                st.markdown(f"- _{c['role']}_: {md(c['clause'])}")
                    if rule.get("effective_date"):
                        st.markdown(f"**Effective:** {rule['effective_date']}")
                    if rule.get("conflict_flag"):
                        st.warning("Conflict flag: " + md(rule.get("conflict_note") or "possible conflict with "
                                                          "another level of law; needs human review."))
                    st.markdown(f"**Source:** {md(rule['citation'])} · document {rule['source_doc_id']}"
                                + (f" · [open the source ↗]({rule['source_url']})" if rule.get("source_url") else ""))
                    st.caption("Quoted from the source (checked against the supplied text)")
                    st.code(rule["quoted_span"], language=None, wrap_lines=True)
                    st.caption(f"Rule {r['team_rule_id']} · record status: {rule['status'].replace('_', ' ')}")

    st.markdown("### Change scenarios for this property")
    st.dataframe([{"Test": t["test_id"], "Scenario": t["title"], "This property": CHANGE_LABEL[cs[t["test_id"]]]}
                  for t in b.change_tests], hide_index=True, width="stretch")

# ---------------------------------------------------------------- change scenarios
with tab_changes:
    st.markdown("### Change scenarios (T1–T5)")
    st.markdown("The five official what-if tests from the challenge. They check what happens when a law takes "
                "effect, when a city boundary matters, when a bill is still pending, and when a measure fails. "
                "Results come from the submitted changes.json. No LLM is involved.")
    tests = {t["test_id"]: t for t in b.change_tests}
    for col, (tid, t) in zip(st.columns(len(tests)), tests.items()):
        e = b.changes[tid]
        n, c = len(e["affected_address_ids"]), len(e["conflict_flag_address_ids"])
        col.markdown(f'<div class="ll-tile"><div class="t">{tid}</div><div class="s">{esc(t["title"])}</div>'
                     f'<div class="n">{n}</div><div class="h">affected{f" · {c} flagged" if c else ""}</div></div>', unsafe_allow_html=True)

    tid = st.segmented_control("Scenario to explore", list(tests), default=DEFAULT_SCENARIO, required=True,
                               key="scenario") or DEFAULT_SCENARIO
    t, e = tests[tid], b.changes[tid]
    affected, flagged = e["affected_address_ids"], e["conflict_flag_address_ids"]
    labels = " and ".join(b.change_map[o]["label"] for o in t["rule_ids"])
    states = ", ".join(t.get("states", []))
    local_of = {a: b.resolutions[a]["local_jurisdiction"] for a in affected}
    if t["type"] == "as_of":
        baseline = f"On **{t['as_of_before']}**, {md(labels)} is enacted but **not yet effective**."
        changed = f"On **{t['as_of_after']}**, it **applies** to **{len(affected)}** {states} addresses."
        if flagged:
            changed += f" **{len(flagged)}** of them are flagged for a possible conflict with local rules."
    elif t["type"] == "boundary":
        baseline = (f"On **{t['as_of']}**, two local rules exist side by side: "
                    + " and ".join(f"{md(b.change_map[o]['label'])}" for o in t["rule_ids"]) + ".")
        per_city = [(b.change_map[o]["jurisdiction"], sum(v == b.change_map[o]["jurisdiction"]
                                                          for v in local_of.values())) for o in t["rule_ids"]]
        changed = ("Each applies only inside its own city: "
                   + ", ".join(f"**{n}** in {j}" for j, n in per_city) + ". No address elsewhere is affected.")
    elif t["type"] == "pending":
        baseline = f"On **{t['as_of']}**, {md(labels)} are bills, not law."
        changed = (f"Reported as **Pending** for **{len(affected)}** {states} addresses, and never as Applies. "
                   f"These addresses would be covered if the bills pass.")
    else:
        baseline = f"{md(labels)} was proposed for {states}."
        changed = "The measure failed. **No address is affected**, and no rent cap is shown."

    with st.container(border=True):
        st.markdown(f"#### {tid} · {md(t['title'])}")
        st.caption(f"What the organizers expect: {md(t['expected_behavior'])}")
        l, r = st.columns(2, gap="medium")
        with l, st.container(border=True):
            st.markdown("**Baseline**")
            st.markdown(baseline)
        with r, st.container(border=True):
            st.markdown("**With the change**")
            st.markdown(changed)
        m1, m2, m3 = st.columns([1, 1, 2])
        m1.metric("Affected addresses", len(affected))
        m2.metric("Conflict flags", len(flagged))
        by_city = b.change_summary["tests"][tid]["affected_by_local_jurisdiction"]
        m3.markdown("**Where**\n\n" + (" · ".join(f"{k} **{v}**" for k, v in by_city.items()) or "Nowhere"))
        if flagged:
            st.warning(f"{len(flagged)} addresses carry a conflict flag for human review: the state law may "
                       f"preempt the local ordinances there.")
        scenario_map(tid)
        with st.expander("How this was computed, and the sources"):
            st.markdown(md(e["notes"]))
            for o in t["rule_ids"]:
                mp = b.change_map[o]
                st.markdown(f"- **{md(mp['label'])}** ({mp['jurisdiction']}). {BASIS_TEXT.get(mp['basis'], mp['basis'])} "
                            f"Source documents: {', '.join(mp['source_doc_ids'])}.")
                for rid in mp.get("team_rule_ids") or []:
                    rule = b.rules.get(rid)
                    if rule:
                        st.markdown(f"  - {md(rule['citation'])} · rule {rid} · {rule['status'].replace('_', ' ')}")
        with st.expander(f"Affected address IDs ({len(affected)})"):
            st.write(", ".join(affected) or "None")

# ---------------------------------------------------------------- how it works
with tab_about:
    st.markdown("### How LeaseLens works")
    st.markdown("An LLM reads the law. Plain Python decides what applies. When the data can't decide, the answer "
                "is Unknown.")
    steps = [("Read the law", "Gemini reads each supplied legal text and turns it into structured rule records."),
             ("Check every quote", "Each rule's quote is matched against the source text. Rules that can't be "
                                   "verified are held back, not published."),
             ("Find the real city", "The U.S. Census Geocoder places each address. The postal city isn't trusted, "
                                    "and unclear matches are flagged."),
             ("Decide, don't guess", "Python tests each rule's conditions as true, false or unknown, and names any "
                                     "missing fact.")]
    for i, (col, (title, text)) in enumerate(zip(st.columns(4, gap="medium"), steps), start=1):
        with col, st.container(border=True):
            st.markdown(f'<div class="ll-step">{i}</div>', unsafe_allow_html=True)
            st.markdown(f"**{title}**")
            st.markdown(text)
    st.markdown("The T1–T5 change scenarios run on the same rules and jurisdictions, with no LLM involved.")
    statuses = Counter(r["resolution_status"] for r in b.resolutions.values())
    for col, (label, value) in zip(st.columns(4), [("Published rules", len(b.rules)), ("Sample addresses", len(b.rows)),
                                                   ("Jurisdictions resolved", statuses["resolved"]),
                                                   ("Change scenarios", len(b.change_tests))]):
        col.metric(label, value)
    with st.expander("Known limitations"):
        st.markdown(
            "- Some rules from explanatory pages were held back because a current effective date couldn't be "
            "verified, notably New Jersey's just-cause and deposit guidance.\n"
            "- The Hoboken and Jersey City ordinances were link-only in the corpus, so T2 uses the organizers' "
            "scenario facts and Census geography, with no quote.\n"
            "- The NJ FAIR Act record was rejected because its quote didn't match the source exactly. T3 uses the "
            "effective date verified from the bill text and the scenario facts.\n"
            "- The sample data has no owner occupancy, subsidy status, owner type or exact certificate-of-occupancy "
            "date, so many answers are Unknown.\n"
            "- Cambridge, Hoboken, Jersey City and Newark have no published local rules.")
    st.caption(DISCLAIMER)
