# Changelog: the Plynth robot kit

## 0.6.0, 2026-10-30

- Signing. `RobotClient` signs each call with a new nonce and the gateway's host as its audience (`X-Nonce`,
  `X-Audience`, in the signed string between the timestamp and the body), with a shared secret or a one-way key alike.
  A building set up from a site file requires both, and refuses a copy of a call and a call signed for another
  building; the sandbox takes calls without them too. Against a gateway older than them, which answers
  `signature_mismatch` without the `nonces` a newer one's 401 carries, the client signs once more without them, as
  0.5.0 and older do, and so does every call after; a wrong key stays a mismatch.
- `call(..., key=...)` sends an `Idempotency-Key` with a call that makes something (help, a report, a charger, a permit,
  an incident, a booking, a handoff, a pass, the front desk's form), and keeps it when it signs the call again: sent
  again within the day, it's answered as the first was and nothing is made twice.
- Crossings. `report(passage_id, "going", clearance=status["clearance"]["token"])` reads a crossing's clearance back
  by its token, as a building requires; move only once the answer is `GOING`. Any other answer raises `GatewayError`
  409, with the passage's `state`, `reason_code`, `retry` and `retry_after` in its `body`. `request(...,
  approach="west")` names the side of a crossing the robot waits on.
- The stream. `stream()`'s timeout is 5 s by default (it was 30): a gateway of this release sends a keep-alive every
  2 s, so 5 s of silence means the connection is gone. Against an older gateway, which sends one every 15 s, pass
  `timeout=20`.
- `plynth.watch` reopens a stream that goes 5 s without a byte (`READ_S`), and reads its notices at once when an
  answer's `hold` says otherwise than its stream did. "Clear and hold" is clear, then hold: the robot clears any doorway,
  car or no-stop area it's in under its own navigation, then says so with `watch.holding()`, which tells the building
  and holds it; `watch.clearing` says what it's clearing for meanwhile. One that hasn't said so within the notice's
  `clear_in_s` (10 s at most, `watch.CLEAR_S`) is held anyway. A hold or a stay put still holds at once. A robot whose
  stream the building refuses (suspended, or its company stopped) is held until a stream opens again.
  `Watch(..., on_passage=...)` is called with each step of the robot's passages. A position the gateway refuses is
  logged once and is still word from the gateway; a 5xx, a gateway up but failing, is no answer.
- `plynth.ask` answers as data too (`structuredContent`, from MCP 2025-06-18 on). With `--acts`, its report tool sends
  a crossing's clearance token, and its words tell a model that a clearance lasts 20 seconds, to move at a crossing
  only once the answer to its `going` is `GOING`, and to leave crossings to the robot's own motion software when it
  takes more than a few seconds a turn.
- `plynth.conformance` records a check it couldn't run as skipped, with `passed` null, never as passed; a run where
  nothing ran neither passed nor failed (exit 1), and the sandbox is told only of runs where something ran.
- The ROS 2 node (`plynth-ros2.zip`). In a building, `plynth_node` sends your localizer's pose in your own map by
  default (`amcl_pose`, a `PoseWithCovarianceStamped`, frame `map`), which the building places from spots you both
  know; started at a building with odometry or the floor plate's calibration (`origin_x`, `origin_z`, `yaw`), it
  refuses to start, since wheel odometry drifts a robot into a false "lost" hold. On the sandbox its defaults are
  0.3.0's, odometry on the plate, with a warning. It follows the floor its ride arrives at. Holds go out on
  `/plynth/hold`; `/plynth/stop`, the old name, says the same for now and will be retired. A clear and hold waits for
  true on `/plynth/holding`, or its `clear_in_s`. `vendor:=unitree` has left the node, which refuses it at start: the
  kit sends no maker's motion or posture commands, so act on `/plynth/hold` in your own software (Plynth keeps the old
  code as an example for a maker or company to adapt and own: ask support@plynth.build).

## 0.5.0, 2026-10-30

- High-security doors. `RobotClient.ask_grant(doors, work_order=..., note=..., minutes=...)` asks for one trip through
  doors that ask for a grant per trip, and `grants()` lists this robot's, waiting or in force; a person at the building
  approves, and each door opens once on it. `presence(passage_id, code)` sends the door's code, as its beacon shows it,
  for a passage waiting in `AWAITING_PRESENCE`; or send `door_code=` in `request()` itself.
- ECDSA P-256 keys. `RobotClient`'s secret may also be a P-256 private key in PEM, the curve TPMs and security chips
  use and a maker's device certificate (IEEE 802.1AR) carries: signed over SHA-256, the signature DER. A signing
  function may return DER or r and s side by side (64 bytes), as a chip gives them.

## 0.4.0, 2026-10-25

- The building for a model: `plynth.ask` (`plynth-ask`), an MCP server over stdio that reads a robot's places,
  directions, notices and every rule (`GET /v1/rules`) for its own model. With `--acts` the model also asks for doors,
  elevator rides and crossings and reports what the robot did; without it, it never acts.
- One-way keys. `RobotClient`'s secret may be the robot's Ed25519 private key, as `ssh-keygen` writes it or in
  PEM (this needs the `cryptography` library), or a function that signs bytes and returns the 64-byte signature, such as the robot's key store or
  security chip, so the private key never sits in Python. The same string is signed, the signature sent in hex.
  `plynth.conformance` takes `--key robot.pem` (or `GATEWAY_ROBOT_KEY`) instead of a secret, and its bad-signature
  check forges an Ed25519 signature for a robot that uses one. Shared secrets work as before, standard library only.

## 0.3.1, 2026-10-25

- `plynth.hello`, `plynth.conformance` and `plynth.grade` say in one line when the `--env` file isn't there, with
  where they looked for it, rather than a traceback; `hello` says when the file doesn't set the robot ID, the secret
  or the address.
- The commands everywhere run from inside `robot-kit`, with `plynth-sandbox.env` saved beside it:
  `--env ../plynth-sandbox.env`.

## 0.3.0, 2026-10-23

- `plynth.watch`: a robot kept connected and held when the building says so. `Watch(robot, on_hold=..., pose=...)`
  keeps the stream open (reopened from its last event), sends the robot's position every second (or a heartbeat),
  confirms each hold it's told of, and calls `on_hold(held, why)` when what the robot must do changes: a hold notice
  (the building's Hold, a keep-out zone it's inside), a `stop`, an emergency that holds, or the link lost past the grace
  time where the owner's rule says hold (`lost_link` in `GET /v1/me`, or a `lost link` notice where the robot is; by
  default robots keep going). A hold in force when the link drops stays until all clear. `watch.help(kind, ...)` asks
  for a person. The ROS 2
  node `plynth_node` runs on it. An error in your own `on_hold` or `on_notices` is logged (`plynth.watch`) and never
  closes the stream or stops the watch's decisions; `on_hold` is called again a quarter second later until it takes
  what the robot must do.
- The conformance check, against the sandbox, tells the sandbox at its end whether every check passed: true or false,
  nothing else. The sandbox counts how many runs pass, so Plynth can see where people get stuck.

## 0.2.2, 2026-10-21

- The client waits out a 429 for you: it sends the call again after its `Retry-After`, up to `wait_429` seconds a
  call (10 by default; 0 raises at once), and a stream's connecting does the same. The conformance check waits up
  to 60, so a busy sandbox slows a run rather than failing it.

## 0.2.1, 2026-10-21

- `GatewayError.retry_after`: the seconds a 429 asks you to wait, from its `Retry-After`.
- No answer raises `URLError`, an `OSError`, never `GatewayError`: a timeout, a cut-off or garbled answer, or a 502,
  503 or 504 that isn't the gateway's own (a proxy's, the site's network's). Reconnect and send again, as the stream
  example does.
- `plynth.hello`'s next step keeps your `--env`.

## 0.2.0, 2026-10-21

- The kit is a package, `plynth`: `plynth.client`, `plynth.conformance` and `plynth.hello`. The old name, `gateway`,
  still works.
- A version, this changelog, a README and the Apache License 2.0. A wheel to `pip install`, and SHA-256 sums, on `/docs`.
- `plynth.hello` checks a key in one step.
- The conformance check reads `GATEWAY_URL`, `GATEWAY_ROBOT_ID` and `GATEWAY_ROBOT_SECRET`, or `--env` with the file
  your company's page gives you. Against the sandbox it uses the sandbox's door and elevator unless you name others.
- Its report records the kit's version, the gateway's release and the API version. It says when the kit answered a
  drill for your robot, and it skips a check your key may not run, saying why.
- The client signs the path it sends, a path in the gateway's address included.
- `plynth.grade`: the sandbox grades your own software on what it saw your robot do, and every check passed gives a
  certificate, sandbox-verified. The client's `test_run=` sends `X-Test-Run`, and the conformance check marks its
  own trips with it, so a building's record shows them as a test and the grade leaves them out.

## 0.1.0, 2026-10-15

- The reference client and the conformance check, as the sandbox's `/docs` first served them.
