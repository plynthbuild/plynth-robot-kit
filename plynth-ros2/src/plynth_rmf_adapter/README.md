# plynth_rmf_adapter

An Open-RMF lift and door adapter for Plynth's robot API. It presents the building's elevators and doors to Open-RMF
as lifts and doors: each lift session and each door request becomes a Plynth passage, which the building owner's rules
decide. Plynth's notices become `/fire_alarm_trigger` and `/plynth/closures`. Python (rclpy), ROS 2 Jazzy, Apache-2.0.

## Run it

```
export GATEWAY_URL=http://127.0.0.1:8765 GATEWAY_ROBOT_ID=<robot ID> GATEWAY_ROBOT_SECRET=<secret>
ros2 run plynth_rmf_adapter adapter --ros-args \
  --params-file $(ros2 pkg prefix plynth_rmf_adapter)/share/plynth_rmf_adapter/config/sandbox.yaml
```

In a building, the URL is `https://` and `GATEWAY_TLS_PIN` holds the fingerprint the building gives you. The YAML lists
which elevators and doors to present, by their Plynth IDs (`GET /v1/places`); each one's name in Open-RMF is its Plynth
ID. The address, robot ID and secret come only from the environment.

| Topic | Type | |
|---|---|---|
| `adapter_lift_requests` | `rmf_lift_msgs/LiftRequest` | read: each lift session's requests |
| `adapter_door_requests` | `rmf_door_msgs/DoorRequest` | read: each requester's door requests |
| `lift_states` | `rmf_lift_msgs/LiftState` | every second, for each elevator in the YAML |
| `door_states` | `rmf_door_msgs/DoorState` | every second, for each door in the YAML |
| `fire_alarm_trigger` | `std_msgs/Bool` | latched: true while the robot's notices carry the fire alarm |
| `plynth/closures` | `std_msgs/String` | latched JSON: what's closed to robots now, as Plynth says it |

Run Open-RMF's door supervisor as usual: the fleet adapter waits for its heartbeat before going through a door. This
adapter reads the same `adapter_door_requests`, so each requester's passage is its own.

## A lift session

1. The session's first request names both floors: `destination_floor` `"<from>:<to>"`, such as `"1:2"`.
2. The car comes. `lift_states` shows it at `<from>`, its door open, `MODE_AGV`, `session_id` the session's.
3. Aboard, the robot asks for `"<to>"` (or `"<to>:<to>"`). The adapter reports `boarded`, and the car goes.
4. It arrives: at `<to>`, door open.
5. Out, `REQUEST_END_SESSION`: the adapter reports `exited`. Before boarding, it cancels the call instead.

## A door

Asked open (`MODE_OPEN`), the door is a door passage, and `door_states` shows it open once Plynth says go through.
Asked closed, the adapter reports `passed`, or cancels a passage not yet let through.

## Check-ins and asking again

In a car or a doorway the adapter checks in every second (`POST /v1/passages/<id>/heartbeat`), which also asks the
car to hold its doors a few seconds more. A call that gets no answer, a 429 or a 5xx is sent again each second with
the same passage ID, so a slow or restarting gateway never gets a second car call.

## Limits

- **One robot identity per adapter.** Plynth decides per robot; an Open-RMF lift or door adapter serves every robot of
  every fleet. This one signs every passage as the one robot whose ID and secret it holds, so the building's rules,
  limits and record see that one robot, whichever Open-RMF robot asked. For a fleet, the building can give the fleet
  one identity and see it as one robot; an identity per robot would need the adapter to pick each robot's secret by its
  session ID, which it doesn't yet.
- **Plynth decides, by the owner's rules.** A refusal shows as the lift not serving the session (no `session_id`, its
  door shut) or the door staying shut, and the log says why: Plynth's reason, `reason_code`, `retry` and `retry_after`.
  Where Plynth says waiting can help, the adapter asks again, under a new passage, after `retry_after` (10 s when it
  doesn't say) while Open-RMF keeps asking. Otherwise it waits for a new session.
- **Both floors first.** Open-RMF's fleet adapter asks first for the robot's own floor, then, once aboard, for the
  destination, one floor at a time. Plynth calls a car only with both, as destination-dispatch elevators do. So a
  session's first request must be `"<from>:<to>"`, the convention Octa's LCI adapter uses; `rmf_fleet_adapter` as
  released sends one floor and needs that change. A first request with one floor isn't served, and the log says so.
- **A new passage each ride.** Open-RMF's `session_id` and `requester_id` are the robot's own name, the same every
  ride; a Plynth passage ID is new for each trip. The adapter makes one per session: the name, made safe, and a suffix.
- **Where the car is.** Plynth tells a robot about its own rides, never where a car is. `current_floor` is the floor of
  the last step this adapter saw (the pickup at `CAR_ARRIVED`, the destination at `ARRIVED`), `""` before its first
  ride. `motion_state` is up or down while its robot rides, unknown while the car comes for it.
- **Doors.** Open means Plynth said go through (`OPEN_COMMANDED`); Plynth doesn't report where the door is. Asked
  closed after that, the adapter reports `passed`: Open-RMF doesn't say whether the robot went through.
- **Modes, and why the door shuts with them.** `MODE_FIRE` while the fire alarm is on or the elevator is in fire
  recall; `MODE_EMERGENCY` while any other emergency is in the robot's notices, on any floor; `MODE_OFFLINE` when the
  gateway doesn't answer or the elevator is out of service; `MODE_AGV` while serving a session; `MODE_HUMAN` otherwise.
  `rmf_fleet_adapter` checks a lift's floor, door and session, not its mode, so in fire, emergency and outage the lift
  shows no session and its door shut, and no boarding starts. A robot aboard at its floor (`ARRIVED`) is still shown
  the open door and its session, to get out: clearing a car is what the building asks in an alarm. When the gateway
  doesn't answer, nothing is known, so nothing shows open: a robot aboard stays aboard until it answers again.
- **Doors in an alarm.** Plynth keeps a door passage already open through an alarm and refuses new ones. The adapter
  shows every door shut while the fire alarm or an emergency is on (on any floor), so no robot starts through, and
  open again after all clear if Plynth kept its passage. Doors show `MODE_OFFLINE` when the gateway doesn't answer.
- **A failed ride with the robot aboard** (fire recall, or a ride that outlasts the building's timeout) shows no
  session and its door shut, since Plynth doesn't say where that car is; the building's people are told to check on
  the robot. `REQUEST_END_SESSION` for it still reports `exited`, which the building takes late as word that the
  robot is out of the car. `rmf_fleet_adapter` sends that only while a lift shows its session, so after such a failure
  someone ends it by hand.
- **Check-ins vouch for the adapter, not the robot.** The building hears if check-ins stop in a car or doorway, but
  this adapter checks in for as long as a passage is there: Open-RMF doesn't tell it whether the robot is moving.
- **Emergencies.** `/fire_alarm_trigger` goes true with the fire alarm and false at the building's all clear (Octa's
  adapter leaves false to a person; here all clear is the building's own notice, and Plynth's rules still decide each
  door and ride asked for after it). Plynth's emergency notice says what each robot should do and where to go; this
  adapter doesn't pass that on, so read it from `GET /v1/notices` or the stream. Newer Open-RMF also reads
  `emergency_signal` (`rmf_fleet_msgs/EmergencySignal`) and marks `fire_alarm_trigger` deprecated; this adapter
  publishes only the latter.
- **Lane closures.** Open-RMF has no standard message for a building to say what's closed: its lane closure requests
  close a fleet's own lanes by their index in the fleet's graph, which only the fleet knows. So the adapter logs each
  closure, outage, no-robot window and keep-out zone in its robot's notices, with Plynth's reason, and publishes them
  on `/plynth/closures` (a JSON list of the notices as Plynth sent them) for the fleet adapter to close lanes with.
- **Restarts.** A restart forgets the sessions under way (a Plynth status doesn't carry a ride's floors to match one
  with). A call not yet boarded times out at the building; a robot left aboard is flagged to the building's people.
- **Tested** in Docker on ROS 2 Jazzy against Plynth's sandbox (simulated doors and elevators), driven with
  `ros2 topic pub`, in Plynth's own tests. Not yet with `rmf_fleet_adapter` itself, on Humble, or with a real elevator.
