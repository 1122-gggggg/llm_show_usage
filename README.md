# llm_show_usage

即時顯示多個 AI CLI 帳號「剩餘配額」的 Rich 終端儀表板。程式會複用各 CLI 已有的本機登入狀態，不會儲存或上傳帳號憑證。

## 支援來源

| 來源 | 配額資料 | 登入方式 |
|---|---|---|
| Claude Code | `/usage` 使用的 OAuth usage API | `claude auth login` |
| OpenAI Codex | `/status` 使用的 ChatGPT usage API | `codex login` |
| Grok Build | `/usage` 使用的 billing API | `grok login --oauth` |
| OpenCode GO | 5 小時、每週、每月 GO 配額 | `opencode providers login -p opencode` |
| GitHub Copilot | Premium requests 剩餘量 | `gh auth login` 或 OpenCode Copilot 登入 |
| Antigravity | `agy -p "/usage"` 的官方唯讀輸出 | 由 `agy -p "/usage"` 開啟官方驗證 |

配額欄顯示的是「剩餘百分比」，不是已使用百分比。預設每 10 秒更新一次；短暫網路錯誤時會保留上一筆成功資料並顯示警告。

## 需求

- Python 3.11 或更新版本
- [uv](https://docs.astral.sh/uv/)
- 只需安裝你要監控的 AI CLI；未安裝的來源不影響其他來源

## 安裝

```powershell
git clone https://github.com/1122-gggggg/llm_show_usage.git
cd llm_show_usage
uv sync
```

## 開啟儀表板

```powershell
uv run llm-usage
```

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
- 按 `Ctrl+C` 離開即時儀表板。

## 常用指令

只顯示一次，不開登入選單：

```powershell
uv run llm-usage --once
```

強制開啟登入選單：

```powershell
uv run llm-usage --login
```

自動登入所有缺少來源：

```powershell
uv run llm-usage --login --yes
```

只顯示指定來源：

```powershell
uv run llm-usage --providers claude,codex,antigravity
```

自訂更新間隔，例如 30 秒：

```powershell
uv run llm-usage --interval 30
```

完整參數：

```text
llm-usage [--interval 10]
          [--providers claude,codex,grok,opencode,copilot,antigravity]
          [--once] [--login] [--yes]
          [--claude-dir PATH] [--codex-dir PATH]
          [--grok-dir PATH] [--opencode-db PATH]
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
uv run llm-usage --login
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
uv sync
uv run pytest -q
uv run --with ruff ruff check src tests
uv run python -m compileall -q src tests
```

目前測試涵蓋配額解析、10 秒快取、短暫失敗 fallback、登入選單、過期 token、Antigravity CLI 輸出、OpenCode SQLite 與 TUI 剩餘量顯示。

## 安全與限制

- 程式只讀本機 CLI 憑證與使用量資料，不會把 token 寫入專案。
- `.gitignore` 排除虛擬環境、快取、環境變數與本機憑證檔。
- Claude、Codex、Grok、Copilot 的個人配額 API 並非全部都有公開穩定規格；CLI 更新後可能需要同步調整解析。
- Antigravity 使用官方 `agy -p "/usage"`，不直接讀取其 Windows 安全儲存內容。
