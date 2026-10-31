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

   robot = RobotClient(url, robot_id, secret)       # on a building's own computer, add pin="sha256:..." from it
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
gateway on a building's own computer `GATEWAY_TLS_PIN`.

## What's here

- `plynth/client.py`: the client. Signing, passages, the stream (read with a 5 s timeout: a gateway of this release
  sends a keep-alive every 2 s; an older one, every 15 s, so against it pass `timeout=20`), notices, profile,
  positions, emergencies, help. It signs each call with a new nonce and the gateway's host as its audience, which a
  building requires. A gateway older than them refuses that as a signature mismatch; the client then signs without
  them, as kit 0.5.0 and older do. `call(..., key=...)` sends an `Idempotency-Key`, so a call that makes something (help, a
  report, a booking) can be sent again safely.
- `plynth/conformance.py`: the conformance check.
- `plynth/hello.py`: checks a key in one step.
- `plynth/grade.py`: the sandbox's grade of your own software, and its certificate. A company's own keys only.
- `plynth/watch.py`: a robot kept connected and held when the building says so. It reopens a stream silent for
  5 s, and reads its notices at once when an answer's `hold` says otherwise than its stream did, so a Hold reaches the
  robot within seconds even on a stream that died without a word. A stream refused (401, 403: the robot suspended,
  its company stopped) holds the robot until a stream opens again. Against a gateway from before this release, set
  `plynth.watch.READ_S = 20`, or it reopens its stream every few seconds. With `pose=` it sends positions, which need
  a company's own keys: a test key from `/docs` may not send them (403). A clear and hold is clear, then hold: your
  robot clears any doorway, car or area it's in, then calls `watch.holding()`, which confirms holding and holds it;
  one that hasn't within the notice's `clear_in_s` (10 s at most) is held anyway.
- `plynth/ask.py`: the building for a model, over MCP (stdio): `plynth-ask`, with the robot's settings in the
  environment. It reads the robot's places, directions, notices and every rule (`GET /v1/rules`), and says what the
  robot is here for. With `--acts`, for your robot's own model running on the robot, it also asks for doors, elevator
  rides and crossings and reports what the robot did, as the robot itself; the building's rules decide each one. A
  crossing's clearance must be read back (`going`, with the `clearance` token its CLEARED status gave, which a
  building requires) within 20 s, only as the robot starts through, and the tool never
  does it for the model: a model that takes more than a few seconds a turn should leave crossings to your robot's own
  motion software, which asks, reads back and moves, while the model chooses where to go. A clearance not read back
  lapses on its own and the next robot goes; one read back and not followed by `passed` within the passage timeout
  closes the crossing for a person to clear. `ask` takes your own `passage_id`, so asking again after no answer is the
  same trip. Without
  `--acts` it never acts, so it's safe for a person's assistant. It speaks MCP 2024-11-05 to 2025-11-25 with the
  handshake, and 2026-07-28 without one; each answer carries the robot API's JSON as `structuredContent` beside the
  text (from 2025-06-18 on).
- `gateway/`: the kit's old name, kept so commands written for it (`python3 -m gateway.conformance`) still work.

## Version and license

The version is in `plynth/__init__.py` and every conformance report; what changed is in `CHANGELOG.md`. The kit is
open source under the Apache License 2.0 (`LICENSE`), copyright 2026 PLYNTH 3D Inc.: copy it into your own code as you
need. The guide, the API reference and
the signing walkthrough are at the sandbox's `/docs`.
