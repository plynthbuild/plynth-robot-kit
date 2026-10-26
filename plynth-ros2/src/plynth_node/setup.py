from setuptools import setup

package = "plynth_node"
setup(
    name=package,
    version="0.1.0",
    packages=[package],
    data_files=[("share/ament_index/resource_index/packages", [f"resource/{package}"]),
                (f"share/{package}", ["package.xml", "LICENSE"]), (f"share/{package}/config", ["config/twist_mux.yaml"])],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="Plynth",
    maintainer_email="support@plynth.build",
    description="A ROS 2 robot connected to a building through Plynth: its position, the building's holds, keep-outs",
    license="Apache-2.0",
    entry_points={"console_scripts": [f"plynth_node = {package}.node:main"]},
)
