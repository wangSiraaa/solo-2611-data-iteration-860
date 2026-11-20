"""Fault-code catalog over binary32 NaN payload bits (pure, storage-free).

A catalog version belongs to a device model and is a list of rules.  Each rule
constrains some payload bits::

    (payload & mask) == value

and explains a *set* of reasons.  Every rule is validated so that ``value``
only sets bits that ``mask`` actually constrains.

Resolution of one missing-measurement code (a NaN payload) is **specificity
based**, not first-match:

* Collect every rule that matches the payload.
* A matching rule ``W`` is *covered* when another matching rule ``E`` is
  strictly more specific: the constrained-bit set of ``E`` is a strict
  superset of that of ``W`` (``mask_E & mask_W == mask_W`` and
  ``mask_E != mask_W``) and agrees with ``W`` on the bits ``W`` constrains
  (``value_E & mask_W == value``).  Note that because ``E`` matches the same
  payload this is equivalent to saying the two are consistent.
* Keep the matching rules that are not covered by any more specific match.
* The reason set is the union of the kept rules' reasons.

Two rules whose masks are incomparable (neither is a subset of the other) can
both remain in effect; their reasons are unioned.  A payload that matches no
rule is reported with the explicit ``UNKNOWN`` marker.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .canonical import digest
from .errors import ValidationError

PAYLOAD_BITS = 23
PAYLOAD_MASK = (1 << PAYLOAD_BITS) - 1

KNOWN = "KNOWN"
UNKNOWN = "UNKNOWN"


def _require_rule_id(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"rule_id must be a non-empty string, got {value!r}")
    return value


def _require_payload_bits(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{field} must be an integer, got {value!r}")
    if not 0 <= value <= PAYLOAD_MASK:
        raise ValidationError(
            f"{field} must fit in {PAYLOAD_BITS} payload bits, got {value!r}"
        )
    return value


def _normalise_reasons(value: Any) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise ValidationError(f"reasons must be an iterable of strings, got {value!r}")
    reasons = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValidationError(f"reason must be a non-empty string, got {item!r}")
        reasons.append(item)
    if not reasons:
        raise ValidationError("a fault rule must explain at least one reason")
    if len(set(reasons)) != len(reasons):
        raise ValidationError(f"reasons contain duplicates: {reasons!r}")
    return tuple(sorted(reasons))


@dataclass(frozen=True)
class FaultRule:
    rule_id: str
    mask: int
    value: int
    reasons: tuple[str, ...]

    @staticmethod
    def from_mapping(obj: Any) -> "FaultRule":
        if not isinstance(obj, dict):
            raise ValidationError(f"fault rule must be an object, got {obj!r}")
        rule_id = _require_rule_id(obj.get("rule_id"))
        mask = _require_payload_bits(obj.get("mask"), f"{rule_id}.mask")
        value = _require_payload_bits(obj.get("value"), f"{rule_id}.value")
        if value & ~mask:
            raise ValidationError(
                f"rule {rule_id!r}: value {value} sets bits outside mask {mask}"
            )
        reasons = _normalise_reasons(obj.get("reasons"))
        return FaultRule(rule_id, mask, value, reasons)

    def matches(self, payload: int) -> bool:
        return (payload & self.mask) == self.value

    def canonical(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "mask": self.mask,
            "value": self.value,
            "reasons": list(self.reasons),
        }


def more_specific(specific: FaultRule, wide: FaultRule) -> bool:
    """True iff ``specific`` is strictly more specific and consistent with ``wide``."""
    if specific.mask == wide.mask:
        return False
    if (specific.mask & wide.mask) != wide.mask:
        return False
    return (specific.value & wide.mask) == wide.value


@dataclass(frozen=True)
class FaultCatalog:
    version: str
    model: str | None
    rules: tuple[FaultRule, ...]

    @staticmethod
    def from_mapping(version: Any, entries: Any, model: Any = None) -> "FaultCatalog":
        if not isinstance(version, str) or not version:
            raise ValidationError("catalog version must be a non-empty string")
        if model is not None and (not isinstance(model, str) or not model.strip()):
            raise ValidationError(f"catalog model must be a non-empty string, got {model!r}")
        if isinstance(entries, (str, bytes)) or not isinstance(entries, Iterable):
            raise ValidationError("catalog entries must be an iterable of objects")
        rules: list[FaultRule] = []
        seen: set[str] = set()
        for entry in entries:
            rule = FaultRule.from_mapping(entry)
            if rule.rule_id in seen:
                raise ValidationError(f"duplicate fault rule id {rule.rule_id!r}")
            seen.add(rule.rule_id)
            rules.append(rule)
        if not rules:
            raise ValidationError("catalog must contain at least one rule")
        rules.sort(key=lambda rule: rule.rule_id)
        return FaultCatalog(version, model, tuple(rules))

    def canonical(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "model": self.model,
            "rules": [rule.canonical() for rule in self.rules],
        }

    def content_digest(self) -> str:
        return digest(self.canonical())

    def resolve(self, payload: int) -> "FaultMatch":
        return resolve_payload(self.rules, payload)

    def rule_ids(self) -> tuple[str, ...]:
        return tuple(rule.rule_id for rule in self.rules)


@dataclass(frozen=True)
class FaultMatch:
    payload: int
    matched_rule_ids: tuple[str, ...]
    retained_rule_ids: tuple[str, ...]
    reasons: tuple[str, ...]
    status: str  # KNOWN | UNKNOWN

    @property
    def known(self) -> bool:
        return self.status == KNOWN

    def canonical(self) -> dict[str, Any]:
        return {
            "payload": self.payload,
            "matched_rule_ids": list(self.matched_rule_ids),
            "retained_rule_ids": list(self.retained_rule_ids),
            "reasons": list(self.reasons),
            "status": self.status,
        }


def resolve_payload(rules: Iterable[FaultRule], payload: int) -> FaultMatch:
    payload = _require_payload_bits(payload, "payload")
    matched = [rule for rule in rules if rule.matches(payload)]
    matched.sort(key=lambda rule: rule.rule_id)
    retained = [
        rule
        for rule in matched
        if not any(more_specific(other, rule) for other in matched)
    ]
    reasons = sorted({reason for rule in retained for reason in rule.reasons})
    status = KNOWN if matched else UNKNOWN
    return FaultMatch(
        payload=payload,
        matched_rule_ids=tuple(rule.rule_id for rule in matched),
        retained_rule_ids=tuple(rule.rule_id for rule in retained),
        reasons=tuple(reasons),
        status=status,
    )


def catalog_from_records(entries: Any, version: str, model: Any = None) -> FaultCatalog:
    """Convenience wrapper used by public read-back of embedded catalogs."""
    return FaultCatalog.from_mapping(version, entries, model)
