"""The image must contain every module the app imports.

app.py grew from one file into five, but the Dockerfile still copied only
app.py. The container died at startup with ModuleNotFoundError, the rollout
never became ready, and the deployment sat there timing out. `nox -s docker`
builds the image but does not run it, so nothing caught it.

This keeps the Dockerfile and the imports in step.
"""

import ast
import pathlib

ROOT = pathlib.Path(__file__).parent
ENTRYPOINT = "app"


def _local_modules() -> set[str]:
    """Return module names in the repository root, excluding tests and tooling."""
    return {
        path.stem
        for path in ROOT.glob("*.py")
        if not path.stem.startswith("test_") and path.stem != "noxfile"
    }


def _imports_of(module: str) -> set[str]:
    """Return the top-level names imported by a module."""
    tree = ast.parse((ROOT / f"{module}.py").read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def _modules_reachable_from(entry: str) -> set[str]:
    """Return every local module the entrypoint needs, directly or indirectly."""
    local = _local_modules()
    seen: set[str] = set()
    queue = [entry]
    while queue:
        module = queue.pop()
        if module in seen:
            continue
        seen.add(module)
        queue.extend(_imports_of(module) & local - seen)
    return seen


def _files_copied_into_the_image() -> set[str]:
    """Return the names the Dockerfile copies from the build context."""
    copied: set[str] = set()
    for line in (ROOT / "Dockerfile").read_text().splitlines():
        stripped = line.strip()
        if not stripped.startswith("COPY ") or "--from" in stripped:
            continue
        for pattern in stripped.split()[1:-1]:  # drop COPY and the destination
            copied |= {match.name for match in ROOT.glob(pattern)}
    return copied


def test_the_image_contains_every_module_the_app_imports():
    """A module the app imports but the image lacks is a crash on startup."""
    needed = {f"{module}.py" for module in _modules_reachable_from(ENTRYPOINT)}
    missing = needed - _files_copied_into_the_image()

    assert not missing, f"Dockerfile does not COPY: {sorted(missing)}"


def test_the_templates_are_in_the_image():
    """The status page renders a template; without it every request 500s."""
    assert "templates" in _files_copied_into_the_image()
