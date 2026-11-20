# 原生桌面與串流

2026-11-14：實體螢幕採 C++／Qt 5 Widgets；Python 保留相機、辨識、控制與錄影。
原生畫面以共用記憶體傳送 BGRx，避開本機 JPEG／HTTP／瀏覽器解碼。
它仍有 CPU 記憶體複製與 Qt 繪圖，不宣稱 GPU zero-copy。

## 安裝

需 Qt 5.9+ Widgets/Network 開發檔、C++14 編譯器與 CMake。Nano 的 Qt 5.9.5
可使用；不需要 Qt WebEngine。先建置，再於已授權的目標安裝：

```sh
cmake -S native -B build/native
cmake --build build/native -j2
python3 deploy/install-desktop.py build/native/shore-tracker-desktop
```

安裝工具備份既有 launcher/autostart/binary，改為 `start-desktop`，不另啟動一份
追蹤後端。安裝時也在使用者桌面加入 Shore Jetson 圖示，標為可信任並開啟 GNOME
桌面圖示顯示；退出 App 後可由桌面圖示或 dock 重開。縮小到 512×300，還原回全螢幕。
`--offline --controls --screenshot /tmp/desktop.png` 只產生合成版面預覽；實際相機模式
拒絕 screenshot 選項。`--smoke-seconds 10` 可做有界顯示測試，不送控制命令。

Nano profile 的來源設定如下；實機是否已套用需另行核對。一般設定仍只限 loopback：

```toml
[camera]
native_display = true
preview_fps = 10

[web]
host = "0.0.0.0"
allow_lan = true
h264_preview = true
port = 80
```

80 的 Linux 綁定權限與既有設定遷移見[環境準備](project-setup.md#web-port-80)。
瀏覽器可直接輸入 `http://<Jetson-IP>/`；已安裝的 App launcher 與 relay 也需使用同一埠。

WebRTC 使用 [go2rtc 官方版本](https://github.com/AlexxIT/go2rtc/releases/tag/v1.9.15)
的 `go2rtc_linux_arm64`（本次測試版本 v1.9.15），以預先核對的 binary 安裝。
本次 binary SHA-256：`af416f22b21a24ab83b8d78642190b4932ae695b4e386853f11d1b0319781763`。

```sh
python3 deploy/install-webrtc.py /path/to/go2rtc_linux_arm64
```

管理／SDP relay 綁定 `127.0.0.1:1984`；瀏覽器透過同一個 port 80 網頁轉接 SDP，
WebRTC 媒體使用 18555 TCP／UDP。不開啟 RTSP server，不做路由器轉發或變更相機網路。
手機／筆電需能連到 Jetson 的管理區網；App 底列在 port 80 只顯示 `IP`，其他埠顯示 `IP:port`。

原生 App 不使用 WebRTC。Web 使用 H.265/WebRTC；失敗回到 JPEG。
開啟網頁不 ARM、不發相機或舵機控制。只有 viewer 接上本機 MPEG-TS source 時
才建立 H.265 encoder；斷線會釋放該 encoder。原有 JPEG 生產仍保留，供回退與既有 API。

`web.h264_preview` 是沿用的設定鍵。網頁協商 H.265，缺少 H.265 或連線失敗時顯示 JPEG。

## Nano 先前 H.264 與 JPEG 編碼實測

2026-11-14，在同一台 Nano／L4T 32.7.6 的實際相機預覽，960×540、約 10 FPS，
各取 15 秒，讀同一條原生 RAM 畫面支線。測試不保存影像、不啟動馬達。

| 編碼 | 輸出 FPS | 視訊 Mbps | CPU（單核心=100%） | 編碼管線 P95 ms |
| --- | ---: | ---: | ---: | ---: |
| JPEG quality 80 | 10.06 | 21.505 | 21.7 | 25.1 |
| 硬體 H.264 1.5 Mbps | 9.79 | 1.498 | 9.5 | 10.8 |

這是此場景的編碼比較，不是海面畫質評分、瀏覽器顯示 FPS、相機到螢幕延遲或完整跟拍
效能。H.264 設定約省 93% 視訊頻寬；細節畫質仍須以實際海面／衝浪目標驗收。
原生 App 的 10 秒 smoke test 呈現 95–106 個 advancing frames；背景服務維持 DISARMED。
區網 Chromium 瀏覽器當次量到 10.0 decode FPS、往返約 6 ms；RTT 不是相機到螢幕延遲。
停止 relay 時可回退到 JPEG，恢復後可重新建立 WebRTC。所有 viewer 及測試 relay 結束後，
後端 `encoder_active=false`；原生螢幕與相機預覽持續運行。

重跑（僅在已授權的 Nano，需相機與 native frame ready）：

```sh
python3 tools/benchmark_streaming.py --seconds 15
# 純編碼器能力檢查；不能當作相機實測
python3 tools/benchmark_streaming.py --synthetic --seconds 5
```

主機測試、目標編譯、當次區網瀏覽器播放與實體／重新開機驗收須分開回報。
不以 API 200、已存 autostart 或一次短測宣稱完整斷電啟動、手機相容性、跟拍或戶外穩定度。
