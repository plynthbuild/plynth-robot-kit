"""Grade your robot's own software on the sandbox: run it through the sandbox's scenario places for a
while, then ask what the sandbox saw it do. Each check passed, failed or not seen yet, with the trips behind it and
how to show it; every check passed is a certificate, sandbox-verified, that anyone can look up by its link.

    python3 -m plynth.grade --env ../plynth-sandbox.env            # from inside robot-kit: the last hour
    python3 -m plynth.grade --env ../plynth-sandbox.env --minutes 30

It grades a company's own robots: a test key from /docs isn't graded (403). The conformance kit's own trips don't
count: they carry X-Test-Run. Exits 0 with a certificate, 1 without one, 2 when something is missing.
"""
import argparse
import os
import sys
import time
import urllib.error

from plynth.client import GatewayError, RobotClient
from plynth.hello import env_file

MARK = {"passed": "PASS", "failed": "FAIL", "not seen": " -- "}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python3 -m plynth.grade", description=__doc__.splitlines()[0])
    parser.add_argument("--env", help="a file of GATEWAY_URL, GATEWAY_ROBOT_ID and GATEWAY_ROBOT_SECRET (plynth-sandbox.env)")
    parser.add_argument("--url", help="the sandbox (or GATEWAY_URL)")
    parser.add_argument("--robot", help="the robot ID (or GATEWAY_ROBOT_ID)")
    parser.add_argument("--minutes", type=int, default=60, help="how far back to look, 1 to 1440 (default 60)")
    a = parser.parse_args(argv)
    if not 1 <= a.minutes <= 1440:
        parser.error("--minutes is 1 to 1440")
    env = {**os.environ, **(env_file(a.env) if a.env else {})}
    url, robot_id, secret = a.url or env.get("GATEWAY_URL"), a.robot or env.get("GATEWAY_ROBOT_ID"), env.get("GATEWAY_ROBOT_SECRET")
    missing = [n for n, v in (("GATEWAY_URL", url), ("GATEWAY_ROBOT_ID", robot_id), ("GATEWAY_ROBOT_SECRET", secret)) if not v]
    if missing:
        print(f"Set {', '.join(missing)} (or pass --env with the file from your company's page).", file=sys.stderr)
        return 2
    try:
        g = RobotClient(url, robot_id, secret).grade(time.time() - a.minutes * 60)
    except ValueError as e:  # plain HTTP off loopback
        print(f"{e}.", file=sys.stderr)
        return 2
    except GatewayError as e:
        error = e.body.get("error", "") if isinstance(e.body, dict) else str(e)
        print(f"The sandbox answered {e.status}: {error}" + (" (grading is the sandbox's; a building has none)"
                                                             if e.status == 404 else ""), file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError) as e:
        print(f"Couldn't reach {url}: {getattr(e, 'reason', e)}.", file=sys.stderr)
        return 1
    print(f"Graded: {robot_id}'s own software at {url}, the last {a.minutes} minutes")
    for c in g["checks"]:
        print(f"  {MARK[c['result']]}  {c['title']}")
        for line in c["evidence"][:3]:
            print(f"        {line}")
        if c["result"] != "passed":
            print(f"        to show it: {c['how']}")
    print("Not graded yet: " + "; ".join(g["not_graded"]) + ".")
    if g["certificate"]:
        cert = g["certificate"]
        print(f"Sandbox-verified. Certificate {cert['id']}: {url.rstrip('/')}/docs/certificates/{cert['id']}")
        return 0
    print("Not sandbox-verified yet: fix what failed, show what wasn't seen, then grade again.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
