# llm_show_usage

即時顯示多個 AI CLI 帳號「剩餘配額」的 Rich 終端儀表板。程式會複用各 CLI 已有的本機登入狀態，不會在專案或其他位置持久化帳號憑證副本；查詢配額時，access token 只會傳送到對應供應商的官方 API，不會送往本專案或其他第三方服務。

## 支援來源

| 來源 | 配額資料 | 登入方式 |
|---|---|---|
| Claude Code | `/usage` 使用的 OAuth usage API | `claude auth login` |
| OpenAI Codex | `/status` 使用的 ChatGPT usage API | `codex login` |
| Grok Build | `/usage` 使用的 billing API | `grok login --oauth` |
| OpenCode GO | 5 小時、每週、每月 GO 配額 | `opencode providers login -p opencode` |
| GitHub Copilot | Premium requests 剩餘量 | `gh auth login` 或 OpenCode Copilot 登入 |
| Antigravity | `agy -p "/usage"` 的官方唯讀輸出 | 由 `agy -p "/usage"` 開啟官方驗證 |
| Oh My Pi | `omp usage --json`：omp 內各模型額度 | `omp auth-broker login`（支援多帳號） |

配額欄顯示的一律是「剩餘百分比」，不是已使用百分比。Oh My Pi 列（`OMP ...`）直接複用 omp 自己的額度快取：同一個來源有多個帳號時，每個帳號各佔一組配額列並以帳號前綴區分（例如 `alice·Gemini 週`），不用切換 CLI 就能一次看完；`agy` 本體只支援單一帳號，多帳號請加在 omp。預設每 10 秒更新一次；短暫網路錯誤時會保留上一筆成功資料並顯示警告。

## 需求

- Python 3.11 或更新版本
- [uv](https://docs.astral.sh/uv/)
- 只需安裝你要監控的 AI CLI；未安裝的來源不影響其他來源

## 安裝

一般使用者建議把 CLI 安裝在 uv 管理的隔離環境，不需要 clone 專案或安裝開發依賴：

```powershell
uv tool install git+https://github.com/1122-gggggg/llm_show_usage
```

上式會追蹤儲存庫的最新預設分支；正式 release 後，建議在網址後加上 `@<版本標籤或 commit SHA>`，以便重現安裝與回滾。

若安裝後目前的終端找不到 `llm-usage`，請執行 `uv tool update-shell`，再重新開啟終端。

## 開啟儀表板

```powershell
llm-usage
```

互動式終端預設會持續更新並顯示登入選單。當標準輸出不是可互動 TTY（例如管線、重新導向或 CI）時，程式會自動只輸出一次，不會啟動持續更新畫面。

互動模式會先顯示來源狀態：

```text
SOURCE LOGIN
  ○ Claude           未登入
  ○ Codex            未登入
  ● Grok             已登入，自動抓取
  ● OpenCode GO      已登入，自動抓取
  ● GitHub Copilot   已登入，自動抓取
  ● Antigravity      已登入，自動抓取

  [1] Claude
  [2] Codex
  選擇要登入的來源 (1,3 / a 全選 / Enter 略過):
```

- 已登入來源會直接抓取，不需要再次選擇。
- 輸入 `1,3` 可同時登入多個缺少的來源。
- 輸入 `a` 登入全部缺少的來源。
- 按 Enter 略過登入。
- 進入即時儀表板後仍可隨時按 `l` + Enter 重新開啟此選單登入缺少的來源，按 `q` + Enter 或 `Ctrl+C` 離開。

## 常用指令

只顯示一次，不開登入選單：

```powershell
llm-usage --once
```

強制開啟登入選單：

```powershell
llm-usage --login
```

自動登入所有缺少來源：

```powershell
llm-usage --login --yes
```

只顯示指定來源：

```powershell
llm-usage --providers claude,codex,antigravity,ohmypi
```

一鍵更新所有本機 LLM CLI（各用官方更新指令，未安裝的自動略過）：

```powershell
llm-usage --update-check
llm-usage --update
```

`--update` 會平行執行 `claude update`、`codex update`、`grok update`、`opencode upgrade`、`agy update`、`omp update` 與 `gh extension upgrade --all`；`gh` 本體請用系統套件管理器更新。任一失敗時 exit code 為 1。

自訂更新間隔，例如 30 秒：

```powershell
llm-usage --interval 30
```

`--interval` 接受 `0.1` 至 `86400` 秒（含端點）；超出範圍或非有限數值會直接顯示參數錯誤。

相較舊版，`--interval` 現在只接受 `0.1` 至 `86400` 秒，使用更短或更長週期的腳本需先調整；未知的 `--providers` 名稱會以 exit code 2 明確報錯；管線與重新導向也改為單次快照，避免背景程序意外常駐。若腳本需要週期資料，請由排程器重複執行 `llm-usage --once`。

完整參數：

```text
llm-usage [--interval 10]
          [--providers claude,codex,grok,opencode,copilot,antigravity,ohmypi]
          [--once] [--login] [--yes]
          [--update] [--update-check]
          [--claude-dir PATH] [--codex-dir PATH]
          [--grok-dir PATH] [--opencode-db PATH]
          [--opencode-auth PATH]
```

## 各來源登入說明

### Claude Code

```powershell
claude auth login
```

登入後可在 Claude Code 內用 `/usage` 交叉確認。若 access token 過期，啟動選單會重新列為未登入。

### OpenAI Codex

```powershell
codex login
```

程式顯示的配額來自 Codex `/status` 使用的同一份 usage 資料。過期 JWT 會自動判定為未登入。

### Grok Build

```powershell
grok login --oauth
```

登入後可在 Grok CLI 內用 `/usage` 交叉確認。

### OpenCode GO

```powershell
opencode providers login -p opencode
```

程式會顯示 GO 的 5 小時、每週與每月剩餘量，並繼續從本機 SQLite 顯示 token 使用統計。
使用 `--opencode-db` 讀取其他 profile 的資料庫時，為避免混用帳號，線上配額預設停用；若確定屬於同一 profile，請同時傳入其 `--opencode-auth PATH`。

### GitHub Copilot

```powershell
gh auth login
```

如果 OpenCode 已保存 `github-copilot` 登入，程式也會直接複用該 session。

### Antigravity

程式使用官方唯讀命令：

```powershell
agy -p "/usage"
```

此命令不會啟動 agent turn。未登入時會進入官方驗證；已登入時直接回傳 Gemini 與 Claude/GPT 的 5 小時和每週剩餘配額。程式固定從使用者主目錄執行，避免出現專案「信任資料夾」提示。

`agy` 本體只支援單一帳號。若要同時觀測多個 Antigravity 帳號，請把帳號加進 Oh My Pi：

```powershell
omp auth-broker login google-antigravity
```

加完後 `OMP Antigravity` 列會把每個帳號的剩餘配額並排顯示，不用切換 CLI。

### Oh My Pi

```powershell
omp auth-broker login
```

不指定來源會進入互動式選擇；也可直接指定例如 `anthropic`、`openai-codex`、`xai-oauth`、`opencode-go`、`google-antigravity`。同一來源可重複登入多個帳號，儀表板會全部顯示。

## 畫面說明

- `CONNECTED`：目前成功取得配額的來源數量。
- `LOW`：剩餘量低於或等於 10% 的配額視窗數。
- 綠色：剩餘量充足。
- 黃色：剩餘量偏低。
- 紅色：即將耗盡或已耗盡。
- `今日` / `本週`：本機日誌或資料庫中可取得的 token 統計。
- 頁尾警告：登入失效、配額 API 暫時失敗或來源缺少 token 明細。

## 疑難排解

### 顯示「請先在該 CLI login」

執行對應來源的登入指令，或重新執行：

```powershell
llm-usage --login
```

### Claude 或 Codex 憑證檔存在，但仍顯示未登入

程式會檢查 token 到期時間。重新登入即可：

```powershell
claude auth login
codex login
```

### Antigravity 顯示未登入

先確認以下命令能輸出配額：

```powershell
agy -p "/usage"
```

若無 `agy` 指令，請先安裝 Antigravity CLI。

### 終端欄位被截斷

將 Windows Terminal 視窗拉寬。窄終端會優先保留來源與剩餘配額欄位。

## 開發與測試

```powershell
git clone https://github.com/1122-gggggg/llm_show_usage.git
cd llm_show_usage
uv sync --locked
uv run --locked pytest -q
uv run --locked ruff check src tests
uv run --locked python -m compileall -q src tests
uv build
```

目前測試涵蓋配額解析、10 秒快取、短暫失敗 fallback、登入選單、過期 token、Antigravity CLI 輸出、OpenCode SQLite 與 TUI 剩餘量顯示。

## 安全與限制

- 程式只讀本機 CLI 憑證與使用量資料，不會把 token 寫入專案或建立新的憑證副本；配額請求只會送往對應供應商的官方 API。
- `.gitignore` 排除虛擬環境、快取、環境變數、本機憑證檔與 OpenCode 本機資料庫。
- Claude、Codex、Grok、Copilot 的個人配額 API 並非全部都有公開穩定規格；CLI 更新後可能需要同步調整解析。
- Antigravity 使用官方 `agy -p "/usage"`，不直接讀取其 Windows 安全儲存內容。
