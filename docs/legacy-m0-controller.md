# GXServo 與 M0 控制板歷史資料

使用者於 **2026-11-14** 再次確認現行硬體已換回 **CH340G USB-TTL → M0／M1
控制板 → 兩顆 180° PWM servo**，RS485 雲台已收起。現行契約與設定待核對事項見
[控制契約](servo-controller.md)。保留此檔名以維持既有連結。

本頁保存 **2026-10-30／31** 的 GXServo、接線、通道、方向與小幅動作證據。
以下「本專案」「目前」均指當時設備；重用相同通訊路徑不代表這些校準仍適用。
特別是 `#01` 水平／`#00` 俯仰、方向 -1、90° 中立與 85–95° 俯仰範圍，
均不得當作本次重新組裝的已驗收值。

此頁的 ASCII 脈寬、0–180°／中立 90°、通道、方向、回中與動作測試指令
不適用 RS485 雲台；不得把舊 USB-TTL 換成 Moxa 後直接沿用。
`m0_pulse` 是目前原始碼可用的 PWM 路徑；具名 M0 設定檔與下方舊上線流程
仍有歷史裝置／參數假設，不可原樣當作目前 CH340G 的部署流程。

## 2026 年 10 月控制板紀錄


本專案獨立維護，不依賴 Shore-Spotter 原始碼、韌體或版本。2026-10-30 依
使用者要求，移入使用者先前提供的控制板說明與四張原始圖片。相同控制板
不代表使用相同馬達；本專案馬達再次確認為 **GXServo 42 kg／180°**。

## 本專案接線與控制

- Jetson Nano → CH340G USB-TTL；TX/RX 交叉接線並共地，使用者於 2026-10-30 確認。
- **2026-10-31 現場實測修正：`#01`＝水平、`#00`＝俯仰**。先前 OUT1／OUT2
  文字對照與現場不符，以單通道實測為準。脈寬增加時 `#00` 朝上、`#01` 朝左，
  使用者確認兩軸均回中、無異常；兩軸方向均設為 -1。
- 設定為 `pan_channel=1`、`tilt_channel=0`；兩軸通道必須不同，只接受 0／1。
  俯仰由設定頁「啟用俯仰」開關加入自動跟拍。
- 9600 baud、8N1；水平 ASCII `#01Pxxxx!`、俯仰 `#00Pxxxx!`，不加換行。
- 0° → 500 μs、90° → 1500 μs、180° → 2500 μs。
- 開機 DISARMED；開啟串口不送中位或舊目標。
- 主機預設 50 Hz 更新；250 ms 無新控制目標停止送出，這不是控制板 watchdog。
- Nano 暫定要求角速度上限 5°/s，搜尋 2°/s；這是命令軌跡上限，非量測軸速。
- 加速度暫定 10°/s²（靜止加速到上限約 0.5 秒）。辨識約 2 Hz 只更新目標；
  獨立控制迴路每 20 ms 計算平滑角度，不等待下一張辨識幀。已回報遺失則保持位置，
  1.5 秒沒有有效辨識結果則 DISARM，另有 250 ms 控制迴路停擺保護。
- 無已確認的 ACK、軸角回授或控制板逾時停機；停止送出不等於切斷電源。

### 自動俯仰

`servo.tilt_enabled` 預設 false；`tilt_direction` 預設 0（未校準）。必須使用
`m0_pulse` 協定且方向校準為 ±1，UI 才允許啟用；開關本身不會 ARM。
`tilt_min_deg=85`、`tilt_max_deg=95`、`tilt_home_deg=90` 為初始小範圍，
不是已驗收的全機械行程。啟用前應確認實際軸在已知中立；本接法無角度讀回。

畫面 y 向下增加：脈寬增加若讓鏡頭朝上，`tilt_direction=-1`；若朝下則為 +1。
只採用現場小角度測試確認的結果。俯仰與水平共用容許比例、靈敏度及速度／加速度限制，
找不到目標時俯仰保持最後要求角度，不自動回中。

兩軸以同一 UART writer、同一新鮮目標期限送出；啟用時每次寫入包含 00／01，
關閉時在 writer lock 內清除待送俯仰通道（本機為 00），不再刷新俯仰目標、不回中。STOP、重新 ARM、
傳輸錯誤或 250 ms 無新目標均丟棄兩軸待送值，禁止重播舊俯仰命令。
設定頁切換後會讀回 `tuning.tilt_enabled`；線條依已套用狀態繪製。

## 文件規格

| 項目 | 原始說明圖片所載規格 |
| --- | --- |
| 通道 | M0／M1，串口可各別控制 |
| 模式 | S1 切手動／回中；手動模式接受串口位置命令 |
| 控制來源 | 旋鈕轉動切回旋鈕；收到有效串口命令切到串口 |
| UART | 9600 baud；宣稱兼容 3.3 V／5 V 輸入；本專案使用 8N1 |
| 指令 | `#<ch>P<time>!`；ch 為 `00`／`01`，time 為四位微秒數 `0500`～`2500` |
| 範例 | `#00P1500!`：M0 中位；`#01P1000!`：M1 1000 μs |
| PWM | 50 Hz、週期 20 ms、脈寬 500～2500 μs |
| 脈寬解析度 | 宣稱 1 μs；不代表實際角度精度 |
| 控制板供電 | 5～9 V；實際電壓必須符合已安裝馬達額定值 |
| 尺寸／重量 | 47.2×29.8 mm／11 g，依圖片 |
| ACK／回授 | 圖片未定義查詢指令或回覆格式 |

控制板與 Servo 使用獨立且符合馬達額定值的電源。8.4 V 是另一組 Youmi
馬達的設定，不沿用於本專案；也不能接入 USB-TTL 邏輯電源端。
串口控制時保持手動燈亮、旋鈕不動。控制板 TX 的存在不證明有角度回授。

本專案 500～2500 μs 端點、中位、轉向與負載仍須在本機構上驗收。
原資料中 Youmi 359°、179.5°中位及其他專案實機結果不適用此馬達。

## 回饋能力查證（2026-10-30）

原始說明僅定義 `#<ch>P<time>!` 位置指令，沒有查詢指令、ACK 或回覆格式。
目前不能從 UART 確認收件、模式、旋鈕值、PWM 值、供電、電流、溫度或堵轉。
這是「沒有已確認的可用介面」，不是已證明韌體永遠不可能回傳。

使用者確認 TX/RX 交叉並共地、控制板處於中立模式。Nano 以 9600 8N1
被動監聽 20.036 秒：TX 0 bytes、RX 0 bytes。未送位置或猜測查詢命令；
結果只表示這段中立模式觀察未見主動回報，不能排除手動模式 ACK。

同日使用者切換到 PWM／手動模式後，再以相同接線與 9600 8N1
被動監聽 20.071 秒：TX 0 bytes、RX 0 bytes。應用程式暫停以釋放串口，
監聽結束後恢復；沒有送出位置或查詢命令。此模式也未觀察到主動回報，
仍不能排除收到特定命令後才回覆。所有 UI 角度維持標示為要求值。

使用者另授權兩軸在 30° 以內低速測試，並確認兩顆起點皆為 90°。
`tools.servo_motion_test` 隨後依序對 `00`、`01` 各送
1500 → 1556 → 1500 μs，最大要求偏移 5.04°，脈寬每次最多 2 μs、
兩次寫入至少相隔 50 ms（最多 3.6°/s），兩軸不並動。共 114 條指令、
TX 1026 bytes；逐次監聽加末尾 3 秒觀察，RX 仍為 0 bytes。
這次也未觀察到位置命令 ACK；主機寫入成功不證明實際轉動或回中。
使用者隨後現場確認兩顆皆緩慢轉動並回中、沒有抖動。這是本次小幅
動作的現場驗收，不是 UART 位置回授、實際角度精度或大負載能力證明。

| 資訊 | 目前能取得的證據 |
| --- | --- |
| USB 轉接器存在、串口開啟 | 主機可確認，不能推定控制板在線 |
| 指令已交給作業系統 | 可由成功寫入得知，不能推定控制板接受 |
| 模式、命令 ACK、控制板 PWM | 未找到文件化讀回方式 |
| Servo 實際角度、到位、堵轉 | 此三線 PWM 接法沒有已定義的位置回傳路徑 |
| 電壓、電流、溫度 | 未找到此控制板的量測回傳欄位 |

需要真實位置時，應增加位置感測或使用有文件化讀回協定的致動器。
不能把已要求角度或 1500 μs 中位設定顯示為量測角度。

## 原始圖片檔

- [控制板外觀](assets/servo-controller/board.png)
- [完整規格](assets/servo-controller/specifications.png)
- [模式與指令](assets/servo-controller/protocol.png)
- [USB-TTL 接線](assets/servo-controller/wiring.png)

## 2026-10-31 通道與方向校準

本次以單通道 `1500 → 1556 → 1500 μs` 測試：先對 `01` 測兩次，現場回報為左右；
再對 `00` 測一次，確認先朝上並回中；最後對 `01` 再測一次，確認先朝左並回中。
每次最多 5.04° 要求偏移、3.6°/s、57 條指令；四次共 TX 2052 bytes、RX 0 bytes。
通道／轉向依使用者現場觀察確認，串口無 ACK 或實際角度回授。
自動俯仰初始只開放 85–95°，尚未驗收大行程或現場人物自動跟拍。

## 舊 UART 上線流程


1. Review this project's [controller contract](servo-controller.md).
2. Confirm GXServo 42 kg / 180 degrees and controller M0. Power off equipment
   before moving wires to Jetson J12 pin 8 TX -> controller RX and pin 6 GND.
   Controller TX and positive supply do not connect to Jetson.
3. Apply the prepared update while tracking is DISARMED and event recording idle.
   Preserve field gain, deadband, direction, zoom and camera orientation. The new
   service must remain DISARMED; opening UART alone must not send any command.
4. Confirm serial service readiness, camera link/advancing frames and inference.
   A ttyTHS1 device or successful write alone does not verify the physical signal.
5. With explicit movement authorization and a person at the servo power cut-off,
   verify 1500 us centre, direction and bounded motion around 90 degrees. Do not
   run the smoke tool alongside the service owning the UART. The tool requires
   `--execute --confirm-centered` and never runs as part of deployment.
6. Verify installed mechanical limits before full-range tracking. Test STOP,
   stale target, disconnect/restart and manual re-ARM; STOP is not power-off and
   the controller may complete its last requested position.
7. Restore camera Ethernet, check image orientation, then validate a full surf
   sequence: chase -> lock -> follow -> finish -> completed MKV -> private Drive.

For a separately authorized bounded motion test, after the service has stopped:
`python3 -m tools.servo_smoke_test --config <local.toml> --execute --confirm-centered`.
It assumes an independently verified 90-degree centre, then requests a slow
90 -> 95 -> 85 -> 90 sequence. There is no ACK or position sensor.

No network, boot firmware, GPIO pinmux, lights or audio changes are included in
the UART application update. If the pin cannot transmit, diagnose it before
considering any device-tree or firmware changes.
