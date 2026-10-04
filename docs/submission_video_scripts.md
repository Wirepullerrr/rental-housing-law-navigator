# LeaseLens — submission video scripts

Three scripts, each under 60 seconds at a normal speaking pace (about 125–130 words each). Stage directions are in *italics*.

---

## A. Team introduction (~50 s)

*Face to camera, or the LeaseLens title screen.*

Hi, I'm Zun from team Maverick, and this is LeaseLens, a rental housing law navigator.

Here's the problem. Rental rules depend on the state, the city, the building, and the date. The data you get is messy, too. A Boston address might say "Dorchester." A San Diego address might say "San Ysidro." And the records usually don't tell you whether the owner lives there or whether the building is subsidized.

If a tool just guesses in those cases, it can give someone the wrong answer about their rights. So I built LeaseLens to answer only what the evidence supports. Every answer quotes the source text. When the data can't settle a question, it says "unknown" and tells you which fact is missing.

It's a prototype, not legal advice.

---

## B. Product demo (~50 s)

*Screen recording of the live app, https://leaselens-maverick.streamlit.app/. Don't change anything: it opens on **A0134, 101 Norfolk St**, as of 2026-10-01.*

1. *Show the title and the not-legal-advice notice.* This is LeaseLens, opened on 101 Norfolk Street.
2. *Point to "Dorchester" under the address, then to the line under the map: "Mailing city Dorchester → Legal city Boston, MA".* The mailing address says Dorchester, but Census puts it in Boston. That's why we don't trust the postal city.
3. *Point to the map.* And here it is on the map.
4. *Point to the status cards.* 34 rules apply, four of them Boston's own. Three are pending bills, so they're not shown as law.
5. *Under Just-cause eviction, open "Fourteen Days' Notice to Quit for Nonpayment".* Each rule shows what it requires, the citation, and the source quote.
6. *Point to the Unknown card, which shows 0.* No unknowns here. But when the data's missing a fact, like whether the owner lives there, LeaseLens says Unknown instead of guessing.
7. *Click the "Change scenarios" tab. T2 is already selected; point to the map.* T2 is Hoboken versus Jersey City. Each city's ban stays inside its own border: blue for Hoboken, green for Jersey City.
8. *Back to the camera, or stay on the map.* None of this is generated live. The LLM read the law ahead of time; plain Python decides what applies.

---

## C. Technical walkthrough (~50 s)

*Screen: the README diagram, then the code folders.*

The LLM reads the law; Python decides what applies.

First, extraction. Gemini reads the 54 public legal texts from the challenge and returns structured rules in a strict schema. Responses are cached, so reruns match.

Next, every quote has to match the source, and effective dates have to come from text I can point to. 228 rules passed. Anything unverified was held back.

Then jurisdiction. The Census Geocoder places all 500 addresses by legal city, not postal city: 473 resolved, 20 flagged for review, and 7 left unresolved instead of guessed.

Last, the rule engine checks each condition as true, false, or unknown. The same rules drive the T1 to T5 change scenarios, with no LLM after extraction, and the test suite runs offline.
