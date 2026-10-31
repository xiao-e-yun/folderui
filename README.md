# folderui

把**資料夾變成立即式 GUI**，給人類和 AI agent 使用。資料夾裡只有兩種東西：

- `*.md` 等檔案：畫面（隨時可以 `cat`）
- `*.sh`：按鈕（只有現在能按的才會出現）

執行按鈕後，腳本會**一直等到你需要做下一個決定**才回應，並印出：操作結果 + 這段期間發生的事 + 目前狀況 + 可以按的按鈕。
所以 agent 的迴圈就是「執行按鈕 → 讀回應 → 執行下一個按鈕」，不需要另外等待或輪詢。

```sh
pip install folderui     # 或 uv add folderui；只用標準函式庫，需要 POSIX shell
```

```console
$ ./bid.sh 40
⏳ 已送出，等待中…              ← 其他人還在喊價，腳本卡住
✅ 已出價 40
📣 bob 45（目前最高：bob 45）    ← 這段期間發生的事（log）
📣 carol 退出
輪到你喊價，目前最高 bob 45     ← 目前狀況（status）

── 可以做的事 ──
./bid.sh  <金額 50-100>  公開喊價
./pass.sh  退出這件拍品
```

## 寫一個 UI

子類別只要實作兩個方法：`draw()` 宣告畫面，`action()` 處理按鈕。

```python
from folderui import FolderUI

class Guess(FolderUI):
    def draw(self):                      # 每一幀重新宣告畫面，不可修改狀態
        self.file("rules.md", "猜 1-100")
        for n, result in self.tries:
            self.log(f"{n}：{result}")   # 回應時只附上還沒送出過的
        self.input("guess", "<數字>")
        self.status(f"已經猜了 {len(self.tries)} 次")

    def action(self, name, args):        # 按鈕被按下：回傳文字，或 raise ValueError 代表失敗
        ...
        return "太大"

Guess("/tmp/my-ui").run()
```

| 方法 | 在哪裡呼叫 | 作用 |
|---|---|---|
| `file(path, content)` | `draw()` | 宣告一個檔案；沒宣告的會自動刪除 |
| `input(path, hint, options=None)` | `draw()` | 宣告一個按鈕 → `<path>.sh`，路徑可以分層（`players/2/kick`） |
| `log(content)` | `draw()` | 宣告一則訊息；每一幀都宣告完整的序列，回應時只附上還沒送出過的 |
| `status(text)` | `draw()` | 目前狀況：附在每次回應的最後（是回應那一刻的狀態，不是對操作的回覆） |
| `close()` | `draw()` 或任何地方 | 結束：移除按鈕、回應等待中的腳本，`run()` 返回 |
| `refresh()` | 任何執行緒 | 狀態被別人改變了：讓這個 UI 重畫（不會等待） |
| `run(background=False)` | | 執行主迴圈；`background=True` 時在背景執行緒執行並回傳該執行緒 |

**回應規則只有一條**：操作處理完之後，第一個「有按鈕可按」的畫面出現時（或 `close()`）才回應。

- 輸入錯誤 → 狀態沒變、按鈕還在 → 立刻回應（`raise` 的訊息，exit code 1）
- 查詢類操作 → 按鈕還在 → 立刻回應
- 送出後要等別人 → 沒有按鈕 → 等到輪到你

## 多人：共用狀態 + 一把鎖 + refresh()

框架不提供伺服器。每個參與者一個 FolderUI，共用同一個遊戲物件：

```python
class Game:
    def __init__(self):
        self.lock = threading.Lock()
        self.uis = []
        ...

    def bid(self, who, amount):
        with self.lock:
            ...                          # 純邏輯；raise ValueError 代表規則不允許
        for ui in self.uis:
            ui.refresh()                 # 只是標記「需要重畫」，不會卡住
        return "✅ 已出價"

class PlayerUI(FolderUI):
    def draw(self):
        with self.game.lock:             # 直接讀共用狀態
            v = self.game.view(self.me)
        ...

    def action(self, name, args):
        return self.game.bid(self.me, int(args[0]))

game = Game()
game.uis = [PlayerUI(f"{root}/player_{i}", game, i) for i in range(n)]
for t in [ui.run(background=True) for ui in game.uis]:
    t.join()
```

每個 UI 在自己的執行緒裡畫畫面、處理按鈕；`refresh()` 是唯一需要跨執行緒呼叫的方法。

## 用程式操作

`Session` 走和按鈕腳本同一個檔案協定，給 bot、測試，或自己寫工具迴圈的程式用：

```python
from folderui import Session
s = Session("/tmp/folderui-auction-xxxx/player_0")
s.ready()
print(s.status())                        # 第一次進來還沒有回應，可以從這裡讀目前狀況
while inputs := s.inputs():              # {路徑: {hint, options}}，空的代表結束
    ok, text = s.input("bid", "40")
```

## 細節

- 腳本預設無限等待。設定 `FOLDERUI_TIMEOUT=秒數` 會在逾時後以 exit code 2 結束；
  **再執行一次同樣的指令即可繼續等待或拿到回應**（不會重複送出）。回應被讀走之前，該按鈕的腳本會保留。
- 協定檔案都是隱藏檔：`.request`、`.response`、`.done`、`.state.json`。
  不能執行腳本的 agent 也可以直接寫 `.request`（按鈕路徑與參數以 NUL 分隔）。

## 範例

```sh
uv run python examples/guess.py                     # 單人：猜數字
uv run python examples/auction.py --players 4 --rounds 3   # 多人：英式拍賣（玩家 + 觀眾）
uv run python examples/auction_bot.py <資料夾> bot0   # 拍賣用的隨機 bot
```

## 開發

```sh
uv sync
uv run pytest
```

發布：在 PyPI 設定 trusted publisher（repo `xiao-e-yun/folderui`、workflow `publish.yml`、environment `pypi`），
之後改 `pyproject.toml` 的版本號並推送 `v*` tag 就會自動發布。
