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
CitationStatus = Literal["exact_match", "normalized_match", "failed"]

# Same pattern as the official schema's effective_date.
PartialDate = Annotated[str, StringConstraints(pattern=r"^\d{4}(-\d{2}(-\d{2})?)?$")]


# ------------------------------------------------------------ generation layer

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
    and propagated deterministically to every rule it governs."""

    model_config = ConfigDict(extra="forbid", strict=True)

    id: str = Field(min_length=1, description="Short id, e.g. 'S1'.")
    kind: Literal["exemption", "coverage_condition"]
    statement: str = Field(min_length=1, description="The condition in plain language.")
    citation: str = Field(min_length=1, description="Official citation of the provision stating it.")
    governs: str | None = Field(description="Provision ref it governs; null if it governs the whole document.")
    evidence: str = Field(min_length=1, description="Verbatim text stating the condition.")


class ExtractedRule(BaseModel):
    """One rule as returned by the model, before trusted metadata is added."""

    model_config = ConfigDict(extra="forbid", strict=True)

    category: Category
    title: str = Field(min_length=1, description="Short name of the law or provision.")
    requirement: str = Field(min_length=1, description="One or two plain-language sentences stating the rule.")
    key_value: str | None = Field(description="Headline number or formula, if the quote states one.")
    coverage_conditions: str | None = Field(description="Coverage specific to this rule only (not document-wide).")
    exemptions: str | None = Field(description="Exemptions specific to this rule only (not document-wide).")
    scope_carve_outs: list[ScopeCarveOut]
    operative_conditions: list[OperativeCondition]
    interaction: str | None = Field(description="Stated relationship with another legal regime; not a mere citation.")
    enactment_status: EnactmentStatus = Field(description="enacted law, pending bill/proposal, or failed proposal.")
    effective_date: PartialDate | None = Field(description="Only an explicitly stated calendar date: YYYY-MM-DD, YYYY-MM or YYYY.")
    effective_date_evidence: str | None = Field(description="Verbatim text stating when the law/obligation takes effect.")
    citation: str = Field(min_length=1, description="Official citation as identified in the document.")
    quoted_span: str = Field(min_length=1, description="Verbatim, contiguous text from ONE segment of the document.")
    conflict_note: str | None = Field(description="Only a genuine simultaneous conflict or ambiguity; never version history.")
    version_note: str | None = Field(description="Amendment/version history of this provision shown in the text.")
    version_evidence: str | None = Field(description="Verbatim text of the version or amendment annotation.")


class ProvisionNote(BaseModel):
    """One entry of the model's provision inventory (audit only; never published)."""

    model_config = ConfigDict(extra="forbid", strict=True)

    ref: str = Field(min_length=1, description="Provision reference as shown in the document, e.g. '(2)(b)'.")
    summary: str = Field(description="A few words on what the provision does.")
    category: Category | None = Field(description="Official category if in scope, else null.")


class ExtractionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    provisions: list[ProvisionNote]
    global_scope: list[ScopeCondition]
    rules: list[ExtractedRule]


# Keywords the provider may rely on. Everything else (pattern, minLength,
# additionalProperties, titles) is enforced locally, not trusted to the API.
_GENERATION_SCHEMA_KEYS = {"type", "properties", "required", "items", "enum", "anyOf", "description",
                           "minimum", "maximum"}


def generation_json_schema() -> dict[str, Any]:
    """ExtractionResponse as a self-contained JSON Schema for constrained decoding:
    $refs inlined and keywords reduced to a conservative, widely supported subset."""
    full = ExtractionResponse.model_json_schema()
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
    model_span: str                    # exactly as the model returned it
    source_span: str | None = None     # exact source text at [start:end]
    reason: str | None = None


class CandidateResult(BaseModel):
    index: int
    accepted: bool = False
    rejection_reasons: list[str] = Field(default_factory=list)
    pydantic_valid: bool = False
    pydantic_errors: list[str] = Field(default_factory=list)
    schema_valid: bool | None = None   # None: not reached
    schema_errors: list[str] = Field(default_factory=list)
    citation: CitationCheck | None = None
    effective_date_evidence: CitationCheck | None = None
    version_evidence: CitationCheck | None = None
    operative_conditions: list[dict[str, Any]] = Field(default_factory=list)  # statement + verified evidence
    propagated_scope: list[dict[str, Any]] = Field(default_factory=list)      # global conditions applied/carved out
    status_derivation: str | None = None
    warnings: list[str] = Field(default_factory=list)
    rule: dict[str, Any] | None = None  # normalized record (kept for review even if rejected)
    raw: Any = None                     # candidate exactly as returned


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
    provision_inventory: list[dict[str, Any]] = Field(default_factory=list)
    global_scope: list[dict[str, Any]] = Field(default_factory=list)  # each with its evidence check
    source_view: dict[str, Any] = Field(default_factory=dict)         # removed page artifacts, verbatim
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    raw_response_text: str = ""

    @property
    def fully_accepted(self) -> bool:
        return not self.errors and self.accepted_count == self.candidate_count
