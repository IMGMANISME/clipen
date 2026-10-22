# 自主剪輯流程重構交接

## 這份 PR 的定位

保留目前可執行的工具、版本與預覽工作，供後續重構比較；不是成片品質驗收。
使用者在 2026-10-20 回報：字幕時間不對、聽不到音樂、畫面缺少節目元素。
這些問題仍未解決，不應因為工具操作或測試通過而把分支當作已達到使用者要求。

## 2026-10-22 程式現況更新

以下依 `main`（含 a5b421a、e7ec41a）的程式與測試整理。「已實作並有測試」只代表程式行為；
真實素材的成片仍需使用者觀看與聆聽驗收，2026-10-20 回報的三個問題尚未經使用者確認已解決。

| 項目 | 現況 | 依據 | 尚未確認 |
| --- | --- | --- | --- |
| A. 解除舊計畫／時間軸依賴 | 已實作。製作工作只需完成素材檢查與至少一段影片，從空白時間軸開始；狀態與檢索工具在製作模式改讀本次需求，不經 `_confirmed_input` | `agent_repository.create_run`、`skill_tools.SourceContext`；`test_starts_without_preparation_plan_or_timeline_and_freezes_skill`、`test_authenticated_production_entry_uses_inventory_without_old_plan` | 舊流程（`timeline_required`、`_confirmed_input`）保留作舊作品相容 |
| B. 字幕對齊 | 已整合本機 forced aligner（`Qwen3-ForcedAligner-0.6B-8bit`，固定 revision 與檔案雜湊）。字幕記錄 `timing_basis`；未對齊的仍標為按字數估時並列入 `unaligned_item_ids` | `alignment.py`、`program_subtitles.py`；`test_on_demand_speech_alignment_preserves_pauses_and_excerpt_timing` 等 | 真實素材的起訖誤差（設計目標 p95 ≤ 200 ms）尚未量測 |
| B. 配樂與混音 | 只在對齊後的人聲區間壓低配樂 7 dB（attack 0.12 s／release 0.4 s）；分別輸出 camera、music、mix stem 並量測，配樂幾乎無聲時回報 `music_effectively_silent` | `program_audio.py`；`test_actual_music_stem_ducks_only_during_aligned_speech_and_reaches_final_mix` | 沒有對齊區間時不壓低；聽感與「聽得到」仍需人工聆聽 |
| C. 節目畫面 | 有 11 類畫面元件與多種動態，實際渲染並檢查版面；裁切、重排、undo 會重新對應字幕與圖形，只把換來源後不安全的效果標為待審 | `components.json`、`recut.py`；`test_graphics_follow_reorder_trim_and_undo_without_clearing_other_treatments`、`test_all_new_families_animate_in_actual_video_and_exit` | 是否達到使用者要的節目感尚未驗收 |
| 片長 | 預設 60 秒樣片；使用者可指定 30–1200 秒，超過 75 秒時放寬鏡頭、文字與效果上限 | `production_limits.py`、`test_explicit_long_form_session_accepts_ten_minutes_without_relaxing_default` | 長片的全片連續性與渲染成本尚未實測 |

下方「2026-10-20 回報時的現況」保留為當時紀錄，其中架構依賴與 forced aligner 未整合兩點已不再成立。

## 2026-10-20 回報時的現況

- 字幕：上游 Qwen3-ASR worker 以整個語音視窗作為 phrase/word 範圍；`program_subtitles.py`
  對摘錄及分頁再按字數比例估時。這不是語音對齊，短句可以停留十多秒，亦可能與實際發言錯位。
- 配樂：最新成功工作確實執行選曲與 place_audio，設定 gain=-14 dB、duck=true。
  使用者仍聽不到音樂，代表「時間軸有音樂層」不足以判定成功。尚未完成成片與音樂 stem 的響度／波形核對；
  現行 sidechain 以整段現場聲觸發、可能持續壓低的假設需量測，不應冒稱已修好。
- 畫面：最新工作只有一個 8 格淡白剪點轉場及片尾淡出，未加入其他節目畫面處理。
  描邊字幕與這些基本效果不足以達成使用者所說的節目感。
- 舊流程仍是啟動前提：`create_run` 需要 current_timeline；`_confirmed_input` 需要已確認計畫，
  而來源選擇與狀態工具仍會經過該條件。新增原片搜尋並沒有解除這個架構依賴。

## 新流程的邊界

1. 使用者提供素材權限、剪輯意圖、參考與預算後即可啟動 agent。
   不再要求先完成整批分析、填計畫、確認計畫、建立舊時間軸。
2. 掃描保留為可驗證的來源登錄：檔案身份、串流、時間基準與存取範圍。
   ASR、對齊、視覺取樣等證據按模型需要建立，可快取，不能當固定必經關卡。
3. 使用者意圖與模型工作筆記分開保存。工作筆記可隨選材、試剪與回看修改，
   不需要把模型自己的敘事計畫升格為使用者確認。
4. LLM 直接調用看素材、查對話、取樣、剪接、字幕、音訊及畫面工具。
   工具回傳實際證據與限制；局部素材不足時擴查來源，而非完成表單即視為完成分析。
5. 本機負責不可變來源、時間計算、執行、權利紀錄、預算、版本與復原。
   試剪 → 輸出 → 檢視畫面及音訊證據 → 修改，形成主要迴圈。

## 建議拆分的實作順序

### A. 解除舊計畫與時間軸依賴

- 新的 agent session 可從來源登錄與意圖建立，初始時間軸可以為空。
- 移除 get_edit_status、來源檢索等工具對 confirmed plan 的間接依賴。
- 保留舊作品的讀取、預覽、匯出及復原；不刪除原有計畫或覆寫使用者資料。
- 驗證重點：沒有舊分析／計畫的新作品也能開始取證與剪輯，取消／重啟仍可恢復。

### B. 按需證據與時間對齊

- 來源 ASR 與語音對齊為不同產物；字幕摘錄／分頁必須映射到實際詞或字的時間。
- 每份證據帶來源版本、範圍、模型版本與不確定狀態，來源或修訂改變時失效。
- 音訊驗證包含 camera、music、final mix 的實際量測，不能以放置音樂層代替聽感驗證。
- 強制對齊已於 2026-10-20 之後接入製作流程（見上方現況更新）；真實素材的字幕同步仍待驗收。

### C. 可表達節目設計的工具與成片驗收

- 依內容配置畫面重點、構圖動態、來源對應的指示／標記、轉場與聲音進退。
  不把「有幾個效果」當作節目品質指標，也不以獨立字卡代替故事。
- 將設計決策落到具體時間與畫面錨點，讓模型能檢視結果並修改。
- 驗證實際字幕同步、配樂可辨識且不蓋人聲、視覺元素確實出現、事件敘事成立。
  技術測試與模型自評都不等於使用者驗收。

完整長片仍是後續目標；預設為 30–75 秒 SDR 樣片，可指定至 1200 秒，但長片流程尚未經實際驗收。
