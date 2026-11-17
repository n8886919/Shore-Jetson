# 歷史 RS485 雲台接線與主設定頁控制

**2026-11-14 更新：RS485 雲台已收起，現行為 CH340G USB-TTL → M0／M1 →
兩顆 180° PWM servo。** 現行設定與停止語意見[控制契約](servo-controller.md)。
以下保留 RS485 接線、主設定頁 Pelco-D 手動面板與歷史測試資料，不能用來控制 PWM；
只有日後明確恢復此雲台並重新核對裝置／授權時才適用。

歷史 RS485 契約見[控制契約歷史章節](servo-controller.md#歷史-rs485-雲台已收起)，
紙本尺寸、行程、速度與承重見[硬體規格](hardware.md)。

使用者提供的[雲台原始說明書](assets/manuals/camera-and-rs485-pan-tilt-20261106.pdf#page=6)
位於合併掃描件第 6–7 頁（紙本第 1–2 頁）；來源與頁碼索引見
[硬體說明書](hardware-manuals.md)。

RS485 雲台使用 **9600 baud、8N1、地址 001（十進位 1）**，由使用者另供
12 V；先核對銘牌極性。手動控制已整合至主畫面的齒輪設定，須明確選用 `pelco_d`，
見[控制設定](servo-controller.md#自動跟拍主程式)，不可把舊 GXServo／M0 控制指向本轉接器。
原獨立雲台測試頁、啟動模組與 18093 服務入口已從來源移除。

2026-11-06 使用者將 Moxa T 端改接 R 端後確認可手動控制轉動。
同日 42 組 GET 查詢全部零 RX；位置、限位、警報與碰撞回授仍不可用，
且未獨立確認實體回傳路徑，不能據此斷定所有查詢都不支援。
測試時沒有安裝／連接相機；鏡頭功能與帶載跟拍不在已驗收項目內。

## 接 Nano

Nano 原生 TX/RX 是 **3.3 V UART**，無法直接接 RS-485 的差動 A/B。
無論 USB 或原生排針，都需要 RS-485 收發器。不要使用 RS-232 或只有 USB-TTL
的轉接器替代。12 V 雲台電源不得接入 Nano 排針或邏輯電源端。

### 使用原生排針

選擇 **3.3 V 邏輯、3.3 V 供電、硬體自動收發方向** 的 TTL ↔ RS-485 模組。
以下為標準 Nano 40-pin 接頭的**實體腳位編號**，第三方載板先核對自身說明書：

| Nano 40-pin | TTL ↔ RS-485 模組 |
| --- | --- |
| Pin 1：3.3 V | VCC（僅限明確支援 3.3 V 供電的模組） |
| Pin 6：GND | GND |
| Pin 8：UART TX | DI／TXD（模組的 TTL 輸入） |
| Pin 10：UART RX | RO／RXD（模組的 TTL 輸出，必須為 3.3 V） |

模組的 A/B 再接雲台 RS-485 A/B，依兩邊的 `+ / −`、D+ / D− 標示核對。
不同廠商 A/B 命名可能相反，不可只靠線色；若確認是通訊極性相反，斷電後交換 A/B。
有獨立通訊 GND／COM 時，依模組與雲台說明書接到 RS-485 側的訊號參考地；
不要把保護地 PE 當成訊號 GND，也不要跨接隔離模組的隔離兩側。

接頭的 TXD／RXD 有時以外部主機視角命名，仍以模組資料表的「輸入／輸出」為準。
普通 5 V MAX485 模組的 RO 可能輸出 5 V，不可直接接 Nano RX。
只有 DE／RE 方向腳、沒有硬體自動方向控制的模組，目前工具不支援。
單一短線先依模組內建終端／偏壓說明接線，不盲目疊加終端電阻。

標準 Nano 常見裝置為 `/dev/ttyTHS1`，但需要核對載板、pinmux 及 console/getty
占用，不能直接照抄。工具會拒絕已識別的核心 console；不能取代對 systemd
getty／nvgetty 的檢查。此開發未變更 Nano 的串口或系統服務。

### 使用 USB 轉 RS-485

Nano USB → 支援 Linux 且自動方向控制的 USB-RS485 → 雲台 A/B。
選用隔離款可隔開主機與現場匯流排；有 RS-485 側 GND 時依說明書接訊號參考地。
用 `ls -l /dev/serial/by-id/` 核對轉接器，優先使用固定識別路徑。
主程式以 exclusive 模式開啟串口，手動／自動交接時先關閉前一個持有者，再開啟下一個。

### Moxa UPort 1130

使用 **RS-485 2-wire** 時，原廠指定兩線接在 **R+（Data+）、R−（Data−）**；
**T+／T− 留空，不與 R 端短接**。這個模式的 R 端同時負責收發。
Moxa 的字母定義是 B＝+、A＝−；其他廠牌雲台可能相反，應依雲台說明書核對
正負，不能直接照 A 對 A、B 對 B。
參考 [Moxa 原廠端子表，第 55 頁](https://moxa.com/getmedia/3b173f1d-2dee-42ef-bdd7-8746dc285bf9/moxa-uport-1100-series-manual-v10.0.pdf#page=55)。

Nano 的映像可能未啟用 `CONFIG_USB_SERIAL_TI`；看到 USB ID `110a:1130` 但沒有
`ttyUSB`，不代表雲台接線有問題。先確認 `ti_usb_3410_5052` 模組及
`/lib/firmware/moxa/moxa-1130.fw` 是否存在。原生 Linux 4.9 驅動已支援 UPort 1130，
以兩線 RS-485 模式初始化。若使用 Moxa 自家 `mxu11x0` 驅動，需依其文件核對介面模式。

補裝模組需使用與執行中核心完全相符的 headers／符號版本；不要為了 USB 轉接器
更換整個 Jetson 核心。安裝後核對驅動綁定、固定裝置連結與使用者存取權限，
再由使用者於停用狀態的頁面連接串口。USB 辨識及串口產生仍不證明雲台收到指令。

## 開啟主設定頁

實機使用既有主服務及網頁入口（預設 loopback 80），在 `/kiosk?layout=nano` 點齒輪，
即可看到「雲台手動控制」。手機頁的控制連結也通往這個主設定面板，不再另開測試頁或服務。
一般開發主機只看離線版面：

```sh
python3 deploy/preview-web.py
```

開啟 `http://127.0.0.1:18092/kiosk?layout=nano&controls=1`；離線預覽拒絕所有硬體控制，
不開串口。實機需已確認轉接器、驅動及權限，且取得當次動作授權：

1. 先停止自動跟拍，點「連接雲台」。主程式先關閉自動 writer，再由手動控制開啟相同串口；
   交接失敗時不開第二個持有者。此時仍未啟用方向控制。
2. 點「啟用手動」後才接受方向操作。預設 **8/63**；速度 1–63 是協定級別，非度／秒。
   手動操作不要求相機已連接或影像軸方向校準，方向按鈕直接代表 Pelco 左／右／上／下。
3. 按住方向按鈕移動，放開即送 STOP。方向鍵僅在手動控制區取得焦點且設定面板開啟時有效，
   滑桿上的方向鍵只調整滑桿。每次按住最多 2 秒；瀏覽器約 120 ms 續租，主程式獨立 watchdog
   在 0.4 秒未收到更新時送 STOP 並停用。延遲指令不能恢復過期動作。
4. 「停止」、空白鍵、Escape、收合設定面板、切到背景、失焦或離頁會撤銷手動控制並嘗試 STOP。
   前端亦拒絕這些事件之前尚在傳輸的 ARM／方向回覆。全域「停止追蹤」同時停止手動雲台。
5. 點「斷開雲台」送 STOP、關閉手動串口，再依設定恢復自動服務，維持 DISARMED。
   手動連接期間無法 ARM 自動跟拍；自動跟拍啟用期間則拒絕連接、啟用或移動手動雲台。
   另一分頁可停止，但無法接管目前手動 session。

`servo.enabled` 管理自動跟拍輸出。設為 false 或 `pan_direction=0` 時仍可在主設定頁明確
連接／啟用手動控制，以供方向校準；讀取頁面、輪詢或開關選單本身不開啟手動串口或發方向指令。
RS485 自動跟拍的方向、速度上限與其 250 ms 控制期限見[歷史契約](servo-controller.md#自動跟拍主程式)。

STOP 是主機停止命令，未確認雲台端 watchdog。Nano 斷電、強制終止或通訊線脫落時可能無法送達。
正常主服務關閉會嘗試 STOP 並關閉手動串口；不完整或失敗的寫入會撤銷手動 session 並嘗試 STOP。
成功寫入不是實體收件或停止回授，仍沒有位置回讀、電子行程限制或自動回中。

## 驗證與來源

```sh
python3 -m pytest -q tests/test_pelco_d.py tests/test_pan_tilt_settings.py
node tests/test_pelco_controls.js
```

協定依 [Pelco D Protocol 5.0.1 原廠文件鏡像](https://www.bit-cctv.com/uploads/file/pelco-ptz-protocols-d-protocol-revision-5.0.1.pdf)
的標準方向位元與 7-byte checksum。地址 1、速度 8 的向右封包為
`FF 01 00 02 08 00 0B`，停止為 `FF 01 00 00 00 00 01`。
GPIO 電氣參考 [NVIDIA Nano 使用指南](https://developer.nvidia.com/embedded/learn/jetson-nano-2gb-devkit-user-guide)，
UART pin 8／10 與 console 檢查參考 [NVIDIA 開發者討論](https://forums.developer.nvidia.com/t/uart1-pin-8-10-j41-uhf-rfid/201536)。
其他 USB-RS485 接線參考 [Waveshare 原廠說明](https://docs.waveshare.com/USB_TO_RS485_C/User-Guide)；
本專案 Moxa 端子以本頁引用的 Moxa 原廠表為準，不沿用其他轉接器的 T／R 接法。
RS485 雲台的 A／B 正負、零點、速度級別對照、端點與帶載動作仍待現場驗收。
