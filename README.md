# obix-mcp

An MCP server that lets an AI agent read a **Niagara** station over **oBIX** — the
lobby, points, folders, histories, open alarms, and live values through
server-side watches — and, only when explicitly enabled, write to a named
subtree.

It is read-only by default. The write tool is not merely refused when writes are
off: it is absent from the tool list, so an agent never sees a capability it
cannot use.

Stdlib Python, no dependencies, one file per concern. MIT.

## Why this exists, and what makes it different

Every protocol detail here was read out of one Niagara 4.15.5.22 install — its
jars and its shipped documentation — rather than recalled from the oBIX
specification. [EVIDENCE.md](EVIDENCE.md) cites the class and method behind each
decision, and says plainly where the install answers nothing.

Some of what came out of doing it that way:

  * **The export branch is mounted as `continuousControl`, not `export`.** It is
    the literal return of `BExportLobbyAgent.getLobbyName()`. An agent that walks
    to `/obix/export` because the documentation calls it the export agent finds
    nothing, and `obix_lobby` says so in its reply.
  * **HTTP Basic is not enabled on a Niagara station by default.** `DigestScheme`
    and `AXDigestScheme` are; `HTTPBasicScheme` has to be added to the
    `AuthenticationService` and assigned to the user. Until it is, every request
    gets 401 whatever the password is — so that is what the 401 message says.
  * **403 means two unrelated things** — an unlicensed oBIX server, or a
    permission refusal on one object — and only the body separates them
    (`"Unlicensed oBIX Server"`). Both readings are in the error.
  * **A relative href means "under this document", and an ordinary GET is where
    that bites.** `ObixEncoder.getChildHref` returns a bare `name + "/"` exactly
    when the parent is the document element — which a direct read always makes it
    — and `configChild` puts every component child through it. So a GET of the
    alarm service — at `/obix/config/Services/AlarmService/`, the href the
    `alarms` ref advertises — answers with its query op at `~alarmQuery/`,
    meaning `/obix/config/Services/AlarmService/~alarmQuery/`. Resolve it against
    `/obix/` the way a lobby child resolves and the station answers 404 while
    working perfectly. The lobby is the one document where both readings agree,
    which is how a bridge passes its whole suite and then fails on the first
    nested op. This one did, until the fixture started serving hrefs the way the
    station does. EVIDENCE.md §N.
  * **Five of the twelve lobby agents write nothing.** `alarm`, `bql`, `def`,
    `ord` and `units` each have an `encodeLobbyChild` that is a single `return`,
    so they resolve but appear in no listing a client can walk. `alarm` is the
    useful one: `/obix/alarm/<uuid>` resolves to a real alarm record. The
    station's own "UUID not found" message never arrives either: it is thrown
    inside a `catch (Exception) { throw new BadUriErr(); }` in the method that
    wrote it, so every failure on that branch is the same empty fault whose
    `display` is a Java class name.

**It has not been run against a JACE or any other controller.** It is tested
against a fixture derived from the same evidence, which proves self-consistency
and nothing about a real station. See EVIDENCE.md §L, and use `--dump` on the
first real run.

## Install and run

```sh
git clone https://github.com/UsamaIqbal0304/obix-mcp
cd obix-mcp
./run-tests.sh          # no station needed
```

The server speaks MCP over stdio:

```sh
export OBIX_PASSWORD='…'          # never a flag: a flag lands in ps and in history
python3 -m obix_mcp --station jace-1.example.net --user obixuser
```

In an MCP client's configuration:

```json
{
  "mcpServers": {
    "obix": {
      "command": "python3",
      "args": ["-m", "obix_mcp", "--station", "jace-1.example.net",
               "--user", "obixuser"],
      "env": { "OBIX_PASSWORD": "…" }
    }
  }
}
```

Flags:

| flag | what it does |
|---|---|
| `--station` | host, or `http(s)://host:port`. The path is fixed at `/obix` by the station itself, so a path you pass is ignored. |
| `--user` | station user. `OBIX_USER` works too. The password comes from `OBIX_PASSWORD` only. |
| `--timeout` | seconds per request, default 20. |
| `--dump` | print every request and reply to stderr, credentials excluded. Use it the first time you point this at a real station. It is chatty, and stderr is a pipe like any other: a client that captures stderr and never reads it will stall the server once the pipe fills. Point it at a file or a log. |
| `--insecure` | skip TLS verification, for a JACE still carrying its self-signed certificate. Warns on stderr. |
| `--allow-write` | permit writes at all. Off by default. |
| `--write-allow` | an href prefix writes are permitted under. Repeatable, required by `--allow-write`, and there is no allow-everything value. |

## Tools

| tool | what an agent gets |
|---|---|
| `obix_about` | product, version, vendor, server time — the cheapest confirmation you are talking to the station you think you are. |
| `obix_lobby` | the top of the tree, plus the documented branches this station lacks. |
| `obix_read` | one object by href, to a bounded depth. |
| `obix_batch_read` | several objects in one POST to the station's own batch op, with a fault row in place of anything that failed. Reads only. |
| `obix_watch_open` | a server-side watch on a set of hrefs, and the first reading of each. |
| `obix_watch_poll` | what changed since the last poll, or everything with `refresh`. |
| `obix_watch_follow` | poll for N seconds and return every change, with the interval clamped. |
| `obix_watch_close` | delete the watch rather than letting the lease lapse. |
| `obix_alarms` | the station's open alarms, through the alarm service's own query op, optionally scoped to one alarm class. Reads only — it cannot acknowledge, clear or change anything. |
| `obix_history` | a history feed, through the station's own canned queries. |
| `obix_write` | write one value to one point. **Present only with `--allow-write`.** |

## What it will not do

This list is the design, not a disclaimer:

  * **No raw-request tool.** Nothing takes a URL or an XML body, because an
    allowlist an agent can step around is decoration. Hrefs are resolved against
    `/obix` on the configured host and anything outside it is refused before a
    socket opens.
  * **No batch write.** The station's batch operation services writes and invokes
    as well as reads, and it is reached by POSTing to the batch op rather than by
    PUTting to a point — so it is the obvious way past the two gates below.
    `obix_batch_read` builds read items and has no verb argument to set; the
    module names no other contract, and a test asserts that, so the way past has
    to be added deliberately rather than found.
  * **No write without two separate decisions** — this
    deployment may write (`--allow-write`), and it may write *there*
    (`--write-allow`). A write to a Niagara point is a plant movement at a
    priority level, not a variable assignment.
  * **No polling faster than 10 seconds.** Tridium's own guidance for a watch on a
    controller is "increase this value to 10 seconds or more" against a 2-second
    default. An agent loop will poll as fast as it is permitted to, so the floor
    is enforced rather than documented.
  * **At most 4 open watches and 200 hrefs each.** These are this bridge's limits,
    not the station's — the install documents no ceiling at all. They exist
    because the thing that breaks under an agent in a loop is a controller in a
    building.
  * **No history URL construction.** The station lists its own queries; this
    follows their hrefs. Guessing a template works on one station and silently
    reads nothing on the next — and the station's real names are not the ones the
    documentation uses (`last24Hours`, not "Last 24 Hours"; eleven of them, not
    three).
  * **No alarm acknowledgement.** Reading alarms turned out to be possible after
    all — the install documents alarm *import* only, but a stock station serves
    its open alarms through the alarm service's `query` op, and serves each record
    at `/obix/alarm/<uuid>`, a branch no lobby lists (EVIDENCE.md §N). Every
    record advertises an `ack` op, and an ack writes to the alarm database: it
    sets the ack user and state, and with a `forceCleared` child it drives the
    source to normal and fires the service's audit action. `obix_alarms` builds
    no ack: no contract naming one, no tool, and a test that walks every string
    in the module and fails on one that mentions it. Acking an alarm is an
    operator's decision about a building, and this bridge is not the place to
    make it from.
  * **No history append.** The same history document that carries the canned
    queries carries four ops, one of which appends records. `obix_history`
    refuses to follow an op by name and says why.

## Layout

```
obix_mcp/obix.py    the protocol client: paths, decode, read, write, faults
obix_mcp/watch.py   watches, leases, poll intervals
obix_mcp/batch.py   one POST for many reads, and why it cannot carry a write
obix_mcp/alarms.py  the open-alarm read, and why it cannot acknowledge
obix_mcp/server.py  the MCP layer: tool schemas, JSON-RPC over stdio, the gates
tests/fixture_station.py   an oBIX server built from EVIDENCE.md, not the spec
tests/test_obix.py         the client
tests/test_server.py       the tool surface and the gates
tests/test_batch.py        the batch wire format, and that it cannot write
tests/test_alarms.py       the alarm query, and that nothing here can ack
EVIDENCE.md         every protocol decision, and where it came from
```

Requires Python 3.9 or newer in principle — stdlib only, annotations deferred —
though it has only been run on 3.12.

Not affiliated with, endorsed by, or supported by Tridium. Niagara and JACE are
their trademarks; oBIX is an OASIS standard. This reads a station over its own
published interface and nothing more.
