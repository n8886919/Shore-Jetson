# AI 開發與日常流程

這是個人維護的 Shore Jetson 專案。目標是讓 AI 能從目前程式與文件接手，
完成一個可驗證的小改動。平常流程是：**說明結果 → 看相關程式 → 實作 → 驗證 → 回報**。
一個任務通常只需一段需求、一份程式差異和驗證結果。

## 文件怎麼用

| 文件 | 用途與更新時機 |
| --- | --- |
| [README](../README.md) | 專案目標、使用者看見的行為；功能改變時更新 |
| [AGENTS.md](../AGENTS.md) | AI 的固定工作規則、驗證入口及操作邊界；出現值得長期保留的教訓才修改 |
| [環境準備](project-setup.md) | 安裝、執行與本機／私有資料的分界；環境步驟改變時更新 |
| 本文件 | 日常流程、任務範本與參考來源 |
| [UI 規格](ui.md) | 目前 1024×600 螢幕、版面限制、選定的 UI skill 與畫面驗收方式 |
| [架構](architecture.md)及各主題文件 | 修改相應模組時按需閱讀；行為或介面改變時同步更新 |
| 本機 `STATUS.md`（若有） | 跨對話未完事項、實機部署與驗收紀錄；保留日期、證據與下一步，Git 忽略 |

固定規則只放一處，其他文件引用即可。不需要每次改字都讀完整專案文件，
也不需要先建立 PRD、ADR、Sprint、任務資料庫或一套 AI 框架。
目前以單一 AI 配合使用者即可；沒有多代理、MCP 或自動化服務的必要依賴。

## 一次開發任務

1. **定義完成條件。** 寫清楚目前問題、希望的結果、不能改動的地方，以及如何驗收。
   小修正可直接在對話說明；需求不明時先釐清會影響結果的部分。
2. **確認現況。** AI 先確認工作目錄、分支與 `git status --short --branch`，看相關 diff、
   程式與測試。工作目錄可能比 Git HEAD 新，保留既有修改和未追蹤檔案。
3. **完成最小改動。** 沿用現有 Python／原生 JavaScript 結構，先補能重現問題的測試，
   再修正行為。純文件、文案或外觀調整按影響選擇驗證，不為湊數新增測試。
   已授權的本機開發持續做到驗證完成；不必每一步重新確認。
4. **驗證並自查。** 開發中跑相關測試，完成時檢查受影響的流程與差異。
   看錯誤路徑、舊設定相容性、資料保留與硬體保護是否受到影響。
5. **交付可接續的結果。** 回報改動、實際執行的檢查及未驗證事項；更新受影響文件。
   跨對話的大任務才在本機 `STATUS.md` 留下剩餘工作。提交、推送與部署依使用者指示執行。

新功能若跨多個模組或預計跨對話，先列 3–5 個可驗證步驟；需要保存時才新增一份
`docs/plans/<任務名稱>.md`，包含目標、範圍、驗收、進度、重要決定與下一步即可。
敏感的裝置資料仍放本機紀錄。不要為每個小修正建立計畫文件。

## 程式與測試入口

以下是導覽，實際行為以當前程式與測試為準；看到文件互相矛盾時標明差異，
涉及實機參數時重新核對目標，不從歷史筆記猜值。

| 要改什麼 | 程式入口 | 優先驗證 |
| --- | --- | --- |
| 設定、啟停與協調 | `shore_tracker/config.py`、`runtime.py`、`coordinator.py` | `tests/test_config.py`、`test_runtime.py`、`test_coordinator.py` |
| 辨識、鎖定與狀態轉移 | `shore_tracker/inference/` | `tests/test_inference_state_machine.py`、`test_inference_tracker.py`、`test_tracking_grace.py`、`test_detection_confirmation.py` |
| 影格、顯示與 UI | `shore_tracker/display.py`、`preview.py`、`web/` | `tests/test_display_frames.py`、`test_web.py` 及三個 Node 測試 |
| 相機管線／TensorRT | `shore_tracker/camera/`、`inference/tensorrt_runtime.py` | `tests/test_camera_pipeline.py`、`test_decoded_frames.py`、`test_tensorrt_runtime.py`；實機另驗 |
| 現行 PWM 雙軸控制 | `shore_tracker/control/pan.py`、`shore_tracker/coordinator.py`、`shore_tracker/servo/simple.py`、`shore_tracker/servo/simple_service.py`、`shore_tracker/servo/pulse_controller.py` | `tests/test_pan_planner.py`、`test_servo_simple.py`、`test_pulse_controller.py`、`test_tilt_control.py`；先讀[現行契約](servo-controller.md)，歷史校準不可直接沿用 |
| 歷史 RS485 相容路徑 | `shore_tracker/control/velocity.py`、`shore_tracker/servo/pelco_client.py`、`coordinator.py`；手動 `servo/pelco_d.py`、`web/static/pan-tilt.js`、`web/server.py` | `tests/test_rs485_tracking.py`、`test_pan_tilt_settings.py`、`test_pelco_d.py`、`tests/test_pelco_controls.js`；[歷史 RS485 操作](pelco-d-test.md)不適用現行 PWM |
| 錄影與保存 | `shore_tracker/recording_triggers.py`、`manual_recording.py`、`storage_service.py` | `tests/test_recording_triggers.py`、`test_manual_recording.py`、`test_storage_service.py` |
| 安裝／Nano | `deploy/`、`config/jetson-nano.toml` | [commissioning](commissioning.md)、[Nano 文件](jetson-nano.md)，按實際目標另做部署驗證 |

表內同列省略的路徑前綴沿用該列第一個完整路徑。只改相關部分，不順手重構整個模組。

## 本機驗證

先依[環境準備](project-setup.md)建立開發環境，從專案根目錄執行。
使用 `.venv` 時先 `source .venv/bin/activate`。

```sh
# 迭代時：只跑本次相關測試，例如設定與協調器
python3 -m pytest -q tests/test_config.py tests/test_coordinator.py

# Python 行為變更完成後
python3 -m pytest -q

# UI 互動或 API／狀態契約變更時
node tests/test_dashboard_controls.js
node tests/test_kiosk_controls.js
node tests/test_control_page.js
node tests/test_pelco_controls.js
node tests/test_servo_test_controls.js

# 檢查差異與空白錯誤；另檢查 git status 中的新檔
git diff --check
git diff --stat
```

純文件修改檢查連結、路徑、指令及差異即可。現在沒有額外的 lint/type-check
指令，也不需要為了這份流程另裝工具。測試失敗先區分本次回歸、既有問題與環境缺件；
只修本次範圍，不用刪掉斷言或放寬條件掩蓋失敗。跳過的測試要列出原因。

畫面調整先讀 [UI 規格](ui.md)，使用其中選定的 `frontend-design` 指引處理視覺改版。
執行 `python3 deploy/preview-web.py`，開啟 `http://127.0.0.1:18092/kiosk?layout=nano`，
以 **1024×600 viewport** 檢查畫面，結束按 Ctrl+C。
檢查控制面板開關、固定底列及狀態文案；Node 測試不能代替實際畫面檢查。
這個預覽拒絕控制指令。`serve` 會啟動正式 runtime，不能作為離線預覽使用。
`doctor` 只檢查靜態前置條件；沒有 GPU、相機或模型的主機回報缺件是合理結果。

## 實機工作與完成判準

開發／測試不需要 Shore-Spotter checkout 或韌體。上傳到私人 Drive 是另外的選用整合，
所需 receiver 見[環境準備](project-setup.md)。

相機探索、直連網路、ONVIF、RTSP 或 PTZ 工作需先完整讀取
`.agents/skills/yl-a85-camera/SKILL.md`。它屬於本機資料，新 checkout 可能沒有；
缺少時先取得這份指引，相應工作暫停，其他本機開發可繼續。
網路變更、實體動作、安裝與部署等邊界以 [AGENTS.md](../AGENTS.md) 為準。
部署前依 [commissioning](commissioning.md)核對目標、設定、待安裝版本與可回復版本。

| 能宣稱的結果 | 必須具備的證據 |
| --- | --- |
| 本機開發完成 | 符合需求、相關測試／畫面檢查結果、差異自查與文件同步 |
| 已部署 | 正確目標上的安裝版本與服務狀態，並核對實際使用的設定 |
| 實機流程正常 | 當次相機、辨識、錄影等所需流程的觀察與時間範圍 |
| 實體驗收完成 | 使用者或量測確認的動作、角度／負載等具體範圍 |

不要把測試通過說成已部署，也不要把 UART 寫入成功說成馬達到位。
短測 FPS、設定 FPS、海面辨識準確率及長時間穩定度分開描述。

## 可直接貼給 AI 的任務

一般實作：

```text
請依 AGENTS.md 處理這個小改動。
目標：……
目前行為／重現方式：……
完成時應該：……（可觀察的結果）
限制：保留現有 WIP；本次只改本機程式，不部署或操作硬體。
請完成實作、相關驗證與文件同步，回報結果和未驗證事項。
```

先討論：

```text
先只讀分析，不修改、不 build、不部署：……
請找出相關程式、可能原因及最小修正方式，說明怎麼驗證。
```

接續未完工作：

```text
請依 AGENTS.md 和目前工作目錄接續：……
若有 STATUS.md，先區分歷史實機結果與待辦；確認 diff 後完成本次範圍。
不要把未提交內容還原，也不要把過去的實機授權沿用到新動作。
```

交付只需簡短列出「改了什麼／驗證結果／仍待處理」，附必要的檔案或指令。
不必另產生每日報告、完整對話備份或重複的待辦清單。

## 參考來源與採用取捨

以下於 **2026-10-30** 查閱。採用方式是針對本專案的選擇，不是來源要求每個專案照做；
公開範例中的安裝、提交或部署指令不會擴大本專案的授權。

| 來源 | 本專案採用的部分 |
| --- | --- |
| [AGENTS.md 開源格式](https://github.com/agentsmd/agents.md/blob/main/README.md) | 用根目錄單一入口提供 AI 專案規則、命令和導覽；保持工具可攜性 |
| [OpenAI：AGENTS.md 載入方式](https://learn.chatgpt.com/docs/agent-configuration/agents-md) | 將共用指引隨原始碼保存；目前沒有需要額外分層的子目錄規則。其他 AI 工具若不自動載入，明確要求先讀此檔 |
| [OpenAI：Codex best practices](https://learn.chatgpt.com/guides/best-practices) | 任務提供目標、背景、限制及完成條件；實作後驗證與檢查差異 |
| [OpenAI：精簡 skills 與 prompts](https://developers.openai.com/blog/rethinking-skills-and-prompts-for-gpt-6-astra) | 只讀任務需要的文件，固定規則保持精簡；本文借用文件設計原則，不指定模型 |
| [GitHub Spec Kit](https://github.com/github/spec-kit/blob/main/README.md) | 借用需求、計畫、工作項目及驗證的順序；目前用對話和現有文件即可，不安裝 CLI 或產生整套範本 |
| [OpenAI：ExecPlans](https://developers.openai.com/cookbook/articles/codex_exec_plans) | 僅在大型／跨對話任務保存可接續的計畫；該文章已標為 archived，只作流程參考，不沿用模型或命令建議 |

先用這套流程實際開發；若同一錯誤或重複操作持續出現，再補一條規則或一個小工具。
