"""Pydantic v2 models: the internal typed contract for extraction.

Two layers, on purpose:
  ExtractedRule  - what the LLM is asked to produce. Semantic fields only; no
                   ids, no source metadata, no query-date-dependent status.
  RuleRecord     - the internal mirror of schema/rule_record.schema.json. Every
                   accepted record must ALSO pass that official JSON Schema.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Category = Literal[
    "rent_increase_limits",
    "just_cause_eviction",
    "security_deposits",
    "application_screening_fees",
    "screening_restrictions",
    "algorithmic_rent_setting",
]
Level = Literal["state", "city"]
Status = Literal["in_force", "not_yet_effective", "pending", "failed"]
EnactmentStatus = Literal["enacted", "pending", "failed"]
# layout_normalized_match (M3.3): equal only after also removing whitespace next to ( ) [ ] (citation.py).
CitationStatus = Literal["exact_match", "normalized_match", "layout_normalized_match", "failed"]
ScopeDecision = Literal["in_scope", "out_of_scope", "uncertain"]
# What a piece of effective-date evidence is. Only an explicit operative date, or a
# relative formula resolved deterministically (temporal.py), may populate effective_date.
EvidenceKind = Literal["explicit_operative_date", "relative_date_formula", "history_note", "operative_condition"]
# What kind of legal source the document is (internal audit metadata; posture.py decides
# whether the declared posture is established and what it implies for status).
Posture = Literal["codified_current_law", "enacted_session_law", "pending_bill", "failed_bill",
                  "bill_status_or_summary_page", "official_explanatory_page", "unknown"]
# What a record's substantive content rests on. Anything but operative_text is labelled in the record.
SourceBasis = Literal["operative_text", "official_bill_summary", "official_bill_history", "official_explanatory_text"]
# How a document-level condition says what it governs (scope.py decides which applies):
# a structural container ("this Division"), explicit references ("Sections 4 and 5"), or a
# named legal mechanism ("the rent cap"), which never propagates without verification.
ScopeMode = Literal["structural", "explicit_reference", "named_subject"]
MappingDecision = Literal["applies", "does_not_apply", "uncertain"]
# The legal function of an inventory provision (input to the scope-challenge check, coverage.py).
Role = Literal["operative_rule", "scope_condition", "exemption", "definition", "remedy", "enforcement",
               "history", "procedure", "boilerplate", "uncertain"]

# Same pattern as the official schema's effective_date.
PartialDate = Annotated[str, StringConstraints(pattern=r"^\d{4}(-\d{2}(-\d{2})?)?$")]


# ------------------------------------------------------------ generation layer

class QuotePart(BaseModel):
    """Verbatim text from ONE numbered segment of the document view."""

    model_config = ConfigDict(extra="forbid", strict=True)

    segment_id: int = Field(ge=1, description="The n of the [[SEGMENT n]] the text is copied from.")
    quoted_text: str = Field(min_length=1, description="Verbatim text copied from that segment only.")


class OperativeCondition(BaseModel):
    """A non-calendar trigger on which a rule's applicability depends (e.g. an agency
    action or a system becoming available). Not an effective date, not an exemption."""

    model_config = ConfigDict(extra="forbid", strict=True)

    statement: str = Field(min_length=1, description="The trigger in plain language.")
    evidence: str = Field(min_length=1, description="Verbatim text stating the trigger.")


class ScopeCarveOut(BaseModel):
    """The source explicitly excludes this rule from a document-level scope condition."""

    model_config = ConfigDict(extra="forbid", strict=True)

    scope_id: str = Field(min_length=1, description="id of the global_scope entry this rule is not subject to.")
    evidence: str = Field(min_length=1, description="Verbatim text showing the exclusion.")


class ScopeCondition(BaseModel):
    """A document- or division-level exemption or coverage condition, extracted once
    and propagated deterministically, by provision id, to every rule it governs."""

    model_config = ConfigDict(extra="forbid", strict=True)

    id: str = Field(min_length=1, description="Short id, e.g. 'S1'.")
    kind: Literal["exemption", "coverage_condition"]
    statement: str = Field(min_length=1, description="The condition in plain language.")
    citation: str = Field(min_length=1, description="Official citation of the provision stating it.")
    scope_mode: ScopeMode = Field(description="structural: it names a container ('this section', 'this "
                                  "Division'); explicit_reference: it cites provisions; named_subject: it names a "
                                  "legal mechanism ('the rent cap').")
    scope_quote: str = Field(min_length=1, description="The exact words that say what it governs, verbatim "
                             "(e.g. 'This Division shall not apply', 'Sections 4 and 5', 'The rent cap').")
    source_provision_id: str | None = Field(description="Inventory id of the provision that states it; null if "
                                            "that text is not an inventory provision.")
    governed_provision_ids: list[str] | None = Field(description="Inventory ids of the provisions it governs; "
                                                     "null ONLY if it governs every provision of the document.")
    evidence_parts: list[QuotePart] = Field(min_length=1, description="Verbatim text stating the condition: "
                                            "one part, or two parts across one [[PAGE BREAK]].")


class DocumentPosture(BaseModel):
    """What kind of legal source the document is, with the text showing it."""

    model_config = ConfigDict(extra="forbid", strict=True)

    posture: Posture
    evidence: str | None = Field(description="One verbatim passage showing the posture (e.g. an enactment, "
                                 "chapter, status or code heading line); null only for unknown.")


class NoRulesJustification(BaseModel):
    """Why a document yields no rule record, grounded in its text."""

    model_config = ConfigDict(extra="forbid", strict=True)

    reason: str = Field(min_length=1, description="One sentence: why the document has no in-scope legal content.")
    evidence: str = Field(min_length=1, description="One verbatim passage that shows it.")


class ExtractedRule(BaseModel):
    """One rule as returned by the model, before trusted metadata is added.
    Field order is generation order: the quote comes before the claims it supports."""

    model_config = ConfigDict(extra="forbid", strict=True)

    category: Category
    citation: str = Field(min_length=1, description="Official citation as identified in the document.")
    provision_ids: list[str] = Field(min_length=1, description="Inventory ids of the provision(s) this record "
                                     "comes from.")
    source_basis: SourceBasis = Field(description="What the record's substance rests on: operative legal text, "
                                      "or an official summary, history or explanation of it.")
    quote_parts: list[QuotePart] = Field(min_length=1, description="Verbatim support: one part, or two parts "
                                         "across one [[PAGE BREAK]] (end of segment n, start of segment n+1).")
    title: str = Field(min_length=1, description="Short name of the law or provision.")
    requirement: str = Field(min_length=1, description="One or two plain-language sentences stating the rule.")
    key_value: str | None = Field(description="Headline number or formula, if the quote states one.")
    coverage_conditions: str | None = Field(description="Coverage specific to this rule only (not document-wide).")
    exemptions: str | None = Field(description="Exemptions specific to this rule only (not document-wide).")
    scope_carve_outs: list[ScopeCarveOut]
    operative_conditions: list[OperativeCondition]
    interaction: str | None = Field(description="Stated relationship with another legal regime; not a mere citation.")
    enactment_status: EnactmentStatus = Field(description="enacted law, pending bill/proposal, or failed proposal.")
    enactment_status_evidence: str | None = Field(description="Verbatim text showing that status (required for "
                                                  "pending and failed).")
    version_note: str | None = Field(description="History or amendment notes and version history; never an effective date.")
    version_evidence: str | None = Field(description="Verbatim text of the history note or version annotation.")
    effective_date_evidence: str | None = Field(description="Verbatim text stating when this obligation takes effect.")
    effective_date_evidence_kind: EvidenceKind | None = Field(description="What effective_date_evidence is; "
                                                              "null when there is no evidence.")
    effective_date: PartialDate | None = Field(description="Only for an explicit_operative_date: the calendar date "
                                               "as written (YYYY-MM-DD, YYYY-MM or YYYY).")
    conflict_note: str | None = Field(description="Only a genuine simultaneous conflict or ambiguity; never version history.")


class ProvisionNote(BaseModel):
    """One entry of the model's provision inventory (audit, coverage closure and scope
    targeting; never published). Rules link to it by `id`."""

    model_config = ConfigDict(extra="forbid", strict=True)

    id: str = Field(min_length=1, description="Stable id in document order: 'P1', 'P2', ...")
    ref: str = Field(min_length=1, description="Provision citation as shown in the document, e.g. '§ 12.34(b)'.")
    anchor: str = Field(min_length=1, description="The first words of the provision, verbatim (one segment).")
    summary: str = Field(description="Short neutral description of the provision.")
    role: Role = Field(description="The provision's legal function.")
    scope: ScopeDecision
    category: Category | None = Field(description="Official category if in_scope, else null.")
    reason: str | None = Field(description="Why it is out_of_scope or uncertain; null if in_scope.")


class ExtractionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    document: DocumentPosture
    provisions: list[ProvisionNote]
    global_scope: list[ScopeCondition]
    rules: list[ExtractedRule]
    no_rules_justification: NoRulesJustification | None = Field(description="Only when `rules` is empty: why the "
                                                                "document has no in-scope legal content.")


# Replay of a cached pre-v6 response (replay.py) only. These relax exactly the fields an
# older prompt never asked for; the adapter records each fill in the run's legacy_replay audit.
class LegacyProvisionNote(ProvisionNote):
    anchor: str | None = None
    role: Role | None = None


class LegacyScopeCondition(ScopeCondition):
    scope_mode: ScopeMode | None = None
    scope_quote: str | None = None


class LegacyExtractedRule(ExtractedRule):
    provision_ids: list[str] = Field(default_factory=list)
    source_basis: SourceBasis | None = None
    enactment_status_evidence: str | None = None


class TargetResolution(BaseModel):
    """The repair pass's decision on one repair target. A target need not become a rule."""

    model_config = ConfigDict(extra="forbid", strict=True)

    ref: str = Field(min_length=1, description="The target ref exactly as listed in the request.")
    scope: ScopeDecision
    reason: str | None = Field(description="One short sentence; required for out_of_scope and uncertain.")
    evidence_parts: list[QuotePart] = Field(description="Verbatim text of the target provision itself "
                                            "(required for out_of_scope); same quote-part rules.")


class ScopeMappingDecision(BaseModel):
    """The repair pass's decision on one (named-subject condition, provision) pair. Never a rule."""

    model_config = ConfigDict(extra="forbid", strict=True)

    condition_id: str = Field(min_length=1, description="The condition id exactly as listed.")
    provision_id: str = Field(min_length=1, description="The provision id exactly as listed.")
    decision: MappingDecision
    reason: str | None = Field(description="One short sentence (required).")
    evidence_parts: list[QuotePart] = Field(description="Verbatim text supporting the decision (required for "
                                            "does_not_apply; same quote-part rules).")


class RepairResponse(BaseModel):
    """Targeted repair pass: a decision for every target and every scope mapping, and records
    only for in-scope targets."""

    model_config = ConfigDict(extra="forbid", strict=True)

    target_resolutions: list[TargetResolution]
    scope_mappings: list[ScopeMappingDecision]
    rules: list[ExtractedRule]


# Keywords the provider may rely on. Everything else (pattern, minLength,
# additionalProperties, titles) is enforced locally, not trusted to the API.
_GENERATION_SCHEMA_KEYS = {"type", "properties", "required", "items", "enum", "anyOf", "description",
                           "minimum", "maximum"}


def generation_json_schema(model: type[BaseModel] = ExtractionResponse) -> dict[str, Any]:
    """A response model as a self-contained JSON Schema for constrained decoding:
    $refs inlined and keywords reduced to a conservative, widely supported subset."""
    full = model.model_json_schema()
    defs = full.get("$defs", {})

    def simplify(node: Any) -> Any:
        if isinstance(node, list):
            return [simplify(x) for x in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            return simplify(defs[node["$ref"].rsplit("/", 1)[-1]])
        out = {}
        for key, value in node.items():
            if key == "properties":
                out[key] = {name: simplify(sub) for name, sub in value.items()}
            elif key in _GENERATION_SCHEMA_KEYS:
                out[key] = simplify(value)
        return out

    return simplify(full)


# --------------------------------------------------------------- record layer

class RuleRecord(BaseModel):
    """Internal mirror of schema/rule_record.schema.json (field order matches the sample)."""

    model_config = ConfigDict(extra="forbid", strict=True)

    team_rule_id: str
    jurisdiction: str
    level: Level
    category: Category
    status: Status
    title: str
    requirement: str
    key_value: str | None = None
    coverage_conditions: str | dict[str, Any] | None = None
    exemptions: str | None = None
    overrides: list[str] = Field(default_factory=list)
    interaction: str | None = None
    effective_date: PartialDate | None = None
    citation: str
    source_doc_id: str | None
    source_url: str
    quoted_span: str = Field(min_length=20)
    confidence: float | None = Field(default=None, ge=0, le=1)
    conflict_flag: bool = False
    conflict_note: str | None = None


# ---------------------------------------------------------------- audit layer

class SourceMeta(BaseModel):
    """Trusted provenance, taken from the corpus manifest and text header."""

    doc_id: str
    jurisdiction: str
    url: str
    source_type: str
    retrieved_at: str
    text_file: str
    content_sha256: str  # sha256 of the UTF-8 body (text after the header)
    body_chars: int


class CitationCheck(BaseModel):
    status: CitationStatus
    normalization: str | None = None   # what was normalized, for normalized_match
    start: int | None = None           # offsets into the source body
    end: int | None = None
    occurrences: int = 0
    model_span: str                    # exactly as the model returned it (parts joined by the marker)
    source_span: str | None = None     # exact source text at [start:end]
    reason: str | None = None
    reconstructed: bool = False        # raw span rebuilt from two quote parts across one page artifact
    parts: list[dict[str, Any]] = Field(default_factory=list)              # each quote part and its own match
    crossed_artifacts: list[dict[str, Any]] = Field(default_factory=list)  # page artifacts inside source_span


class CandidateResult(BaseModel):
    index: int
    origin: Literal["primary", "repair"] = "primary"
    accepted: bool = False
    # Passed every check except temporal resolution: preserved with its evidence, never published.
    held: bool = False
    # Passed every check, but its verified validity window ended on or before as_of (validity.py):
    # kept in the audit as a historical rule (temporal_state expired / repealed), never published.
    historical: bool = False
    temporal_state: str | None = None   # in_force | not_yet_effective | pending | failed | expired | repealed | held
    validity: dict[str, Any] = Field(default_factory=dict)       # validity.py window audit
    # Suppressed as an exact same-source duplicate of this validated candidate (finalize).
    duplicate_of: int | None = None
    rejection_reasons: list[str] = Field(default_factory=list)
    provision_ids: list[str] = Field(default_factory=list)       # links to inventory ids, as given
    source_basis: str | None = None
    status_evidence: CitationCheck | None = None                  # enactment_status_evidence check
    legislative: dict[str, Any] = Field(default_factory=dict)     # legislative.py status decision
    scope_inputs: dict[str, Any] = Field(default_factory=dict)    # rule-specific scope text, to recompose
    pydantic_valid: bool = False
    pydantic_errors: list[str] = Field(default_factory=list)
    schema_valid: bool | None = None   # None: not reached
    schema_errors: list[str] = Field(default_factory=list)
    citation: CitationCheck | None = None
    effective_date_evidence: CitationCheck | None = None
    version_evidence: CitationCheck | None = None
    operative_conditions: list[dict[str, Any]] = Field(default_factory=list)  # statement + verified evidence
    propagated_scope: list[dict[str, Any]] = Field(default_factory=list)      # global conditions applied/carved out
    temporal: dict[str, Any] = Field(default_factory=dict)                    # effective-date evidence decision
    status_derivation: str | None = None
    warnings: list[str] = Field(default_factory=list)
    rule: dict[str, Any] | None = None  # normalized record (kept for review even if rejected)
    raw: Any = None                     # candidate exactly as returned

    @property
    def validated(self) -> bool:
        """Passed every check: published (accepted), historical (expired as of the query
        date), or suppressed as an exact same-source duplicate of such a record. Coverage,
        repair-target and scope-mapping logic treat all three alike."""
        return self.accepted or self.historical or self.duplicate_of is not None


class RepairPass(BaseModel):
    """Audit of the (at most one) targeted repair request of a pipeline run."""

    invoked: bool = False
    reason: str
    prompt_version: str | None = None
    # One entry per target: ref, sources, pre-repair state, the repair's classification,
    # produced/accepted candidate indices and the final resolution (repair.py).
    targets: list[dict[str, Any]] = Field(default_factory=list)
    cache_key: str | None = None
    cache_hit: bool | None = None
    cache_entry: str | None = None
    prompt_sha256: str | None = None
    provider_metadata: dict[str, Any] = Field(default_factory=dict)
    raw_response_text: str = ""
    # Named-subject scope mappings verified by this request (scope.py), with their final state.
    mappings: list[dict[str, Any]] = Field(default_factory=list)
    candidate_indices: list[int] = Field(default_factory=list)
    accepted_count: int = 0
    rejected_count: int = 0
    errors: list[str] = Field(default_factory=list)


class ExtractionRun(BaseModel):
    disclaimer: str = ("Not legal advice. Automated extraction for a research prototype; "
                       "verify against the cited source.")
    run_at: str
    source: SourceMeta
    provider: str
    model: str
    prompt_version: str
    prompt_sha256: str
    response_schema_sha256: str
    generation_settings: dict[str, Any]
    as_of: str
    cache_key: str
    cache_hit: bool
    cache_entry: str
    provider_metadata: dict[str, Any] = Field(default_factory=dict)
    candidate_count: int = 0
    accepted_count: int = 0
    rules: list[dict[str, Any]] = Field(default_factory=list)
    candidates: list[CandidateResult] = Field(default_factory=list)
    posture: dict[str, Any] = Field(default_factory=dict)             # declared vs established (posture.py)
    base_dates: list[dict[str, Any]] = Field(default_factory=list)    # enactment dates found in the raw text
    provision_inventory: list[dict[str, Any]] = Field(default_factory=list)
    global_scope: list[dict[str, Any]] = Field(default_factory=list)  # each with its evidence check
    source_view: dict[str, Any] = Field(default_factory=dict)         # removed page artifacts, verbatim
    coverage: dict[str, Any] = Field(default_factory=dict)            # inventory coverage closure
    scope_challenges: list[dict[str, Any]] = Field(default_factory=list)  # out_of_scope items re-examined
    scope_mappings: list[dict[str, Any]] = Field(default_factory=list)    # named-subject pairs and outcomes
    legislative_session: dict[str, Any] | None = None                     # legislative.find_session
    empty_result: dict[str, Any] | None = None                        # no-rules justification and its check
    legacy_replay: dict[str, Any] | None = None                       # set only when replaying a pre-v6 response
    dedupe: list[dict[str, Any]] = Field(default_factory=list)        # same-source duplicates suppressed (finalize)
    chunking: dict[str, Any] | None = None                            # large-document mode audit (chunked.py)
    repair: RepairPass | None = None
    document_status: Literal["complete", "review_required"] = "review_required"
    review_reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    raw_response_text: str = ""

    @property
    def fully_accepted(self) -> bool:
        return not self.errors and self.accepted_count == self.candidate_count
