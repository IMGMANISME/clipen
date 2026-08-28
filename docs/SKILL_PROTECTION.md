# 系統 Skills 保護

狀態：已建立獨立保護工具、簽署目前 5 個系統 Skills 並提供本機唯讀設定。
這不是完整 App；目前後端透過 SkillStore 驗證系統內容，但不將原文、系統清單
或內部指令送到 UI／瀏覽器 API。使用者可編輯的產品項目改為本次剪輯 Plan，
生成、編輯與執行尚未實作。後續入口必須使用同一個 SkillStore，不能直接
讀寫檔案後宣稱受保護。

## 現在的保護

- **明確身分**：使用 `system:clipen-core`、`user:my-style` 等識別。
  系統來源與角色取自已簽署的清單；使用者檔案一律是普通使用者文字資料，
  即使標頭聲稱 `origin: system` 或 `role: core` 也不會升級。
- **禁止系統寫入**：SkillStore 的 save／delete 拒絕所有 `system:` 操作，
  不提供一般使用者更新系統 Skill 的入口。這也適用於核心 Skill。
- **驗證後讀取**：每次載入系統 Skill 都先驗證整個套件的 Ed25519 簽章、
  本版清單指紋、檔案清單、大小與 SHA-256。新增、缺失或被修改的檔案都拒絕。
  回傳的是此次驗證過的內容，不是讓呼叫端重新開啟的路徑。
- **使用者資料隔離**：使用者 Skills 只保存於已被 Git 忽略的
  `.clipen/user-skills/`；檔名由受限識別產生，不接受任意寫入路徑。
- **介面分離**：系統原文不放入狀態回應、前端 JavaScript、隱藏 DOM 或靜態下載。
  不提供系統原文另存副本。Plan 是依需求與素材產生的創作安排，不是系統原文。
- **安全檔案操作**：檢查擁有者及目錄寫入權限，拒絕路徑越界、符號連結、
  硬連結與非一般檔案；以目錄描述符定位，使用原子替換避免沿連結寫入其他檔案。
- **本機唯讀**：`lock-system` 先驗證套件，再將系統 Skill 檔案設為 `0444`、
  系統目錄設為 `0555`，最後重新驗證。這是防誤改，並非對管理員的安全保證。

驗證失敗不會自動改寫、重新簽署或重下載檔案；先保留現況並回報錯誤。
維護者確認原因後才透過受信任的發行內容恢復。

## 維護者與內部 AI 工具的使用方式

目前小工具使用 Python 3.10 以上、支援 Ed25519 的 OpenSSL，以及 POSIX 的
no-follow／dir-fd 檔案操作。不需安裝 Python 套件；在本機 macOS 驗證，
尚未驗證 Linux，Windows 目前不支援。不會因平台或 OpenSSL 不支援而跳過檢查。
這是此保護工具的實作選擇，不代表整個 App 已選定 Python 架構。

在專案根目錄執行：

```sh
python3 -m tools.skill_guard verify
python3 -m tools.skill_guard list
python3 -m tools.skill_guard read system:clipen-core
python3 -m tools.skill_guard lock-system
```

既有使用者文字儲存原型的建立、修改及刪除由同一入口處理（不是 Plan API）：

```sh
python3 -m tools.skill_guard save user:my-style --file /path/to/my-style.md
python3 -m tools.skill_guard read user:my-style
python3 -m tools.skill_guard delete user:my-style
```

讀取回傳 JSON 中的 `id`、`role`、`editable` 是工具判定的身分；`content`
內部的同名標頭不具授權效果。儲存工具只限制來源、大小與存取方式，不驗證
計畫品質，也不能保證模型遵循指令。它不提供 plan revision、素材證據綁定或
使用者確認；不能把寫入成功當作計畫已批准，且不支援直接複製系統原文的產品流程。

這些命令不啟動 UI、不處理影片，也不產生 AI 或外部服務費用。

## 隨附的驗證資料

系統套件包含 `skills/system-manifest.json`、`skills/system-manifest.sig` 與
`tools/skill_trust.py` 中的公鑰／版本指紋；它們是產品驗證所需的公開資料。
簽章私鑰不隨產品發布。一般使用者不透過重新簽署來修復驗證失敗，應先停止
使用有問題的系統套件，再依受信任的版本更新流程處理。

工作階段應使用同一次驗證的內容快照；新版載入前重新驗證。
一般 Git checkout 不會完整保留唯讀權限，新安裝時需重新執行唯讀設定。

## 能力邊界

此方案假設驗證程式、信任公鑰／指紋、Python／OpenSSL 與作業系統沒有被替換。
它能限制正常 API 操作，並讓未被修改的載入器拒絕遭竄改的系統 Skills；
不能阻止擁有本機管理權限的人修改驗證程式、換掉信任資料，或直接讓自己的
AI 讀其他指令。也不把同一帳號下的惡意程序、任意目錄搬移或記憶體竄改列為
已防禦的情境。明文 Skills 不是加密的商業機密保護。

後端只讀取已驗證的系統快照，UI 不接收此快照。後續 AI 執行核心需直接使用
SkillStore 的身分與內容，不信任前端傳入的唯讀標記或
未驗證的系統文字；發布前也要驗證整合。未實作遠端授權服務或完整更新／撤銷系統。

## 驗證依據

自動測試涵蓋正常讀寫、修改系統內容與清單、無效簽章、缺少工具、來源偽造、
越界識別、連結繞過、寫入時替換成連結、原子寫入失敗與唯讀設定。
測試只使用暫存目錄，不讀取保留影片、不呼叫 AI，也不使用發行私鑰。

實作參考 [OpenSSL 的 Ed25519 簽章操作](https://docs.openssl.org/3.6/man1/openssl-pkeyutl/)
與 [Python 的檔案描述符操作](https://docs.python.org/3.10/library/os.html)。
本方案不是 TUF 的完整實作，不宣稱具備完整的金鑰撤銷、更新防重放或遠端信任管理。
