"""Nox configuration for linting."""

import nox

nox.options.default_venv_backend = "uv"
nox.options.reuse_venv = "always"


@nox.session
def lint(session: nox.Session) -> None:
    """Run ruff linter and formatter checks."""
    session.install("ruff")
    session.run("ruff", "check", ".")
    session.run("ruff", "format", "--check", ".")
