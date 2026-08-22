"""Nox sessions for linting, formatting, and testing."""

import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

import nox

nox.options.default_venv_backend = "uv"
nox.options.reuse_venv = "yes"
nox.options.sessions = ["ruff", "pylint", "tests", "docker"]

IMAGE = "controlmyspa-porssari:test"
SMOKE_CONTAINER = "controlmyspa-porssari-smoke"
SMOKE_PORT = 18080
SMOKE_TIMEOUT_SECONDS = 90
HTTP_OK = 200


def _project_deps() -> list[str]:
    """Read project dependencies from pyproject.toml."""
    data = tomllib.loads(Path("pyproject.toml").read_text())
    return data["project"]["dependencies"]


def _dev_dep(name: str) -> str:
    """Read a pinned dev dependency spec from pyproject.toml."""
    data = tomllib.loads(Path("pyproject.toml").read_text())
    for dep in data["dependency-groups"]["dev"]:
        if dep.startswith(name):
            return dep
    return name


@nox.session
def ruff(session: nox.Session) -> None:
    """Run ruff linter and formatter checks."""
    session.install(_dev_dep("ruff"))
    session.run("ruff", "check", ".")
    session.run("ruff", "format", "--check", ".")


@nox.session
def pylint(session: nox.Session) -> None:
    """Run pylint on the application modules."""
    session.install("pylint", *_project_deps())
    session.run("pylint", "app", "pricing", "scheduling", "storage", "thermal")


@nox.session
def tests(session: nox.Session) -> None:
    """Run the test suite."""
    session.install("pytest", "pytest-cov", *_project_deps())
    session.run(
        "pytest",
        "--cov=app",
        "--cov=pricing",
        "--cov=scheduling",
        "--cov=storage",
        "--cov=thermal",
        "--cov-report=term",
        "--cov-report=xml:coverage.xml",
    )


def _wait_for_http(url: str, timeout: float) -> None:
    """Poll until `url` answers 200, or give up and say what went wrong."""
    deadline = time.monotonic() + timeout
    last_error: object = "no attempt made"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310
                if response.status == HTTP_OK:
                    return
                last_error = f"HTTP {response.status}"
        except (urllib.error.URLError, OSError) as exc:
            last_error = exc
        time.sleep(1)
    msg = f"{url} never became ready: {last_error}"
    raise TimeoutError(msg)


@nox.session(venv_backend="none")
def docker(session: nox.Session) -> None:
    """Build the image and prove the app starts and serves inside it.

    A build that succeeds proves nothing: the image that took production down
    built fine and died on import. So the container is actually run, and the
    status page -- the same path the readiness probe hits -- must answer.
    """
    session.run("docker", "build", "-t", IMAGE, ".", external=True)
    # Cheap first: a missing module fails here with a readable traceback.
    session.run(
        "docker", "run", "--rm", IMAGE, "python", "-c", "import app", external=True
    )

    session.run(
        "docker", "rm", "-f", SMOKE_CONTAINER, external=True, success_codes=[0, 1]
    )
    session.run(
        "docker",
        "run",
        "--detach",
        "--name",
        SMOKE_CONTAINER,
        "--publish",
        f"{SMOKE_PORT}:8080",
        # No spa credentials: control() fails in a background thread, which is
        # what happens in production when the Balboa API is down, and must not
        # stop the web server coming up.
        "--env",
        "TEMP_HIGH=37",
        "--env",
        "TEMP_LOW=10",
        "--env",
        "SQLITE_PATH=/nonexistent/temperatures.db",
        IMAGE,
        external=True,
    )
    try:
        _wait_for_http(f"http://127.0.0.1:{SMOKE_PORT}/", SMOKE_TIMEOUT_SECONDS)
        session.log("the image serves the status page")
    except TimeoutError:
        session.run(
            "docker", "logs", SMOKE_CONTAINER, external=True, success_codes=[0, 1]
        )
        raise
    finally:
        session.run(
            "docker", "rm", "-f", SMOKE_CONTAINER, external=True, success_codes=[0, 1]
        )
