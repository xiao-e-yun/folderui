"""The screen is a tree of nodes. draw() returns the top-level nodes; each node renders itself through a ctx.

Primitives (on ctx): folder(name) -> sub-ctx, file(name, content), action(name, help), read(name), emit(name, *args),
and render(node) to place a child node. Folder, File and Action are thin nodes over these; Editable is built in.

An app can write its own node: subclass Node, implement render(ctx), and optionally action(name, args, ctx).
A button press goes to the innermost node that placed it first, with the name relative to where that node was
placed. The node returns a response (stop there), returns PASS (let it bubble up unchanged), or calls
ctx.emit(name, *args) to send its own event up. Whatever reaches the top goes to the UI's action(name, args).
"""
import dataclasses
import tomllib
from typing import Any


class _Pass:
    def __repr__(self):
        return "PASS"


PASS = _Pass()  # returned by Node.action: "not mine, let it bubble up"


def join(base: str, name: str) -> str:
    return f"{base}/{name}" if base else name


def check_name(name: str) -> str:
    parts = name.split("/")
    if name.startswith("/") or any(not p or p.startswith(".") for p in parts):
        raise ValueError(f"names must be relative, with no empty or hidden segments: {name!r}")
    return name


class Node:
    def render(self, ctx: "Ctx") -> None:
        raise NotImplementedError

    def action(self, name: str, args: list, ctx: "Ctx") -> Any:
        """A button this node placed was pressed (name is relative to where the node was placed)."""
        return PASS


class Ctx:
    """Where a node draws: a folder path, and the chain of nodes above it (for events).
    In a node's action() the drawing calls (folder/file/action/render) are not available; read and emit are."""

    def __init__(self, ui, frame, base: str = "", chain: tuple = ()):
        self._ui, self._frame, self.base, self._chain = ui, frame, base, chain

    def folder(self, name: str) -> "Ctx":
        return Ctx(self._ui, self._frame, join(self.base, check_name(name)), self._chain)

    def file(self, name: str, content: str, writable: bool = False) -> None:
        """A file (mode 444, or 644 if writable: the user may change it, e.g. a draft that a button submits).
        It is rewritten only when content differs from the last frame."""
        self._drawing().add_file(join(self.base, check_name(name)), content, 0o644 if writable else 0o444)

    def action(self, name: str, help: str = "") -> None:
        self._drawing().add_action(join(self.base, check_name(name)), help, self._chain)

    def render(self, node: "Node") -> None:
        node.render(Ctx(self._ui, self._drawing(), self.base, (*self._chain, (node, self.base))))

    def read(self, name: str) -> str:
        """What is in the file on disk right now ("" if it is missing): e.g. a file the user edited."""
        return self._ui._read(join(self.base, check_name(name)))

    def emit(self, name: str, *args) -> Any:
        """From a node's action(): send an event up to the nodes above it, then to the UI's action()."""
        if not self._chain:
            raise RuntimeError("emit() is for nodes; call it from a node's action()")
        node, base = self._chain[-1]
        return self._ui._dispatch(join(base, check_name(name)), list(args), self._chain[:-1])

    def _drawing(self):
        if self._frame is None:
            raise RuntimeError("nodes can only draw in render(), not in action()")
        return self._frame


class Folder(Node):
    def __init__(self, name: str, children=()):
        self.name, self.children = name, list(children)

    def render(self, ctx):
        sub = ctx.folder(self.name)
        for child in self.children:
            sub.render(child)


class File(Node):
    """A file: read-only (444), or writable (644) for drafts the user edits and a button submits."""

    def __init__(self, name: str, content: str, writable: bool = False):
        self.name, self.content, self.writable = name, content, writable

    def render(self, ctx):
        ctx.file(self.name, self.content, self.writable)


class Action(Node):
    """A button: <name>.sh. `./<name>.sh --help` prints help without pressing it."""

    def __init__(self, name: str, help: str = ""):
        self.name, self.help = name, help

    def render(self, ctx):
        ctx.action(self.name, self.help)


# ---------------- Editable: a file bound to a dict or an object ----------------

class Editable(Node):
    """A TOML file the user edits (mode 644), bound to a dict, a dataclass or a plain object.

    When the user saves it, the values are checked (known keys, same types, then data.check(new) if the object has
    one) and written into data in place. If anything is wrong, data stays as it was, the user's file is left alone
    to fix, and the reason is in <name>.error. Field help comes from dataclass metadata={"help": ...}.
    """

    HEADER = "# Edit and save: changes apply at once. Mistakes are explained in {name}.error.\n"

    def __init__(self, name: str, data):
        self.name, self.data = name, data

    def render(self, ctx):
        path = join(ctx.base, check_name(self.name))
        ctx._drawing().add_editable(path, self)
        error = ctx._ui._edit_errors.get(path)
        if error:
            ctx.file(self.name + ".error", error + "\n")

    # -- values --

    def values(self) -> dict:
        if isinstance(self.data, dict):
            return dict(self.data)
        if dataclasses.is_dataclass(self.data):
            return {f.name: getattr(self.data, f.name) for f in dataclasses.fields(self.data)}
        return {k: v for k, v in vars(self.data).items() if not k.startswith("_")}

    def helps(self) -> dict:
        if dataclasses.is_dataclass(self.data):
            return {f.name: f.metadata.get("help", "") for f in dataclasses.fields(self.data)}
        return {}

    def text(self) -> str:
        out = [self.HEADER.format(name=self.name)]
        helps = self.helps()
        for key, value in self.values().items():
            out.append("")
            if helps.get(key):
                out.append(f"# {helps[key]}")
            out.append(f"{key} = {to_toml(value)}")
        return "\n".join(out) + "\n"

    def apply(self, text: str) -> None:
        """Check the user's file and write it into data; raises ValueError with the reason."""
        try:
            got = tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            raise ValueError(f"not valid TOML: {e}") from None
        old = self.values()
        unknown = sorted(set(got) - set(old))
        if unknown:
            raise ValueError(f"unknown setting(s): {', '.join(unknown)} (known: {', '.join(old)})")
        new = dict(old)
        for key, value in got.items():
            if not same_shape(old[key], value):
                raise ValueError(f"{key} should look like {to_toml(old[key])}")
            new[key] = value
        check = getattr(self.data, "check", None)
        if callable(check) and not isinstance(self.data, dict):
            check(new)
        if isinstance(self.data, dict):
            self.data.update(new)
        else:
            for key, value in new.items():
                setattr(self.data, key, value)


def same_shape(old, new) -> bool:
    if isinstance(old, bool) or isinstance(new, bool):
        return isinstance(old, bool) and isinstance(new, bool)
    if isinstance(old, (int, float)) and not isinstance(old, bool):
        return isinstance(new, (int, float)) and (isinstance(old, float) or isinstance(new, int))
    if isinstance(old, list):
        return isinstance(new, list) and (not old or all(same_shape(old[0], v) for v in new))
    return type(old) is type(new)


def to_toml(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(to_toml(v) for v in value) + "]"
    raise TypeError(f"Editable supports strings, numbers, booleans and lists of them, not {type(value).__name__}")
