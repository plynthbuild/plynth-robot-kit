# Plynth for ROS 2

Three ROS 2 packages that let a robot running ROS use Plynth's robot API: be on the building's map and held when the
building says so, go through a door or ride an elevator, and let the building owner's rules decide. Python (rclpy) on
ROS 2 Jazzy and Humble, Apache-2.0, maintained by Plynth. All use the robot kit (`plynth.client`, `plynth.watch`),
unchanged.

| Package | Executable | What it is |
|---|---|---|
| `plynth_node` | `plynth_node` | The robot node: where the robot is (in a building, its localizer's pose in your own map; on the sandbox, its odometry on the floor plate), the building's holds on `/plynth/hold` for a twist_mux lock, and the keep-out zones. Start here, with your company's own keys: it sends positions, which a sandbox test key may not (403). |
| `plynth_ros_example` | `ride` | Walks through a door, then rides an elevator, from one node. The example to read. |
| `plynth_rmf_adapter` | `adapter` | An Open-RMF lift and door adapter: lift sessions and door requests become Plynth passages. Its protocol and limits are in [its README](src/plynth_rmf_adapter/README.md). |

## plynth_node

It needs a company's own key: a test key from the sandbox's `/docs` rides and uses the doors only, so its positions
are refused (403, `not_for_this_key`). `ride` and the adapter work with either.

On the sandbox, which places your odometry on the floor plate, as it has since kit 0.3.0:

```
set -a; . ./plynth-sandbox.env; set +a   # GATEWAY_URL, GATEWAY_ROBOT_ID and GATEWAY_ROBOT_SECRET
ros2 run plynth_node plynth_node --ros-args -p pose_topic:=/odom -p origin_x:=0.5 -p origin_z:=0.5
ros2 run twist_mux twist_mux --ros-args --params-file $(ros2 pkg prefix plynth_node)/share/plynth_node/config/twist_mux.yaml -r cmd_vel_out:=cmd_vel
```

In a building, with your robot's keys there and the building's `GATEWAY_TLS_PIN`, the node's defaults send your
localizer's pose in your own map (twist_mux as above):

```
ros2 run plynth_node plynth_node
```

**Where the robot is, in a building.** Your localizer's pose, read from `pose_topic` (`amcl_pose` by default, a
`PoseWithCovarianceStamped`, heard as AMCL keeps it, `transient_local`, so a node started beside a robot standing still
is told where it is; `pose_type:=pose` for a `PoseStamped`), in metres in your own map, and sent every `period` seconds,
on `floor`. Positions in your map need the building to have placed it on its floor for your company, from spots you
both know; until then the building refuses them (400), and the node's log says why. Only a pose in your map's frame is
sent: one whose header names another frame than `map_frame` (`map`) isn't, since odometry's frame drifts. After a ride,
the node takes the floor the car arrived at, as the building records it; `ros2 param set /plynth_node floor 3` sets it
by hand. If no pose has come 5 s after the node starts, its log says what it listens for: a launch file for another
topic or message type sends nothing.

**On the sandbox**, the node's defaults are kit 0.3.0's, so launch files written for them keep working there:
odometry (`pose_type` `odometry`, a `nav_msgs/Odometry` on `odom`) placed on the floor plate by hand (`frame` `plate`),
with a warning, since wheel odometry drifts with every metre and a building holds a robot that drifts off its route as
lost. A building refuses both at start. `origin_x` and `origin_z` (0 to 1) say where your frame's origin is on the
plate, `yaw` (radians) how far your x axis is turned anticlockwise from the plate's x, east, seen from above, as ROS
turns, and the plate's size comes from the building (or set `plate_w_m` and `plate_d_m`). Your +y is north, the
plate's z runs south. A point off the plate isn't sent. With `frame:=map` these five are refused at start, since your
map doesn't use them. To try the building's way on the sandbox, ask Plynth to place your map, then add
`-p frame:=map`: the pose's defaults follow the frame, your localizer's on `amcl_pose` in a map.

**Holding.** `/plynth/hold` (`std_msgs/Bool`) goes out `hold_hz` (4) times a second: true while the robot must hold.
`/plynth/stop`, its old name, says the same, so launch files written for it keep working; it will be retired. Use it as
a twist_mux lock with a timeout, as `config/twist_mux.yaml` does: while it says true, or says nothing for half a second
(the node stopped), twist_mux passes none of your velocity commands below the lock's priority. It's true while:

- the building holds your robot: the owner's Hold (every robot, your company's or yours), confirmed for you (holding);
- your robot is inside a keep-out zone, or an owner's keep-out rule in its hours, until it's out or the zone is
  lifted or the rule's hours end;
- a `stop` notice holds it (a lost robot, or one the owner has suspended or whose company it has stopped, whose
  stream is refused until it's resumed), or an emergency tells it to hold or stay put;
- an emergency tells it to clear and hold, once it has cleared: see below;
- the link to the building is lost past the grace time, where the owner's rule says hold: for your company (read from
  `GET /v1/me`), or in the place the robot is (a `lost link` notice), each kept for when the link is gone. By default
  robots keep going; doors and elevators still refuse. A hold in force when the link drops stays until all clear.

**Clear and hold is clear, then hold.** Holding at once could leave your robot in a fire door's swing, the one place the
notice says to clear. So the node doesn't hold it at once: `/plynth/held` lists the emergency in `clearing`, and the log
says so. Your robot's own navigation clears any doorway, car or no-stop area it's in, then publishes true on
`/plynth/holding` (`std_msgs/Bool`): the node tells the building (holding) and the hold starts. If your robot hasn't said
so within the notice's `clear_in_s` (10 s at most, the fire doors' delay), the node holds it anyway. `/plynth/holding`
is a signal, not a state: a change to true while the emergency is in `clearing` counts, so publish true once it has
cleared, or false while it moves and true when it stops clear; a true it says over and over counts once.

`zero_twist:=true` also publishes a zero `Twist` on `cmd_vel_topic` while held. A loop of your own that sends velocity
commands outside twist_mux talks over a hold: put every command through it. Also latched: `/plynth/held` (`{"held",
"why", "clearing"}`), `/plynth/notices` and `/plynth/keepouts` (the keep-out zones on your floors, keep-out rules in
their hours among them, as JSON).

**A software hold, never a safety function.** The twist_mux lock and `zero_twist` are holds your company installs by
choice, inside your robot and beneath its own safety layer. Sent over a network, a hold arrives late and can be lost:
it supplements, never replaces, the robot's own safety controls and a person with its remote. A robot whose software
doesn't follow it carries on.

**`vendor:=unitree` has left the node** (2026-10-30), and the node refuses it at start. It sent Unitree's `StopMove` and
`BalanceStand` into the robot's own sport API when a hold started: a motion and a posture command on a building notice,
which is steering, and Plynth never steers. And from January 2027 the EU Machinery Regulation lets a supervisor stop or
move such a machine remotely only when it can see the machine's movement and working area. Act on `/plynth/hold` in your
own software. Plynth keeps that code as an example for a robot's maker or company to adapt and own; ask
support@plynth.build for it.

## Use it with the sandbox

1. Install the robot kit's client: `pip install https://sandbox.plynth.build/docs/kit/plynth_robot_kit-0.6.0-py3-none-any.whl`
   (on Ubuntu 24.04, add `--break-system-packages`).
2. Copy `src/plynth_ros_example` and `src/plynth_rmf_adapter` into your workspace's `src/`. For the adapter,
   `apt install ros-jazzy-rmf-lift-msgs ros-jazzy-rmf-door-msgs`. Then `colcon build`.
3. Load your robot's key from the file your company's page gives you with each secret, and run `ride` (below):
   `set -a; . ./plynth-sandbox.env; set +a`.

Or in Docker: put the kit's wheel next to the `Dockerfile` and `docker build -t plynth-ros2 .`.

## ride

```
set -a; . ./plynth-sandbox.env; set +a   # GATEWAY_URL, GATEWAY_ROBOT_ID and GATEWAY_ROBOT_SECRET
ros2 run plynth_ros_example ride --ros-args -p door:=door -p elevator:=lift -p from_floor:=1 -p to_floor:=2
```

| Parameter | Default | |
|---|---|---|
| `url`, `robot_id`, `secret` | `GATEWAY_URL`, `GATEWAY_ROBOT_ID`, `GATEWAY_ROBOT_SECRET` | Use the environment for the secret: any node can read a parameter |
| `door` | `door` | The door's Plynth ID |
| `elevator` | `lift` | The elevator's Plynth ID |
| `from_floor`, `to_floor` | `1`, `2` | The ride, in the building's floor labels |

It logs each state, publishes each passage status as JSON on `/plynth/passage` and the robot's notices on
`/plynth/notices` (`std_msgs/String`, both latched). A refusal says why (`reason_code`); while it says waiting can help
(`retry`), the node asks again after `retry_after` (5 s when it doesn't say), at most three times, and not at all past
a minute. No answer from the gateway is a no: the node stops and exits 1, except in the car, where it stays aboard and
keeps asking until the ride arrives. A ride that ends with the robot aboard says what to do in the car
(`aboard_action`: get out, get out at your floor, or stay put until a person comes), and the node logs that. In a
building, the URL is `https://` and `GATEWAY_TLS_PIN` holds the fingerprint the building gives.

## License

Open source under the Apache License 2.0 (`LICENSE`, here and in each package), copyright 2026 PLYNTH 3D Inc.
