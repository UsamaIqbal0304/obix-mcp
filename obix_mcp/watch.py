"""watch.py — oBIX watches, which is how you follow changing values without polling every point.

Read EVIDENCE.md §F before changing anything here. The operation *names* and
their input/output contracts are evidenced from the shipped jars:
`BObixWatchService.make` produces an `obix:Watch`, and a watch carries `add`
(WatchIn -> WatchOut), `remove` (WatchIn -> Nil), `pollChanges` (Nil ->
WatchOut), `pollRefresh` (Nil -> WatchOut) and `delete` (Nil -> Nil), each as a
`BObixOp` default value in the bytecode.

Tridium's shipped oBIX guide contains no literal request or response document
anywhere — that was checked, not assumed — so the bodies here were taken from
the station's own decoder and encoder instead, which is better evidence than a
manual anyway:

  * `BObixWatch.getHrefs(ObixDecoder)` is `document.elem("list").elems()`, and
    `doAdd` then reads attribute `val` off each child, skipping any child that
    has none. So a WatchIn needs one element named `list` whose children each
    carry the href in `val`. The list's `name` attribute and the children's
    element names are never read by this station.
  * `doAdd`, `doPollChanges` and `doPollRefresh` all build the reply as
    `Obj().setIs("obix:WatchOut")` then `initList("values", "obix:obj")`, so
    the readings come back inside `<list name="values" of="obix:obj">`.
  * when one href fails to resolve, the loop calls `encoder.setHref(href)` and
    `encoder.encode(Throwable)` and carries on to the next one. One bad href
    costing the caller nothing but that row is the station's behaviour, not
    this module being generous.

`--dump` on the CLI prints what went out and came back, so the day a station
disagrees the disagreement is visible rather than mysterious.
"""
from __future__ import annotations

import time

from .obix import (DEFAULT_LEASE_SECONDS, MIN_POLL_SECONDS, OBIX_NS, ObixClient,
                   ObixError, ObixObject, _xml_attr, decode)


def _watch_in(hrefs: list[str]) -> bytes:
    """An obix:WatchIn: one `list` element whose children carry hrefs in `val`.

    `name="hrefs"` and the `uri` element name are the specification's, and this
    station reads neither — see BObixWatch.getHrefs in EVIDENCE.md §F. They are
    written anyway, because a different oBIX server may well read them."""
    items = "".join(f'<uri val="{_xml_attr(h)}"/>' for h in hrefs)
    return (f'<obj xmlns="{OBIX_NS}" is="obix:WatchIn">'
            f'<list name="hrefs">{items}</list></obj>').encode("utf-8")


class Watch:
    """One server-side watch, with its op hrefs taken from the station's reply.

    The op hrefs are read out of the Watch object the station returned rather
        than assembled from a template, because that is the one part of this the
    station is authoritative about. Assembling them would work right up until a
    station laid them out differently.
    """

    def __init__(self, client: ObixClient, obj: ObixObject):
        self.client = client
        self.href = obj.href
        if not self.href:
            raise ObixError("the station's make returned a watch with no href")
        self.ops = {}
        for name in ("add", "remove", "pollChanges", "pollRefresh", "delete"):
            child = obj.child(name)
            if child is not None and child.href:
                self.ops[name] = child.href
        missing = [n for n in ("add", "pollChanges", "delete") if n not in self.ops]
        if missing:
            raise ObixError(
                f"the watch at {self.href} does not expose {', '.join(missing)}. "
                f"It carries: {', '.join(sorted(self.ops)) or '(nothing)'}")
        lease = obj.child("lease")
        self.lease_seconds = _reltime_seconds(lease.val if lease else None) or DEFAULT_LEASE_SECONDS
        self.hrefs: list[str] = []
        self._last_poll = 0.0

    def _op(self, name: str, body: bytes = b"") -> ObixObject:
        if name not in self.ops:
            raise ObixError(f"this watch has no {name} op")
        out = self.client.invoke(self.ops[name], body)
        self._last_poll = time.monotonic()
        return out

    def add(self, hrefs: list[str]) -> ObixObject:
        """Register hrefs. The reply is the first reading of each, so it is
        returned rather than discarded — an add that is followed by an
        immediate pollChanges is two round trips for one answer."""
        if not hrefs:
            raise ObixError("add needs at least one href")
        out = self._op("add", _watch_in(hrefs))
        for h in hrefs:
            if h not in self.hrefs:
                self.hrefs.append(h)
        return out

    def remove(self, hrefs: list[str]) -> ObixObject:
        out = self._op("remove", _watch_in(hrefs))
        self.hrefs = [h for h in self.hrefs if h not in hrefs]
        return out

    def poll_changes(self) -> ObixObject:
        return self._op("pollChanges")

    def poll_refresh(self) -> ObixObject:
        """Every registered value, not only what changed. Costs the station more,
        so it is a separate call rather than a flag."""
        return self._op("pollRefresh")

    def delete(self) -> None:
        try:
            self._op("delete")
        finally:
            self.ops.clear()
            self.hrefs.clear()

    @property
    def seconds_until_expiry(self) -> float:
        """How long the station will keep this watch without another poll.

        A watch is leased, not owned: BObixWatchService.defaultLeaseTime is 30
        seconds, so an agent that thinks for a minute between polls comes back
        to a watch that is gone. The MCP layer uses this to renew rather than
        to explain the failure afterwards."""
        if not self._last_poll:
            return self.lease_seconds
        return max(0.0, self.lease_seconds - (time.monotonic() - self._last_poll))


def make_watch(client: ObixClient) -> Watch:
    """POST watchService/make. The op href comes from the lobby, not a template."""
    service = client.read("watchService/")
    make = service.child("make")
    href = make.href if make is not None and make.href else "watchService/make"
    return Watch(client, client.invoke(href))


def _reltime_seconds(val: str | None) -> float | None:
    """Parse the xs:duration oBIX uses for reltime, e.g. PT30S, PT1M30S.

    Only the fields a lease can plausibly use are handled; a duration in months
    is not a lease, and silently reading one as seconds would be worse than
    declining to."""
    if not val:
        return None
    s = val.strip()
    neg = s.startswith("-")
    if neg:
        s = s[1:]
    if not s.startswith("P"):
        return None
    if "Y" in s or "M" in s.split("T")[0]:
        return None
    total, num, fields = 0.0, "", 0
    date_part, _, time_part = s[1:].partition("T")
    for ch in date_part:
        if ch.isdigit() or ch == ".":
            num += ch
        elif ch == "D" and num:
            total += float(num) * 86400
            num, fields = "", fields + 1
        else:
            return None
    for ch in time_part:
        if ch.isdigit() or ch == ".":
            num += ch
        elif num:
            mult = {"H": 3600, "M": 60, "S": 1}.get(ch)
            if mult is None:
                return None
            total += float(num) * mult
            num, fields = "", fields + 1
        else:
            return None
    if num or not fields:
        # "PT" names no duration and "PT5" no unit for it. Reading either as
        # zero seconds would expire every watch on the spot.
        return None
    return -total if neg else total


def changes_to_rows(out: ObixObject) -> list[dict]:
    """Flatten a WatchOut into one row per point.

    The station encodes a WatchOut as a list named "values" of "obix:obj"
    (BObixWatch.doAdd, EVIDENCE.md §F). The bare form — children directly on the
    WatchOut — is accepted too, because another oBIX server that omits the
    wrapper should still produce readings rather than an empty list and a
    puzzled agent.
    """
    values = out.child("values")
    items = values.children if values is not None else [
        c for c in out.children if c.tag != "list" or c.name != "hrefs"]
    rows = []
    for item in items:
        rows.append({
            "href": item.href,
            "tag": item.tag,
            "value": item.value(),
            **({"status": item.status} if item.status else {}),
            **({"unit": item.unit} if item.unit else {}),
            **({"display": item.display} if item.display else {}),
        })
    return rows


def safe_interval(requested: float | None) -> float:
    """Clamp a poll interval to Tridium's own floor for a controller.

    Their driver defaults to 2 seconds and their documentation says to increase
    it to 10 or more to reduce load on the platform. An agent loop will poll as
    fast as it is allowed to, so the floor is enforced here rather than
    documented and hoped for."""
    if requested is None:
        return MIN_POLL_SECONDS
    return max(float(requested), MIN_POLL_SECONDS)
