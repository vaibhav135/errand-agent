"""Layer import rules: the enforcement behind "the LLM proposes, the workflow decides".

An import is the only way one module can call another, so checking imports proves
what code *can* call. Each layer may import only the layers listed in RULES (see
docs/ARCHITECTURE.md, "Layers and import rules"). `ast` is used instead of grep so
aliased, relative and function-local imports are caught, while strings and comments
that merely mention a module are ignored. Not caught: importlib.import_module() with
a computed string, which would be deliberate evasion rather than an accident.
"""

import ast
from pathlib import Path

import pytest

PACKAGE = "errand_agent"
SRC = Path(__file__).resolve().parents[2] / "src" / PACKAGE

RULES: dict[str, set[str]] = {
    "core": set(),
    "store": {"core"},
    "tracing": {"core"},
    # The provider contract (ToolEnv, models, errors). Everything talks to providers
    # through it; only `api` (app wiring) ever touches the concrete mock.
    "providers": {"core"},
    "mocks": {"core", "store", "providers"},
    "memory": {"core", "store", "tracing"},
    "workflow": {"core", "store", "tracing", "memory", "providers"},
    # proactive detects provider events and starts runs; it never needs the LLM, and
    # the agent never needs the monitor, so neither imports the other.
    "proactive": {"core", "store", "tracing", "memory", "providers", "workflow"},
    "agent": {"core", "store", "tracing", "memory", "providers", "workflow"},
    "api": {
        "core",
        "store",
        "tracing",
        "providers",
        "mocks",
        "memory",
        "workflow",
        "agent",
        "proactive",
    },
}


def _module_parts(path: Path, root: Path) -> list[str]:
    rel = path.relative_to(root.parent).with_suffix("")
    parts = list(rel.parts)
    return parts[:-1] if parts[-1] == "__init__" else parts


def imported_layers(path: Path, root: Path) -> set[str]:
    """Internal layers that the module at `path` imports (any form, any depth)."""
    parts = _module_parts(path, root)
    is_package = path.name == "__init__.py"
    found: set[str] = set()

    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        targets: list[str] = []
        if isinstance(node, ast.Import):
            targets = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative: resolve against this module's package
                package = parts if is_package else parts[:-1]
                base = package[: len(package) - (node.level - 1)]
                module = ".".join([*base, *(node.module.split(".") if node.module else [])])
            else:
                module = node.module or ""
            # `from errand_agent import agent` names the layer in the imported names.
            targets = (
                [f"{module}.{alias.name}" for alias in node.names]
                if module == PACKAGE
                else [module]
            )
        for target in targets:
            bits = target.split(".")
            if len(bits) >= 2 and bits[0] == PACKAGE:
                found.add(bits[1])
    return found


def violations(root: Path, rules: dict[str, set[str]] = RULES) -> list[str]:
    problems: list[str] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if len(rel.parts) == 1:
            continue  # package root (__init__.py) is not a layer
        layer = rel.parts[0]
        if layer not in rules:
            problems.append(f"{rel}: layer '{layer}' has no import rule; add it to RULES")
            continue
        for imported in sorted(imported_layers(path, root) - {layer} - rules[layer]):
            problems.append(f"{rel}: '{layer}' may not import '{imported}'")
    return problems


# --- the real codebase --------------------------------------------------------------


def test_codebase_respects_layer_rules() -> None:
    assert violations(SRC) == []


def test_every_layer_on_disk_has_a_rule() -> None:
    on_disk = {p.name for p in SRC.iterdir() if p.is_dir() and not p.name.startswith("__")}
    assert on_disk <= RULES.keys()


# --- the checker itself (synthetic package) ------------------------------------------


@pytest.fixture
def fake_pkg(tmp_path: Path) -> Path:
    root = tmp_path / PACKAGE
    for layer in ("workflow", "agent", "core"):
        (root / layer).mkdir(parents=True)
        (root / layer / "__init__.py").write_text("")
    (root / "__init__.py").write_text("")
    return root


RULES_FOR_FAKE = {"workflow": {"core"}, "agent": {"core", "workflow"}, "core": set()}


@pytest.mark.parametrize(
    "source",
    [
        "from errand_agent.agent import ask_model",
        "import errand_agent.agent",
        "import errand_agent.agent.prompts as p",
        "from errand_agent import agent",
        "from ..agent import ask_model",
        "from .. import agent",
        "def f():\n    from errand_agent.agent import ask_model\n    return ask_model",
    ],
    ids=["from", "import", "aliased", "from-package", "relative", "relative-package", "local"],
)
def test_checker_catches_forbidden_import_in_any_form(fake_pkg: Path, source: str) -> None:
    (fake_pkg / "workflow" / "booking.py").write_text(source + "\n")

    assert violations(fake_pkg, RULES_FOR_FAKE) == [
        "workflow/booking.py: 'workflow' may not import 'agent'"
    ]


@pytest.mark.parametrize(
    "source",
    [
        '"""Must not import errand_agent.agent: the workflow decides."""',
        "# from errand_agent.agent import ask_model",
        'NOTE = "errand_agent.agent"',
        "from errand_agent.core.clock import Clock",
        "from errand_agent.workflow import other",  # same layer is always fine
        "from . import sibling",
        "import json",
    ],
    ids=["docstring", "comment", "string", "allowed", "same-layer", "relative-same", "stdlib"],
)
def test_checker_allows_mentions_and_permitted_imports(fake_pkg: Path, source: str) -> None:
    (fake_pkg / "workflow" / "booking.py").write_text(source + "\n")

    assert violations(fake_pkg, RULES_FOR_FAKE) == []


def test_checker_flags_layer_without_a_rule(fake_pkg: Path) -> None:
    (fake_pkg / "utils").mkdir()
    (fake_pkg / "utils" / "helpers.py").write_text("")

    assert violations(fake_pkg, RULES_FOR_FAKE) == [
        "utils/helpers.py: layer 'utils' has no import rule; add it to RULES"
    ]
