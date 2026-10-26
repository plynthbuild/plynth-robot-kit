# Plynth's ROS 2 packages: the ride example, the Open-RMF lift and door adapter, and the robot node, built in /ws.
# ROS 2 Jazzy by default; Humble with --build-arg ROS_DISTRO=humble (what Unitree recommends). test.sh copies the robot
# kit's wheel here, builds this, and runs all three against a local sandbox.
ARG ROS_DISTRO=jazzy
FROM ros:${ROS_DISTRO}-ros-base
ARG ROS_DISTRO
# pip into the system Python that ROS runs on (Ubuntu 24.04 asks for this; 22.04's pip ignores it)
ENV PIP_BREAK_SYSTEM_PACKAGES=1
# socat is for test.sh: inside the container it brings the host's sandbox to 127.0.0.1 (see README.md); twist_mux is
# for the robot node's hold, as a robot would run it (src/plynth_node/config/twist_mux.yaml).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ros-${ROS_DISTRO}-rmf-lift-msgs ros-${ROS_DISTRO}-rmf-door-msgs ros-${ROS_DISTRO}-twist-mux python3-pip socat \
    && rm -rf /var/lib/apt/lists/*
# The kit's client (the plynth package) into the system Python that ROS runs on.
COPY plynth_robot_kit-*.whl /tmp/
RUN pip3 install --no-cache-dir /tmp/plynth_robot_kit-*.whl && rm /tmp/plynth_robot_kit-*.whl
COPY src /ws/src
RUN cd /ws && . /opt/ros/${ROS_DISTRO}/setup.sh && colcon build && rm -rf build log
# Every command, docker run's or docker exec /ros_entrypoint.sh's, starts with the workspace sourced.
RUN sed -i 's|/opt/ros/$ROS_DISTRO/setup.bash|/ws/install/setup.bash|' /ros_entrypoint.sh
