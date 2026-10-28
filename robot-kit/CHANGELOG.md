# Changelog: the Plynth robot kit

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
