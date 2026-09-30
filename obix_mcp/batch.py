"""batch.py — one POST that reads many points, instead of one GET each.

Read EVIDENCE.md §M before changing anything here. Every line of the wire
format below was read out of `com.tridium.obix.server.BBatchOp.invoke` and the
encoder it writes through, because Tridium's shipped documentation contains no
literal batch request or reply document anywhere:

  * the *request document element must be named `list`*. `invoke` takes
    `decoder.getDocument()` and throws IllegalArgumentException("Expecting list
    element but encountered " + name) for anything else. A WatchIn wraps its
    list in an `<obj>`; a BatchIn must not.
  * each child must be named `uri`, or the station throws
    Exception("Unexpected batch element: " + name) for that item.
  * each child needs a `val`, or the item comes back as a fault row saying
    "Missing val attribute".
  * `val` is resolved by `ObixUtils.resource(encoder.getLobbyPath(), val)`,
    which is `val.indexOf(lobbyPath)` and throws BadUriErr when that is -1. So
    *the val has to be a full path including /obix* — the relative hrefs that
    work in a WatchIn are rejected here. This is the one difference between the
    two request bodies that costs an afternoon.
  * the item's verb is its `is` attribute, matched with `contains`: obix:Read
    encodes the resolved target, obix:Write services a write, obix:Invoke an
    invoke. `is` defaults to the empty string, so an item without one is
    answered with Exception("Unknown batch contract: " + elem) — the attribute
    is not optional in practice.
  * the reply is `Obj().initList(null, "obix:BatchOut")`, and initList is
    setName/setOf, so it arrives as `<list of="obix:BatchOut">` with **no `is`
    attribute and no name**. A client that looks for is="obix:BatchOut" finds
    nothing.
  * before each item the station calls `encoder.setHref(val)`, so every row —
    reading or fault — carries back the val that was sent. Rows are matched by
    href rather than by position, which is what makes a partial failure
    readable.
  * a failed item is `encoder.encode(Throwable)`: an `<err href="..."
    display="..."/>`, with an `is` only for the exception types the encoder maps
    (BadUriErr, PermissionErr, UnsupportedErr). A plain failure carries no
    contract at all, so nothing here depends on one.

This module builds read items and nothing else. The station's batch op also
services writes and invokes — that is in the bytecode above — and a batch write
would sidestep both write gates in obix.py, because it travels as a POST to the
batch op rather than as a PUT to a point. The answer is not a check inside the
batch path: it is that no code here can express one. There is no verb argument,
no passthrough of a caller's `is`, and the only contract string outside this
docstring is the read one. test_batch.py asserts exactly that, with the
docstring excluded, so an edit that adds a second contract fails the suite
rather than quietly widening what an agent can reach.
"""
from __future__ import annotations

import urllib.parse

from .obix import OBIX_NS, ObixClient, ObixError, ObixObject, _xml_attr

# The lobby name BBatchOp.getLobbyName() returns. The href still comes from the
# lobby the station served, not from this string.  EVIDENCE.md §M
BATCH_LOBBY_NAME = "batch"

# The only item contract this module knows how to write.
READ_CONTRACT = "obix:Read"

# A no-argument BadUriErr has a null message, so ObixEncoder.encode(Throwable)
# falls back to Throwable.toString() and the `display` an operator sees is a Java
# class name. Translating the ones the batch path can produce is worth more than
# passing them through.  EVIDENCE.md §M
_BARE_FAULTS = {
    "com.tridium.obix.util.BadUriErr":
        "the station could not resolve that href. In a batch the val must be a "
        "full path beginning /obix — a relative href works in a watch and not here.",
}

# One POST, one reply, all of it in the controller's memory at once. Tridium
# compiles in no ceiling on batch size (EVIDENCE.md §J), which is a reason to
# pick one here: 50 objects is a useful saving over 50 round trips and still a
# reply a JACE can build without thinking about it.
MAX_BATCH_ITEMS = 50


def batch_in(paths: list[str]) -> bytes:
    """A BatchIn document: `<list>` of `<uri is="obix:Read" val="/obix/..."/>`.

    `paths` are full servlet paths, not relative hrefs — see the module
    docstring for why the station insists.
    """
    if not paths:
        raise ObixError("a batch needs at least one href")
    items = "".join(f'<uri is="{READ_CONTRACT}" val="{_xml_attr(p)}"/>' for p in paths)
    return (f'<list xmlns="{OBIX_NS}" is="obix:BatchIn">{items}</list>').encode("utf-8")


def batch_out_rows(out: ObixObject) -> list[dict]:
    """Flatten a BatchOut into one row per item, faults included.

    The station's own reply is a bare `<list of="obix:BatchOut">` whose children
    are the rows. A reply that wraps the list in an `<obj>` — which another oBIX
    server may well do, and which is what a WatchOut looks like — is read too,
    because refusing it would turn a cosmetic difference into no data at all.
    """
    items = out.children
    if out.tag != "list":
        lst = next((c for c in out.children if c.tag == "list"), None)
        items = lst.children if lst is not None else out.children
    rows = []
    for item in items:
        row = {"href": item.href, "tag": item.tag}
        if item.tag == "err":
            # No `is` on a generic failure, so the display is the whole answer.
            row["error"] = item.display or item.display_name or "the station gave no detail"
            hint = _BARE_FAULTS.get(row["error"].strip())
            if hint:
                row["hint"] = hint
            if item.contract:
                row["contract"] = item.contract
        else:
            row["value"] = item.value()
            for key in ("status", "unit", "display"):
                if getattr(item, key):
                    row[key] = getattr(item, key)
            if item.contract:
                row["contract"] = item.contract
        rows.append(row)
    return rows


def batch_href(client: ObixClient) -> str:
    """The batch op's href, taken from the lobby the station served.

    Assembling /obix/batch would work on a stock station and hide the case that
    matters: a lobby without the batch agent. Then the honest answer is that
    this station has no batch op, not a 404 from a URL this tool invented.
    """
    lobby = client.lobby()
    child = lobby.child(BATCH_LOBBY_NAME)
    if child is None or not child.href:
        names = ", ".join(c.name for c in lobby.children if c.name) or "(nothing)"
        raise ObixError(
            f"this station's lobby has no {BATCH_LOBBY_NAME!r} op, so it cannot read a "
            f"batch. Read the points one at a time instead. The lobby offers: {names}")
    return child.href


def servlet_paths(client: ObixClient, hrefs: list[str]) -> list[str]:
    """Turn hrefs into the full paths a BatchIn needs, through the client's own
    resolver — so a href that points off-station is refused here, exactly as it
    would be on a read."""
    return [urllib.parse.urlsplit(client.url_for(h)).path for h in hrefs]


def _same_object(row_href: str, path: str, href: str) -> bool:
    """Whether a reply row is about the href that was asked for.

    The station sets the row's href to the val that was sent, so on a stock
    station this is string equality. It is written as a suffix comparison because
    that claim has not been checked against a real JACE, and a station that
    trimmed the lobby path off its rows would otherwise make every reading look
    unanswered while also returning it.
    """
    a = (row_href or "").strip("/")
    for want in (path, href):
        b = (want or "").strip("/")
        if a and b and (a == b or a.endswith("/" + b) or b.endswith("/" + a)):
            return True
    return False


def read_many(client: ObixClient, hrefs: list[str], op: str = "") -> dict:
    """Read every href in one POST to the station's batch op.

    `op` skips the lobby read when a caller already holds the href — worth one
    fewer request on a controller, and safe because the href still came from the
    station in the first place.

    Returns the rows and, separately, the hrefs the station answered nothing
    about — a station is not obliged to return a row per item, and silently
    dropping the difference is how a caller comes to believe a point was read.
    """
    if not hrefs:
        raise ObixError("a batch needs at least one href")
    if len(hrefs) > MAX_BATCH_ITEMS:
        raise ObixError(
            f"{len(hrefs)} hrefs is past this bridge's batch limit of {MAX_BATCH_ITEMS}. "
            f"The station compiles in no limit of its own, so this one is here to keep a "
            f"controller's reply small; split the read.")
    paths = servlet_paths(client, hrefs)
    op = op or batch_href(client)
    out = client.invoke(op, batch_in(paths))
    rows = batch_out_rows(out)
    unanswered = [h for h, p in zip(hrefs, paths)
                  if not any(_same_object(r.get("href", ""), p, h) for r in rows)]
    result = {"op": op, "requested": len(paths), "rows": rows}
    if unanswered:
        result["no_row_returned"] = unanswered
    return result
