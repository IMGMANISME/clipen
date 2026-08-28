# 本機 UI／AI 通道原型

這是 P0 技術原型，**目前只提供文字工作通道，不能剪片**。
以 Python 標準函式庫實作，與未來完整 App 分開；不需要安裝第三方 Python 套件。
本次 PR 範圍是啟動、工具探索、授權／狀態與 UI 韌性；**尚未完成原 AI 自動喚醒，
不代表完整 P0-A 已通過**。
本次交付技術原型 A 的基礎部分；原 session 綁定、自動喚醒與真實模型端到端
驗收留到後續獨立 PR，不作為本次合併條件。完整 P0-A 的產品驗收要求維持不變。

介面只以桌面瀏覽器為開發與驗收目標，不提供手機版。桌面視窗縮放、分割視窗、
瀏覽器縮放與鍵盤操作仍需可用；既有窄視窗樣式只作基本容錯。

## 日常入口：直接開啟 AI CLI

使用者要的流程是：**clone → terminal cd 到 repo 根目錄 → `codex` 或 `claude`
→ 首次必要登入／信任／授權 → MCP 接入成功 → 自動開啟本機 UI**。
不要求 Clipen wrapper，也不需要另外提示 AI 開 UI。

```sh
cd clipen
codex
# 或退出後改用 claude；不要在同一 workspace 同時開兩個原型。
```

需要 Python 3.10+（PATH 的 `python3`）、Ed25519 OpenSSL、POSIX，以及已安裝
並登入的 AI CLI。依賴缺少時停止並顯示錯誤，不背景安裝或跳過檢查。

### repo 隨附的產品設定

| AI | 隨 clone 分發的設定 | 路徑與信任 |
| --- | --- | --- |
| Codex | 根目錄 `.codex/config.toml` | 支援專案設定的 CLI 在專案受信任後載入；從 repo 根目錄啟動，MCP `cwd = "."` 保持該目錄，不是相對 `.codex/` |
| Claude Code | 根目錄 `.mcp.json` | 首次由使用者批准專案 MCP；相對 args 以啟動目錄解析，所以目前限定從 repo 根目錄啟動 |

兩者都以 `python3 integrations/stdio_server.py` 接入同一個產品通道。
腳本以自身位置定位 clone，不帶作者電腦的絕對路徑；支援含空白的 clone 路徑。
設定不選模型、不含憑證、不改全域設定、不自動批准信任／MCP，也不設權限 bypass。
不依賴開發用 AGENTS／CLAUDE 文件或被忽略的開發 Skills。

開 UI 的動作位於 MCP 的 `notifications/initialized` 處理程序；不用等模型先
生成一段指令，也不需要 SessionStart hook 重複啟動。每個程序只開一次頁。
被拒絕的 MCP／不受信任專案不應繞過限制啟動。

### terminal 連結與關閉行為

服務開始後向本次控制終端輸出 `http://127.0.0.1:<port>/#<本次開頁權限>`。
這是本機 loopback 入口，不是對外網址；port 在每次啟動時由系統分配。
完整連結在本次程序期間可以重複使用，包括換一個本機瀏覽器重新開頁。
它具有本機操作權限，不要分享、截圖公開或貼進 issue；程序結束後失效，
重啟會產生新權限。自動開頁仍使用原有 60 秒一次性 bootstrap。

MCP stdout 保持純 JSON-RPC；stderr 只留不含權限的 origin。完整重開連結
只寫入本次 `/dev/tty`，沒有終端時不搜尋其他終端，也不將完整連結寫進 log。
**Codex 0.150.1 的原生 PTY 已捕捉到此輸出，但全畫面重繪可能覆蓋它；
提示持續可見尚未保證。** 沒有控制終端的宿主只能使用自動開啟的分頁。

只關分頁不停止 AI；terminal 關閉、stdio EOF、父程序消失或收到 HUP／TERM／INT
會停止服務。macOS 另偵測控制終端掛斷，即使父程序忽略 HUP 也會停止；不讀取
鍵盤輸入。已開頁面在下一次輪詢失敗後隱藏並停用工作台、停止輪詢，顯示停用
畫面（前景每秒、背景每 15 秒輪詢，單次請求逾時 5 秒；瀏覽器可能再延後）。
瞬間斷線也會保守停用，不假稱一定是 terminal 已關閉。停用畫面提供唯讀草稿
供複製，並可手動「重新檢查連線」；這只查詢本次服務，不重送任何工作。
服務已結束時須重新開 AI，使用新連結。不嘗試關閉使用者整個瀏覽器。

草稿只留分頁記憶體，不寫入 localStorage，也不保存瀏覽器憑證。離開有草稿或
未確認請求的分頁時會要求瀏覽器提示，但不能保證瀏覽器關閉／崩潰後還原草稿。

### 實測與未完成的部分

- **Claude Code 2.1.229** 的 `claude mcp get clipen` 已偵測 Project config，
  顯示 `Pending approval`；尚未替使用者批准，所以尚未記為真實自動開頁通過。
  使用者在根目錄執行 `claude` 後，需自行確認首次專案與 MCP 授權。
- **Codex CLI 0.150.1** 已偵測設定，直接啟動時 MCP 初始化成功，本機服務
  `/health` 正常，瀏覽器開頁函式回報成功；尚未目視確認使用者預設瀏覽器的
  自動開頁。先前 0.41.0 查不到 server，不能將新版能力套用到舊版。
  本次修正 `cwd = ".."` 誤指 repo 上一層的問題；設定改為 `cwd = "."`。
- **已修正 0.150.1 的 `/mcp` 重複啟動相容性，原生 CLI 實測顯示 connected／3 tools。**
  資料庫與 UI 延後到 initialized 才啟動；若 workspace 已有 writer，額外程序
  只提供工具 metadata，狀態明示 discovery_only，取件／提交一律拒絕。
  它不讀取原工作、不顯示私人連結、不開第二頁，退出不影響原服務。
- **MCP 初始化／開 UI 與模型接收工作是兩件事。** 專案設定不自動送出模型
  prompt；UI 上送出的工作仍需原 AI 呼叫取件工具。只有工具正在等待接件時才
  啟用送出。初始化回應包含有限文字通道指引，但指引不會啟動模型輪次。
  自動喚醒與兩次真實回覆仍待實作／驗收，不把「自動開頁」當成完整流程。
- 模型的登入可用性不由 MCP 握手證明；UI 仍區分通道、取件與回覆。
- 同一 repo 目前一個 writer；第二個 CLI 即使能看見清單，也不能接管或處理工作，
  尚未實作多 AI 共用服務。CLI 退出後工作留本機，下次先核對。

### 舊 launcher 僅供診斷

```sh
python3 -m prototypes.local_ui launch codex --connect-only
```

此命令曾用於舊 CLI 的暫時 MCP 設定測試，**不是使用者日常入口，不算原流程
驗收**。不帶 `--connect-only` 時會附帶文字實驗 prompt，要求處理兩筆工作或
累積 30 次閒置回覆後停止；它可能消耗模型額度／費用，提示限制不等於費用上限。
不要用它掩蓋舊版 CLI 無法讀取專案設定的問題。

### 只看本機介面

```sh
python3 -m prototypes.local_ui serve
```

診斷模式不接 AI，送出按鈕停用。terminal 顯示的是本次程序有效的完整重開連結，
可重複使用；Ctrl+C 關服務後失效。不要複製已清掉 fragment 的網址到另一個
瀏覽器期待自動登入。綁定位置固定為 `127.0.0.1`，不支援 LAN／公開部署。

## 現在實作的邊界

| 項目 | 原型行為 |
| --- | --- |
| AI 身分 | MCP 用戶端自報 name/version ＋本次 connection ID；**不是驗證過的供應商 session ID** |
| 通道就緒 | 分開顯示連線／等待接件／回覆；AI 不在取件等待中時停用送出 |
| 資料 | 只讀啟動需要的程式／已簽署系統 Skills；工作只送出 UI 明確授權的文字 |
| 系統 Skills | 後端驗證並保留內部快照；瀏覽器只收到驗證狀態，不接收原文或系統清單 |
| 剪輯計畫 Plan | AI 依內部規則、需求、毛片及範例分析產生的本次計畫；生成與編輯器尚未實作，API 回傳 plan=null |
| 持久化 | `.clipen/prototype/state.sqlite`；SQLite DELETE journal／FULL 同步，單 writer 程序鎖 |
| 重送 | request ID ＋內容指紋去重；同 ID 不同內容拒絕；修改要求預期版本 |
| 中斷 | queued／delivered 在重新啟動後標 uncertain，需先核對再明確授權重送 |
| 停止收件 | 保留工作、拒收舊 attempt 回覆；**不能保證供應商停止運算／計費** |
| 生命週期 | HTTP 與 MCP 在同一程序；terminal／AI 結束即停止 UI 服務，資料保留；不在背景常駐 |
| UI 更新 | 前景 1 秒、背景 15 秒；ETag／304；先顯示 20 筆，再按需增加；不是 SSE 回放 |
| 容量 | 最多 100 個文字工作；輸入 1,000 Unicode code points／4,000 bytes、回覆 16,000 bytes |

本機狀態目錄須由當前帳號擁有、權限 `0700`；DB／鎖檔 `0600`。
不支援網路磁碟／同步目錄，也不自動修正既有目錄權限。只部署在可信任的本機
工作目錄。拒絕已知的 symlink／hardlink 與不安全目錄，但不防同帳號惡意程序
競態替換檔案或管理員改程式，不能將原型當作完整沙箱。

API 使用精確 Host／Origin、CSRF、HttpOnly／SameSite cookie、短效 bootstrap
與本次程序有效的 terminal 重開權限；兩種權限都不出現在 API 回應或一般 log。
沒有任意檔案、shell、AI 寫入或系統 Skill 寫入 HTTP 端點。Cookie 不依 port
隔離，因此不以「localhost」取代驗證。模型仍有原本 CLI 的其他能力；原型提示
要求只用三個 Clipen 工具，但這不是針對整個 AI 宿主的權限沙箱。不要送出惡意
或敏感測試資料。

## 驗證紀錄

2026-08-28 的本機驗證：

- 85 項 Python／HTTP／stdio 合成測試與 Skills 測試通過，包含 EOF、HUP／TERM／INT、
  父程序強制結束、控制終端掛斷、DB／鎖釋放、連結重用與權限檢查。
- 10 項前端狀態回歸測試通過，涵蓋送出條件、未知回執／同筆核對、阻止連點、
  保留新草稿、304、停用／重連、授權失效、鍵盤焦點／回覆通知、分批顯示與背景節流。
  測試使用小型 DOM／fetch fixture，不冒充真實瀏覽器。
- 原生 Codex **0.150.1**：不帶 wrapper、MCP 覆寫或模型 prompt，啟動本機服務
  並呼叫瀏覽器；terminal 重開連結可用；`/mcp` 列出 3 個工具，查詢不停止原 UI。
  未送模型工作，沒有修改全域 MCP 或代為批准其他權限。
- 先前的 wrapper 測試，真實 Codex CLI **0.41.0**：暫時 MCP 設定被接受，`/mcp` 列出
  `clipen_next_work`、`clipen_complete_work`、`clipen_connection_status`。
  未送模型 prompt，未驗收模型答案或供應商 session 身分；自動開頁仍需使用者
  在自己的互動式終端確認。原型未修改其他既有 MCP 設定。
- 原先唯讀 Skills 檢視已移除，UI 改為 Plan 未實作說明，系統原文不進入瀏覽器 API。
  未連 AI／未等待取件禁止送出；真實瀏覽器已驗證兩筆合成工作與回覆、重新整理
  保存、HTML 作為純文字、斷線草稿保留與手動重連失敗提示。
  合成端明示「非 AI」，不能作為真實 AI 證據。
  先前曾檢查窄版畫面；依最新範圍，手機版不列入後續開發或驗收。
- 效能界線測試：閒置取件只查詢一次佇列，工作變更才喚醒；100 筆測試資料的
  JSON 小於 2.1 MB，相同狀態只序列化一次；前端先渲染 20 筆且保留未變更節點。
  一次本機量測：1,934,378 bytes 的首次序列化約 7.8 ms，快取讀取 p95 約
  0.0008 ms。這只是序列化微量測，不包含網路／UI／模型，不是跨平台保證。
- **尚未通過完整 P0-A**：真實模型的兩次往返、閒置 10 分鐘後取件、權限拒絕、
  供應商 session 核對，皆需後續驗證／實作。最新產品需求改為 terminal 結束即
  停止 UI，父程序退出後不再維持獨立服務。
- ASR、影音分析、Resolve、剪輯品質與完整恢復不在本原型測試範圍。

重跑：

```sh
python3 -m unittest discover -s tests -p 'test_*.py' -v
node --check prototypes/local_ui/static/app.js
node --check prototypes/local_ui/static/ui-state.js
node --test tests/test_ui.mjs
python3 -m tools.release_audit
python3 -m tools.release_audit --base origin/main
```

測試需要允許本機監聽埠；不需 AI 憑證、私人素材或簽署私鑰。Node 只用於
JavaScript 語法與狀態測試（Node 20+；本機 20.19.1），執行 UI 不需要 Node。

CI 設定涵蓋 macOS／Python 3.10、Linux／Python 3.12 與 Node 22，使用固定
action commit、唯讀權限及 10 分鐘逾時，不呼叫模型。macOS CI 明確選用 runner
已安裝的 OpenSSL 3；原生 LibreSSL 不可代替所需的 Ed25519 功能。生命週期測試
在同一時間上限內等待 HTTP 停止與資料庫鎖釋放，不假定兩者同時完成；永久鎖定
及其他資料庫錯誤仍須失敗。遠端結果以最新 PR checks 為準。
發布掃描除目前樹外，可依明確基準查待推歷史；若開發文件
已在舊 commits，刪除目前檔案仍不能算通過。掃描不自動整理歷史，也不保證偵測
所有私人內容；推送前仍須檢查實際暫存內容與全部待推 commits。

專案設定依據：[Codex 專案設定與信任](https://learn.chatgpt.com/docs/config-file/config-basic)、
[Codex MCP 設定](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)、
[Claude 專案 MCP 與批准](https://code.claude.com/docs/en/mcp)、
[Claude 相對路徑](https://code.claude.com/docs/en/debug-your-config)。

協定依據：[MCP stdio](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports)、
[MCP 初始化](https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle)、
[Codex MCP 設定](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)。
相容性仍以實測版本為準，不將最新文件的能力套用到每個舊 CLI。
