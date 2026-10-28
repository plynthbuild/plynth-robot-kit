# Plynth robot kit

The reference client and the conformance check for Plynth's robot API, in standard-library Python 3.9 or later.
Nothing to install: unzip it and run it. Or `pip install` the wheel from the sandbox's `/docs`.

## Start

1. **Your key.** A test key from `/docs`, or, on your company's page, *Get Secret* for each robot: each comes once,
   with a file, `plynth-sandbox.env`, holding the sandbox's address, the robot ID and the secret. Save it in the
   folder you unzip this kit in, beside `robot-kit`.
2. **Check it** (from inside this folder, `robot-kit`):

   ```
   python3 -m plynth.hello --env ../plynth-sandbox.env
   ```

   It says whether the gateway accepts the key, whose robot it is, whether your clock is right, and which doors and
   elevators the robot may use.
3. **Run the conformance check:**

   ```
   python3 -m plynth.conformance --env ../plynth-sandbox.env --report conformance.json
   ```

   It rides the elevator, goes through the door, runs the emergency drills, and prints PASS, SKIP or FAIL for each
   check. Against the sandbox it uses the sandbox's door and elevator; in a building, name them (`--door`,
   `--elevator`, `--from`, `--to`): there it really opens them. Add `--robot-answers` to have your own software, not
   the kit, answer the drills: that's the result a building wants.
4. **Build your integration** with the client, or from the guide in any language:

   ```python
   import uuid
   from plynth.client import RobotClient

   robot = RobotClient(url, robot_id, secret)       # in a building, add pin="sha256:..." from the building
   passage = str(uuid.uuid4())                      # a new ID for each trip; resending the same one is safe
   status = robot.request(passage, "lift", from_floor="1", to_floor="2")
   ```

5. **Get sandbox-verified**, with your company's own keys: a test key from `/docs` isn't graded (403). Run your own
   software, not the kit, through the sandbox's scenario places for a while, then:

   ```
   python3 -m plynth.grade --env ../plynth-sandbox.env --minutes 60
   ```

   The sandbox grades what it saw your robot do: a new passage ID for each trip, its reports at doors and on rides,
   waiting out refusals and 429s, resuming the stream, answering drills in time. Every check passed gives a
   certificate with a link a building can check. The kit's own trips don't count: it marks them with `X-Test-Run`.

The settings can also come from the environment: `GATEWAY_URL`, `GATEWAY_ROBOT_ID`, `GATEWAY_ROBOT_SECRET`, and for a
building's gateway `GATEWAY_TLS_PIN`.

## What's here

- `plynth/client.py`: the client. Signing, passages, the stream, notices, profile, positions, emergencies, help.
- `plynth/conformance.py`: the conformance check.
- `plynth/hello.py`: checks a key in one step.
- `plynth/grade.py`: the sandbox's grade of your own software, and its certificate. A company's own keys only.
- `plynth/watch.py`: a robot kept connected and held when the building says so. With `pose=` it sends positions,
  which need a company's own keys: a test key from `/docs` may not send them (403).
- `plynth/ask.py`: the building for a model, over MCP (stdio): `plynth-ask`, with the robot's settings in the
  environment. It reads the robot's places, directions, notices and every rule (`GET /v1/rules`), and says what the
  robot is here for. With `--acts`, for your robot's own model running on the robot, it also asks for doors, elevator
  rides and crossings and reports what the robot did, as the robot itself; the building's rules decide each one.
  Without it, it never acts, so it's safe for a person's assistant.
- `gateway/`: the kit's old name, kept so commands written for it (`python3 -m gateway.conformance`) still work.

## Version and license

The version is in `plynth/__init__.py` and every conformance report; what changed is in `CHANGELOG.md`. The kit is
open source under the Apache License 2.0 (`LICENSE`), copyright 2026 PLYNTH 3D Inc.: copy it into your own code as you
need. The guide, the API reference and
the signing walkthrough are at the sandbox's `/docs`.
