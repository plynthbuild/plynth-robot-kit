"""The kit's old name: plynth.grade is the same code, kept so commands written for gateway.grade still work."""
from plynth.grade import *  # noqa: F401,F403
if __name__ == "__main__":
    import sys
    from plynth.grade import main
    sys.exit(main())
