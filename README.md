<div align="center">

<img src=".github/assets/logo.svg" width="72" alt="Clipen" />

# Clipen

**讓 AI 幫你剪，讓你決定怎麼剪。**

選一個剪輯樣板、交出自己的素材，Clipen 用你已經在用的 Claude Code 或 Codex 剪出影片；<br />
看片時在時間線上留下注記，AI 就照著改。素材不上傳，全部在你的 Mac 上完成。

[![npm](https://img.shields.io/npm/v/@clipen/clipen-cli?color=e9b44c&label=npm)](https://www.npmjs.com/package/@clipen/clipen-cli) [![License: MIT](https://img.shields.io/badge/license-MIT-e9b44c)](LICENSE) ![macOS Apple silicon](https://img.shields.io/badge/macOS-Apple%20silicon-1f2026) ![Claude Code or Codex](https://img.shields.io/badge/AI-Claude%20Code%20%7C%20Codex-1f2026)

<br />

<img src=".github/assets/workbench.jpg" alt="Clipen 工作台：預覽畫面、審片注記與多軌時間線" />

</div>

## 開始使用

```bash
npx @clipen/clipen-cli          # 安裝或更新
cd 要存放作品的資料夾
clipen claude                   # 或 clipen codex
```

瀏覽器會打開工作台：建立作品、指定毛片資料夾、選一個樣板，按「開始製作」。

## 功能

### 🎬 剪輯樣板

樣板決定故事怎麼說、節奏、字幕與畫面設計；你寫下的需求永遠優先。內建九種：旅遊 vlog、美食探店、日常 vlog、產品評測、活動紀錄、短影音精華、產品開箱、旅遊精華與產品 demo。

<img src=".github/assets/templates.jpg" alt="Clipen 內建的九種剪輯樣板" />

也可以加入別人分享或自己做的樣板。樣板只有文字和參考圖，格式和 Agent Skills 相同，可以從 [`examples/skills`](examples/skills) 開始改。

### 📝 審片注記

邊看邊在時間點留注記：修改、拿掉、這樣可以、加強爆點、地點補充、聲音。注記可以指向整個鏡頭，也可以指向某個字幕或元件，送出後 AI 會逐筆處理並回覆。

只有需要你決定、或 Clipen 確實做不到的事才會留下「未處理」，而且會帶到下一次修改。

### 🎞 時間線與元件

字卡、地點標示和畫面元件都在多軌時間線上，可以直接拖曳、拉長或疊到另一軌。內附 13 套中日文字型，AI 做的每個元件都套用同一組外觀；你手動改過的地方，AI 下次修改會保留。

### 🔒 留在你的電腦上

- 語音辨識在本機執行，逐字稿與字幕不離開你的 Mac
- 只有你在畫面上確認後才呼叫 AI，用的是 Claude Code 或 Codex 自己的登入與額度
- 每次製作與修改都留下版本，可以回到任何一版
- 下載 MP4、Clipen 剪輯資料，或匯出到 DaVinci Resolve 繼續剪

## 安裝需求

- Apple 晶片（M 系列）的 Mac
- [Node.js](https://nodejs.org) 18 以上
- 已登入的 [Claude Code](https://claude.com/claude-code) 或 [Codex CLI](https://github.com/openai/codex)

安裝程式會在需要時下載固定版本的 Python，不動系統 Python、不需要 sudo，並安裝本機語音辨識模型（約 3 GB）。沒有 ffmpeg 時會提示 `brew install ffmpeg`。

| 指令 | 做什麼 |
| --- | --- |
| `clipen claude`／`clipen codex` | 在目前資料夾啟動工作台 |
| `npx @clipen/clipen-cli@latest` | 更新到最新版，作品資料保留 |
| `clipen uninstall` | 移除程式、模型與指令，保留作品、毛片與匯出檔 |

作品存在資料夾裡的 `.clipen` 子目錄，毛片留在原本的位置。

## 開發

```bash
./clipen install                            # 從原始碼安裝
PYTHONPATH=src:tests/python .venv/bin/python -m unittest discover -s tests/python
cd apps/web && npm run lint && npm test
npm run release -- patch                    # 準備新版本
```

後端在 [`src/clipen`](src/clipen)（Python），工作台介面在 [`apps/web`](apps/web)（React + Vite），`npx` 安裝器在 [`bin/clipen.js`](bin/clipen.js)。系統設計見 [產品架構](ARCHITECTURE.md)。

歡迎留下[問題或建議](https://github.com/IMGMANISME/clipen/issues)，請勿附上私人素材、帳號憑證或本機完整路徑。

## 授權

[MIT](LICENSE) © [GMAN](https://github.com/IMGMANISME)。內附字型採用 SIL Open Font License，見 [`src/clipen/rendering/fonts`](src/clipen/rendering/fonts)。
