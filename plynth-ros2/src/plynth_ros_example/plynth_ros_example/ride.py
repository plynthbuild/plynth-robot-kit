"""Walk through Plynth's door and ride its elevator, from a ROS 2 node: the example for robot companies.

    ros2 run plynth_ros_example ride --ros-args -p door:=door -p elevator:=lift -p from_floor:=1 -p to_floor:=2

The gateway's address, the robot's ID and its secret come from the parameters url, robot_id and secret, or from
GATEWAY_URL, GATEWAY_ROBOT_ID and GATEWAY_ROBOT_SECRET. Use the environment for the secret: any node on the ROS graph
can read a parameter. In a building, also set GATEWAY_TLS_PIN to the fingerprint the building gives you.

Each passage status goes out as JSON on /plynth/passage, and the robot's notices on /plynth/notices. A refusal is an
answer, not an error: it says why (reason_code) and whether waiting can help (retry, retry_after). No answer means
no: the node stops and exits 1. In the car, no answer means stay aboard and ask again until the ride arrives.
"""
import http.client
import json
import os
import sys
import time
import uuid

import rclpy
from rcl_interfaces.msg import ParameterDescriptor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile
from std_msgs.msg import String

from plynth.client import GatewayError, RobotClient

FINAL = {"PASSED", "RELEASED", "DENIED", "FAILED", "CANCELLED"}
NO_ANSWER = (OSError, http.client.HTTPException, ValueError)  # none, or half of one: a timeout, a drop, a restart
REFUSED = {"DENIED", "FAILED", "CANCELLED"}
TRIES = 3              # asks, when a refusal says waiting can help
LONGEST_WAIT_S = 60    # a longer retry_after is the building saying "not now": give up and plan another way
DOOR_WAIT_S = 150      # a door where a person approves robots waits up to two minutes
CAR_WAIT_S = 120
# A ride that ended with the robot maybe still in the car says what the robot does there (aboard_action); a value this
# node doesn't know is "get out".
ABOARD_ACTION = {
    "stay put": "Aboard, and the ride ended in a fault: stay put in the car until a person comes",
    "get out at your floor": "Aboard, and the building lost the car: get out only at your own floor, by your own "
                             "sensing, where the car stops with its doors open, then hold clear of them",
    "get out": "Aboard, and the ride didn't arrive: get out only where the car stops with its doors open, then hold "
               "clear of them, as the building's notices say",
}


class Ride(Node):
    def __init__(self):
        super().__init__("ride")

        def setting(name, env):  # the parameter, or else the environment
            return self.declare_parameter(name, "").value or os.environ.get(env, "")

        self.robot = RobotClient(setting("url", "GATEWAY_URL"), setting("robot_id", "GATEWAY_ROBOT_ID"),
                                 setting("secret", "GATEWAY_ROBOT_SECRET"),
                                 pin=os.environ.get("GATEWAY_TLS_PIN") or None)  # a building's certificate
        label = ParameterDescriptor(dynamic_typing=True)  # -p to_floor:=2 arrives as a number, B as text
        self.door = str(self.declare_parameter("door", "door", label).value)
        self.elevator = str(self.declare_parameter("elevator", "lift", label).value)
        self.from_floor = str(self.declare_parameter("from_floor", "1", label).value)
        self.to_floor = str(self.declare_parameter("to_floor", "2", label).value)
        latched = DurabilityPolicy.TRANSIENT_LOCAL  # a subscriber that joins late still gets them
        self.passage_out = self.create_publisher(String, "plynth/passage", QoSProfile(depth=50, durability=latched))
        self.notices_out = self.create_publisher(String, "plynth/notices", QoSProfile(depth=1, durability=latched))
        self.last = None

    def show(self, status):
        """Log a passage's state and publish its status, once per change."""
        if (status["passage_id"], status["state"]) != self.last:
            self.last = (status["passage_id"], status["state"])
            self.passage_out.publish(String(data=json.dumps(status)))
            where = f"{status['resource_id']}: {status['state']}" + (f", car {status['car']}" if status["car"] else "")
            if status["state"] in REFUSED:
                self.get_logger().warn(f"{where}: {status['reason']} (reason_code {status['reason_code']}, "
                                       f"retry {status['retry']}, retry_after {status['retry_after']})")
            else:
                self.get_logger().info(where + (f", approval code {status['approval_code']}: show it on the screen"
                                                if status.get("approval_code") else ""))
        return status

    def notices(self):
        """What to plan around now (closures, the fire alarm, emergencies...): read before each passage."""
        notices = self.robot.notices()
        self.notices_out.publish(String(data=json.dumps(notices)))
        for n in notices:
            self.get_logger().info(f"Notice: {n['kind']}" + (f" at {n['resource_id']}" if n.get("resource_id") else "")
                                   + (f": {n['reason']}" if n.get("reason") else ""))

    def ask(self, resource, **floors):
        """Ask to pass. After a refusal, ask again, under a new ID, only while it says waiting can help."""
        for attempt in range(1, TRIES + 1):
            passage = str(uuid.uuid4())  # new for each trip; sending the same request again would be safe
            status = self.show(self.robot.request(passage, resource, **floors))
            wait = status["retry_after"] if status["retry_after"] is not None else 5
            if status["state"] not in REFUSED or not status["retry"] or attempt == TRIES or wait > LONGEST_WAIT_S:
                return passage, status
            self.get_logger().info(f"Asking again in {wait} s")
            time.sleep(wait)

    def wait_for(self, passage, state, status, limit_s, aboard=False):
        """Poll about twice a second until `state`, the passage ends, or time runs out; the last status. Aboard, each
        poll is a check-in, so the building's people hear if the robot goes quiet in the car; and no answer (a car out
        of Wi-Fi, a 503) isn't a reason to get out: the robot stays in the car and asks again."""
        deadline = time.monotonic() + limit_s
        while status["state"] != state and status["state"] not in FINAL and time.monotonic() < deadline:
            time.sleep(0.5)
            poll = self.robot.heartbeat if aboard else self.robot.status
            try:
                status = self.show(poll(passage))
            except (*NO_ANSWER, GatewayError) as e:
                if not aboard or isinstance(e, GatewayError) and e.status != 429 and e.status < 500:
                    raise
                self.get_logger().warn(f"No answer ({e}): staying aboard, asking again", throttle_duration_sec=5)
        return status

    def run(self):
        self.notices()
        passage, status = self.ask(self.door)
        status = self.wait_for(passage, "OPEN_COMMANDED", status, DOOR_WAIT_S)
        if status["state"] != "OPEN_COMMANDED":
            if status["state"] not in FINAL:
                self.show(self.robot.cancel(passage))
            return False
        self.get_logger().info("Going through the door")  # a robot drives through here
        self.show(self.robot.report(passage, "passed"))

        self.notices()
        passage, status = self.ask(self.elevator, from_floor=self.from_floor, to_floor=self.to_floor)
        status = self.wait_for(passage, "CAR_ARRIVED", status, CAR_WAIT_S)
        if status["state"] != "CAR_ARRIVED":
            if status["state"] not in FINAL:
                self.show(self.robot.cancel(passage))  # no car waits for nobody
            return False
        self.get_logger().info(f"Boarding car {status['car']} on floor {self.from_floor}")  # drive in here
        status = self.show(self.robot.report(passage, "boarded"))
        status = self.wait_for(passage, "ARRIVED", status, CAR_WAIT_S, aboard=True)
        if status["state"] != "ARRIVED":  # aboard, a ride is never cancelled: the building ended it
            self.get_logger().error(ABOARD_ACTION.get(status.get("aboard_action"), ABOARD_ACTION["get out"]))
            return False
        self.get_logger().info(f"Getting out on floor {self.to_floor}")  # drive out here
        return self.show(self.robot.report(passage, "exited"))["state"] == "RELEASED"


def main():
    rclpy.init()
    node = Ride()
    log = node.get_logger()
    try:
        ok = node.run()
    except NO_ANSWER as e:  # no answer, a timeout, a certificate that doesn't match, half an answer
        log.error(f"No answer from the gateway ({e}): treating it as no, and stopping")
        ok = False
    except GatewayError as e:  # the gateway refused the call itself: a wrong key, a clock off by 30 s, a bad request
        log.error(f"The gateway refused the call: {e}")
        ok = False
    if ok:
        log.info(f"Done: through {node.door}, then {node.elevator} from floor {node.from_floor} to {node.to_floor}")
    node.destroy_node()
    rclpy.try_shutdown()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
