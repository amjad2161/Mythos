import os

from setuptools import find_packages, setup

_HERE = os.path.abspath(os.path.dirname(__file__))


def _read(name: str) -> str:
    with open(os.path.join(_HERE, name), encoding="utf-8") as fh:
        return fh.read()


# Single source of truth for the version: mythos/__init__.py
_version = "0.0.0"
for _line in _read(os.path.join("mythos", "__init__.py")).splitlines():
    if _line.startswith("__version__"):
        _version = _line.split("=", 1)[1].strip().strip('"').strip("'")
        break

setup(
    name="mythos",
    version=_version,
    description="Mythos – Full Autonomous AI System",
    long_description=_read("README.md"),
    long_description_content_type="text/markdown",
    author="Mythos",
    license="MIT",
    url="https://github.com/amjad2161/Mythos",
    packages=find_packages(exclude=["tests*"]),
    py_modules=["main"],   # the `mythos` console script imports the top-level main module
    python_requires=">=3.9",
    install_requires=[
        "anthropic>=0.20.0",
    ],
    extras_require={
        "openai": ["openai>=1.0.0"],
        "dev": ["pytest>=7.0", "pytest-cov"],
    },
    entry_points={
        "console_scripts": [
            "mythos=main:main",
        ],
    },
    classifiers=[
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3 :: Only",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
)
