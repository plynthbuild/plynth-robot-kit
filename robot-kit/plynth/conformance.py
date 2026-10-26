"""The robot side of the onboarding test: run as a robot against any gateway, the sandbox or a real building.

    python -m plynth.conformance --env ../plynth-sandbox.env     # the sandbox: its door and elevator by default
    GATEWAY_URL=URL GATEWAY_ROBOT_ID=ID GATEWAY_ROBOT_SECRET=S python -m plynth.conformance \\
        --door DOOR --elevator LIFT --from 1 --to 2 [--pin sha256:HEX | --cafile site-ca.pem] [--report out.json]

In the test kit it's `python3 -m plynth.conformance`. The address, robot ID and secret come from GATEWAY_URL,
GATEWAY_ROBOT_ID and GATEWAY_ROBOT_SECRET, or --env with the file your company's page gives you (or --url, --robot;
--secret still works, but a flag shows in `ps` and shell history). In a building, name the door, elevator and floors:
there it opens real ones. Against the sandbox they default to its door, its elevator and a ride from 1 to 2.
Off loopback the gateway must be HTTPS, verified by the fingerprint the building gives you (--pin, or
GATEWAY_TLS_PIN) or by the site's certificate authority (--cafile); with a mismatch every call fails and nothing is sent.

It checks the things a robot company's software must handle before any site visit: signing, replay refusal,
a door passage, safe retries, a full elevator ride followed on the push stream, cancel, reason codes on every
refusal, notices, the stream itself, and picking up after a reconnect. It prints a PASS/FAIL table, writes a JSON report with each check's
evidence for the go-live test report, and exits non-zero on any failure.

Against the sandbox, at its end, it tells the sandbox whether every check passed (true or false, nothing else), so
the sandbox can count how many runs pass.

Against the sandbox it also runs the emergency drills: a fire alarm (told on the stream,
held at every door, confirmed, all clear, and asking again gets through), a go-to-zone drill (sent to the holding zone
on its floor, going, arrived), and three a data center needs: a clean-agent pre-discharge in the sandbox's server
room (told its countdown, confirmed within 5 s), an emergency power off there (clear and hold), and a doorway dwell
(stopped in a no-stop area, told to move on). The kit answers for the robot unless --robot-answers: then your robot's
own software must answer each drill within its deadline, which is the test that counts. A building runs its own drills,
so there they're skipped (SKIP, not a failure); ask the site to run its fire drill with your robot there.

Against a real building it really opens the door and calls the car: run it with the robot at the door and the
elevator lobby, and someone from the site watching. It reports "boarded" and "exited" for the robot.
"""
import argparse
import datetime
import json
import os
import ssl
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from plynth.client import LOOPBACK, GatewayError, RobotClient
from plynth.hello import env_file  # one reader of plynth-sandbox.env, which says so in one line when it isn't there

UNKNOWN_PLACE = "conformance-no-such-place"
SANDBOX_ROOM = ("server-room", "2", 0.8, 0.8)  # the sandbox's gas-protected room, and a spot inside it


class Skipped(Exception):
    """A check that can't run here, and why. Not a failure."""


def _id(what):
    return f"conformance-{uuid.uuid4().hex[:12]}-{what}"


def _expect(ok, why):
    if not ok:
        raise AssertionError(why)


def _refused(fn, code):
    """Call fn; it must raise GatewayError with HTTP `code`."""
    try:
        answer = fn()
    except GatewayError as e:
        _expect(e.status == code, f"expected HTTP {code}, got {e.status} {e.body}")
        return {"http": e.status, "body": e.body}
    raise AssertionError(f"expected HTTP {code}, got 200 {answer}")


def _coded(status):
    _expect(status.get("reason_code") and "retry" in status and "retry_after" in status,
            f"a refusal without reason_code, retry and retry_after: {status}")


def _follow(events, passage_id, want):
    """Read the stream until this passage reaches `want` (or ends some other way). Returns that step.
    The gateway's own timeout ends a stuck passage, so this never waits for ever."""
    for event, data in events:
        if event == "passage" and data["passage_id"] == passage_id:
            if data["state"] == want:
                return data
            _expect(data["state"] not in ("DENIED", "FAILED", "CANCELLED", "RELEASED", "PASSED"),
                    f"waiting for {want}, the passage ended {data['state']}: {data['reason']}")
    raise AssertionError(f"the stream closed before {want}")


def _open_stream(robot):
    """Connect and take the first event, the notices, so nothing that follows is missed."""
    events = robot.stream()
    event, data = next(events)
    _expect(event == "notices", f"the stream should start with the notices, got {event}")
    return events


def check_tls(robot, a):
    url = urlsplit(robot.base_url)
    if url.scheme != "https":
        _expect(url.hostname in LOOPBACK, f"plain HTTP to {url.hostname}: off loopback use https:// with --pin or --cafile")
        return {"scheme": "http", "loopback": True}
    _expect(robot.pin or robot.context is None or robot.context.verify_mode == ssl.CERT_REQUIRED,
            "HTTPS without verifying the gateway's certificate: pass --pin or --cafile")
    return {"scheme": "https", "verified_by": f"pin {robot.pin}" if robot.pin else "certificate authority"}


def check_health(robot, a):
    answer = robot.health()
    _expect(answer == {"ok": True}, f"health said {answer}")
    return answer


def check_bad_signature(robot, a):
    bad = (lambda message: bytes(64)) if callable(robot.secret) else robot.secret + "-wrong"  # a one-way key: a forged one
    wrong = RobotClient(robot.base_url, robot.robot_id, bad, robot.timeout, robot.context, robot.pin,
                        test_run=robot.test_run, wait_429=robot.wait_429)
    return _refused(wrong.notices, 401)


def check_replay(robot, a):
    return _refused(lambda: robot.call("GET", "/v1/notices", timestamp=int(time.time()) - 300), 401)


def check_door(robot, a):
    pid = _id("door")
    opened = robot.request(pid, a.door)
    _expect(opened["state"] == "OPEN_COMMANDED", f"expected OPEN_COMMANDED, got {opened}")
    passed = robot.report(pid, "passed")
    _expect(passed["state"] == "PASSED" and passed["done"], f"expected PASSED and done, got {passed}")
    return {"passage_id": pid, "states": [opened["state"], passed["state"]]}


def check_retry(robot, a):
    pid = _id("retry")
    first = robot.request(pid, a.door)
    again = robot.request(pid, a.door)
    _expect(again == first, f"an exact retry changed the answer: {first} then {again}")
    conflict = _refused(lambda: robot.request(pid, a.door, note="different contents"), 409)
    if first["state"] == "OPEN_COMMANDED":
        robot.report(pid, "passed")  # finish it, so the owner's screen doesn't show a stuck door passage
    return {"passage_id": pid, "retry": again["state"], "different_contents": conflict}


def check_ride(robot, a):
    pid = _id("ride")
    events = _open_stream(robot)
    try:
        called = robot.request(pid, a.elevator, from_floor=a.from_floor, to_floor=a.to_floor)
        _expect(called["state"] == "CAR_CALLED", f"expected CAR_CALLED, got {called}")
        here = _follow(events, pid, "CAR_ARRIVED")
        _expect(here.get("car"), f"the car-arrived step names no car: {here}")
        boarded = robot.report(pid, "boarded")
        _expect(boarded["state"] == "DEST_SENT", f"expected DEST_SENT after boarded, got {boarded}")
        heard = robot.heartbeat(pid)  # in the car, still here
        _expect(heard["state"] in ("DEST_SENT", "ARRIVED"), f"a check-in should answer with the ride's status, got {heard}")
        _follow(events, pid, "ARRIVED")
        released = robot.report(pid, "exited")
        _expect(released["state"] == "RELEASED" and released["done"], f"expected RELEASED and done, got {released}")
    finally:
        events.close()
    return {"passage_id": pid, "car": here["car"], "seen_on_stream": ["CAR_ARRIVED", "ARRIVED"],
            "states": [called["state"], boarded["state"], released["state"]]}


def check_cancel(robot, a):
    pid = _id("cancel")
    called = robot.request(pid, a.elevator, from_floor=a.from_floor, to_floor=a.to_floor)
    _expect(called["state"] in ("CAR_CALLED", "CAR_ARRIVED"), f"expected a car call, got {called}")
    cancelled = robot.cancel(pid)
    _expect(cancelled["state"] == "CANCELLED", f"expected CANCELLED, got {cancelled}")
    _coded(cancelled)
    again = robot.cancel(pid)
    _expect(again == cancelled, f"cancelling twice changed the answer: {again}")
    return {"passage_id": pid, "state": cancelled["state"], "reason_code": cancelled["reason_code"]}


def check_refusal_codes(robot, a):
    refused = robot.request(_id("refusal"), UNKNOWN_PLACE)
    _expect(refused["state"] == "DENIED", f"expected DENIED for an unknown place, got {refused}")
    _coded(refused)
    _expect(refused["reason_code"] == "unknown_resource" and refused["retry"] is False,
            f"expected unknown_resource without retry, got {refused}")
    return {k: refused[k] for k in ("state", "reason", "reason_code", "retry", "retry_after")}


def check_notices(robot, a):
    notices = robot.notices()
    _expect(isinstance(notices, list) and all(isinstance(n, dict) and "kind" in n for n in notices),
            f"notices should be a list of {{kind, ...}}, got {notices}")
    return {"kinds": [n["kind"] for n in notices]}


def check_places(robot, a):
    seen = robot.places()
    places = {p["id"]: p for p in seen["places"]}
    _expect(a.door in places and a.elevator in places,
            f"places should list the door {a.door} and the elevator {a.elevator}, got {sorted(places)}")
    lift = places[a.elevator]
    _expect({a.from_floor, a.to_floor} <= set(lift["floors"]),
            f"the elevator should serve floors {a.from_floor} and {a.to_floor}, got {lift['floors']}")
    return {"places": sorted(places), "elevator_floors": lift["floors"], "open_now": {k: p["open_now"] for k, p in places.items()}}


def check_stream(robot, a):
    pid = _id("stream")
    events = _open_stream(robot)
    try:
        robot.request(pid, UNKNOWN_PLACE)  # a refusal: nothing in the building moves
        step = _follow(events, pid, "REQUESTED")
        denied = _follow(events, pid, "DENIED")
    finally:
        events.close()
    _coded(denied)
    return {"passage_id": pid, "steps": [step["state"], denied["state"]], "reason_code": denied["reason_code"]}


def _limited_key(e):
    """The gateway's 403 with code not_for_this_key: this key may not do it (the sandbox's try-it key: rides and doors
    only). Any other 403, a suspended robot's say, is a real failure."""
    return e.status == 403 and isinstance(e.body, dict) and e.body.get("code") == "not_for_this_key"


def _not_for_this_key(e):
    """Skip, saying why, a check this key may not run."""
    if _limited_key(e):
        raise Skipped(e.body.get("error", "this key may not")) from None


def _drill(robot, **kind):
    """Start a drill, or skip: only the sandbox takes POST /v1/drills, and not from the shared try-it key."""
    try:
        return robot.drill(**kind)
    except GatewayError as e:
        _not_for_this_key(e)
        if e.status == 404:
            raise Skipped("only the sandbox starts drills: in a building, ask the site to run its fire drill with your"
                          " robot there, and it answers on its own") from None
        raise


def _told(events, eid, all_clear=False):
    """Read the stream until this robot's notices carry emergency `eid` (or its all clear). That notice."""
    for event, data in events:
        if event == "notices":
            told = next((n for n in data if n.get("kind") == "emergency" and n["id"] == eid
                         and (n["emergency"] == "all clear") == all_clear), None)
            if told:
                return told
    raise AssertionError(f"the stream closed before emergency {eid}{' cleared' if all_clear else ''}")


def _answer(robot, told, a, arrive=False):
    """Answer the drill as told: going to its zone (and, with arrive, there and arrived) or holding; or with
    --robot-answers wait for the robot's own software to, within the deadline. [the answers seen]"""
    if a.robot_answers:
        until = time.time() + told["deadline_s"] + (60 if arrive else 0)
        seen = []
        while time.time() < until:
            now = next(n for n in robot.notices() if n.get("kind") == "emergency" and n["id"] == told["id"])
            if now["answer"] and now["answer"] not in seen:
                seen.append(now["answer"])
            if now["answer"] == "cannot" or seen and (not arrive or now["answer"] == "arrived"):
                break
            time.sleep(0.5)
        _expect(seen and "cannot" not in seen, f"the robot's own software didn't confirm in time: {seen or 'no answer'}")
        _expect(not arrive or seen[-1] == "arrived", f"the robot didn't say it arrived: {seen}")
        return seen
    target = told["target"]
    if told["action"] != "go to zone" or not target:
        robot.confirm(told["id"], "holding")
        return ["holding"]
    robot.confirm(told["id"], "going", eta_s=5)
    if not arrive:
        return ["going"]
    robot.position(target["floor"], target["x"], target["z"])  # there, as a robot that shares its position says
    _expect(robot.confirm(told["id"], "arrived")["answer"] == "arrived", "arrived wasn't recorded")
    return ["going", "arrived"]


def check_fire_drill(robot, a):
    robot.position(a.from_floor, 0.5, 0.5)  # in the building, as a robot that shares its position says
    events = _open_stream(robot)
    try:
        eid = _drill(robot, kind="fire alarm")["emergency"]
        told = _told(events, eid)
        _expect(told["emergency"] == "fire alarm" and told["action"] in ("go to zone", "clear and hold", "hold")
                and told["confirm_by"], f"a fire alarm notice should say what to do and by when: {told}")
        _expect(any(n["kind"] == "fire alarm" for n in robot.notices()), "the fire alarm notice is missing")
        held = robot.request(_id("drill-door"), a.door)
        _expect(held["state"] == "DENIED" and held["reason_code"] == "fire_alarm", f"the door should be held: {held}")
        answers = _answer(robot, told, a)
        robot.end_drills()
        clear = _told(events, eid, all_clear=True)
        _expect(clear["action"] == "ask again", f"all clear should say ask again: {clear}")
    finally:
        events.close()
    pid = _id("after-drill")
    again = robot.request(pid, a.door)
    _expect(again["state"] == "OPEN_COMMANDED", f"after all clear, asking again should get through: {again}")
    robot.report(pid, "passed")
    return {"emergency": eid, "told": told["action"], "target": (told["target"] or {}).get("name"),
            "door_held": held["reason_code"], "answers": answers, "all_clear": clear["action"],
            "answered_by": "the robot's own software" if a.robot_answers else "the kit"}


def check_zone_drill(robot, a):
    robot.position(a.from_floor, 0.5, 0.5)
    events = _open_stream(robot)
    try:
        eid = _drill(robot, kind="evacuation", floor=a.from_floor)["emergency"]
        told = _told(events, eid)
        target = told["target"]
        _expect(told["action"] == "go to zone" and target and target["floor"] == a.from_floor,
                f"an evacuation of floor {a.from_floor} should send the robot to a holding zone there: {told}")
        answers = _answer(robot, told, a, arrive=True)
        robot.end_drills()
        _told(events, eid, all_clear=True)
    finally:
        events.close()
    return {"emergency": eid, "zone": target["name"], "floor": target["floor"], "answers": answers,
            "answered_by": "the robot's own software" if a.robot_answers else "the kit"}


def _room_drill(robot, a, kind, **more):
    """A drill in the sandbox's protected room, with the robot inside it: what it was told, and the answers."""
    room, floor, x, z = SANDBOX_ROOM
    robot.position(floor, x, z)
    events = _open_stream(robot)
    try:
        eid = _drill(robot, kind=kind, room=room, **more)["emergency"]
        told = _told(events, eid)
        asked = time.time()
        answers = _answer(robot, told, a)
        took = time.time() - asked
        robot.end_drills()
        _told(events, eid, all_clear=True)
    finally:
        events.close()
    _expect(told.get("never") and told.get("also"), f"an emergency should say what never to do, and what else: {told}")
    _expect(told["scope"].get("room") == room, f"the drill should be scoped to the room: {told['scope']}")
    return eid, told, answers, took


def check_pre_discharge_drill(robot, a):
    eid, told, answers, took = _room_drill(robot, a, "pre-discharge", seconds=30)
    _expect(told["emergency"] == "pre-discharge" and told["deadline_s"] <= 5, f"confirm within 5 s: {told}")
    _expect(told["action"] in ("go to zone", "clear and hold") and told["discharge_at"] > told["clear_by"],
            f"a pre-discharge says when the agent discharges and when to be clear by: {told}")
    _expect(a.robot_answers or took <= 5, f"confirmed after {took:.1f} s; the countdown allows 5")
    return {"emergency": eid, "told": told["action"], "target": (told["target"] or {}).get("name"),
            "seconds": told["seconds"], "answers": answers,
            "answered_by": "the robot's own software" if a.robot_answers else "the kit"}


def check_epo_drill(robot, a):
    eid, told, answers, _ = _room_drill(robot, a, "emergency power off")
    _expect(told["action"] == "clear and hold", f"an emergency power off says clear and hold: {told}")
    return {"emergency": eid, "told": told["action"], "answers": answers,
            "answered_by": "the robot's own software" if a.robot_answers else "the kit"}


def check_dwell_drill(robot, a):
    try:
        robot.end_drills()  # nothing to end: a way to know this is the sandbox, where the owner isn't paged
    except GatewayError as e:
        _not_for_this_key(e)
        if e.status == 404:
            raise Skipped("only in the sandbox: in a building a robot stopped in a doorway goes on the owner's screen")\
                from None
        raise
    floor, zone = next(((f, zs[0]) for f, zs in (robot.places().get("no_stop") or {}).items() if zs), (None, None))
    if zone is None:
        raise Skipped("this gateway names no no-stop areas")
    x, z = zone["x"] + zone["w"] / 2, zone["z"] + zone["d"] / 2
    stopped, until, told = time.time(), time.time() + 60, None
    while time.time() < until and told is None:
        robot.position(floor, x, z)  # still there
        told = next((n for n in robot.notices() if n.get("kind") == "no-stop"), None)
        if told is None:
            time.sleep(1)
    _expect(told and told["zone"] == zone["name"], f"no no-stop notice in a minute stopped in {zone['name']}")
    robot.position(floor, 0.5, 0.5)  # moved on
    _expect(not [n for n in robot.notices() if n.get("kind") == "no-stop"], "the no-stop notice should clear on moving")
    return {"zone": zone["name"], "told_after_s": round(time.time() - stopped), "reason": told["reason"]}


def check_help(robot, a):
    try:
        robot.end_drills()  # nothing to end: a way to know this is the sandbox, where no one is called
    except GatewayError as e:
        _not_for_this_key(e)
        if e.status == 404:
            raise Skipped("only in the sandbox: in a building a request for help calls a person") from None
        raise
    asked = robot.call("POST", "/v1/help", {"kind": "other", "text": "conformance test: no one needs to come"})
    _expect(asked["status"] == "open" and asked["id"], f"a request for help should be open, with an id: {asked}")
    told = [m["text"] for m in robot.messages()]
    _expect(any("people have been told" in t for t in told), f"the robot should hear it was passed on: {told}")
    done = robot.call("POST", f"/v1/help/{asked['id']}/resolved", {"note": "the conformance test"})
    _expect(done["status"] == "resolved", f"resolved should close it: {done}")
    return {"help": asked["id"], "status": done["status"], "heard": told[-1]}


def check_resume(robot, a):
    me = robot.me()
    _expect(me["id"] == robot.robot_id and "time" in me, f"me should name this robot and the gateway's clock: {me}")
    _open_stream(robot).close()
    seen = robot.last_event_id
    _expect(seen is not None, "the stream's events should carry an id")
    pid = _id("resume")
    robot.request(pid, UNKNOWN_PLACE)  # a refusal while the stream is closed: nothing in the building moves
    events = robot.stream(after=seen)
    try:
        denied = _follow(events, pid, "DENIED")
    finally:
        events.close()
    listed = [p["passage_id"] for p in robot.passages(since=me["time"] - 60)]
    _expect(pid in listed, f"GET /v1/passages?since= should list the refused passage, got {listed}")
    return {"robot": me["id"], "after": seen, "replayed": denied["state"], "passage_id": pid}


CHECKS = [
    ("HTTPS with the gateway's certificate verified (plain HTTP on loopback only)", check_tls),
    ("health", check_health),
    ("a bad signature is refused (401)", check_bad_signature),
    ("an old timestamp is refused as a replay (401)", check_replay),
    ("door passage: OPEN_COMMANDED, passed, PASSED", check_door),
    ("an exact retry repeats the answer; other contents get 409", check_retry),
    ("elevator ride: car arrives on the stream with its car, boarded, a check-in, exited, RELEASED", check_ride),
    ("cancel before boarding: CANCELLED, and again is harmless", check_cancel),
    ("every refusal carries reason_code, retry and retry_after", check_refusal_codes),
    ("notices are readable", check_notices),
    ("places list the door and the elevator, with the ride's floors", check_places),
    ("the stream delivers passage events", check_stream),
    ("after a reconnect: who it is, the steps missed replayed, its passages listed", check_resume),
    ("fire alarm drill: told on the stream, held at the door, confirmed, all clear, through when it asks again",
     check_fire_drill),
    ("go-to-zone drill: sent to the holding zone on its floor, going, arrived", check_zone_drill),
    ("pre-discharge drill: the room's countdown, confirmed within 5 s, all clear", check_pre_discharge_drill),
    ("emergency power off drill: clear and hold, confirmed, all clear", check_epo_drill),
    ("doorway dwell: stopped in a no-stop area, told to move on, cleared on moving", check_dwell_drill),
    ("help request: open, passed on in its messages, resolved", check_help),
]


def run(robot, door, elevator, from_floor, to_floor, robot_answers=False):
    """Every check in order: [{"check", "passed", "evidence"}], "skipped" (why) on one that can't run here. A failed
    check doesn't stop the rest."""
    a = argparse.Namespace(door=door, elevator=elevator, from_floor=str(from_floor), to_floor=str(to_floor),
                           robot_answers=robot_answers)
    results = []
    for name, check in CHECKS:
        try:
            results.append({"check": name, "passed": True, "evidence": check(robot, a)})
        except Skipped as e:
            results.append({"check": name, "passed": True, "skipped": str(e), "evidence": None})
        except GatewayError as e:
            if _limited_key(e):
                results.append({"check": name, "passed": True, "skipped": e.body["error"], "evidence": None})
            else:  # the failure is the evidence
                results.append({"check": name, "passed": False, "evidence": f"{type(e).__name__}: {e}"})
        except Exception as e:  # the failure is the evidence
            results.append({"check": name, "passed": False, "evidence": f"{type(e).__name__}: {e}"})
    return results


KIT_VERSION = "0.4.0"  # the test kit's own version (docs/api/kit/CHANGELOG.md), written into every report


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python3 -m plynth.conformance", description=__doc__.splitlines()[0])
    parser.add_argument("--env", help="a file of GATEWAY_URL, GATEWAY_ROBOT_ID and GATEWAY_ROBOT_SECRET (plynth-sandbox.env)")
    parser.add_argument("--url", help="the gateway, e.g. https://sandbox.plynth.build (or GATEWAY_URL)")
    parser.add_argument("--robot", help="the robot ID the building issued (or GATEWAY_ROBOT_ID)")
    parser.add_argument("--secret", help="that robot's secret; better in GATEWAY_ROBOT_SECRET, as a flag shows in ps and history")
    parser.add_argument("--key", help="instead of a secret, the file of its one-way key's private half, PEM (or"
                                      " GATEWAY_ROBOT_KEY); needs the cryptography library")
    parser.add_argument("--pin", default=os.environ.get("GATEWAY_TLS_PIN"),
                        help="the gateway certificate's fingerprint from the building, sha256:<hex> (or GATEWAY_TLS_PIN);"
                             " old,new while the building changes its certificate")
    parser.add_argument("--cafile", help="the site's certificate authority, instead of a pin")
    parser.add_argument("--door", help="a door this robot may pass (the sandbox: door)")
    parser.add_argument("--elevator", help="an elevator this robot may ride (the sandbox: lift)")
    parser.add_argument("--from", dest="from_floor", help="the floor to ride from (the sandbox: 1)")
    parser.add_argument("--to", dest="to_floor", help="the floor to ride to (the sandbox: 2)")
    parser.add_argument("--report", help="write the JSON report here")
    parser.add_argument("--robot-answers", action="store_true",
                        help="in the drills, wait for your robot's own software to answer instead of answering for it")
    args = parser.parse_args(argv)
    env = {**os.environ, **(env_file(args.env) if args.env else {})}
    args.url, args.robot = args.url or env.get("GATEWAY_URL"), args.robot or env.get("GATEWAY_ROBOT_ID")
    key = args.key or env.get("GATEWAY_ROBOT_KEY")
    args.secret = Path(key).expanduser().read_text() if key else args.secret or env.get("GATEWAY_ROBOT_SECRET")
    args.pin = args.pin or env.get("GATEWAY_TLS_PIN")
    for name, value in (("GATEWAY_URL (or --url)", args.url), ("GATEWAY_ROBOT_ID (or --robot)", args.robot),
                        ("GATEWAY_ROBOT_SECRET (or GATEWAY_ROBOT_KEY)", args.secret)):
        if not value:
            parser.error(f"set {name}, or pass --env with the file from your company's page")

    try:
        robot = RobotClient(args.url, args.robot, args.secret, pin=args.pin, cafile=args.cafile, test_run="conformance",
                            wait_429=60)  # a busy sandbox: wait each call's turn rather than fail it
    except ValueError as e:  # plain HTTP off loopback
        parser.error(str(e))
    sandbox = None
    if not (args.door and args.elevator and args.from_floor and args.to_floor):
        try:
            sandbox = robot.me().get("sandbox")
        except (GatewayError, OSError) as e:
            parser.error(f"couldn't ask the gateway who this robot is: {e}")
        if not sandbox:
            parser.error("in a building, name the door, the elevator and the ride (--door, --elevator, --from, --to):"
                         " this test really opens them")
        args.door, args.elevator = args.door or "door", args.elevator or "lift"
        args.from_floor, args.to_floor = args.from_floor or "1", args.to_floor or "2"
    started = datetime.datetime.now().astimezone()
    try:
        versions = robot.about()
    except (GatewayError, OSError):
        versions = {"release": None, "api": None}
    results = run(robot, args.door, args.elevator, args.from_floor, args.to_floor, args.robot_answers)
    failed = [r for r in results if not r["passed"]]
    if sandbox:  # the sandbox counts the runs that pass, true or false and nothing else; never this run's result
        try:
            robot.call("POST", "/v1/sandbox/conformance", {"passed": not failed})
        except (GatewayError, OSError):
            pass
    skipped = [r for r in results if "skipped" in r]
    print(f"Conformance: {args.url} as {args.robot}, {started:%Y-%m-%d %H:%M %Z}"
          f" (kit {KIT_VERSION}, gateway {versions['release'] or 'unknown'}, API v{versions['api'] or '?'})")
    for r in results:
        print(f"  {'SKIP' if 'skipped' in r else 'PASS' if r['passed'] else 'FAIL'}  {r['check']}")
        if not r["passed"] or "skipped" in r:
            print(f"        {r.get('skipped') or r['evidence']}")
        elif isinstance(r["evidence"], dict) and r["evidence"].get("scheme") == "http":
            print("        plain HTTP on your own computer; anywhere else the gateway is HTTPS, verified")
        elif isinstance(r["evidence"], dict) and r["evidence"].get("answered_by") == "the kit":
            print("        the kit answered for your robot; add --robot-answers to test your own software")
    ran = len(results) - len(skipped)
    print(f"{ran - len(failed)} of {ran} passed" + (f", {len(skipped)} skipped (they can't run with this key or here)"
                                                    if skipped else "") + (f", {len(failed)} failed." if failed else "."))
    if args.report:
        with open(args.report, "w") as f:
            json.dump({"gateway": args.url, "robot": args.robot, "time": started.isoformat(timespec="seconds"),
                       "kit": KIT_VERSION, "gateway_release": versions["release"], "api": versions["api"],
                       "door": args.door, "elevator": args.elevator, "ride": [args.from_floor, args.to_floor],
                       "passed": not failed, "checks": results}, f, indent=2)
        print(f"Report: {args.report}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
