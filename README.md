<div align="center">

# Clipen

**讓 AI 幫你剪，讓你決定怎麼剪。**

從剪輯樣板與自己的素材出發，讓 AI 製作影片，再透過審片注記調整作品。

[產品架構](ARCHITECTURE.md) · [MIT 授權](LICENSE) · [回報問題](https://github.com/IMGMANISME/clipen/issues)

</div>

---

Clipen v1.0.0 是本機 AI 剪輯工作台。選擇剪輯樣板、提供素材與需求，使用 Codex
或 Claude 製作影片，再透過影片時間點與畫面元素記下修改要求，下載完成的影片。

| 理解你的素材 | 樣板製作與審片修改 | 保留作品控制權 |
| --- | --- | --- |
| 分析畫面、聲音、逐字稿與參考風格。 | 選擇樣板與視覺風格，透過注記調整片段、字幕與聲音。 | 查看歷史版本、下載成片與剪輯資料。 |

尚未提供獨立桌面安裝包。DaVinci Resolve 工程匯出尚未提供；目前可下載 Clipen 的剪輯資料。

## 安裝

需要 Apple 晶片（M 系列）的 Mac、[Node.js](https://nodejs.org) 18 以上，以及已登入的
[Claude Code](https://claude.com/claude-code) 或 [Codex CLI](https://github.com/openai/codex)。

```bash
npx @imgmanisme/clipen
```

沒有 Python 3.10 以上時，Clipen 會下載一份固定版本的 Python，不會動到系統 Python，也不需要
sudo。程式放在 `~/Library/Application Support/Clipen`，字型從原始來源下載並核對檢查碼，接著
安裝本機語音辨識模型（約 3 GB）與全域指令 `clipen`。沒有 Google Chrome 時會改裝 Chromium；
沒有 ffmpeg 時會提示安裝方式（`brew install ffmpeg`）。

安裝完成後開一個新的終端機視窗，進入要存放作品資料的目錄，再以 `clipen claude` 或
`clipen codex` 啟動本機工作台（分別使用 Claude Code 或 Codex CLI 的登入與額度）。
之後要更新，再執行一次 `npx @imgmanisme/clipen@latest`。

從 source checkout 安裝時，改為執行 `./install`，其餘相同。

## 使用

Clipen 只會在該目錄的 `.clipen`
子目錄保存作品；毛片與參考作品仍留在使用者選取的位置。只有在使用者從 UI 確認
特定步驟後才呼叫 AI；不需要開啟 AI 對話或手動架設本機服務。
每次啟動都會先驗證該工作區內的作品；空工作區可直接進入 UI 建立第一個作品，
若作品資料損壞或無法識別則會停止啟動，且不會自行刪除或覆蓋資料。

只有版本號、介面或一般程式碼更新時，可直接沿用已安裝的執行環境。依賴套件或安裝設定
變更、執行環境缺損時，啟動器才會提示重新安裝（`npx @imgmanisme/clipen` 或 `./install`）；既有作品資料會保留。

## 選一個剪輯樣板

製作影片時，在「AI 節目製作」頁選一個剪輯樣板，再寫下這次想要的影片。樣板決定故事怎麼說、
節奏、字幕與畫面設計；你寫的需求永遠優先。

除了內建樣板，也可以在作品首頁的「我的剪輯樣板」加入別人分享或自己做的樣板，所有工作區
都能使用。樣板只包含文字說明與參考圖，Clipen 不會執行裡面的任何程式；影片開始製作後，
之後更新或移除樣板都不影響這支影片。Codex 與 Claude 讀到的樣板內容相同。

想自己做樣板，可以參考 [`examples/skills/`](examples/skills) 的範例；格式和 Claude、
Codex 使用的 Agent Skills 相同。

`clipen uninstall`（source checkout 為 `./uninstall`）會移除 Clipen runtime、本機語音模型、全域指令與 PATH 設定，但會
保留作品資料、毛片、參考作品與匯出檔案。

系統設計、資料權限、中斷恢復、`clean`／`reset` 與 Resolve 交付規則請見
[產品架構基準](ARCHITECTURE.md)。

有想改善的剪輯情境，歡迎留下[問題或建議](https://github.com/IMGMANISME/clipen/issues)。
請勿附上私人素材、帳號憑證、逐字稿或本機完整路徑。

## 授權

Clipen 以 [MIT 授權](LICENSE)開源。內附字型各自採用 SIL Open Font License，授權全文放在
[`src/clipen/rendering/fonts/`](src/clipen/rendering/fonts)，來源記錄在同一目錄的 `manifest.json`。

