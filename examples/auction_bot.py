"""A random bot for the auction (example of driving a FolderUI from code):

    python examples/auction_bot.py <folder> <name>
"""
import random
import re
import sys

from folderui import Session

PITCHES = ["A legendary treasure, worth every coin!", "A family heirloom, well preserved.",
           "Honestly, I'm not sure what it's worth."]


def main(path, name):
    s, rng = Session(path), random.Random()
    s.ready()
    while inputs := s.inputs():
        if "join" in inputs:
            _, text = s.input("join", name)
            if not text.startswith("✅"):  # name already taken
                name += "'"
        elif "pick" in inputs:  # the **names** in hand.md are the options
            s.input("pick", rng.choice(re.findall(r"\*\*(.+?)\*\*", s.read("hand.md"))))
        elif "describe" in inputs:
            s.input("describe", rng.choice(PITCHES))
        elif "bid" in inputs and rng.random() < 0.6:
            _, text = s.input("bid", str(rng.randint(5, 60)))
            if not text.startswith("✅"):  # below the minimum raise or not enough money: drop out instead
                s.input("pass")
        elif "pass" in inputs:
            s.input("pass")
    print(f"{name}: done")


if __name__ == "__main__":
    main(*sys.argv[1:3])
