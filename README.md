# folderui

Turn **a folder into an immediate-mode GUI** for humans and AI agents. The folder holds only two kinds of things:

- `*.md` and other files: the screen (`cat` them any time)
- `*.sh`: buttons (only the ones you can press right now exist)

Running a button **blocks until you have the next decision to make**, then prints whatever the app decides
(the result of your action, what happened meanwhile, what you can do now). The framework prints nothing of its own.
So an agent's loop is just "run a button → read the response → run the next button", with no extra waiting or polling.

```sh
pip install folderui     # or: uv add folderui. Standard library only; needs a POSIX shell.
```

```console
$ ./bid.sh 40                   ← blocks while the other players are bidding
✅ You bid 40
📣 bob 45 (high bid: bob 45)
📣 carol drops out
👉 Your turn to bid: ./bid.sh <amount> (at least 50) or ./pass.sh
```

## Writing a UI

Subclasses implement `draw()` to declare the screen and `action()` to handle buttons; the optional `display()`
decides what the response says.

```python
from folderui import FolderUI

class Guess(FolderUI):
    def draw(self):                      # re-declare the whole screen every frame; must not change state
        self.file("rules.md", "Guess 1-100 with ./guess.sh <number>")
        self.input("guess")

    def action(self, name, args):        # a button was pressed: return text (a rejection is just text, too)
        ...
        return "too high"

    def display(self, result):           # optional: called once per response; result is the action's text
        return f"{result}\n\n{len(self.tries)} guesses so far"

Guess("/tmp/my-ui").run()
```

| Method | Called from | Purpose |
|---|---|---|
| `file(path, content)` | `draw()` | Declare a file; undeclared files are deleted |
| `input(path)` | `draw()` | Declare a button → `<path>.sh`; paths may be nested (`players/2/kick`). Document usage in your `.md` files |
| `close()` | `draw()` or anywhere | Finish: remove the buttons, answer the waiting script, return from `run()` |
| `refresh()` | any thread | The state was changed elsewhere: redraw this UI (does not block) |
| `run(background=False)` | | Run the main loop; with `background=True`, run it in a thread and return the thread |
| `inputs` | `display()` | Read-only list of the buttons available right now |

The response is `display(result)`; without a `display()` it is just the action's return value. Like HTTP:

- **exit 0** (200): the app answered. What the text says, including whether the action was accepted, is up to the app.
  If `action()` or `display()` raises, that is the app's problem: the error is printed as the response (the full
  traceback goes to the host's stderr).
- **exit 1** (4xx): `Operation Unavailable` plus the reason: another action is in flight, or the button is no longer on screen.

The framework never appends a button list or a summary of screen changes: with a large screen (a Blender bridge,
a map game) that is only noise, so `display()` decides what to tell the agent, e.g. "your turn, use ./bid.sh <amount>"
or "the screen changed, read shop.md".

`display()` runs exactly once per response, so it can remember what this participant has already seen and include
only new messages:

```python
def display(self, result):
    with self.game.lock:
        new, self.seen = self.game.events[self.seen:], len(self.game.events)
    return "\n".join([result, *new, "Your turn"])
```

**There is only one response rule**: after the action is handled, respond at the first frame that has a button
available (or after `close()`).

- Invalid input → state unchanged, buttons still there → respond immediately (the app's error text)
- Queries → buttons still there → respond immediately
- Waiting for others after acting → no buttons → wait until it's your turn

## Multiplayer: shared state + a lock + refresh()

There is no server. Each participant gets a FolderUI, and they all share one game object:

```python
class Game:
    def __init__(self):
        self.lock = threading.Lock()
        self.uis = []
        ...

    def bid(self, who, amount):
        with self.lock:
            ...                          # pure logic; return an error text if the rules forbid it
        for ui in self.uis:
            ui.refresh()                 # only marks "needs redraw"; never blocks
        return "✅ You bid"

class PlayerUI(FolderUI):
    def draw(self):
        with self.game.lock:             # read the shared state directly
            v = self.game.view(self.me)
        ...

    def action(self, name, args):
        return self.game.bid(self.me, int(args[0]))

game = Game()
game.uis = [PlayerUI(f"{root}/player_{i}", game, i) for i in range(n)]
for t in [ui.run(background=True) for ui in game.uis]:
    t.join()
```

Each UI draws its screen and handles its buttons on its own thread; `refresh()` is the only method meant to be
called across threads.

## Driving a UI from code

`Session` speaks the same file protocol as the button scripts, for bots, tests, or your own tool loop:

```python
from folderui import Session
s = Session("/tmp/folderui-auction-xxxx/player_0")
s.ready()
print(s.read("rules.md"))
while inputs := s.inputs():              # buttons available right now; empty means closed
    ok, text = s.input("bid", "40")
```

## Details

- Scripts wait indefinitely and never time out on their own. If the agent's command is killed by a timeout, the
  request has already been sent: **run the same command again to keep waiting or get the response** (it is not
  sent twice); running a different button is rejected (exit code 1). The button's script is kept until its
  response has been read.
- Pressing a button that is no longer on screen (the screen was redrawn just as the script ran) gets an immediate
  "Operation Unavailable" response from the framework (exit code 1), without calling `action()` or `display()`.
- Concurrent presses in the same folder are serialized by a `.lock` directory; a press that finds it held is
  rejected (exit code 1).
- Protocol files are hidden: `.request`, `.response`, `.done`, `.lock`, `.state.json`.
  An agent that cannot run scripts can write `.request` directly (button path and arguments, NUL-separated).

## Examples

```sh
uv run python examples/guess.py                            # single player: guess the number
uv run python examples/auction.py --players 4 --rounds 3   # multiplayer: English auction (players + spectator)
uv run python examples/auction_bot.py <folder> bot0        # a random bot for the auction
uv run python examples/frontier.py --players 6 --turns 15  # multiplayer: simultaneous-turn territory game with fog of war (2-8 players)
uv run python examples/frontier_bot.py <folder> bot0       # a random bot for Frontier
```

## Development

```sh
uv sync
uv run pytest
```

Releasing: configure a trusted publisher on PyPI (repo `xiao-e-yun/folderui`, workflow `publish.yml`,
environment `pypi`), then bump the version in `pyproject.toml` and push a `v*` tag to publish automatically.
