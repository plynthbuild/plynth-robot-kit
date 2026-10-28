"""The kit's old name: plynth.ask is the same code, kept so commands written for gateway.ask still work."""
from plynth.ask import *  # noqa: F401,F403
if __name__ == "__main__":
    import sys
    from plynth.ask import main
    sys.exit(main())
