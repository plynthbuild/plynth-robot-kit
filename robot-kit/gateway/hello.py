"""The kit's old name: plynth.hello is the same code, kept so commands written for gateway.hello still work."""
from plynth.hello import *  # noqa: F401,F403
if __name__ == "__main__":
    import sys
    from plynth.hello import main
    sys.exit(main())
