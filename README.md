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

## 用 Skill 決定怎麼剪

Skill 是交給 AI 的剪輯指引：故事怎麼說、節奏、字幕、畫面元件與聲音。製作影片時，
在「AI 節目製作」頁選一套 Skill，再寫下這次的需求；你寫的需求永遠優先。

- **內建 Skill**：一套「人物與體驗旅遊節目」題材，以及 12 套各有畫面風格的節目模板。
- **自訂 Skill**：在作品首頁的「Skill 庫」匯入自己的 Skill 資料夾，所有工作區都能使用。
  格式和 Claude、Codex 使用的 Agent Skills 相同：

  ```text
  my-skill/
  ├── SKILL.md         必填，開頭的 frontmatter 寫 name 與 description
  ├── references/*.md  選填，AI 需要時才讀的細節
  ├── assets/*.png     選填，風格參考圖
  └── clipen.json      選填，選用 Clipen 內建的畫面風格、字體、元件與預設片長，
                       或用 extends 延伸一套內建 Skill
  ```

  [`examples/skills/`](examples/skills) 有兩套範例：只用三種元件的「單人口播」，
  以及延伸「今天吃哪邊」的「美食開箱日記」。

AI 每次都會先讀 Clipen 內建的製作核心，再讀你選的 Skill。核心的邊界不會被 Skill
改變：AI 只能使用 Clipen 提供的剪輯工具，不能捏造素材裡沒有的事實。Clipen 只讀取
Skill 裡的文字與 PNG，資料夾裡的程式不會執行；製作開始時會凍結當下的 Skill，之後
更新或移除不影響已開始的作品。Codex 與 Claude 讀到的 Skill 完全相同。

`./uninstall` 會移除 Clipen runtime、本機語音模型、全域指令與 PATH 設定，但會
保留作品資料、毛片、參考作品與匯出檔案。

系統設計、資料權限、中斷恢復、`clean`／`reset` 與 Resolve 交付規則請見
[產品架構基準](ARCHITECTURE.md)。

有想改善的剪輯情境，歡迎留下[問題或建議](https://github.com/IMGMANISME/clipen/issues)。
請勿附上私人素材、帳號憑證、逐字稿或本機完整路徑。
