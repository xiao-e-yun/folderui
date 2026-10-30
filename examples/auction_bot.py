"""拍賣用的隨機 bot（程式型 agent 範例）：python examples/auction_bot.py <資料夾> <名字>"""
import random
import sys

from folderui import Session

PITCHES = ["傳說中的寶物，絕對物超所值！", "家族世代相傳，保存完好。", "老實說，我也不確定它值多少。"]


def main(path, name):
    s, rng = Session(path), random.Random()
    s.ready()
    while buttons := s.buttons():
        if "join" in buttons:
            ok, _ = s.press("join", name)
            if not ok:  # 名字被用走了
                name += "'"
        elif "pick" in buttons:
            s.press("pick", rng.choice(buttons["pick"]["options"]))
        elif "describe" in buttons:
            s.press("describe", rng.choice(PITCHES))
        elif "bid" in buttons and rng.random() < 0.6:
            ok, _ = s.press("bid", str(rng.randint(5, 60)))
            if not ok:  # 低於最低加價或錢不夠，改成退出
                s.press("pass")
        elif "pass" in buttons:
            s.press("pass")
    print(f"{name}: 結束")


if __name__ == "__main__":
    main(*sys.argv[1:3])
