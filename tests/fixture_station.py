"""fixture_station.py — a small oBIX server that behaves the way the evidence says a station does.

This is not a Niagara station and does not pretend to be one. It exists so the
client can be tested against something, and its behaviour is derived from
EVIDENCE.md rather than from the oBIX specification wherever the two could
differ:

  * the tree is served under /obix and nowhere else;
  * GET reads, PUT writes, POST invokes — the station's own verb split;
  * the lobby's export branch is named continuousControl;
  * a watch is leased for 30 seconds and its ops are add, remove, pollChanges,
    pollRefresh and delete;
  * the batch branch is an op, a BatchIn's document element is `list` and its
    items are `uri` elements whose `val` must carry the lobby path, one bad item
    is one fault row among the readings, and the reply is a `<list
    of="obix:BatchOut">` with no `is` — and batch *writes* are serviced here,
    because the station services them and a fixture that could not write would
    make the bridge's read-only batch look proved when it was not;
  * with --mode disabled every request is 410 "oBIX Server Disabled", and with
    --mode unlicensed every request is 403 "Unlicensed oBIX Server", which are
    the two codes and the two literal strings the station's own servlet sends.

Where the evidence is silent — the XML body of a WatchIn, for instance — this
fixture uses the specification's shape and the test that depends on it says so.
A test passing here is evidence the client is self-consistent, not evidence the
station agrees. The README says the same thing in fewer words.
"""
from __future__ import annotations

import base64
import http.server
import json
import re
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET

NS = 'xmlns="http://obix.org/ns/schema/1.0"'

# The station builds the canned history queries off its own clock. Frozen here.
HISTORY_NOW = "2026-09-30T12:00:00.000Z"
HISTORY_START = "2026-09-29T12:00:00.000Z"

# The eleven refs BObixHistoryAgent.encodeFinishing writes, in its own order and
# with its own names — `last24Hours`, not "Last 24 Hours", and two names that
# carry a space and brackets. The hrefs are relative and already carry the
# bounds as a query string.  EVIDENCE.md §N
CANNED_HISTORY_QUERIES = (
    ("unboundedQuery", "~historyQuery?limit=1000"),
    ("today", f"~historyQuery?start=2026-09-30T00:00:00.000Z&end={HISTORY_NOW}"),
    ("last24Hours", f"~historyQuery?start={HISTORY_START}&end={HISTORY_NOW}"),
    ("yesterday", "~historyQuery?start=2026-09-29T00:00:00.000Z"
                  "&end=2026-09-29T23:59:59.999Z"),
    ("weekToDate", f"~historyQuery?start=2026-09-28T00:00:00.000Z&end={HISTORY_NOW}"),
    ("lastWeek", "~historyQuery?start=2026-09-21T00:00:00.000Z"
                 "&end=2026-09-27T23:59:59.999Z"),
    ("last7Days", f"~historyQuery?start=2026-09-23T12:00:00.000Z&end={HISTORY_NOW}"),
    ("monthToDate", f"~historyQuery?start=2026-09-01T00:00:00.000Z&end={HISTORY_NOW}"),
    ("lastMonth", "~historyQuery?start=2026-08-01T00:00:00.000Z"
                  "&end=2026-08-31T23:59:59.999Z"),
    ("yearToDate (limit=1000)",
     f"~historyQuery?start=2026-01-01T00:00:00.000Z&end={HISTORY_NOW}&limit=1000"),
    ("lastYear (limit=1000)",
     "~historyQuery?start=2025-01-01T00:00:00.000Z"
     "&end=2025-12-31T23:59:59.999Z&limit=1000"),
)


def _watch_in_hrefs(body):
    """Decode a WatchIn exactly the way BObixWatch does it.

    getHrefs is `document.elem("list").elems()` and doAdd then reads attribute
    `val` off each child, skipping children that have none. So the list's name
    and the children's element names are ignored here too: a fixture that was
    fussier than the station would pass requests the station rejects, and
    reject requests the station accepts.
    """
    doc = ET.fromstring(body.decode("utf-8"))
    lst = next((e for e in doc.iter() if e.tag.rsplit("}", 1)[-1] == "list"), None)
    if lst is None:
        return []
    hrefs = [e.attrib["val"] for e in list(lst) if "val" in e.attrib]
    return [h[len("/obix/"):] if h.startswith("/obix/") else h for h in hrefs]


def _xa(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


class AckInputError(Exception):
    """What an ack input with no ackUser child produces: a throw from inside
    invoke, which the servlet turns into one err document and a 200."""


class BatchDocError(Exception):
    """A BatchIn whose document element is not `list`. The station throws before
    it has written anything, so the reply is one err document."""


class Station:
    """The object tree, and the watches held against it."""

    def __init__(self):
        # When True, about_xml mimics a stock Niagara station: the frozen
        # "Niagara AX" product name and the two Tridium extension fields. Default
        # off so the fixture stays a generic (non-Tridium) oBIX server.
        self.niagara_about = False
        # href -> (tag, value, unit, writable)
        self.points = {
            "config/AHU-01/SupplyAirTemp/": ["real", 13.4, "obix:units/celsius", False],
            "config/AHU-01/ReturnAirTemp/": ["real", 21.9, "obix:units/celsius", False],
            "config/AHU-01/FanCommand/": ["bool", True, "", True],
            "config/AHU-01/ZoneSetpoint/": ["real", 21.0, "obix:units/celsius", True],
            "config/AHU-01/Mode/": ["str", "occupied", "", True],
        }
        self.watches = {}
        self._n = 0
        self.writes = []
        # Open alarms, as BAlarmServiceAgent encodes them: several contracts in
        # one `is`, a niagara-uuid as the only handle, and an ack op per record.
        self.alarms = [
            {
                "uuid": "5f2e6d3c-9a41-4b77-8c10-0a1b2c3d4e5f",
                "is": "obix:Alarm obix:AckAlarm obix:PointAlarm",
                "source": "station:|slot:/Drivers/AHU-01/SupplyAirTemp",
                "sourceStation": "AHU-01",
                "msgText": "Supply air temperature above limit",
                "alarmClass": "DefaultAlarmClass",
                "priority": 255,
                "alarmValue": "28.4 \u00b0C",
                "timestamp": "2026-09-30T09:14:02.100Z",
                "acked": False,
            },
            {
                "uuid": "7c9b0a15-2d3e-4f60-b871-99aa88bb77cc",
                "is": "obix:Alarm obix:AckAlarm obix:StatefulAlarm",
                "source": "station:|slot:/Drivers/AHU-01/FanCommand",
                "sourceStation": "AHU-01",
                "msgText": "Fan failed to start",
                "alarmClass": "CriticalAlarmClass",
                "priority": 1,
                "timestamp": "2026-09-30T09:20:41.000Z",
                "normalTimestamp": "2026-09-30T09:31:00.000Z",
                "acked": True,
                "ackUser": "someone else",
                "ackTimestamp": "2026-09-30T09:33:12.000Z",
            },
        ]
        self.acks = []
        self.force_clears = []
        # Like the ack, the history append is implemented rather than stubbed
        # out: a test that proves the bridge cannot append is only worth
        # something against a station that would have accepted one.
        self.history_appends = []

    def next_watch(self):
        self._n += 1
        wid = f"watch{self._n}"
        self.watches[wid] = {"hrefs": [], "seen": {}, "made": time.monotonic()}
        return wid

    def lobby_xml(self):
        kids = ""
        # Seven elements from twelve agents. The other five — alarm, bql, def,
        # ord and units — have an encodeLobbyChild whose whole body is `return`:
        # they write nothing, so each is resolvable and unlisted. Putting them in
        # the list here hid that for two sessions.
        for name in ("about", "alarms", "batch", "config",
                     "continuousControl", "histories", "watchService"):
            if name == "alarms":
                # BAlarmsLobbyAgent extends BShortcutLobbyAgent, so its
                # encodeLobbyChild sets the href to
                # makeSlotPathUri(encoder, AlarmService.toPathString()) =
                # concat(concat(lobbyPath, "config"), "/Services/AlarmService")
                # — the alarm service reached under the `config` (station)
                # branch, NOT a bare "alarms/". A GET of "alarms/" by name would
                # resolve the shortcut with an empty remainder to
                # `station:|slot:`, the station root; the useful object is only
                # at the advertised href, and this is that href.
                kids += ('<ref name="alarms" href="config/Services/AlarmService/" '
                         'is="obix:AlarmSubject"/>')
                continue
            if name == "batch":
                # BBatchOp.encodeLobbyChild is initOp("batch", "obix:BatchIn",
                # "obix:BatchOut") with the href the encoder is already holding,
                # so the batch branch arrives as an op and not as a ref to a
                # folder. The client takes this href rather than building one.
                kids += ('<op name="batch" href="batch" in="obix:BatchIn" '
                         'out="obix:BatchOut"/>')
                continue
            is_ = ' is="obix:WatchService"' if name == "watchService" else ""
            kids += f'<ref name="{name}" href="{name}/"{is_}/>'
        return f'<obj {NS} href="/obix/" is="obix:Lobby">{kids}</obj>'

    def about_xml(self):
        if self.niagara_about:
            # A stock Niagara 4 station: the frozen AX brand and the two Tridium
            # extension fields that sit outside the oBIX About contract.
            return (f'<obj {NS} href="/obix/about/" is="obix:About">'
                    f'<str name="obixVersion" val="1.0"/>'
                    f'<str name="serverName" val="jace"/>'
                    f'<str name="vendorName" val="Tridium, Inc."/>'
                    f'<str name="productName" val="Niagara AX"/>'
                    f'<str name="productVersion" val="4.14.0.162"/>'
                    f'<str name="tz" val="America/New_York"/>'
                    f'<str name="componentCount" val="4213"/>'
                    f'<str name="localHistoryCount" val="87"/>'
                    f'<abstime name="serverTime" val="2026-09-30T12:00:00Z"/></obj>')
        return (f'<obj {NS} href="/obix/about/" is="obix:About">'
                f'<str name="obixVersion" val="1.0"/>'
                f'<str name="serverName" val="fixture"/>'
                f'<str name="vendorName" val="Not Tridium"/>'
                f'<str name="productName" val="oBIX fixture station"/>'
                f'<str name="productVersion" val="0"/>'
                f'<abstime name="serverTime" val="2026-09-30T12:00:00Z"/></obj>')

    def point_xml(self, href):
        tag, val, unit, writable = self.points[href]
        v = "true" if val is True else "false" if val is False else val
        u = f' unit="{unit}"' if unit else ""
        w = ' writable="true"' if writable else ""
        return (f'<{tag} {NS} href="/obix/{href}" val="{_xa(v)}"{u}{w} '
                f'is="obix:Point" status="ok"/>')

    # A station answers a missing object with a fault. Serving an empty folder
    # for any path that happens to end in a slash hid that.
    def is_folder(self, prefix):
        return any(h.startswith(prefix) for h in self.points)

    def folder_xml(self, prefix):
        kids = ""
        for href in self.points:
            if href.startswith(prefix) and href != prefix:
                rest = href[len(prefix):].rstrip("/")
                if "/" not in rest:
                    kids += f'<ref name="{rest}" href="{rest}/" is="obix:Point"/>'
        if not kids and prefix == "config/":
            kids = '<ref name="AHU-01" href="AHU-01/"/>'
        return f'<obj {NS} href="/obix/{prefix}">{kids}</obj>'

    def watch_service_xml(self):
        return (f'<obj {NS} href="/obix/watchService/" is="obix:WatchService">'
                f'<op name="make" href="make/" '
                f'in="obix:Nil" out="obix:Watch"/></obj>')

    def watch_xml(self, wid):
        h = f"/obix/watchService/{wid}/"
        return (f'<obj {NS} href="{h}" is="obix:Watch">'
                f'<reltime name="lease" val="PT30S"/>'
                f'<op name="add" href="add/" in="obix:WatchIn" out="obix:WatchOut"/>'
                f'<op name="remove" href="remove/" in="obix:WatchIn" out="obix:Nil"/>'
                f'<op name="pollChanges" href="pollChanges/" in="obix:Nil" out="obix:WatchOut"/>'
                f'<op name="pollRefresh" href="pollRefresh/" in="obix:Nil" out="obix:WatchOut"/>'
                f'<op name="delete" href="delete/" in="obix:Nil" out="obix:Nil"/></obj>')

    def watch_out_xml(self, hrefs):
        items = ""
        for h in hrefs:
            if h.strip("/") + "/" in self.points or h in self.points:
                items += self.point_xml(h if h in self.points else h.strip("/") + "/")
            else:
                items += (f'<err {NS} href="/obix/{h}" is="obix:BadUriErr" '
                          f'display="no such object"/>')
        # Obj().setIs("obix:WatchOut").initList("values", "obix:obj") is what the
        # station's own encoder writes, `of` included.
        return (f'<obj {NS} is="obix:WatchOut">'
                f'<list name="values" of="obix:obj">{items}</list></obj>')

    # --------------------------------------------------------------- batch op

    # ObixEncoder's lobbyPath, which BObixServer builds as "/" + servletName and
    # ObixUtils.resource strips off every val in a BatchIn. It is not the
    # hard-coded getServletPath(), which is a different string that happens to
    # read the same on a stock station.
    lobby_path = "/obix"

    def batch_xml(self, body):
        """Answer a BatchIn item by item, the way BBatchOp.invoke does.

        This fixture is deliberately *more* capable than the bridge: BBatchOp
        services obix:Write and obix:Invoke items as well as reads, so this does
        too. Proving the bridge cannot write in a batch against a fixture that
        could not write anyway would prove nothing.
        """
        doc = ET.fromstring(body.decode("utf-8"))
        if doc.tag.rsplit("}", 1)[-1] != "list":
            # invoke() throws before the reply is opened, so the whole document
            # is a fault rather than a BatchOut with fault rows in it.
            raise BatchDocError("Expecting list element but encountered "
                                + doc.tag.rsplit("}", 1)[-1])
        rows = "".join(self._batch_item(el) for el in list(doc))
        # Obj().initList(null, "obix:BatchOut"), and initList is setName/setOf:
        # a list with `of`, with no name and no `is`.
        return f'<list {NS} of="obix:BatchOut">{rows}</list>'

    def _batch_item(self, el):
        name = el.tag.rsplit("}", 1)[-1]
        # Every one of these failures is caught per item by BBatchOp's own
        # try/catch, encoded as an err in place, and the loop carries on.
        if name != "uri":
            return self._batch_err("", "Unexpected batch element: " + name)
        val = el.attrib.get("val")
        if val is None:
            return self._batch_err("", f'Missing val attribute: <{name}/>')
        if self.lobby_path not in val:
            # ObixUtils.resource throws a no-argument BadUriErr, whose getMessage
            # is null, so the encoder falls back to Throwable.toString() and the
            # display an operator sees is a Java class name.
            return self._batch_err(val, "com.tridium.obix.util.BadUriErr",
                                   "obix:BadUriErr")
        rest = val[val.index(self.lobby_path) + len(self.lobby_path):]
        if rest in ("", "/"):
            rest = "/"
        elif rest.endswith("/"):
            rest = rest[:-1]
        key = rest.lstrip("/") + "/"
        contract = el.attrib.get("is", "")
        # The dispatch is `contains`, not equality, and `is` defaults to the
        # empty string — so an item with no contract is a fault, not a read.
        if "obix:Read" in contract:
            if key not in self.points:
                return self._batch_err(val, "no such object", "obix:BadUriErr")
            return self.point_xml(key).replace(f'href="/obix/{key}"',
                                               f'href="{_xa(val)}"')
        if "obix:Write" in contract:
            # ObixUtils.child("in", elem) matches on the *name attribute* of a
            # child, not on its element name.
            child = next((c for c in list(el) if c.attrib.get("name") == "in"), None)
            if child is None:
                return self._batch_err(val, "Batch write missing child named 'in'")
            if key not in self.points:
                return self._batch_err(val, "no such object", "obix:BadUriErr")
            tag, _v, _u, writable = self.points[key]
            if not writable:
                return self._batch_err(val, "not writable", "obix:PermissionErr")
            raw = child.attrib.get("val", "")
            self.points[key][1] = ((raw == "true") if tag == "bool"
                                   else float(raw) if tag == "real"
                                   else int(raw) if tag == "int" else raw)
            self.writes.append((key, tag, self.points[key][1]))
            return self.point_xml(key).replace(f'href="/obix/{key}"',
                                               f'href="{_xa(val)}"')
        if "obix:Invoke" in contract:
            child = next((c for c in list(el) if c.attrib.get("name") == "in"), None)
            if child is None:
                return self._batch_err(val, "Batch invoke missing child named 'in'")
            return self._batch_err(val, "nothing under this fixture is invocable")
        return self._batch_err(val, f'Unknown batch contract: <{name} val="{val}"/>')

    def _batch_err(self, href, display, contract=""):
        """One fault row: initErr(href, display) is `<err href= display=>`, and
        the `is` attribute appears only for the exception types the encoder maps.
        A plain Exception carries none, which is why it is optional here."""
        is_ = f' is="{contract}"' if contract else ""
        h = f' href="{_xa(href)}"' if href else ""
        return f'<err{h}{is_} display="{_xa(display)}"/>'

    def alarms_xml(self):
        """The alarm service, as BAlarmServiceAgent.encodeFinishing leaves it.

        The op and feed hrefs are *relative and slash-terminated*, because
        `getChildHref(parentHref, name)` returns a bare `name + "/"` when the
        parent is the document element — which a direct GET always makes it.
        Serving them absolute would hide the resolution the client has to do.

        The subject lives at /obix/config/Services/AlarmService/ — the href the
        lobby's `alarms` ref advertises through makeSlotPathUri — not at a bare
        /obix/alarms/, which resolves to the station root."""
        h = "/obix/config/Services/AlarmService/"
        open_now = sum(1 for a in self.alarms if not a["acked"])
        return (f'<obj {NS} href="{h}" is="obix:AlarmSubject">'
                f'<int name="count" val="{open_now}" min="{open_now}"/>'
                f'<op name="query" href="~alarmQuery/" in="obix:AlarmFilter" '
                f'out="obix:AlarmQueryOut"/>'
                f'<feed name="feed" href="~alarmFeed/" in="obix:AlarmFilter" '
                f'of="obix:Alarm"/>'
                f'<ref name="CriticalAlarmClass" href="CriticalAlarmClass/" '
                f'is="obix:AlarmSubject"/></obj>')

    def alarm_class_xml(self, name):
        """One alarm class. BAlarmClassAgent advertises the same op and feed."""
        h = f"/obix/config/Services/AlarmService/{name}/"
        n = sum(1 for a in self.alarms if a["alarmClass"] == name)
        return (f'<obj {NS} href="{h}" is="obix:AlarmSubject">'
                f'<int name="count" val="{n}" min="{n}"/>'
                f'<op name="query" href="~alarmQuery/" in="obix:AlarmFilter" '
                f'out="obix:AlarmQueryOut"/>'
                f'<feed name="feed" href="~alarmFeed/" in="obix:AlarmFilter" '
                f'of="obix:Alarm"/></obj>')

    def alarm_record_xml(self, rec, href=""):
        """One record. The ack op is advertised with the out contract
        BAlarmWrapper.encode names — which is not the `is` the ack reply
        actually carries. Reproduced rather than tidied."""
        href = href or f'/obix/alarm/{rec["uuid"]}'
        fields = (f'<str name="niagara-uuid" val="{_xa(rec["uuid"])}"/>'
                  f'<str name="source" val="{_xa(rec["source"])}"/>'
                  f'<str name="sourceStation" val="{_xa(rec["sourceStation"])}"/>'
                  f'<str name="msgText" val="{_xa(rec["msgText"])}"/>'
                  f'<str name="alarmClass" val="{_xa(rec["alarmClass"])}"/>'
                  f'<int name="priority" val="{rec["priority"]}"/>'
                  f'<abstime name="timestamp" val="{rec["timestamp"]}"/>')
        for name in ("normalTimestamp", "ackTimestamp", "ackUser", "alarmValue"):
            if rec.get(name):
                tag = "abstime" if name.endswith("Timestamp") else "str"
                fields += f'<{tag} name="{name}" val="{_xa(rec[name])}"/>'
        return (f'<obj {NS} href="{href}" is="{rec["is"]}">{fields}'
                f'<op name="ack" href="ack/" in="obix:AlarmAckIn" '
                f'out="obix:AlarmAckOut"/></obj>')

    def alarm_query_xml(self, body, alarm_class=""):
        """An AlarmQueryOut. The data list is written before the count, because
        the station streams a cursor and only then knows what it sent."""
        limit, start, end = None, "", ""
        try:
            el = ET.fromstring(body.decode("utf-8")) if body else None
        except ET.ParseError:
            el = None
        for child in (el if el is not None else []):
            name = child.attrib.get("name", "")
            if name == "limit":
                limit = int(child.attrib.get("val", "0") or 0)
            elif name == "start":
                start = child.attrib.get("val", "")
            elif name == "end":
                end = child.attrib.get("val", "")
        rows = [a for a in self.alarms if not alarm_class
                or a["alarmClass"] == alarm_class]
        if start:
            rows = [a for a in rows if a["timestamp"] >= start]
        if end:
            rows = [a for a in rows if a["timestamp"] <= end]
        if limit:
            rows = rows[:limit]
        data = "".join(self.alarm_record_xml(a) for a in rows)
        first = rows[0]["timestamp"] if rows else "null"
        last = rows[-1]["timestamp"] if rows else "null"
        return (f'<obj {NS} is="obix:AlarmQueryOut">'
                f'<list name="data" of="obix:Alarm">{data}</list>'
                f'<int name="count" val="{len(rows)}"/>'
                f'<abstime name="start" val="{first}"/>'
                f'<abstime name="end" val="{last}"/></obj>')

    def ack_alarm(self, uuid, body, user="fixture operator"):
        """What BAlarmWrapper.invoke does, including the two details a client
        trips over: an input with no `ackUser` child throws before any branch
        runs, and the `ackUser` that is sent is discarded in favour of the
        authenticated user."""
        rec = next((a for a in self.alarms if a["uuid"] == uuid), None)
        if rec is None:
            raise KeyError(uuid)
        el = ET.fromstring(body.decode("utf-8")) if body else None
        sent = None
        forced = False
        for child in (el if el is not None else []):
            if child.attrib.get("name") == "ackUser":
                sent = child.attrib.get("val", "")
            if child.attrib.get("name") == "forceCleared":
                forced = True
        if sent is None:
            # get("ackUser").toString() on a missing child: the NPE happens
            # before the branch that would have handled it.
            raise AckInputError("java.lang.NullPointerException")
        rec["acked"] = True
        rec["ackUser"] = user            # not `sent`: the station overrides it
        rec["ackTimestamp"] = "2026-09-30T12:00:00.000Z"
        self.acks.append((uuid, sent, user))
        if forced:
            rec["normalTimestamp"] = "2026-09-30T12:00:00.000Z"
            self.force_clears.append(uuid)
        # initOp advertised obix:AlarmAckOut; invoke sets obix:AckAlarmOut.
        return (f'<obj {NS} is="obix:AckAlarmOut">'
                f'{self.alarm_record_xml(rec)}</obj>')

    def histories_xml(self):
        return (f'<obj {NS} href="/obix/histories/">'
                f'<ref name="AHU-01" href="AHU-01/"/></obj>')

    def history_feed_xml(self):
        """One history, as BObixHistoryAgent.encodeFinishing leaves it.

        Two things here are the station's and not this fixture's invention:

          * the four ops are named `query`, `rollup`, `feed` and `append`, and
            their in/out attributes are *def paths* — `/obix/def/obix:HistoryFilter`
            — rather than contract names. A client matching `in="obix:HistoryFilter"`
            matches nothing.
          * the canned queries are `ref` elements, not ops, and their hrefs are
            relative with a query string already filled in:
            `~historyQuery?start=...&end=...`. The eleven names are Tridium's
            spelling, two of which carry a space and brackets inside `name`.

        The station computes the bounds from its own clock; this fixture freezes
        them, because a test that moves with the clock proves nothing.
        EVIDENCE.md §N
        """
        h = "/obix/histories/AHU-01/SupplyAirTemp/"
        ops = (("query", "obix:HistoryFilter", "obix:HistoryQueryOut", "~historyQuery/"),
               ("rollup", "obix:HistoryRollupIn", "obix:HistoryRollupOut", "~historyRollup/"),
               ("append", "obix:HistoryAppendIn", "obix:HistoryAppendOut", "~historyAppend/"))
        op_xml = "".join(
            f'<op name="{n}" href="{href}" in="/obix/def/{in_}" out="/obix/def/{out}"/>'
            for n, in_, out, href in ops)
        op_xml += ('<feed name="feed" href="~historyFeed/" '
                   'in="/obix/def/obix:HistoryFilter" of="/obix/def/obix:HistoryRecord"/>')
        refs = "".join(f'<ref name="{_xa(n)}" href="{_xa(href)}"/>'
                       for n, href in CANNED_HISTORY_QUERIES)
        return (f'<obj {NS} href="{h}" is="obix:History">'
                f'<int name="count" val="1440"/>'
                f'<abstime name="start" val="{HISTORY_START}"/>'
                f'<abstime name="end" val="{HISTORY_NOW}"/>'
                f'{op_xml}{refs}</obj>')

    def history_result_xml(self, query=""):
        """A HistoryQueryOut, echoing the bounds the href carried.

        Echoing them is the point: the canned hrefs are relative *and* carry a
        query string, so a client that resolves them wrongly, or drops the
        query, gets a document that says so."""
        q = urllib.parse.parse_qs(query)
        bounds = "".join(
            f'<str name="asked-{k}" val="{_xa(v[0])}"/>'
            for k, v in sorted(q.items()) if v)
        return (f'<obj {NS} is="obix:HistoryQueryOut">'
                f'<int name="count" val="2"/>{bounds}'
                f'<list name="data" of="obix:HistoryRecord">'
                f'<obj><abstime name="timestamp" val="2026-09-30T11:00:00Z"/>'
                f'<real name="value" val="13.1"/></obj>'
                f'<obj><abstime name="timestamp" val="2026-09-30T11:15:00Z"/>'
                f'<real name="value" val="13.4"/></obj></list></obj>')

    # Values drift so a pollChanges has something to report, the way plant does.
    def tick(self):
        p = self.points["config/AHU-01/SupplyAirTemp/"]
        p[1] = round(p[1] + 0.1, 2)


class Handler(http.server.BaseHTTPRequestHandler):
    station: Station
    mode = "ok"          # ok | disabled | unlicensed
    require_auth = False
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    # ------------------------------------------------------------------ verbs

    def do_GET(self):
        self._serve("GET")

    def do_PUT(self):
        self._serve("PUT")

    def do_POST(self):
        self._serve("POST")

    def do_DELETE(self):
        self._xml(405, f'<err {NS} is="obix:UnsupportedErr" display="Unsupported HTTP method: DELETE"/>')

    def _serve(self, verb):
        split = urllib.parse.urlsplit(self.path)
        path = split.path
        if not path.startswith("/obix"):
            self._xml(404, f'<err {NS} display="not the oBIX servlet"/>')
            return
        if self.mode == "disabled":
            self.send_response(410)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.mode == "unlicensed":
            self._plain(403, "Unlicensed oBIX Server")
            return
        if self.require_auth and not self._authed():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="niagara"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        href = path[len("/obix"):].lstrip("/")
        body = b""
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            body = self.rfile.read(n)
        try:
            self._route(verb, href, body, split.query)
        except KeyError:
            self._xml(404, f'<err {NS} is="obix:BadUriErr" href="/obix/{_xa(href)}" '
                           f'display="no such object"/>')

    def _authed(self):
        h = self.headers.get("Authorization", "")
        if not h.startswith("Basic "):
            return False
        try:
            user, _, _pw = base64.b64decode(h[6:]).decode().partition(":")
        except Exception:
            return False
        return bool(user)

    def _route(self, verb, href, body, query=""):
        st = self.station
        if verb == "GET":
            if href in ("", "/"):
                return self._xml(200, st.lobby_xml())
            if href == "about/":
                return self._xml(200, st.about_xml())
            if href == "watchService/":
                return self._xml(200, st.watch_service_xml())
            if href in ("config/Services/AlarmService",
                        "config/Services/AlarmService/"):
                return self._xml(200, st.alarms_xml())
            m = re.match(r"^config/Services/AlarmService/([A-Za-z0-9_]+)/?$", href)
            if m:
                return self._xml(200, st.alarm_class_xml(m.group(1)))
            m = re.match(r"^alarm/([^/]+)/?$", href)
            if m:
                rec = next((a for a in st.alarms if a["uuid"] == m.group(1)), None)
                if rec is None:
                    # BAlarmLobbyAgent.resolve does write a diagnostic —
                    # `new BadUriErr("UUID not found: " + s)` — and then throws
                    # it *inside* a try whose handler is
                    # `catch (Exception) { throw new BadUriErr(); }`. BadUriErr
                    # extends RuntimeException, so its own message is caught and
                    # discarded by the method that wrote it. An undecodable UUID
                    # and a decodable one that is simply not in the database
                    # therefore come back identical: no message at all, and
                    # ObixEncoder.encode(Throwable) falls back to
                    # Throwable.toString(), so `display` is a Java class name.
                    return self._xml(200, f'<err {NS} is="obix:BadUriErr" '
                                          f'display="com.tridium.obix.util.BadUriErr"/>')
                return self._xml(200, st.alarm_record_xml(rec))
            if href == "histories/":
                return self._xml(200, st.histories_xml())
            if href == "histories/AHU-01/SupplyAirTemp/":
                return self._xml(200, st.history_feed_xml())
            # The station advertises the same op twice: `~historyQuery/` on the
            # op element and `~historyQuery?start=...` on every canned ref. Both
            # spellings are served, and only under the history they belong to —
            # a client that resolved them against /obix/ lands nowhere.
            if re.match(r"^histories/AHU-01/SupplyAirTemp/~historyQuery/?$", href):
                return self._xml(200, st.history_result_xml(query))
            if href in st.points:
                return self._xml(200, st.point_xml(href))
            if href.endswith("/") and st.is_folder(href):
                return self._xml(200, st.folder_xml(href))
            raise KeyError(href)

        if verb == "PUT":
            if href not in st.points:
                raise KeyError(href)
            tag, _val, unit, writable = st.points[href]
            if not writable:
                return self._xml(403, f'<err {NS} is="obix:PermissionErr" '
                                      f'href="/obix/{_xa(href)}" display="not writable"/>')
            el = ET.fromstring(body.decode("utf-8"))
            sent_tag = el.tag.rsplit("}", 1)[-1]
            if sent_tag != tag:
                return self._xml(400, f'<err {NS} is="obix:UnsupportedErr" '
                                      f'display="expected {tag}, got {sent_tag}"/>')
            raw = el.attrib.get("val", "")
            st.points[href][1] = (raw == "true") if tag == "bool" else (
                float(raw) if tag == "real" else int(raw) if tag == "int" else raw)
            st.writes.append((href, tag, st.points[href][1]))
            return self._xml(200, st.point_xml(href))

        # POST is invoke.
        if href in ("batch", "batch/"):
            try:
                return self._xml(200, st.batch_xml(body))
            except BatchDocError as exc:
                # The servlet catches the throw, builds a fresh encoder and
                # calls encode(Throwable): a 200 carrying one err document, not
                # an HTTP error status.
                return self._xml(200, f'<err {NS} href="/obix/batch" '
                                      f'display="{_xa(exc)}"/>')
        m = re.match(r"^config/Services/AlarmService/~alarmQuery/?$", href)
        if m:
            return self._xml(200, st.alarm_query_xml(body))
        m = re.match(r"^config/Services/AlarmService/([A-Za-z0-9_]+)/~alarmQuery/?$", href)
        if m:
            return self._xml(200, st.alarm_query_xml(body, alarm_class=m.group(1)))
        m = re.match(r"^alarm/([^/]+)/ack/?$", href)
        if m:
            try:
                return self._xml(200, st.ack_alarm(m.group(1), body))
            except AckInputError as exc:
                # A throw inside invoke: one err document, and a 200.
                return self._xml(200, f'<err {NS} href="/obix/{_xa(href)}" '
                                      f'display="{_xa(exc)}"/>')
            except KeyError:
                # Same catch-all as the read path: no message survives.
                return self._xml(200, f'<err {NS} is="obix:BadUriErr" '
                                      f'display="com.tridium.obix.util.BadUriErr"/>')
        if re.match(r"^histories/AHU-01/SupplyAirTemp/~historyQuery/?$", href):
            # The op takes a filter document; the canned refs reach the same
            # place with a GET and a query string.
            return self._xml(200, st.history_result_xml(query))
        if re.match(r"^histories/AHU-01/SupplyAirTemp/~historyAppend/?$", href):
            st.history_appends.append(body.decode("utf-8", "replace"))
            return self._xml(200, f'<obj {NS} is="obix:HistoryAppendOut">'
                                  f'<int name="numRecords" val="1"/></obj>')
        m = re.match(r"^watchService/make/?$", href)
        if m:
            return self._xml(200, st.watch_xml(st.next_watch()))
        m = re.match(r"^watchService/(watch\d+)/(add|remove|pollChanges|pollRefresh|delete)/?$",
                     href)
        if not m:
            raise KeyError(href)
        wid, op = m.group(1), m.group(2)
        w = st.watches.get(wid)
        if w is None:
            return self._xml(404, f'<err {NS} is="obix:BadUriErr" display="watch gone"/>')
        if op == "delete":
            st.watches.pop(wid, None)
            return self._xml(200, f'<obj {NS} is="obix:Nil"/>')
        if op in ("add", "remove"):
            hrefs = _watch_in_hrefs(body)
            if op == "add":
                for h in hrefs:
                    if h not in w["hrefs"]:
                        w["hrefs"].append(h)
                        # The add reply is itself a reading, so the value is
                        # already known to the client; a point counts as
                        # changed from here on, not from nothing.
                        w["seen"][h] = st.points.get(h, [None, None])[1]
                return self._xml(200, st.watch_out_xml(hrefs))
            w["hrefs"] = [h for h in w["hrefs"] if h not in hrefs]
            for h in hrefs:
                w["seen"].pop(h, None)
            return self._xml(200, f'<obj {NS} is="obix:Nil"/>')
        if op == "pollRefresh":
            return self._xml(200, st.watch_out_xml(w["hrefs"]))
        st.tick()
        changed = []
        for h in w["hrefs"]:
            cur = st.points.get(h, [None, None])[1]
            if w["seen"].get(h) != cur:
                w["seen"][h] = cur
                changed.append(h)
        return self._xml(200, st.watch_out_xml(changed))

    # ------------------------------------------------------------- responses

    def _xml(self, code, body):
        raw = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _plain(self, code, text):
        raw = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def serve(mode="ok", require_auth=False):
    """Start the fixture on an ephemeral port. Returns (base_url, station, stop)."""
    station = Station()

    class H(Handler):
        pass
    H.station = station
    H.mode = mode
    H.require_auth = require_auth

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_port}"

    def stop():
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)

    return base, station, stop


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Run the oBIX fixture station.")
    ap.add_argument("--mode", default="ok", choices=["ok", "disabled", "unlicensed"])
    ap.add_argument("--auth", action="store_true")
    ap.add_argument("--port", type=int, default=0)
    a = ap.parse_args()
    st = Station()

    class H(Handler):
        pass
    H.station, H.mode, H.require_auth = st, a.mode, a.auth
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", a.port), H)
    print(f"fixture station on http://127.0.0.1:{srv.server_port}/obix  mode={a.mode} "
          f"auth={'on' if a.auth else 'off'}")
    print(json.dumps(sorted(st.points), indent=1))
    srv.serve_forever()
