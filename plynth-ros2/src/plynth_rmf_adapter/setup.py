from setuptools import setup

package = "plynth_rmf_adapter"
setup(
    name=package,
    version="0.1.0",
    packages=[package],
    data_files=[("share/ament_index/resource_index/packages", [f"resource/{package}"]),
                (f"share/{package}", ["package.xml", "README.md", "LICENSE"]),
                (f"share/{package}/config", ["config/sandbox.yaml"])],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="Plynth",
    maintainer_email="support@plynth.build",
    description="An Open-RMF lift and door adapter for Plynth's robot API",
    license="Apache-2.0",
    entry_points={"console_scripts": [f"adapter = {package}.adapter:main"]},
)
