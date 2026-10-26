"""A robot kept connected, and held when the building says so (plans/onboarding-build-2026-10-23.md, item 2): the kit's
helper for a robot's own software, and what the ROS 2 node (integrations/ros2, plynth_node) runs on. Standard library
only, like the client.

    from plynth.client import RobotClient
    from plynth.watch import Watch

    robot = RobotClient(url, robot_id, secret)
    watch = Watch(robot, on_hold=lambda held, why: ..., pose=lambda: ("1", x, z)).start()
    ...
    watch.held      # True while the robot must hold
    watch.help("blocked path", where={"floor": "1", "x": 0.4, "z": 0.6})

It keeps the stream open, reopening it from its last event when it drops; sends where the robot is every `every`
seconds (pose() gives (floor, x, z) on the floor plate, or ("map", floor, map_x, map_y) in the fleet's own map once
the building has placed it), or a heartbeat when there's no pose, so it knows the link is there; confirms each hold it's
told of (holding); and calls on_hold(held, why) each time what the robot must do changes.

The robot must hold while any notice says so: kind "hold" (the owner's Hold, or a keep-out zone it's inside), "stop"
(a lost robot, until a person lets it go on), or an emergency whose action is hold, clear and hold or stay put. A hold in
force when the link drops stays until the robot hears all clear: it never resumes on its own. When it has heard nothing
from the gateway for the grace time, it does what the owner's rule says (Peter, 2026-10-23): keep going, by default, or
hold. Its rule comes from GET /v1/me (its company's) and, while it's in a place the owner chose, from a "lost link"
notice; both are kept for when the link is gone. A hold over a network is a software hold, never an emergency stop:
keep the robot's own e-stop, and its remote, in a person's hand.
"""
import logging
import threading
import time

from plynth.client import GatewayError  # plynth.client in the kit (gateway.kit)

log = logging.getLogger("plynth.watch")

HOLD_ACTIONS = ("hold", "clear and hold", "stay put")
POLICY = {"action": "continue", "grace_s": 5}  # until the robot has read its own: the building's default


def holds(notices):
    """Why the robot must hold, from its notices: a list of reasons, empty when it may move."""
    out = []
    for n in notices or ():
        kind = n.get("kind")
        if kind in ("hold", "stop") or kind == "emergency" and n.get("action") in HOLD_ACTIONS:
            out.append(n.get("reason") or kind)
    return out


class Watch:
    def __init__(self, robot, on_hold=None, pose=None, every=1.0, on_notices=None, clock=time.monotonic):
        """robot: a RobotClient. on_hold(held, why): called when what the robot must do changes, why a list of reasons
        ("lost the gateway" among them when it's the link). pose(): (floor, x, z), or ("map", floor, map_x, map_y), or
        None for not now. every: seconds between positions (or heartbeats). on_notices(notices): every change of them.
        An error in either is logged, never the end of the stream or of the watch's decisions; on_hold is called
        again a quarter second later until it takes what the robot must do."""
        self.robot, self.on_hold, self.pose, self.every, self.on_notices = robot, on_hold, pose, every, on_notices
        self.clock = clock
        self.policy = dict(POLICY)
        self.notices, self.why, self.held = [], [], False
        self._told = (False, [])  # what on_hold last took
        self.threads = []
        self.heard = clock()  # the last time the gateway answered or pushed anything
        self._stop = threading.Event()
        self._confirmed = set()
        self._lock = threading.Lock()

    @property
    def rule(self):
        """What to do if the link drops, now: a place's the robot is in (a "lost link" notice), else its own."""
        here = next((n for n in self.notices if n.get("kind") == "lost link"), None)
        return {"action": here.get("action", "hold"), "grace_s": float(here.get("grace_s", 5))} if here else self.policy

    @property
    def lost(self):
        """No word from the gateway for longer than the grace time."""
        return self.clock() - self.heard > self.rule["grace_s"]

    def start(self):
        self._read_policy()
        self.threads = [threading.Thread(target=run, daemon=True, name=f"plynth {run.__name__}")
                        for run in (self._listen, self._report, self._decide)]
        for thread in self.threads:
            thread.start()
        return self

    def stop(self):
        self._stop.set()

    def help(self, kind, where=None, text=None, urgency="normal"):
        """Ask the building for a person (blocked path, door won't open, elevator problem, lost, low battery, payload
        problem, fault, other); updates come in its messages."""
        body = {"kind": kind, "urgency": urgency, **({"where": where} if where else {}), **({"text": text} if text else {})}
        return self.robot.call("POST", "/v1/help", body)

    def _heard_now(self):
        with self._lock:
            self.heard = self.clock()

    def _read_policy(self):
        try:
            me = self.robot.me()
        except Exception:  # not now: the last one read (or the safe default) stands
            return
        self._heard_now()
        if isinstance(me.get("lost_link"), dict):
            rule = me["lost_link"]
            self.policy = {"action": rule.get("action", POLICY["action"]), "grace_s": float(rule.get("grace_s", 5))}

    def _listen(self):
        """The stream, kept open: reopened from its last event when it drops, and the policy read again then."""
        while not self._stop.is_set():
            try:
                for event, data in self.robot.stream(timeout=20, after=self.robot.last_event_id):
                    self._heard_now()
                    if event == "notices":
                        self._notices(data)
                    if self._stop.is_set():
                        return
            except GatewayError as e:
                if e.status in (401, 403):  # suspended, or its company stopped: hold, and keep asking
                    self._notices([{"kind": "stop", "reason": f"refused by the gateway ({e.status})"}])
            except Exception:  # dropped, timed out or no answer: open it again
                pass
            self._stop.wait(1)
            self._read_policy()

    def _notices(self, notices):
        with self._lock:
            self.notices = list(notices or [])
        if self.on_notices:
            try:
                self.on_notices(self.notices)
            except Exception:  # the robot's own code: never the end of its stream
                log.exception("on_notices raised")
        for n in self.notices:  # confirm each hold with an ID once: holding
            if n.get("kind") == "hold" and n.get("id") is not None and n["id"] not in self._confirmed:
                try:
                    self.robot.confirm(n["id"], "holding")
                    self._confirmed.add(n["id"])
                except Exception:
                    pass  # asked again with the next change of notices
        self._decide_once()

    def _report(self):
        """Where the robot is, every `every` s, or that it's there: each answer is word from the gateway."""
        while not self._stop.is_set():
            try:
                at = self.pose() if self.pose else None
                if at and at[0] == "map":
                    self.robot.map_position(*at[1:])
                elif at:
                    self.robot.position(*at)
                else:
                    self.robot.online()
                self._heard_now()
            except Exception:
                pass  # no answer: the link may be gone; _decide sees it
            self._stop.wait(self.every)

    def _decide(self):
        while not self._stop.is_set():
            self._decide_once()
            self._stop.wait(0.25)

    def _decide_once(self):
        with self._lock:
            why = holds(self.notices)  # the last heard: a hold in force when the link drops stays
            if self.lost and self.rule["action"] == "hold":
                why.append("lost the gateway")
            self.held, self.why = bool(why), why
            changed, self._told = (self.held, why) != self._told, (self.held, why)
        if changed and self.on_hold:
            try:
                self.on_hold(self.held, list(why))
            except Exception:  # the robot's own code (2026-10-23: rclpy's ValueError at each release closed the
                log.exception("on_hold raised")  # stream for a second): logged, and told again at the next decision
                with self._lock:
                    self._told = None
