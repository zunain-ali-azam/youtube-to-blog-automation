"""Step-style prompts on a vertical rail, in the style of clack:

    ◆  Model                 <- the active step
    │  ● claude-opus-5-5
    │  ○ claude-sonnet-5-5
    └

Once answered, a step collapses to its question and answer:

    ◇  Model
    │  claude-opus-5-5
    │

Ctrl+C (or Esc) marks the step cancelled and raises KeyboardInterrupt.
"""

from dataclasses import dataclass
from typing import Callable

from prompt_toolkit import print_formatted_text
from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import BufferControl, FormattedTextControl, HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.processors import PasswordProcessor

RAIL = "fg:ansibrightblack"
DIM = "fg:ansibrightblack"
ACTIVE = "fg:ansimagenta"
DONE = "fg:ansigreen"
WARN = "fg:ansiyellow"
CANCEL = "fg:ansired"

# Lists longer than this scroll, and typing filters them.
MAX_VISIBLE = 10

Validator = Callable[[str], bool | str]


@dataclass
class Option:
    value: str
    label: str
    hint: str = ""
    # Shown instead of the hint; the option can't be picked.
    disabled: str = ""


def _print(parts: list[tuple[str, str]]) -> None:
    print_formatted_text(FormattedText(parts))


def done(message: str, answer: str) -> None:
    """A finished step: also used for settings that are already saved."""
    _print([(DONE, "◇"), ("", f"  {message}\n"), (RAIL, "│"), (DIM, f"  {answer}\n"), (RAIL, "│")])


def _cancelled(message: str) -> None:
    _print([(CANCEL, "■"), ("", f"  {message}\n"), (RAIL, "│"), (CANCEL, "  Cancelled")])


def _run(app: Application, message: str):
    try:
        return app.run()
    except KeyboardInterrupt:
        _cancelled(message)
        raise


def _bindings() -> KeyBindings:
    kb = KeyBindings()

    @kb.add("c-c")
    @kb.add("escape", eager=True)
    def _(event):
        event.app.exit(exception=KeyboardInterrupt())

    return kb


def _header(message: str, error: str = "") -> list[tuple[str, str]]:
    return [(WARN if error else ACTIVE, "▲" if error else "◆"), ("bold", f"  {message}")]


def _footer(error: str = "") -> list[tuple[str, str]]:
    return [(WARN, f"└  {error}")] if error else [(ACTIVE, "└")]


def select(message: str, options: list[Option], default: str | None = None) -> str:
    """Arrow keys to move, Enter to pick. Long lists scroll and filter as you type."""
    searchable = len(options) > MAX_VISIBLE
    state = {"query": "", "index": 0, "offset": 0}

    def shown() -> list[Option]:
        query = state["query"].lower()
        return [o for o in options if query in o.label.lower()] if query else options

    def move(step: int) -> None:
        items = shown()
        if not items:
            return
        index = state["index"]
        for _ in range(len(items)):
            index = (index + step) % len(items)
            if not items[index].disabled:
                break
        state["index"] = index

    values = [o.value for o in options]
    state["index"] = values.index(default) if default in values and not options[values.index(default)].disabled else 0
    if options[state["index"]].disabled:
        move(1)

    def render() -> list[tuple[str, str]]:
        items = shown()
        index = min(state["index"], max(len(items) - 1, 0))
        state["index"] = index
        # Keep the highlighted row inside the visible window.
        if index < state["offset"]:
            state["offset"] = index
        elif index >= state["offset"] + MAX_VISIBLE:
            state["offset"] = index - MAX_VISIBLE + 1
        start = min(state["offset"], max(len(items) - MAX_VISIBLE, 0))

        out = _header(message) + [("", "\n")]
        if searchable:
            out += [(ACTIVE, "│"), (DIM, "  Search: "), ("", state["query"]), (ACTIVE, "▌\n")]
        if not items:
            out += [(ACTIVE, "│"), (DIM, "  No match\n")]
        if start:
            out += [(ACTIVE, "│"), (DIM, f"  ↑ {start} more\n")]
        for i, option in enumerate(items[start:start + MAX_VISIBLE], start):
            out += [(ACTIVE, "│"), ("", "  ")]
            if option.disabled:
                out += [(DIM, f"○ {option.label}"), (DIM, f"  ({option.disabled})\n")]
            elif i == index:
                out += [(DONE, "● "), ("", option.label), (DIM, f"  {option.hint}" if option.hint else ""), ("", "\n")]
            else:
                out += [(DIM, f"○ {option.label}\n")]
        if len(items) > start + MAX_VISIBLE:
            out += [(ACTIVE, "│"), (DIM, f"  ↓ {len(items) - start - MAX_VISIBLE} more\n")]
        return out + _footer()

    kb = _bindings()

    @kb.add("up")
    def _(event):
        move(-1)

    @kb.add("down")
    def _(event):
        move(1)

    @kb.add("enter")
    def _(event):
        items = shown()
        if items and not items[state["index"]].disabled:
            event.app.exit(result=items[state["index"]])

    if searchable:
        @kb.add("<any>")
        def _(event):
            if event.data.isprintable() and len(event.data) == 1:
                state["query"] += event.data
                state["index"] = 0

        @kb.add("backspace")
        def _(event):
            state["query"] = state["query"][:-1]

    app = Application(layout=Layout(Window(FormattedTextControl(render, show_cursor=False))),
                      key_bindings=kb, erase_when_done=True)
    option = _run(app, message)
    done(message, option.label)
    return option.value


def text(message: str, default: str = "", validate: Validator | None = None, secret: bool = False,
         shown_as: Callable[[str], str] | None = None) -> str:
    """One line of input. `secret` hides the typing; `shown_as` formats the answer once done."""
    state = {"error": ""}
    buffer = Buffer(document=Document(default, len(default)), multiline=False)
    buffer.on_text_changed += lambda _: state.update(error="")

    kb = _bindings()

    @kb.add("enter")
    def _(event):
        value = buffer.text.strip()
        result = validate(value) if validate else True
        if result is True:
            event.app.exit(result=value)
        else:
            state["error"] = result or "Invalid"

    rail = Window(FormattedTextControl(lambda: [(WARN if state["error"] else ACTIVE, "│  ")]), width=3)
    field = Window(BufferControl(buffer, input_processors=[PasswordProcessor("•")] if secret else []), height=1)
    layout = HSplit([
        Window(FormattedTextControl(lambda: _header(message, state["error"])), height=1),
        VSplit([rail, field]),
        Window(FormattedTextControl(lambda: _footer(state["error"])), height=1),
    ])
    app = Application(layout=Layout(layout, focused_element=field), key_bindings=kb, erase_when_done=True)
    value = _run(app, message)
    done(message, shown_as(value) if shown_as else value or "(skipped)")
    return value


def confirm(message: str, default: bool = True) -> bool:
    """Yes / No; arrow keys, Tab, y or n to switch, Enter to confirm."""
    state = {"yes": default}

    def render() -> list[tuple[str, str]]:
        def side(label: str, on: bool) -> list[tuple[str, str]]:
            return [(DONE, f"● {label}")] if on else [(DIM, f"○ {label}")]
        return (_header(message) + [("", "\n"), (ACTIVE, "│  ")]
                + side("Yes", state["yes"]) + [(DIM, " / ")] + side("No", not state["yes"])
                + [("", "\n")] + _footer())

    kb = _bindings()

    @kb.add("left")
    @kb.add("right")
    @kb.add("tab")
    def _(event):
        state["yes"] = not state["yes"]

    @kb.add("y")
    def _(event):
        state["yes"] = True

    @kb.add("n")
    def _(event):
        state["yes"] = False

    @kb.add("enter")
    def _(event):
        event.app.exit(result=state["yes"])

    app = Application(layout=Layout(Window(FormattedTextControl(render, show_cursor=False))),
                      key_bindings=kb, erase_when_done=True)
    answer = _run(app, message)
    done(message, "Yes" if answer else "No")
    return answer
