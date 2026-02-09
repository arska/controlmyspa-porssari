"""Nox sessions for linting, formatting, and testing."""

import tomllib
from pathlib import Path

import nox

nox.options.default_venv_backend = "uv"
nox.options.reuse_venv = "yes"
nox.options.sessions = ["ruff", "pylint", "docker"]


def _project_deps() -> list[str]:
    """Read project dependencies from pyproject.toml."""
    data = tomllib.loads(Path("pyproject.toml").read_text())
    return data["project"]["dependencies"]


@nox.session
def ruff(session: nox.Session) -> None:
    """Run ruff linter and formatter checks."""
    session.install("ruff")
    session.run("ruff", "check", ".")
    session.run("ruff", "format", "--check", ".")


@nox.session
def pylint(session: nox.Session) -> None:
    """Run pylint on app module."""
    session.install("pylint", *_project_deps())
    session.run("pylint", "app")


@nox.session
def tests(session: nox.Session) -> None:
    """Run the test suite."""
    session.install("pytest", "pytest-cov", *_project_deps())
    session.run(
        "pytest", "--cov=app", "--cov-report=term", "--cov-report=xml:coverage.xml"
    )


@nox.session(venv_backend="none")
def docker(session: nox.Session) -> None:
    """Build and smoke-test the Docker image."""
    session.run(
        "docker", "build", "-t", "controlmyspa-porssari:test", ".", external=True
    )
    session.run(
        "docker",
        "run",
        "--rm",
        "controlmyspa-porssari:test",
        "python",
        "-c",
        "import app",
        external=True,
    )
