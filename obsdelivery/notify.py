"""Fault catalogs, missing-measurement notification products and the
stoppage reconstruction service.

A *notification product* is an immutable, independently readable batch of
missing-measurement notifications.  Each line is built from an original
binary32 bit pattern and a catalog version:

* only a NaN pattern is a missing-measurement code; its 23-bit payload is
  resolved against the catalog;
* finite values, infinity, "no record" (``MISSING``) and NaN payloads that
  differ are all kept distinct via the recorded ``kind``/``payload``/``bits``;
* every line carries the fixed revision, channel, timing reading, original
  bits, the retained (specificity-filtered) rules and the resolved reason set;
* an unexplained code is kept with the explicit ``UNKNOWN`` marker rather than
  guessing a reason.

The stoppage reconstruction lives in ``stoppages``; this module just persists
its inputs (registrations) and result so a later reader can re-open it.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from . import semantics
from .canonical import canonical_json, digest
from .errors import (
    CatalogConflict,
    CatalogNotFound,
    IntegrityError,
    NotificationBatchConflict,
    NotificationBatchNotFound,
    StopReconstructionNotFound,
    ValidationError,
)
from .faults import FaultCatalog
from .service import SemanticsService, _require_revision
from .stoppages import normalise_registrations, solve
from .storage import Store, utcnow


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} must be a non-empty string, got {value!r}")
    return value


class NotificationService:
    """Catalogs + notification products + persisted reconstructions."""

    def __init__(self, store: Store, semantics_service: SemanticsService | None = None):
        self.store = store
        self.semantics = semantics_service or SemanticsService(store)

    # -- fault catalogs ------------------------------------------------------
    def publish_catalog(self, version: str, rules: Iterable[dict], model: str | None = None) -> dict:
        catalog = FaultCatalog.from_mapping(version, rules, model)
        body = catalog.canonical()
        content_digest = catalog.content_digest()
        existing = self.store.conn.execute(
            "SELECT * FROM fault_catalog WHERE version=?", (version,)
        ).fetchone()
        if existing is not None:
            if existing["content_digest"] != content_digest:
                raise CatalogConflict(
                    f"fault catalog {version!r} exists with different content"
                )
            return self._catalog_view(existing, created=False)
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO fault_catalog"
                "(version, model, rules_json, content_digest, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (version, model, canonical_json(body["rules"]), content_digest, utcnow()),
            )
        row = self.store.conn.execute(
            "SELECT * FROM fault_catalog WHERE version=?", (version,)
        ).fetchone()
        return self._catalog_view(row, created=True)

    def _catalog_from_row(self, row) -> FaultCatalog:
        import json

        rules = json.loads(row["rules_json"])
        catalog = FaultCatalog.from_mapping(row["version"], rules, row["model"])
        if catalog.content_digest() != row["content_digest"]:
            raise IntegrityError(
                f"fault catalog {row['version']!r} digest mismatch"
            )
        return catalog

    def _catalog_view(self, row, created: bool) -> dict:
        catalog = self._catalog_from_row(row)
        return {
            "version": catalog.version,
            "model": catalog.model,
            "content_digest": row["content_digest"],
            "rules": [rule.canonical() for rule in catalog.rules],
            "created": created,
        }

    def get_catalog(self, version: str) -> dict:
        row = self.store.conn.execute(
            "SELECT * FROM fault_catalog WHERE version=?", (version,)
        ).fetchone()
        if row is None:
            raise CatalogNotFound(version)
        return self._catalog_view(row, created=False)

    def resolve_code(self, catalog_version: str, bits: str) -> dict:
        """Resolve one original bit pattern against a catalog (no persistence)."""
        catalog = self._catalog_row_catalog(catalog_version)
        return self._resolve_bits(catalog, bits)

    def _catalog_row_catalog(self, version: str) -> FaultCatalog:
        row = self.store.conn.execute(
            "SELECT * FROM fault_catalog WHERE version=?", (version,)
        ).fetchone()
        if row is None:
            raise CatalogNotFound(version)
        return self._catalog_from_row(row)

    @staticmethod
    def _resolve_bits(catalog: FaultCatalog, bits: str) -> dict:
        decoded = semantics.decode(semantics.parse_bits(bits))
        base = {
            "bits": decoded.bits,
            "kind": decoded.kind,
            "sign": decoded.sign,
            "payload": decoded.payload,
            "is_missing_code": decoded.kind == semantics.NAN,
        }
        if decoded.kind != semantics.NAN:
            base.update(
                {
                    "status": "NOT_A_MISSING_CODE",
                    "matched_rule_ids": [],
                    "retained_rule_ids": [],
                    "reasons": [],
                }
            )
            return base
        match = catalog.resolve(decoded.payload)
        base.update(
            {
                "status": match.status,
                "matched_rule_ids": list(match.matched_rule_ids),
                "retained_rule_ids": list(match.retained_rule_ids),
                "reasons": list(match.reasons),
            }
        )
        return base

    # -- notification products ----------------------------------------------
    def publish_notifications(
        self,
        product_id: str,
        catalog_version: str,
        notifications: Sequence[dict],
    ) -> dict:
        """Build and persist an immutable batch of notification lines."""
        if not isinstance(product_id, str) or not product_id:
            raise ValidationError("product_id must be a non-empty string")
        catalog = self._catalog_row_catalog(catalog_version)
        if isinstance(notifications, (str, bytes)) or not isinstance(notifications, Sequence):
            raise ValidationError("notifications must be a sequence")
        lines = []
        seen = set()
        for record in notifications:
            if not isinstance(record, dict):
                raise ValidationError(f"notification record is not an object: {record!r}")
            nid = _require_text(record.get("notification_id"), "notification_id")
            if nid in seen:
                raise ValidationError(f"duplicate notification_id {nid!r}")
            seen.add(nid)
            sample_id = _require_text(record.get("sample_id"), "sample_id")
            revision = _require_revision(record.get("revision"))
            channel = _require_text(record.get("channel"), "channel")
            reading = record.get("reading")
            if isinstance(reading, bool) or not isinstance(reading, int):
                raise ValidationError(f"{nid}.reading must be an integer, got {reading!r}")
            bits_raw = record.get("bits")
            try:
                bits = semantics.format_bits(semantics.parse_bits(bits_raw))
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc
            resolution = self._resolve_bits(catalog, bits)
            if not resolution["is_missing_code"]:
                raise ValidationError(
                    f"{nid}: {bits} is {resolution['kind']}, not a NaN missing code"
                )
            lines.append(
                {
                    "notification_id": nid,
                    "sample_id": sample_id,
                    "revision": revision,
                    "channel": channel,
                    "reading": reading,
                    "original_bits": bits,
                    "payload": resolution["payload"],
                    "kind": resolution["kind"],
                    "sign": resolution["sign"],
                    "applicable_rules": resolution["retained_rule_ids"],
                    "matched_rules": resolution["matched_rule_ids"],
                    "reasons": resolution["reasons"],
                    "status": resolution["status"],
                }
            )
        lines.sort(key=lambda line: line["notification_id"])
        body = {
            "product_id": product_id,
            "catalog_version": catalog.version,
            "catalog_digest": catalog.content_digest(),
            "notifications": lines,
        }
        content_digest = digest(body)

        existing = self.store.conn.execute(
            "SELECT * FROM notification_products WHERE product_id=?", (product_id,)
        ).fetchone()
        if existing is not None:
            if existing["content_digest"] != content_digest:
                raise NotificationBatchConflict(
                    f"notification product {product_id!r} exists with different content"
                )
            return self._product_view(existing, created=False)

        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO notification_products"
                "(product_id, catalog_version, notifications_json, content_digest, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    product_id,
                    catalog.version,
                    canonical_json(body),
                    content_digest,
                    utcnow(),
                ),
            )
            for ordinal, line in enumerate(lines):
                conn.execute(
                    "INSERT INTO notification_lines "
                    "(product_id, ordinal, notification_id, sample_id, revision, "
                    " channel, reading, original_bits, payload, retained_rules_json, "
                    " reasons_json, status, canonical_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        product_id,
                        ordinal,
                        line["notification_id"],
                        line["sample_id"],
                        line["revision"],
                        line["channel"],
                        line["reading"],
                        line["original_bits"],
                        line["payload"],
                        canonical_json(line["applicable_rules"]),
                        canonical_json(line["reasons"]),
                        line["status"],
                        canonical_json(line),
                    ),
                )
        row = self.store.conn.execute(
            "SELECT * FROM notification_products WHERE product_id=?", (product_id,)
        ).fetchone()
        return self._product_view(row, created=True)

    def get_notifications(self, product_id: str) -> dict:
        row = self.store.conn.execute(
            "SELECT * FROM notification_products WHERE product_id=?", (product_id,)
        ).fetchone()
        if row is None:
            raise NotificationBatchNotFound(product_id)
        return self._product_view(row, created=False)

    def _product_view(self, row, created: bool) -> dict:
        import json

        body = json.loads(row["notifications_json"])
        actual = digest(body)
        if actual != row["content_digest"]:
            raise IntegrityError(
                f"notification product {row['product_id']!r} digest mismatch"
            )
        rows = self.store.conn.execute(
            "SELECT canonical_json FROM notification_lines WHERE product_id=? "
            "ORDER BY ordinal",
            (row["product_id"],),
        ).fetchall()
        stored = [json.loads(r["canonical_json"]) for r in rows]
        if canonical_json(stored) != canonical_json(body["notifications"]):
            raise IntegrityError(
                f"notification product {row['product_id']!r}: line table differs"
            )
        return {
            "product_id": row["product_id"],
            "catalog_version": row["catalog_version"],
            "catalog_digest": body["catalog_digest"],
            "content_digest": row["content_digest"],
            "notifications": body["notifications"],
            "created": created,
        }

    # -- stoppage reconstruction --------------------------------------------
    def reconstruct(
        self,
        reconstruction_id: str,
        product_id: str,
        registrations: Sequence[dict],
        reasons: Iterable[str] = (),
    ) -> dict:
        """Solve the minimum stoppages and persist the result immutably."""
        if not isinstance(reconstruction_id, str) or not reconstruction_id:
            raise ValidationError("reconstruction_id must be a non-empty string")
        product = self.get_notifications(product_id)
        channels = normalise_registrations(registrations)
        notifications = [
            {
                "notification_id": line["notification_id"],
                "channel": line["channel"],
                "reading": line["reading"],
                "bits": line["original_bits"],
                "payload": line["payload"],
                "rules": line["applicable_rules"],
                "reasons": line["reasons"],
                "status": line["status"],
            }
            for line in product["notifications"]
        ]
        result = solve(notifications, [c.canonical() for c in channels.values()], reasons)
        result["product_id"] = product_id
        result["catalog_version"] = product["catalog_version"]
        result["registrations"] = [channels[c].canonical() for c in sorted(channels)]
        content_digest = digest(
            {
                "product_id": product_id,
                "registrations": result["registrations"],
                "reasons": result["reasons"],
                "result": result,
            }
        )
        existing = self.store.conn.execute(
            "SELECT * FROM stop_reconstructions WHERE reconstruction_id=?",
            (reconstruction_id,),
        ).fetchone()
        if existing is not None:
            if existing["content_digest"] != content_digest:
                raise ValidationError(
                    f"reconstruction {reconstruction_id!r} exists with different content"
                )
            return self._reconstruction_view(existing, created=False)
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO stop_reconstructions "
                "(reconstruction_id, product_id, registrations_json, result_json, "
                " content_digest, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    reconstruction_id,
                    product_id,
                    canonical_json(result["registrations"]),
                    canonical_json(result),
                    content_digest,
                    utcnow(),
                ),
            )
        row = self.store.conn.execute(
            "SELECT * FROM stop_reconstructions WHERE reconstruction_id=?",
            (reconstruction_id,),
        ).fetchone()
        return self._reconstruction_view(row, created=True)

    def get_reconstruction(self, reconstruction_id: str) -> dict:
        row = self.store.conn.execute(
            "SELECT * FROM stop_reconstructions WHERE reconstruction_id=?",
            (reconstruction_id,),
        ).fetchone()
        if row is None:
            raise StopReconstructionNotFound(reconstruction_id)
        return self._reconstruction_view(row, created=False)

    def _reconstruction_view(self, row, created: bool) -> dict:
        import json

        result = json.loads(row["result_json"])
        registrations = json.loads(row["registrations_json"])
        actual = digest(
            {
                "product_id": row["product_id"],
                "registrations": registrations,
                "reasons": result.get("reasons", []),
                "result": result,
            }
        )
        if actual != row["content_digest"]:
            raise IntegrityError(
                f"reconstruction {row['reconstruction_id']!r} digest mismatch"
            )
        result["reconstruction_id"] = row["reconstruction_id"]
        result["created"] = created
        result["content_digest"] = row["content_digest"]
        return result
