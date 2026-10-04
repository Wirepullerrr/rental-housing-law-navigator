# LeaseLens — submission summary

| | |
|---|---|
| Project | **LeaseLens** (Rental Housing Law Navigator) |
| Team | **Rule of One** (solo) |
| GitHub | `<GITHUB_URL>` (origin remote: https://github.com/Wirepullerrr/rental-housing-law-navigator) |
| Live demo | `<DEMO_URL>` |
| Query date | 2026-10-01 |

> Informational prototype for hackathon purposes. **Not legal advice.**

## Files

The three JSON files below are byte-identical copies of the authoritative outputs, checked by `scripts/check_submission.py`.

| File | Source | Content |
|---|---|---|
| `rules.json` | `outputs/m3/full_v2/rules.json` | 228 rule records (`{"rules": [...]}`, as in the template); 215 in force, 11 pending, 2 failed |
| `lookups.json` | `outputs/m5/lookups.json` | all 500 addresses exactly once; 14,508 applies · 16,131 unknown · 662 pending |
| `changes.json` | `outputs/m6/changes.json` | T1–T5 exactly once, in order |

## Final counts

- **Extraction (M3).** 54 supplied-text documents handled. 228 publishable rules, with 0 schema failures, 0 duplicate IDs and 0 citation failures among published records (210 exact + 18 normalized quote matches). 183 records held and 15 rejected.
- **Jurisdiction (M4).** 500 addresses: 473 resolved, 20 flagged for review, 7 unresolved. 33 postal-city ≠ legal-city cases; 8 reviewed overrides.
- **Applicability (M5).** 500 lookup rows. 135 exempt results omitted; unresolved addresses are reported as unknown, never guessed.
- **Change tests (M6).**

  | Test | Scenario | Affected | Conflict flags |
  |---|---|---|---|
  | T1 | CA AB 325 takes effect | 248 CA | 0 |
  | T2 | Hoboken vs Jersey City local bans | 90 (40 + 50; no Newark) | 0 |
  | T3 | NJ FAIR Act: enacted, not yet effective | 139 NJ | 90 (Hoboken + Jersey City) |
  | T4 | MA bills S.2983 / H.5222 | 105 MA, reported pending | 0 |
  | T5 | MA rent-control ballot question struck | 0 | 0 |

- **Tests.** 380 passing, with network access blocked for the whole suite.

## Known limitations

1. **Some rules from explanatory pages are held, not published,** because their current operative dates could not be established safely (notably NJ guidance D067).
2. **T2 has no citable ordinance text.** The Hoboken and Jersey City ordinance texts are link-only in the supplied corpus, so T2 uses organizer-supplied test facts and Census geography. No legal quote is fabricated.
3. **The NJ FAIR Act record stays rejected,** because its model quote inserted bracket characters not in the source. T3 uses the effective date verified from the source text (2027-07-01) and the organizer-supplied scenario facts.
4. **Many results are `unknown`,** because owner occupancy, subsidy status, owner type, certificate-of-occupancy dates and other facts are absent from the challenge data.
5. **No local rules are published for Cambridge, Hoboken, Jersey City or Newark.**

## Technical description

LeaseLens separates what an LLM is good at from what must be exact.

**Extraction.** Gemini reads each supplied legal text and returns structured rule records under a JSON schema. A content-addressed prompt/provider cache makes every run reproducible.

**Verification before publication.** A record is published only if:
- its quoted span matches the raw source text;
- its schema validates;
- its legislative status and effective date are supported by verified text spans, including relative dates such as "the first day of the twelfth month next following enactment".

Anything that fails is held or rejected, not guessed.

**Jurisdiction.** Each address is resolved with the U.S. Census Geocoder rather than its postal city. Reviewed overrides are recorded with their evidence, and unresolved addresses stay unresolved.

**Applicability.** A deterministic Python engine:
- classifies each rule's coverage conditions and exemptions into property facts;
- evaluates them with three-valued true/false/unknown logic;
- returns applies, unknown (naming the missing fact), pending or not yet effective.

**Change tests.** A deterministic change engine runs T1–T5 over the same records, jurisdictions and dates.

**Demo.** A Streamlit app reads only the committed outputs, with no network access, and shows every answer with its reason, citation and verbatim source quote.
