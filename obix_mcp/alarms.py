"""alarms.py — what is in alarm right now, through the alarm service's own query op.

Read EVIDENCE.md §N before changing anything here. The shape of this is not
guessable from the oBIX specification, because the useful part is a Niagara
extension: the alarm service encodes itself as an `obix:AlarmSubject` carrying
an op and a feed, and every interesting detail was read out of
`com.tridium.obix.server.BAlarmServiceAgent`, `BAlarmServiceQuery` and
`BAlarmLobbyAgent`:

  * the `alarms` lobby child is a *ref*, not a folder you reach by that name.
    `BAlarmsLobbyAgent` extends `BShortcutLobbyAgent`, so its encodeLobbyChild
    sets the href to `makeSlotPathUri(encoder, getComponent().toPathString())`
    — `getComponent()` is `Sys.getService(BAlarmService.TYPE)`, and the uri is
    `concat(concat(lobbyPath, "config"), "/Services/AlarmService")`, i.e.
    `/obix/config/Services/AlarmService`. That advertised href is the alarm
    service. A bare GET of `/obix/alarms/` by *name* does not reach it: the
    shortcut's `resolve("")` builds `station:|slot:`, which is the station root.
    This module follows the ref's href from the lobby, never the name, so it
    lands on the service either way. The service's own children are alarm
    classes, not alarm records, so walking it finds no alarms.
    `BAlarmServiceAgent.encodeFinishing` is what adds the parts that matter: an
    `<int name="count">` of the open alarms, an op named `query` (in
    `obix:AlarmFilter`, out `obix:AlarmQueryOut`) and a feed named `feed`. Their
    hrefs come from `encoder.getChildHref`, and the names the station builds them
    from are `~alarmQuery` and `~alarmFeed`.
  * the op is where the records are. `BAlarmServiceQuery.invoke` reads three
    children out of the filter by name — `limit`, `start`, `end` — and turns
    them into a BQL query over the alarm database: `alarm:|bql:select * from
    openAlarms`, with `where alarmClass = '...'` when the op belongs to one
    alarm class and `lastUpdate` bounds when the filter carried times.
  * the reply is `<obj is="obix:AlarmQueryOut">` and the records arrive first,
    as `<list name="data" of="obix:Alarm">`. `count`, `start` and `end` are
    written *after* the list, because the station streams the cursor and only
    then knows them. Nothing here may depend on child order.
  * each record is an `obj` whose `is` names several contracts at once —
    `obix:Alarm obix:AckAlarm`, plus `obix:PointAlarm` or `obix:StatefulAlarm`.
    Its fields are the names in ALARM_FIELDS, and it carries a `niagara-uuid`,
    which is the only handle by which one alarm can be named again later.
  * `BAlarmLobbyAgent` (the singular `alarm`, not `alarms`) is one of five lobby
    agents whose `encodeLobbyChild` is a single `return`: it writes no element.
    So `/obix/alarm/<uuid>` resolves while appearing in no lobby listing — a
    client that only walks what it is shown cannot find it. The other four
    unlisted branches are `bql`, `def`, `ord` and `units`.

This module reads. The alarm records the station sends advertise an `ack` op,
and an ack is an invoke that writes to the alarm database — it sets the record's
user, ack time and ack state, and with a `forceCleared` child it also drives the
source state to normal and fires the service's `auditForceClear` action. That
travels as a POST to the record, not as a PUT to a point, so neither write gate
in obix.py would see it; the same hole the batch op could have opened. The
answer is the same as it is there. There is no ack in this module: no contract
naming one, no code assembling a record's ack href, and no tool that could carry
an input to it. test_alarms.py walks every string constant in this file and fails
on any that mentions ack outside the two field names a record reports, so adding
one is a failing suite rather than a quietly wider reach.
"""
from __future__ import annotations

from .obix import OBIX_NS, ObixClient, ObixError, ObixObject

# BAlarmsLobbyAgent.getLobbyName() returns "alarms". The op href still comes
# from the object the station served, never from this string.  EVIDENCE.md §N
ALARMS_LOBBY_NAME = "alarms"

# The names BAlarmServiceAgent.encodeFinishing gives the op and the feed it
# advertises on the alarm service. The hrefs it builds them from are
# ~alarmQuery and ~alarmFeed; this looks the op up by name so that a station
# which numbers its hrefs differently still works.  EVIDENCE.md §N
QUERY_OP_NAME = "query"
FEED_NAME = "feed"

# The in and out contracts of that op.
FILTER_CONTRACT = "obix:AlarmFilter"
QUERY_OUT_CONTRACT = "obix:AlarmQueryOut"

# The `<int name="count">` encodeFinishing writes on the alarm service: the
# number of open alarms, counted from AlarmDbConnection.getOpenAlarms().
COUNT_FIELD = "count"

# The list inside an AlarmQueryOut, and the contract its members carry.
DATA_FIELD = "data"
ALARM_CONTRACT = "obix:Alarm"

# Every field name BAlarmServiceAgent writes on a record, in the order an
# operator wants to read them rather than the order the station writes them.
# A field the station omits is left out of the row instead of being reported
# as empty, because "no ack time" and "ack time is blank" are different claims.
ALARM_FIELDS = (
    "source", "sourceStation", "msgText", "alarmClass", "priority",
    "alarmValue", "timestamp", "normalTimestamp", "ackTimestamp", "ackUser",
    "originalSource", "originalAlarmClass", "originalPriority",
)

# The record's Niagara UUID. It is the handle the alarm branch resolves by, and
# it is the one field of a record that is not a description of the alarm.
UUID_FIELD = "niagara-uuid"

# A query the station answers by streaming a cursor over its alarm database
# into one reply document. Tridium compiles in no ceiling (EVIDENCE.md §J), so
# this one is here to keep a controller's reply small. The station applies it:
# `limit` goes into the filter rather than being trimmed off the answer here,
# which is the difference between a small reply and a large one truncated.
MAX_ALARM_LIMIT = 200
DEFAULT_ALARM_LIMIT = 50


def filter_in(start: str = "", end: str = "", limit: int = 0) -> bytes:
    """An AlarmFilter document, carrying only the bounds the caller gave.

    `BAlarmServiceQuery.invoke` reads each child by name and tolerates a
    missing one, so an empty filter is a valid request meaning "every open
    alarm". Sending `start`/`end` as empty elements would not be: they become
    `lastUpdate` bounds in a BQL string.
    """
    if limit and limit < 0:
        raise ObixError("a limit is a count of records, so it cannot be negative")
    if limit > MAX_ALARM_LIMIT:
        raise ObixError(
            f"a limit of {limit} is past this bridge's ceiling of {MAX_ALARM_LIMIT}. "
            f"The station has no limit of its own, so this one is here to keep a "
            f"controller's reply small; narrow the window with start and end instead.")
    parts = []
    if limit:
        parts.append(f'<int name="limit" val="{int(limit)}"/>')
    for name, when in (("start", start), ("end", end)):
        if when:
            parts.append(f'<abstime name="{name}" val="{_attr(when)}"/>')
    return (f'<obj xmlns="{OBIX_NS}" is="{FILTER_CONTRACT}">'
            f'{"".join(parts)}</obj>').encode("utf-8")


def _attr(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def alarms_object(client: ObixClient) -> ObixObject:
    """The alarm service as the station encodes it, or a reason it is absent.

    An installation can run the oBIX server with no alarm service, and a lobby
    with no `alarms` branch is that, not a fault. Saying so is more use than a
    404 from a URL this tool assembled.
    """
    lobby = client.lobby()
    child = lobby.child(ALARMS_LOBBY_NAME)
    if child is None or not child.href:
        names = ", ".join(c.name for c in lobby.children if c.name) or "(nothing)"
        raise ObixError(
            f"this station's lobby has no {ALARMS_LOBBY_NAME!r} branch, so it exposes no "
            f"alarms over oBIX. The lobby offers: {names}")
    return client.read(child.href)


def query_href(subject: ObixObject) -> str:
    """The href of the query op on an alarm subject — the service, or one class.

    Looked up by name rather than assembled from `~alarmQuery`, because the
    station builds that href through `encoder.getChildHref` and a client has no
    business reproducing an encoder's escaping.
    """
    op = subject.child(QUERY_OP_NAME)
    if op is None or not op.href:
        offered = ", ".join(c.name for c in subject.children if c.name) or "(nothing)"
        raise ObixError(
            f"this alarm subject advertises no {QUERY_OP_NAME!r} op, so its alarms cannot "
            f"be listed. It offers: {offered}")
    return op.href


def open_count(subject: ObixObject) -> int | None:
    """The count the station wrote on the alarm service, if it wrote one."""
    child = subject.child(COUNT_FIELD)
    if child is None:
        return None
    value = child.value()
    return value if isinstance(value, int) else None


def alarm_rows(out: ObixObject) -> list[dict]:
    """One row per record in an AlarmQueryOut.

    The records live in a `<list name="data">`. A station that returned them as
    direct children instead is read too: the list is found by name where it
    exists and fallen back to by contract, and neither is assumed to come before
    the count.
    """
    data = out.child(DATA_FIELD)
    if data is None:
        data = next((c for c in out.children
                     if c.tag == "list" and ALARM_CONTRACT in (c.contract or "")), None)
    members = data.children if data is not None else [
        c for c in out.children if ALARM_CONTRACT in (c.contract or "")]
    rows = []
    for rec in members:
        row: dict = {}
        if rec.href:
            row["href"] = rec.href
        uuid = rec.child(UUID_FIELD)
        if uuid is not None and uuid.val:
            row["uuid"] = uuid.val
        for name in ALARM_FIELDS:
            child = rec.child(name)
            if child is not None and child.val not in (None, ""):
                row[name] = child.value()
        if rec.contract:
            row["contracts"] = rec.contract.split()
        rows.append(row)
    return rows


def query_bounds(out: ObixObject) -> dict:
    """The count, start and end the station wrote after the list.

    These are the *answer's* bounds, not the request's: `iterateResults` reports
    the first and last record it actually streamed.
    """
    meta: dict = {}
    for name in (COUNT_FIELD, "start", "end"):
        child = out.child(name)
        if child is not None and child.val not in (None, ""):
            meta[name] = child.value()
    return meta


def open_alarms(client: ObixClient, start: str = "", end: str = "",
                limit: int = 0, scope: str = "", op: str = "") -> dict:
    """Every open alarm the station will report, in one POST to its query op.

    `scope` is the href of an alarm subject to ask instead of the service — an
    alarm class, which `BAlarmClassAgent` gives the same op — so a caller can
    narrow to one class without filtering the answer afterwards.

    `op` skips the two reads that find the op, for a caller that already holds
    the href. That costs the station's own open-alarm count, which is written on
    the service and not in the reply, so the count is left out rather than
    carried over from an earlier call and presented as current.
    """
    # The request document is built first so that a limit past the cap is
    # refused before this touches the station at all. A bridge that discovers
    # the op, then refuses, has already spent two reads to say no.
    body = filter_in(start=start, end=end, limit=limit or DEFAULT_ALARM_LIMIT)
    subject = None
    if not op:
        subject = client.read(scope) if scope else alarms_object(client)
        op = query_href(subject)
    out = client.invoke(op, body)
    result = {"op": op, "rows": alarm_rows(out)}
    result.update(query_bounds(out))
    count = open_count(subject) if subject is not None else None
    if count is not None:
        result["open_alarms_on_station"] = count
    if not out.contract:
        # The station does set is="obix:AlarmQueryOut" here, unlike the batch
        # reply. Saying so when it is missing is worth more than failing on it.
        result["note"] = "the reply carried no is attribute, so its contract is unconfirmed"
    return result
