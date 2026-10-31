"""A small robot client for the gateway's robot API: the reference for robot companies' engineers.

    import uuid
    from plynth.client import RobotClient, GatewayError

    robot = RobotClient("https://gateway.hospital.example:8443", "tug-07", secret)
    passage_id = str(uuid.uuid4())  # a new ID for each trip
    status = robot.request(passage_id, "lift-a", from_floor="1", to_floor="4")
    if status["state"] == "DENIED":
        ...  # status["reason_code"] says why; status["retry"] says whether waiting can help
    for event, data in robot.stream():  # or poll robot.status(passage_id) about once a second
        if event == "passage" and data["state"] == "CAR_ARRIVED":
            ...  # board car data["car"], then robot.report(passage_id, "boarded")

Standard library only, so it can be copied into a fleet's own code as it stands. Every call but health is
signed with the robot's own secret: hex HMAC-SHA256 over "METHOD\\nPATH\\nTIMESTAMP\\nNONCE\\nAUDIENCE\\n"
followed by the exact body bytes, sent as X-Robot-Id, X-Timestamp (unix seconds), X-Nonce (new for each call),
X-Audience (the gateway's host, as in the URL you give) and X-Signature. A copy of a call is refused, and so is a
call signed for another building; a building requires both, and the sandbox takes calls without them too. A gateway
older than them is signed for without them (call). Keep the robot's clock on NTP: a request more than 30 seconds off
the gateway's clock is refused as a replay.

A one-way key instead (the building keeps only its public half): pass, as `secret`, the robot's Ed25519 private
key, as ssh-keygen writes it or in PEM (this needs the cryptography library), or a function that signs bytes and returns the 64-byte
signature, such as the robot's own key store or security chip, so the private key never sits in Python at all.
An ECDSA P-256 private key works the same way (the curve TPMs and security chips use, and a maker's device
certificate carries): signed over SHA-256, the signature DER, or r and s side by side, 64 bytes, from a chip.
The same string is signed; X-Signature carries the signature in hex.

Fail closed: if the gateway doesn't answer within your timeout, treat it as "not authorized" and use your
fallback. A refusal is not an error: it comes back as a status with state DENIED. Errors (401, 404, 409, ...)
raise GatewayError.

Verify the gateway: TLS is the robot's only proof that the answer came from the building. Pass the fingerprint
the building gives you, pin="sha256:<hex>" (best for a site's self-signed certificate), or cafile= the site's
certificate authority. A certificate that doesn't match fails before anything is sent, as urllib.error.URLError
whose reason is an ssl.SSLCertVerificationError. Plain http:// is refused anywhere but loopback (the sandbox).
"""
import hashlib
import hmac
import http.client
import json
import secrets
import select
import ssl
import threading
import time
import urllib.error
import urllib.parse

LOOPBACK = ("127.0.0.1", "localhost", "::1")
IDLE_S = 5  # reuse a connection idle at most this long; the gateway drops one idle 10 s


class GatewayError(Exception):
    """The gateway answered with an HTTP error. `status` is the code; `body` is its JSON, e.g. {"error": "...",
    "code": "..."}; `retry_after` is the seconds a 429 asks you to wait (its Retry-After), else None. No answer at all,
    or a 502, 503 or 504 from something in between rather than the gateway, raises URLError (an OSError) instead."""

    def __init__(self, status, body, retry_after=None):
        super().__init__(f"HTTP {status}: {body.get('error', body) if isinstance(body, dict) else body}")
        self.status, self.body, self.retry_after = status, body, retry_after


class _PinnedConnection(http.client.HTTPSConnection):
    """Checks the gateway's certificate against the pin right after the handshake, before a byte of the request."""

    def __init__(self, *args, pin=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.pin = pin

    def connect(self):
        super().connect()
        if not self.pin:
            return
        seen = "sha256:" + hashlib.sha256(self.sock.getpeercert(binary_form=True)).hexdigest()
        if seen not in self.pin.split(","):  # fingerprints are public, so a plain comparison is fine
            self.sock.close()
            raise ssl.SSLCertVerificationError(ssl.SSL_ERROR_SSL,
                f"the gateway's certificate is {seen}, not the pinned {self.pin}: this may not be the building's "
                "gateway, so nothing was sent. If the building changed its certificate, get the new fingerprint from it.")


class RobotClient:
    def __init__(self, base_url, robot_id, secret, timeout=10, context=None, pin=None, cafile=None, observer=False,
                 test_run=None, wait_429=10):
        """timeout: seconds to wait for an answer (more than the slowest elevator command at your site).
        observer: True for software that only reads on the robot's behalf (an MCP server, a dashboard): its calls
        don't count as the robot being online.
        test_run: a label sent as X-Test-Run on every call, so the building's record marks these passages as a test
        (the conformance kit sends "conformance"); the sandbox's grade leaves them out.
        wait_429: the seconds a call (or a stream's connecting) may spend waiting out 429s, each for its Retry-After,
        before it's sent again, as the guide says to; a 429 means the gateway did nothing with it. Past that, or with
        0, the GatewayError is raised, with retry_after.
        pin: the gateway certificate's fingerprint from the building, "sha256:<hex>" (colons and case don't matter);
        while the building changes its certificate, the old and the new one, comma-separated.
        cafile: the site's own certificate authority; the gateway's name is checked too.
        context: an ssl.SSLContext of your own, instead of cafile."""
        url = urllib.parse.urlsplit(base_url)
        if url.scheme != "https" and url.hostname not in LOOPBACK:
            raise ValueError(f"{base_url}: use https:// off loopback, verified with pin= or cafile=")
        if cafile:
            context = ssl.create_default_context(cafile=cafile)
        elif pin and context is None:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname, context.verify_mode = False, ssl.CERT_NONE  # the pin is the check
        if isinstance(secret, str) and secret.lstrip().startswith("-----BEGIN"):  # a one-way key's private half:
            from cryptography.hazmat.primitives import serialization  # as ssh-keygen writes it, or PEM
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.asymmetric import ec
            load = (serialization.load_ssh_private_key if "OPENSSH PRIVATE KEY" in secret
                    else serialization.load_pem_private_key)
            key = load(secret.encode(), password=None)
            secret = ((lambda message: key.sign(message, ec.ECDSA(hashes.SHA256())))
                      if isinstance(key, ec.EllipticCurvePrivateKey) else key.sign)
        self.base_url, self.robot_id, self.secret = base_url.rstrip("/"), robot_id, secret
        self.audience = url.netloc.rpartition("@")[2]  # the gateway's host as this robot reaches it, port included
        self.bound = True  # each call signs a nonce and the audience; False once a gateway older than them answers (call)
        self.pin = pin and ",".join("sha256:" + p.strip().lower().removeprefix("sha256:").replace(":", "")
                                    for p in pin.split(","))
        self.timeout, self.context, self.observer, self.test_run = timeout, context, observer, test_run
        self.wait_429 = wait_429
        self._url, self._local = url, threading.local()  # each thread keeps its own connection, call after call
        self.last_event_id = None  # the stream's last event, to pick up from after a reconnect (stream(after=...))

    # --- passages: one robot through one door, or one elevator ride ---

    def request(self, passage_id, resource_id, **fields):
        """Ask to pass. Pick a new passage_id per trip (a UUID is best); resending the same request is safe.
        For an elevator, send from_floor and to_floor; ride_alone=True asks for a car to itself, where allowed.
        For a crossing, approach= the side the robot waits on (its directions' passage gives it), its line there."""
        return self.call("POST", "/v1/passages", {"passage_id": passage_id, "resource_id": resource_id, **fields})

    def status(self, passage_id):
        return self.call("GET", f"/v1/passages/{passage_id}")

    def report(self, passage_id, event, clearance=None):
        """"boarded" or "exited" for a ride, "passed" for a door or a crossing. Only your report completes a passage.
        At a crossing's CLEARED, "going" with clearance=status["clearance"]["token"] reads the clearance back; move
        only once the answer is GOING."""
        return self.call("POST", f"/v1/passages/{passage_id}/report",
                         {"event": event, **({"clearance": clearance} if clearance is not None else {})})

    def presence(self, passage_id, code):
        """At a high-security door that asks for proof the robot is there (state AWAITING_PRESENCE): the door's code,
        as its beacon shows it now. Or send it as door_code in request() itself."""
        return self.call("POST", f"/v1/passages/{passage_id}/presence", {"code": code})

    def ask_grant(self, doors, work_order=None, note="", minutes=None):
        """A grant for one trip through high-security doors that ask for one: a person at the building approves it,
        then each door opens once on it. Poll grants() for its status."""
        return self.call("POST", "/v1/grants", {"doors": doors, "note": note,
                                                **({"work_order": work_order} if work_order else {}),
                                                **({"minutes": minutes} if minutes else {})})

    def grants(self):
        return self.call("GET", "/v1/grants")["grants"]

    def passages(self, since=None):
        """This robot's passages not yet ended, as statuses; with `since` (unix time, a day back at most) also those
        that ended since. Read it after a restart, and carry on with each one under way."""
        return self.call("GET", "/v1/passages" + (f"?since={since}" if since is not None else ""))["passages"]

    def cancel(self, passage_id):
        """Withdraw before boarding; a standing car call is released at once. Aboard, report "exited" instead."""
        return self.call("POST", f"/v1/passages/{passage_id}/cancel")

    # --- the building ---

    def heartbeat(self, passage_id):
        """Still here: send every second or two while in a car or a doorway (see Gateway.heartbeat)."""
        return self.call("POST", f"/v1/passages/{passage_id}/heartbeat")

    def me(self):
        """Who this robot is here: name, model, maker, group and its priority, company and its access, and the
        gateway's clock (`time`) to check yours against."""
        return self.call("GET", "/v1/me")

    def visits(self):
        """This robot's bookings not cancelled and not over, each with what would stop it (`conflicts`)."""
        return self.call("GET", "/v1/visits")["visits"]

    def about(self):
        """What answers at the other end: {"release", "api"} from GET /v1/health's headers (the gateway's release tag
        and the API version it serves), for a test report. Unsigned, like health."""
        conn = self._connection()
        try:
            response = self._send(conn, "GET", "/v1/health", None, False)
            response.read()
        except BaseException:
            conn.close()
            raise
        return {"release": response.getheader("X-Gateway-Release"), "api": response.getheader("X-Api-Version")}

    def places(self):
        """This robot's doors and elevators, with their floors, rules, windows and closures, to plan a route."""
        return self.call("GET", "/v1/places")

    def send_profile(self, **sizes):
        """This robot's size and loads, each optional: length_m, width_m, height_m, weight_kg, payload_kg. Each door
        and car is checked against them; places() then says per place whether it fits."""
        return self.call("POST", "/v1/profile", sizes)

    def profile(self):
        return self.call("GET", "/v1/profile")

    def notices(self):
        """What to plan around now: closures, a pause, a fire alarm, no-robot windows, devices out of service."""
        return self.call("GET", "/v1/notices")["notices"]

    def say(self, text):
        """Tell the building in plain words what this robot is here for or needs; it answers in plain words. Act on
        `decision` (may it go, which way, why not), never on the words: the door still asks the rules."""
        return self.call("POST", "/v1/say", {"text": text})

    def messages(self, after=0):
        """What the building has told this robot since message `after`, such as a hazard staff reported."""
        return self.call("GET", f"/v1/messages?after={after}")["messages"]

    def front_desk(self):
        """The front desk's form: what it asks for a delivery, a pickup or maintenance, and the values it takes."""
        return self.call("GET", "/v1/front-desk")

    def intake(self, **manifest):
        """Arrive at the front desk with the form filled in from the order (task, what, unit, contents, size: any of
        them). Complete, the reply is an `outcome`; else a `question` to answer."""
        return self.call("POST", "/v1/intake", manifest)

    def answer(self, intake_id, value):
        """Answer the front desk's question: plain words, a choice it offered, or for size {"weight_kg", "longest_cm"}."""
        return self.call("POST", f"/v1/intake/{intake_id}", {"answer": value})

    def ask_pass(self, floors, note=""):
        """A visitor company's robot asks for a pass for one trip."""
        return self.call("POST", "/v1/passes", {"floors": floors, "note": note})

    # --- handoffs: goods left at a handoff spot for another company's robots ---

    def handoff(self, spot, for_company, kind=None):
        """About to leave goods at a handoff spot for `for_company`'s robots; `kind` from the purposes list. Nothing
        else: a note, an order or a patient detail is refused (400). Returns the handoff, with its id."""
        return self.call("POST", "/v1/handoffs", {"spot": spot, "for_company": for_company,
                                                  **({"kind": kind} if kind else {})})

    def left(self, handoff_id):
        """Put down at the spot: the company it's for is told in its notices."""
        return self.call("POST", f"/v1/handoffs/{handoff_id}/left")

    def collected(self, handoff_id):
        """Picked up, by a robot of the company it was left for."""
        return self.call("POST", f"/v1/handoffs/{handoff_id}/collected")

    def handoffs(self):
        """Handoffs waiting for this robot's company, and those it left in the last day."""
        return self.call("GET", "/v1/handoffs")["handoffs"]

    def confirm(self, emergency_id, action, eta_s=None, text=None):
        """Answer an emergency notice: "going" (eta_s, seconds to its target), "holding", "cannot" (text, why), then
        "arrived". Optional to build; the building shows the owner a robot that says it confirms and doesn't."""
        body = {"action": action, **({"eta_s": eta_s} if eta_s is not None else {}), **({"text": text} if text else {})}
        return self.call("POST", f"/v1/emergency/{emergency_id}/ack", body)

    def drill(self, kind="fire alarm", **where):
        """The sandbox only: start an emergency drill (kind, and floor or area for one of a floor) to test answers
        against. A building answers 404: it runs its own drills."""
        return self.call("POST", "/v1/drills", {"kind": kind, **where})

    def grade(self, since=None):
        """The sandbox only: this robot's own software graded on what the sandbox saw it do since `since` (unix time,
        a day back at most; an hour by default). Every check passed: a certificate, sandbox-verified."""
        return self.call("POST", "/v1/sandbox/grade", {} if since is None else {"since": since})

    def end_drills(self):
        """The sandbox only: end every drill now; each robot hears all clear."""
        return self.call("POST", "/v1/drills/end")

    def directions(self, to, frm=None, floor=None):
        """The way to `to` (a door, elevator or spot's ID, or a zone's name) from where the robot is, or `frm`: the
        passages to ask for in order, each with where to wait and where it comes out, where the place is, the areas to
        keep out of, and the same in plain words; or why not (refused, reason_code)."""
        query = urllib.parse.urlencode({k: v for k, v in (("to", to), ("from", frm), ("floor", floor)) if v is not None})
        return self.call("GET", f"/v1/directions?{query}")

    def position(self, floor, x, z):
        """Optional: where the robot is now, x and z from 0 to 1 across the floor plate. Never logged."""
        return self.call("POST", "/v1/position", {"floor": floor, "x": x, "z": z})

    def online(self, battery=None, state=None, software=None):
        """Still here, between other calls (about every 30 s), with the battery (0 to 1) and state (idle, working,
        charging or error) if you have them. Any signed call counts; 60 s without one shows the robot offline.
        software: the robot's software version (40 characters at most); when it changes, the owner is asked to
        rerun its conformance test."""
        body = {k: v for k, v in (("battery", battery), ("state", state), ("software", software)) if v is not None}
        return self.call("POST", "/v1/heartbeat", body)
    def map_position(self, floor, map_x, map_y):
        """Optional: where the robot is in your fleet's own map, in metres, once the building has placed that map
        on its floor plate (a frame for your company, on this floor or every floor). Never logged."""
        return self.call("POST", "/v1/position", {"floor": floor, "map_x": map_x, "map_y": map_y})

    def health(self):
        return self.call("GET", "/v1/health", signed=False)

    def stream(self, timeout=5, after=None):
        """Yield (event, data) as they happen: "passage" (a step of one of this robot's passages, shaped like
        a status) and "notices" (the whole list, at once on connecting and whenever it changes). The gateway
        sends a keep-alive every 2 s, so `timeout` seconds of silence means the connection is gone: reconnect.
        after: pick up where a dropped stream left off; the steps missed since come first (at most 500 of the last
        day). Each event's ID is kept in self.last_event_id, so a robot reconnects with:

            while True:
                try:
                    for event, data in robot.stream(after=robot.last_event_id): ...
                except OSError:  # dropped, timed out or no answer (a proxy's 502 too): reconnect
                    time.sleep(1)  # a GatewayError is the gateway's own answer: 401 or 403 means stop
        """
        headers = {"Last-Event-ID": str(after)} if after is not None else {}
        conn = self._connect(timeout)  # its own connection: the stream holds it until it ends
        try:
            response, waited = self._send(conn, "GET", "/v1/stream", None, True, None, headers), 0
            while response.status >= 400:  # a 429 waited out within wait_429, as call() does; anything else raises
                data, after = response.read(), response.getheader("Retry-After") or ""
                wait = int(after) if response.status == 429 and after.isdigit() else None
                if wait is None or waited + wait > self.wait_429:
                    self._answer(response, data)  # raises GatewayError, or URLError for no answer
                time.sleep(wait)
                waited += wait
                conn.close()
                conn = self._connect(timeout)
                response = self._send(conn, "GET", "/v1/stream", None, True, None, headers)
            event, data, event_id = None, [], None
            for line in response:
                line = line.decode().rstrip("\r\n")
                if not line:  # a blank line ends an event
                    if event and data:
                        if event_id is not None:
                            self.last_event_id = event_id
                        yield event, json.loads("\n".join(data))
                    event, data, event_id = None, [], None
                elif line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    data.append(line[5:].strip())
                elif line.startswith("id:"):
                    event_id = int(line[3:].strip())
                # a line starting with ":" is a keep-alive comment
        except http.client.HTTPException as e:  # cut off mid-event: as a dropped stream, so the robot reconnects
            raise urllib.error.URLError(e) from None
        finally:
            conn.close()

    # --- the wire ---

    def call(self, method, path, body=None, signed=True, timestamp=None, key=None):
        """One signed call; returns the JSON answer or raises GatewayError. `timestamp` is for tests only.
        Calls from one thread reuse its connection while the gateway keeps it open, so a check-in every second or
        two makes no new handshake. Nothing is ever resent, but after a 429, which the gateway did nothing with: it's
        sent again after its Retry-After while the waiting stays within wait_429 seconds. And once after a gateway
        older than X-Nonce and X-Audience refuses them, as a signature mismatch it did nothing with and without the
        "nonces" a newer gateway's 401 carries: signed without them, and if that works, so is every call after
        (stream and fetch too). A wrong key stays a mismatch.
        ponytail: for the sandbox until its first release that takes them (kit 0.5.0 and older sign without); drop it
        once no gateway is older.
        key: an Idempotency-Key (a UUID) for a call that makes something: help, a report, a charger, a permit, an
        incident, a booking, a handoff, a pass, the front desk's form. Sent again with the same key within a day, after
        a timeout say, the call is answered as the first was, and nothing is made twice."""
        waited = 0
        while True:
            try:
                return self._call_once(method, path, body, signed, timestamp, key)
            except GatewayError as e:
                if (signed and self.bound and e.status == 401 and isinstance(e.body, dict)
                        and e.body.get("code") == "signature_mismatch" and not e.body.get("nonces")):
                    self.bound = False
                    try:
                        return self._call_once(method, path, body, signed, timestamp, key)
                    except GatewayError:
                        self.bound = True  # not that: a wrong key, or a building's refusal of the old way
                        raise e from None
                if e.status != 429 or e.retry_after is None or waited + e.retry_after > self.wait_429:
                    raise
                time.sleep(e.retry_after)
                waited += e.retry_after

    def _call_once(self, method, path, body, signed, timestamp, key=None):
        conn = self._connection()
        try:
            response = self._send(conn, method, path, body, signed, timestamp, {"Idempotency-Key": key} if key else None)
            data = response.read()
        except urllib.error.URLError:
            conn.close()
            raise
        except (http.client.HTTPException, OSError) as e:  # cut off halfway, or timed out: as a dropped connection
            conn.close()
            raise urllib.error.URLError(e) from None
        except BaseException:
            conn.close()  # its state is unknown: the next call opens a fresh one
            raise
        self._local.used = time.monotonic()
        return self._answer(response, data)

    def _connection(self):
        """This thread's connection. One idle past IDLE_S, or one the gateway has already closed (readable while idle
        means it hung up: a restart, its idle limit), is closed first, so the next call reconnects instead of failing."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._local.conn = self._connect(self.timeout)
        elif conn.sock is not None and (time.monotonic() - getattr(self._local, "used", 0) > IDLE_S
                                        or select.select([conn.sock], [], [], 0)[0]):
            conn.close()  # http.client reconnects on the next request, checking the pin again
        return conn

    def _connect(self, timeout):
        host, port = self._url.hostname, self._url.port
        if self._url.scheme == "https":
            return _PinnedConnection(host, port, timeout=timeout, context=self.context, pin=self.pin)
        return http.client.HTTPConnection(host, port, timeout=timeout)

    def _send(self, conn, method, path, body, signed=True, timestamp=None, headers=None):
        raw = json.dumps(body).encode() if body is not None else b""
        headers = (headers or {}) | ({"Content-Type": "application/json"} if raw else {}) | (
            {"X-Robot-Observer": "1"} if self.observer else {}) | ({"X-Test-Run": self.test_run} if self.test_run else {})
        path = self._url.path.rstrip("/") + path  # what's sent is what's signed, a prefix in the gateway's URL too
        if signed:
            ts, nonce = str(int(time.time()) if timestamp is None else timestamp), secrets.token_hex(16)
            bound = f"{nonce}\n{self.audience}\n" if self.bound else ""  # without, as 0.5.0 and older sign: an older gateway
            message = f"{method}\n{path}\n{ts}\n{bound}".encode() + raw
            headers |= {"X-Robot-Id": self.robot_id, "X-Timestamp": ts,
                        **({"X-Nonce": nonce, "X-Audience": self.audience} if self.bound else {}),
                        "X-Signature": self.secret(message).hex() if callable(self.secret)
                        else hmac.new(self.secret.encode(), message, hashlib.sha256).hexdigest()}
        try:
            conn.request(method, path, body=raw if method == "POST" else None, headers=headers)
        except OSError as e:  # couldn't connect, or the pin didn't match: nothing reached the gateway
            raise urllib.error.URLError(e) from None
        try:
            return conn.getresponse()
        except (http.client.HTTPException, OSError) as e:  # no answer in time, or a garbled or cut-off one: URLError
            raise urllib.error.URLError(e) from None

    def fetch(self, path):
        """A file the gateway gives this robot, signed like any call: a trip package's route or map, a keep-out mask
        (GET /v1/directions/<id>/floors/<floor>/..., GET /v1/ros/<floor>/...). Its bytes, or GatewayError."""
        conn = self._connection()
        try:
            response = self._send(conn, "GET", path, None)
            data = response.read()
        except urllib.error.URLError:
            conn.close()
            raise
        except (http.client.HTTPException, OSError) as e:  # cut off halfway, or timed out: as a dropped connection
            conn.close()
            raise urllib.error.URLError(e) from None
        except BaseException:
            conn.close()
            raise
        self._local.used = time.monotonic()
        return self._answer(response, data) if response.status >= 400 else data

    @staticmethod
    def _answer(response, data):
        if response.status >= 400:
            try:
                body = json.loads(data)
            except ValueError:
                body = data.decode(errors="replace")
            if response.status in (502, 503, 504) and not (isinstance(body, dict) and "request_id" in body):
                # a proxy or the site's network answering for a gateway it couldn't reach: no answer, not a refusal
                raise urllib.error.URLError(f"no answer from the gateway (HTTP {response.status} from in between)")
            after = response.getheader("Retry-After") or ""
            raise GatewayError(response.status, body, int(after) if after.isdigit() else None)
        try:
            return json.loads(data or b"null")
        except ValueError:  # not the gateway's JSON (a proxy's page in between?): a call that failed, not an answer
            raise urllib.error.URLError(f"the answer to HTTP {response.status} wasn't JSON") from None


class PersonClient(RobotClient):
    """One of the building's own people on the operator side (/ops), under their own sign-in, for software that acts
    for that person alone, such as their own AI's MCP server (plynth.ask_people): it sees what their role sees and
    nothing more. token: a session from signing in; or person and password, to sign in on the first call and again
    when the session ends (a session lasts one shift). The same TLS checks as a robot's: pin= or cafile=."""

    def __init__(self, base_url, token=None, person=None, password=None, **kwargs):
        super().__init__(base_url, person or "", "", **kwargs)
        self.token, self._login = token, (person, password)

    def _send(self, conn, method, path, body, signed=True, timestamp=None, headers=None):
        auth = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        return super()._send(conn, method, path, body, False, timestamp, (headers or {}) | auth)

    def sign_in(self):
        self.token = None
        self.token = super().call("POST", "/ops/signin", {"id": self._login[0], "password": self._login[1]})["token"]

    def call(self, method, path, body=None, signed=False, timestamp=None):
        if self.token is None and self._login[1]:
            self.sign_in()
        try:
            return super().call(method, path, body, False, timestamp)
        except GatewayError as e:
            if e.status != 401 or not self._login[1]:
                raise
            self.sign_in()  # the shift's session ended: once more, under a new one
            return super().call(method, path, body, False, timestamp)
