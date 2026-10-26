"""The kit's old name: plynth.conformance is the same code, kept so commands written for gateway.conformance still work."""
from plynth.conformance import *  # noqa: F401,F403
if __name__ == "__main__":
    import sys
    from plynth.conformance import main
    sys.exit(main())
