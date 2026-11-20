"""Minimum real-stoppage reconstruction (pure, exact, subset DP).

Inputs
------
* A batch of missing-measurement notifications.  Each carries a fixed
  revision, an instrument channel, a timing reading, original bits and a
  resolved reason set (from the fault catalog).
* For each channel: the owning *header* (meter/表头) and an integer closed
  interval ``[lo, hi]`` of the reporting delay.

Model
-----
A real stoppage has one actual time ``T = reading - delay`` (one delay is
chosen per notification, so two notifications on the same channel may have
different actual delays) and one reason ``R``.  A header leaves **at most one**
notification for a given stoppage, so no two notifications owned by the same
header may share a stoppage.  Every notification is produced by exactly one
stoppage.

A group of notifications can form one stoppage iff

* their headers are pairwise distinct;
* the intersection of their feasible time windows
  ``[reading - delay_hi, reading - delay_lo]`` is non-empty (closed intervals;
  touching at a single endpoint is enough -- in one dimension pairwise
  intersection implies a common point);
* their reason sets have a non-empty common intersection.

We find the minimum number of groups covering the batch with an exact subset
DP over ``2**n`` (``n <= 16``); we never enumerate set partitions (Bell(16) is
far too large) and never expand a long integer time range.  Among *all*
minimum-cardinality explanations we report, per reason, the minimum and maximum
number of stoppages attributed to that reason, plus one concrete witness
grouping with a common feasible time and reason for each stoppage.

Reason attribution
------------------
Each group may choose any reason in its common set.  Therefore, over minimum
explanations, for a reason ``r``:

* the maximum count is the largest number of groups whose common set contains
  ``r`` (assign all of those to ``r``; other groups pick another common
  reason);
* the minimum count is the smallest number of groups whose common set is
  exactly ``{r}`` (every other group can avoid ``r``).

Both extremes are computed with a second DP that follows only
minimum-cardinality transitions.

Evidence gaps
-------------
If any notification has no resolved reasons (an unexplained missing code) or
its channel has no header registration, no minimum is claimed: the result is
infeasible and lists the offending notifications, preserving the unknown
conclusion for the batch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .errors import ValidationError

KNOWN = "KNOWN"
UNKNOWN = "UNKNOWN"

OK = "OK"
INFEASIBLE = "INFEASIBLE"
_UNREGISTERED = "UNREGISTERED"
_HAS_UNKNOWN = "HAS_UNKNOWN"

_INF = 1 << 30


# ---------------------------------------------------------------------------
# validation / normalisation
# ---------------------------------------------------------------------------

def _require_text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} must be a non-empty string, got {value!r}")
    return value


def _require_int(value, field, *, allow_none=False):
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{field} must be an integer, got {value!r}")
    return value


@dataclass(frozen=True)
class HeaderChannel:
    channel: str
    header: str | None
    delay_lo: int | None
    delay_hi: int | None

    def canonical(self) -> dict:
        return {
            "channel": self.channel,
            "header": self.header,
            "delay_interval": None
            if self.delay_lo is None
            else [self.delay_lo, self.delay_hi],
        }


def normalise_registrations(registrations: Iterable[dict]) -> dict[str, HeaderChannel]:
    out: dict[str, HeaderChannel] = {}
    for item in registrations:
        if not isinstance(item, dict):
            raise ValidationError(f"registration must be an object, got {item!r}")
        channel = _require_text(item.get("channel"), "channel")
        header = item.get("header")
        if header is not None:
            _require_text(header, "header")
        if "delay_interval" in item:
            interval = item.get("delay_interval")
            if interval is None:
                lo = hi = None
            else:
                if not isinstance(interval, (list, tuple)) or len(interval) != 2:
                    raise ValidationError(
                        f"channel {channel!r}: delay_interval must be [lo, hi]"
                    )
                lo, hi = interval
        else:
            lo = item.get("delay_lo")
            hi = item.get("delay_hi")
        if (lo is None) != (hi is None):
            raise ValidationError(
                f"channel {channel!r}: delay_lo and delay_hi must both be given"
            )
        if lo is not None:
            _require_int(lo, f"{channel}.delay_lo")
            _require_int(hi, f"{channel}.delay_hi")
            if lo > hi:
                raise ValidationError(
                    f"channel {channel!r}: delay_lo {lo} exceeds delay_hi {hi}"
                )
        if channel in out:
            raise ValidationError(f"duplicate channel registration {channel!r}")
        out[channel] = HeaderChannel(channel, header, lo, hi)
    return out


@dataclass(frozen=True)
class _Notif:
    notification_id: str
    channel: str
    header: str | None
    reading: int
    bits: str | None
    payload: int | None
    rules: tuple[str, ...]
    reasons: tuple[str, ...]
    status: str
    delay_lo: int | None
    delay_hi: int | None

    @property
    def window(self) -> tuple[int, int] | None:
        if self.delay_lo is None or self.delay_hi is None:
            return None
        return (self.reading - self.delay_hi, self.reading - self.delay_lo)

    def canonical(self) -> dict:
        window = self.window
        return {
            "notification_id": self.notification_id,
            "channel": self.channel,
            "header": self.header,
            "reading": self.reading,
            "bits": self.bits,
            "payload": self.payload,
            "rules": list(self.rules),
            "reasons": list(self.reasons),
            "status": self.status,
            "delay_interval": None
            if self.delay_lo is None
            else [self.delay_lo, self.delay_hi],
            "window": None if window is None else [window[0], window[1]],
        }


def _build_notifs(notifications, registrations):
    channels = normalise_registrations(registrations)
    notifs: list[_Notif] = []
    seen = set()
    for raw in notifications:
        if not isinstance(raw, dict):
            raise ValidationError(f"notification must be an object, got {raw!r}")
        nid = _require_text(raw.get("notification_id"), "notification_id")
        if nid in seen:
            raise ValidationError(f"duplicate notification id {nid!r}")
        seen.add(nid)
        channel = _require_text(raw.get("channel"), f"{nid}.channel")
        reading = _require_int(raw.get("reading"), f"{nid}.reading")
        bits = raw.get("bits")
        if bits is not None:
            _require_text(bits, f"{nid}.bits")
        payload = raw.get("payload")
        _require_int(payload, f"{nid}.payload", allow_none=True)
        rules = raw.get("rules", ())
        reasons = raw.get("reasons", ())
        if isinstance(rules, (str, bytes)) or not isinstance(rules, Iterable):
            raise ValidationError(f"{nid}.rules must be an iterable")
        if isinstance(reasons, (str, bytes)) or not isinstance(reasons, Iterable):
            raise ValidationError(f"{nid}.reasons must be an iterable")
        reasons = tuple(reasons)
        status = raw.get("status", KNOWN if reasons else UNKNOWN)
        if status not in (KNOWN, UNKNOWN):
            raise ValidationError(f"{nid}.status must be {KNOWN!r} or {UNKNOWN!r}")
        if status == KNOWN and not reasons:
            raise ValidationError(f"{nid}: KNOWN notification must have reasons")
        reg = channels.get(channel)
        notifs.append(
            _Notif(
                notification_id=nid,
                channel=channel,
                header=None if reg is None else reg.header,
                reading=reading,
                bits=bits,
                payload=payload,
                rules=tuple(rules),
                reasons=reasons,
                status=status,
                delay_lo=None if reg is None else reg.delay_lo,
                delay_hi=None if reg is None else reg.delay_hi,
            )
        )
    if not notifs:
        raise ValidationError("at least one notification is required")
    notifs.sort(key=lambda n: n.notification_id)
    return channels, notifs


# ---------------------------------------------------------------------------
# feasible groups
# ---------------------------------------------------------------------------

def _feasible_groups(notifs, reason_index):
    """Return arrays indexed by bitmask: feasible, window bounds, reason mask."""
    n = len(notifs)
    size = 1 << n
    header_bit: dict[str, int] = {}
    for notif in notifs:
        header_bit.setdefault(notif.header, len(header_bit))
    header_mask = [0] * size
    win_lo = [0] * size
    win_hi = [0] * size
    rmask = [0] * size
    feasible = [False] * size
    for mask in range(1, size):
        low = mask & -mask
        i = low.bit_length() - 1
        rest = mask ^ low
        hb = 1 << header_bit[notifs[i].header]
        lo_i, hi_i = notifs[i].window
        if rest == 0:
            header_mask[mask] = hb
            win_lo[mask], win_hi[mask] = lo_i, hi_i
            rmask[mask] = _reason_bits(notifs[i].reasons, reason_index)
            feasible[mask] = True
            continue
        header_mask[mask] = header_mask[rest] | hb
        win_lo[mask] = max(win_lo[rest], lo_i)
        win_hi[mask] = min(win_hi[rest], hi_i)
        # A stoppage has ONE reason, so the group's usable reasons are the
        # *intersection* of its members' reason sets, not the union.
        rmask[mask] = rmask[rest] & _reason_bits(notifs[i].reasons, reason_index)
        feasible[mask] = (
            (header_mask[mask].bit_count() == mask.bit_count())
            and win_lo[mask] <= win_hi[mask]
            and rmask[mask] != 0
        )
    return feasible, win_lo, win_hi, rmask


def _reason_bits(reasons, reason_index) -> int:
    bits = 0
    for reason in reasons:
        bits |= 1 << reason_index[reason]
    return bits


# ---------------------------------------------------------------------------
# solver
# ---------------------------------------------------------------------------

def solve(
    notifications: Iterable[dict],
    registrations: Iterable[dict],
    reasons: Iterable[str] = (),
) -> dict:
    """Reconstruct the minimum number of real stoppages explaining a batch."""
    channels, notifs = _build_notifs(list(notifications), list(registrations))
    observed = {reason for n in notifs for reason in n.reasons}
    reason_list = sorted({_require_text(r, "reason") for r in reasons} | observed)
    reason_index = {reason: i for i, reason in enumerate(reason_list)}

    unexplained = [n.canonical() for n in notifs if n.status != KNOWN or not n.reasons]
    unregistered = [n.canonical() for n in notifs if n.header is None]
    gaps = []
    if unexplained:
        gaps.append({"code": _HAS_UNKNOWN, "notifications": unexplained})
    if unregistered:
        gaps.append({"code": _UNREGISTERED, "notifications": unregistered})

    base = {
        "channels": [channels[c].canonical() for c in sorted(channels)],
        "notifications": [n.canonical() for n in notifs],
        "reasons": reason_list,
        "evidence_gaps": gaps,
        "unexplained_notifications": unexplained,
        "unregistered_notifications": unregistered,
    }

    empty_counts = {
        reason: {"min": None, "max": None, "possible": False} for reason in reason_list
    }

    if unexplained or unregistered:
        base.update(
            {
                "feasible": False,
                "code": INFEASIBLE,
                "min_stoppages": None,
                "witness": None,
                "reason_counts": empty_counts,
            }
        )
        return base

    feasible, win_lo, win_hi, rmask = _feasible_groups(notifs, reason_index)
    size = 1 << len(notifs)
    dp_size = _minimum_cover_sizes(feasible, size)
    full = size - 1
    min_stoppages = dp_size[full]
    if min_stoppages >= _INF:
        # Registered singletons are always feasible; fail closed regardless.
        base.update(
            {
                "feasible": False,
                "code": INFEASIBLE,
                "min_stoppages": None,
                "witness": None,
                "reason_counts": empty_counts,
            }
        )
        return base

    reason_counts = _reason_extremes(feasible, rmask, dp_size, full, reason_list)
    witness = _build_witness(notifs, feasible, dp_size, full)
    base.update(
        {
            "feasible": True,
            "code": OK,
            "min_stoppages": min_stoppages,
            "witness": witness,
            "reason_counts": reason_counts,
        }
    )
    return base


def _minimum_cover_sizes(feasible, size) -> list[int]:
    dp = [_INF] * size
    dp[0] = 0
    for mask in range(1, size):
        low = mask & -mask
        rest = mask ^ low
        best = _INF
        sub = rest
        while True:
            group = sub | low
            if feasible[group]:
                cand = dp[mask ^ group] + 1
                if cand < best:
                    best = cand
            if sub == 0:
                break
            sub = (sub - 1) & rest
        dp[mask] = best
    return dp


def _reason_extremes(feasible, rmask, dp_size, full, reason_list) -> dict:
    """Min/max usage of each reason over all minimum-cardinality covers.

    For every mask we track, per reason, the smallest and largest count that a
    minimum cover of ``mask`` can use.  Only transitions that stay on a
    minimum-size cover are followed.
    """
    size = len(dp_size)
    n_reasons = len(reason_list)
    lo = [[0] * n_reasons for _ in range(size)]
    hi = [[0] * n_reasons for _ in range(size)]
    for mask in range(1, size):
        if dp_size[mask] >= _INF:
            continue
        low = mask & -mask
        rest = mask ^ low
        lo_row = [None] * n_reasons
        hi_row = [None] * n_reasons
        sub = rest
        while True:
            group = sub | low
            previous = mask ^ group
            if feasible[group] and dp_size[previous] + 1 == dp_size[mask]:
                p_lo = lo[previous]
                p_hi = hi[previous]
                gmask = rmask[group]
                for r in range(n_reasons):
                    bit = 1 << r
                    # min: this group is forced to r iff its common set is exactly {r}
                    c_lo = p_lo[r] + (1 if gmask == bit else 0)
                    if lo_row[r] is None or c_lo < lo_row[r]:
                        lo_row[r] = c_lo
                    # max: this group can be assigned r iff r is in its common set
                    c_hi = p_hi[r] + (1 if gmask & bit else 0)
                    if hi_row[r] is None or c_hi > hi_row[r]:
                        hi_row[r] = c_hi
            if sub == 0:
                break
            sub = (sub - 1) & rest
        lo[mask] = [0 if v is None else v for v in lo_row]
        hi[mask] = [0 if v is None else v for v in hi_row]
    out = {}
    for r, reason in enumerate(reason_list):
        out[reason] = {
            "min": lo[full][r],
            "max": hi[full][r],
            "possible": hi[full][r] >= 1,
        }
    return out


def _build_witness(notifs, feasible, dp_size, full) -> dict:
    groups = []
    mask = full
    while mask:
        low = mask & -mask
        rest = mask ^ low
        chosen = None
        sub = rest
        # Prefer a larger first group for a stable, readable witness.
        while True:
            group = sub | low
            if (
                feasible[group]
                and dp_size[mask ^ group] + 1 == dp_size[mask]
                and (chosen is None or group.bit_count() > chosen.bit_count())
            ):
                chosen = group
            if sub == 0:
                break
            sub = (sub - 1) & rest
        indices = [i for i in range(len(notifs)) if chosen >> i & 1]
        windows = [notifs[i].window for i in indices]
        lo = max(w[0] for w in windows)
        hi = min(w[1] for w in windows)
        common = set(notifs[indices[0]].reasons)
        for i in indices[1:]:
            common &= set(notifs[i].reasons)
        groups.append(
            {
                "reason": min(common),
                "common_reasons": sorted(common),
                "time_interval": [lo, hi],
                "time_witness": lo,
                "notification_ids": [notifs[i].notification_id for i in indices],
                "channels": sorted({notifs[i].channel for i in indices}),
            }
        )
        mask ^= chosen
    groups.sort(key=lambda g: g["notification_ids"])
    for number, group in enumerate(groups, start=1):
        group["stoppage_id"] = number
    return {"min_stoppages": len(groups), "groups": groups}
