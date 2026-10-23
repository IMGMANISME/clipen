<div align="center">

# Clipen

**讓 AI 幫你剪，讓你決定怎麼剪。**

從參考風格與自己的素材出發，先確認剪輯計畫，再完成作品。

[產品架構](ARCHITECTURE.md) · [回報問題](https://github.com/IMGMANISME/clipen/issues)

</div>

---

Clipen 是正在開發中的本機 AI 剪輯工作台。使用者將能提供毛片、參考作品與需求，
修改 AI 提出的剪輯計畫，再選擇直接出片或交付 DaVinci Resolve 可編輯工程。

| 理解你的素材 | 先看計畫再開工 | 保留作品控制權 |
| --- | --- | --- |
| 分析畫面、聲音、逐字稿與參考風格。 | 調整片段、字幕、節奏、插圖與效果安排。 | 匯出成片，或在 Resolve 繼續編輯各個元素。 |

> Clipen 尚未發布可供一般使用者安裝的版本。請勿將私人素材交給未標示為正式
> release 的版本。

從 source checkout 第一次設定時執行 `./install`。完成後，先進入要存放作品資料
的目錄，再以 `clipen codex` 或 `clipen claude` 啟動本機工作台（分別使用 Codex CLI
或 Claude Code 的登入與額度）。Clipen 只會在該目錄的 `.clipen`
子目錄保存作品；毛片與參考作品仍留在使用者選取的位置。只有在使用者從 UI 確認
特定步驟後才呼叫 AI；不需要開啟 AI 對話或手動架設本機服務。
每次啟動都會先驗證該工作區內的作品；空工作區可直接進入 UI 建立第一個作品，
若作品資料損壞或無法識別則會停止啟動，且不會自行刪除或覆蓋資料。

## 選一個剪輯樣板

製作影片時，在「AI 節目製作」頁選一個剪輯樣板，再寫下這次想要的影片。樣板決定故事怎麼說、
節奏、字幕與畫面設計；你寫的需求永遠優先。

除了內建樣板，也可以在作品首頁的「我的剪輯樣板」加入別人分享或自己做的樣板，所有工作區
都能使用。樣板只包含文字說明與參考圖，Clipen 不會執行裡面的任何程式；影片開始製作後，
之後更新或移除樣板都不影響這支影片。Codex 與 Claude 讀到的樣板內容相同。

想自己做樣板，可以參考 [`examples/skills/`](examples/skills) 的範例；格式和 Claude、
Codex 使用的 Agent Skills 相同。

`./uninstall` 會移除 Clipen runtime、本機語音模型、全域指令與 PATH 設定，但會
保留作品資料、毛片、參考作品與匯出檔案。

系統設計、資料權限、中斷恢復、`clean`／`reset` 與 Resolve 交付規則請見
[產品架構基準](ARCHITECTURE.md)。

有想改善的剪輯情境，歡迎留下[問題或建議](https://github.com/IMGMANISME/clipen/issues)。
請勿附上私人素材、帳號憑證、逐字稿或本機完整路徑。
