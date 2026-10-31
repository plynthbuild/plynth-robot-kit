"""Plynth's elevators and doors as an Open-RMF lift and door adapter.

    ros2 run plynth_rmf_adapter adapter --ros-args --params-file config/sandbox.yaml

Open-RMF's fleet adapters ask for lifts on adapter_lift_requests and for doors on adapter_door_requests. Each lift
session and each door request becomes a Plynth passage, signed as one robot (GATEWAY_URL, GATEWAY_ROBOT_ID,
GATEWAY_ROBOT_SECRET, and GATEWAY_TLS_PIN in a building). Every second the node publishes lift_states and door_states
for the elevators and doors named in its YAML (each named by its Plynth ID), fire_alarm_trigger from the robot's
notices, and plynth/closures: the closures, outages, no-robot windows and keep-out zones in force, as JSON.

A lift session:
  1. Its first request names both floors, destination_floor "<from>:<to>" ("1:2"): Plynth calls a car only with both.
  2. The car comes: lift_states shows it at <from>, door open, in AGV mode, held for the session.
  3. Aboard, the robot asks for "<to>" (or "<to>:<to>"): reported boarded, and the car goes.
  4. It arrives: at <to>, door open.
  5. Out, END_SESSION: reported exited. Before boarding, END_SESSION cancels the call.
A door asked open is a door passage, open once Plynth says go through, never while the door is only unlocked; asked
closed, it's reported passed.
In a car or a doorway the adapter checks in each second, which also asks the car to hold its doors a little longer.

Refused, a lift or door doesn't serve the robot, and the log says why. Open-RMF's fleet adapter looks at a lift's floor,
door and session, not its mode, so a lift shows its door open and the session only while serving it: in FIRE, EMERGENCY
or OFFLINE nothing starts, though a robot aboard at its floor is still let out. When the gateway doesn't answer, no
lift and no door shows open at all.
"""
import http.client
import json
import os
import re
import time
import uuid

import rclpy
from rcl_interfaces.msg import ParameterDescriptor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile
from rmf_door_msgs.msg import DoorMode, DoorRequest, DoorState
from rmf_lift_msgs.msg import LiftRequest, LiftState
from std_msgs.msg import Bool, String

from plynth.client import GatewayError, RobotClient

REFUSED = {"DENIED", "FAILED", "CANCELLED"}
FINAL = REFUSED | {"PASSED", "RELEASED"}
AT_FLOOR = {"CAR_ARRIVED", "ARRIVED"}  # the car stands at a floor, its door open for the robot
AT_DOOR = AT_FLOOR | {"BOARDED", "DEST_SENT", "OPEN_COMMANDED"}  # in a car or a doorway: a check-in each second
CLOSES = {"closure", "out of service", "no-robot window", "keep out"}  # notices that close a way through now
NO_ANSWER = (OSError, http.client.HTTPException, ValueError)  # none, or half of one: a timeout, a drop, a restart
RETRY_S = 10  # after a refusal that says waiting helps but not for how long


def passage_id(requester):
    """New for each trip. Open-RMF's session and requester IDs are the robot's own name, the same every ride."""
    return re.sub(r"[^A-Za-z0-9_.-]", "-", requester)[:100] + "-" + uuid.uuid4().hex[:12]


def again_later(error):
    """A 429 or a 5xx decides nothing: ask again later with the same passage ID, as after no answer."""
    return isinstance(error, GatewayError) and (error.status == 429 or error.status >= 500)


class Adapter(Node):
    def __init__(self):
        super().__init__("plynth_rmf_adapter")
        self.robot = RobotClient(os.environ["GATEWAY_URL"], os.environ["GATEWAY_ROBOT_ID"],
                                 os.environ["GATEWAY_ROBOT_SECRET"], timeout=5,
                                 pin=os.environ.get("GATEWAY_TLS_PIN") or None)
        ids = ParameterDescriptor(dynamic_typing=True)  # an ID may read as a number, and an empty list as nothing
        lifts = [str(i) for i in self.declare_parameter("lifts", ["lift"], ids).value or []]
        self.doors = [str(i) for i in self.declare_parameter("doors", ["door"], ids).value or []]
        self.floor = dict.fromkeys(lifts, "")   # each car's floor at the last step this robot saw ("": none yet)
        self.served = dict.fromkeys(lifts, [])  # each elevator's floors, from GET /v1/places
        self.rides, self.passes, self.leaving = {}, {}, []  # lift: its session's ride; (door, requester): a passage
        self.online, self.placed, self.notices, self.warned = False, False, [], set()
        self.fire = self.closed = None  # as last published
        latched = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self.lift_states = self.create_publisher(LiftState, "lift_states", 10)
        self.door_states = self.create_publisher(DoorState, "door_states", 10)
        self.fire_alarm = self.create_publisher(Bool, "fire_alarm_trigger", latched)
        self.closures = self.create_publisher(String, "plynth/closures", latched)
        self.create_subscription(LiftRequest, "adapter_lift_requests", self.on_lift, 10)
        self.create_subscription(DoorRequest, "adapter_door_requests", self.on_door, 10)
        self.create_timer(1.0, self.tick)

    # --- what Open-RMF asks ---

    def on_lift(self, msg):
        lift, ride = msg.lift_name, self.rides.get(msg.lift_name)
        if lift not in self.floor or ride and ride["owner"] != msg.session_id and not self.over(ride):
            return  # not one of ours, or the lift is serving another session
        if msg.request_type == LiftRequest.REQUEST_END_SESSION:
            if ride and ride["owner"] == msg.session_id:
                self.leave(self.rides.pop(lift), "exited" if ride["boarded"] else "cancel")
            return
        if ride is None or ride["owner"] != msg.session_id or self.again(ride):
            return self.start_ride(msg)
        if (self.state(ride) == "CAR_ARRIVED" and msg.destination_floor != ride["first"]
                and msg.destination_floor.split(":")[-1] == ride["to"]):  # aboard, it asks for its floor
            ride["boarded"] = True
            self.update(ride, self.robot.report, ride["id"], "boarded")

    def start_ride(self, msg):
        frm, _, to = msg.destination_floor.partition(":")
        if not to or to == frm:  # Open-RMF's own first ask is the robot's floor alone, with no destination yet
            if (msg.session_id, msg.destination_floor) not in self.warned:  # once: Open-RMF asks every second
                self.warned.add((msg.session_id, msg.destination_floor))
                self.get_logger().warn(
                    f"lift {msg.lift_name}, session {msg.session_id}: asked for {msg.destination_floor!r} alone. "
                    "Plynth calls a car only with both floors: send destination_floor '<from>:<to>' (e.g. '1:2') first")
            return
        self.start(self.rides, msg.lift_name, self.trip(msg.lift_name, msg.session_id, f"lift {msg.lift_name}, session "
                   f"{msg.session_id}", first=msg.destination_floor, from_floor=frm, to_floor=to))

    def on_door(self, msg):
        if msg.door_name not in self.doors:
            return
        key = (msg.door_name, msg.requester_id)
        entry = self.passes.get(key)
        if msg.requested_mode.value == DoorMode.MODE_OPEN:
            if entry is None or self.again(entry):
                self.start(self.passes, key, self.trip(msg.door_name, msg.requester_id,
                                                       f"door {msg.door_name}, for {msg.requester_id}"))
        elif entry is not None:  # asked closed: through, or no longer wanted
            self.leave(self.passes.pop(key), "passed" if self.state(entry) == "OPEN_COMMANDED" else "cancel")

    # --- passages ---

    @staticmethod
    def trip(resource, owner, name, first=None, **floors):
        return {"id": passage_id(owner), "resource": resource, "owner": owner, "name": name, "floors": floors,
                "first": first, "from": floors.get("from_floor"), "to": floors.get("to_floor"), "status": None,
                "boarded": False, "retry_at": None, "end": None}

    def start(self, table, key, entry):
        table[key] = entry
        self.ask(entry)

    def ask(self, entry):  # the same ID again until it's answered: sending the same request twice is safe
        self.update(entry, self.robot.request, entry["id"], entry["resource"], **entry["floors"])

    def leave(self, entry, event):
        """Done with a passage: report what the robot did ("exited", "passed") or "cancel" it, again each second until
        the gateway answers. "exited" goes even after a failed ride: the building still takes it, as word that the
        robot is out of the car."""
        if event == "exited" or not self.over(entry):
            self.get_logger().info(f"{entry['name']}: {event}")
            entry["end"] = event
            self.leaving.append(entry)
            self.finish()

    def finish(self):
        waiting = []
        for entry in self.leaving:
            if entry["end"] == "cancel":
                answered = self.update(entry, self.robot.cancel, entry["id"])
            else:
                answered = self.update(entry, self.robot.report, entry["id"], entry["end"])
            if not answered:
                waiting.append(entry)
        self.leaving = waiting

    @staticmethod
    def state(entry):
        return entry["status"] and entry["status"]["state"]

    def over(self, entry):
        return self.state(entry) in FINAL

    def again(self, entry):
        """Refused, and by now waiting should have helped: ask again, under a new ID."""
        return self.over(entry) and entry["retry_at"] is not None and time.time() >= entry["retry_at"]

    def update(self, entry, call, *args, **kwargs):
        """One call about a passage, its answer kept and its new state logged; True once the gateway has decided.
        With no answer, a 429 or a 5xx, the passage stays as it was, to be asked again with the same ID."""
        try:
            status = call(*args, **kwargs)
        except GatewayError as e:
            if again_later(e):
                self.answered(e)
                return False
            # it refused the call itself (400, 404, 409...): this passage goes no further
            status = {"state": "FAILED", "reason": str(e), "reason_code": None, "retry": False, "retry_after": None}
        except NO_ANSWER as e:
            self.answered(e)
            return False
        else:
            self.answered()
        changed, entry["status"] = self.state(entry) != status["state"], status
        if changed:
            self.log(entry, status)
        return True

    def log(self, entry, status):
        if status["state"] in REFUSED and entry["end"] is None:  # refused, or stopped, by the building, not by us
            wait = RETRY_S if status["retry_after"] is None else status["retry_after"]
            entry["retry_at"] = time.time() + wait if status["retry"] and not entry["boarded"] else None
            self.get_logger().warn(f"{entry['name']}: not served, {status['state']}: {status['reason']} (reason_code "
                                   f"{status['reason_code']}, retry {status['retry']}, "
                                   f"retry_after {status['retry_after']})")
        else:
            car, code = status.get("car"), status.get("approval_code")
            self.get_logger().info(f"{entry['name']}: {status['state']}" + (f", car {car}" if car else "")
                                   + (f", approval code {code}" if code else ""))
        if status["state"] == "CAR_ARRIVED":
            self.floor[entry["resource"]] = entry["from"]
        elif status["state"] == "ARRIVED":
            self.floor[entry["resource"]] = (status.get("arrived") or {}).get("floor") or entry["to"]

    def answered(self, error=None):
        if error is None and not self.online:
            self.get_logger().info("The gateway answers")
        elif error is not None and self.online:
            self.get_logger().error(f"No answer from the gateway ({error}): every lift and door shows OFFLINE")
        self.online = error is None

    # --- every second ---

    def tick(self):
        try:
            self.notices = self.robot.notices()
            if not self.placed:
                self.place(self.robot.places()["places"])
            self.answered()
        except (*NO_ANSWER, GatewayError) as e:  # a key the gateway refuses is no answer too: nothing can be served
            self.answered(e)
        if self.online:
            for entry in [*self.rides.values(), *self.passes.values()]:
                if entry["status"] is None:
                    self.ask(entry)
                elif self.state(entry) in AT_DOOR:  # a check-in: if it stops, the building's people are told
                    self.update(entry, self.robot.heartbeat, entry["id"])
                elif not self.over(entry):
                    self.update(entry, self.robot.status, entry["id"])
            self.finish()
            self.tell()
        self.publish()

    def place(self, places):
        ids = {p["id"]: p for p in places}
        for name in [*self.served, *self.doors]:
            if name not in ids:
                self.get_logger().warn(f"{name} isn't one of this robot's doors or elevators (GET /v1/places)")
        for lift in self.served:
            self.served[lift] = [str(f) for f in ids.get(lift, {}).get("floors") or []]
        self.placed = True

    def tell(self):
        """The fire alarm on /fire_alarm_trigger, and the closures on /plynth/closures, whenever they change."""
        fire = any(n.get("kind") == "fire alarm" or n.get("kind") == "emergency" and n.get("emergency") == "fire alarm"
                   for n in self.notices)
        if fire != self.fire:
            self.fire = fire
            self.fire_alarm.publish(Bool(data=fire))
            if fire:
                self.get_logger().warn("Fire alarm on: /fire_alarm_trigger true")
            else:
                self.get_logger().info("No fire alarm: /fire_alarm_trigger false")
        closed = [n for n in self.notices if n.get("kind") in CLOSES]
        if closed != self.closed:
            for n in closed:
                if n not in (self.closed or []):
                    where = n.get("resource_id") or n.get("name")
                    self.get_logger().info(f"Closed to robots, {n['kind']}: {where}: {n.get('reason')}"
                                           + (f" (floors {n['floors']})" if n.get("floors") else ""))
            self.closed = closed
            self.closures.publish(String(data=json.dumps(closed)))

    def publish(self):
        now = self.get_clock().now().to_msg()
        emergency = any(n.get("kind") == "emergency" and n.get("emergency") != "all clear" for n in self.notices)
        for lift, floor in self.floor.items():
            down = [n.get("reason") or "" for n in self.notices
                    if n.get("kind") == "out of service" and n.get("resource_id") == lift]
            mode = (LiftState.MODE_OFFLINE if not self.online else
                    LiftState.MODE_FIRE if self.fire or any("recall" in r for r in down) else
                    LiftState.MODE_EMERGENCY if emergency else LiftState.MODE_OFFLINE if down else None)
            ride = self.rides.get(lift)
            state = self.state(ride) if ride else None
            # In an alarm or an outage nothing starts, but a robot aboard at its floor still gets out: clearing a car
            # is what the building asks. With no answer from the gateway nothing is known, so nothing shows open.
            serving = ride is not None and state not in REFUSED and (
                mode is None or self.online and state == "ARRIVED")
            msg = LiftState(lift_time=now, lift_name=lift, available_floors=self.served[lift], current_floor=floor,
                            destination_floor=(ride["to"] if ride["boarded"] else ride["from"]) if serving else floor,
                            available_modes=[LiftState.MODE_HUMAN, LiftState.MODE_AGV],
                            current_mode=mode or (LiftState.MODE_AGV if serving else LiftState.MODE_HUMAN),
                            session_id=ride["owner"] if serving else "")
            msg.door_state = LiftState.DOOR_OPEN if serving and state in AT_FLOOR else LiftState.DOOR_CLOSED
            msg.motion_state = self.motion(ride) if serving else LiftState.MOTION_STOPPED
            self.lift_states.publish(msg)
        for door in self.doors:  # Plynth keeps a door passage open through an alarm: the robot holds until it's over
            # open where the door's position switch read it open, or it says nothing: a badge door only unlocked
            # ("unlocked", on the badge system's word) may still be shut
            go = not (self.fire or emergency) and any(
                self.state(e) == "OPEN_COMMANDED" and e["status"].get("door") in (None, "opened")
                for (d, _), e in self.passes.items() if d == door)
            mode = DoorMode.MODE_OFFLINE if not self.online else DoorMode.MODE_OPEN if go else DoorMode.MODE_CLOSED
            self.door_states.publish(DoorState(door_time=now, door_name=door, current_mode=DoorMode(value=mode)))

    def motion(self, ride):
        if self.state(ride) in AT_FLOOR:
            return LiftState.MOTION_STOPPED
        floors = self.served[ride["resource"]]
        if ride["boarded"] and ride["from"] in floors and ride["to"] in floors:
            up = floors.index(ride["to"]) > floors.index(ride["from"])
            return LiftState.MOTION_UP if up else LiftState.MOTION_DOWN
        return LiftState.MOTION_UNKNOWN  # on its way to the robot from wherever it was: Plynth doesn't say


def main():
    rclpy.init()
    node = Adapter()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    node.destroy_node()
    rclpy.try_shutdown()


if __name__ == "__main__":
    main()
