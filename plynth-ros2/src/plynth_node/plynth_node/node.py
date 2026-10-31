"""Plynth's robot node (plans/onboarding-build-2026-10-23.md, item 2): a ROS 2 robot connected to a building through
Plynth from its keys, in minutes. It tells the building where the robot is, holds the robot when the building says so,
and tells the robot the keep-out zones. It runs on the robot kit's plynth.watch.

    set -a; . ./plynth.env; set +a          # GATEWAY_URL, GATEWAY_ROBOT_ID, GATEWAY_ROBOT_SECRET (and a building's
    ros2 run plynth_node plynth_node        # GATEWAY_TLS_PIN)

Where the robot is, in a building: its localizer's pose, from `pose_topic` (amcl_pose, a PoseWithCovarianceStamped,
by default, heard as AMCL keeps it, transient_local; `pose_type` pose for a PoseStamped), in metres in the fleet's own
map, which the building has placed on its floor (a frame for your company); sent every `period` seconds, and only from
a pose in that map's frame (`map_frame`, map). On `floor`, until a ride arrives at another, as the building records it
(or set `floor` while it runs). No pose within POSE_WAIT_S of starting is logged, with what the node listens for. For
the sandbox only, and refused anywhere else, its defaults there as in kit 0.3.0: `frame` plate, and on the plate
`pose_type` odometry (a nav_msgs/Odometry, on odom, which drifts with every metre), placed on the floor plate by hand:
`origin_x` and `origin_z` (0 to 1, where the robot frame's origin is on the plate), `yaw` (radians, the robot frame's x
axis turned anticlockwise from the plate's x, east, seen from above, as ROS turns) and the plate's size, which the
building gives (or `plate_w_m` and `plate_d_m`); any of these with `frame` map is refused. A point off the plate isn't
sent.

Holding: /plynth/hold (std_msgs/Bool) is published `hold_hz` times a second, true while the robot must hold, and so is
/plynth/stop, its old name, until it's retired. Use it as a twist_mux lock with a timeout (config/twist_mux.yaml): if
this node stops, the lock engages and the robot holds. It's true while a notice holds the robot (the building's Hold, a
keep-out zone it's inside, a stop, an emergency that holds; one in force when the link drops stays until all clear),
and when the link to the building is lost past the grace time, where the owner's rule says hold (for your company, or
in the place the robot is; by default robots keep going). Each hold is confirmed to the building (holding). A "clear
and hold" is clear, then hold: the robot's own navigation clears any doorway, car or no-stop area it's in, then
publishes true on /plynth/holding, which tells the building (holding), and the hold starts; one that hasn't said so
within the notice's clear_in_s (10 s at most) is held anyway. /plynth/holding is a signal, not a state: a change to
true while /plynth/held lists the emergency in clearing counts, and a true said over and over counts once. Optionally
`zero_twist` also publishes a zero Twist on `cmd_vel_topic` while held. The lock and zero_twist are software holds a
robot company installs by choice, inside its own robot and beneath the robot's own safety layer: never safety
functions.

Also, latched: /plynth/held (std_msgs/String, JSON {"held", "why", "clearing"}), /plynth/notices and /plynth/keepouts
(the keep-out zones on the robot's floors, JSON). Keep the robot's own safety controls, and its remote, in a person's
hand.
"""
import json
import math
import os
import threading
import time

import rclpy
from geometry_msgs.msg import Twist
from rcl_interfaces.msg import ParameterDescriptor
from rclpy.node import Node
from rclpy.parameter import Parameter
from rclpy.qos import DurabilityPolicy, QoSProfile
from std_msgs.msg import Bool, String

from plynth.client import RobotClient
from plynth.watch import Watch

# The robotics review (2026-10-29): StopMove and BalanceStand sent into a robot's own API on a building notice are the
# kit commanding its motion and posture, which "never steers" rules out
UNITREE = ("vendor unitree has left plynth_node: it sent Unitree's StopMove and BalanceStand into a robot's own API on a"
           " building notice, commanding its motion and posture, which Plynth never does. Act on /plynth/hold in your own"
           " software; the README says why, and how to ask for the example to adapt and own")
SANDBOX_ONLY = (("frame plate", "a hand-set origin and yaw on the plate, outside the frame the building places for your"
                 " company"),
                ("pose_type odometry", "odometry drifts with every metre, and a building holds a robot that drifts off"
                 " its route as lost"))
PLATE = {"origin_x": 0.5, "origin_z": 0.5, "yaw": 0.0, "plate_w_m": 0.0, "plate_d_m": 0.0}  # frame plate's, by hand
POSE_WAIT_S = 5  # no pose by then: the log says what the node listens for (a launch file for another pose, say)


class PlynthNode(Node):
    def __init__(self):
        super().__init__("plynth_node")

        def setting(name, env):  # the parameter, or else the environment (use it for the secret)
            return self.declare_parameter(name, "").value or os.environ.get(env, "")

        any_type = ParameterDescriptor(dynamic_typing=True)  # -p floor:=2 arrives as a number, B as text
        p = lambda name, default: self.declare_parameter(name, default, any_type).value  # noqa: E731  (4 or 4.0)
        self.declare_parameter("floor", "1", any_type)  # read each time: a ride's arrival sets it (_on_passage)
        self.hz = float(p("hold_hz", 0.0) or p("stop_hz", 4.0))  # stop_hz: hold_hz's old name
        self.zero, vendor = bool(p("zero_twist", False)), p("vendor", "none")
        if vendor == "unitree":
            raise ValueError(UNITREE)
        if vendor != "none":
            raise ValueError("vendor is none")
        self.robot = RobotClient(setting("url", "GATEWAY_URL"), setting("robot_id", "GATEWAY_ROBOT_ID"),
                                 setting("secret", "GATEWAY_ROBOT_SECRET"), timeout=3,
                                 pin=os.environ.get("GATEWAY_TLS_PIN") or None)
        sandbox = self._on_sandbox()  # its defaults there are kit 0.3.0's, odometry on the plate, so launch files
        # written for them keep working; a building's are the localizer's pose in the map it places for the company
        self.frame, self.map_frame = p("frame", "plate" if sandbox else "map"), p("map_frame", "map")
        plate = self.frame == "plate"  # the pose's defaults follow the frame: on the plate, odometry on odom (0.3.0)
        kind = p("pose_type", "odometry" if plate else "pose_cov")
        topic = p("pose_topic", "odom" if plate else "amcl_pose")
        if self.frame not in ("map", "plate"):
            raise ValueError("frame is map or plate")
        hand = {name: p(name, None) for name in PLATE}  # the plate's, set by hand: None where they aren't
        if not plate and (given := [name for name, value in hand.items() if value is not None]):
            raise ValueError(f"{', '.join(given)} place a pose on the floor plate (frame plate, the sandbox's), and are"
                             " ignored in your own map (frame map): drop them, or on the sandbox set frame plate")
        ox, oz, self.yaw, w, d = (float(PLATE[name] if value is None else value) for name, value in hand.items())
        self.origin, self.plate = (ox, oz), (w, d)
        used = [(what, why) for (what, why), on in zip(SANDBOX_ONLY, (plate, kind == "odometry")) if on]
        if used and not sandbox:
            what, why = used[0]
            raise ValueError(f"{what} is for the sandbox only: {why}. In a building, send your localizer's pose (pose_type"
                             " pose_cov, on amcl_pose, say) in your own map (frame map), once the building has placed it"
                             " on its floor")
        for what, why in used:
            self.get_logger().warn(f"{what}, for the sandbox only: {why}")
        latched = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self.hold_out = self.create_publisher(Bool, p("hold_topic", "plynth/hold"), 10)
        self.stop_out = self.create_publisher(Bool, p("stop_topic", "plynth/stop"), 10)  # the old name, until retired
        self.held_out = self.create_publisher(String, "plynth/held", latched)
        self.notices_out = self.create_publisher(String, "plynth/notices", latched)
        self.keepouts_out = self.create_publisher(String, "plynth/keepouts", latched)
        self.twist_out = self.create_publisher(Twist, p("cmd_vel_topic", "cmd_vel"), 10) if self.zero else None
        self.at, self.lock, self.warned, self.clearing, self.holding_was = None, threading.Lock(), None, [], False
        self.started = time.monotonic()
        self._listen_pose(topic, kind)
        self.create_subscription(Bool, "plynth/holding", self._holding, 10)
        if self.frame == "plate" and not all(self.plate):
            self.plate = self._plate()
        self.watch = Watch(self.robot, on_hold=self._on_hold, pose=self._where, every=float(p("period", 1.0)),
                           on_notices=self._on_notices, on_passage=self._on_passage)
        self.watch.start()  # once self.watch is there: its first notices use it
        self.create_timer(1.0 / self.hz, self._tick)
        self.get_logger().info(f"Connected as {self.robot.robot_id} to {self.robot.base_url}; /plynth/hold at"
                               f" {self.hz:g} Hz; if the link is lost: {self.watch.rule['action']}"
                               f" after {self.watch.rule['grace_s']:g} s")

    def _on_sandbox(self):
        """Whether the gateway is the sandbox, as GET /v1/me says; no answer counts as a building."""
        try:
            return bool(self.robot.me().get("sandbox"))
        except Exception as e:  # noqa: BLE001
            self.get_logger().warn(f"The gateway didn't say whether it's the sandbox ({e}): taken as a building")
            return False

    def _listen_pose(self, topic, kind):
        if kind == "odometry":
            from nav_msgs.msg import Odometry as Msg
            get = lambda m: m.pose.pose.position  # noqa: E731
        elif kind == "pose":
            from geometry_msgs.msg import PoseStamped as Msg
            get = lambda m: m.pose.position  # noqa: E731
        elif kind == "pose_cov":
            from geometry_msgs.msg import PoseWithCovarianceStamped as Msg
            get = lambda m: m.pose.pose.position  # noqa: E731
        else:
            raise ValueError("pose_type is pose_cov, pose or odometry")

        def got(msg):
            point = get(msg)
            with self.lock:
                self.at = (point.x, point.y, msg.header.frame_id)
        # AMCL keeps its last pose for a subscriber that asks (transient_local, as Nav2's nav2_simple_commander
        # asks), so a node started beside a robot standing still is told where it is, not when it next moves
        # ponytail: by the topic's name; read the publishers' QoS (get_publishers_info_by_topic) if a localizer
        # publishes amcl_pose volatile, which this subscriber can't hear (rclpy logs the incompatible QoS)
        latched = topic.rsplit("/", 1)[-1] == "amcl_pose"
        self.create_subscription(Msg, topic, got, QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)
                                 if latched else 10)
        self.listening = f"{topic} (a {Msg.__name__}, pose_type {kind}{', transient_local' if latched else ''})"

    def _plate(self):
        """The floor plate's size in metres, as the building gives it (GET /v1/places), for placing on it."""
        try:
            size = self.robot.places().get("plate_m")
        except Exception as e:  # noqa: BLE001
            size = None
            self.get_logger().warn(f"The building didn't say its plate's size ({e}): set plate_w_m and plate_d_m")
        if not size:
            self.get_logger().warn("No plate size: positions aren't sent until plate_w_m and plate_d_m are set")
            return (0.0, 0.0)
        return (float(size[0]), float(size[1]))

    def _where(self):
        """(floor, x, z) on the plate, ("map", floor, x, y) in the fleet's map, or None: before the first pose, for a
        pose in another frame than the map's, and off the plate."""
        with self.lock:
            at = self.at
        if at is None:  # a launch file written for another pose, or no localizer yet: said, never silent
            if time.monotonic() - self.started > POSE_WAIT_S:
                return self._warn(f"No pose on {self.listening} in {POSE_WAIT_S} s: nothing is sent until one comes."
                                  " Set pose_topic and pose_type to your localizer's")
            return None
        floor = str(self.get_parameter("floor").value)
        if self.frame == "map":
            if at[2] and at[2] != self.map_frame:
                return self._warn(f"A pose in {at[2]}, not {self.map_frame}: not sent. Send your localizer's pose, in"
                                  " your map's frame (map_frame)")
            self._warn(None)
            return ("map", floor, at[0], at[1])
        if not all(self.plate):
            return None
        c, s = math.cos(self.yaw), math.sin(self.yaw)  # x east and y north (REP 103); the plate's z runs north to south
        x = self.origin[0] + (c * at[0] - s * at[1]) / self.plate[0]
        z = self.origin[1] - (s * at[0] + c * at[1]) / self.plate[1]
        if not (0 <= x <= 1 and 0 <= z <= 1):
            return self._warn(f"The robot is off the plate (x {x:.2f}, z {z:.2f}): not sent. Check origin_x, origin_z,"
                              " yaw and the plate's size")
        self._warn(None)
        return (floor, x, z)

    def _warn(self, text):
        """A warning, once until it changes (poses arrive many times a second); None, for _where."""
        if text and text != self.warned:
            self.get_logger().warn(text)
        self.warned = text

    def _on_passage(self, step):
        """A ride's arrival: the robot is on the floor its car came to, as the building records it, from then."""
        floor = (step.get("arrived") or {}).get("floor")
        if floor is not None and floor != str(self.get_parameter("floor").value):
            self.set_parameters([Parameter("floor", value=floor)])
            mine = step["arrived"].get("map_id")
            self.get_logger().info(f"On floor {floor}, where its elevator arrived" + (f" (your map {mine})" if mine else ""))

    def _holding(self, msg):
        """/plynth/holding true: the robot's own word that it has cleared, for a "clear and hold", and stopped. A
        signal, not a state: only a change to true while it's clearing counts, so a true said over and over (a robot's
        "stopped") never holds it where it stands; once one is taken, the next true counts again."""
        rose, self.holding_was = msg.data and not self.holding_was, msg.data
        if rose and self.watch.clearing:
            self.holding_was = False
            threading.Thread(target=self.watch.holding, daemon=True).start()  # its call to the building off ROS's thread

    def _hold(self, held):
        for out in (self.hold_out, self.stop_out):  # /plynth/stop: the old name, said too until it's retired
            out.publish(Bool(data=held))

    def _held(self, held, why):
        self.held_out.publish(String(data=json.dumps({"held": held, "why": why, "clearing": self.watch.clearing})))

    def _on_hold(self, held, why):
        self._hold(held)  # at once, not at the next tick: the lock engages as soon as it can
        self._held(held, why)
        if held:  # a log call for each severity: rclpy refuses a second severity from one line (ValueError), which at
            self.get_logger().warn(f"Hold: {', '.join(why)}")  # every release closed the watch's stream for a second
        else:  # and could stop its decisions (the hackathon test, 2026-10-23)
            self.get_logger().info("Released: the robot goes on under its own supervision")

    def _on_notices(self, notices):
        self.notices_out.publish(String(data=json.dumps(notices)))
        zones = [n for n in notices if n.get("kind") == "keep out"]
        self.keepouts_out.publish(String(data=json.dumps(zones)))
        clearing = self.watch.clearing
        if clearing != self.clearing:
            self.clearing = clearing
            self._held(self.watch.held, self.watch.why)
            if clearing:
                self.get_logger().warn(f"Clear and hold ({', '.join(clearing)}): clear any doorway, car or no-stop area"
                                       " under your own navigation, then publish true on /plynth/holding")

    def _tick(self):
        """/plynth/hold, steadily: a twist_mux lock reads silence as a hold."""
        self._hold(self.watch.held)
        if self.watch.held and self.twist_out is not None:
            self.twist_out.publish(Twist())


def main(args=None):
    rclpy.init(args=args)
    node = PlynthNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.watch.stop()
        node.destroy_node()
        rclpy.try_shutdown()
