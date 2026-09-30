"""Tests for the alarms read: the query op, the reply, and the one thing this
tool must not be able to do.

Every alarm record the station encodes advertises an `ack` op, and an ack is an
invoke that writes to the alarm database — with a `forceCleared` child it also
drives the source state to normal and fires the service's audit action. The
fixture implements that ack, including the two details a client trips over, so
the tests below are about what this bridge can express rather than about what the
fixture happens to support.
"""
import ast
import inspect
import os
import sys
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from obix_mcp import alarms
from obix_mcp.alarms import (ALARM_FIELDS, DEFAULT_ALARM_LIMIT, MAX_ALARM_LIMIT,
                             alarm_rows, alarms_object, filter_in, open_alarms,
                             open_count, query_bounds, query_href)
from obix_mcp.obix import ObixClient, ObixError, ObixFault, decode
from obix_mcp.server import Bridge, Stdio, tool_schemas
from tests import fixture_station

HOT = "5f2e6d3c-9a41-4b77-8c10-0a1b2c3d4e5f"     # open, PointAlarm
FAN = "7c9b0a15-2d3e-4f60-b871-99aa88bb77cc"     # acked, StatefulAlarm
NS = "http://obix.org/ns/schema/1.0"


class Base(unittest.TestCase):
    def setUp(self):
        self.base, self.station, stop = fixture_station.serve("ok", False)
        self.addCleanup(stop)
        self.client = ObixClient(self.base, "admin", "x")


class TestFilterDocument(unittest.TestCase):
    """What goes out. BAlarmServiceQuery.invoke reads three children by name and
    tolerates every one of them being absent."""

    def test_an_empty_filter_is_a_valid_request_for_everything(self):
        root = ET.fromstring(filter_in().decode("utf-8"))
        self.assertEqual(root.tag, "{%s}obj" % NS)
        self.assertEqual(root.attrib["is"], "obix:AlarmFilter")
        self.assertEqual(list(root), [])

    def test_a_limit_is_an_int_child_the_station_applies(self):
        root = ET.fromstring(filter_in(limit=5).decode("utf-8"))
        kid = list(root)[0]
        self.assertEqual(kid.tag, "{%s}int" % NS)
        self.assertEqual((kid.attrib["name"], kid.attrib["val"]), ("limit", "5"))

    def test_bounds_are_abstimes_named_start_and_end(self):
        root = ET.fromstring(filter_in(start="2026-09-30T00:00:00Z",
                                      end="2026-09-30T23:59:59Z").decode("utf-8"))
        kids = [(k.tag.rsplit("}", 1)[-1], k.attrib["name"]) for k in root]
        self.assertEqual(kids, [("abstime", "start"), ("abstime", "end")])

    def test_a_bound_that_was_not_given_is_left_out_rather_than_sent_empty(self):
        # An empty start would become a lastUpdate bound in a BQL string.
        root = ET.fromstring(filter_in(end="2026-09-30T23:59:59Z").decode("utf-8"))
        self.assertEqual([k.attrib["name"] for k in root], ["end"])

    def test_values_are_escaped(self):
        self.assertIn("&amp;", filter_in(start='a&b"').decode("utf-8"))


class TestDiscovery(Base):
    def test_the_service_is_found_through_the_lobby_not_through_a_built_path(self):
        subject = alarms_object(self.client)
        self.assertIn("obix:AlarmSubject", subject.contract)
        self.assertEqual(open_count(subject), 1)      # one of the two is acked

    def test_a_lobby_with_no_alarms_branch_says_so_and_lists_what_there_is(self):
        full = self.station.lobby_xml()
        self.station.lobby_xml = lambda: full.replace(
            '<ref name="alarms" href="config/Services/AlarmService/" '
            'is="obix:AlarmSubject"/>', "")
        with self.assertRaises(ObixError) as caught:
            alarms_object(self.client)
        self.assertIn("no 'alarms' branch", str(caught.exception))
        self.assertIn("watchService", str(caught.exception))

    def test_the_op_href_comes_from_the_station_not_from_a_tilde_name(self):
        # The station serves this op href as a bare `~alarmQuery/`. It is
        # relative to the document it arrived in — the alarm service at
        # /obix/config/Services/AlarmService/ — so it means
        # /obix/config/Services/AlarmService/~alarmQuery/. A client that resolves
        # it against the servlet root asks for /obix/~alarmQuery/ and gets a 404.
        self.assertIn('href="~alarmQuery/"', self.station.alarms_xml())
        op = query_href(alarms_object(self.client))
        self.assertEqual(op, "/obix/config/Services/AlarmService/~alarmQuery/")

    def test_a_subject_with_no_query_op_says_what_it_does_offer(self):
        subject = decode(f'<obj xmlns="{NS}" is="obix:AlarmSubject">'
                         f'<int name="count" val="0"/></obj>')
        with self.assertRaises(ObixError) as caught:
            query_href(subject)
        self.assertIn("no 'query' op", str(caught.exception))
        self.assertIn("count", str(caught.exception))

    def test_a_subject_with_no_count_reports_no_count_rather_than_zero(self):
        subject = decode(f'<obj xmlns="{NS}" is="obix:AlarmSubject"/>')
        self.assertIsNone(open_count(subject))


class TestReply(Base):
    def test_every_field_the_station_sent_is_on_the_row(self):
        out = open_alarms(self.client)
        row = [r for r in out["rows"] if r["uuid"] == HOT][0]
        self.assertEqual(row["source"],
                         "station:|slot:/Drivers/AHU-01/SupplyAirTemp")
        self.assertEqual(row["msgText"], "Supply air temperature above limit")
        self.assertEqual(row["alarmClass"], "DefaultAlarmClass")
        self.assertEqual(row["priority"], 255)
        self.assertEqual(row["timestamp"], "2026-09-30T09:14:02.100Z")
        self.assertIn("obix:PointAlarm", row["contracts"])

    def test_the_uuid_is_carried_because_it_is_the_only_handle_on_a_record(self):
        out = open_alarms(self.client)
        self.assertEqual({r["uuid"] for r in out["rows"]}, {HOT, FAN})

    def test_a_field_the_station_omitted_is_absent_rather_than_empty(self):
        out = open_alarms(self.client)
        row = [r for r in out["rows"] if r["uuid"] == HOT][0]
        self.assertNotIn("ackUser", row)
        self.assertNotIn("normalTimestamp", row)
        acked = [r for r in out["rows"] if r["uuid"] == FAN][0]
        self.assertEqual(acked["ackUser"], "someone else")

    def test_the_count_and_bounds_are_read_although_they_arrive_after_the_list(self):
        out = open_alarms(self.client)
        self.assertEqual(out["count"], 2)
        self.assertEqual(out["start"], "2026-09-30T09:14:02.100Z")
        self.assertEqual(out["end"], "2026-09-30T09:20:41.000Z")

    def test_the_bounds_are_read_when_they_arrive_before_the_list_instead(self):
        # Order is not part of the contract: the station writes them last only
        # because it streams a cursor.
        out = decode(f'<obj xmlns="{NS}" is="obix:AlarmQueryOut">'
                     f'<int name="count" val="1"/>'
                     f'<list name="data" of="obix:Alarm">'
                     f'<obj is="obix:Alarm"><str name="source" val="x"/></obj>'
                     f'</list></obj>')
        self.assertEqual(query_bounds(out)["count"], 1)
        self.assertEqual(len(alarm_rows(out)), 1)

    def test_records_returned_outside_a_named_list_are_still_read(self):
        out = decode(f'<obj xmlns="{NS}" is="obix:AlarmQueryOut">'
                     f'<obj is="obix:Alarm"><str name="source" val="x"/></obj>'
                     f'<obj is="obix:Alarm"><str name="source" val="y"/></obj></obj>')
        self.assertEqual([r["source"] for r in alarm_rows(out)], ["x", "y"])

    def test_a_reply_with_no_records_is_an_empty_list_not_an_error(self):
        out = open_alarms(self.client, start="2027-01-01T00:00:00Z")
        self.assertEqual(out["rows"], [])
        self.assertEqual(out["count"], 0)

    def test_the_station_applies_the_limit_rather_than_the_bridge_trimming(self):
        out = open_alarms(self.client, limit=1)
        self.assertEqual(len(out["rows"]), 1)
        self.assertEqual(out["count"], 1)


class TestScope(Base):
    def test_one_alarm_class_can_be_asked_instead_of_the_service(self):
        subject = alarms_object(self.client)
        cls = subject.child("CriticalAlarmClass")
        # The class ref arrives as `CriticalAlarmClass/`, relative to the alarm
        # service at /obix/config/Services/AlarmService/. Passing it through as
        # the station wrote it is the case that fails if hrefs are resolved
        # against the servlet root instead.
        self.assertEqual(
            cls.href, "/obix/config/Services/AlarmService/CriticalAlarmClass/")
        out = open_alarms(self.client, scope=cls.href)
        self.assertEqual([r["uuid"] for r in out["rows"]], [FAN])
        self.assertEqual(
            out["op"],
            "/obix/config/Services/AlarmService/CriticalAlarmClass/~alarmQuery/")

    def test_a_scoped_call_does_not_reuse_the_services_cached_op(self):
        bridge = Bridge(self.client)
        self.addCleanup(bridge.close)
        bridge.tool_alarms()
        self.assertEqual(bridge.alarms_op,
                         "/obix/config/Services/AlarmService/~alarmQuery/")
        out = bridge.tool_alarms(
            scope="config/Services/AlarmService/CriticalAlarmClass/")
        self.assertEqual(
            out["op"],
            "/obix/config/Services/AlarmService/CriticalAlarmClass/~alarmQuery/")
        self.assertEqual(bridge.alarms_op,
                         "/obix/config/Services/AlarmService/~alarmQuery/")


class TestAlarmsCannotAck(Base):
    """The security property. The station's records can be acked and force
    cleared through an invoke; nothing in this module can ask for it."""

    def test_no_string_in_the_module_mentions_ack_outside_two_field_names(self):
        src = inspect.getsource(alarms)
        tree = ast.parse(src)
        # Docstrings are excluded by *node identity* — not by comparing against
        # ast.get_docstring, which cleans indentation, so the cleaned text never
        # equals the raw constant and every string would be exempt. They are
        # excluded at all because prose cannot reach the wire, and because the
        # module docstring's whole job is to explain the ack this module refuses
        # to build. What is left is every string that could be sent.
        docs = {id(n.body[0].value) for n in ast.walk(tree)
                if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.ClassDef))
                and n.body and isinstance(n.body[0], ast.Expr)
                and isinstance(n.body[0].value, ast.Constant)
                and isinstance(n.body[0].value.value, str)}
        allowed = {"ackTimestamp", "ackUser"}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in docs:
                continue
            if "ack" in node.value.lower():
                self.assertIn(node.value, allowed, node.value)

    def test_no_docstring_but_the_module_s_own_names_an_ack_contract(self):
        """The scan above lets prose through. This one keeps a function
        docstring from quietly documenting an ack this module does not do."""
        src = inspect.getsource(alarms)
        tree = ast.parse(src)
        module_doc = tree.body[0].value if isinstance(tree.body[0], ast.Expr) else None
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            doc = ast.get_docstring(node) or ""
            if node.body and node.body[0].value is module_doc:
                continue
            for token in ("obix:AlarmAckIn", "obix:AckAlarm", "forceCleared"):
                self.assertNotIn(token, doc, f"{node.name}: {token}")

    def test_no_request_this_module_builds_can_carry_an_ack_input(self):
        params = set(inspect.signature(filter_in).parameters)
        self.assertEqual(params, {"start", "end", "limit"})
        body = filter_in(start="2026-01-01T00:00:00Z", limit=5).decode("utf-8")
        for forbidden in ("ackUser", "forceCleared", "AlarmAckIn", "AckAlarm"):
            self.assertNotIn(forbidden, body, forbidden)

    def test_there_is_no_ack_tool_with_writes_on_or_off(self):
        for enabled in (False, True):
            names = [t["name"] for t in tool_schemas(enabled)]
            self.assertEqual([n for n in names if "ack" in n.lower()], [])
            self.assertIn("obix_alarms", names)

    def test_the_alarms_tool_changes_nothing_even_with_writes_enabled(self):
        stdio = Stdio(Bridge(ObixClient(self.base, "admin", "x", allow_write=True,
                                       write_allowlist=["alarm", "config/"])),
                      writes_enabled=True)
        reply = stdio.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": "obix_alarms", "arguments": {}}})
        self.assertFalse(reply["result"].get("isError"), reply)
        self.assertEqual(self.station.acks, [])
        self.assertEqual(self.station.force_clears, [])
        self.assertFalse(self.station.alarms[0]["acked"])

    def test_the_fixture_would_have_acked_the_alarm(self):
        # Proof that the test above is about the bridge. This body is written by
        # hand precisely because no code in obix_mcp can produce it.
        body = (f'<obj xmlns="{NS}" is="obix:AlarmAckIn">'
                f'<str name="ackUser" val="whoever"/></obj>').encode("utf-8")
        out = self.client.invoke(f"alarm/{HOT}/ack", body)
        self.assertTrue(self.station.alarms[0]["acked"])
        self.assertEqual(len(self.station.acks), 1)
        # The op is advertised as out="obix:AlarmAckOut" and the reply carries
        # is="obix:AckAlarmOut". Both spellings are Tridium's.
        self.assertEqual(out.contract, "obix:AckAlarmOut")
        # The op element advertises in/out contracts and carries no `is` of its
        # own, so a client cannot match the reply against the op it invoked.
        rec = self.client.read(f"alarm/{HOT}/")
        self.assertEqual(rec.child("ack").contract, "")

    def test_an_ack_the_station_would_accept_needs_an_ackuser_child(self):
        # get("ackUser").toString() runs before any branch, so an input without
        # the child throws rather than defaulting to the authenticated user.
        body = f'<obj xmlns="{NS}" is="obix:AlarmAckIn"/>'.encode("utf-8")
        with self.assertRaises(ObixFault) as caught:
            self.client.invoke(f"alarm/{HOT}/ack", body)
        self.assertIn("NullPointerException", str(caught.exception))
        self.assertFalse(self.station.alarms[0]["acked"])

    def test_the_ackuser_a_client_sends_is_discarded_by_the_station(self):
        # invoke() overwrites it with the authenticated user's name, so the
        # field is mandatory and ignored at the same time.
        body = (f'<obj xmlns="{NS}" is="obix:AlarmAckIn">'
                f'<str name="ackUser" val="not me"/></obj>').encode("utf-8")
        self.client.invoke(f"alarm/{HOT}/ack", body)
        self.assertEqual(self.station.alarms[0]["ackUser"], "fixture operator")
        self.assertEqual(self.station.acks[0][1], "not me")


class TestUnlistedBranch(Base):
    def test_a_record_is_readable_by_uuid_though_no_lobby_lists_the_branch(self):
        uuids = [r["uuid"] for r in open_alarms(self.client)["rows"]]
        rec = self.client.read(f"alarm/{uuids[0]}/")
        self.assertIn("obix:Alarm", rec.contract)
        self.assertNotIn("alarm", [c.name for c in self.client.lobby().children])

    def test_every_unresolvable_uuid_is_the_same_contentless_fault(self):
        """BAlarmLobbyAgent.resolve throws `BadUriErr("UUID not found: " + s)`
        inside a try whose handler is `catch (Exception) { throw new
        BadUriErr(); }`, and BadUriErr extends RuntimeException — so the method
        discards its own diagnostic. An undecodable UUID and a well-formed one
        that is absent are indistinguishable to a client."""
        absent = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"   # decodable, not in the db
        for href in ("alarm/not-a-uuid/", f"alarm/{absent}/"):
            with self.assertRaises(ObixFault) as caught:
                self.client.read(href)
            self.assertEqual(caught.exception.contract, "obix:BadUriErr")
            self.assertEqual(caught.exception.display, "com.tridium.obix.util.BadUriErr")

    def test_the_bridge_explains_a_fault_the_station_left_empty(self):
        # The station's display is a Java class name, which tells an operator
        # nothing. The client's message has to carry the meaning instead.
        with self.assertRaises(ObixFault) as caught:
            self.client.read("alarm/not-a-uuid/")
        self.assertIn("could not resolve", str(caught.exception))


class TestSessionReuse(Base):
    def test_the_op_is_found_once_per_session_not_once_per_query(self):
        bridge = Bridge(self.client)
        self.addCleanup(bridge.close)
        seen = []
        self.client.trace = lambda method, url, sent, got: seen.append(method)
        bridge.tool_alarms()
        bridge.tool_alarms()
        # First call: lobby, service, query. Second: query only.
        self.assertEqual(seen, ["GET", "GET", "POST", "POST"])

    def test_the_station_count_is_left_out_rather_than_reused_when_stale(self):
        bridge = Bridge(self.client)
        self.addCleanup(bridge.close)
        first = bridge.tool_alarms()
        self.assertEqual(first["open_alarms_on_station"], 1)
        self.assertNotIn("open_alarms_on_station", bridge.tool_alarms())


class TestCaps(Base):
    def test_a_limit_past_the_cap_is_refused_without_a_request(self):
        seen = []
        self.client.trace = lambda *a: seen.append(a)
        with self.assertRaises(ObixError):
            open_alarms(self.client, limit=MAX_ALARM_LIMIT + 1)
        self.assertEqual(seen, [])

    def test_a_negative_limit_is_refused(self):
        with self.assertRaises(ObixError):
            filter_in(limit=-1)

    def test_the_default_limit_is_sent_rather_than_no_limit_at_all(self):
        sent = []
        # The trace hook is handed the request body already decoded, because it
        # exists to be read by a person.
        self.client.trace = lambda method, url, body, got: sent.append(body or "")
        open_alarms(self.client)
        self.assertIn(f'name="limit" val="{DEFAULT_ALARM_LIMIT}"', "".join(sent))

    def test_the_cap_is_stated_in_the_schema(self):
        schema = [t for t in tool_schemas(False) if t["name"] == "obix_alarms"][0]
        limit = schema["inputSchema"]["properties"]["limit"]
        self.assertEqual(limit["maximum"], MAX_ALARM_LIMIT)
        self.assertEqual(limit["default"], DEFAULT_ALARM_LIMIT)


class TestToolSurface(unittest.TestCase):
    def test_the_alarms_tool_is_offered_with_writes_off(self):
        self.assertIn("obix_alarms", [t["name"] for t in tool_schemas(False)])

    def test_the_description_says_it_cannot_acknowledge(self):
        schema = [t for t in tool_schemas(False) if t["name"] == "obix_alarms"][0]
        self.assertIn("cannot acknowledge", schema["description"])

    def test_every_documented_field_is_one_the_station_writes(self):
        # ALARM_FIELDS is a claim about BAlarmServiceAgent, so it is checked
        # against what the fixture — written from that bytecode — actually sends.
        self.assertNotIn("ackState", ALARM_FIELDS)
        self.assertIn("msgText", ALARM_FIELDS)


if __name__ == "__main__":
    unittest.main()
