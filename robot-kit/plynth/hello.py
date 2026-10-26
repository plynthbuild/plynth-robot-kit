"""Check a robot's key in one step, before anything else: does the gateway accept it, whose robot is it, is the clock
right, and which doors and elevators may it use.

    python3 -m plynth.hello --env ../plynth-sandbox.env  # from inside robot-kit: the file that comes with each key
    GATEWAY_URL=... GATEWAY_ROBOT_ID=... GATEWAY_ROBOT_SECRET=... python3 -m plynth.hello  # or the environment

For a building's gateway, add its certificate's fingerprint (--pin sha256:..., or GATEWAY_TLS_PIN) or its certificate
authority (--cafile). Exits 0 when the key works, 1 when the gateway refuses it or can't be reached, 2 when something
is missing.
"""
import argparse
import os
import sys
import time
import urllib.error

from plynth.client import GatewayError, RobotClient

WHY = {
    "signature mismatch": "The gateway didn't accept the signature. This tool signs for you, so check the robot ID and the"
                          " secret: the whole secret, no spaces, for this robot ID (a secret replaced on your company's"
                          " page or by the building stops the old one at once).",
    "timestamp": "Your computer's clock is {skew} s from the gateway's, past the 30 s allowed: turn on NTP (automatic"
                 " time), then try again.",
}


def env_file(path):
    """KEY=VALUE lines, as plynth-sandbox.env holds them; blank lines and # comments skipped. For the kit's hello, grade
    and conformance check alike: a file that isn't there ends the run with one line saying where it was looked for (the
    sandbox pressure test, 2026-10-25: a traceback, FileNotFoundError, as a first try's answer)."""
    try:
        f = open(path)
    except FileNotFoundError:
        print(f"No file at {os.path.abspath(path)}. Save plynth-sandbox.env there, from your key's page (or Get Secret on"
              " your company's page), or pass --env with where it is.", file=sys.stderr)
        raise SystemExit(2) from None
    out = {}
    with f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                out[key.strip().removeprefix("export ").strip()] = value.strip().strip('"').strip("'")
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python3 -m plynth.hello", description=__doc__.splitlines()[0])
    parser.add_argument("--env", help="a file of GATEWAY_URL, GATEWAY_ROBOT_ID and GATEWAY_ROBOT_SECRET (plynth-sandbox.env)")
    parser.add_argument("--url", help="the gateway (or GATEWAY_URL)")
    parser.add_argument("--robot", help="the robot ID (or GATEWAY_ROBOT_ID)")
    parser.add_argument("--pin", help="a building gateway's certificate fingerprint, sha256:<hex> (or GATEWAY_TLS_PIN)")
    parser.add_argument("--cafile", help="a building's certificate authority, instead of a pin")
    a = parser.parse_args(argv)
    env = {**os.environ, **(env_file(a.env) if a.env else {})}
    url, robot_id, secret = a.url or env.get("GATEWAY_URL"), a.robot or env.get("GATEWAY_ROBOT_ID"), env.get("GATEWAY_ROBOT_SECRET")
    missing = [n for n, v in (("GATEWAY_URL", url), ("GATEWAY_ROBOT_ID", robot_id), ("GATEWAY_ROBOT_SECRET", secret)) if not v]
    if missing:
        print(f"{a.env} doesn't set {', '.join(missing)}: pass --env the plynth-sandbox.env your key came with."
              if a.env else f"Set {', '.join(missing)}, or pass --env with plynth-sandbox.env, the file your key came"
              " with.", file=sys.stderr)
        return 2
    try:
        robot = RobotClient(url, robot_id, secret, pin=a.pin or env.get("GATEWAY_TLS_PIN"), cafile=a.cafile)
    except ValueError as e:  # plain HTTP off loopback
        print(f"{e}.", file=sys.stderr)
        return 2
    asked = time.time()
    try:
        me = robot.me()
        places = robot.places().get("places", [])
    except GatewayError as e:
        error = e.body.get("error", "") if isinstance(e.body, dict) else str(e)
        if e.status == 401 and error.startswith("timestamp") and isinstance(e.body, dict) and e.body.get("time"):
            print(WHY["timestamp"].format(skew=round(e.body["time"] - asked)), file=sys.stderr)
        elif e.status == 401:
            print(WHY["signature mismatch"], file=sys.stderr)
        else:
            print(f"The gateway answered {e.status}: {error}", file=sys.stderr)
        if isinstance(e.body, dict) and e.body.get("request_id"):
            print(f"(Request {e.body['request_id']}, if you write to Plynth about it.)", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError) as e:
        print(f"Couldn't reach {url}: {getattr(e, 'reason', e)}. Check the address"
              + ("; for a building's gateway, its certificate (--pin or --cafile)." if url.startswith("https") else "."),
              file=sys.stderr)
        return 1
    skew = me["time"] - (asked + time.time()) / 2
    company = (me.get("company") or {}).get("name") or "your company"
    print(f"Your key works: {me['id']} ({company}) at {url}.")
    print(f"Clock: within {abs(skew):.1f} s of the gateway's" + ("; turn on NTP before it drifts past 30 s." if abs(skew) > 5 else "."))
    if me.get("active") is False:
        print("This robot is suspended here: its requests are refused until the building resumes it.")
    if places:
        print("It may use: " + "; ".join(f"{p['id']} ({p['name']}, floor{'s' if len(p.get('floors') or []) > 1 else ''} "
                                         f"{', '.join(p.get('floors') or [])})" for p in places) + ".")
    print(f"Next: python3 -m plynth.conformance{f' --env {a.env}' if a.env else ''}  (it rides the elevator and goes"
          " through the door, and says what passed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
