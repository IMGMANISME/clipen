# LLM 主導的製作工具層

這份文件盤點目前可執行的 52 個 MCP 工具。LLM 決定取樣、剪輯、呈現、聲音及修改順序；
Clipen 負責來源邊界、時間計算、參數檢查、本機執行、不可變版本及工作預算。
不再要求為了完成階段而加入最低數量的字卡或特效。

## 工具盤點

| 任務 | 可呼叫工具 | 實際結果 |
| --- | --- | --- |
| 理解任務與能力 | `get_edit_status`, `list_editing_tools` | 使用者確認的敘事意圖、工具用途、支援限制及剩餘預算 |
| 參考已認可方向 | `get_program_reference` | 工作開始時固定的使用者基準：故事與字幕範例；AI 無法自行設定認可 |
| 發掘對話事件 | `search_source_dialogue` | 檢索目前全部可用原片口語、相鄰句、來源時間與草稿狀態；預設排除基準原片，搜尋結果仍需看畫面確認 |
| 檢索素材 | `search_media`, `list_source_media`, `register_source_range` | 分頁檢索候選與完整原片清單；可在先前分析視窗以外建立新候選，不能越出已授權來源 |
| 預覽畫布 | `set_canvas` | 640×360、1280×720、1920×1080 的 SDR 樣片畫布；變更後重新審片 |
| 重做敘事規劃 | `record_story_plan`, `read_story_plan`, `plan_episode` | 保存主題、觀眾問題、體驗與回應，逐鏡連到來源口語；不冒稱使用者已確認模型安排 |
| 檢視證據 | `inspect_clip`, `sample_source`, `inspect_candidates`, `inspect_cut` | 既有證據與 LLM 自選原片時間點的實際取樣圖、來源時間 |
| 讀取剪輯 | `inspect_sequence` | 目前不可變時間軸版本與素材範圍 |
| 選材與敘事 | `replace_sequence`, `insert_clip`, `remove_item`, `reorder_clips` | 真正更動片段與順序的時間軸版本 |
| 精剪 | `trim_clip`, `split_clip`, `replace_clip_source` | 原片範圍內的精確裁切、分割與重新計算的時間軸 |
| 口語與字幕 | `list_transcripts`, `replace_subtitles` | 完整口語的來源對應、可編輯描邊字幕與重點台詞；明確 opt-in 才能採用標示待核對的 ASR 草稿，不把截斷句子當完整發言 |
| 字卡與文字設計 | `add_title`, `add_clip_title`, `edit_text`, `configure_text_style` | 文字、時間、大小、位置、換行寬度、對齊、前景與背景 RGBA；字幕與字卡分軌 |
| 畫面動態 | `set_clip_effect`, `remove_clip_effect` | 推近、緩推、重新構圖、變速的語意效果與參數 |
| 顏色與轉場 | `set_color_grade`, `set_transition`, `set_fade` | 每鏡對比、飽和度、亮度與剪點黑／白淡轉 |
| 遮罩與隱私 | `set_mosaic` | 依時間啟閉的多個矩形像素化區域；支援分段指定位置，未提供自動追蹤 |
| 原音 | `set_source_audio` | 保留、靜音、人聲處理及音量 |
| 配樂取得 | `search_music` | 公開曲目搜尋、下載、量測，保存作者、原始來源與授權；只外送一般音樂詞 |
| 音訊證據 | `list_audio_assets`, `inspect_audio` | 已取得候選、來源、音量、峰值與節拍量測；不宣稱聆聽 |
| 環境聲與音效 | `extract_audio` | 從來源指定範圍擷取 PCM 音訊，保留來源與權利未知註記 |
| 配樂與音效混音 | `place_audio`, `remove_audio` | 多層局部音訊、來源偏移、時間、音量、淡入淡出及原音側鏈壓低 |
| 輸出與回看 | `render_preview`, `inspect_preview`, `cancel_preview` | 實際 MP4、完整解碼與影音時長檢查、自選時間點的成片畫面、取消 |
| 逐鏡審片 | `review_cut` | 逐鏡取樣覆蓋、逐字卡事實核對、具體保留／待修判斷；待修問題阻止完成，修改後須重新審片。LLM 自評，不冒稱人工或獨立驗收 |
| 技術回饋 | `validate_sequence` | 時間、文字碰撞、來源音訊、樣式、HDR 限制、預覽新鮮度；不等於美感驗收 |
| 歷史與復原 | `get_edit_history`, `undo_revision`, `restore_revision` | 不可變版本、工具參數、失敗代碼與可還原歷史 |
| 可編輯交付 | `export_edit` | 原生時間軸 JSON、自訂文字樣式及本版使用音訊的來源授權記錄 |
| 完成 | `finish_edit` | 凍結已實際輸出且取樣檢視過的當前版本；舊版本預覽不能替新版背書 |

## 執行協定

- MCP 是持續的工具工作流程，不是先填滿一整包效果 JSON 再一次輸出。
- 工具 schema 與實作皆接上 `EditAgentTools.call`，並在 Codex 的精確允許清單中註冊。
- 每次寫入形成不可變 revision；失敗回傳可修正的錯誤，已付費的同一次 LLM 工作可接著決策。
- 重新剪輯會明確清除舊時間對應的文字、效果與音訊放置，回傳 `invalidated`。
  這是保守的第一版策略；LLM 必須依新剪輯重套，不能靜默沿用錯誤時間。
- 相同 revision 的預覽會重用；修改後回看舊預覽會得到 `stale: true`。
- 本機預覽與音訊擷取不額外啟動 LLM。預設每次工作 96 次工具操作、4 次預覽，
  個別工具另有有界輸入；輸出失敗仍計入本機預覽次數。
- 重啟不會自動重送付費工作。未完成預覽由既有恢復流程標成中斷。
- 既有固定兩次 AI 的 finishing 流程保留作實驗紀錄，UI 的主要入口改用工具型 agent。
- MCP 單次工具逾時設為 600 秒，讓有界配樂下載可以完成；預覽使用背景 handle，
  回看最多等待 30 秒，避免模型連續產生大量輪詢。
  設定欄位依據 [官方設定參考](https://developers.openai.com/codex/config-reference)。

## 支援邊界與未完成部分

這是現有「素材分析與計畫已準備完成 → 短樣片」的工具化實作，**不是整個產品已全自動化**。
前置的目錄授權、模型安裝、素材掃描／ASR／視覺證據建立及計畫確認仍沿用原本工作流程，
尚未全部改成由這個 agent 發起的工具。工具可以重新檢索完整已掃描原片、自訂候選與敘事計畫並自行取樣，
不能把未掃描、未授權的素材宣稱已分析。

目前輸出仍受既有短樣片範圍限制：30–75 秒、1–30 個主畫面鏡頭、單鏡最長 30 秒（完整口語可保留較長鏡頭，不再強制塞滿八鏡）。
樣片的時長／鏡頭限制是現有交付規格，不是聲稱 LLM 只能處理短片。
尚未提供完整長片、多機同步、多畫面合成、物件自動追蹤、生成影音／配音、
可感知音樂的模型輸入、HDR mastering 或驗證過的 Resolve 專案交付。

字幕預設使用已審核 ASR 的完整來源句；可明確允許保留來源與待核對狀態的草稿，草稿不能改寫原話。
成片回看目前是時間點取樣，不代表模型完整觀看連續動作或聽過成片。
未配置的能力會在 `list_editing_tools` 明列，不應用空工具或成功假資料掩蓋。

## 驗證

```sh
PYTHONPATH=tests/python .venv/bin/python -m unittest test_production_tools
PYTHONPATH=tests/python .venv/bin/python -m unittest discover -s tests/python
cd apps/web && npm run build && npm run lint
```

工具整合測試不呼叫 LLM：以真實 FFmpeg 執行取樣、擷取音訊、馬賽克、調色、轉場、
原音靜音、局部混音、輸出、成片回看及版本新鮮度檢查。
另外覆蓋自訂文字的實際光柵輸出、失敗重試、預覽預算、取消及來源授權資料保存。
這些證明工具能執行，不證明 LLM 已達到使用者期望的剪輯品質。

## 2026-10-19 節目方向

新的 UI 工作帶入使用者的節目方向：停用獨立字卡，以對話、期待、體驗與回應組成單一事件。
字幕支援一般口語、重點台詞、安靜段落三種呈現，標點分頁與動畫使用固定換行，避免字級跳動導致孤字。
詳見 [PROGRAM_FORMAT.md](PROGRAM_FORMAT.md)。目前實際樣片由這次對話透過同一工具層製作；尚不代表自主 agent 已達成相同品質。

## 2026-10-17 實際失敗案例修正

上一版把單張成片截圖視為已審完整版本，且缺少擴回完整口語與真正片尾淡出的操作。
現在必須涵蓋每鏡首、中、末影格及所有文字中點，再保存相同 revision 的逐鏡判斷。
`validate_sequence.render_review` 會列出尚未看過的時間；`inspect_cut` 可批次取樣三鏡。
有 revise 鏡頭、文字 matches=false、或修改後只有舊預覽時不能完成。這些只是可稽核的
檢視證據及模型判斷，不能保證美感、文字辨識或音訊已正確。

`list_transcripts` 對截斷句回傳完整文字與 complete_source_span，交給
`replace_clip_source` 擴回原片，而非為了通過驗證一律刪掉口語。`add_clip_title`
把字卡限制在該鏡並保存模型引用的依據，仍須在成片核對實際招牌／字幕。
`set_transition` 是進入下一鏡的黑／白轉場；`set_fade` 的 fade_out_frames
才是該鏡尾部淡出，尤其用於片尾。

剪輯參考：[Adobe B-roll](https://www.adobe.com/creativecloud/video/discover/b-roll.html)
說明環境／細節鏡頭應補充事件意義與銜接，而不是取代主要互動；
[B-Script](https://arxiv.org/abs/1902.11216) 示範以逐字稿與敘事上下文支援 B-roll 選擇。
這些原則轉為可觀察、可修改的工具，不以特效數量或自填分數判定品質。

逐字稿 status=needs_review 的資料不再被當作無口語而消失；工具會明確回傳
provisional_text 與來源時間，仍不把低信心 ASR 冒充已核對字幕。
配樂詞彙加入 japanese／citypop／jazz 等風格；search_music 只回傳當次搜尋候選，
另列舊曲目 ID，避免搜尋新風格時總是選到舊清單第一首。公開曲庫逾時或沒有
符合授權的候選會回報失敗；既有曲目可重用，但不能宣稱已聽過或已符合指定風格。
