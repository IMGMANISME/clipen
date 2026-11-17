<img width="1440" height="810" alt="Clipen 的九種剪輯樣板" src=".github/assets/templates.jpg" />

# Clipen

讓 AI 幫你剪，讓你決定怎麼剪。選一個剪輯樣板、交出自己的素材，Clipen 用你已經在用的 Claude Code 或 Codex 製作影片；看片時在畫面上記下想改的地方，AI 就照著改。

一切都在你的 Mac 上：素材不上傳，語音辨識在本機跑。

```bash
npx @clipen/clipen-cli
cd 要存放作品的資料夾
clipen claude        # 或 clipen codex
```

[產品架構](ARCHITECTURE.md) · [回報問題](https://github.com/IMGMANISME/clipen/issues) · [npm](https://www.npmjs.com/package/@clipen/clipen-cli)

## 剪輯樣板

製作影片時先選一個樣板，再寫下這次想要的影片。樣板決定故事怎麼說、節奏、字幕與畫面設計；你寫的需求永遠優先。

內建九種：旅遊 vlog、美食探店、日常 vlog、產品評測、活動紀錄、短影音精華、產品開箱、旅遊精華與產品 demo。

- **加入自己的樣板。** 在作品首頁的「我的剪輯樣板」加入別人分享或自己做的樣板，所有工作區都能用。
- **只有文字和參考圖。** Clipen 不會執行樣板裡的任何程式；影片開始製作後，更新或移除樣板都不影響這支影片。
- **格式和 Agent Skills 相同。** 可以從 [`examples/skills/`](examples/skills) 的範例開始改。

## 審片注記

邊看影片邊在時間點上留注記：要修改、拿掉、這樣可以、加強爆點、地點補充、聲音或其他。注記可以指向整個鏡頭，也可以指向某個字幕或畫面元件，送出後 AI 會逐筆處理。

- **每次修改都有說明。** AI 改完要交代改了什麼、為什麼，並逐筆回覆每則注記是否處理。
- **不會隨便留下「未處理」。** 只有需要你決定或提供、或 Clipen 確實做不到的事才能留下，而且會帶到下一次修改。
- **版本隨時可回去。** 每次 AI 製作、手動修改和還原都會留下一個版本，可以看、可以下載。

## 時間線與元件

字卡、地點標示和其他畫面元件都在時間線上，可以直接拖曳、拉長，或移到另一軌疊在上面。

- **元件外觀** 是一組字型與配色，AI 做的每個元件都要套用其中一組，風格才會一致。
- **內附 13 套字型**，包含中文、日文手寫與標題字體，在任何一台 Mac 上看起來都一樣。
- **手動修改** 文字、時間與外觀後，AI 下一次修改會保留你的調整。

## 其他

- 本機語音辨識產生逐字稿與字幕，素材不離開你的電腦
- 指定自己的配樂，AI 會依歌曲長度安排段落與淡入淡出
- 下載這一版影片（MP4）或 Clipen 的剪輯資料，也可以匯出到 DaVinci Resolve 繼續剪
- 作品存在該資料夾的 `.clipen` 子目錄，毛片留在原本的位置
- 只有在你從畫面上確認後才呼叫 AI，使用 Claude Code 或 Codex 自己的登入與額度

## 安裝

需要 Apple 晶片（M 系列）的 Mac、[Node.js](https://nodejs.org) 18 以上，以及已登入的 [Claude Code](https://claude.com/claude-code) 或 [Codex CLI](https://github.com/openai/codex)。

`npx @clipen/clipen-cli` 會在沒有 Python 3.10 以上時下載一份固定版本的 Python，不動系統 Python，也不需要 sudo。程式放在 `~/Library/Application Support/Clipen`，字型從原始來源下載並核對檢查碼，接著安裝本機語音辨識模型（約 3 GB）與全域指令 `clipen`。沒有 Google Chrome 時會改裝 Chromium；沒有 ffmpeg 時會提示安裝方式（`brew install ffmpeg`）。

| 指令 | 做什麼 |
| --- | --- |
| `clipen claude`／`clipen codex` | 在目前資料夾啟動工作台 |
| `npx @clipen/clipen-cli@latest` | 更新到最新版，作品資料保留 |
| `clipen uninstall` | 移除程式、語音模型與指令，保留作品、毛片與匯出檔案 |

每次啟動都會先檢查這個資料夾裡的作品；作品資料損壞或無法辨識時會停止啟動，不會自行刪除或覆蓋。

## 開發

| 路徑 | 內容 |
| --- | --- |
| [`src/clipen`](src/clipen) | Python 後端：本機服務、素材分析、AI 製作工具與算圖 |
| [`apps/web`](apps/web) | 工作台介面（React + Vite） |
| [`bin/clipen.js`](bin/clipen.js) | `npx` 安裝器：準備 Python、程式與字型 |
| [`examples/skills`](examples/skills) | 自訂剪輯樣板的範例 |
| [`tests`](tests) | Python 與 npm 套件測試 |

```bash
./install                                   # 從原始碼安裝
PYTHONPATH=src:tests/python .venv/bin/python -m unittest discover -s tests/python
cd apps/web && npm run build && npm run lint && npm test
npm run release -- patch                    # 準備新版本：改版本號、重建、測試、發布試跑
```

系統設計、資料權限、中斷恢復與交付規則見 [產品架構基準](ARCHITECTURE.md)。

有想改善的剪輯情境，歡迎留下[問題或建議](https://github.com/IMGMANISME/clipen/issues)。請勿附上私人素材、帳號憑證、逐字稿或本機完整路徑。

## 授權

MIT © [GMAN](https://github.com/IMGMANISME)

內附字型各自採用 SIL Open Font License，授權全文與來源在 [`src/clipen/rendering/fonts/`](src/clipen/rendering/fonts)。
