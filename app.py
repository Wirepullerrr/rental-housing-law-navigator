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

from navigator.demo import (CATEGORY_LABELS, DEFAULT_AS_OF, DEMO_EXAMPLES, MISSING_FACT_LABELS,  # noqa: E402
                            RESULT_LABELS, STATUS, STATUSES, address_label, change_status, glance_lines, load_bundle,
                            map_points, matches_submission, missing_fact_counts, review_signals, rule_results,
                            status_counts)

DISCLAIMER = "Informational prototype for hackathon purposes. Not legal advice."
HOW_TO_STEPS = ("Pick a property", "Choose the legal reference date", "Review results on the right")
AS_OF_HELP = ("**The legal reference date.** It isn't the year the building was built, and it isn't the date the "
              "data was collected.\n\nThe submitted answers are for 2026-10-01; other dates re-check each rule's "
              "recorded effective date.")
STATUS_COLOR = {"applies": "#21a366", "unknown": "#e08a00", "pending": "#3b82f6", "not_yet_effective": "#8b5cf6",
                "superseded": "#8a8f98"}
BADGE = {"applies": ":green-badge[Applies]", "unknown": ":orange-badge[Unknown]", "pending": ":blue-badge[Pending]",
         "not_yet_effective": ":violet-badge[Not yet effective]", "superseded": ":gray-badge[Superseded]"}
STATUS_GUIDE = "\n\n".join(f"**{s.label}**: {s.help}" for s in STATUSES)
CITY_COLORS = [[59, 130, 246], [16, 185, 129]]                  # blue, green (scenario maps)
CONFLICT_COLOR = [234, 88, 12]
DEFAULT_SCENARIO = "T2"                                         # tight Hoboken / Jersey City cluster on the map
CARD_H, MAP_H = 296, 182                                        # the three overview cards share one height
MATCH_TEXT = {"Exact": "Exact", "Non_Exact": "Close match (not exact)"}
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
[data-testid="stMainBlockContainer"] {padding-top:3.4rem; padding-bottom:4rem}
[data-testid="stMainBlockContainer"] h1 {padding:0 0 .15rem; font-size:2.3rem}
[data-testid="stAlertContainer"] {padding:.55rem .95rem}
.ll-tag {opacity:.8; margin:-.45rem 0 .1rem}
.ll-intro {border:1px solid rgba(128,128,128,.25);border-radius:10px;padding:.7rem 1rem;
  background:rgba(128,128,128,.06);margin-bottom:.75rem;font-size:.95rem;line-height:1.5}
.ll-intro .hd {font-weight:700;margin-bottom:.1rem}
.ll-intro .why {font-size:.85rem;opacity:.72;margin-top:.25rem}
.ll-num {display:inline-flex;align-items:center;justify-content:center;width:1.45rem;
  height:1.45rem;border-radius:50%;margin-right:.5rem;font-size:.8rem;border:1px solid rgba(128,128,128,.5)}
.ll-card-title {font-size:.76rem;font-weight:700;text-transform:uppercase;letter-spacing:.07em;opacity:.7;
  margin-bottom:.45rem}
.ll-place {font-size:1.3rem;font-weight:700;line-height:1.25}
.ll-place-sub {opacity:.72;font-size:.9rem;margin:.1rem 0 .6rem}
.ll-grid {display:grid;grid-template-columns:max-content 1fr;gap:.32rem 1rem;font-size:.93rem}
.ll-k {opacity:.68}
.ll-v {font-weight:500;overflow-wrap:anywhere}
.ll-muted {opacity:.6;font-style:italic;font-weight:400}
.ll-chip {display:inline-block;padding:.05rem .6rem;border-radius:999px;font-size:.78rem;font-weight:600;
  border:1px solid var(--c);color:var(--c);background:color-mix(in srgb, var(--c) 10%, transparent);
  margin:0 .3rem .45rem 0;white-space:nowrap}
.ll-chip.n {--c:currentColor;opacity:.8;font-weight:500}
.ll-flow {display:flex;align-items:baseline;flex-wrap:wrap;gap:.15rem .45rem;font-size:.92rem;line-height:1.4}
.ll-flow .k {font-size:.78rem;opacity:.68}
.ll-flow .arrow {opacity:.55}
.ll-mini {font-size:.8rem;opacity:.68;margin-top:.1rem}
.ll-noplace {display:flex;align-items:center;justify-content:center;border:1px dashed rgba(128,128,128,.45);
  border-radius:8px;opacity:.7;font-size:.9rem}
.ll-sechead {display:flex;align-items:baseline;flex-wrap:wrap;gap:.3rem 1rem;margin-top:.3rem}
.ll-h {font-size:1.2rem;font-weight:700}
.ll-stat {border:1px solid rgba(128,128,128,.25);border-left:6px solid var(--c);border-radius:10px;
  padding:.6rem .9rem;background:color-mix(in srgb, var(--c) 9%, transparent);height:7.4rem;box-sizing:border-box;overflow:hidden;margin-bottom:.85rem}
.ll-stat .n {font-size:2.2rem;font-weight:700;line-height:1.1;color:var(--c)}
.ll-stat .l {font-weight:650;font-size:.98rem}
.ll-stat .h {font-size:.8rem;opacity:.72;margin-top:.1rem}
.ll-stat.zero {border-left-color:rgba(128,128,128,.45);background:transparent}
.ll-stat.zero .n {color:inherit;opacity:.35}
.ll-stat.zero .l, .ll-stat.zero .h {opacity:.6}
.ll-sec {font-size:.74rem;font-weight:700;text-transform:uppercase;letter-spacing:.07em;opacity:.7;
  margin:.15rem 0 .3rem}
.ll-line {display:flex;align-items:baseline;gap:.5rem;margin:.2rem 0;font-size:.98rem}
.ll-note {font-size:.82rem;opacity:.68}
.ll-dot {display:inline-block;width:.7rem;height:.7rem;border-radius:50%;flex:none;margin-right:.1rem}
.ll-facts {margin:.1rem 0 .3rem;padding-left:1.1rem}
.ll-facts li {margin:.12rem 0}
.ll-facts li span {opacity:.68;font-size:.88rem}
.ll-attn {border-left:3px solid #d08700;padding:.15rem 0 .15rem .7rem;margin:.45rem 0;font-size:.92rem}
.ll-cat {display:flex;align-items:baseline;gap:.75rem;margin:1rem 0 .2rem;padding-bottom:.3rem;
  border-bottom:1px solid rgba(128,128,128,.25)}
.ll-cat .t {font-size:1.08rem;font-weight:700}
.ll-cat .c {font-size:.85rem;opacity:.7}
.ll-tile {border:1px solid rgba(128,128,128,.25);border-radius:10px;padding:.65rem .85rem;height:11rem;box-sizing:border-box;margin-bottom:.85rem}
.ll-tile .t {font-weight:700;font-size:1.05rem}
.ll-tile .s {font-size:.82rem;opacity:.75;min-height:2.6rem;margin:.15rem 0 .3rem}
.ll-tile .n {font-size:1.8rem;font-weight:700;line-height:1.1}
.ll-tile .h {font-size:.78rem;opacity:.72}
.ll-mini-stats {display:flex;gap:2rem;margin:.1rem 0 1rem}
.ll-mini-stats .n {font-size:1.9rem;font-weight:700;line-height:1.1}
.ll-mini-stats .l {font-size:.82rem;opacity:.7}
.ll-step {font-size:1.9rem;font-weight:800;opacity:.3;line-height:1}
.ll-side-note {font-size:.84rem;opacity:.75;line-height:1.5}
.ll-howto {font-size:.84rem;line-height:1.35;padding-bottom:.75rem;margin-bottom:1rem;
  border-bottom:1px solid rgba(128,128,128,.25)}
.ll-howto .hd {font-size:.72rem;font-weight:700;text-transform:uppercase;letter-spacing:.07em;opacity:.7;
  margin-bottom:.35rem}
.ll-howto .st {display:flex;align-items:flex-start;gap:.5rem;margin:.3rem 0;opacity:.85}
.ll-howto .ll-num {margin:0;flex:none;width:1.2rem;height:1.2rem;font-size:.7rem}
.ll-help {font-size:.8rem;opacity:.68;line-height:1.35;margin:-.6rem 0 1rem}
</style>
"""
_MD_SPECIAL = str.maketrans({c: "\\" + c for c in "\\`*_[]<>#|$~"})


def md(text: object) -> str:
    """Escape source text for st.markdown ('$' would otherwise start LaTeX)."""
    return str(text).translate(_MD_SPECIAL)


def esc(text: object) -> str:
    return html.escape(str(text))


def nice_date(d: date) -> str:
    return f"{d:%b} {d.day}, {d.year}"


def muted(text: str = "not in the data") -> str:
    return f'<span class="ll-muted">{esc(text)}</span>'


def chip(text: str, color: str | None = None, tip: str | None = None) -> str:
    style = f' style="--c:{color}"' if color else ""
    title = f' title="{esc(tip)}"' if tip else ""
    return f'<span class="ll-chip{"" if color else " n"}"{style}{title}>{esc(text)}</span>'


def units_html(facts) -> str:
    if facts.units_source == "conflict":
        return muted("conflicting values in the data")
    if not facts.units_known:
        return muted()
    n = (str(facts.units_min) if facts.units_min == facts.units_max
         else f"{facts.units_min}–{facts.units_max}" if facts.units_max else f"{facts.units_min}+")
    return esc(n) + (' <span class="ll-muted">(from the use description)</span>'
                     if facts.units_source == "use_description" else "")


def grid(pairs: list[tuple[str, str]]) -> str:
    return '<div class="ll-grid">' + "".join(f'<div class="ll-k">{esc(k)}</div><div class="ll-v">{v}</div>'
                                             for k, v in pairs) + "</div>"


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
        st.markdown(f'<div class="ll-noplace" style="height:{MAP_H - 34}px">No coordinates recorded</div>',
                    unsafe_allow_html=True)
        st.caption("Map unavailable for this address: the Census match recorded no coordinates.")
        return
    import pydeck as pdk
    others = [p for p in pts if p["address_id"] != address_id]
    draw_map([point_layer(others, [140, 140, 140, 150], 5), point_layer(sel, [230, 57, 70, 240], 11, outline=True)],
             pdk.ViewState(latitude=sel[0]["lat"], longitude=sel[0]["lon"], zoom=14),
             "<b>{address_id}</b><br/>{address}<br/>{jurisdiction}", height=MAP_H)


def scenario_map(test_id: str) -> None:
    e = b.changes[test_id]
    ids, flagged = e["affected_address_ids"], set(e["conflict_flag_address_ids"])
    if not ids:
        st.markdown('<div class="ll-noplace" style="height:300px">Nothing to map: no property is affected.</div>',
                    unsafe_allow_html=True)
        return
    by_id = {p["address_id"]: p for p in all_points()}
    pts = [{**by_id[a], "conflict_flag": "yes" if a in flagged else "no"} for a in ids if a in by_id]
    if not pts:
        st.caption("Map unavailable: none of the affected properties has coordinates.")
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
             + [point_layer([p for p in pts if p["conflict_flag"] == "yes"], CONFLICT_COLOR + [235], 7, outline=True)],
             view, "<b>{address_id}</b><br/>{address}<br/>{jurisdiction}<br/>Conflict flag: {conflict_flag}",
             height=410)
    legend = [(name, rgb) for name, g, rgb in groups if g]
    if flagged:
        legend.append(("Affected, conflict flag (review suggested)", CONFLICT_COLOR))
    st.markdown('<div class="ll-flow">' + "".join(
        f'<span><span class="ll-dot" style="display:inline-block;background:rgb({c[0]},{c[1]},{c[2]})"></span> '
        f'{esc(n)}</span>' for n, c in legend) + "</div>", unsafe_allow_html=True)
    if len(pts) < len(ids):
        st.caption(f"{len(ids) - len(pts)} affected properties have no recorded coordinates and aren't on the map.")


b = bundle()

# ---------------------------------------------------------------- header
st.title("LeaseLens")
st.markdown('<div class="ll-tag"><b>Rental Housing Law Navigator</b> · Auditable rental-law guidance by '
            'property, jurisdiction, and date.</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="ll-intro"><div class="hd">What LeaseLens does</div>'
    "Pick a property and date. LeaseLens finds the legal jurisdiction, shows the housing rules that may apply, "
    "and links every answer back to its source. Missing a required fact? It says <b>Unknown</b> instead of "
    "guessing.<div class=\"why\">Why it matters: the mailing city isn't always the legal city, and rules change "
    "with place, property facts and date.</div></div>", unsafe_allow_html=True)
st.warning(DISCLAIMER, icon="⚠️")

# ---------------------------------------------------------------- sidebar: control panel
with st.sidebar:
    st.markdown('<div class="ll-howto"><div class="hd">How to use</div>' + "".join(
        f'<div class="st"><span class="ll-num">{i}</span>{esc(s)}</div>' for i, s in enumerate(HOW_TO_STEPS, 1))
        + "</div>", unsafe_allow_html=True)
    source = st.radio("Property list", ["Demo examples", "All 500"], label_visibility="collapsed",
                      captions=["Curated properties that show the main LeaseLens features",
                                "Browse every challenge property"])
    options = list(DEMO_EXAMPLES) if source == "Demo examples" else sorted(b.rows)
    aid = st.selectbox("Property", options, format_func=lambda a: (
        f"{b.rows[a]['street_address']}, {b.rows[a]['postal_city']} · {DEMO_EXAMPLES[a]}"
        if source == "Demo examples" else address_label(b, a)))
    st.markdown('<div class="ll-help">Select the rental property you want to check.</div>', unsafe_allow_html=True)
    as_of = st.date_input("As-of date", value=DEFAULT_AS_OF, min_value=date(2026, 1, 1),
                          max_value=date(2027, 12, 31), format="YYYY-MM-DD", help=AS_OF_HELP)
    st.markdown('<div class="ll-help">Check the rules as they stood on this date.</div>', unsafe_allow_html=True)
    st.divider()
    st.markdown('<div class="ll-side-note"><b>500 challenge properties</b><br>Results run from saved, validated '
                'outputs.<br>No live legal or AI calls.</div>', unsafe_allow_html=True)

tab_lookup, tab_changes, tab_about = st.tabs(["Address lookup", "Change scenarios (T1–T5)", "How it works"])

# ---------------------------------------------------------------- address lookup
with tab_lookup:
    row, facts, res = b.rows[aid], b.facts[aid], b.resolutions[aid]
    status = res["resolution_status"]
    results = results_for(aid, as_of)
    counts = status_counts(results)            # the one count object for cards, chips and summary
    signals = review_signals(b, aid)
    when = nice_date(as_of)
    local = res["local_jurisdiction"]
    mailing = row["postal_city"].strip()
    differs = bool(local) and mailing.lower() != local.split(",")[0].lower()

    # ---- 1-2. What property is this, and which legal jurisdiction is it in?
    c1, c2, c3 = st.columns([1, 1, 1.2], gap="small")
    with c1, st.container(border=True, height=CARD_H):
        st.markdown('<div class="ll-card-title">Property</div>'
                    f'<div class="ll-place">{esc(row["street_address"])}</div>'
                    f'<div class="ll-place-sub">{esc(mailing)}, {esc(row["state"])} {esc(row["zip"])} · {esc(aid)}</div>'
                    + grid([("Year built", esc(facts.year_built) if facts.year_built is not None else muted()),
                            ("Units", units_html(facts)),
                            ("Use", esc(row["use_description"]) if row["use_description"] else muted()),
                            ("Data source", esc(row["source_dataset"]))]), unsafe_allow_html=True)
    with c2, st.container(border=True, height=CARD_H):
        review_tip = next((s["detail"] for s in signals if s["kind"] == "jurisdiction"), None)
        chips = {"resolved": chip("✓ Census resolved", "#21a366"),
                 "review_required": chip("Review suggested", "#c27c00", review_tip),
                 "unresolved": chip("Unresolved", "#8a8f98", review_tip)}[status]
        if res.get("override"):
            chips += chip("Manually reviewed", "#8b5cf6", res["override"].get("authoritative_reason", "")[:400])
        if status == "unresolved":
            body = (f'<div class="ll-place">{muted("Not resolved")}</div>'
                    '<div class="ll-place-sub">Census couldn\'t place this address, so LeaseLens doesn\'t guess a '
                    'city. Every rule stays Unknown.</div>')
        else:
            body = (f'<div class="ll-place">{esc(local or res["state_jurisdiction"])}</div>'
                    f'<div class="ll-place-sub">State law: {esc(res["state_jurisdiction"])}'
                    + (f' · {esc(res["county"]["name"])}' if res.get("county") else "") + "</div>"
                    + grid([("Census match", esc(MATCH_TEXT.get(res["match_type"], res["match_type"]))
                             if res["match_type"] else muted("manual review")),
                            ("Matched as", esc(res["matched_address"]) if res["matched_address"]
                             else muted("set by manual review"))]))
        st.markdown('<div class="ll-card-title">Legal jurisdiction</div>', unsafe_allow_html=True,
                    help="The city and state whose laws govern the property. LeaseLens finds it from Census "
                         "geography, not from the mailing address. Hover a chip for review details.")
        st.markdown(chips + body, unsafe_allow_html=True)
    with c3, st.container(border=True, height=CARD_H, gap="xsmall"):
        st.markdown('<div class="ll-card-title">Where it is</div>', unsafe_allow_html=True)
        property_map(aid)
        if differs:
            flow = (f'<span class="k">Mailing city</span><b>{esc(mailing)}</b><span class="arrow">→</span>'
                    f'<span class="k">Legal city</span><b>{esc(local)}</b>')
        else:
            flow = (f'<span class="k">Legal jurisdiction</span>'
                    f'<b>{esc(local or res["state_jurisdiction"] or "not resolved")}</b>')
        how = {"resolved": "Resolved with Census geography", "review_required": "Census match needs review",
               "unresolved": "No Census match"}[status]
        if res.get("override"):
            how = "Set by manual review of the Census evidence"
        st.markdown(f'<div class="ll-flow">{flow}</div><div class="ll-mini">{how} · red dot: this property</div>',
                    unsafe_allow_html=True)

    # ---- 3. What applies today?
    city_has_rules = bool(local) and any(r["jurisdiction"] == local for r in b.rules.values())
    context = []
    if res["state_jurisdiction"]:
        context.append(chip(f"State law · {res['state_jurisdiction']}"))
    if local:
        context.append(chip(f"City law · {local.split(',')[0]}") if city_has_rules
                       else chip(f"No published {local.split(',')[0]} city rules",
                                 tip="The supplied corpus gave no publishable local rules for this city, so only "
                                     "state rules are evaluated."))
    if status == "unresolved":
        context.append(chip("Jurisdiction unresolved"))
    st.markdown(f'<div class="ll-sechead"><div class="ll-h">What applies on {esc(when)}</div><div>'
                + "".join(context) + "</div></div>", unsafe_allow_html=True, help=STATUS_GUIDE)
    for col, s in zip(st.columns(len(STATUSES)), STATUSES):
        n = counts[s.key]
        col.markdown(f'<div class="ll-stat{"" if n else " zero"}" style="--c:{STATUS_COLOR[s.key]}" '
                     f'title="{esc(s.label)}: {esc(s.help)}"><div class="n">{n}</div><div class="l">{s.label}</div>'
                     f'<div class="h">{s.meaning}</div></div>', unsafe_allow_html=True)

    # ---- 4. What can't be decided, and is there anything to review?
    missing = missing_fact_counts(results)
    attention = bool(missing) or bool(signals)
    with st.container(border=True):
        left, right = st.columns([1, 1.15], gap="large") if attention else (st.container(), None)
        with left:
            lines = "".join(
                f'<div class="ll-line"><span class="ll-dot" style="background:{STATUS_COLOR[k]}"></span>'
                f'<span>{esc(text)}'
                + (' <span class="ll-note">Proposals, not current law.</span>' if k == "pending" else "")
                + "</span></div>" for k, text in glance_lines(counts))
            touched = [(t, s) for t, s in change_status(b, aid).items() if s == "affected"]
            titles = {t["test_id"]: t["title"] for t in b.change_tests}
            if touched:
                lines += ('<div class="ll-note" style="margin-top:.35rem">Also in change scenarios: '
                          + "; ".join(f"{t} ({esc(titles[t])})" for t, _ in touched) + "</div>")
            st.markdown('<div class="ll-sec">At a glance</div>'
                        + (lines or '<div class="ll-line">No published rule reaches this property on this '
                                    'date.</div>'), unsafe_allow_html=True)
            if as_of == DEFAULT_AS_OF:
                if matches_submission(b, aid, results):
                    st.caption(":green[✓ Same answers as our submitted lookups.json for this property "
                               "(as of 2026-10-01).]")
                else:
                    st.error("These results differ from the submitted lookups.json row.")
            else:
                st.caption(f"Showing {when}. Each rule's recorded effective date was re-checked for this day. "
                           f"The submitted answers are for Oct 1, 2026.")
        if right is not None:
            with right:
                parts = ['<div class="ll-sec">Needs attention</div>']
                if missing:
                    top, more = missing[:4], len(missing) - 4
                    parts.append('<div style="font-weight:600;font-size:.95rem">Missing information</div>'
                                 '<div class="ll-note">These facts aren\'t in the supplied property data, so the '
                                 'rules that depend on them stay Unknown.</div><ul class="ll-facts">'
                                 + "".join(f"<li>{esc(label)} <span>· affects {n} rule{'s' if n != 1 else ''}"
                                           f"</span></li>" for label, n in top) + "</ul>"
                                 + (f'<div class="ll-note">+ {more} more</div>' if more > 0 else ""))
                for s in signals:
                    parts.append(f'<div class="ll-attn">{chip("Review suggested", "#c27c00")}<b>{esc(s["title"])}'
                                 f'</b><br><span class="ll-note">{esc(s["detail"])}</span></div>')
                st.markdown("".join(parts), unsafe_allow_html=True)

    # ---- 5-6. Why, and what source supports it?
    st.markdown('<div class="ll-h" style="margin-top:.6rem">Rules and sources</div>', unsafe_allow_html=True)
    st.caption("Open a rule to see what it says, why LeaseLens classified it this way, and the source text.")
    if results:
        available = [s.key for s in STATUSES if counts[s.key]]
        shown = st.pills("Show", available, selection_mode="multi",
                         default=["applies"] if counts["applies"] else available,
                         format_func=lambda k: f"{RESULT_LABELS[k]} ({counts[k]})") or []
        hidden = sum(counts[k] for k in available if k not in shown)
        if hidden:
            st.caption(f"{hidden} more {'are' if hidden != 1 else 'is'} hidden: turn on the other statuses above "
                       f"to see them.")
        for cat, label in CATEGORY_LABELS.items():
            in_cat = [r for r in results if r["rule"]["category"] == cat]
            group = sorted((r for r in in_cat if r["result"] in shown), key=lambda r: list(STATUS).index(r["result"]))
            if not group:
                continue
            tally = Counter(r["result"] for r in in_cat)
            st.markdown(f'<div class="ll-cat"><span class="t">{esc(label)}</span><span class="c">'
                        + " · ".join(f"{RESULT_LABELS[k]} {tally[k]}" for k in STATUS if tally[k])
                        + "</span></div>", unsafe_allow_html=True)
            for r in group:
                rule, rid = r["rule"], r["team_rule_id"]
                key = rule.get("key_value") or ""
                head = f"{BADGE[r['result']]} **{md(rule['title'])}** :gray[· {md(rule['jurisdiction'])}]"
                if key and len(key) <= 40:
                    head += f" :gray[· {md(key)}]"
                with st.expander(head):
                    st.markdown('<div class="ll-sec">What the rule says</div>\n\n' + md(rule["requirement"])
                                + (f"\n\n**Key figure:** {md(key)}" if key else ""), unsafe_allow_html=True)
                    st.markdown('<div class="ll-sec">Why LeaseLens classified it this way</div>\n\n'
                                + md(r["explanation"])
                                + (f"\n\nEffective date: {rule['effective_date']}" if rule.get("effective_date")
                                   else ""), unsafe_allow_html=True)
                    if r["reasons"]:
                        st.markdown('<div class="ll-sec">Missing facts</div>\n\n'
                                    + "\n".join(f"- {md(MISSING_FACT_LABELS.get(x, m))}"
                                                for x, m in zip(r["reasons"], r["missing"])),
                                    unsafe_allow_html=True)
                        with st.popover("See the exact conditions"):
                            for c in r["causes"]:
                                st.markdown(f"- _{c['role']}_: {md(c['clause'])}")
                    if rule.get("conflict_flag"):
                        st.info("Review suggested: " + md(rule.get("conflict_note") or "possible conflict with "
                                                          "another level of law."))
                    with st.container(horizontal=True, vertical_alignment="center", gap="medium"):
                        st.markdown(f'<div class="ll-sec">Source</div>\n\n**{md(rule["citation"])}** · document '
                                    f'{rule["source_doc_id"]}', unsafe_allow_html=True)
                        if rule.get("source_url"):
                            st.link_button("View source", rule["source_url"], icon=":material/open_in_new:",
                                           key=f"src-{rid}")
                    st.markdown('<div class="ll-sec">Quoted source text</div>', unsafe_allow_html=True)
                    st.code(rule["quoted_span"], language=None, wrap_lines=True)
                    st.caption(f"Audit · quote checked against the supplied source text · evaluated as of {when} · "
                               f"rule {rid} · record status: {rule['status'].replace('_', ' ')}")

# ---------------------------------------------------------------- change scenarios
with tab_changes:
    st.markdown('<div class="ll-h">Change scenarios</div>', unsafe_allow_html=True)
    st.caption("The five official what-if tests. Pick one to see what changed, where it matters, and how many "
               "properties it touches. Results come from the submitted changes.json; no LLM is involved.")
    tests = {t["test_id"]: t for t in b.change_tests}
    for col, (tid, t) in zip(st.columns(len(tests)), tests.items()):
        e = b.changes[tid]
        n, c = len(e["affected_address_ids"]), len(e["conflict_flag_address_ids"])
        col.markdown(f'<div class="ll-tile"><div class="t">{tid}</div><div class="s">{esc(t["title"])}</div>'
                     f'<div class="n">{n}</div><div class="h">properties affected</div>'
                     + (chip(f"Review suggested · {c}", "#c27c00") if c else "") + "</div>", unsafe_allow_html=True)

    tid = st.segmented_control("Scenario to explore", list(tests), default=DEFAULT_SCENARIO, required=True,
                               key="scenario") or DEFAULT_SCENARIO
    t, e = tests[tid], b.changes[tid]
    affected, flagged = e["affected_address_ids"], e["conflict_flag_address_ids"]
    labels = " and ".join(b.change_map[o]["label"] for o in t["rule_ids"])
    states = ", ".join(t.get("states", []))
    by_city = b.change_summary["tests"][tid]["affected_by_local_jurisdiction"]
    if t["type"] == "as_of":
        baseline = f"On **{t['as_of_before']}**, {md(labels)} is enacted but **not yet effective**."
        changed = f"On **{t['as_of_after']}**, it **applies** to **{len(affected)}** {states} properties."
    elif t["type"] == "boundary":
        baseline = (f"On **{t['as_of']}**, two local rules exist side by side: "
                    + " and ".join(md(b.change_map[o]["label"]) for o in t["rule_ids"]) + ".")
        changed = ("**The city boundary decides which local rule applies.** Census geography puts each property in "
                   "exactly one city, so each ban covers only its own city's properties, and none elsewhere.")
    elif t["type"] == "pending":
        baseline = f"On **{t['as_of']}**, {md(labels)} are bills, not law."
        changed = (f"Reported as **Pending** for **{len(affected)}** {states} properties, and never as Applies. "
                   f"They would be covered if the bills pass.")
    else:
        baseline = f"{md(labels)} was proposed for {states}."
        changed = "The measure failed. **No property is affected**, and no rent cap is shown."

    with st.container(border=True):
        st.markdown(f'<div class="ll-h">{tid} · {esc(t["title"])}</div>', unsafe_allow_html=True)
        left, right = st.columns([1, 1.45], gap="large")
        with left:
            st.markdown('<div class="ll-sec">What changed</div>\n\n'
                        f"**Before:** {baseline}\n\n**With the change:** {changed}", unsafe_allow_html=True)
            st.markdown('<div class="ll-sec">How many</div><div class="ll-mini-stats">'
                        f'<div><div class="n">{len(affected)}</div><div class="l">properties affected</div></div>'
                        f'<div><div class="n">{len(flagged)}</div><div class="l">conflict flags</div></div></div>',
                        unsafe_allow_html=True)
            st.markdown('<div class="ll-sec">Where it matters</div>\n\n'
                        + (" · ".join(f"{md(k)} **{v}**" for k, v in by_city.items()) or "Nowhere"),
                        unsafe_allow_html=True)
            if flagged:
                st.markdown('<div class="ll-sec">Review</div>'
                            + chip("Review suggested", "#c27c00")
                            + f"<div class='ll-note'>{len(flagged)} properties carry a conflict flag: the state law "
                              f"may preempt the local ordinances there. LeaseLens flags this for human review; it "
                              f"doesn't resolve the conflict.</div>", unsafe_allow_html=True)
        with right:
            scenario_map(tid)
        with st.expander("How this was computed, and the sources"):
            st.markdown(f"**What the organizers expect:** {md(t['expected_behavior'])}")
            st.markdown(md(e["notes"]))
            for o in t["rule_ids"]:
                mp = b.change_map[o]
                st.markdown(f"- **{md(mp['label'])}** ({mp['jurisdiction']}). {BASIS_TEXT.get(mp['basis'], mp['basis'])} "
                            f"Source documents: {', '.join(mp['source_doc_ids'])}.")
                for rid in mp.get("team_rule_ids") or []:
                    rule = b.rules.get(rid)
                    if rule:
                        st.markdown(f"  - {md(rule['citation'])} · rule {rid} · {rule['status'].replace('_', ' ')}")
        with st.expander(f"Affected property IDs ({len(affected)})"):
            st.write(", ".join(affected) or "None")

# ---------------------------------------------------------------- how it works
with tab_about:
    st.markdown('<div class="ll-h">How LeaseLens works</div>', unsafe_allow_html=True)
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
        with col, st.container(border=True, height=200):
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
