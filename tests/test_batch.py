"""Tests for the batch read: the wire format, the per-item faults, and the one
security property that matters.

The station's batch operation services writes and invokes as well as reads, and
it is reached by POSTing to the batch op rather than by PUTting to a point — so
it is the obvious way round the two write gates in obix.py. The fixture
implements batch writes on purpose, so that the tests below are about what the
bridge can express rather than about what the fixture happens to support.
"""
import ast
import inspect
import json
import os
import sys
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from obix_mcp import batch
from obix_mcp.batch import (MAX_BATCH_ITEMS, batch_href, batch_in, batch_out_rows,
                            read_many, servlet_paths)
from obix_mcp.obix import ObixClient, ObixError, ObixFault, decode
from obix_mcp.server import Bridge, Stdio, tool_schemas
from tests import fixture_station

TEMP = "config/AHU-01/SupplyAirTemp/"
RETURN = "config/AHU-01/ReturnAirTemp/"
SETPOINT = "config/AHU-01/ZoneSetpoint/"


class Base(unittest.TestCase):
    def setUp(self):
        self.base, self.station, stop = fixture_station.serve("ok", False)
        self.addCleanup(stop)
        self.client = ObixClient(self.base, "admin", "x")


class TestRequestBody(unittest.TestCase):
    """What goes out. BBatchOp reads the document element's name, each child's
    name, each child's val and each child's is — and nothing else."""

    def test_the_document_element_is_a_bare_list_not_an_obj(self):
        # A WatchIn is <obj><list>...</list></obj>; a BatchIn is <list>...</list>.
        # invoke() throws IllegalArgumentException on anything else.
        root = ET.fromstring(batch_in(["/obix/a/"]).decode("utf-8"))
        self.assertEqual(root.tag.rsplit("}", 1)[-1], "list")

    def test_every_item_is_a_uri_element_with_val_and_a_read_contract(self):
        root = ET.fromstring(batch_in(["/obix/a/", "/obix/b/"]).decode("utf-8"))
        kids = list(root)
        self.assertEqual([k.tag.rsplit("}", 1)[-1] for k in kids], ["uri", "uri"])
        self.assertEqual([k.attrib["val"] for k in kids], ["/obix/a/", "/obix/b/"])
        # `is` defaults to "" at the station and the dispatch is `contains`, so
        # an item without one is answered with "Unknown batch contract".
        self.assertEqual([k.attrib["is"] for k in kids], ["obix:Read", "obix:Read"])

    def test_values_are_escaped(self):
        body = batch_in(['/obix/config/A&B/']).decode("utf-8")
        self.assertIn("&amp;", body)
        ET.fromstring(body)

    def test_an_empty_batch_is_refused_here_rather_than_at_the_station(self):
        with self.assertRaises(ObixError):
            batch_in([])


class TestFullPaths(Base):
    """ObixUtils.resource is val.indexOf(lobbyPath), and throws BadUriErr when
    that is -1 — so a relative href, which is what a WatchIn carries, does not
    work in a BatchIn."""

    def test_relative_hrefs_are_expanded_to_servlet_paths(self):
        self.assertEqual(servlet_paths(self.client, [TEMP]), ["/obix/" + TEMP])

    def test_an_absolute_href_from_a_previous_reply_survives_unchanged(self):
        self.assertEqual(servlet_paths(self.client, ["/obix/" + TEMP]),
                         ["/obix/" + TEMP])

    def test_an_off_station_href_is_refused_before_a_socket_opens(self):
        with self.assertRaises(ObixError):
            servlet_paths(self.client, ["http://elsewhere.example/obix/x/"])

    def test_a_val_without_the_lobby_path_comes_back_as_a_bad_uri_row(self):
        # Sent deliberately malformed, which is the only way to see the station's
        # own answer to the mistake this module exists to avoid.
        op = batch_href(self.client)
        rows = batch_out_rows(self.client.invoke(
            op, batch_in(["config/AHU-01/SupplyAirTemp/"])))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tag"], "err")
        self.assertEqual(rows[0].get("contract"), "obix:BadUriErr")
        self.assertIn("full path", rows[0]["hint"])


class TestReply(Base):
    def test_the_op_href_comes_from_the_lobby(self):
        self.assertEqual(batch_href(self.client), "batch")

    def test_a_lobby_without_a_batch_op_says_so_instead_of_guessing_a_url(self):
        original = self.station.lobby_xml

        def no_batch():
            return original().replace(
                '<op name="batch" href="batch" in="obix:BatchIn" out="obix:BatchOut"/>', "")
        self.station.lobby_xml = no_batch
        with self.assertRaises(ObixError) as caught:
            batch_href(self.client)
        self.assertIn("no 'batch' op", str(caught.exception))

    def test_reading_three_points_returns_three_rows_in_one_post(self):
        seen = []
        self.client.trace = lambda method, url, sent, got: seen.append((method, url))
        out = read_many(self.client, [TEMP, RETURN, SETPOINT])
        self.assertEqual(out["requested"], 3)
        self.assertEqual(len(out["rows"]), 3)
        self.assertEqual({r["value"] for r in out["rows"]},
                         {13.4, 21.9, 21.0})
        # One GET for the lobby to find the op, then one POST for all three.
        self.assertEqual([m for m, _ in seen], ["GET", "POST"])

    def test_rows_carry_the_href_that_was_sent_so_they_can_be_matched(self):
        out = read_many(self.client, [TEMP, RETURN])
        self.assertEqual([r["href"] for r in out["rows"]],
                         ["/obix/" + TEMP, "/obix/" + RETURN])

    def test_a_reply_list_carries_no_is_attribute(self):
        # Obj.initList sets name and of, never is. A client matching on
        # is="obix:BatchOut" would find nothing, which is why nothing here does.
        raw = self.client.request("POST", batch_href(self.client),
                                  batch_in(["/obix/" + TEMP]))
        root = ET.fromstring(raw)
        self.assertEqual(root.tag.rsplit("}", 1)[-1], "list")
        self.assertNotIn("is", root.attrib)
        self.assertEqual(root.attrib.get("of"), "obix:BatchOut")

    def test_one_bad_href_becomes_one_row_and_the_others_are_still_read(self):
        out = read_many(self.client, [TEMP, "config/AHU-01/NoSuchPoint/", RETURN])
        self.assertEqual(len(out["rows"]), 3)
        good = [r for r in out["rows"] if r["tag"] != "err"]
        bad = [r for r in out["rows"] if r["tag"] == "err"]
        self.assertEqual(len(good), 2)
        self.assertEqual(len(bad), 1)
        self.assertIn("NoSuchPoint", bad[0]["href"])
        self.assertTrue(bad[0]["error"])

    def test_a_bare_class_name_display_is_translated_rather_than_shown_raw(self):
        rows = batch_out_rows(decode(
            '<list xmlns="http://obix.org/ns/schema/1.0" of="obix:BatchOut">'
            '<err href="/x/" display="com.tridium.obix.util.BadUriErr"/></list>'))
        self.assertIn("full path", rows[0]["hint"])

    def test_a_wrapped_reply_from_another_server_is_still_read(self):
        rows = batch_out_rows(decode(
            '<list xmlns="http://obix.org/ns/schema/1.0" of="obix:BatchOut">'
            '<real href="/obix/a/" val="1.5"/></list>'))
        self.assertEqual(rows[0]["value"], 1.5)
        wrapped = batch_out_rows(decode(
            '<obj xmlns="http://obix.org/ns/schema/1.0" is="obix:BatchOut">'
            '<list of="obix:obj"><real href="/obix/a/" val="1.5"/></list></obj>'))
        self.assertEqual(wrapped[0]["value"], 1.5)

    def test_a_document_level_failure_is_a_fault_not_a_row(self):
        # The station throws before the reply list is opened, and the servlet
        # answers 200 with one err document. decode() raises on that.
        with self.assertRaises(ObixFault):
            self.client.invoke(batch_href(self.client),
                               b'<obj xmlns="http://obix.org/ns/schema/1.0"/>')

    def test_a_href_the_station_answers_nothing_about_is_reported(self):
        # A station is under no obligation to return a row per item. Dropping one
        # here proves the caller is told, instead of believing both were read.
        self.station.batch_xml = lambda body: (
            '<list xmlns="http://obix.org/ns/schema/1.0" of="obix:BatchOut">'
            f'<real href="/obix/{TEMP}" val="13.4"/></list>')
        out = read_many(self.client, [TEMP, RETURN])
        self.assertEqual(out["no_row_returned"], [RETURN])


class TestBatchCannotWrite(Base):
    """The security property. The station's batch op writes; this bridge's batch
    tool has no way to ask it to."""

    def test_the_request_builder_has_no_verb_argument(self):
        params = list(inspect.signature(batch_in).parameters)
        self.assertEqual(params, ["paths"])

    def test_no_code_in_the_module_names_a_write_or_an_invoke_contract(self):
        # The module docstring documents the station's dispatch, so it names all
        # three contracts. Everything below it may name exactly one.
        src = inspect.getsource(batch)
        doc = ast.get_docstring(ast.parse(src))
        code = src.replace(doc, "")
        for forbidden in ("obix:Write", "obix:Invoke"):
            self.assertNotIn(forbidden, code, forbidden)
        self.assertIn("obix:Read", code)

    def test_no_body_this_module_can_build_carries_a_write_item(self):
        body = batch_in(servlet_paths(self.client, [SETPOINT])).decode("utf-8")
        self.assertNotIn("obix:Write", body)
        self.assertNotIn("obix:Invoke", body)
        self.assertNotIn("<in", body)
        self.assertNotIn('name="in"', body)

    def test_the_batch_tool_moves_nothing_even_with_writes_enabled(self):
        stdio = Stdio(Bridge(ObixClient(self.base, "admin", "x", allow_write=True,
                                       write_allowlist=["config/"])),
                      writes_enabled=True)
        reply = stdio.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": "obix_batch_read",
                                         "arguments": {"hrefs": [SETPOINT]}}})
        self.assertFalse(reply["result"].get("isError"), reply)
        self.assertEqual(self.station.writes, [])
        self.assertEqual(self.station.points[SETPOINT][1], 21.0)

    def test_the_fixture_would_have_performed_a_batch_write(self):
        # Proof that the test above is about the bridge and not about a fixture
        # that cannot write anyway. This body is hand-written here precisely
        # because no code in obix_mcp can produce it.
        body = ('<list xmlns="http://obix.org/ns/schema/1.0" is="obix:BatchIn">'
                f'<uri is="obix:Write" val="/obix/{SETPOINT}">'
                '<real name="in" val="18.5"/></uri></list>').encode("utf-8")
        self.client.invoke(batch_href(self.client), body)
        self.assertEqual(self.station.points[SETPOINT][1], 18.5)
        self.assertEqual(len(self.station.writes), 1)

    def test_the_batch_write_the_station_would_accept_needs_a_named_in_child(self):
        # ObixUtils.child("in", elem) matches the name *attribute*, so an <in>
        # element is not an `in` child. Recorded as a test because it is the
        # kind of detail a reimplementation gets wrong.
        body = ('<list xmlns="http://obix.org/ns/schema/1.0" is="obix:BatchIn">'
                f'<uri is="obix:Write" val="/obix/{SETPOINT}">'
                '<in val="18.5"/></uri></list>').encode("utf-8")
        rows = batch_out_rows(self.client.invoke(batch_href(self.client), body))
        self.assertEqual(rows[0]["tag"], "err")
        self.assertIn("missing child named 'in'", rows[0]["error"])
        self.assertEqual(self.station.points[SETPOINT][1], 21.0)


class TestSessionReuse(Base):
    def test_the_lobby_is_read_once_per_session_not_once_per_batch(self):
        bridge = Bridge(self.client)
        self.addCleanup(bridge.close)
        seen = []
        self.client.trace = lambda method, url, sent, got: seen.append(method)
        bridge.tool_batch_read([TEMP])
        bridge.tool_batch_read([RETURN])
        self.assertEqual(seen, ["GET", "POST", "POST"])

    def test_a_caller_holding_the_op_href_can_skip_the_lobby_read(self):
        op = batch_href(self.client)
        seen = []
        self.client.trace = lambda method, url, sent, got: seen.append(method)
        out = read_many(self.client, [TEMP], op=op)
        self.assertEqual(seen, ["POST"])
        self.assertEqual(len(out["rows"]), 1)

    def test_a_row_whose_href_lost_the_lobby_path_still_counts_as_answered(self):
        # Not evidenced from a station: a defensive match, tested so that the
        # defence is visible rather than implied.
        self.station.batch_xml = lambda body: (
            '<list xmlns="http://obix.org/ns/schema/1.0" of="obix:BatchOut">'
            f'<real href="{TEMP}" val="13.4"/></list>')
        out = read_many(self.client, [TEMP])
        self.assertNotIn("no_row_returned", out)


class TestCaps(Base):
    def test_the_cap_is_this_bridges_and_is_stated_in_the_schema(self):
        schema = [t for t in tool_schemas(False) if t["name"] == "obix_batch_read"][0]
        self.assertEqual(schema["inputSchema"]["properties"]["hrefs"]["maxItems"],
                         MAX_BATCH_ITEMS)

    def test_a_batch_past_the_cap_is_refused_without_a_request(self):
        seen = []
        self.client.trace = lambda *a: seen.append(a)
        with self.assertRaises(ObixError):
            read_many(self.client, [TEMP] * (MAX_BATCH_ITEMS + 1))
        self.assertEqual(seen, [])

    def test_an_empty_batch_is_refused(self):
        with self.assertRaises(ObixError):
            read_many(self.client, [])


class TestToolSurface(unittest.TestCase):
    def test_the_batch_tool_is_offered_with_writes_off(self):
        # Unlike obix_write: reading many points is a read, and hiding it would
        # push an agent towards twenty reads on a controller.
        self.assertIn("obix_batch_read", [t["name"] for t in tool_schemas(False)])

    def test_the_description_says_it_cannot_write(self):
        schema = [t for t in tool_schemas(False) if t["name"] == "obix_batch_read"][0]
        self.assertIn("cannot write", schema["description"])


if __name__ == "__main__":
    unittest.main()
