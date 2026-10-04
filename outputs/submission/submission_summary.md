# LeaseLens — submission summary

| | |
|---|---|
| Project | LeaseLens (Rental Housing Law Navigator) |
| Team | Maverick |
| GitHub | `<GITHUB_URL>` (origin remote: https://github.com/Wirepullerrr/rental-housing-law-navigator) |
| Live demo | https://leaselens-maverick.streamlit.app/ |
| Query date | 2026-10-01 |

Hackathon prototype. Not legal advice.

## Files

These are byte-identical copies of the final pipeline outputs. `scripts/check_submission.py` verifies that.

| File | Copied from | Contents |
|---|---|---|
| `rules.json` | `outputs/m3/full_v2/rules.json` | 228 rule records in the template's `{"rules": [...]}` wrapper: 215 in force, 11 pending, 2 failed |
| `lookups.json` | `outputs/m5/lookups.json` | all 500 addresses, each once: 14,508 `applies`, 16,131 `unknown`, 662 `pending` |
| `changes.json` | `outputs/m6/changes.json` | T1–T5, each once, in order |

## Results

- **Extraction:** 54 supplied-text documents processed and 228 rules published. Every published quote matches its source (210 exactly, 18 after normalization). Another 183 records were held and 15 rejected.
- **Jurisdiction:** 473 of 500 addresses resolved, 20 flagged for review, 7 left unresolved. 33 addresses have a postal city that differs from their legal city.
- **Applicability:** a lookup row for every address. 135 results were left out because a property exemption clearly applies. Unresolved addresses get `unknown` for every rule.
- **Change tests:**

  | Test | Scenario | Affected | Conflict flags |
  |---|---|---|---|
  | T1 | CA AB 325 takes effect | 248 CA | 0 |
  | T2 | Hoboken vs Jersey City local bans | 90 (40 + 50, none in Newark) | 0 |
  | T3 | NJ FAIR Act enacted, not yet effective | 139 NJ | 90 (Hoboken + Jersey City) |
  | T4 | MA bills S.2983 and H.5222 | 105 MA, reported as pending | 0 |
  | T5 | MA rent-control ballot question struck | 0 | 0 |

- **Tests:** 380 passing, with network access blocked.

## Known limitations

1. Some rules from explanatory pages were held rather than published, because a current operative date couldn't be verified. The main one is New Jersey guidance D067.
2. Many results are `unknown` because the data has no owner occupancy, subsidy status, owner type or exact certificate-of-occupancy date.
3. The Hoboken and Jersey City ordinances behind T2 were link-only, so T2 uses the organizer-supplied scenario facts and Census geography, with no quote.
4. The NJ FAIR Act candidate was rejected because its quote added bracket characters not in the source. T3 uses the effective date verified from the bill text (2027-07-01) and the scenario facts.
5. Cambridge, Hoboken, Jersey City and Newark have no published local rules.

## How it works

Gemini reads each supplied legal text and returns structured rule records against a JSON schema. A record is published only if:
- its quote matches the raw source text;
- it passes schema validation;
- its status and effective date are backed by verified text.

Records that fail are held or rejected.

Addresses are resolved with the U.S. Census Geocoder rather than their postal city, with manual overrides recorded alongside their evidence. A deterministic Python engine then evaluates each rule's coverage conditions and exemptions as true, false or unknown. When a needed fact is missing, it reports `unknown` and names that fact.

The same records and jurisdictions drive the T1–T5 change engine, which uses no LLM. The Streamlit demo reads only the committed outputs.
