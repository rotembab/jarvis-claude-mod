"""Static checks of the keyboard layer (DESIGN 5.5, 3.0, 6.4): what a module may import, what may never reach a log,
which modules do no I/O, that every stub is only a stub, that no dependency was added and the frozen files are
untouched.

P10, P43 (import boundary, in fresh interpreters and by syntax), P80, S23, S53, S81 (the log lint, written over a list
of module names so that later modules join it by adding a name), X47 (no I/O in the air detector), the stub rules of
6.0,
the dependency rule of 3.15 and the frozen-file gate of 6.4.
"""

from __future__ import annotations

import ast
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tokenize
import tomllib
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import jarvis_hands

PKG = "jarvis_hands"
SRC = Path(jarvis_hands.__file__).resolve().parent
HANDS = SRC.parent.parent  # plugin/hands
REPO = HANDS.parent.parent

# --------------------------------------------------------------------------- the module map of DESIGN 3.0

T0_MODULES = ["types", "limits", "tuning", "settings"]
#: module (relative to keyboard/) -> the track that owns it and replaces its stub (6.2)
STUB_OWNERS: dict[str, str] = {
    "layout": "T1",
    "hands": "T1",
    "press": "T1",
    "press_pinch": "T1",
    "press_air": "T1",
    "plane": "T1",
    "synth": "T1",
    "warmup": "T2",
    "ladder": "T2",
    "compose": "T2",
    "review": "T2",
    "session": "T2",
    "sink": "T2",
    "practice": "T2",
    "trace": "T2",
    "rig": "T2",
    "controller": "T5",
    "keytest": "T3",
    "keytrace": "T8",
    "keyreplay": "T8",
}
OVERLAY_STUB_OWNERS = {"keyboard_render": "T4", "text": "T4"}
KEYBOARD_MODULES = [*T0_MODULES, *STUB_OWNERS]
#: the modules the follow-on decoder track adds; they are allowed to exist, nothing else is
FOLLOW_ON = ("dec_",)

#: Every module that must import in a fresh interpreter, as labels relative to the package.
PROBED = [
    "keyboard",
    *(f"keyboard.{m}" for m in KEYBOARD_MODULES),
    "desktop.keys",
    "desktop.base",
    "desktop.fake",
    "overlay.base",
    "overlay.keyboard_render",
    "overlay.text",
    "protocol",
]


def path_of(label: str) -> Path:
    base = SRC.joinpath(*label.split("."))
    return base / "__init__.py" if base.is_dir() else base.with_suffix(".py")


# --------------------------------------------------------------------------- imports, by syntax


@dataclass(frozen=True)
class Imp:
    target: str  # an absolute module name
    lineno: int
    typing_only: bool  # inside `if TYPE_CHECKING:`
    lazy: bool  # inside a function


def module_of(path: Path) -> str:
    parts = list(path.resolve().relative_to(SRC.parent).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def imports_of(path: Path) -> list[Imp]:
    """Every import in the file, relative ones resolved to absolute names (a name that is a submodule counts as one)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    package = module_of(path) if path.name == "__init__.py" else module_of(path).rpartition(".")[0]
    found: list[Imp] = []

    def submodule_exists(parts: list[str]) -> bool:
        base = SRC.parent.joinpath(*parts)
        return base.with_suffix(".py").is_file() or (base / "__init__.py").is_file()

    def visit(node: ast.AST, typing_only: bool, lazy: bool) -> None:
        for child in ast.iter_child_nodes(node):
            child_typing, child_lazy = typing_only, lazy
            if isinstance(child, ast.If) and _is_type_checking(child.test):
                for sub in child.body:
                    visit_one(sub, True, lazy)
                for sub in child.orelse:
                    visit_one(sub, typing_only, lazy)
                continue
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                child_lazy = True
            visit_one(child, child_typing, child_lazy)

    def visit_one(node: ast.AST, typing_only: bool, lazy: bool) -> None:
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append(Imp(alias.name, node.lineno, typing_only, lazy))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")
                base = base[: len(base) - (node.level - 1)]
                module = base + (node.module.split(".") if node.module else [])
            else:
                module = node.module.split(".") if node.module else []
            if module:
                found.append(Imp(".".join(module), node.lineno, typing_only, lazy))
            for alias in node.names:
                if alias.name != "*" and submodule_exists([*module, alias.name]):
                    found.append(Imp(".".join([*module, alias.name]), node.lineno, typing_only, lazy))
        else:
            visit(node, typing_only, lazy)

    visit(tree, False, False)
    return found


def own_imports(path: Path, *, include_typing: bool = False) -> set[str]:
    """The `jarvis_hands.*` modules a file imports (the package itself excluded)."""
    return {
        i.target
        for i in imports_of(path)
        if (i.target == PKG or i.target.startswith(PKG + "."))
        and i.target != PKG
        and (include_typing or not i.typing_only)
    }


def third_party(path: Path) -> set[str]:
    out = set()
    for i in imports_of(path):
        top = i.target.split(".")[0]
        if top != PKG and top not in sys.stdlib_module_names:
            out.add(top)
    return out


def stdlib_imports(path: Path) -> set[str]:
    return {i.target.split(".")[0] for i in imports_of(path) if i.target.split(".")[0] in sys.stdlib_module_names}


# --------------------------------------------------------------------------- fresh interpreters (P10)

PROBE = """
import importlib, json, sys
importlib.import_module(sys.argv[1])
print(json.dumps({
    "jarvis": sorted(m for m in sys.modules if m == "jarvis_hands" or m.startswith("jarvis_hands.")),
    "third": sorted(n for n in ("numpy", "cv2", "mediapipe", "PIL", "jsonschema") if n in sys.modules),
}))
"""


def _probe(args: tuple[str, Path]) -> tuple[str, dict[str, Any] | str]:
    label, cwd = args
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", PROBE, f"{PKG}.{label}"],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=cwd,
        check=False,
    )
    if result.returncode != 0:
        return label, f"exit {result.returncode}: {result.stderr[-1500:]}"
    return label, json.loads(result.stdout.splitlines()[-1])


@pytest.fixture(scope="module")
def fresh(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict[str, Any] | str]:
    """What `sys.modules` holds after importing each module alone in a new interpreter (computed once, in parallel)."""
    cwd = tmp_path_factory.mktemp("probe")
    with ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 2)) as pool:
        return dict(pool.map(_probe, [(label, cwd) for label in PROBED]))


def loaded(fresh: dict[str, Any], label: str) -> set[str]:
    answer = fresh[label]
    assert isinstance(answer, dict), f"{label} does not import in a fresh interpreter: {answer}"
    return {m.removeprefix(PKG + ".") for m in answer["jarvis"] if m != PKG}


@pytest.mark.parametrize("label", PROBED)
def test_every_module_imports_in_a_fresh_interpreter(fresh: dict[str, Any], label: str) -> None:
    assert isinstance(fresh[label], dict), fresh[label]


#: reached by no keyboard module (3.0); `settings` (the HandsSettings module) is reached by the controller only,
#: through protocol
NEVER_FROM_KEYBOARD = {"runtime", "executor", "gestures", "actions", "control", "cli", "settings"}
CLI_FACING = {"keyboard.controller", "keyboard.keytest", "keyboard.keytrace", "keyboard.keyreplay"}
NO_HEAVY_IMPORT = [m for m in PROBED if m not in CLI_FACING]


@pytest.mark.parametrize(
    "label", [m for m in PROBED if m.startswith(("keyboard", "overlay.keyboard", "overlay.text", "desktop."))]
)
def test_a_keyboard_module_pulls_in_no_pointer_module(fresh: dict[str, Any], label: str) -> None:
    banned = NEVER_FROM_KEYBOARD - ({"settings"} if label == "keyboard.controller" else set())
    assert not (loaded(fresh, label) & banned), loaded(fresh, label) & banned


@pytest.mark.parametrize("label", ["protocol", "overlay.base"])
def test_protocol_and_overlay_base_reach_only_keyboard_types(fresh: dict[str, Any], label: str) -> None:
    reached = {m for m in loaded(fresh, label) if m.startswith("keyboard")}
    assert reached <= {"keyboard", "keyboard.types"}, reached
    assert not (loaded(fresh, label) & {"runtime", "executor", "gestures", "actions", "control", "cli"})


@pytest.mark.parametrize("label", NO_HEAVY_IMPORT)
def test_no_camera_or_tracker_library_is_pulled_in(fresh: dict[str, Any], label: str) -> None:
    answer = fresh[label]
    assert isinstance(answer, dict)
    assert not ({"cv2", "mediapipe"} & set(answer["third"])), answer["third"]


def test_the_package_init_imports_nothing_else(fresh: dict[str, Any]) -> None:
    """3.0: `keyboard/__init__` is a docstring with no re-exports: importing the package imports no module of it."""
    assert loaded(fresh, "keyboard") == {"keyboard"}
    tree = ast.parse(path_of("keyboard").read_text(encoding="utf-8"))
    assert len(tree.body) == 1 and isinstance(tree.body[0], ast.Expr)
    assert isinstance(tree.body[0].value, ast.Constant) and isinstance(tree.body[0].value.value, str)


TIGHT: dict[str, set[str]] = {
    # label -> the only `jarvis_hands` modules a fresh import of it may load (package inits included)
    "keyboard.types": {"keyboard", "keyboard.types"},
    "keyboard.limits": {"keyboard", "keyboard.limits"},
    "keyboard.tuning": {"keyboard", "keyboard.limits", "keyboard.tuning"},
    "keyboard.settings": {"keyboard", "keyboard.types", "keyboard.limits", "keyboard.settings"},
    "desktop.keys": {"desktop", "desktop.base", "desktop.keys", "geometry"},
}


@pytest.mark.parametrize("label", TIGHT)
def test_the_leaves_load_nothing_beyond_their_allowed_modules(fresh: dict[str, Any], label: str) -> None:
    assert loaded(fresh, label) <= TIGHT[label], loaded(fresh, label) - TIGHT[label]


def test_overlay_base_loads_only_its_own_package_geometry_and_keyboard_types(fresh: dict[str, Any]) -> None:
    assert loaded(fresh, "overlay.base") <= {"overlay", "overlay.base", "geometry", "keyboard", "keyboard.types"}


# ------------------------------------------------------------------------- the import rules, by syntax (P10, P43, P80)

#: label -> the only `jarvis_hands` modules the file itself may import (typing-only imports included)
ONLY_IMPORTS: dict[str, set[str]] = {
    "keyboard.types": set(),
    "keyboard.limits": set(),
    "keyboard.tuning": {"keyboard.limits"},
    "keyboard.settings": {"keyboard.types", "keyboard.limits"},
    "keyboard.press_air": {"keyboard.types", "keyboard.limits", "keyboard.tuning"},
    "keyboard.ladder": {"keyboard.types", "keyboard.limits", "keyboard.tuning"},
    "keyboard.compose": {"keyboard.types", "keyboard.limits", "desktop.keys"},
    # overlay.base is not in the design's list (3.0), but ReviewMachine.view returns the ComposeView defined there
    "keyboard.review": {"keyboard.types", "keyboard.limits", "keyboard.compose", "desktop.keys", "overlay.base"},
    "desktop.keys": set(),
    "desktop.base": {"geometry", "desktop.keys"},
    "overlay.base": {"geometry", "keyboard.types"},
    "protocol": {"settings", "keyboard.types", "desktop.base"},
}


@pytest.mark.parametrize("label", ONLY_IMPORTS)
def test_the_files_import_only_what_the_design_allows(label: str) -> None:
    got = {m.removeprefix(PKG + ".") for m in own_imports(path_of(label), include_typing=True)}
    # a submodule import brings its parent package with it; parents are not a dependency of their own
    got = {m for m in got if not any(other.startswith(m + ".") for other in got)}
    assert got <= ONLY_IMPORTS[label], got - ONLY_IMPORTS[label]


@pytest.mark.parametrize("label", ["keyboard.press_air", "keyboard.ladder"])
def test_the_air_hot_path_has_no_numpy_and_no_third_party(label: str) -> None:
    assert third_party(path_of(label)) == set()


def keyboard_files() -> list[Path]:
    return sorted((SRC / "keyboard").glob("*.py"))


def test_no_keyboard_module_imports_a_pointer_module() -> None:
    banned = {f"{PKG}.{m}" for m in NEVER_FROM_KEYBOARD}
    for path in keyboard_files():
        allowed = banned - ({f"{PKG}.settings"} if path.stem == "controller" else set())
        hit = {i.target for i in imports_of(path) if i.target in allowed}
        assert not hit, (path.name, hit)


def test_only_the_controller_imports_protocol_and_overlay_state() -> None:
    for path in keyboard_files():
        if path.stem == "controller":
            continue
        assert f"{PKG}.protocol" not in {i.target for i in imports_of(path)}, path.name
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
        assert "OverlayState" not in names, path.name


def test_the_keyboard_modules_import_downward_without_a_cycle() -> None:
    graph: dict[str, set[str]] = {}
    for path in keyboard_files():
        me = module_of(path)
        graph[me] = {
            i.target for i in imports_of(path) if i.target.startswith(f"{PKG}.keyboard.") and not i.typing_only
        }
    state: dict[str, int] = {}

    def walk(node: str, trail: tuple[str, ...]) -> None:
        if state.get(node) == 2:
            return
        assert state.get(node) != 1, f"import cycle: {' -> '.join((*trail, node))}"
        state[node] = 1
        for nxt in graph.get(node, ()):
            walk(nxt, (*trail, node))
        state[node] = 2

    for node in graph:
        walk(node, ())


def test_only_the_runtime_and_the_cli_import_the_cli_facing_modules_and_only_lazily() -> None:
    targets = {f"{PKG}.{m}" for m in CLI_FACING}
    for path in sorted(SRC.rglob("*.py")):
        for imp in imports_of(path):
            if imp.target in targets:
                assert path.name in ("runtime.py", "cli.py") and path.parent == SRC, (path, imp)
                assert imp.lazy, (path, imp)


def test_no_decoder_module_is_imported_by_the_review_path() -> None:
    """P80: compose, session and sink import none of the `dec_*` modules (true while they do not exist, and after)."""
    for stem in ("compose", "session", "sink", "review"):
        for imp in imports_of(path_of(f"keyboard.{stem}")):
            assert not imp.target.rpartition(".")[2].startswith(FOLLOW_ON), (stem, imp)


def test_the_files_of_the_keyboard_package_are_the_map() -> None:
    names = {p.stem for p in keyboard_files()} - {"__init__"}
    extra = {n for n in names if n not in KEYBOARD_MODULES and not n.startswith(FOLLOW_ON)}
    assert not extra, f"modules outside the map of DESIGN 3.0: {extra}"
    assert set(KEYBOARD_MODULES) <= names
    for stem in OVERLAY_STUB_OWNERS:
        assert (SRC / "overlay" / f"{stem}.py").is_file()
    assert (SRC / "desktop" / "keys.py").is_file()


# --------------------------------------------------------------------------- no new dependency (3.15)

BASE_DEPENDENCIES = {"mediapipe==0.10.33", "numpy>=2", "opencv-contrib-python>=5.0,<6"}
#: the one addition DESIGN 3.15 allows, and only track T4 makes it
PILLOW = "pillow>=12.3,<13"


def test_no_dependency_was_added() -> None:
    project = tomllib.loads((HANDS / "pyproject.toml").read_text(encoding="utf-8"))
    deps = set(project["project"]["dependencies"])
    assert deps in (BASE_DEPENDENCIES, BASE_DEPENDENCIES | {PILLOW}), deps
    assert {"pytest>=8.3", "jsonschema>=4.23"} <= set(project["dependency-groups"]["dev"])  # the dev group may grow


def git_show(repo: Path, base: str, path: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "show", f"{base}:{path}"], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(f"git show failed: {result.stderr.strip()}")
    return result.stdout


@pytest.mark.skipif(
    not os.environ.get("KB_FROZEN_BASE"), reason="set KB_FROZEN_BASE=<base commit> to check the version (C6, 3.15)"
)
def test_the_hands_version_was_not_bumped_by_this_change() -> None:
    """C6: the version is whatever the base commit says. Bound to the base so that a later release can bump it."""
    base = os.environ["KB_FROZEN_BASE"]
    then = tomllib.loads(git_show(REPO, base, "plugin/hands/pyproject.toml"))["project"]["version"]
    package = git_show(REPO, base, "plugin/hands/src/jarvis_hands/__init__.py")
    assert f'__version__ = "{then}"' in package
    now = tomllib.loads((HANDS / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    assert now == then and jarvis_hands.__version__ == then


def test_the_keyboard_code_imports_only_the_standard_library_numpy_and_pillow() -> None:
    drawing = {SRC / "overlay" / "keyboard_render.py", SRC / "overlay" / "text.py"}
    for path in [*keyboard_files(), SRC / "desktop" / "keys.py", *drawing]:
        allowed = {"numpy", "PIL"} if path in drawing else {"numpy"}
        extra = third_party(path) - allowed
        # keytrace and keyreplay open the camera and the tracker through the package's own wrappers, not directly
        assert not extra, (path.name, extra)


# --------------------------------------------------------------------------- no I/O (X47, 5.11 "static no-I/O list")

#: pure modules: no file, clock, thread, process, network or environment (the sink talks only to the desktop it gets)
PURE = [
    "keyboard.types",
    "keyboard.limits",
    "keyboard.settings",
    "keyboard.layout",
    "keyboard.hands",
    "keyboard.press",
    "keyboard.press_pinch",
    "keyboard.press_air",
    "keyboard.plane",
    "keyboard.synth",
    "keyboard.warmup",
    "keyboard.ladder",
    "keyboard.compose",
    "keyboard.review",
    "keyboard.session",
    "keyboard.sink",
    "keyboard.rig",
    "desktop.keys",
]
FORBIDDEN_IMPORTS = {
    "os", "sys", "io", "pathlib", "json", "subprocess", "socket", "threading", "time", "tempfile", "shutil",
    "urllib", "http", "ctypes", "sqlite3", "pickle", "multiprocessing", "asyncio", "select", "signal",
    "secrets", "uuid", "datetime", "glob", "platform", "webbrowser", "queue", "concurrent",
}  # fmt: skip
FORBIDDEN_CALLS = {"open", "print", "input", "exec", "eval", "compile", "__import__", "breakpoint"}


@pytest.mark.parametrize("label", PURE)
def test_pure_modules_do_no_io(label: str) -> None:
    path = path_of(label)
    assert not (stdlib_imports(path) & FORBIDDEN_IMPORTS), stdlib_imports(path) & FORBIDDEN_IMPORTS
    tree = ast.parse(path.read_text(encoding="utf-8"))
    calls = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert not (calls & FORBIDDEN_CALLS), calls & FORBIDDEN_CALLS


def test_the_tuning_loader_only_reads() -> None:
    path = path_of("keyboard.tuning")
    assert stdlib_imports(path) <= {
        "__future__", "dataclasses", "json", "logging", "math", "pathlib", "typing", "collections", "re", "functools",
    }  # fmt: skip
    tree = ast.parse(path.read_text(encoding="utf-8"))
    writers = {
        "write_text",
        "write_bytes",
        "mkdir",
        "unlink",
        "rename",
        "replace",
        "touch",
        "rmdir",
        "open",
        "dump",
        "write",
    }
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not (attrs & writers), attrs & writers
    calls = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert not (calls & FORBIDDEN_CALLS), calls & FORBIDDEN_CALLS


def test_nothing_in_the_package_prints_except_the_three_console_tools() -> None:
    """stdout is the event stream and stderr a log: a stray print of typed text is a leak channel (SR13)."""
    console = {"keytest", "keytrace", "keyreplay"}
    for path in [
        *keyboard_files(),
        SRC / "desktop" / "keys.py",
        SRC / "overlay" / "keyboard_render.py",
        SRC / "overlay" / "text.py",
    ]:
        if path.stem in console:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        prints = [
            n.lineno
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "print"
        ]
        assert not prints, (path.name, prints)


# --------------------------------------------------------------------------- the log lint (S23, S53, S81, X47)

#: Written over a list of module paths (relative to the package), not a fixed set: the decoder track's files join by
#: name (H9).
LOG_LINT_MODULES: list[str] = [
    *(f"keyboard/{m}.py" for m in KEYBOARD_MODULES),
    "desktop/windows.py",
    "desktop/keys.py",
    "overlay/keyboard_render.py",
    "overlay/text.py",
]
#: modules of this list that also forbid formatting an exception object in a log call (F4): all of keyboard/
EXC_LINT_PREFIX = "keyboard/"

#: names that stand for typed content, a box, a plan, a tap record, a depth trace, a target or hit key, or a decoder
#: request (S23, S53, S81, X47)
CONTENT_NAMES = {
    "char", "text", "echo", "legend", "stroke", "word", "buffer", "en", "he",
    "compose", "remaining", "plan", "step", "touch", "touches", "summary",
    "head", "cands", "req", "result", "protect",
    "trace", "depth", "depths", "lift", "target", "hit", "hits", "box", "ch",
}  # fmt: skip
EXC_NAMES = {"exc", "err", "e", "error", "exception"}
#: attributes that are counts or enums, safe to log whatever object carries them
SAFE_ATTRS = {
    "kind",
    "sent",
    "of",
    "outcome",
    "reason",
    "total",
    "index",
    "run",
    "first",
    "state",
    "version",
    "seq",
    "length",
}
LOGGERS = {"log", "logger", "logging", "_log", "LOG", "LOGGER"}
LOG_METHODS = {"debug", "info", "warning", "warn", "error", "critical", "exception", "fatal", "log"}
#: calls whose result is a number or a type name, whatever they are given
OPAQUE_CALLS = {"len", "type", "int", "bool", "id"}


def is_log_call(node: ast.AST) -> bool:
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in LOG_METHODS):
        return False
    owner = node.func.value
    return (isinstance(owner, ast.Name) and owner.id in LOGGERS) or (
        isinstance(owner, ast.Attribute) and owner.attr in LOGGERS
    )


def interpolated_names(expr: ast.AST) -> Iterator[tuple[str, int]]:
    """The identifiers an expression reads, left out where only a count or a type name is taken from them."""
    if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name) and expr.func.id in OPAQUE_CALLS:
        return
    if isinstance(expr, ast.Attribute):
        if expr.attr in SAFE_ATTRS:
            return
        yield expr.attr, expr.lineno
        yield from interpolated_names(expr.value)
        return
    if isinstance(expr, ast.Name):
        yield expr.id, expr.lineno
        return
    for child in ast.iter_child_nodes(expr):
        yield from interpolated_names(child)


def log_format(call: ast.Call) -> str | None:
    """The format text of a log call (its first argument when that is a string constant)."""
    first = call.args[0] if call.args else None
    return first.value if isinstance(first, ast.Constant) and isinstance(first.value, str) else None


def log_formats(source: str) -> set[str]:
    calls = (node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Call) and is_log_call(node))
    return {text for call in calls if (text := log_format(call)) is not None}


def log_violations(
    source: str,
    *,
    exceptions: bool,
    forbidden: set[str] = CONTENT_NAMES,
    allow: Mapping[str, frozenset[str]] | None = None,
) -> list[str]:
    """``allow`` maps a log call's format text to the names that call may interpolate whatever they are called."""
    tree = ast.parse(source)
    problems: list[str] = []
    banned = forbidden | (EXC_NAMES if exceptions else set())
    for node in ast.walk(tree):
        if is_log_call(node):
            assert isinstance(node, ast.Call)
            banned_here = banned - (allow or {}).get(log_format(node) or "", frozenset())
            if exceptions and isinstance(node.func, ast.Attribute) and node.func.attr == "exception":
                problems.append(f"{node.lineno}: log.exception writes the traceback text")
            for kw in node.keywords:
                if exceptions and kw.arg == "exc_info":
                    problems.append(f"{node.lineno}: exc_info= writes the traceback text")
            for arg in [*node.args, *(kw.value for kw in node.keywords if kw.arg != "exc_info")]:
                for name, line in interpolated_names(arg):
                    if name in banned_here:
                        problems.append(f"{line}: {name}")
        elif isinstance(node, ast.Raise) and node.exc is not None:
            # exception text is data too: no message of a keyboard exception interpolates content (SR13)
            if isinstance(node.exc, ast.Call):
                for arg in [*node.exc.args, *(kw.value for kw in node.exc.keywords)]:
                    for name, line in interpolated_names(arg):
                        if name in forbidden:
                            problems.append(f"{line}: raise with {name}")
    return problems


#: Log calls that exist on main, name a content word by accident and carry none: {module: {format text: the names that
#: call may interpolate}}. The call is found by its format text, so a reformat keeps the exemption, while any other
#: content name in the same call still counts. (The display list is monitor names; `result`
#: there is a list of `Display`.)
KNOWN_SAFE_LOG_CALLS: dict[str, dict[str, frozenset[str]]] = {
    "desktop/windows.py": {"displays: %s": frozenset({"result"})},
}


@pytest.mark.parametrize("module", LOG_LINT_MODULES)
def test_no_log_call_or_exception_message_interpolates_typed_content(module: str) -> None:
    path = SRC / module
    assert path.is_file(), module
    source = path.read_text(encoding="utf-8")
    allow = KNOWN_SAFE_LOG_CALLS.get(module, {})
    stale = set(allow) - log_formats(source)
    assert not stale, f"{module} has no log call with the format {sorted(stale)}: drop its exemption"
    problems = log_violations(source, exceptions=module.startswith(EXC_LINT_PREFIX), allow=allow)
    assert not problems, problems


def test_an_exempt_log_call_is_exempt_for_its_named_variable_only() -> None:
    allow = {"displays: %s": frozenset({"result"})}
    shown = 'log.info("displays: %s", "; ".join(_describe(d) for d in result) or "none")'
    assert log_violations(shown, exceptions=False) == ["1: result"]
    assert log_violations(shown, exceptions=False, allow=allow) == []
    # reformatted over several lines: still exempt for `result`
    assert log_violations('log.info(\n    "displays: %s",\n    result,\n)', exceptions=False, allow=allow) == []
    # any other content name in the same call, the same name under another format text, a log call of another kind
    assert log_violations('log.info("displays: %s", result, text)', exceptions=False, allow=allow) == ["1: text"]
    assert log_violations('log.info("monitors: %s", result)', exceptions=False, allow=allow) == ["1: result"]
    assert log_violations('log.info(f"displays: {result}")', exceptions=False, allow=allow) == ["1: result"]
    assert log_formats(shown) == {"displays: %s"} and log_formats("log.info(msg)") == set()


def test_the_lint_list_covers_every_keyboard_file() -> None:
    covered = {m for m in LOG_LINT_MODULES if m.startswith("keyboard/")}
    present = {
        f"keyboard/{p.name}" for p in keyboard_files() if p.stem != "__init__" and not p.stem.startswith(FOLLOW_ON)
    }
    assert present <= covered, present - covered


@pytest.mark.parametrize(
    "line",
    [
        'log.info("typed %s", text)',
        'log.info("typed %s", stroke.value)',
        'log.info(f"typed {char}")',
        'log.info(f"typed {stroke!r}")',
        'log.debug("x %s", self.buffer)',
        'log.warning("legend %s", key.en)',
        'log.warning("legend %s", key.he)',
        'logger.info("word " + word)',
        'log.info("%s", echo)',
        'log.info("%s", "".join(plan))',
        'log.info("%s", summary)',
        'log.info("%s", touch.u)',
        'log.info("%s", touches)',
        'log.info("%s", step.stroke)',
        'log.info("%s", req)',
        'log.info("%s", result.cands)',
        'log.info("%s", head)',
        'log.info("%s", protect)',
        'log.info("%s", compose.text())',
        'log.info("{}".format(text))',
        'log.info("%s", remaining)',
    ],
)
def test_the_content_lint_bites(line: str) -> None:
    assert log_violations(line, exceptions=False), line


#: The names DESIGN X47 and S81 list, as the lint reads them: a depth trace, a Touch, a target, a hit, the box, a
#: decoder request, its candidates and the protect set. A name missing here is a leak the lint cannot see.
X47_NAMES = ("trace", "depth", "depths", "lift", "target", "hit", "hits", "box", "ch")
S81_NAMES = ("head", "text", "cands", "touches", "touch", "req", "result", "protect")


@pytest.mark.parametrize("name", [*X47_NAMES, *S81_NAMES])
def test_the_content_lint_bites_every_name_of_x47_and_s81(name: str) -> None:
    for line in (
        f'log.info("%s", {name})',
        f'log.info(f"seen {{{name}}}")',
        f'log.warning("%s", obj.{name})',
        f'logger.debug("x" + str({name}))',
        f'log.info("%s", "".join({name}))',
    ):
        assert log_violations(line, exceptions=False), line
        assert log_violations(line, exceptions=True), line
    assert log_violations(f'log.info("%d", len({name}))', exceptions=True) == []  # a count of them is fine


@pytest.mark.parametrize(
    "line",
    [
        'log.info("typed %d characters", len(text))',
        'log.info("typed %d", len(self.buffer))',
        'log.info("closed %s", reason)',
        'log.info("n=%d", summary.sent)',
        'log.info("%s of %s", summary.sent, summary.of)',
        'log.info("step %d of %d", step.index, step.total)',
        'log.info("%s", summary.outcome)',
        'log.info("%s", stroke.kind)',
        'log.info("%s", type(exc).__name__)',
        'log.info("frames %d", frames)',
        "print(text)",  # not a log call (the print rule is its own test)
        'other.info("%s", text)',
    ],
)
def test_the_content_lint_lets_counts_and_enums_through(line: str) -> None:
    assert log_violations(line, exceptions=True) == [], line


@pytest.mark.parametrize(
    "line",
    [
        'log.exception("failed")',
        'log.error("failed", exc_info=True)',
        'log.warning("failed", exc_info=exc)',
        'log.warning("failed: %s", exc)',
        'log.warning(f"failed: {exc}")',
        'log.warning("failed: %s", err)',
        'log.warning("failed: %s", e)',
        'log.warning("failed: %s", str(exc))',
        'log.warning("failed: %s", exc.args)',
    ],
)
def test_the_exception_lint_bites_in_keyboard_modules(line: str) -> None:
    assert log_violations(line, exceptions=True), line
    assert not log_violations(line, exceptions=False), line  # outside keyboard/ this rule does not apply


def test_the_exception_lint_allows_the_type_name() -> None:
    assert log_violations('log.warning("failed: %s", type(exc).__name__)', exceptions=True) == []


@pytest.mark.parametrize(
    "line",
    [
        'raise KeyRefused(f"bad stroke {stroke!r}")',
        'raise ValueError("bad " + char)',
        'raise OSError(f"cannot type {text}")',
        'raise ValueError("%s" % word)',
        'raise ValueError("x".format(buffer))',
    ],
)
def test_the_raise_lint_bites(line: str) -> None:
    assert log_violations(line, exceptions=True), line


def test_the_raise_lint_allows_fixed_text() -> None:
    assert log_violations('raise KeyRefused("that key is not allowed")', exceptions=True) == []
    assert log_violations('raise ValueError(f"size {len(text)}")', exceptions=True) == []
    assert log_violations("raise", exceptions=True) == []


# --------------------------------------------------------------------------- stubs (6.0 rule 2)

#: what each stub module must offer, by name (DESIGN 3.x). A class maps to its members; a plain name to None.
PINNED: dict[str, dict[str, set[str] | None]] = {
    "keyboard.layout": {
        "WIDTH_U": None,
        "CellKind": None,
        "ROW_TABLE": None,
        "REVIEW_ROW_TABLE": None,
        "LAYOUTS": None,
        "layout_for": None,
        "LAYOUT": None,
        "KEY_COUNT": None,
        "key_at": None,
        "ROWS": None,
        "char_for": None,
        "legend": None,
        "Key": {"index", "kind", "row", "col", "width", "en", "he"},
        "Layout": {"name", "rows", "home_v", "keys", "gaps", "key_at", "find", "count"},
    },
    "keyboard.hands": {"HandTracker": {"update", "reset"}},
    "keyboard.press": {"PressUnavailable": None, "make_press": None},
    "keyboard.press_pinch": {
        "PinchPress": {
            "name",
            "requires_review",
            "rejects",
            "update",
            "reset",
            "set_finger",
            "fingers",
            "quality",
            "set_level",
            "set_calibrating",
        },
    },
    "keyboard.press_air": {
        "AirTapPress": {
            "name",
            "requires_review",
            "rejects",
            "update",
            "reset",
            "set_finger",
            "fingers",
            "quality",
            "set_level",
            "set_calibrating",
        },
    },
    "keyboard.plane": {
        "Plane": {"cx", "cy", "px", "py", "rows", "units", "pose"},
        "Placement": {"plane", "home", "home_f"},
        "place_plane": None,
    },
    "keyboard.synth": {
        "SynthHand": {"side", "at", "posture", "pinch", "coactivation", "jitter", "z_noise", "rng", "observe"},
        "Typist": {"hover", "place", "warm", "press", "type", "tap_n"},
        "AirTypist": {"observe"},
        "Noise": {"sigma", "ar", "blur", "glitch_p", "glitch_fw", "amp_gain", "coh_deg", "coh_ar"},
        "Burst": {"observe"},
        "STYLES": None,
        "REST": None,
    },
    "keyboard.warmup": {
        "Warmup": {"update", "thresholds", "prompt", "strays", "required", "done", "complete", "depth"},
    },
    "keyboard.ladder": {"Level": None, "AirLadder": {"level", "reason", "strict", "update"}},
    "keyboard.compose": {
        "InsertRefusal": None,
        "insert_check": None,
        "ComposeBuffer": {
            "__len__",
            "__repr__",
            "version",
            "last_edit_t",
            "text",
            "touches",
            "append",
            "backspace",
            "clear",
            "consume",
            "replace_span",
        },
    },
    "keyboard.review": {
        "InsertStep": {"stroke", "index", "total", "first", "run", "kind"},
        "InsertSummary": {"kind", "outcome", "sent", "of", "reason"},
        "ReviewMachine": {
            "state",
            "buffer",
            "counts",
            "running",
            "tap",
            "tick",
            "note_step",
            "take_summary",
            "disarm",
            "discard",
            "view",
            "_after_edit",
            "_poll_decoder",
            "_chip_tap",
            "_undo_correction",
        },
        "GUARD_OF_KEY": None,
        "GUARD_TAPS": None,
        "GUARD_WINDOW": None,
        "REVIEW_TEXT": None,
    },
    "keyboard.session": {
        "SessionOutput": {"strokes", "view", "closed", "steps"},
        "KeyboardSession": {
            "update",
            "note_result",
            "note_step",
            "take_summary",
            "recenter",
            "set_private",
            "close",
            "closed",
            "armed",
            "private",
            "counts",
            "press_name",
            "review_state",
            "compose_len",
            "discarded",
            "practice_result",
        },
    },
    "keyboard.sink": {
        "SinkFailed": None,
        "KeySink": {
            "start",
            "gate",
            "send",
            "begin_run",
            "send_run",
            "end_run",
            "target_name",
            "runaway",
            "last_hold",
            "run_active",
            "counts",
        },
    },
    "keyboard.practice": {
        "PracticeResult": {
            "completed",
            "presses",
            "correct",
            "hit_rate",
            "rest_s",
            "phantoms",
            "phantoms_per_min",
            "fps",
            "per_finger",
            "talk_s",
            "talk_phantoms",
        },
        "load_marker": None,
    },
    "keyboard.trace": {"cleanup": None},
    "keyboard.rig": {
        "KbRig": {
            "feed",
            "typed",
            "closed",
            "counts",
            "tap",
            "insert",
            "send",
            "box",
            "summary_log",
            "chips",
            "chip_active",
        },
        "ScriptedPress": {
            "name",
            "requires_review",
            "rejects",
            "update",
            "reset",
            "set_finger",
            "fingers",
            "quality",
            "set_level",
            "set_calibrating",
        },
    },
    "keyboard.controller": {
        "KeyboardDeps": {
            "data_dir",
            "desktop",
            "overlay",
            "displays",
            "ready",
            "paused",
            "desktop_blocked",
            "overlay_enabled",
            "fps",
            "emit",
            "show",
            "pointer_off",
            "pointer_reset",
            "report",
            "clock",
        },
        "KeyboardController": {
            "settings",
            "active",
            "wants_two_hands",
            "needs_release",
            "take_release",
            "command",
            "frame",
            "pointer_frame",
            "close",
            "status",
        },
    },
    "keyboard.keytest": {"add_arguments": None, "run": None},
    "keyboard.keytrace": {"add_arguments": None, "run": None},
    "keyboard.keyreplay": {"add_arguments": None, "run": None},
    "overlay.keyboard_render": {
        "KeyboardGeometry": {"pitch", "width", "height", "keys", "strip", "echo", "compose"},
        "keyboard_geometry": None,
        "bake_base": None,
        "compose": None,
        "KeyboardDrawError": {"code"},
        "DrawErrorCode": None,
        "DRAW_ERROR_CODES": None,
    },
    "overlay.text": {"base_direction": None, "wrap_lines": None, "bidi_display": None},
}
OWNER_OF: dict[str, str] = {
    **{f"keyboard.{m}": o for m, o in STUB_OWNERS.items()},
    **{f"overlay.{m}": o for m, o in OVERLAY_STUB_OWNERS.items()},
}
STUB_LABELS = list(OWNER_OF)
#: Exception types that are part of the contract and so are real in their stub: the stub rules skip the constructor and
#: ``__str__`` of these and nothing else; ``test_the_draw_error_*`` below holds their shape.
REAL_IN_STUB: dict[str, set[str]] = {"overlay.keyboard_render": {"KeyboardDrawError"}}


def stub_tree(label: str) -> ast.Module:
    return ast.parse(path_of(label).read_text(encoding="utf-8"))


def marker(tree: ast.Module, name: str) -> Any:
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
            and node.value
        ):
            return ast.literal_eval(node.value)
    return None


def is_stub(label: str) -> bool:
    return marker(stub_tree(label), "STUB_OWNER") is not None


def canonical_getattr(label: str, owner: str) -> list[ast.stmt]:
    source = f'''
def __getattr__(name: str) -> object:
    if name in _STUB_DATA:
        raise NotImplementedError("{owner}: {label}")
    raise AttributeError(f"module {{__name__!r}} has no attribute {{name!r}}")
'''
    return ast.parse(source).body[0].body  # type: ignore[attr-defined]


def body_without_docstring(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.stmt]:
    body = list(fn.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return body


SIMPLE_VALUE = (
    ast.Constant,
    ast.Tuple,
    ast.List,
    ast.Dict,
    ast.Name,
    ast.Attribute,
    ast.Subscript,
    ast.UnaryOp,
    ast.BinOp,
    ast.BoolOp,
)


def stub_problems(label: str, owner: str, tree: ast.Module | None = None) -> list[str]:
    """Everything in a stub that is more than a pinned signature with a body that raises."""
    tree = tree if tree is not None else stub_tree(label)
    problems: list[str] = []
    message = f"{owner}: {label}"

    def check_function(fn: ast.FunctionDef | ast.AsyncFunctionDef, where: str) -> None:
        if fn.name == "__getattr__" and where == "module":
            if [ast.dump(s) for s in body_without_docstring(fn)] != [
                ast.dump(s) for s in canonical_getattr(label, owner)
            ]:
                problems.append("module __getattr__ is not the canonical stub lookup")
            return
        body = body_without_docstring(fn)
        ok = (
            len(body) == 1
            and isinstance(body[0], ast.Raise)
            and isinstance(body[0].exc, ast.Call)
            and isinstance(body[0].exc.func, ast.Name)
            and body[0].exc.func.id == "NotImplementedError"
            and len(body[0].exc.args) == 1
            and isinstance(body[0].exc.args[0], ast.Constant)
            and body[0].exc.args[0].value == message
            and not body[0].exc.keywords
        )
        if not ok:
            problems.append(f"{where}.{fn.name} is not `raise NotImplementedError({message!r})`")

    def check_value(node: ast.AST, where: str) -> None:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call) and not (
                isinstance(sub.func, ast.Name) and sub.func.id in ("field", "dataclass")
            ):
                problems.append(f"{where}: a call in a value")
            if isinstance(sub, ast.Lambda | ast.ListComp | ast.DictComp | ast.SetComp | ast.GeneratorExp):
                problems.append(f"{where}: logic in a value")

    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue
        if isinstance(node, ast.Import | ast.ImportFrom):
            continue
        if isinstance(node, ast.If) and _is_type_checking(node.test) and not node.orelse:
            if not all(isinstance(sub, ast.Import | ast.ImportFrom) for sub in node.body):
                problems.append("TYPE_CHECKING block with more than imports")
            continue
        if isinstance(node, ast.Assign | ast.AnnAssign):
            if node.value is not None:
                check_value(node.value, "module value")
            continue
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            check_function(node, "module")
            continue
        if isinstance(node, ast.ClassDef):
            for deco in node.decorator_list:
                check_value(deco, f"decorator of {node.name}")
            real = node.name in REAL_IN_STUB.get(label, ())
            for member in node.body:
                if isinstance(member, ast.Expr) and isinstance(member.value, ast.Constant):
                    continue
                if isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef):
                    if not (real and member.name in ("__init__", "__str__")):
                        check_function(member, node.name)
                elif isinstance(member, ast.Assign | ast.AnnAssign):
                    if member.value is not None:
                        check_value(member.value, f"{node.name} attribute")
                elif isinstance(member, ast.Pass):
                    continue
                else:
                    problems.append(f"{node.name}: {type(member).__name__} in a class body")
            continue
        problems.append(f"top level {type(node).__name__} (line {node.lineno})")
    return problems


@pytest.mark.parametrize("label", STUB_LABELS)
def test_a_stub_declares_its_owner(label: str) -> None:
    owner = marker(stub_tree(label), "STUB_OWNER")
    if owner is None:
        pytest.skip(f"{label} has been implemented: its own tests take over")
    assert owner == OWNER_OF[label]


@pytest.mark.parametrize("label", STUB_LABELS)
def test_a_stub_is_pinned_signatures_and_raising_bodies_only(label: str) -> None:
    if not is_stub(label):
        pytest.skip(f"{label} has been implemented")
    assert stub_problems(label, OWNER_OF[label]) == []


def defined_names(label: str) -> set[str]:
    module = importlib.import_module(f"{PKG}.{label}")
    return set(dir(module)) | set(getattr(module, "_STUB_DATA", ()))


def member_names(cls: type) -> set[str]:
    names = set(dir(cls)) | set(getattr(cls, "__annotations__", {}))
    for base in cls.__mro__:
        names |= set(getattr(base, "__annotations__", {}))
    return names


PINNED_CASES = [(label, name) for label, names in PINNED.items() for name in names]


@pytest.mark.parametrize(("label", "name"), PINNED_CASES, ids=[f"{label}:{name}" for label, name in PINNED_CASES])
def test_the_pinned_names_are_offered(label: str, name: str) -> None:
    assert name in defined_names(label)
    members = PINNED[label][name]
    if members is not None:
        module = importlib.import_module(f"{PKG}.{label}")
        cls = getattr(module, name)
        assert isinstance(cls, type), f"{label}.{name} should be a class"
        missing = members - member_names(cls)
        assert not missing, missing


@pytest.mark.parametrize("label", STUB_LABELS)
def test_a_stub_module_imports_quietly_and_fails_loudly_when_used(label: str) -> None:
    if not is_stub(label):
        pytest.skip(f"{label} has been implemented")
    module = importlib.import_module(f"{PKG}.{label}")
    message = f"{OWNER_OF[label]}: {label}"
    tree = stub_tree(label)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name != "__getattr__":
            fn = getattr(module, node.name)
            required = sum(1 for a in node.args.args if a is not None) - len(node.args.defaults)
            kw_required = {
                a.arg: None for a, d in zip(node.args.kwonlyargs, node.args.kw_defaults, strict=True) if d is None
            }
            with pytest.raises(NotImplementedError, match=message):
                fn(*([None] * required), **kw_required)
        elif isinstance(node, ast.ClassDef):
            init = next((m for m in node.body if isinstance(m, ast.FunctionDef) and m.name == "__init__"), None)
            if init is None or node.name in REAL_IN_STUB.get(label, ()):
                continue
            cls = getattr(module, node.name)
            required = len(init.args.args) - 1 - len(init.args.defaults)
            kw_required = {
                a.arg: None for a, d in zip(init.args.kwonlyargs, init.args.kw_defaults, strict=True) if d is None
            }
            with pytest.raises(NotImplementedError, match=message):
                cls(*([None] * required), **kw_required)
    for name in getattr(module, "_STUB_DATA", ()):
        with pytest.raises(NotImplementedError, match=message):
            getattr(module, name)
    with pytest.raises(AttributeError):
        _ = module.no_such_name_at_all


def test_the_stub_checker_bites() -> None:
    """The checks above would pass an implemented module if they only looked for a raise: these are the failures."""
    label, owner = "keyboard.layout", "T1"
    good = 'STUB_OWNER = "T1"\n\ndef f() -> int:\n    raise NotImplementedError("T1: keyboard.layout")\n'
    cases = {
        "logic": "def f() -> int:\n    return 1\n",
        "wrong text": 'def f() -> int:\n    raise NotImplementedError("T1: keyboard.plane")\n',
        "wrong owner": 'def f() -> int:\n    raise NotImplementedError("T2: keyboard.layout")\n',
        "two statements": 'def f() -> int:\n    x = 1\n    raise NotImplementedError("T1: keyboard.layout")\n',
        "other exception": 'def f() -> int:\n    raise RuntimeError("T1: keyboard.layout")\n',
        "no text": "def f() -> int:\n    raise NotImplementedError\n",
        "method logic": "class A:\n    def m(self) -> int:\n        return 2\n",
        "toplevel call": "x = compute()\n",
        "toplevel if": "if True:\n    pass\n",
        "pass body": "def f() -> None:\n    pass\n",
        "bad getattr": "def __getattr__(name):\n    return 1\n",
        "loop": "for i in range(3):\n    pass\n",
        "list comp": "x = [i for i in range(3)]\n",
    }

    def problems_of(source: str) -> list[str]:
        return stub_problems(label, owner, ast.parse(source))

    assert problems_of(good) == []
    assert problems_of("if TYPE_CHECKING:\n    from .types import Side\n") == []
    assert problems_of("if TYPE_CHECKING:\n    x = compute()\n")
    for name, source in cases.items():
        assert problems_of(source), name


def test_the_real_exception_in_a_stub_is_exempt_for_two_methods_only() -> None:
    label, owner = "overlay.keyboard_render", "T4"
    source = "class {name}(RuntimeError):\n    def __init__(self, code: str) -> None:\n        self.code = code\n{more}"
    extra = "    def other(self) -> int:\n        return 1\n"

    def problems_of(name: str, more: str = "") -> list[str]:
        return stub_problems(label, owner, ast.parse(source.format(name=name, more=more)))

    assert problems_of("KeyboardDrawError") == []
    assert problems_of("KeyboardDrawError", extra)  # a third method is logic
    assert problems_of("SomethingElse")  # and no other class gets the exemption


# --------------------------------------------------------------------------- key counts are derived (P11)

#: The layout's own test pins the counts on purpose (U47); nowhere else may a test or a comment state them.
KEY_COUNT_TESTS = {"test_kb_layout.py"}
KEY_COUNTS = {40, 45}  # not a key count: these are the numbers the scan looks for
#: A line, or a docstring, that says it is not about the keys is let through.
NOT_A_KEY_COUNT = "not a key count"
KEY_COUNT_IN_TEXT = re.compile(r"(?<![\w.])(?:40|45)(?!\w|\.\d)")
KEY_COUNT_IN_DOCS = re.compile(r"\b(?:40|45)[ -](?:direct |review )?(?:keys|cells)\b", re.IGNORECASE)


def key_count_sites(source: str) -> list[str]:
    """``line: text`` of every key count written in code, a comment or a docstring, bar the lines that disown it."""
    lines = source.splitlines()
    tree = ast.parse(source)
    found: list[int] = []
    docstrings: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            first, last = node.lineno, node.end_lineno or node.lineno
            docstrings.append((first, last))
            body = lines[first - 1 : last]
            if NOT_A_KEY_COUNT not in " ".join(body).lower():
                found += [first + i for i, line in enumerate(body) if KEY_COUNT_IN_TEXT.search(line)]
        elif isinstance(node, ast.Constant) and type(node.value) is int and node.value in KEY_COUNTS:
            if ast.get_source_segment(source, node) == str(node.value):  # decimal: 0x28 and 0x2D are VK codes
                found.append(node.lineno)
    for token in tokenize.generate_tokens(iter(source.splitlines(keepends=True)).__next__):
        if token.type == tokenize.COMMENT and KEY_COUNT_IN_TEXT.search(token.string):
            found.append(token.start[0])
    return [
        f"{n}: {lines[n - 1].strip()}"
        for n in sorted(set(found))
        if NOT_A_KEY_COUNT not in lines[n - 1].lower() or any(a <= n <= b for a, b in docstrings)
    ]


def key_count_files() -> list[Path]:
    files = [*keyboard_files(), SRC / "overlay" / "keyboard_render.py", SRC / "overlay" / "text.py"]
    files += [SRC / "desktop" / "keys.py", *sorted((HANDS / "tests").glob("test_kb_*.py"))]
    return [path for path in files if path.name not in KEY_COUNT_TESTS]


@pytest.mark.parametrize("path", key_count_files(), ids=lambda p: p.name)
def test_no_test_or_comment_states_the_number_of_keys(path: Path) -> None:
    """P11: ``KEY_COUNT`` and ``Layout.count`` come from the tables; only U47 pins the counts."""
    sites = key_count_sites(path.read_text(encoding="utf-8"))
    assert not sites, f"derive it from the layout, or say 'not a key count' on the line: {sites}"


def test_no_document_states_the_number_of_keys() -> None:
    documents = [p for p in REPO.rglob("*.md") if not {".venv", "node_modules", ".git"} & set(p.parts)]
    assert documents  # the repository has documents; the scan reads them
    said = {p.relative_to(REPO).as_posix() for p in documents if KEY_COUNT_IN_DOCS.search(p.read_text("utf-8"))}
    assert not said, f"say 'every key' or name the layout, not a number: {sorted(said)}"


@pytest.mark.parametrize(
    "source",
    [
        "x = len(keys) == 40",
        "for i in range(45):\n    pass",
        '"""The direct layout (40 keys)."""',
        'def f():\n    """Short.\n\n    The review layout has 45 cells.\n    """',
        "y = 1  # all 40 of them",
        "# 40 | 45.",
        "class A:\n    #: the 45 in review\n    n: int = 0",
        "z = [0] * 40",
    ],
)
def test_the_key_count_scan_bites(source: str) -> None:
    assert key_count_sites(source), source


@pytest.mark.parametrize(
    "source",
    [
        "x = 0.40",
        "x = 0.45  # seconds",
        "code = 0x28  # VK_DOWN",
        "code = 0x2D",
        '"""Takes 400 frames, 1.45 s or 4045 px."""',
        "y = 41\nz = [0] * 39",
        "# 140 and 450",
        "a = 40  # not a key count: a width in pixels",
        'def f():\n    """About 40 pixels (not a key count)."""',
        'name = "40 keys"',  # a string that is not a docstring or a comment is not documentation
    ],
)
def test_the_key_count_scan_lets_other_numbers_through(source: str) -> None:
    assert key_count_sites(source) == [], source


# --------------------------------------------------------------------------- the draw error (3.11 item 6, F4, S22b)

#: The fixed strings DESIGN 3.11 item 6 allows as the code of a ``KeyboardDrawError``.
DRAW_CODES = ("font", "surface", "size", "internal")
DRAW_SENTINEL = "zzqxjv"


def draw_error_problems(cls: type) -> list[str]:
    """What is wrong with a ``KeyboardDrawError``: it may carry one of four fixed codes and no other text (F4)."""
    problems: list[str] = []
    if not (isinstance(cls, type) and issubclass(cls, Exception)):
        return ["not an exception class"]
    for code in DRAW_CODES:
        err = cls(code)
        shown = (getattr(err, "code", None), str(err), err.args, repr(err))
        if shown != (code, code, (code,), f"{cls.__name__}({code!r})"):
            problems.append(f"{code}: {shown}")
    hostile = [
        DRAW_SENTINEL,
        f"bad stroke {DRAW_SENTINEL!r}",
        RuntimeError(DRAW_SENTINEL),
        OSError(DRAW_SENTINEL),
        DRAW_SENTINEL.encode(),
        "",
        "Font",
        None,
        5,
    ]
    for value in hostile:
        err = cls(value)
        carried = f"{err} {err!r} {err.args!r} {vars(err)!r} {getattr(err, 'code', '')!r}"
        if DRAW_SENTINEL in carried or "bad stroke" in carried:
            problems.append(f"{type(value).__name__}: the text got through: {carried}")
        elif (getattr(err, "code", None), str(err), err.args) != ("internal", "internal", ("internal",)):
            problems.append(f"{type(value).__name__}: not turned into 'internal': {carried}")
    return problems


def test_the_draw_error_carries_one_of_four_fixed_codes_and_no_other_text() -> None:
    from jarvis_hands.overlay import keyboard_render as kr

    assert tuple(kr.DRAW_ERROR_CODES) == DRAW_CODES
    assert draw_error_problems(kr.KeyboardDrawError) == []


def test_the_draw_error_checker_bites() -> None:
    class Bare(RuntimeError):  # what the stub was: a free-text exception without a code
        pass

    class Passes(RuntimeError):
        def __init__(self, code: object) -> None:
            super().__init__(code)
            self.code = code

    class Verbose(RuntimeError):
        def __init__(self, code: object) -> None:
            super().__init__(code if code in DRAW_CODES else "internal")
            self.code = self.args[0]

        def __str__(self) -> str:
            return f"cannot draw ({self.code}): {self.__cause__}"

    class Right(RuntimeError):
        def __init__(self, code: object = "internal") -> None:
            safe = code if type(code) is str and code in DRAW_CODES else "internal"
            super().__init__(safe)
            self.code = safe

        def __str__(self) -> str:
            return self.code

    assert draw_error_problems(Right) == []
    for wrong in (Bare, Passes, Verbose):
        assert draw_error_problems(wrong), wrong.__name__
    assert draw_error_problems(object)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- frozen files (6.4)

FROZEN = [
    *(f"plugin/hands/src/jarvis_hands/{m}.py" for m in (
        "gestures", "executor", "actions", "calibration", "mapping", "poses", "filters", "settings", "control",
        "events", "landmarks", "geometry", "clock", "synthetic",
    )),
    "plugin/hands/src/jarvis_hands/camera",
    "plugin/hands/src/jarvis_hands/tracker",
    "plugin/hands/src/jarvis_hands/overlay/render.py",
    "plugin/hooks/register.tsx",
    "plugin/hooks/test-harness.ts",
    *(f"plugin/hands/tests/{m}.py" for m in ("conftest", "scripted", "test_runtime", "test_run_fake", "test_poses")),
]  # fmt: skip


def changed_since(repo: Path, base: str, paths: list[str]) -> list[str]:
    """Files of `paths` that differ from `base` in the working tree (committed or not). Raises when git cannot say."""
    result = subprocess.run(
        ["git", "-C", str(repo), "diff", "--name-only", base, "--", *paths],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git diff failed: {result.stderr.strip()}")
    return [line for line in result.stdout.splitlines() if line]


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_the_frozen_file_check_sees_a_change(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The machine's own git settings must not decide this test: here they sign every commit with a program that does
    # not exist and run a hook that refuses, so only a test that sets its own terms can commit.
    hostile = tmp_path_factory.mktemp("hostile-git")
    (hostile / "hooks").mkdir()
    hook = hostile / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    config = hostile / "gitconfig"
    config.write_text(
        f"[commit]\n\tgpgsign = true\n[tag]\n\tgpgsign = true\n[gpg]\n\tprogram = {(hostile / 'no-gpg').as_posix()}\n"
        f"[core]\n\thooksPath = {(hostile / 'hooks').as_posix()}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    no_hooks = tmp_path_factory.mktemp("no-hooks")

    def git(*args: str) -> None:
        subprocess.run(
            [
                "git",
                "-C",
                str(tmp_path),
                *("-c", "user.email=t@example.invalid", "-c", "user.name=t"),
                *(
                    "-c",
                    "commit.gpgsign=false",
                    "-c",
                    "tag.gpgsign=false",
                    "-c",
                    f"core.hooksPath={no_hooks.as_posix()}",
                ),
                *args,
            ],
            check=True,
            capture_output=True,
        )

    git("init", "-q")
    (tmp_path / "frozen.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "free.py").write_text("y = 1\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-q", "-m", "base")
    assert changed_since(tmp_path, "HEAD", ["frozen.py"]) == []
    (tmp_path / "free.py").write_text("y = 2\n", encoding="utf-8")
    assert changed_since(tmp_path, "HEAD", ["frozen.py"]) == []
    (tmp_path / "frozen.py").write_text("x = 2\n", encoding="utf-8")
    assert sorted(changed_since(tmp_path, "HEAD", ["frozen.py", "free.py"])) == ["free.py", "frozen.py"]
    with pytest.raises(RuntimeError):
        changed_since(tmp_path, "no-such-revision", ["frozen.py"])


def test_the_frozen_list_names_files_that_exist() -> None:
    for entry in FROZEN:
        assert (REPO / entry).exists(), entry


@pytest.mark.skipif(
    not os.environ.get("KB_FROZEN_BASE"), reason="set KB_FROZEN_BASE=<base commit> to check the frozen files (6.4)"
)
def test_the_frozen_files_are_untouched() -> None:
    base = os.environ["KB_FROZEN_BASE"]
    assert changed_since(REPO, base, FROZEN) == []
