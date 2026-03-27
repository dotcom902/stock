# 🤖 AI-Driven Swing Sniper (AI 量化波段狙擊系統)

這是一套結合「傳統量化技術面指標」與「生成式 AI (Gemini 2.0 Flash)」的全自動美股波段交易雷達。系統每日於盤後自動運行，精準捕捉市場資金輪動，並針對高 Beta 值的熱門股提供全天候的期權與正股操作 SOP。

## ⚙️ 核心架構與交易邏輯 (Trading Logic)

本系統採用 **「漏斗式過濾架構 (Sniper Strategy)」**，透過四個階段將市場雜訊降至最低：

### Phase 1: 動態資金池 (Dynamic Universe Generation)
系統每日自動構建三層監控池：
1. **基準底倉**：Nasdaq 100 成分股 (捕捉大型權值股錯殺機會)。
2. **核心必掃名單**：賦予光通信 (COHR/LITE)、半導體 (AMD/MU/SNDK) 與太空科技 (ASTS/RKLB) 絕對保送權。
3. **AI 動態熱門板塊**：呼叫 Gemini AI 判讀當日總經，自動選出額外 3 大最具爆發力的資金板塊。

### Phase 2: 全天候 5 階段生命週期判定 (Lifecycle Engine)
有別於傳統的盲目抄底，系統針對「熱門板塊」採用 5 大嚴格型態判定：
* ⚠️ **極端超買 (RSI ≥ 75 + 正乖離 ≥ 10%)**：強烈提示獲利了結或建倉 Bear Call Spread。
* 🥈 **動能突破 (RSI > 65 + RVOL > 1.2x)**：建議追擊正股，並強制設定 8% 移動停損。
* ⏳ **高檔震盪 (RSI 55~65 + 橫盤收斂)**：建議持有正股者賣出 Covered Call 賺取時間價值。
* 🥇 **強勢回檔 (RSI 40~55 + 守住均線)**：勝率最高的黃金買點，建議建倉 Bull Put Spread。
* 🛑 **趨勢破壞 (RSI < 40)**：熱門股轉弱，嚴禁接刀，無條件避開或停損。

### Phase 3: 狙擊手動態評分 (Top 5 Sniper Filter)
* **基本面評分**：爬取最新新聞標題與目標價，若含利多關鍵字 (Upgrade, Beat) 加分，利空扣分。
* **熱度加權**：結合基本面評分與 RVOL 計算最終權重。
* **Top 5 鎖定**：系統**每日僅允許將排名最高的前 5 檔極端股票**送入 AI 審查，大幅節省 API 額度並提高準確度。

### Phase 4: AI 一票否決大腦 (AI Veto)
將 Top 5 標的送交 Gemini 2.0 Flash 進行邏輯審查。若 AI 判讀利空為「結構性破壞 (如：財報暴雷、假帳、核心掉單)」，將直接觸發【🛑 AI 否決】，推翻所有技術面買進訊號。

## 🛡️ 系統防護機制 (Robustness)
* **反爬蟲裝甲**：底層採用 `curl_cffi` 完美偽裝 Chrome TLS 指紋，突破 Google News 阻擋。
* **網址加密防護**：採用 Base64 編碼核心 URL，防止 Markdown 編輯器轉換破壞結構。
* **智慧限流 (Quota Radar)**：內建 Exponential Backoff 處理 `429 Too Many Requests`，並具備「日額度耗盡」靜默退避機制，確保程式永不崩潰。

## 🚀 部署與執行
本專案依賴 GitHub Actions 每日自動觸發 (Cron Job)。請確保於 Repository Secrets 中設定：
* `GEMINI_API_KEY`: Google AI Studio 金鑰
* `MAIL_USER` / `MAIL_PASS`: 寄件用 Gmail 與應用程式密碼
* `MAIL_TO`: 收件人信單 (以逗號分隔)
