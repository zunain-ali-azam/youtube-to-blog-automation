"""Terminal look: welcome banner, the step rail, stage progress, alerts and the result card.

The guided run reads top to bottom as one rail (see prompts.py for the questions):

    ┌  yt2blog
    │
    ◇  Model
    │  claude-opus-5-5
    │
    ◇  Generating
    │  ✔ Writing the article   47s
    └  Done

     ✔ SUCCESS  Blog saved to generated_blogs/…
"""

import time
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from rich.console import Console, Group
from rich.live import Live
from rich.box import ROUNDED
from rich.panel import Panel
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from explainer.pipeline import STAGE_NAMES

console = Console(highlight=False)

ACCENT = "magenta"
RAIL = "bright_black"

STAGE_LABELS = {
    "metadata": "Fetching video details",
    "transcript": "Getting the transcript",
    "generate": "Writing the article",
    "code": "Matching code from the repo",
    "checks": "Running quality checks",
    "render": "Rendering Markdown and HTML",
    "ghost": "Creating the Ghost draft",
}


def _version() -> str:
    try:
        return version("explainer")
    except PackageNotFoundError:
        return "dev"


# --- welcome ----------------------------------------------------------------

# "YT2BLOG" in the ANSI Shadow figlet font, one list of rows per letter.
_FONT = {
    "Y": ["██╗   ██╗", "╚██╗ ██╔╝", " ╚████╔╝ ", "  ╚██╔╝  ", "   ██║   ", "   ╚═╝   "],
    "T": ["████████╗", "╚══██╔══╝", "   ██║   ", "   ██║   ", "   ██║   ", "   ╚═╝   "],
    "2": ["██████╗ ", "╚════██╗", " █████╔╝", "██╔═══╝ ", "███████╗", "╚══════╝"],
    "B": ["██████╗ ", "██╔══██╗", "██████╔╝", "██╔══██╗", "██████╔╝", "╚═════╝ "],
    "L": ["██╗     ", "██║     ", "██║     ", "██║     ", "███████╗", "╚══════╝"],
    "O": [" ██████╗ ", "██╔═══██╗", "██║   ██║", "██║   ██║", "╚██████╔╝", " ╚═════╝ "],
    "G": [" ██████╗ ", "██╔════╝ ", "██║  ███╗", "██║   ██║", "╚██████╔╝", " ╚═════╝ "],
}
# Top to bottom: light pink to deep magenta. The box-drawing edges are the shadow.
_GRADIENT = ["#f9a8d4", "#f472b6", "#e879f9", "#d946ef", "#c026d3", "#a21caf"]
_SHADOW = "#581c87"
_LOGO_WIDTH = sum(len(rows[0]) for rows in _FONT.values())

_COMMANDS = [
    ("explainer", "new blog post (this guided run)"),
    ("explainer run <url>", "the same, without questions"),
    ("explainer status", "every blog made so far"),
    ("explainer push <slug>", "send a saved blog to Ghost"),
    ("explainer start --reconfigure", "enter your API keys again"),
]


def _logo() -> Text:
    logo = Text()
    for row in range(6):
        line = "".join(_FONT[ch][row] for ch in "YT2BLOG")
        for char in line:
            logo.append(char, style=f"bold {_GRADIENT[row]}" if char == "█" else _SHADOW)
        if row == 5:
            logo.append(f"  v{_version()}", style="dim")
        logo.append("\n")
    return logo


def banner() -> None:
    """Welcome tag, the block-letter logo, what it does, the commands and the controls."""
    console.clear()
    console.print()
    welcome = Text.assemble(("✻ ", ACCENT), "Welcome to ", ("yt2blog", "bold"))
    console.print(Panel(welcome, border_style=RAIL, box=ROUNDED, expand=False, padding=(0, 1)))
    console.print()
    if console.width >= _LOGO_WIDTH + 10:
        console.print(_logo(), end="")
    else:
        # Too narrow for the logo: a cli-welcome style title badge instead.
        console.print(Text.assemble(("  yt2blog  ", f"bold black on {ACCENT}"), (f" v{_version()}", "dim")))
    console.print()
    console.print(Text("Turn a YouTube video into a blog post you can publish.", style="bold"))
    console.print(Text("You need a YouTube Data API key and an API key for your LLM provider; they're saved in .env after the first run.", style="dim"))
    console.print()
    commands = Table.grid(padding=(0, 3))
    commands.add_column(style=f"bold {ACCENT}", no_wrap=True)
    commands.add_column(style="dim")
    for command, what in _COMMANDS:
        commands.add_row(command, what)
    console.print(commands)
    console.print()
    console.print(Text.assemble(("↑↓", "bold"), (" move  ·  ", "dim"), ("Enter", "bold"), (" pick  ·  ", "dim"),
                                ("Ctrl+C", "bold"), (" quit", "dim")))
    console.print()


# --- the rail ---------------------------------------------------------------

def intro(title: str) -> None:
    console.print(Text.assemble(("┌", RAIL), "  ", (f" {title} ", f"bold black on {ACCENT}")))
    console.print(Text("│", style=RAIL))


def outro(message: str, ok: bool = True) -> None:
    console.print(Text.assemble(("└", RAIL), "  ", (message, "green" if ok else "red")))
    console.print()


class spinner:
    """`with spinner("Loading models"):` shows ◐ on the rail; nothing is left behind."""

    def __init__(self, message: str):
        grid = Table.grid()
        grid.add_row(Spinner("circleHalves", style="magenta"), Text(f"  {message}…"))
        self.live = Live(grid, console=console, transient=True, refresh_per_second=12)

    def __enter__(self) -> "spinner":
        self.live.__enter__()
        return self

    def __exit__(self, *exc) -> None:
        self.live.__exit__(*exc)


def mask(secret: str) -> str:
    return f"{secret[:4]}…{secret[-4:]}" if len(secret) > 12 else "••••"


def duration(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m {secs:02d}s" if minutes else f"{secs}s"


# event -> (icon, icon style, note in the time column)
_EVENTS = {
    "pending": ("○", RAIL, ""),
    "done": ("✔", "green", ""),
    "cached": ("✔", "dim green", "cached"),
    "skipped": ("–", RAIL, "skipped"),
    "failed": ("✖", "red", "failed"),
}


class StageProgress:
    """The pipeline as one rail step; pass `update` to run_all as on_stage."""

    def __init__(self, notes: dict[str, str] | None = None):
        self.state = {name: "pending" for name in STAGE_NAMES}
        self.started: dict[str, float] = {}
        self.took: dict[str, float] = {}
        self.notes = notes or {}
        self.result: str | None = None  # "done" or "failed" once finished
        self.live = Live(self, console=console, refresh_per_second=12)

    def __enter__(self) -> "StageProgress":
        self.live.__enter__()
        return self

    def __exit__(self, exc_type, *rest) -> None:
        self.result = "failed" if exc_type else "done"
        self.live.refresh()
        self.live.__exit__(exc_type, *rest)
        if not console.is_terminal:
            # Without a terminal, Live leaves the cursor at the end of its last line.
            console.line()

    def update(self, name: str, event: str) -> None:
        if event == "running":
            self.started[name] = time.monotonic()
        elif name in self.started:
            self.took[name] = time.monotonic() - self.started[name]
        self.state[name] = event
        self.live.refresh()

    def __rich__(self) -> Group:
        table = Table.grid(padding=(0, 1))
        table.add_column(width=2)  # rail
        table.add_column(width=1)  # icon
        table.add_column()  # label
        table.add_column(justify="right", style="dim")
        if self.result == "done":
            head = Text("◇", style="green")
        elif self.result == "failed":
            head = Text("■", style="red")
        else:
            head = Spinner("circleHalves", style="magenta")
        for name in STAGE_NAMES:
            event = self.state[name]
            quiet = event in ("pending", "skipped", "cached")
            label = Text(STAGE_LABELS[name], style="bold" if event == "running" else "dim" if quiet else "")
            if note := self.notes.get(name):
                label.append(f"  {note}", style="dim")
            if event == "running":
                icon = Spinner("dots", style=ACCENT)
                right = duration(time.monotonic() - self.started[name])
            else:
                symbol, style, right = _EVENTS[event]
                icon = Text(symbol, style=style)
                if event == "done" and name in self.took:
                    right = duration(self.took[name])
            table.add_row(Text("│", style=RAIL), icon, label, f"  {right}" if right else "")
        table.add_row(Text("│", style=RAIL), "", "", "")
        header = Table.grid()
        header.add_row(head, Text("  Generating", style="bold"))
        return Group(header, table)


# --- alerts (after the rail) ------------------------------------------------

_ALERTS = {
    "success": ("✔ SUCCESS", "black on green", "green"),
    "info": ("ℹ INFO", "black on blue", "blue"),
    "warning": ("⚠ WARNING", "black on yellow", "yellow"),
    "error": ("✖ ERROR", "white on red", "red"),
}


def alert(kind: str, message: str | Text, detail: str | Text | None = None) -> None:
    badge, badge_style, color = _ALERTS[kind]
    # Same-width badges keep the messages lined up.
    line = Text.assemble("  ", (f" {badge:<9} ", badge_style), "  ")
    line.append(message if isinstance(message, Text) else Text(message, style=color))
    console.print(line)
    if detail is not None:
        indent = " " * 15
        console.print(Text.assemble(indent, detail if isinstance(detail, Text) else Text(detail, style="dim")))


def warnings(items: list[dict]) -> None:
    if not items:
        alert("success", "Quality checks passed with no warnings")
        return
    alert("warning", f"{len(items)} quality warning{'s' if len(items) != 1 else ''}")
    indent = " " * 15
    for item in items:
        console.print(Text.assemble(indent, ("• ", "yellow"), (f"{item['check']}  ", "bold"), (item["message"], "dim")))


def next_steps(rows: list[tuple[str, str]]) -> None:
    console.print()
    console.print(Text("  Next", style="bold"))
    grid = Table.grid(padding=(0, 3))
    grid.add_column(style=ACCENT, no_wrap=True)
    grid.add_column(style="dim")
    for command, what in rows:
        grid.add_row(f"    {command}", what)
    console.print(grid)
    console.print()


def link(path: Path) -> Text:
    """A path the terminal can open with a click, shown relative to the cwd when possible."""
    try:
        shown = path.relative_to(Path.cwd())
    except ValueError:
        shown = path
    return Text(str(shown), style=f"{ACCENT} link file://{path.resolve()}")


# --- cards (status and other one-shot commands) -----------------------------

def result_card(rows: list[tuple[str, str | Text]], ok: bool, title: str) -> None:
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="dim", no_wrap=True)
    grid.add_column(overflow="fold")
    for key, value in rows:
        # Plain text, so an error message like "[Errno 2]" isn't read as markup.
        grid.add_row(key, Text(value) if isinstance(value, str) else value)
    style = "green" if ok else "red"
    icon = "✔" if ok else "✖"
    console.print()
    console.print(Panel(grid, title=Text(f" {icon} {title} ", style=f"bold {style}"), title_align="left",
                        border_style=style, padding=(1, 2)))
