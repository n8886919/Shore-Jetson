# RS485 雲台與監視器說明書

使用者於 2026-11-06 提供的合併掃描件，原檔名為 `已掃描_20261106-0113.pdf`。
[完整原始 PDF](assets/manuals/camera-and-rs485-pan-tilt-20261106.pdf) 共 7 頁，
保留原始位元組與頁序，未重新壓縮、裁切或拆頁。以下頁碼均為 PDF 頁序。

| 文件 | PDF 頁碼 | 內容 |
| --- | --- | --- |
| [監視器／IP Camera 使用說明書](assets/manuals/camera-and-rs485-pan-tilt-20261106.pdf#page=1) | 1–5 | NVR／電腦連接、CMS 2.0、IP 設定、故障排除、P2P／NVSIP；第 5 頁為封面 |
| [電動支架雲台說明書](assets/manuals/camera-and-rs485-pan-tilt-20261106.pdf#page=6) | 6–7 | RS-485、PELCOD、9600 baud、地址 1、DC 12 V ±20%；規格與安裝尺寸圖，紙本頁碼 1–2 |

## 與本專案的關係

- 攝影機掃描件未標示具體型號，也未提供 RTSP 路徑或 ONVIF 介面規格；
  不據此推定 YL-A85-800／MCA85_AF 的所有功能或目前網路設定。
  專案串流與錄影方式見[相機管線](camera-pipeline.md)。
- 雲台掃描件未標示具體型號；2026-11-06 曾選為 **RS485 雲台**，現已收起。
  使用者於 2026-11-14 再次確認現行為 CH340G USB-TTL → M0／M1 → 兩顆 180° PWM servo。
  現行[控制契約](servo-controller.md)與[硬體](hardware.md)已區分兩者；
  [RS485 手動測試](pelco-d-test.md)僅作歷史參考。
  PWM 控制板原始圖片與舊校準見[歷史 M0 資料](legacy-m0-controller.md)，
  不直接沿用為目前驗收。此掃描件的 RS485 速度、行程、12 V 與承重不適用 PWM。

## 來源與完整性

- 來源：使用者提供的掃描件；私人附件連結不隨原始碼公開。
- 檔案大小：3,150,251 bytes。
- SHA-256：`b467930543f6cbfffbbfce116e62e22769497bf78c6d72a045e89113c4249c6d`。
- 原件是影像掃描，沒有可搜尋的文字層；已逐頁檢視內容。
