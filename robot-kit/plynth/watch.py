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

It keeps the stream open, reopening it from its last event when it drops or goes READ_S seconds without a byte (the
gateway sends a keep-alive every 2 s); sends where the robot is every `every` seconds (pose() gives (floor, x, z) on
the floor plate, or ("map", floor, map_x, map_y) in the fleet's own map once the building has placed it), or a
heartbeat when there's no pose, so it knows the link is there, and reads its notices at once when an answer's `hold`
says otherwise than its stream did; confirms each hold it's told of (holding); and calls on_hold(held, why) each time
what the robot must do changes. A position the gateway refuses is logged, once, and is word from the gateway all the
same; its 5xx, up but failing, is no answer.

The robot must hold while any notice says so: kind "hold" (the owner's Hold, or a keep-out zone it's inside), "stop"
(a lost robot, until a person lets it go on; or one suspended, or its company stopped, whose stream is refused: held
until a stream opens again), or an emergency whose action is hold or stay put. "Clear and hold" is clear, then hold:
the robot clears any doorway, car or no-stop area it's in under its own navigation, then says so with
watch.holding(), which tells the building (holding) and holds it; watch.clearing says what it's clearing for
meanwhile. If it hasn't said so within the notice's clear_in_s (CLEAR_S at most), it's held anyway, and holds()
counts it once the building says its time is up. A hold in force when the link drops stays until the robot hears all
clear: it never resumes on its own. When it has heard nothing from the gateway for the grace time, it does what the
owner's rule says (Peter, 2026-10-23): keep going, by default, or hold. Its rule comes from GET /v1/me (its
company's) and, while it's in a place the owner chose, from a "lost link" notice; both are kept for when the link is
gone. A hold over a network is a software hold, never a safety function: keep the robot's own safety controls, and its
remote, in a person's hand.
"""
import logging
import threading
import time

from plynth.client import GatewayError  # plynth.client in the kit (gateway.kit)

log = logging.getLogger("plynth.watch")

HOLD_ACTIONS = ("hold", "stay put")  # at once; "clear and hold" once the robot has cleared (the robotics review, 2026-10-29)
CLEAR_S = 10  # a "clear and hold" holds by then at the latest: fire and smoke doors close within 10 s (HCAI PIN 69 H)
POLICY = {"action": "continue", "grace_s": 5}  # until the robot has read its own: the building's default
READ_S = 5  # the gateway's stream sends a keep-alive every 2 s: this long without a byte, the stream is dead


def holds(notices):
    """Why the robot must hold, from its notices: a list of reasons, empty when it may move. A "clear and hold" counts
    once the robot has said it's holding (the building's answer holding, or state held), or once the building says its
    time to clear has run out (clear_in_s 0, or clear_by gone); until then it clears."""
    out = []
    for n in notices or ():
        kind = n.get("kind")
        if kind in ("hold", "stop") or kind == "emergency" and (n.get("action") in HOLD_ACTIONS or _cleared(n)
                                                                or _due(n)):
            out.append(n.get("reason") or kind)
    return out


def _cleared(n):
    return n.get("action") == "clear and hold" and (n.get("answer") == "holding" or n.get("state") == "held")


def _clearing(n):
    """A "clear and hold" the robot hasn't yet said it's holding for: it's clearing."""
    return n.get("kind") == "emergency" and n.get("action") == "clear and hold" and not _cleared(n)


def _due(n):
    """A "clear and hold" still clearing whose time to clear, by the building's own word, has run out."""
    return _clearing(n) and (n.get("clear_in_s") is not None or n.get("clear_by") is not None) and clearing_s(n) == 0


def clearing_s(n, now=None):
    """How long a "clear and hold" leaves the robot to clear before it's held anyway, from now: the notice's
    clear_in_s (what it had left when the building sent it), else what's left until its clear_by (the building's unix
    time), and CLEAR_S at most."""
    if n.get("clear_in_s") is not None:
        left = n["clear_in_s"]
    elif n.get("clear_by") is not None:
        left = n["clear_by"] - (time.time() if now is None else now)
    else:
        left = CLEAR_S
    return max(0.0, min(float(CLEAR_S), float(left)))


class Watch:
    def __init__(self, robot, on_hold=None, pose=None, every=1.0, on_notices=None, clock=time.monotonic,
                 on_passage=None):
        """robot: a RobotClient. on_hold(held, why): called when what the robot must do changes, why a list of reasons
        ("lost the gateway" among them when it's the link). pose(): (floor, x, z), or ("map", floor, map_x, map_y), or
        None for not now. every: seconds between positions (or heartbeats). on_notices(notices): every change of them.
        on_passage(step): each step of the robot's passages as the stream gives it (a ride's has `arrived`, the floor
        it came to). An error in any is logged, never the end of the stream or of the watch's decisions; on_hold is
        called again a quarter second later until it takes what the robot must do."""
        self.robot, self.on_hold, self.pose, self.every, self.on_notices = robot, on_hold, pose, every, on_notices
        self.clock, self.on_passage = clock, on_passage
        self.policy = dict(POLICY)
        self.notices, self.why, self.held = [], [], False
        self._told = (False, [])  # what on_hold last took
        self._refused = False  # its stream refused (401, 403): held on that until a stream opens again
        self.threads = []
        self.heard = clock()  # the last time the gateway answered or pushed anything
        self._stop = threading.Event()
        self._confirmed = set()
        self._clear_until = {}  # each "clear and hold" it's clearing for: when it holds anyway (-inf: it said it's holding)
        self._refusal = None  # the gateway's last refusal of a position, said once
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

    @property
    def clearing(self):
        """What the robot is to clear for first, then hold: the reason of each "clear and hold" it hasn't said it's
        holding for, while its time to clear lasts."""
        now = self.clock()
        return [n.get("reason") or "clear and hold" for n in self.notices
                if _clearing(n) and self._clear_until.get(n.get("id"), now) > now]

    def holding(self):
        """The robot's own word that it has cleared what each "clear and hold" asked (a doorway, a car, the area) and
        stopped: it's held from now, and the building is told (holding). One not told now is told again with the
        next change of notices."""
        with self._lock:
            for n in self.notices:
                if _clearing(n):
                    self._clear_until[n.get("id")] = float("-inf")
        self._decide_once()
        self._confirm()

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
                for event, data in self.robot.stream(timeout=READ_S, after=self.robot.last_event_id):
                    self._heard_now()
                    self._refused = False
                    if event == "notices":
                        self._notices(data)
                    elif event == "passage" and self.on_passage:
                        try:
                            self.on_passage(data)
                        except Exception:  # the robot's own code: never the end of its stream
                            log.exception("on_passage raised")
                    if self._stop.is_set():
                        return
            except GatewayError as e:
                if e.status in (401, 403):  # suspended, or its company stopped: hold, and keep asking
                    self._refused = True
                    self._notices([{"kind": "stop", "reason": f"refused by the gateway ({e.status})"}])
            except Exception:  # dropped, timed out or no answer: open it again
                pass
            self._stop.wait(1)
            self._read_policy()

    def _notices(self, notices):
        with self._lock:
            self.notices = list(notices or [])
            now = self.clock()  # each "clear and hold" it's clearing for: its time to clear runs from first hearing it
            self._clear_until = {n.get("id"): self._clear_until.get(n.get("id"), now + clearing_s(n))
                                 for n in self.notices if _clearing(n)}
        if self.on_notices:
            try:
                self.on_notices(self.notices)
            except Exception:  # the robot's own code: never the end of its stream
                log.exception("on_notices raised")
        self._confirm()
        self._decide_once()

    def _confirm(self):
        """Confirm each hold with an ID once, and each "clear and hold" the robot has said it's holding for: holding."""
        for n in list(self.notices):
            if n.get("id") is None:
                continue
            if n.get("kind") == "hold" and n["id"] not in self._confirmed:
                try:
                    self.robot.confirm(n["id"], "holding")
                    self._confirmed.add(n["id"])
                except Exception:
                    pass  # asked again with the next change of notices
            elif _clearing(n) and self._clear_until.get(n["id"]) == float("-inf"):
                try:
                    answered = self.robot.confirm(n["id"], "holding")  # the notice, with its answer
                except Exception:
                    continue  # held all the same; told again with the next change of notices
                with self._lock:
                    self.notices = [answered if m is n else m for m in self.notices]

    def _report(self):
        """Where the robot is, every `every` s, or that it's there: each answer is word from the gateway, and says
        whether the robot must hold and why; where that isn't what the stream said, the stream missed something (it may
        have died without a word), so the notices are read at once. Never while its stream is refused: that stop stays
        until a stream opens (the robotics review, 2026-10-30: a re-read let a suspended robot go each second)."""
        while not self._stop.is_set():
            try:
                at = self.pose() if self.pose else None
                if at and at[0] == "map":
                    answer = self.robot.map_position(*at[1:])
                elif at:
                    answer = self.robot.position(*at)
                else:
                    answer = self.robot.online()
                self._heard_now()
                self._refusal = None
                said = answer.get("hold") if isinstance(answer, dict) else None
                if isinstance(said, dict) and not self._refused and said.get("why") != holds(self.notices):
                    self._notices(self.robot.notices())  # a second hold on top of a first, too
            except GatewayError as e:  # refused (a floor the building hasn't, no frame yet for the company's map): the
                if e.status < 500:  # gateway's own answer, so word from it, never a lost link; logged, once. Its 5xx is
                    self._heard_now()  # no answer: up but failing (its database full, say), it can't send a hold either
                    if str(e) != self._refusal:
                        log.warning("the gateway refused the robot's %s: %s", "position" if at else "heartbeat", e)
                        self._refusal = str(e)
            except Exception:
                pass  # no answer: the link may be gone; _decide sees it
            self._stop.wait(self.every)

    def _decide(self):
        while not self._stop.is_set():
            self._decide_once()
            self._stop.wait(0.25)

    def _decide_once(self):
        with self._lock:
            now = self.clock()  # the last heard: a hold in force when the link drops stays, and so does a clearing
            why = holds(self.notices) + [n.get("reason") or "clear and hold" for n in self.notices
                                         if _clearing(n) and not _due(n) and self._clear_until.get(n.get("id"), now) <= now]
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
