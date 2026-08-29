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

Clipen 的目標入口是 `clipen codex`。它會啟動本機工作台，只有在使用者從 UI
確認特定步驟後才呼叫 AI；不需要開啟 AI 對話或手動架設本機服務。

系統設計、資料權限、中斷恢復、`clean`／`reset` 與 Resolve 交付規則請見
[產品架構基準](ARCHITECTURE.md)。

有想改善的剪輯情境，歡迎留下[問題或建議](https://github.com/IMGMANISME/clipen/issues)。
請勿附上私人素材、帳號憑證、逐字稿或本機完整路徑。
