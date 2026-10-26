# Plynth for ROS 2

Three ROS 2 packages that let a robot running ROS use Plynth's robot API: be on the building's map and held when the
building says so, go through a door or ride an elevator, and let the building owner's rules decide. Python (rclpy) on
ROS 2 Jazzy and Humble, Apache-2.0, maintained by Plynth. All use the robot kit (`plynth.client`, `plynth.watch`),
unchanged.

| Package | Executable | What it is |
|---|---|---|
| `plynth_node` | `plynth_node` | The robot node: where the robot is, the building's holds on `/plynth/stop` for a twist_mux lock, and the keep-out zones. Start here, with your company's own keys: it sends positions, which a sandbox test key may not (403). |
| `plynth_ros_example` | `ride` | Walks through a door, then rides an elevator, from one node. The example to read. |
| `plynth_rmf_adapter` | `adapter` | An Open-RMF lift and door adapter: lift sessions and door requests become Plynth passages. Its protocol and limits are in [its README](src/plynth_rmf_adapter/README.md). |

## plynth_node

It needs a company's own key: a test key from the sandbox's `/docs` rides and uses the doors only, so its positions
are refused (403, `not_for_this_key`). `ride` and the adapter work with either.

```
set -a; . ./plynth-sandbox.env; set +a   # GATEWAY_URL, GATEWAY_ROBOT_ID and GATEWAY_ROBOT_SECRET
ros2 run plynth_node plynth_node --ros-args -p pose_topic:=/odom -p origin_x:=0.5 -p origin_z:=0.5
ros2 run twist_mux twist_mux --ros-args --params-file $(ros2 pkg prefix plynth_node)/share/plynth_node/config/twist_mux.yaml -r cmd_vel_out:=cmd_vel
```

**Where the robot is.** Read from `pose_topic` (`nav_msgs/Odometry` by default; `pose_type:=pose` for a `PoseStamped`,
`pose_cov` for a `PoseWithCovarianceStamped` such as `/amcl_pose`) and sent every `period` seconds, on `floor`. With
`frame:=plate` (the default) it's placed on the building's floor plate by calibration: `origin_x` and `origin_z` (0 to
1, where your frame's origin is on the plate), `yaw` (radians, your x axis against the plate's) and the plate's size
in metres, which the building gives (or set `plate_w_m` and `plate_d_m`). With `frame:=map`, positions go in metres in
your own map, once the building has placed it on its floor for your company.

**Holding.** `/plynth/stop` (`std_msgs/Bool`) goes out `stop_hz` (4) times a second: true while the robot must hold.
Use it as a twist_mux lock with a timeout, as `config/twist_mux.yaml` does: while it says true, or says nothing for half
a second (the node stopped), twist_mux passes none of your velocity commands below the lock's priority. It's true while:

- the building holds your robot: the owner's Hold (every robot, your company's or yours), confirmed for you (holding);
- your robot is inside a keep-out zone, until it's out or the zone is lifted;
- a `stop` notice holds it (a lost robot), or an emergency tells it to hold, clear and hold or stay put;
- the link to the building is lost past the grace time, where the owner's rule says hold: for your company (read from
  `GET /v1/me`), or in the place the robot is (a `lost link` notice), each kept for when the link is gone. By default
  robots keep going; doors and elevators still refuse. A hold in force when the link drops stays until all clear.

`zero_twist:=true` also publishes a zero `Twist` on `cmd_vel_topic` while held. `vendor:=unitree` sends Unitree's
`StopMove` and then `BalanceStand` on `/api/sport/request` when a hold starts (needs `unitree_ros2`; never `Damp`: a
standing humanoid falls). A loop of your own that sends velocity commands outside twist_mux talks over a hold: put every
command through it. Also latched: `/plynth/held` (`{"held", "why"}`), `/plynth/notices` and `/plynth/keepouts` (the
keep-out zones on your floors, as JSON).

**It's a hold, not an emergency stop.** A software hold sent over a network supplements, never replaces, the robot's own
e-stop and a person with its remote. A robot whose software doesn't follow it carries on.

## Use it with the sandbox

1. Install the robot kit's client: `pip install https://sandbox.plynth.build/docs/kit/plynth_robot_kit-0.4.0-py3-none-any.whl`
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
keeps asking until the ride arrives. In a building, the URL is `https://` and `GATEWAY_TLS_PIN` holds the fingerprint
the building gives.

## License

Open source under the Apache License 2.0 (`LICENSE`, here and in each package), copyright 2026 PLYNTH 3D Inc.
