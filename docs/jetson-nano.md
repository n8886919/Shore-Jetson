# Jetson Nano 移植與 FPS 實測

此路徑針對 4 GB Nano、JetPack 4.6.6 / L4T 32.7.6、Ubuntu 18.04、
TensorRT 8.2.1。Orin 原始版本保留於 `332edfc` / `codex/orin-baseline`；
Nano 變更位於 `codex/jetson-nano`。引擎必須在目標板上建立，不能沿用 Orin 引擎。

10/19 完成隔離執行環境、模型移植與效能測試。10/30 已整合正式服務的
RGBA 推論分支：同一硬體解碼器分出預覽及推論，推論保留來源尺寸、4 fps
交付最新影格，模型每次輸入仍為 896×512，辨識預設不額外限速。
Nano 預覽 10 fps，先在 NVMM 丟棄多餘影格，原始壓縮錄影不受降頻影響。
GStreamer 1.14 使用同步 splitmuxsink 與 Matroska muxer 物件，不要求新版屬性。

使用者於 2026-10-31 確認目前螢幕為 **1024×600 橫向**，是 UI 的主要設計與驗收尺寸，
完整規格見 [UI 規格](ui.md)。
本機 kiosk 使用 `?display=local&layout=nano`：1024×600、固定底部狀態列、
選單收合時影片 960×540，展開時等比例縮成 768×432、不裁切；右側選單寬 256 px，
包含原黑色區域與右下角。50×50 px 圓角齒輪隨選單左移，動畫 0.1 秒。
右上角縮小／關閉按鈕與齒輪同為 50×50；縮小會把瀏覽器視窗變成 512×300，
露出 Jetson 桌面，再按還原回全螢幕。關閉前確認，dock 的 Shore Jetson 圖示可重開；原 1280×800 版面仍保留。

使用者於 **2026-11-19** 確認改用 **40-pin 原生 3.3 V UART → M0／M1 控制板
→ 兩顆 180° PWM servo**，取代先前 CH340G USB-TTL。`m0_pulse` 維持 9600 8N1
與 ASCII 脈寬命令；實機僅切換 `servo.serial_device`，保留通道、方向、速度與限制。
需核對 `/dev/ttyTHS1` 非 console、停用占用該埠的 `nvgetty`，並以 udev 維持
`root:dialout 0660`；不要修改另一組 `ttyS0` 除錯 console。
現行設定與 STOP 語意見[控制契約](servo-controller.md)。一般 Pelco-D/Moxa 範例與
具名 M0 profile 的舊運動參數都不能直接當作本次實機設定。

2026-11-06 RS485 階段曾補裝匹配 `4.9.337-tegra` 的 `ti_usb_3410_5052` 模組，
使用 Moxa R+／R−、9600 8N1、地址 001、獨立 12 V。手動轉動與 42 組零 RX 查詢
屬該次未裝相機的雲台測試；不適用目前 PWM，詳見[歷史 RS485 資料](pelco-d-test.md)。
下文 2026 年 10 月效能與部署紀錄仍是當時結果；即使換回相同 PWM 通訊路徑，
舊角度、通道、方向、回中與驗收也不能當作目前組裝的證據，見[歷史 M0 資料](legacy-m0-controller.md)。

## 安裝路徑

- `deploy/prepare-nano-system.sh`：需 root，清除使用者已授權移除的套件，
  安裝系統 TensorRT 與編譯依賴。使用前先讀腳本中的明確套件清單。
- `deploy/bootstrap-nano-user.sh`：一般使用者建立隔離 Python 3.11，
  編譯匹配系統 TensorRT 8.2.1 的 Python binding 與 GStreamer GI。
  不替換 Ubuntu 的 Python 3.6。binding 的 dtype helper 改用 `np.bool_`，
  相容 NumPy 1.26，未修改 NumPy 全域型別。
- `deploy/nano-benchmark-network.sh`：需另行取得網路變更授權；建立專用
  Ethernet、DHCP 與禁止轉送規則，保留 Wi-Fi。NetworkManager 1.10 使用
  IPv6 `link-local`。測試 profile 不自動連線，`--remove` 可移除。
- `deploy/install-nano-user.sh`：安裝正式應用程式、使用者服務及 kiosk。
  Web 預設改為 80；一般使用者服務若無低埠權限，安裝前會停止，
  需另行授權管理員設定，見[Web port 80](project-setup.md#web-port-80)。
  FPS 測試不需要執行此步；正式服務含錄影管線，不能把啟動服務當作純效能測試。

執行環境位於 `~/.local/share/shore-tracker/{python,venv,app,models}`。
`app` 可指向 `releases/` 下的部署目錄；腳本應使用 Python 絕對路徑，
避免以 symlink 工作目錄下的 `../venv` 定位直譯器。設定檔中的 `~` 會先展開。
相機 URI 僅放在 `~/.config/shore-tracker/secrets.env`，權限 0600；
不要放入指令列、報告或 Git。在核對當次 UART 設定、相機方向、中立與機械限制前，
待部署設定維持 `servo.enabled=false`；不直接套用一般 Pelco-D 範例或舊 M0 profile。

`deploy/install-nano-network.sh` 為已核准的專用 eth0 安裝流程：保留 Wi-Fi
預設路由，禁止 eth0 轉送，提供不宣告 gateway／DNS 的 DHCP，並啟用開機服務。
機器專屬保留租約只放 `/etc/shore-tracker/camera-unit.conf`，不進 Git。
移除時先停止／停用 `shore-tracker-camera-dhcp.service` 與
`shore-tracker-camera-network.service`，將 `shore-camera-benchmark` 的 autoconnect
設為 no，再移除該服務使用的 eth0 FORWARD 規則。不要變更 Wi-Fi profile。

一般使用者安裝路徑的服務位於 `~/.config/systemd/user/shore-tracker.service`；
改用 port 80 的管理員遷移可使用 `/etc/systemd/system/shore-tracker.service`，
仍以原使用者執行，並停用舊 user service，避免兩個程序搶占 UART。
後端以 `sudo systemctl` 管理，WebRTC relay 仍用 `systemctl --user`；
權限與入口見[Web port 80](project-setup.md#web-port-80)。kiosk 由桌面 autostart 啟動。
安裝與當次啟動可驗證；完整斷電／重新開機仍須另行驗收。

### Camera SD recording

選用相機卡模式後，開始與停止按鈕控制相機自己的定時錄影排程。Nano 僅從 RTSP 取得預覽與辨識影像。開啟頁面、啟用追蹤或辨識到衝浪者均不會開始錄影。原有 Nano 影片仍可在掛載原磁碟後透過檔案頁讀取。

1. 在 Nano 的相機專用網路核實管理帳密可透過安佳 CGI `getuid` 取得工作階段，測試時以隱藏輸入取得密碼，僅在記憶體處理。相機拒絕的帳密請先向機主或賣家核對。取得可用帳密後，由機主在服務既有的 `EnvironmentFile` 加入兩個變數 `SHORE_CAMERA_ADMIN_USER` 與 `SHORE_CAMERA_ADMIN_PASSWORD`。Nano 安裝使用 `~/.config/shore-tracker/secrets.env`，由服務帳號擁有、權限 `0600`。保留原 RTSP 設定，避免將帳密寫入命令歷史、TOML、報告或 Git。
2. 部署含相機卡控制的後端與網頁，在生效 TOML 的既有 `[recording]` 區段加入 `destination = "camera_sd"`。使用安裝時選定的 systemd 層級重新啟動服務，再查 `/api/diagnostics` 的 `recording.destination`。省略此欄或設成 `"jetson"` 會使用本機錄影。曾將影片遷到 Nano SD 的安裝，其 `50-video-storage.conf` 另以掛載相依性與 `mountpoint` 限制服務啟動。需要拔除 Nano SD 也能運作時，另行調整這個 drop-in 並驗收無卡啟動。
3. 先按「查詢相機卡」。相機須回報本機儲存已開、遠端儲存已關，才能操作排程。記下測試前狀態與當日最後片段，再用「開始相機卡錄影」和「停止相機卡錄影」核對排程的獨立讀回。等待片段結束後再查一次，用相機本地時間與非零大小辨認新增片段。在相機回放中確認這段影像能播放，並將排程還原至機主指定的狀態。排程回讀只證明設定生效，容量、掛載與寫入故障原因仍需從相機查證。

10/30 先前限速 2 Hz 的整機短測（kiosk、辨識、預覽與背景錄影同時運作，35 次每秒觀察）：
辨識平均 1.93 Hz、觀察值 1.79～2.04 Hz，預覽平均 10.09 fps；各元件全程 ready、
全程 DISARMED。這是短時間運作結果，並非持續至少 2 Hz、海面準確率、長時間
熱穩定或 Servo 實體驗收。1024×600 實機 DOM 已核對：版面完整覆蓋視窗，
底列 y=544、高 56 px；控制面板展開／收合不改變底列位置。
最終套件及一般 kiosk 啟動後另觀察 25 次：辨識平均 1.87 Hz、
1.60～2.02 Hz，預覽平均 10.07 fps，全程 ready／DISARMED。
此組測試整機約 1.9 Hz，不能宣稱穩定達到至少 2 Hz。
其後已移除額外辨識限速與一秒預覽緩衝，改為把最新有效辨識框放在最新影像；
前述數值仍是舊有限速設定的實測，不能當作不限速後的辨識吞吐量。

## 桌面 OC 通知

`System throttled due to Over-current` 由 NVIDIA `nvpmodel_indicator` 發出，
不是 kiosk 網頁內的提示。若要避免反覆遮住畫面，可在使用者的指示器副本中，
只於 `do_warning()` 顯示前過濾 `throttle-Over-current`；保留原事件紀錄、
溫度降頻與高溫表面通知，以及電源模式選單。這只調整桌面顯示，不會修復供電問題，
也不改核心 OC／溫度保護或功耗模式。

使用者副本位於 `~/.local/share/shore-tracker/desktop/nvpmodel_indicator.py`，
由 `~/.config/autostart/nvpmodel_indicator.desktop` 覆寫同名桌面啟動項目；
載入模組仍取自 `/usr/share/nvpmodel_indicator`，系統原檔保留。
原廠選單的 `Settings → Disable notification` 會同時停用其他警告，且不保存到下次登入，
不適合作為只關閉 OC 的設定。

修改前備份與即時還原工具放在 `~/.local/share/shore-tracker/maintenance/quiet-oc-*/`；
用 `python3 restore.py` 可移除本次覆寫並重啟原廠指示器。
桌面登入會自動選用同名覆寫，重新登入／開機仍須另行驗收；
JetPack 套件更新後須核對使用者副本與新版原廠指示器的差異。

## 模型轉換

原始 opset 17 模型的 `HardSwish` 無法直接由此板的 TensorRT 8.2 parser 匯入。
在具有 `onnx==1.17.0`、`onnxruntime==1.20.1` 的主機執行：

```sh
python -m tools.convert_nano_models --source ORIGINAL_MODEL_DIR --output NANO_MODEL_DIR
```

轉換僅將 HardSwish 展開為 HardSigmoid × input，不修改權重或原始檔案。
工具以零值及固定種子的隨機輸入比對原始與轉換後的 ONNX Runtime FP32 輸出，
動作模型涵蓋 batch 1、4、20；結果存於 `conversion-proof.json`。
2026-10-19 兩個模型在這些測試輸入上的最大絕對誤差皆為 0，且目標板 parser
已成功匯入。這不等於 FP16 精度或實際海面辨識品質已驗收。

轉換後模型放在目標板 `models/nano-trt82/`；`config/jetson-nano.toml`
使用此路徑。第一次載入會建立 FP16 引擎，建置時間不計入穩態 FPS。

## 最大解析度與 2 Hz 目標

2026-10-19 相機 ONVIF 主串流回報 3840×2160 / 20 fps，列出的最大解析度
也是 3840×2160。原有 960×540 是應用程式預覽尺寸，並非相機上限。
目前模型每次輸入固定為 896×512；讀入 4K 並不等於模型逐像素處理整幅 4K。

以下純效能工具只在記憶體中解碼及辨識，不錄影、不發 UART 或 PTZ 命令：

```sh
cd ~/.local/share/shore-tracker/app
OPENBLAS_NUM_THREADS=1 ../venv/bin/python -m tools.benchmark_nano \
  --models ../models/nano-trt82 \
  --env-file ~/.config/shore-tracker/secrets.env \
  --frame-size native --native-tiles --samples 30 --tile-samples 5 \
  --output ~/shore-tracker-setup/benchmark-native.json
```

工具分開回報：

1. 合成輸入的 detector 延遲，以及動作模型每批延遲／每秒 crop 數。
2. 相機原始尺寸解碼，再整張縮放到模型固定輸入的完整辨識 FPS。
3. 原始尺寸分區辨識：4K 使用 25 個有重疊的 896×512 區塊，區塊像素
   不縮小；合併框與全圖 NMS 後進行追蹤及動作辨識。

第 3 種是 benchmark 專用實驗，未接入正式服務；分區邊界與場景上下文改變
可能影響準確率。每個完整畫面須完成所有區塊，不能把區塊 FPS 當作全圖 FPS。
預覽解碼 FPS、純模型 FPS 與完成辨識 FPS 也不得互換。
正式辨識及分區 benchmark 在全圖 NMS 後最多保留 10 個候選，依偵測信心排序；
動作模型合成輸入測試另外列出 batch 2、5、10。`--extra-action-crops 2`、
`--extra-action-crops 5` 或 `--extra-action-crops 10` 可加上相應數量的固定
模擬裁切負載，回報時須與實際偵測數分開；它不是場景真人數或辨識準確率的證據。
減少候選數仍需跑完整幅偵測，主要節省裁切與動作分類，FPS 不會按人數反比增加。
`completed_frame_age_p95_ms` 從 appsink 畫面準備完成開始算，不代表相機曝光至結果
的端到端延遲。目標為完整辨識至少 2 Hz，須由實測結果判定，不以設定值代替。

## 2026-10-31 5W／MAXN 比較：現有供電條件

Nano 4 GB 使用同一組現存 FP16 引擎、偵測門檻 0.70、NMS 後上限 10，
以 MAXN → 5W → 5W → MAXN 順序各測兩輪；每輪熱身 5 張後量測 30 張。
4K H.265 解碼保留 3840×2160，RGBA 每秒取 4 張，整張縮放至 896×512；
實際偵測 0～1 個候選，另外固定執行 10 個模擬動作裁切。
正式服務暫停，桌面保留；測試不錄影、不寫 UART、不操作 PTZ，未鎖定時脈。

| 模式 | 完整流程 FPS | 平均處理時間 | 平均輸入功耗 |
| --- | ---: | ---: | ---: |
| 5W | 1.05 | 945 ms | 3.89 W |
| MAXN | 1.14 | 870 ms | 4.54 W |

FPS 以各模式共 60 張除以完整採樣時間計算；功耗來自 jtop 的 `POM_5V_IN`
讀值，以各輪時間加權，包含板上與背景桌面負載，不是相機或整套系統耗電。
目前條件下 MAXN 吞吐量高約 9%，5W 平均輸入功耗低約 14%；兩者均未達 2 Hz。
合成輸入的 detector-only FPS 反而是 5W 約 3.97、MAXN 約 3.40，
顯示單模型速度不能代表完整流程，也不能由此宣稱 MAXN 正常供電時的上限。

**本次存在供電保護降頻，結果僅代表當時供電條件。** 量測期間持續出現
`soctherm: OC ALARM 0x00000001`；抽樣輸入電壓約 4.49～4.59 V，GPU 最高 32°C。
[NVIDIA 文件](https://docs.nvidia.com/jetson/archives/l4t-archived/l4t-3276/Tegra%20Linux%20Driver%20Package%20Development%20Guide/power_management_nano.html)
說明 OC 告警對應欠壓或過流觸發硬體降頻；目前未分離確認電源、線材或板端原因。
應先排除供電告警再重測正常功耗模式差異。這不是海面準確率或長時間穩定度驗收。
測試完成已恢復原本 MAXN 與正式服務，保持 DISARMED。

### 同日後續：2／5／10 個模擬候選負載

依使用者指示，OC 告警只記錄、不作為測試中止條件，硬體保護與動態時脈
維持原設定。以 MAXN → 5W → 5W → MAXN 跑完四輪，所有資料在同一次開機取得。
每種模式、每種負載各兩輪，每輪熱身 5 張後量測 20 張，合計 240 張實測畫面。
來源仍為 4K H.265、RGBA 4 fps、全圖縮至 896×512、門檻 0.70、候選上限 10。
本批場景實際偵測皆為 0；另外固定執行 2／5／10 個動作裁切來模擬候選負載。
正式服務暫停，桌面保留；benchmark 不錄影、不寫 UART、不操作 PTZ。

| 模擬候選數 | 5W FPS | MAXN FPS | 5W 平均輸入功耗 | MAXN 平均輸入功耗 |
| --- | ---: | ---: | ---: | ---: |
| 2 | 1.61 | 2.23 | 4.46 W | 5.69 W |
| 5 | 1.51 | 2.06 | 4.47 W | 5.31 W |
| 10 | 1.29 | 1.80 | 4.20 W | 5.30 W |

FPS 為各模式同負載共 40 張除以採樣總時間，功耗為每輪 `POM_5V_IN` 均值按
時間加權。本批 10 候選對照值與前次不同；判斷目標數的差異應使用本表同批數據。
2／5 候選時 MAXN 比 5W 快約 38%／36%，平均輸入功耗增加約 1.22／0.85 W。
若以 2 FPS 為目標，MAXN 的 2 候選結果較有餘裕，5 候選僅接近門檻；5W 未達標。
這是模擬裁切負載的短測結果，尚未涵蓋真人海面辨識及正式 UI／錄影同時運作。
告警未中止測試，但其對速度的影響仍包含在結果中。完成後恢復測前的 5W 與正式服務。

## 2026-10-19 實機結果

相機實際解碼尺寸均為 3840×2160。CPU/GPU 使用 MAXN、未鎖定時脈，
先在 NVMM 丟棄多餘畫面，只傳出 4 fps，保留每張解析度。熱身不計入採樣，
平均 FPS 包含等待新畫面時間，P95 是收到輸入後的辨識處理時間。

| 管線 | 採樣 | 完整畫面 Hz | 處理 P95 | 場景／分類負載 |
| --- | ---: | ---: | ---: | --- |
| 4K JPEG → 整張縮至模型輸入 | 60 | 1.69 | 682 ms | 0 個偵測 |
| 4K RGBA → 整張縮至模型輸入 | 120 | 2.82 | 364 ms | 0 個偵測 |
| 同上，另加 4 個固定區域動作分類 | 120 | 2.50 | 411 ms | 0 個偵測＋4 個模擬分類裁切 |
| 4K 原尺寸 25 區塊，JPEG 輸入 | 5 | 0.189 | 5415 ms | 每張 0–1 個偵測 |

原尺寸逐區塊方案在此模型／Nano 上未達 2 Hz。省去 JPEG 編解碼後，
4K 來源整張縮放方案有 2 Hz 的測試餘裕，但 detector 每次看到的仍是
896×512，不能稱為全圖原始 4K 細節辨識。模擬裁切只測計算負載，未偽造
偵測結果，也不能視為四位真人、動作分類或海面辨識準確率的驗收。

重現 4 個額外分類裁切的測試：

```sh
cd ~/.local/share/shore-tracker/app
OPENBLAS_NUM_THREADS=1 ../venv/bin/python -m tools.benchmark_nano \
  --models ../models/nano-trt82 \
  --env-file ~/.config/shore-tracker/secrets.env \
  --frame-size native --frame-fps 4 --transport rgba \
  --extra-action-crops 4 --live-only --samples 120 \
  --output ~/shore-tracker-setup/benchmark-native-rgba-action4.json
```

純 detector 固定輸入 896×512 實測平均 175.47 ms（5.70 次／秒）；
動作模型 batch 1／4／20 平均 9.98／12.63／53.17 ms。這些不含相機及前後處理。
完成系統清理後，目標板結果移至
`~/.local/share/shore-tracker/data/benchmarks/2026-10-19/benchmark-native*.json`。

FP16 對原始 ONNX Runtime FP32 的固定隨機輸入比較，輸出皆為有限值且尺寸正確。
detector cls／box／centerness 最大絕對誤差分別為 0.03623／0.07492／0.01820；
動作 logits 為 0.00496。這是原始 tensor 數值差異，不是框的像素誤差或準確率。

測試時使用 MAXN，未鎖定 `jetson_clocks`。海面實景準確率、長時間溫度／功耗、
正式錄影與 UI 同時運作的吞吐量、控制板與伺服實體行為仍須分開驗證。

## 專用設備清理

`deploy/clean-nano-system.py` 預設只顯示移除計畫；在已授權清理的 Nano 上，
使用 `sudo python3 clean-nano-system.py --apply` 才會刪除。
報告儲存於呼叫 sudo 的使用者家目錄；直接以 root 執行時，需以 `--user USER`
明確指定非 root 的報告擁有者，不使用寫死的個人目錄。
工具以明確套件清單和已審查的連帶移除清單作限制，不執行 blanket autoremove。
GPU 執行庫、Wi-Fi、SSH、桌面及系統設定工具保留。
開發用 headers／static libraries 與執行庫分開處理；移除前者後仍須驗證引擎
載入、GPU 推論及相機解碼，不能以套件移除成功代替功能驗收。

Nano 上未使用的原始 ONNX 副本、編譯樹、下載壓縮檔、合成數值測試輸入及 pip
快取已清除；保留 Nano 轉換後 ONNX、引擎、模型契約及小型驗證報告。
日後若需重編 TensorRT Python binding，先重新執行系統依賴準備腳本，再執行
bootstrap；單純用現有 runtime 建立新的 TensorRT 引擎不需要這些開發套件。
系統 SSH 使用自訂 OpenSSL 3，`/opt/openssl-3.1.4/lib` 必須保留。
清理紀錄存於 `~/.local/share/shore-tracker/maintenance/`，不保存刪除內容的備份。

清理後遇到 SSH 檢查失敗時，不應略過檢查。此 Nano 原有的自訂 OpenSSH 9.6p1
沒有回報 systemd readiness，但服務仍設為 `Type=notify`，造成每 90 秒逾時重啟。
`deploy/repair-nano-ssh.sh` 僅針對已確認的 Nano／L4T／OpenSSH／OpenSSL 路徑，
加入 `Type=simple` override 並重啟 SSH listener；要求 `KillMode=process`，以保留
既有登入 session。以 sudo 執行後，再重跑清理腳本完成驗證。
清理腳本重跑時保留先前套件計畫；結果中的刪除數量及釋放空間只計本次執行。
`root-cleanup-result.json` 必須是 `status=complete` 才表示系統檢查全部通過；
`verification_pending` 表示仍有檢查未完成。

2026-10-19 修正後實機驗證：清理報告 `status=complete`，54 個已審查套件均
不再安裝，Docker／containerd 資料目錄已不存在，`dpkg --audit` 無異常。
SSH 為 `Type=simple`、`active/running`；同一 listener 運行超過 105 秒，
`NRestarts=0`，新 SSH 連線成功。原有 `UsePAM` 不支援警告仍存在，未改動
驗證設定。root 目錄共 164 KiB，檢查項目未見舊專案；根分割區可用約 7.8 GiB。

套件移除後的 30 張實機複驗：4K RGBA 解碼、整張縮至 896×512、額外 4 個
模擬動作分類裁切，平均 2.50 Hz、處理 P95 417 ms。兩個 GPU 引擎正常，未錄影
或操作伺服；此結果仍不是原尺寸 4K 全圖辨識或海面準確率驗收。

### 風扇溫控

2026-11-14 已將先前固定 `manual`、100% 改為 jtop 的 `temp_control`，
由 Nano 內建核心溫控依 CPU／GPU 的 `thermal-fan-est` 估計溫度調整 PWM。
目前板上升溫節點為 51／61／71／82°C，對應 PWM 80／120／160／255；
降溫另有遲滯，低溫時可以停轉。這是板上設定值，不是實測 RPM。
控制機制見 [NVIDIA Nano 熱管理文件](https://docs.nvidia.com/jetson/archives/l4t-archived/l4t-3276/Tegra%20Linux%20Driver%20Package%20Development%20Guide/power_management_nano.html)。

設定保存在 `/usr/local/jtop/config.json`，已啟用的 `jtop.service` 會於開機載入；
系統清理須保留 jtop 及此設定。在 jtop 的風扇控制選擇 `temp_control` 即為自動溫控，
不要再選 `manual` 固定速度。從手動切換時，舊的 PWM 目標可能暫留；本次在估計
溫度約 35°C、核心 cooling state 為 0 時清除舊全速目標，實機讀回
`temp_control=1`，`cur_pwm` 由 255 平滑降至 0，並核對已保存的 profile。
未重新開機或進行升溫測試；tach 回授仍未啟用，不能據此宣稱實體轉速驗收。

2026-11-17 重開機複驗發現：jtop 保存的 `temp_control` 及核心 `temp_control=1`
都仍在，但 `/etc/rc.local` 有舊指令 `echo 255 | sudo tee /sys/devices/pwm-fan/target_pwm`，
本次開機日誌確認它有執行。估計溫度 25.5°C、cooling state 為 0 時，PWM 仍為 255。
因此只保存自動 profile 尚未解決此機的開機全速問題；須先備份並移除該固定全速
指令，再驗證下一次開機。此次僅只讀檢查裝置，因 SSH 無免密碼 sudo，尚未移除。

## Waveshare eMMC 版的 microSD 影片暫存

此機的系統碟是內建 16 GB eMMC；Waveshare 載板的 TF 卡槽使用 SDMMC3
（`/sdhci@700b0400`）。2026-11-17 實機只有 eMMC 被辨識，執行中的 SDMMC3
仍為 `disabled`。先前在 `/tmp` 編譯的測試 DTB 未安裝，重開機後也已不存在。
這個狀態尚不能判定卡片本身或其檔案系統是否正常。

R32.7.6 支援透過 `extlinux.conf` 的 `FDTOVERLAYS` 合併小型 DTBO，保留開機程式
對原 DTB 做的修正；參考 [NVIDIA 開機文件](https://docs.nvidia.com/jetson/archives/l4t-archived/l4t-3276/Tegra%20Linux%20Driver%20Package%20Development%20Guide/uboot_guide.html)。
依 [Waveshare 卡槽設定](https://www.waveshare.com/wiki/JETSON-NANO-DEV-KIT)，
此次候選 overlay 只改 SDMMC3 的三項屬性：`status = "okay"`、
`cd-gpios = <&gpio 0xc2 0>`、`uhs-mask = <0xc>`。GPIO 以原 DTB 的 symbol 解析，
不把其他版本的 phandle 數字直接套入。

候選已在 Nano 編譯，並與執行中／磁碟上的兩份基底各自離線合併；逐項比較確認
只有上述三項差異，eMMC 節點及其他屬性相同。安裝腳本另已驗證原開機項目保留、
重複執行、設定變動拒絕、檔案毀損拒絕、寫入失敗及回復。
初版 `install` 模式保留 `primary` 為預設；手動回復依賴可操作的開機
選單，不能當作自動失敗回復保證。

也可以用 SSH 選定預設測試項目，不必在現場操作選單。已準備的 `activate` 模式
會安裝同一份 overlay，將 `DEFAULT` 改為 `shore-sdmmc3`，保留原 `primary` 項目；
`rollback` 可在仍能登入時還原原始開機設定。兩種預設與還原路徑均已在隔離檔案
環境測試。切換前會要求追蹤 DISARMED、錄影 idle。這不是一次性
開機或自動回復機制；若核心無法開機而失去 SSH，仍須用現場選單或其他救援方式。

2026-11-18 已完成實機重開機複驗：擁有者執行 `activate`，目前預設為
`shore-sdmmc3`，執行中的 SDMMC3 為 `okay`，核心以 SDR104 辨識到 29.1 GiB SDHC
卡 `/dev/mmcblk1`；系統根目錄仍在 eMMC `/dev/mmcblk0p1`。卡上原有 FAT32 分割區
已由桌面掛載，16 MiB 暫存檔寫入、fsync、讀回 SHA-256 比對通過，測試檔已清除。
這只證明當次小範圍檔案讀寫，不是完整卡片容量或長時間錄影驗收。

2026-11-18 擁有者同意改為 ext4，並要求直接刪除舊影片，已完成實機執行：

- 已核對卡片 CID、分割區大小及原 UUID，僅格式化 SD `/dev/mmcblk1p1`。
  卡片標籤為 `SHORE_VIDEO`，以新 UUID 掛載至 `/mnt/shore-video`；根目錄仍在 eMMC。
- 原 `recording.root` 與 `data_dir/manual-recordings` 路徑已連結至卡上的
  `recordings`、`manual-recordings`，應用程式設定檔保持不變。
- 掛載與缺卡保護通過後，已刪除 eMMC 上核對過的 49 段舊影片，共約 5.1 GiB，
  未複製至 SD。eMMC 可用約 7.9 GiB，SD 可用約 28.5 GiB。
- 經原錄影路徑進行 16 MiB 暫存檔寫入、fsync、讀回 SHA-256 比對通過，測試檔已刪除。
  執行中的主程式已建立當日空錄影目錄；目前沒有舊影片檔案。

`/etc/fstab` 以 UUID 固定掛載，使用 `nosuid,nodev,noexec,nofail` 與 10 秒裝置等待。
主服務 drop-in 以 `RequiresMountsFor`、`BindsTo` 與啟動前掛載檢查保護錄影位置，
未掛卡的裸目錄不允許應用程式帳號寫入。實測卸載會停止服務，重新啟動服務會自動
掛載 SD，避免影片寫回 eMMC；代價是缺卡時主程式也不啟動。

主服務已恢復 active、追蹤 DISARMED、手動錄影 idle。擁有者隨後重開機，已確認
新開機的 SD 自動以相同 UUID 掛載，兩個錄影路徑仍指向 SD、應用程式帳號可寫，
主服務自動啟動且 `NRestarts=0`，系統根目錄仍在 eMMC。重開機後的持久設定已通過
複驗；實際相機錄影及長時間穩定度尚未驗收。
