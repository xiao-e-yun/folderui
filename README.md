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
⏳ 已送出，等待中…              ← 其他人還在出價，腳本卡住
✅ 已出價 40
開標：bob 5、carol 15、你 40
🔨 你以 40 得標 …
🔔 bob 拍賣「月光琉璃盞」…

── 可以做的事 ──
./bid.sh  <金額 1-60>  密封出價
./pass.sh  不出價
```

## 寫一個 UI

```python
from folderui import FolderUI

class Guess(FolderUI):
    def draw(self):                      # 每一幀重新宣告畫面，不可修改狀態
        self.file("rules.md", "猜 1-100")
        self.button("guess", "<數字>")
        self.reply(f"已經猜了 {len(self.tries)} 次")

    def action(self, name, args):        # 按鈕被按下：回傳文字，或 raise ValueError 代表失敗
        n = int(args[0])
        ...
        return "太大"

Guess("/tmp/my-ui").run()
```

| 方法 | 在哪裡呼叫 | 作用 |
|---|---|---|
| `file(path, content)` | `draw()` | 宣告一個檔案；沒宣告的會自動刪除 |
| `button(path, hint, options=None)` | `draw()` | 宣告一個按鈕 → `<path>.sh`，路徑可以分層（`players/2/kick`） |
| `reply(text)` | `draw()` | 腳本回應時印出的目前狀況 |
| `notify(text)` | `action()` / `receive()` | 累積一則消息，下一次回應時送出 |
| `close()` | `action()` / `receive()` | 結束：移除按鈕、回應等待中的腳本 |
| `push(msg)` | 其他執行緒 | 送訊息給這個 UI，會呼叫 `receive(msg)` |

**回應規則只有一條**：操作處理完之後，第一個「有按鈕可按」的畫面出現時（或 `close()`）才回應。

- 輸入錯誤 → 狀態沒變、按鈕還在 → 立刻回應（`raise` 的訊息，exit code 1）
- 查詢類操作 → 按鈕還在 → 立刻回應
- 送出後要等別人 → 沒有按鈕 → 等到輪到你

## 多人：1 個伺服器 + N 個客戶端

```python
class MyServer(Server):
    def handle(self, who, cmd):          # 純邏輯；raise ValueError 代表規則不允許
        ...
        for p in self.clients:
            self.push(p, Update(...))     # 推播給客戶端
        return "✅ 已出價"

class PlayerUI(FolderUI):
    def action(self, name, args):
        return self.server.call(self.me, ("bid", int(args[0])))   # 同步呼叫，例外會傳回來
    def receive(self, update):
        self.view = update.view          # 伺服器推播 → 框架自動重畫

server = MyServer()
for i in range(n):
    server.connect(i, PlayerUI(f"{root}/player_{i}", server, i))
server.start()
for ui in server.clients.values():
    ui.start()
```

框架不管多人同步：伺服器在一個執行緒依序處理請求，推播走無上限佇列（伺服器永遠不等客戶端）。

## 程式型 agent

```python
from folderui import Session
s = Session("/tmp/folderui-auction-xxxx/player_0")
s.ready()
while buttons := s.buttons():            # {路徑: {hint, options}}，空的代表結束
    ok, text = s.press("bid", "40")
```

## 細節

- 腳本預設無限等待。設定 `FOLDERUI_TIMEOUT=秒數` 會在逾時後以 exit code 2 結束；
  **再執行一次同樣的指令即可繼續等待或拿到回應**（不會重複送出）。回應被讀走之前，該按鈕的腳本會保留。
- 協定檔案都是隱藏檔：`.request`、`.response`、`.done`、`.buttons.json`。
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
