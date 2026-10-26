# Plynth robot kit

The robot side of Plynth: the code a robot runs to go through a building's doors and ride its elevators through
Plynth's robot API, while the building owner's rules decide. Apache-2.0.

| Folder | What it is |
|---|---|
| [`robot-kit/`](robot-kit/README.md) | The reference client, the conformance check and a key check, in standard-library Python 3.9 or later. Nothing to install. |
| [`plynth-ros2/`](plynth-ros2/README.md) | Three ROS 2 packages (Jazzy and Humble): `plynth_node`, the robot node, with holds on `/plynth/stop` for a twist_mux lock; `plynth_ros_example`, a door then an elevator ride from one node; `plynth_rmf_adapter`, an Open-RMF lift and door adapter. |

## Try it

The hosted sandbox is a simulated building with doors, a badge door and an elevator bank, and failures on demand. A
test key takes one click, with no sign-up: [sandbox.plynth.build/docs](https://sandbox.plynth.build/docs). Then follow
[`robot-kit/README.md`](robot-kit/README.md), or [`plynth-ros2/README.md`](plynth-ros2/README.md) for ROS 2.

## What's open and what isn't

This repository is the robot side, and it's open source. The gateway that runs in each building, and the owner's app,
are Plynth's service. These folders are the same files the sandbox serves as `robot-kit.zip` and `plynth-ros2.zip`;
their checksums are at [sandbox.plynth.build/docs/kit/SHA256SUMS](https://sandbox.plynth.build/docs/kit/SHA256SUMS).

Something broken or missing? Open an issue, or write to studio@plynth.build.

Copyright 2026 PLYNTH 3D Inc. Licensed under the [Apache License, Version 2.0](LICENSE).
