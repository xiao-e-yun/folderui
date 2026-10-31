"""Single-player example: guess the number. No shared state needed; one FolderUI is the whole app.

    uv run python examples/guess.py [--dir DIR] [--seed N]
"""
import argparse
import random
import tempfile

from folderui import FolderUI


class Guess(FolderUI):
    def __init__(self, path, seed=None):
        super().__init__(path)
        self.secret = random.Random(seed).randint(1, 100)
        self.tries: list[tuple[int, str]] = []

    def draw(self):
        self.file("rules.md", "# Guess the number\n\nI picked an integer from 1 to 100. Guess it with `./guess.sh <number>`.\n")
        if self.tries:  # append-only, so it works well as a log
            self.file("history.md", "".join(f"- {n}: {r}\n" for n, r in self.tries))
        self.input("guess")

    def display(self, result):
        if self.closed:
            return result
        return f"{result}\n\n{len(self.tries)} guesses so far. Keep going with ./guess.sh <number>."

    def action(self, name, args):
        if len(args) != 1 or not args[0].isdigit() or not 1 <= int(args[0]) <= 100:
            return "❌ usage: ./guess.sh <integer from 1 to 100>"
        n = int(args[0])
        result = "🎉 correct" if n == self.secret else ("too high" if n > self.secret else "too low")
        self.tries.append((n, result))
        if n == self.secret:
            self.close()
            return f"🎉 Correct! The answer is {n}. You took {len(self.tries)} guesses."
        return f"{n} is {result}"


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir")
    ap.add_argument("--seed", type=int)
    args = ap.parse_args()
    ui = Guess(args.dir or tempfile.mkdtemp(prefix="folderui-guess-"), args.seed)
    print(ui.root, flush=True)
    ui.run()
