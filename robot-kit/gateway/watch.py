"""The kit's old name: plynth.watch is the same code, kept so commands written for gateway.watch still work."""
from plynth.watch import *  # noqa: F401,F403
if __name__ == "__main__":
    import sys
    from plynth.watch import main
    sys.exit(main())
