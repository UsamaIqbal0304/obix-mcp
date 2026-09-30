"""obix.py — an oBIX client for a Niagara station, in the standard library only.

Why no dependencies: this runs on an engineer's laptop, on a jump box, and
sometimes on the same constrained network as the controller. Every dependency
is something to audit before it is allowed near a building. urllib, ElementTree
and base64 are enough.

Every protocol decision in this file is traceable to something in EVIDENCE.md,
which cites the Niagara install it was read out of. Where the oBIX
specification says one thing and a Niagara station demonstrably does another,
this file follows the station and says so in a comment.
"""
from __future__ import annotations

import base64
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

# BObixServer.getServletPath() is a hard `ldc "/obix"` in the bytecode, and so
# are the soap, wsdl, xsd and xsl paths beside it. None of them reads the
# `servletName` property, but two other code paths do: the servlet is registered
# with the web server under that name, and every encoder is built with a lobby
# path of "/" + servletName, which is the prefix a URI in a request body must
# carry. On a stock station all of that agrees on /obix. Not configurable here,
# because a setting would imply the station honours one everywhere — and where
# the two disagree the station is misconfigured, not flexible.  EVIDENCE.md §A
SERVLET_PATH = "/obix"

OBIX_NS = "http://obix.org/ns/schema/1.0"

# The lobby children a client actually sees, as the literal strings each
# B*LobbyAgent.getLobbyName() returns. Only the seven agents whose
# encodeLobbyChild writes an element land in a GET of /obix; these are they.
# Worth reading twice: the agent Tridium's own documentation calls the *export*
# agent mounts as `continuousControl`, so an agent looking for /obix/export
# finds nothing.  EVIDENCE.md §B
LOBBY_CHILDREN = (
    "about", "alarms", "batch", "config", "continuousControl",
    "histories", "watchService",
)

# Twelve agents are registered on ObixLobby; only seven (above) write an element
# into the list a client walks. The other five have an encodeLobbyChild that is
# a single `return` — they resolve but appear in no listing: `alarm` serves
# /obix/alarm/<uuid> (a Niagara UUID against the alarm database), `bql` runs a
# BQL query, `ord` resolves a raw ORD, `def` is the contract dictionary, and
# `units` the unit database. None of these is a branch a station lacks — each is
# a branch no station shows.  EVIDENCE.md §N
UNLISTED_BRANCHES = ("alarm", "bql", "def", "ord", "units")

# The oBIX About contract is this fixed set of fields. Niagara freezes some of
# their defaults (productName is the literal "Niagara AX", never overwritten) and
# adds two of its own outside the contract — componentCount (the station's
# component count) and localHistoryCount — so /obix/about hands a read-only
# client a station-size readout. Only serverTime is refreshed per request; the
# rest are a snapshot from when the About type first loaded.  EVIDENCE.md §O
ABOUT_CONTRACT = (
    "obixVersion", "serverName", "serverTime", "serverBootTime",
    "vendorName", "vendorUrl", "productName", "productVersion",
    "productUrl", "tz",
)

# BObixServer.service(WebOp) switches on the uppercased request method:
# GET encodes the resolved target (read), PUT calls ObixUtils.serviceWrite,
# POST invokes. The station's WSDL declares the same three as obixRead,
# obixWrite and obixInvoke.  EVIDENCE.md §C
VERB_READ, VERB_WRITE, VERB_INVOKE = "GET", "PUT", "POST"

# Tridium's own recommendation for the watch poll interval on a controller is
# "increase this value to 10 seconds or more", against a driver default of 2.
# A tool an agent drives will poll more eagerly than a human would, so the
# floor here is the recommendation rather than the default.  EVIDENCE.md §J
MIN_POLL_SECONDS = 10.0

# BObixWatchService.defaultLeaseTime defaults to 30 seconds, so a watch left
# unpolled is collected. Renew at a third of the lease.  EVIDENCE.md §F
DEFAULT_LEASE_SECONDS = 30.0


class ObixError(Exception):
    """Anything that stopped the request, with a message an engineer can act on."""


# A Niagara err whose `display` is a bare Java class name carries no message at
# all: `ObixEncoder.encode(Throwable)` uses getMessage() and falls back to
# toString() when it is null, so this is what a no-argument throw looks like on
# the wire. It happens more than a reader would expect — `BAlarmLobbyAgent.resolve`
# wraps its whole body in `catch (Exception) { throw new BadUriErr(); }`, which
# swallows even the "UUID not found: ..." diagnostic the same method wrote — so
# the client says what the station did not.  EVIDENCE.md §N
_BARE_CLASS_DISPLAY = re.compile(r"^(?:[a-z][\w$]*\.)+[A-Z][\w$]*$")

_BARE_CLASS_HELP = {
    "com.tridium.obix.util.BadUriErr":
        "the station could not resolve that href, and its reply carries no reason. "
        "On the alarm branch every failure looks like this one: an unparseable UUID, "
        "a UUID that is simply not in the alarm database, and a database error are "
        "all the same empty fault.",
    "com.tridium.obix.util.PermissionErr":
        "the station refused the object on permissions, without saying which check "
        "failed. The oBIX user's own permissions on that component are where to look.",
}


class ObixFault(ObixError):
    """The station answered with an <err> element. This is the station saying no."""

    def __init__(self, contract: str, display: str, href: str = ""):
        self.contract, self.display, self.href = contract, display, href
        detail = display or "no detail given"
        if _BARE_CLASS_DISPLAY.match(display or ""):
            detail = (f"{display} — that display is a Java class name, not a message: "
                      + _BARE_CLASS_HELP.get(
                          display.strip(),
                          "the station threw without one, so the class is all it said."))
        super().__init__(f"{contract or 'obix:err'}: {detail}"
                         + (f" (at {href})" if href else ""))


# The station-level failures worth naming, because each has one cause and one fix
# and none of them is a bug in this tool. The two whole-server codes are read
# straight out of BObixServer.service(WebOp): 403 "Unlicensed oBIX Server" and
# 410 "oBIX Server Disabled". 401 is the authentication scheme.
# EVIDENCE.md §D, §I
_STATUS_HELP = {
    401: ("The station rejected the credentials. Niagara does not enable HTTP Basic "
          "by default: HTTPBasicScheme has to be added to the station's "
          "AuthenticationService and assigned to the user this tool signs in as. "
          "Until it is, every request here is a 401 no matter what the password is. "
          "The scheme's internal name is 'n4HTTPbasic'. Do not expect a realm in "
          "the challenge — a Niagara 401 carries 'WWW-Authenticate: BASIC "
          "handshakeToken=<session id>' and no realm at all. If the password has "
          "non-ASCII characters, suspect the charset: the station decodes the "
          "credentials in its default charset, and this client encodes UTF-8."),
    # 403 carries two unrelated meanings and the body is what separates them, so
    # this text sends the reader to the body rather than guessing for them.
    403: ("A 403 here is one of two different things, and the station's own words "
          "below say which. 'Unlicensed oBIX Server' means the whole server is off "
          "for licensing: the obixDriver feature needs export=true and the station "
          "needs a licensed web feature. Anything else means this object was "
          "refused — the user lacks permission on it, or it is not exposed to "
          "oBIX at all."),
    410: ("The station's ObixNetwork is present but its server is disabled. Set "
          "ObixNetwork > Server > Enabled to true. A disabled server answers 410 to "
          "every oBIX request."),
}


@dataclass
class ObixObject:
    """One decoded oBIX object. Kept deliberately close to the wire.

    `contract` is the `is` attribute, `val` the raw string value, `children`
    the nested objects. Nothing here guesses a Python type for `val` — see
    `value()`, which converts only when the element name says how.
    """
    tag: str
    name: str = ""
    href: str = ""
    contract: str = ""
    val: str | None = None
    display: str = ""
    display_name: str = ""
    status: str = ""
    unit: str = ""
    writable: bool = False
    children: list["ObixObject"] = field(default_factory=list)

    def value(self):
        """The value as a Python object, converted only where the tag says how.

        oBIX types the value by element name, not by sniffing the string, and
        so does this: a `str` whose val happens to read "true" stays the string
        "true". Guessing is how a set point ends up written as a boolean.
        """
        if self.val is None:
            return None
        if self.tag == "bool":
            return self.val == "true"
        if self.tag == "int":
            try:
                return int(self.val)
            except ValueError:
                return self.val
        if self.tag == "real":
            try:
                return float(self.val)
            except ValueError:
                return self.val
        return self.val

    def child(self, name: str) -> "ObixObject | None":
        for c in self.children:
            if c.name == name:
                return c
        return None

    def as_dict(self, depth: int = 3) -> dict:
        d = {"tag": self.tag}
        for k in ("name", "href", "contract", "display", "display_name",
                  "status", "unit"):
            if getattr(self, k):
                d[k] = getattr(self, k)
        if self.val is not None:
            d["value"] = self.value()
        if self.writable:
            d["writable"] = True
        if self.children and depth > 0:
            d["children"] = [c.as_dict(depth - 1) for c in self.children]
        elif self.children:
            d["children_omitted"] = len(self.children)
        return d


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def resolve_href(base: str, href: str) -> str:
    """Resolve one href against the href of the object that carried it.

    This is the rule the station encodes to, read out of
    `ObixEncoder.getChildHref(parentHref, name)` and its one caller that matters,
    `ObixEncoder.configChild` — which is the path *every* component child goes
    through, not a special case for a few agents:

      * when the parent element is the document element and its href ends in a
        slash, the child href is a bare `name + "/"`;
      * when the parent has no href, likewise a bare `name + "/"`;
      * otherwise `ObixUtils.concat(parentHref, name + "/")`.

    So the hrefs a client cannot resolve against the servlet root are exactly
    the ones an ordinary GET returns. A GET of the alarm service — at
    `/obix/config/Services/AlarmService/`, the href the `alarms` ref advertises —
    answers with its query op at `~alarmQuery/`, and that means
    `/obix/config/Services/AlarmService/~alarmQuery/`. Resolving it against
    `/obix/` — which is what a client does if it treats every relative href the
    way a lobby child behaves — asks for `/obix/~alarmQuery/`, and the station
    answers 404. The lobby is the one document where the two agree, which is why
    a bridge can pass every test it has against a lobby walk and still fail on
    the first nested op.

    An href beginning `|` is left alone: `configChild` falls back to
    `"|" + ord.encodeToString()` for a child it cannot name, and that is an ORD,
    not a path to join onto anything.  EVIDENCE.md §N
    """
    h = (href or "").strip()
    if not h or h.startswith("|"):
        return h
    if re.match(r"^https?://", h) or h.startswith("/"):
        return h
    b = (base or "").strip()
    if not b or b.startswith("|") or not re.match(r"^(https?://|/)", b):
        return h
    return urllib.parse.urljoin(b, h)


def _resolve_tree(obj: "ObixObject", base: str) -> None:
    """Resolve every href in a decoded document, depth first.

    Each element is resolved against its parent's *resolved* href, and an
    element without one passes its own parent's down — which is what the encoder
    does when it hands `getChildHref` a null parent href.
    """
    obj.href = resolve_href(base, obj.href)
    inner = obj.href or base
    for c in obj.children:
        _resolve_tree(c, inner)


def decode(xml: str | bytes, doc_href: str = "") -> ObixObject:
    """Decode one oBIX document, with every href resolved.

    `doc_href` is the path the document was fetched from. The station normally
    sets the document element's own href and that wins; `doc_href` is what makes
    a reply that omits it — a batch reply, a fault — still resolvable. Hrefs are
    resolved here rather than at the point of use because a caller that holds an
    href from a nested read has no way to know what it was relative to.
    See `resolve_href`.  EVIDENCE.md §N

    The station sends text/xml with the oBIX namespace as the default, so every
    element arrives namespace-qualified from ElementTree's point of view. Both
    forms are accepted because a fixture, a proxy or a hand-written request may
    send unqualified elements, and rejecting those would make the tool harder to
    test than to trust.
    """
    try:
        root = ET.fromstring(xml.strip() if isinstance(xml, str) else xml)
    except ET.ParseError as exc:
        raise ObixError(f"the station's reply is not XML: {exc}") from exc
    obj = _decode_elem(root)
    base = (doc_href or "").strip()
    _resolve_tree(obj, base)
    if not obj.href and base:
        # The document element is addressable at the path it was read from even
        # when the station leaves the href off, and a batch reply does exactly
        # that: `<list of="obix:BatchOut">` with no href and no name.
        obj.href = base
    if obj.tag == "err":
        raise ObixFault(obj.contract, obj.display or obj.display_name, obj.href)
    return obj


def _decode_elem(el) -> ObixObject:
    a = el.attrib
    obj = ObixObject(
        tag=_localname(el.tag),
        name=a.get("name", ""),
        href=a.get("href", ""),
        contract=a.get("is", ""),
        val=a.get("val"),
        display=a.get("display", ""),
        display_name=a.get("displayName", ""),
        status=a.get("status", ""),
        unit=a.get("unit", ""),
        writable=a.get("writable") == "true",
    )
    obj.children = [_decode_elem(c) for c in el]
    return obj


def encode_value(tag: str, value, name: str = "") -> bytes:
    """One oBIX value element, for a write.

    The tag is the caller's decision, not this function's: the station types
    the write by element name, so writing a numeric point needs <real>, and
    sending <str val="21.5"/> is a different request that will be refused or,
    worse, coerced.
    """
    if tag == "bool":
        v = "true" if (value is True or str(value).lower() == "true") else "false"
    elif tag in ("int", "real"):
        v = str(value)
    else:
        v = str(value)
    parts = [f'<{tag} xmlns="{OBIX_NS}"']
    if name:
        parts.append(f' name="{_xml_attr(name)}"')
    parts.append(f' val="{_xml_attr(v)}"/>')
    return "".join(parts).encode("utf-8")


def _xml_attr(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


class ObixClient:
    """A read-mostly oBIX client.

    Writes are refused unless the caller passes `allow_write=True` *and* the
    href matches `write_allowlist`. Two gates rather than one because the first
    is a deployment decision and the second is a scope decision, and an agent
    that can widen one should not be able to widen the other.
    """

    def __init__(self, base: str, user: str = "", password: str = "",
                 timeout: float = 20.0, verify_tls: bool = True,
                 allow_write: bool = False,
                 write_allowlist: list[str] | None = None,
                 max_bytes: int = 4_000_000, trace=None):
        self.base = self._normalise_base(base)
        self.timeout = timeout
        self.allow_write = allow_write
        self.write_allowlist = list(write_allowlist or [])
        self.max_bytes = max_bytes
        # A callable(method, url, sent, got) or None. It is how the SPEC-DERIVED
        # request bodies in watch.py become checkable against a real station:
        # what went out and what came back, and never a header.
        self.trace = trace
        # Credentials are held for the life of the process and never written
        # anywhere. The Authorization header is built per request rather than
        # stored, and nothing in this module logs a header.
        self._auth = None
        if user:
            # Sent pre-emptively on every request, and that is deliberate. A
            # station has two callback handlers registered on the HTTP Basic
            # scheme: the RFC 7617 one, which parses a header that is already
            # there and proceeds straight to the login module, and a
            # challenge-only one that answers 401 with Niagara's own AuthMessage
            # and a session-bound handshakeToken. Which one answers is decided
            # by the Authorization scheme token, and a `Basic` credential is
            # never Niagara's `HELLO` handshake token, so sending credentials up
            # front reaches the RFC 7617 handler every time. UTF-8 here is this
            # client's choice; the
            # station decodes with `new String(bytes)` and no charset argument,
            # so a non-ASCII password can disagree.  EVIDENCE.md §D.1
            token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
            self._auth = "Basic " + token
        self._ctx = None
        if self.base.startswith("https://"):
            self._ctx = ssl.create_default_context()
            if not verify_tls:
                # Offered because a JACE's out-of-the-box certificate is
                # self-signed and an engineer commissioning one has a real
                # reason to look past that. It is not the default, and the
                # server tool prints a warning when it is used.
                self._ctx.check_hostname = False
                self._ctx.verify_mode = ssl.CERT_NONE

    @staticmethod
    def _normalise_base(base: str) -> str:
        """Accept a host, a URL, or a URL that already ends in /obix.

        Nobody should have to remember which. What this must not do is let a
        caller point the tool at some other path on the station and call the
        result oBIX, so anything after the servlet path is dropped.
        """
        b = base.strip()
        if not re.match(r"^https?://", b):
            b = "http://" + b
        p = urllib.parse.urlsplit(b)
        if not p.netloc:
            raise ObixError(f"cannot read a host out of {base!r}")
        return f"{p.scheme}://{p.netloc}{SERVLET_PATH}"

    def url_for(self, href: str) -> str:
        """Resolve an href the way a lobby walk produces them.

        Station hrefs come back relative — `config/`, `about/` — and an agent
        will also paste absolute ones straight out of a previous reply. Both
        have to land on the same URL, and neither may escape the servlet path.
        """
        h = (href or "").strip()
        if not h:
            return self.base + "/"
        if re.match(r"^https?://", h):
            p = urllib.parse.urlsplit(h)
            own = urllib.parse.urlsplit(self.base)
            if (p.scheme, p.netloc) != (own.scheme, own.netloc):
                raise ObixError(
                    f"href {h} points at another host; this client only talks to "
                    f"{own.scheme}://{own.netloc}")
            path = p.path
            if not path.startswith(SERVLET_PATH):
                raise ObixError(f"href {h} is outside {SERVLET_PATH}")
            return f"{p.scheme}://{p.netloc}{path}" + (f"?{p.query}" if p.query else "")
        if h.startswith(SERVLET_PATH + "/") or h == SERVLET_PATH:
            own = urllib.parse.urlsplit(self.base)
            return f"{own.scheme}://{own.netloc}{h}"
        if h.startswith("/"):
            raise ObixError(f"href {h} is outside {SERVLET_PATH}")
        return f"{self.base}/{h.lstrip('/')}"

    # ------------------------------------------------------------- transport

    def request(self, method: str, href: str, body: bytes | None = None) -> str:
        url = self.url_for(href)
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Accept", "text/xml")
        if body is not None:
            req.add_header("Content-Type", "text/xml; charset=utf-8")
        if self._auth:
            req.add_header("Authorization", self._auth)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout,
                                        context=self._ctx) as resp:
                raw = resp.read(self.max_bytes + 1)
        except urllib.error.HTTPError as exc:
            self._traced(method, url, body, f"HTTP {exc.code} {exc.reason}")
            detail = ""
            try:
                detail = exc.read(2000).decode("utf-8", "replace").strip()
            except Exception:
                pass
            help_ = _STATUS_HELP.get(exc.code, "")
            # An <err> body is the station explaining itself, and is more use
            # than the status line, so it wins where both are present. It also
            # replaces the generic advice: a fault already names the cause, and
            # 403's advice in particular offers two readings, so printing it
            # next to "PermissionErr" would point at a licence that is fine.
            # 401 is the exception — the scheme has to be named, and a station
            # that answers 401 has not got as far as saying why.
            if detail.lstrip().startswith("<"):
                try:
                    decode(detail)
                except ObixFault as fault:
                    raise ObixError(f"HTTP {exc.code} from {url}: {fault}"
                                    + (f"\n{help_}" if exc.code == 401 else "")) from exc
                except ObixError:
                    pass
            raise ObixError(f"HTTP {exc.code} {exc.reason} from {url}"
                            + (f"\n{help_}" if help_ else "")
                            + (f"\nStation said: {detail[:400]}" if detail else "")) from exc
        except urllib.error.URLError as exc:
            raise ObixError(
                f"could not reach {url}: {exc.reason}. Check the station is up, that "
                f"its web service is on the port you used, and that the oBIX servlet "
                f"is served at {SERVLET_PATH}.") from exc
        except (socket.timeout, TimeoutError) as exc:
            raise ObixError(f"{url} did not answer within {self.timeout:g}s") from exc
        if len(raw) > self.max_bytes:
            raise ObixError(
                f"the reply from {url} is larger than {self.max_bytes} bytes. This is "
                f"usually a whole subtree rather than one object — read a child href "
                f"instead, or raise max_bytes deliberately.")
        text = raw.decode("utf-8", "replace")
        self._traced(method, url, body, text)
        return text

    def _traced(self, method, url, sent, got):
        if self.trace is None:
            return
        try:
            self.trace(method, url, (sent or b"").decode("utf-8", "replace"), got)
        except Exception:
            # A broken trace must not cost the caller its reply.
            pass

    # ------------------------------------------------------------ operations

    def doc_path(self, href: str = "") -> str:
        """The servlet path a href lands on — the base every href inside the
        reply is relative to.  EVIDENCE.md §N"""
        return urllib.parse.urlsplit(self.url_for(href)).path

    def read(self, href: str = "") -> ObixObject:
        """GET the object at href. BObixServer encodes the resolved target."""
        return decode(self.request(VERB_READ, href), self.doc_path(href))

    def lobby(self) -> ObixObject:
        return self.read("")

    def about(self) -> ObixObject:
        return self.read("about/")

    def write(self, href: str, tag: str, value, name: str = "") -> ObixObject:
        """PUT a value. Gated twice — see the class docstring.

        The gates are this bridge's, not the protocol's. `ObixUtils.serviceWrite`
        sets any resolvable property — `BComplex.set(prop, value, user)` — and
        consults no export descriptor and no allow-list; the only refusals in it
        are the station's own permission check on the target component's category
        (rethrown as `PermissionErr "<user> cannot write this object"`) and one
        hardcoded case for program `source`/`classFile`. So the absence of an
        oBIX export does not make a property safe, and the allowlist below is the
        fence precisely because the wire does not supply one.  EVIDENCE.md §E
        """
        if not self.allow_write:
            raise ObixError(
                "writes are disabled. This client is read-only unless it is started "
                "with writes enabled, which is a deliberate act: a write to a station "
                "moves plant.")
        target = self.url_for(href)
        if not self._allowed(target):
            raise ObixError(
                f"{href} is not in the write allowlist. Writing is enabled but scoped: "
                f"add a prefix that covers this href to widen it. Allowed now: "
                f"{', '.join(self.write_allowlist) or '(nothing)'}")
        return decode(self.request(VERB_WRITE, href, encode_value(tag, value, name)),
                      self.doc_path(href))

    def _allowed(self, absolute_url: str) -> bool:
        path = urllib.parse.urlsplit(absolute_url).path
        for prefix in self.write_allowlist:
            p = urllib.parse.urlsplit(self.url_for(prefix)).path
            if path == p or path.startswith(p.rstrip("/") + "/"):
                return True
        return False

    def invoke(self, href: str, body: bytes = b"") -> ObixObject:
        """POST to an op's href. The station routes POST to the invoke path."""
        return decode(self.request(VERB_INVOKE, href, body or b""),
                      self.doc_path(href))
