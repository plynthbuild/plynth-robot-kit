"""Ask the building: an MCP server through which an AI model asks a building questions as one robot.

    PYTHONPATH=src .venv/bin/python -m plynth.ask                     # Courier 2, in the running demo
    PYTHONPATH=src .venv/bin/python -m plynth.ask --robot runner-1
    GATEWAY_URL=https://gateway.example:8443 GATEWAY_ROBOT_ID=tug-07 GATEWAY_ROBOT_SECRET=... \\
        GATEWAY_TLS_PIN=sha256:... python -m plynth.ask                # a real building

It speaks MCP over stdio (one JSON-RPC message per line) and calls the robot API with that robot's own key, so
the model sees exactly what that robot may see, and nothing else. It reads, and it can talk to the building
(say: the building's own model answers, the rules decide; gateway.building), but it can't ask for a trip, open a
door or call an elevator. Those stay on the robot API, where the owner's rules apply and the fire alarm always
wins. Its calls are marked as an observer's, so asking about a robot never makes it look online. Standard
library only, like the rest of the gateway.

With --acts it's for a robot's own model, on the robot (Peter, 2026-10-28: robots "will have their own foundation
models and be able to make their own decisions"): it also asks for doors, rides and crossings and says what the robot
did, as the robot itself. The owner's rules decide each one, as for any robot's software.
"""
import argparse
import http.client
import json
import os
import sys
import time
import uuid
from pathlib import Path
from urllib.error import URLError
from urllib.parse import quote

from plynth.client import GatewayError, RobotClient

VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")  # MCP versions with a handshake, newest first
# MCP 2026-07-28 has none (the robotics review, 2026-10-29): each request names its version in _meta, server/discover
# says what's spoken, and every result says resultType; a list's says how long to keep it (ttlMs, cacheScope).
STATELESS, META = "2026-07-28", "io.modelcontextprotocol/"
UNSTRUCTURED = ("2025-03-26", "2024-11-05")  # before structuredContent (2025-06-18): the text alone
DEMO_KEYS = Path.home() / "Library/Application Support/Robot Gateway/demo.db.robots.json"  # the Dock app's demo (Plynth.app; its
# data folder kept its first name, so the demo's data stayed put when the app was renamed)
INSTRUCTIONS = ("You are asking a building's gateway questions as one robot, with that robot's own key, so you see "
                "only what it may see; call whoami to learn which robot and building. You can read, and say what the robot is "
                "here for (the building answers in plain words, and its decision is the rules'), but never act: "
                "trips, doors and elevators go through the robot API, and the fire alarm always wins. If the "
                "building doesn't answer, treat that as not authorized. Say times in the building's local time.")
READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
TALK = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}  # recorded
SAY = {"type": "object", "properties": {"text": {"type": "string", "description": "What the robot is here for or "
       "needs, in plain words (up to 500 characters)"}}, "required": ["text"]}
HOURS = {"type": "object", "properties": {"hours": {
    "type": "number", "description": "How far back to include ended trips, in hours (0 to 24, default 2)"}}}


def _hours(args):
    hours = args.get("hours", 2)
    if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not 0 <= hours <= 24:
        raise ValueError(f"hours must be a number from 0 to 24, not {hours!r}")
    return hours


WHERE = {"type": "object", "required": ["to"], "properties": {
    "to": {"type": "string", "description": "Where to: a door, elevator or handoff spot's ID (from 'places'), or a zone's "
                                            "name such as Robot Holding or Robots' Home"},
    "from": {"type": "string", "description": "Where from, if not where the robot is now: a door's ID, or floor,x,z"},
    "floor": {"type": "string", "description": "Which floor, where the place or zone is on more than one"}}}


FLOOR = {"type": "object", "properties": {"floor": {
    "type": "string", "description": "One floor, such as 4; leave it out for every floor this robot may reach"}}}


def _rules(robot, args):
    floor = args.get("floor")
    return robot.call("GET", "/v1/rules" + (f"?floor={quote(str(floor))}" if floor else ""))


def _trips(robot, args):
    """Every trip under way, and the newest ended ones within `hours`; `capped` says if older ones were left out."""
    return robot.call("GET", f"/v1/passages?since={time.time() - 3600 * _hours(args)}")


TOOLS = {  # name: (title, description, input schema, the robot API call)
    "whoami": ("Who am I", "Who this robot is in this building: name, model and maker; its company and that "
               "company's access (the building's own fleet, a contractor, a tenant or a visitor, and until when); "
               "its group and priority; and the building's clock.", None, lambda robot, args: robot.me()),
    "places": ("My doors and elevators", "This robot's doors and elevators: the floors each serves, who lets it "
               "through (automatic, a badge or a person), the owner's rules (days and hours), no-robot windows, "
               "closures, whether each is open to this robot right now and why not, and whether the robot fits. "
               "Use it for questions like 'can I go to floor 6?' or 'can I take the visitor elevators at noon?'.",
               None, lambda robot, args: robot.places()),
    "notices": ("What to plan around", "What this robot should plan around right now: closures, a fire alarm, an "
                "owner's pause, no-robot windows, devices out of service, keep-out zones such as a wet floor staff "
                "reported, and bookings that won't get through.", None, lambda robot, args: robot.notices()),
    "trips": ("My trips", "This robot's trips through doors and elevators: every one under way, and those that "
              "ended in the last `hours` hours (the newest 200; `capped` is true if older ones were left out), each "
              "with its state and, if refused, the reason and whether waiting would help.", HOURS, _trips),
    "bookings": ("My bookings", "This robot's booked trips not yet over, each with anything that would stop it.",
                 None, lambda robot, args: robot.visits()),
    "directions": ("The way there", "How to get somewhere in the building from where this robot is: the doors and "
                   "elevators to ask for in order, where to wait for each and where it comes out, the areas to keep "
                   "out of, and the same in plain words. Only through what this robot may use on the trip, judged "
                   "over its half hour: each passage's `waits` says what to wait for there (a line, a rule about to "
                   "open); if there's no way, why not, and when to ask again. Use it for 'how do I get to the service "
                   "elevators?' or 'which way to the lab?'.", WHERE,
                   lambda robot, args: robot.directions(args.get("to"), args.get("from"), args.get("floor"))),
    "say": ("Tell the building", "Say in plain words what this robot is here for or needs ('bringing lab samples to "
            "floor 2', 'what should I know?'). The building answers in plain words, with the rules' own decision: may "
            "it go, which way, why not. It opens nothing; trips still go through the robot API.", SAY,
            lambda robot, args: robot.say(args.get("text", ""))),
    "messages": ("What the building told me", "What the building has told this robot, such as a hazard staff just "
                 "reported on a floor it uses, newest last.", None, lambda robot, args: robot.messages()),
    "rules": ("The rules", "Every rule of the building for this robot, on its floors or one: its hallways' rules (keep "
              "right), the crossings it takes turns at, the owner's rules for areas at set hours (slow to a speed, quiet, "
              "don't stop, keep out), keep-out zones and closures, each in plain words with whether it's in force now, "
              "and its priority in a line. Use it for 'what are the rules on floor 4?' or 'may I stop here?'.",
              FLOOR, _rules),
}
TALKS = {"say"}  # recorded in the owner's log, so not read only; never destructive
SERVER = ("ask-the-building", INSTRUCTIONS, TOOLS, TALKS)  # name, instructions, tools, those not read only

# A robot's own model, on the robot (--acts; Peter, 2026-10-28: "robots will have their own foundation models and be
# able to make their own decisions, but we want plynth to be able to control access"): it also asks for each door, ride
# and crossing and says what the robot did, with the robot's own key, never as an observer. The building's rules decide
# each one, exactly as for any robot software; a person's assistant keeps the read-only server above.
PASSAGE = {"type": "object", "required": ["resource_id"], "properties": {
    "resource_id": {"type": "string", "description": "The door, elevator or crossing, by its ID from 'places' or "
                                                     "'directions'"},
    "passage_id": {"type": "string", "description": "Your own ID for this trip, 1-128 letters, digits, '.', '-' or "
                                                    "'_' (a UUID is best): ask again with the same one after no answer, "
                                                    "and it's the same trip, never a second; a new one for each new "
                                                    "trip. Left out, one is made"},
    "from_floor": {"type": "string", "description": "An elevator ride: the floor it gets on at"},
    "to_floor": {"type": "string", "description": "An elevator ride: the floor it gets off at"},
    "wait_in_line": {"type": "boolean", "description": "An elevator: wait in line rather than be refused while it's "
                                                       "busy"}}}
STEP = {"type": "object", "required": ["passage_id", "event"], "properties": {
    "passage_id": {"type": "string"},
    "event": {"type": "string", "enum": ["going", "passed", "ready", "boarded", "exited", "assisted"],
              "description": "going: read a crossing's clearance back within 20 seconds of CLEARED, only as the robot "
                             "starts through; passed: through a door or a "
                             "crossing; ready: at the elevator lobby on its turn; boarded, exited: into and out of a "
                             "car; assisted: a person helped it through"},
    "clearance": {"type": "string", "description": "going: the clearance's token, as CLEARED gave it in "
                                                   "clearance.token; a building refuses a readback without it"}}}
ONE = {"type": "object", "required": ["passage_id"], "properties": {"passage_id": {"type": "string"}}}


def _ask_for(robot, args):
    fields = {k: args[k] for k in ("from_floor", "to_floor", "wait_in_line") if args.get(k) is not None}
    return robot.request(args.get("passage_id") or str(uuid.uuid4()), args.get("resource_id"), **fields)


# A crossing's clearance is read back by whatever moves the robot, only as it moves (core: a clearance not read back in
# 20 s is void, so a robot that asked early or went quiet never holds the crossing; only one that said it's going can
# leave it closed, STUCK, for a person). The tool never says going for the model (the robotics review's follow-up,
# 2026-10-30: doing so let a model slower than the passage timeout close crossings): a model that takes more than a
# few seconds a turn leaves crossings to the robot's own motion software, which asks, reads back and moves.
ACTS = {
    "ask": ("Ask to go through", "Ask the building for a door, an elevator ride or a crossing, as this robot. The "
            "answer is the passage, with its passage_id and state; give your own passage_id to ask again safely. Move "
            "only on OPEN_COMMANDED (a door: go through, then report passed), CAR_ARRIVED (board and report boarded; "
            "get out at ARRIVED and report exited) or, at a crossing, once you've reported going on CLEARED and the "
            "answer is GOING (then report passed once through). CLEARED lasts 20 seconds: report going only as the robot "
            "starts through, and if you can't within that, let it lapse and ask again. A robot whose model takes "
            "longer than a few seconds a turn should leave crossings to its own motion software. IN_LINE: wait where "
            "you are, out of the way. DENIED: read reason_code, retry and retry_after.",
            PASSAGE, _ask_for),
    "report": ("Say what the robot did", "Tell the building what this robot just did in a passage: going (read a "
               "crossing's clearance back, with the clearance's token, within 20 seconds of CLEARED, as it starts "
               "through), passed, ready, "
               "boarded, exited or assisted. Only what it did.", STEP,
               lambda robot, args: robot.report(args.get("passage_id"), args.get("event"),
                                                clearance=args.get("clearance"))),
    "passage": ("A passage now", "One of this robot's passages, as it stands now, with expires_in_s: the seconds left "
                "before its state lapses on its own (a crossing's clearance: 20).", ONE,
                lambda robot, args: robot.status(args.get("passage_id"))),
    "withdraw": ("Withdraw", "Withdraw a passage this robot no longer needs, before it boards or goes in.", ONE,
                 lambda robot, args: robot.cancel(args.get("passage_id"))),
}
ACT_INSTRUCTIONS = ("You run one robot in a building, with that robot's own key. Read the building's rules ('rules') "
                    "and the way ('directions'), and ask the building for each door, elevator ride and crossing "
                    "('ask'): its rules decide. Move only when a passage says so, say truthfully what the robot did "
                    "('report'), and treat no answer as not authorized. The building's holds, an emergency and the "
                    "fire alarm always win, and the robot's own safety comes first. Say times in the building's local "
                    "time.")
ACTING = ("ask-the-building", ACT_INSTRUCTIONS, TOOLS | ACTS, TALKS | set(ACTS))
_peer = {"capabilities": {}, "lines": iter(()), "waiting": [], "asked": 0,  # the client on stdio, as serve() reads it
         "version": None}  # the version its handshake settled on, if it made one


def confirm(message):
    """Ask the person in their own client (MCP elicitation), so the model can't answer for them: True if they accept,
    False if they decline or close it, None if the client can't ask (a tool then needs the model's confirm: true)."""
    if "elicitation" not in _peer["capabilities"]:
        return None
    _peer["asked"] += 1
    mid = f"confirm-{_peer['asked']}"
    print(json.dumps({"jsonrpc": "2.0", "id": mid, "method": "elicitation/create", "params": {
        "message": message, "requestedSchema": {"type": "object", "properties": {}}}}), flush=True)
    for line in _peer["lines"]:  # anything else that comes meanwhile waits its turn
        try:
            answer = json.loads(line)
        except ValueError:
            answer = None
        if isinstance(answer, dict) and answer.get("id") == mid and "method" not in answer:
            return (answer.get("result") or {}).get("action") == "accept"
        _peer["waiting"].append(line)
    return False


def handle(message, connect, server=SERVER):
    """One JSON-RPC message in, its answer out, or None for a notification. connect() returns the client the tools
    call (a RobotClient here); `server` is (name, instructions, tools, those not read only), for plynth.ask_people."""
    label, instructions, tools, talks = server
    if not isinstance(message, dict):
        return _error(None, -32600, "not a JSON-RPC request")
    method, params, mid = message.get("method"), message.get("params") or {}, message.get("id")
    if mid is None:
        return None  # notifications (initialized, cancelled) take no answer
    if not isinstance(params, dict):
        return _error(mid, -32602, "params must be an object")
    name, meta = params.get("name"), params.get("_meta")
    asked = meta.get(f"{META}protocolVersion") if isinstance(meta, dict) else None  # 2026-07-28: on every request
    if asked is not None and asked not in (STATELESS, *VERSIONS):
        return _error(mid, -32022, f"unsupported protocol version: {asked!r}",
                      {"supported": [STATELESS, *VERSIONS], "requested": asked})
    me = {"name": label, "version": "0.1.0"}
    if method == "initialize":
        can = params.get("capabilities")
        _peer["capabilities"] = can if isinstance(can, dict) else {}
        wanted = params.get("protocolVersion")
        _peer["version"] = wanted if wanted in VERSIONS else VERSIONS[0]
        result = {"protocolVersion": _peer["version"], "capabilities": {"tools": {}}, "serverInfo": me,
                  "instructions": instructions}
    elif method == "server/discover":  # who it is goes in _meta (below), as 2026-07-28 has it; cacheable, as a list is
        result = {"supportedVersions": [STATELESS, *VERSIONS], "capabilities": {"tools": {}},
                  "instructions": instructions, "ttlMs": 3_600_000, "cacheScope": "public"}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [{"name": name, "title": title, "description": about,
                             "inputSchema": schema or {"type": "object"},
                             "annotations": TALK if name in talks else READ_ONLY}
                            for name, (title, about, schema, _) in tools.items()],
                  **({"ttlMs": 3_600_000, "cacheScope": "public"} if asked == STATELESS else {})}  # fixed for a run
    elif method == "tools/call" and isinstance(name, str) and name in tools:
        args = params.get("arguments") or {}
        structured = asked is not None or _peer.get("version") not in UNSTRUCTURED
        result = call(connect, name, args, tools, structured) if isinstance(args, dict) else _text(
            "Can't ask: arguments must be an object", error=True)
    elif method == "tools/call":
        return _error(mid, -32602, f"unknown tool: {name!r}")
    else:
        return _error(mid, -32601, f"unknown method: {method}")
    if asked == STATELESS or method == "server/discover":  # every result says it's complete, and who answered
        result |= {"resultType": "complete", "_meta": {f"{META}serverInfo": me}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def call(connect, name, args, tools=TOOLS, structured=True):
    """A tool's answer: the robot API's own JSON as text, and beside it, from MCP 2025-06-18 on, as structuredContent
    (an object there: a list as {"result": [...]}), which a planner's function calling reads best."""
    try:
        answer = tools[name][3](connect(), args)
    except GatewayError as e:
        return _text(f"The building refused: {e}", error=True)
    except (URLError, OSError, http.client.HTTPException) as e:  # no answer, or one cut off halfway
        return _text(f"The building didn't answer ({e!r}). Treat that as not authorized.", error=True)
    except (KeyError, ValueError) as e:
        return _text(f"Can't ask: {e}", error=True)
    return _text(json.dumps(answer, indent=1)) | (
        {"structuredContent": answer if isinstance(answer, dict) else {"result": answer}} if structured else {})


def respond(message, connect, server=SERVER):
    """handle(), with JSON-RPC's internal error for anything unforeseen, so one message never stops the server."""
    try:
        return handle(message, connect, server)
    except Exception as e:  # the last resort: answer and log it, never exit (eng review R5)
        print(f"{server[0]}: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        return _error(message.get("id") if isinstance(message, dict) else None, -32603,
                      f"internal error: {type(e).__name__}")


def _error(mid, code, text, data=None):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": text, **({"data": data} if data else {})}}


def _text(text, error=False):
    return {"content": [{"type": "text", "text": text}], **({"isError": True} if error else {})}


def connector(robot_id, keys, observer=True):
    """A real building's robot comes from the environment. The demo's robots get new keys every start, so each
    call reads this run's from the file the demo writes beside its database. observer: False for a robot's own model
    (--acts), whose calls are the robot's own."""
    if secret := os.environ.get("GATEWAY_ROBOT_SECRET"):
        robot = RobotClient(os.environ["GATEWAY_URL"], os.environ.get("GATEWAY_ROBOT_ID", robot_id), secret,
                            pin=os.environ.get("GATEWAY_TLS_PIN"), observer=observer)
        return lambda: robot

    def demo():
        try:
            run = json.loads(Path(keys).read_text())
        except FileNotFoundError:
            raise ValueError("no robot to ask as: set GATEWAY_URL, GATEWAY_ROBOT_ID and GATEWAY_ROBOT_SECRET for your "
                             "robot, or open the Plynth app for its demo") from None
        return RobotClient(run["url"], robot_id, run["robots"][robot_id], observer=observer)
    return demo


def main():
    parser = argparse.ArgumentParser(prog="python -m plynth.ask", description=__doc__.splitlines()[0])
    parser.add_argument("--robot", default="parcel-2", help="which demo robot to ask as (default Courier 2)")
    parser.add_argument("--keys", default=os.environ.get("GATEWAY_DEMO_KEYS", DEMO_KEYS),
                        help="the demo's keys file (default: the Dock app's)")
    parser.add_argument("--acts", action="store_true", help="for the robot's own model, on the robot: it may also ask "
                        "for doors, elevator rides and crossings, and say what the robot did")
    args = parser.parse_args()
    serve(connector(args.robot, args.keys, observer=not args.acts), ACTING if args.acts else SERVER)


def serve(connect, server=SERVER, stdin=None):
    """MCP over stdio until stdin closes."""
    lines = iter(stdin if stdin is not None else sys.stdin)
    _peer.update(lines=lines, waiting=[], capabilities={}, version=None)
    while (line := _peer["waiting"].pop(0) if _peer["waiting"] else next(lines, None)) is not None:
        if not line.strip():  # stdout carries only JSON-RPC; anything else goes to stderr
            continue
        try:
            message = json.loads(line)
        except ValueError:
            answer = _error(None, -32700, "not JSON")
        else:
            answer = respond(message, connect, server)
        if answer:
            print(json.dumps(answer), flush=True)


if __name__ == "__main__":
    main()
