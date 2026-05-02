from setuptools import setup, find_packages

setup(
    name="mythos",
    version="0.1.0",
    description="Mythos – Full Autonomous AI System",
    long_description=open("README.md", encoding="utf-8").read(),
    long_description_content_type="text/markdown",
    author="Mythos",
    packages=find_packages(exclude=["tests*"]),
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
        "License :: OSI Approved :: MIT License",
    ],
)
