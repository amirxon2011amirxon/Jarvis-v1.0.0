"""Jarvis v1.0.0 packaging.

Install with::

    pip install .

The console entry point is ``jarvis`` (implemented by the existing
``web.launch.main`` startup path).  Runtime dependencies are read from
``requirements.txt`` so the development and installed dependency lists cannot
drift apart.
"""
from __future__ import annotations

from pathlib import Path

from setuptools import find_packages, setup

ROOT = Path(__file__).resolve().parent
README = ROOT / "readme.md"
if not README.is_file():
    README = ROOT / "README.md"


def _runtime_requirements() -> list[str]:
    lines: list[str] = []
    path = ROOT / "requirements.txt"
    if not path.is_file():
        return lines
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            lines.append(line)
    return lines


setup(
    name="jarvis",
    version="1.0.0",
    description=(
        "Jarvis — a local, voice-first AI assistant powered by Gemini Live."
    ),
    long_description=README.read_text(encoding="utf-8") if README.is_file() else "",
    long_description_content_type="text/markdown",
    license="MIT",
    license_files=["LICENSE", "THIRD_PARTY_LICENSES.md"],
    python_requires=">=3.11",
    packages=find_packages(
        include=["actions*", "core*", "web*", "config*", "Skills*", "frontend*"]
    ),
    py_modules=["main"],
    include_package_data=True,
    package_data={
        "core": ["prompt.txt"],
        "frontend": ["dist/*", "dist/assets/*"],
        "Skills": [
            "README.md",
            "Builtin/*/manifest.yaml",
            "Builtin/*/SKILL.md",
            "Builtin/*/references/*",
            "Builtin/*/resources/*",
        ],
    },
    install_requires=_runtime_requirements(),
    extras_require={
        "dev": [
            "pytest>=8,<10",
            "ruff>=0.9,<1",
            "playwright>=1.45,<2",
        ],
    },
    entry_points={
        "console_scripts": [
            "jarvis=web.launch:main",
        ],
    },
    classifiers=[
        "Development Status :: 5 - Production/Stable",
        "Environment :: Console",
        "Intended Audience :: End Users/Desktop",
        "License :: OSI Approved :: MIT License",
        "Operating System :: Microsoft :: Windows",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
        "Topic :: Multimedia :: Sound/Audio :: Speech",
        "Topic :: Scientific/Engineering :: Artificial Intelligence",
    ],
    zip_safe=False,
)
