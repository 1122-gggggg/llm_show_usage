# llm_show_usage

即時顯示 Oh My Pi（OMP）內模型與多帳號「剩餘配額」的 Rich 終端儀表板。預設只執行 `omp usage --json`，不另外掃描其他 CLI 的日誌、憑證或資料庫，也不直接查詢其他 CLI 的配額 API。不會持久化帳號憑證副本。

## 支援來源

預設只啟用 **Oh My Pi**。下表其他獨立 CLI 來源僅在明確使用 `--providers` 指定時才啟用。

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
- Oh My Pi（`omp`），並在 OMP 內登入要監控的帳號

## 安裝

一般使用者建議把 CLI 安裝在 uv 管理的隔離環境，不需要 clone 專案或安裝開發依賴：

```powershell
uv tool install git+https://github.com/1122-gggggg/llm_show_usage
```

上式會追蹤儲存庫的最新預設分支；正式 release 後，建議在網址後加上 `@<版本標籤或 commit SHA>`，以便重現安裝與回滾。

若安裝後目前的終端找不到 `llm-usage`，請執行 `uv tool update-shell`，再重新開啟終端。

## 開啟儀表板

```powershell
llu
```

互動式終端會持續更新 OMP 配額，不會要求登入其他 CLI。當標準輸出不是可互動 TTY（例如管線、重新導向或 CI）時，程式會自動只輸出一次。按 `j` / `k` 向下 / 向上捲動，按 `q` 或 `Ctrl+C` 離開；Linux / macOS 的字母按鍵需再按 Enter，Windows 不需要。

登入或新增 OMP 帳號：

```powershell
omp auth-broker login
```

`llm-usage` 舊指令仍可使用，與 `llu` 相同。

## 更新儀表板

```powershell
llu update
```

從本專案 GitHub **main 分支最新 commit** 更新儀表板。即使套件版本號未變，也會重新抓取 main；不會更新 Claude、Codex 等其他 CLI。需要 `uv`、Git 與網路連線。已安裝的 uv tool 會在原本隔離環境中更新套件，不刪除正在使用的 Python 執行檔，因此 Windows 也能同步等待更新。看到「更新完成！執行 llu 即可開啟儀表板。」後才會返回終端提示字元；失敗會顯示原因並回傳非零 exit code。

舊版尚無 `llu` 指令時，先執行一次：

```powershell
uv tool install --force --refresh git+https://github.com/1122-gggggg/llm_show_usage.git@main
```

此更新方式安裝至 uv tool 的隔離環境，不會修改開發者的 git checkout 或專案虛擬環境。

## 常用指令

只顯示一次，不開登入選單：

```powershell
llm-usage --once
```

開啟 OMP 官方登入：

```powershell
llm-usage --login
```

亦可使用下列指令開啟 OMP 官方登入：

```powershell
llm-usage --login --yes
```

預設無需指定來源；只有要額外啟用獨立 CLI 查詢時才使用：

```powershell
llm-usage --providers claude,codex,antigravity,ohmypi
```

另外保留舊的「更新所有本機 LLM CLI」功能（與 `llu update` 更新儀表板不同，需明確指定）：

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
llu [update] [--interval 10]
          [--providers ohmypi]
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

- **自適應排版**：120 欄以上使用總覽表，同時比較來源、配額與今日 / 本週統計；較窄視窗改用來源卡片，完整換行，不隱藏本週統計或截掉配額。
- **配額優先**：每個視窗先顯示剩餘百分比與 10 格進度條，再顯示帳號 / 配額名稱與本機時區的重設時間；多帳號保留各自的列。
- `CONNECTED`：有配額視窗資料的來源數 / 總來源數；不代表每個視窗都有可用百分比。
- 頂部 `LOW ≤10%`：剩餘量低於或等於 10% 的配額視窗數；`OFFLINE` 表示來源尚無配額資料。
- 來源狀態：`OK` 表示已知配額皆超過 20%；`LOW` 表示至少一個視窗剩餘量 ≤20%；`UNKNOWN` 表示仍有未取得有效百分比的視窗。低配額優先於未知狀態。
- 綠色：剩餘量 >20%；黃色：10% < 剩餘量 ≤20%；紅色：剩餘量 ≤10%。未知配額不會當成 100%。
- `今日` / `本週`：本機日誌或資料庫中可取得的 token 統計；`↑` 為輸入、`↓` 為輸出，模型明細列在所屬來源下。
- **來源提醒**：獨立警告區塊顯示登入失效、配額 API 暫時失敗或來源缺少 token 明細。
- **長畫面導覽**：互動模式的操作列固定在底部，顯示目前可見行數；`j` / `k` 每次捲動 5 行，不會因捲動重新查詢 API，定時更新保留捲動位置。`--once` 與非 TTY 輸出仍一次列出完整資料。

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

### 視窗太窄或帳號太多

窄視窗會自動改用來源卡片，文字換行但不省略配額或統計。將視窗拉寬至 120 欄以上可切換總覽表；內容超過螢幕高度時，用 `j` / `k` 捲動至其他帳號或來源提醒。Linux / macOS 請在字母按鍵後按 Enter。

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

效能與更新行為：

- JSONL 完整行直接解碼，只有跨批次或尚未寫完的行才配置累積緩衝區；保留行長限制與增量讀取行為。
- 即時模式忽略無效按鍵與空白 Enter，不會因此提前查詢配額或推遲原定更新時間。
- 標準輸入不是 TTY 時直接等待更新間隔，Windows 也不會持續輪詢鍵盤。

## 安全與限制

- 程式只讀本機 CLI 憑證與使用量資料，不會把 token 寫入專案或建立新的憑證副本；配額請求只會送往對應供應商的官方 API。
- `.gitignore` 排除虛擬環境、快取、環境變數、本機憑證檔與 OpenCode 本機資料庫。
- Claude、Codex、Grok、Copilot 的個人配額 API 並非全部都有公開穩定規格；CLI 更新後可能需要同步調整解析。
- Antigravity 使用官方 `agy -p "/usage"`，不直接讀取其 Windows 安全儲存內容。
