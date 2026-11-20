"""Public artefact rendering and verification.

* ``public_semantic_product`` — immutable canonical observation product.
* ``public_delivery`` — standalone delivery document with enough information
  for an independent reader to re-check the actual decimal representation
  without consulting the current observation state.

Both builders re-verify the stored content digests before rendering.
"""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path

from .delivery import DeliveryService
from .faults import FaultCatalog
from .faults import UNKNOWN as FAULT_UNKNOWN
from .notify import NotificationService
from .service import SemanticsService
from .stoppages import solve as solve_stoppages
from . import semantics


def public_semantic_product(semantics: SemanticsService, product_id: str) -> dict:
    product = semantics.get_semantic_product(product_id)
    return {
        "kind": "semantic_product",
        "product_id": product["id"],
        "reference_version": product["reference_version"],
        "reference_digest": product["reference_digest"],
        "content_digest": product["content_digest"],
        "sample_ids": product["sample_ids"],
        "records": [
            {
                "sample_id": item["sample_id"],
                "received_revision": item["received_revision"],
                "original_bits": item["bits"],
                "kind": item["kind"],
                "sign": item["sign"],
                "payload": item["payload"],
                "exact_value": item["value"],
                "negated_value": item["negated_value"],
                "expected": item["expected"],
                "tolerance": item["tolerance"],
                "status": item["status"],
                "difference": item["difference"],
                "abs_difference": item["abs_difference"],
            }
            for item in product["items"]
        ],
    }


def public_delivery(delivery: DeliveryService, delivery_id: str) -> dict:
    view = delivery.get_delivery(delivery_id)
    return {
        "kind": "delivery",
        "delivery_id": view["delivery_id"],
        "canonical_product": {
            "product_id": view["product_id"],
            "content_digest": view["product_digest"],
        },
        "reference": {
            "version": view["reference_version"],
            "content_digest": view["reference_digest"],
        },
        "reader_contract": view["contract"],
        "contract_id": view["contract_id"],
        "content_digest": view["content_digest"],
        "lines": [
            {
                "sample_id": line["sample_id"],
                "original_bits": line["original_bits"],
                "kind": line["kind"],
                "sign": line["sign"],
                "original_exact_value": line["exact_value"],
                "original_negated_value": line["negated_value"],
                "semantic_status": line["status"],
                "delivered_status": line["delivered_status"],
                "coefficient": line["coefficient"],
                "scale": line["scale"],
                "reader_value": line["reader_value"],
                "exact_error": line["error"],
                "decoder": "coefficient / 10**scale",
                "evidence": line["evidence"],
            }
            for line in view["lines"]
        ],
    }


def verify_public_delivery(document: dict) -> list[str]:
    """Re-derive all checks from a standalone public document.

    Returns a list of textual problems (empty means the document is
    internally consistent).  It does not need a database: it recomputes the
    coefficient bound, error budget, scale legality and reader value.
    """
    problems: list[str] = []
    contract = document["reader_contract"]
    allowed = set(contract["allowed_scales"])
    bound = contract["max_abs_coefficient"]
    budget = Fraction(contract["max_abs_error"])
    required = {
        "sample_id", "original_bits", "kind", "sign", "original_exact_value",
        "semantic_status", "delivered_status", "coefficient", "scale",
        "reader_value", "exact_error",
    }
    for line in document["lines"]:
        missing = required - line.keys()
        if missing:
            problems.append(f"{line.get('sample_id')}: missing {sorted(missing)}")
            continue
        sample = line["sample_id"]
        scale = line["scale"]
        coefficient = line["coefficient"]
        if scale not in allowed:
            problems.append(f"{sample}: scale {scale} not allowed")
        if abs(coefficient) > bound:
            problems.append(f"{sample}: coefficient {coefficient} exceeds {bound}")
        if line["semantic_status"] != line["delivered_status"]:
            problems.append(f"{sample}: semantic status changed on delivery")
        value = Fraction(line["original_exact_value"])
        delivered = Fraction(coefficient, 10 ** scale)
        error = delivered - value
        if str(Fraction(line["exact_error"])) != str(error):
            problems.append(f"{sample}: stored error {line['exact_error']} != {error}")
        if abs(error) > budget:
            problems.append(f"{sample}: error {error} exceeds budget {budget}")
        if str(Fraction(line["reader_value"])) != str(delivered):
            problems.append(f"{sample}: reader value mismatch")
    return problems


def write_json(path: str | Path, document: dict) -> Path:
    target = Path(path)
    target.write_text(json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8")
    return target


# ---------------------------------------------------------------------------
# notification / stoppage public documents
# ---------------------------------------------------------------------------

def public_notifications(notify: NotificationService, product_id: str) -> dict:
    """Standalone document of a notification batch, including its catalog.

    The embedded catalog rules let an independent reader re-resolve every
    payload (retained rules, reasons and UNKNOWN status) without the database.
    """
    product = notify.get_notifications(product_id)
    catalog = notify.get_catalog(product["catalog_version"])
    return {
        "kind": "notification_product",
        "product_id": product["product_id"],
        "catalog": {
            "version": catalog["version"],
            "model": catalog["model"],
            "content_digest": catalog["content_digest"],
            "rules": catalog["rules"],
        },
        "content_digest": product["content_digest"],
        "notifications": [
            {
                "notification_id": line["notification_id"],
                "sample_id": line["sample_id"],
                "revision": line["revision"],
                "channel": line["channel"],
                "reading": line["reading"],
                "original_bits": line["original_bits"],
                "payload": line["payload"],
                "applicable_rules": line["applicable_rules"],
                "reasons": line["reasons"],
                "status": line["status"],
            }
            for line in product["notifications"]
        ],
    }


def public_reconstruction(notify: NotificationService, reconstruction_id: str) -> dict:
    view = notify.get_reconstruction(reconstruction_id)
    product = notify.get_notifications(view["product_id"])
    return {
        "kind": "stop_reconstruction",
        "reconstruction_id": view["reconstruction_id"],
        "product_id": view["product_id"],
        "catalog_version": view["catalog_version"],
        "registrations": view["registrations"],
        "content_digest": view["content_digest"],
        "reasons": view["reasons"],
        "result": {
            "feasible": view["feasible"],
            "code": view["code"],
            "min_stoppages": view["min_stoppages"],
            "witness": view["witness"],
            "reason_counts": view["reason_counts"],
            "evidence_gaps": view["evidence_gaps"],
            "unexplained_notifications": view["unexplained_notifications"],
            "unregistered_notifications": view["unregistered_notifications"],
        },
        "notifications": [
            {
                "notification_id": line["notification_id"],
                "channel": line["channel"],
                "reading": line["reading"],
                "payload": line["payload"],
                "original_bits": line["original_bits"],
                "applicable_rules": line["applicable_rules"],
                "reasons": line["reasons"],
                "status": line["status"],
            }
            for line in product["notifications"]
        ],
    }


def verify_public_notifications(document: dict) -> list[str]:
    """Re-resolve every notification from the embedded catalog (no database)."""
    problems: list[str] = []
    catalog_body = document.get("catalog", {})
    try:
        catalog = FaultCatalog.from_mapping(
            catalog_body.get("version"), catalog_body.get("rules", []), catalog_body.get("model")
        )
    except Exception as exc:  # noqa: BLE001 - report, do not raise
        return [f"catalog invalid: {exc}"]
    if catalog.content_digest() != catalog_body.get("content_digest"):
        problems.append("catalog digest mismatch")
    for line in document.get("notifications", []):
        nid = line.get("notification_id")
        try:
            decoded = semantics.decode(semantics.parse_bits(line["original_bits"]))
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{nid}: bad bits: {exc}")
            continue
        if decoded.kind != semantics.NAN:
            problems.append(f"{nid}: {decoded.bits} is not a NaN missing code")
            continue
        if decoded.payload != line["payload"]:
            problems.append(f"{nid}: payload disagrees with bits")
        match = catalog.resolve(decoded.payload)
        if list(match.retained_rule_ids) != list(line.get("applicable_rules", [])):
            problems.append(f"{nid}: retained rules disagree")
        if list(match.reasons) != list(line.get("reasons", [])):
            problems.append(f"{nid}: reasons disagree")
        if match.status != line.get("status"):
            problems.append(f"{nid}: status disagrees")
        if match.status == FAULT_UNKNOWN and line.get("reasons"):
            problems.append(f"{nid}: UNKNOWN code carries reasons")
    return problems


def verify_public_reconstruction(document: dict) -> list[str]:
    """Re-solve the batch independently and compare every claimed quantity."""
    problems: list[str] = []
    notifications = [
        {
            "notification_id": line["notification_id"],
            "channel": line["channel"],
            "reading": line["reading"],
            "bits": line.get("original_bits"),
            "payload": line.get("payload"),
            "rules": line.get("applicable_rules", []),
            "reasons": line.get("reasons", []),
            "status": line.get("status"),
        }
        for line in document.get("notifications", [])
    ]
    # Recompute independently from the public registrations.
    recomputed = solve_stoppages(
        notifications,
        document.get("registrations", []),
        document.get("reasons", []),
    )
    claimed = document.get("result", {})
    if recomputed["feasible"] != claimed.get("feasible"):
        problems.append("feasibility disagrees with independent solve")
    if recomputed["code"] != claimed.get("code"):
        problems.append("code disagrees with independent solve")
    if recomputed["min_stoppages"] != claimed.get("min_stoppages"):
        problems.append("minimum stoppage count disagrees")
    if recomputed["reason_counts"] != claimed.get("reason_counts"):
        problems.append("reason count bounds disagree")
    if recomputed["feasible"]:
        witness = claimed.get("witness") or {}
        problems.extend(_verify_witness(document, witness))
    return problems


def _verify_witness(document: dict, witness: dict) -> list[str]:
    """Check that the witness grouping really explains every notification."""
    problems: list[str] = []
    notifications = {
        line["notification_id"]: line for line in document.get("notifications", [])
    }
    registrations = {reg["channel"]: reg for reg in document.get("registrations", [])}
    groups = witness.get("groups", [])
    if witness.get("min_stoppages") != len(groups):
        problems.append("witness stoppage count does not match its groups")
    covered: set[str] = set()
    for group in groups:
        ids = group.get("notification_ids", [])
        if not ids:
            problems.append("empty witness group")
            continue
        reason = group.get("reason")
        headers = set()
        low, high = None, None
        for nid in ids:
            line = notifications.get(nid)
            if line is None:
                problems.append(f"witness refers to unknown notification {nid}")
                continue
            if nid in covered:
                problems.append(f"notification {nid} appears in two groups")
            covered.add(nid)
            if reason is not None and reason not in line.get("reasons", []):
                problems.append(f"{nid}: witness reason {reason!r} not applicable")
            reg = registrations.get(line["channel"])
            if reg is None or reg.get("header") is None:
                problems.append(f"{nid}: channel has no header registration")
                continue
            headers.add(reg["header"])
            delay_lo, delay_hi = reg["delay_interval"]
            wlo = line["reading"] - delay_hi
            whi = line["reading"] - delay_lo
            low = wlo if low is None else max(low, wlo)
            high = whi if high is None else min(high, whi)
        if len(headers) != len(ids):
            problems.append("witness group puts two notifications of one header together")
        if low is not None and high is not None and low > high:
            problems.append("witness group has an empty time intersection")
        claimed_interval = group.get("time_interval")
        if claimed_interval is not None and low is not None:
            if [low, high] != list(claimed_interval):
                problems.append("witness time interval disagrees with re-derivation")
    if set(notifications) - covered:
        problems.append("witness leaves some notifications unexplained")
    return problems
