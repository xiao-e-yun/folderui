"""單人範例：猜數字。不需要伺服器，一個 FolderUI 就是整個應用。

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
        self.file("rules.md", "# 猜數字\n\n我想了一個 1-100 的整數，用 `./guess.sh <數字>` 猜猜看。\n")
        if self.tries:  # 只往後加 → 適合當紀錄
            self.file("history.md", "".join(f"- {n}：{r}\n" for n, r in self.tries))
        self.button("guess", "<1-100 的整數>")
        self.status(f"已經猜了 {len(self.tries)} 次。")

    def action(self, name, args):
        if len(args) != 1 or not args[0].isdigit() or not 1 <= int(args[0]) <= 100:
            raise ValueError("用法：./guess.sh <1-100 的整數>")
        n = int(args[0])
        result = "🎉 答對了" if n == self.secret else ("太大" if n > self.secret else "太小")
        self.tries.append((n, result))
        if n == self.secret:
            self.close()
            return f"🎉 答對了！答案是 {n}，共猜了 {len(self.tries)} 次"
        return f"{n} {result}"


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir")
    ap.add_argument("--seed", type=int)
    args = ap.parse_args()
    ui = Guess(args.dir or tempfile.mkdtemp(prefix="folderui-guess-"), args.seed)
    print(ui.root, flush=True)
    ui.run()
