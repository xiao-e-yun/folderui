"""A random bot for Frontier (example of driving a FolderUI from code):

    python examples/frontier_bot.py <folder> <name>
"""
import random
import sys

from folderui import Session


def main(path, name):
    s, rng = Session(path), random.Random()
    s.ready()
    while inputs := s.inputs():
        if "join" in inputs:
            _, text = s.input("join", name)
            if text.startswith("Rejected"):  # name already taken
                name += "'"
            continue
        for button in inputs:
            if button.endswith("/move"):
                for d in rng.sample("NSEW", 4):  # try directions until one is legal
                    if not s.input(button, d)[1].startswith("Rejected"):
                        break
            elif button.endswith("/recruit") and rng.random() < 0.5:
                s.input(button, "1")
        if rng.random() < 0.2:
            s.input("say", rng.choice(["Peace?", "I am coming for you.", "Let's split the middle."]))
        s.input("end_turn")
    print(f"{name}: done")


if __name__ == "__main__":
    main(*sys.argv[1:3])
