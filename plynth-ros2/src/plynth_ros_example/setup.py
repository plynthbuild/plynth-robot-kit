from setuptools import setup

package = "plynth_ros_example"
setup(
    name=package,
    version="0.1.0",
    packages=[package],
    data_files=[("share/ament_index/resource_index/packages", [f"resource/{package}"]),
                (f"share/{package}", ["package.xml", "LICENSE"])],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="Plynth",
    maintainer_email="support@plynth.build",
    description="Walk through a door and ride an elevator through Plynth's robot API, from a ROS 2 node",
    license="Apache-2.0",
    entry_points={"console_scripts": [f"ride = {package}.ride:main"]},
)
