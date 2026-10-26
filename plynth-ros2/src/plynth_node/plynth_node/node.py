"""Plynth's robot node (plans/onboarding-build-2026-10-23.md, item 2): a ROS 2 robot connected to a building through
Plynth from its keys, in minutes. It tells the building where the robot is, holds the robot when the building says so,
and tells the robot the keep-out zones. It runs on the robot kit's plynth.watch.

    set -a; . ./plynth.env; set +a          # GATEWAY_URL, GATEWAY_ROBOT_ID, GATEWAY_ROBOT_SECRET (and a building's
    ros2 run plynth_node plynth_node        # GATEWAY_TLS_PIN)

Where the robot is: from `pose_topic` (nav_msgs/Odometry by default; `pose_type` pose or pose_cov for a PoseStamped or
a PoseWithCovarianceStamped such as /amcl_pose), sent every `period` seconds. With `frame` "plate" it's placed on the
floor plate by calibration: `origin_x` and `origin_z` (0 to 1, where the robot frame's origin is on the plate), `yaw`
(radians, the robot frame's x axis against the plate's), and the plate's size, which the building gives (or
`plate_w_m` and `plate_d_m`). With `frame` "map", positions go in metres in the fleet's own map, which the building has
placed on its floor (a frame for your company). `floor` is the floor it's on.

Holding: /plynth/stop (std_msgs/Bool) is published `stop_hz` times a second, true while the robot must hold. Use it as
a twist_mux lock with a timeout (config/twist_mux.yaml): if this node stops, the lock engages and the robot holds, the
safe side. It's true while a notice holds the robot (the building's Hold, a keep-out zone it's inside, a stop, an
emergency that holds; one in force when the link drops stays until all clear), and when the link to the building is
lost past the grace time, where the owner's rule says hold (for your company, or in the place the robot is; by default
robots keep going). Each hold is confirmed to the building (holding). Optionally `zero_twist` also publishes a zero
Twist on `cmd_vel_topic` while held, and `vendor` "unitree" sends Unitree's StopMove then BalanceStand on
/api/sport/request when a hold starts (never Damp: a standing humanoid falls).

Also, latched: /plynth/held (std_msgs/String, JSON {"held", "why"}), /plynth/notices and /plynth/keepouts (the keep-out
zones on the robot's floors, JSON). A hold over a network is a software hold, not an emergency stop: keep the robot's own
e-stop, and its remote, in a person's hand.
"""
import json
import math
import os
import threading

import rclpy
from geometry_msgs.msg import Twist
from rcl_interfaces.msg import ParameterDescriptor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile
from std_msgs.msg import Bool, String

from plynth.client import RobotClient
from plynth.watch import Watch

STOP_MOVE, BALANCE_STAND = 1003, 1002  # Unitree's sport API (unitree_api/msg/Request); 1001, Damp, is never sent


class PlynthNode(Node):
    def __init__(self):
        super().__init__("plynth_node")

        def setting(name, env):  # the parameter, or else the environment (use it for the secret)
            return self.declare_parameter(name, "").value or os.environ.get(env, "")

        any_type = ParameterDescriptor(dynamic_typing=True)  # -p floor:=2 arrives as a number, B as text
        p = lambda name, default: self.declare_parameter(name, default, any_type).value  # noqa: E731  (4 or 4.0)
        self.floor = str(self.declare_parameter("floor", "1", any_type).value)
        self.frame = p("frame", "plate")
        self.origin = (float(p("origin_x", 0.5)), float(p("origin_z", 0.5)))
        self.yaw = float(p("yaw", 0.0))
        self.plate = (float(p("plate_w_m", 0.0)), float(p("plate_d_m", 0.0)))
        self.stop_hz, self.zero, self.vendor = float(p("stop_hz", 4.0)), bool(p("zero_twist", False)), p("vendor", "none")
        if self.frame not in ("plate", "map") or self.vendor not in ("none", "unitree"):
            raise ValueError("frame is plate or map; vendor is none or unitree")
        self.robot = RobotClient(setting("url", "GATEWAY_URL"), setting("robot_id", "GATEWAY_ROBOT_ID"),
                                 setting("secret", "GATEWAY_ROBOT_SECRET"), timeout=3,
                                 pin=os.environ.get("GATEWAY_TLS_PIN") or None)
        latched = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self.stop_out = self.create_publisher(Bool, p("stop_topic", "plynth/stop"), 10)
        self.held_out = self.create_publisher(String, "plynth/held", latched)
        self.notices_out = self.create_publisher(String, "plynth/notices", latched)
        self.keepouts_out = self.create_publisher(String, "plynth/keepouts", latched)
        self.twist_out = self.create_publisher(Twist, p("cmd_vel_topic", "cmd_vel"), 10) if self.zero else None
        self.sport = self._unitree() if self.vendor == "unitree" else None
        self.at, self.lock = None, threading.Lock()
        self._listen_pose(p("pose_topic", "odom"), p("pose_type", "odometry"))
        if self.frame == "plate" and not all(self.plate):
            self.plate = self._plate()
        self.watch = Watch(self.robot, on_hold=self._on_hold, pose=self._where, every=float(p("period", 1.0)),
                           on_notices=self._on_notices).start()
        self.create_timer(1.0 / self.stop_hz, self._tick)
        self.get_logger().info(f"Connected as {self.robot.robot_id} to {self.robot.base_url}; /plynth/stop at"
                               f" {self.stop_hz:g} Hz; if the link is lost: {self.watch.rule['action']}"
                               f" after {self.watch.rule['grace_s']:g} s")

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
            raise ValueError("pose_type is odometry, pose or pose_cov")

        def got(msg):
            point = get(msg)
            with self.lock:
                self.at = (point.x, point.y)
        self.create_subscription(Msg, topic, got, 10)

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
        """(floor, x, z) on the plate, ("map", floor, x, y) in the fleet's map, or None before the first pose."""
        with self.lock:
            at = self.at
        if at is None:
            return None
        if self.frame == "map":
            return ("map", self.floor, at[0], at[1])
        if not all(self.plate):
            return None
        c, s = math.cos(self.yaw), math.sin(self.yaw)
        x = self.origin[0] + (c * at[0] - s * at[1]) / self.plate[0]
        z = self.origin[1] + (s * at[0] + c * at[1]) / self.plate[1]
        return (self.floor, min(1.0, max(0.0, x)), min(1.0, max(0.0, z)))

    def _on_hold(self, held, why):
        self.stop_out.publish(Bool(data=held))  # at once, not at the next tick: the lock engages as soon as it can
        self.held_out.publish(String(data=json.dumps({"held": held, "why": why})))
        if held:  # a log call for each severity: rclpy refuses a second severity from one line (ValueError), which at
            self.get_logger().warn(f"Hold: {', '.join(why)}")  # every release closed the watch's stream for a second
        else:  # and could stop its decisions (the hackathon test, 2026-10-23)
            self.get_logger().info("Released: the robot may go on")
        if held and self.sport is not None:
            self.sport(STOP_MOVE)
            self.sport(BALANCE_STAND)

    def _on_notices(self, notices):
        self.notices_out.publish(String(data=json.dumps(notices)))
        zones = [n for n in notices if n.get("kind") == "keep out"]
        self.keepouts_out.publish(String(data=json.dumps(zones)))

    def _tick(self):
        """/plynth/stop, steadily: a twist_mux lock reads silence as a hold."""
        self.stop_out.publish(Bool(data=self.watch.held))
        if self.watch.held and self.twist_out is not None:
            self.twist_out.publish(Twist())

    def _unitree(self):
        """Unitree's sport API on /api/sport/request (unitree_ros2): StopMove, then BalanceStand. Never Damp."""
        try:
            from unitree_api.msg import Request
        except ImportError:
            self.get_logger().error("vendor unitree: unitree_api isn't installed (unitree_ros2); holding by /plynth/stop only")
            return None
        out = self.create_publisher(Request, "/api/sport/request", 10)

        def send(api_id):
            msg = Request()
            msg.header.identity.api_id = api_id
            msg.parameter = "{}"
            out.publish(msg)
        return send


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
