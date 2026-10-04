# LeaseLens — submission video scripts

Three scripts, each under 60 seconds at a normal speaking pace (about 130–150 words). Stage directions are in *italics*.

---

## A. Team introduction (~45 s)

*Face to camera, or the LeaseLens title screen.*

Hi, I'm Zun from team Maverick, and this is LeaseLens, a rental housing law navigator.

Here's the problem. Rental rules depend on the state, the city, the building, and the date. The data you get is messy, too. A Boston address might say "Dorchester." A San Diego address might say "San Ysidro." And the records usually don't tell you whether the owner lives there or whether the building is subsidized.

If a tool just guesses in those cases, it can give someone the wrong answer about their rights. So I built LeaseLens to answer only what the evidence supports. Every answer quotes the source text. When the data can't settle a question, it says "unknown" and tells you which fact is missing.

It's a prototype, not legal advice.

---

## B. Product demo (~55 s)

*Screen recording of `uv run streamlit run app.py`. Demo address: **A0500, 8811 Burnet Ave, Los Angeles**.*

1. *Show the header and the disclaimer.* This is LeaseLens. The not-legal-advice notice stays on screen the whole time.
2. *Sidebar: Demo examples → A0500. Leave the date at 2026-10-01.* I'll pick an apartment building in LA. It was built in 1954, it has 36 units, and I'm checking it as of October 1st, 2026.
3. *Point to B. Jurisdiction.* The Census Geocoder puts it in the City of Los Angeles. So it gets California rules and LA rules.
4. *Point to the metrics and the green check.* 43 rules apply and 44 are unknown. The green check means this matches the file we submitted.
5. *Scroll to Security deposits and open a green "Applies" rule.* Each rule shows what it requires, why it applies, the citation, and the exact quote from the source.
6. *Scroll up to Rent increase limits and open the orange "Gross rental rate increase cap".* California's rent cap comes back unknown here. It depends on whether a local ordinance like LA's rent stabilization covers the building, and whether it's subsidized. The data doesn't say. So, unknown. That's an honest answer, not a failure.
7. *Click the "Change scenarios" tab and scroll to T1 and T3.* These are the five official change tests. In T1, California's pricing-algorithm law kicks in for 248 addresses. In T3, New Jersey's FAIR Act takes effect in July 2027, and Hoboken and Jersey City get flagged for a possible conflict.

---

## C. Technical walkthrough (~58 s)

*Screen: the README Mermaid diagram, then the code folders.*

The main idea: the LLM only does extraction. Everything after that is plain Python.

First, extraction. Gemini reads each of the 54 legal texts we were given and returns structured rules that fit a strict schema. Responses are cached, so reruns give the same result.

Second, checking. Every quote has to match the source text, and effective dates have to come from text I can point to, even relative ones like "the first day of the twelfth month after enactment." 228 rules passed. Anything I couldn't verify was held or rejected, not published.

Third, jurisdiction. The Census Geocoder places all 500 addresses by legal city, not postal city. 473 resolved, 20 got flagged for review, and 7 stayed unresolved instead of being guessed.

Fourth, the rule engine. It checks each rule's conditions as true, false, or unknown.

Last, the same engine runs T1 through T5. 380 tests pass, all offline.
