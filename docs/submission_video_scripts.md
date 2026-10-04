# LeaseLens — submission video scripts

Three scripts, each under 60 seconds when spoken at a natural pace (about 130–150 words). Stage directions are in *italics*.

---

## A. Team introduction (~45 s)

*Face to camera, or the LeaseLens title screen.*

Hi, I'm Zun, and I'm Rule of One: a one-person team. My project is LeaseLens, a rental housing law navigator.

The problem: rental rules depend on the state, the city, the building and the date. And the data we're given is messy. A Boston address might say "Dorchester." A San Diego address might say "San Ysidro." And the records usually don't say whether the owner lives there or whether the building is subsidized.

A tool that guesses confidently in those cases is dangerous. So I built LeaseLens around one principle: answer only what the evidence supports. Every answer cites the exact source text, and when the data can't settle a question, LeaseLens says "unknown" and names the missing fact.

It's an informational prototype, not legal advice.

---

## B. Product demo (~55 s)

*Screen recording of `uv run streamlit run app.py`. Demo address: **A0500, 8811 Burnet Ave, Los Angeles**.*

1. *Show the header and the disclaimer.* This is LeaseLens. The not-legal-advice notice is always on screen.
2. *Sidebar: Demo examples → A0500. Leave the date at 2026-10-01.* I'll pick a Los Angeles apartment building, built in 1954, with 36 units, as of October 1st, 2026.
3. *Point to B. Jurisdiction.* The Census Geocoder places it in the City of Los Angeles, so both California and Los Angeles rules are checked.
4. *Point to the metrics and the green check.* 43 rules apply and 44 are unknown. This matches the submitted lookups file exactly.
5. *Scroll to Security deposits and open a green "Applies" rule.* Each rule shows the requirement, why it applies, the citation, and the quoted source text, verified word for word.
6. *Scroll up to Rent increase limits and open the orange "Gross rental rate increase cap".* California's statewide rent cap is unknown here. Whether it covers this building depends on whether a local ordinance, like LA rent stabilization, covers it, and whether it's subsidized, and the data doesn't say. Unknown is an honest answer, not a failure.
7. *Click the "Change scenarios" tab and scroll to T1 and T3.* The five official change tests. T1: California's algorithmic-pricing law takes effect for 248 addresses. T3: New Jersey's FAIR Act becomes effective in July 2027, with conflict flags on Hoboken and Jersey City.

---

## C. Technical walkthrough (~58 s)

*Screen: the README Mermaid diagram, then the code folders.*

LeaseLens uses the LLM only where it's needed, and deterministic Python everywhere else.

**One. Extraction.** Gemini reads each of the 54 supplied legal texts and returns structured rule records under a strict JSON schema. A prompt and response cache makes runs reproducible.

**Two. Verification.** Every quote must match the raw source text, and effective dates must come from verified spans. That includes relative dates like "the first day of the twelfth month after enactment." 228 rules passed, with zero citation failures. Anything unverifiable is held or rejected, never published.

**Three. Jurisdiction.** The Census Geocoder resolves all 500 addresses to their legal city, not their postal city: 473 resolved, 20 flagged for review, 7 left unresolved rather than guessed.

**Four. Applicability.** A rule engine with three-valued true/false/unknown logic tests each rule's conditions and exemptions against the property facts.

**Five. Change tests.** The same engine runs T1 through T5 deterministically. 380 tests pass, with the network blocked.
