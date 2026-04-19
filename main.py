import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime
import warnings
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import xml.etree.ElementTree as ET
import urllib.parse
import time  
import json
import base64  
import requests  
from google import genai  
from curl_cffi import requests as cffi_requests

warnings.filterwarnings('ignore')

def get_safe_url(b64_str):
    return base64.b64decode(b64_str).decode('utf-8')

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
AI_MODEL_NAME = 'gemini-2.0-flash'  

ai_client = None
if GEMINI_API_KEY:
    ai_client = genai.Client(api_key=GEMINI_API_KEY)

DAILY_QUOTA_EXHAUSTED = False

MY_PORTFOLIO = ['NVDA', 'TSM', 'CRWV', 'PLTR', 'MSTR'] 

CORE_WATCHLIST = [
    'COHR', 'LITE', 'FN', 'NTAP',       
    'AMD', 'ARM', 'MU', 'SNDK', 'SMCI', 
    'ASTS', 'RKLB', 'LUNR', 'BKSY',      
]

REQ_SESSION = requests.Session()
REQ_SESSION.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
})

# ==========================================
# 🚦 宏觀紅綠燈大盤防禦機制 (VIX + QQQ RSI)
# ==========================================
def get_macro_regime():
    """
    結合 VIX 恐慌指數與 QQQ RSI 判定市場風險偏好狀態
    """
    try:
        print("🌍 正在掃描總體經濟宏觀指標 (QQQ & VIX)...")
        # 同時抓取 QQQ 與 VIX
        df_data = yf.download(['QQQ', '^VIX'], period="3mo", progress=False)
        
        if df_data.empty or 'Close' not in df_data:
            return "大盤狀態: 未知", "UNKNOWN"
            
        df_closes = df_data['Close']
        
        # 1. 計算 QQQ 14日 RSI
        qqq_prices = df_closes['QQQ'].dropna()
        if len(qqq_prices) < 20: return "大盤狀態: 數據不足", "UNKNOWN"
        
        delta = qqq_prices.diff()
        gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
        loss = -delta.clip(upper=0).ewm(alpha=1/14, adjust=False).mean()
        qqq_rsi = (100 - (100 / (1 + gain / loss))).fillna(100)
        latest_qqq_rsi = float(qqq_rsi.iloc[-1])
        
        # 2. 獲取最新 VIX 指數
        vix_prices = df_closes['^VIX'].dropna()
        latest_vix = float(vix_prices.iloc[-1])
        
        # 3. 紅綠燈邏輯判定
        signal = "UNKNOWN"
        if latest_vix > 25 or latest_qqq_rsi < 30:
            status_icon = "🔴"
            status_text = "紅燈 (Risk-Off / 恐慌蔓延)"
            signal = "RED"
        elif latest_vix < 20 and latest_qqq_rsi > 75:
            status_icon = "🟡"
            status_text = "黃燈 (極端貪婪 / 防範回調)"
            signal = "YELLOW"
        elif latest_vix < 20 and 40 <= latest_qqq_rsi <= 75:
            status_icon = "🟢"
            status_text = "綠燈 (Risk-On / 情緒穩定)"
            signal = "GREEN"
        else:
            status_icon = "🟠"
            status_text = "橘燈 (震盪過渡期)"
            signal = "ORANGE"
            
        desc = f"<span style='font-size:16px;'>{status_icon}</span> <b>{status_text}</b> <br>▪️ VIX 恐慌指數: <b>{latest_vix:.2f}</b> <br>▪️ QQQ 日線 RSI: <b>{latest_qqq_rsi:.2f}</b>"
        return desc, signal
        
    except Exception as e:
        print(f"⚠️ 宏觀指標抓取失敗: {e}")
        return "大盤狀態: 未知", "UNKNOWN"

# ==========================================
# 🧠 AI 熱門板塊動態偵測
# ==========================================
def get_ai_dynamic_sectors(max_retries=2):
    global DAILY_QUOTA_EXHAUSTED
    fallback_tickers = ['MARA', 'IREN', 'SYM', 'PATH', 'CRWD']
    fallback_desc = "AI 應用, 區塊鏈, 網路安全 (備用預設)"

    if not ai_client or DAILY_QUOTA_EXHAUSTED: return fallback_tickers, fallback_desc

    today_str = datetime.today().strftime('%Y-%m-%d')
    prompt = f"""
    現在是 {today_str}。你是一位華爾街頂尖的「板塊輪動與資金流向分析師」。
    請評估當前美股市場的最新動態，選出「當前資金最集中、最具爆發力的 3 個產業板塊」。
    然後，為這 3 個板塊各挑選 3~4 檔最具代表性、流動性佳的美股股票代碼（總共約 9~12 檔）。
    請以 JSON 格式輸出：{{"sector_names": "名稱", "tickers": ["代碼1"]}}
    """
    for attempt in range(max_retries):
        try:
            print(f"🧠 正在請 AI 偵測今日熱門板塊...")
            response = ai_client.models.generate_content(model=AI_MODEL_NAME, contents=prompt)
            data = json.loads(response.text.replace('```json', '').replace('```', '').strip())
            return [t.strip().upper() for t in data.get("tickers", fallback_tickers)], data.get("sector_names", fallback_desc)
        except Exception as e:
            if "429" in str(e): time.sleep(15)
            else: return fallback_tickers, fallback_desc
    return fallback_tickers, fallback_desc

# ==========================================
# 🤖 AI 個股投研分析
# ==========================================
def analyze_stock_with_ai(ticker, signal_type, rvol, news_list_raw, max_retries=2):
    global DAILY_QUOTA_EXHAUSTED
    if DAILY_QUOTA_EXHAUSTED or not ai_client: return "⚠️ AI 暫停分析"
    
    news_text = "\n".join([f"- {item.get('title', '')}" for item in news_list_raw[:5]])
    prompt = f"""你是華爾街交易員。{ticker} 觸發 {signal_type}，RVOL {rvol}。新聞：{news_text}
    量化法則：
    1. 超跌/強勢回檔：大盤錯殺則【可以建倉】。
    2. 動能突破：實質利多則【可以建倉】。
    3. 極端超買/高檔震盪：提示風險，結論為【鎖定利潤】或【觀望收租】。
    4. 結構/趨勢破壞：一律【高風險避開】。
    格式：50字內理由。結論：【上述四者擇一】。"""
    
    for attempt in range(max_retries):
        try:
            response = ai_client.models.generate_content(model=AI_MODEL_NAME, contents=prompt)
            time.sleep(4) 
            return response.text.replace('\n', '<br>')
        except Exception as e:
            if "429" in str(e): time.sleep(15)
            else: return "AI 伺服器忙線"
    return "⚠️ 系統節流跳過"

def get_robust_news(ticker_obj, ticker_symbol):
    news_items = []
    try:
        base_url = get_safe_url('aHR0cHM6Ly9uZXdzLmdvb2dsZS5jb20vcnNzL3NlYXJjaD9xPQ==')
        query = urllib.parse.quote(f"{ticker_symbol} stock")
        url = f"{base_url}{query}&hl=en-US&gl=US&ceid=US:en"
        response = cffi_requests.get(url, impersonate="chrome110", timeout=10)
        if response.status_code == 200:
            root = ET.fromstring(response.text)
            for item in root.findall('.//channel/item')[:5]:
                title = item.find('title').text.split(' - ')[0]
                news_items.append({'title': title, 'publisher': 'Google'})
            if news_items: return news_items
    except: pass
    return [] 

def get_nasdaq_100_tickers():
    try:
        url = get_safe_url('aHR0cHM6Ly9lbi53aWtpcGVkaWEub3JnL3dpa2kvTmFzZGFxLTEwMA==')
        response = cffi_requests.get(url, impersonate="chrome110", timeout=15)
        tables = pd.read_html(response.text)
        for table in tables:
            if 'Ticker' in table.columns: return table['Ticker'].tolist(), "✅ Nasdaq 100 成功"
    except: return ['AAPL', 'MSFT'], "⚠️ 抓取失敗"

def get_fundamental_sentiment_score(ticker_obj, ticker_symbol):
    score = 50
    latest_news_str = "無最新新聞" 
    sector = "未知板塊"
    days_to_earnings = "未知"
    news_list_raw = [] 
    upside_str = "-" 

    try:
        info = ticker_obj.info
        if not info: raise ValueError("無法獲取 info")

        sector = info.get('sector', '未知板塊')
        industry = info.get('industry', '')
        if industry: sector = f"{sector} ({industry})"

        earn_ts = info.get('earningsTimestamp')
        if earn_ts:
            earn_date = datetime.fromtimestamp(earn_ts)
            days = (earn_date.date() - datetime.today().date()).days
            if days >= 0:
                days_to_earnings = f"⚠️ {days}天後" if days <= 5 else f"{days}天後"
            else:
                days_to_earnings = "近期已發布"

        current_price = info.get('currentPrice', 0)
        target_price = info.get('targetMeanPrice', 0)
        
        if current_price and target_price and current_price > 0 and target_price > 0:
            upside = (target_price - current_price) / current_price
            if upside > 0:
                upside_str = f"<span style='color:#27ae60;font-weight:bold;'>+{round(upside*100, 1)}%</span>"
            else:
                upside_str = f"<span style='color:#c0392b;'>{round(upside*100, 1)}%</span>"
                
            if upside > 0.15: score += 15
            elif upside < 0: score -= 15

        rec = info.get('recommendationKey', '')
        if rec in ['buy', 'strong_buy']: score += 10
        elif rec in ['sell', 'strong_sell', 'underperform']: score -= 15

        bull_keywords = ['upgrade', 'beat', 'growth', 'surge', 'buy', 'higher', 'record']
        bear_keywords = ['downgrade', 'miss', 'cut', 'drop', 'lawsuit', 'sell', 'lower', 'weak']
        
        news_list_raw = get_robust_news(ticker_obj, ticker_symbol)
        if news_list_raw:
            news_score = 0
            news_display = [] 
            for i, article in enumerate(news_list_raw):
                title = article.get('title', '')
                if any(k in title.lower() for k in bull_keywords): news_score += 4
                if any(k in title.lower() for k in bear_keywords): news_score -= 5
                if i < 2 and title:
                    news_display.append(f"▪️ {title}")

            score += news_score
            if news_display: latest_news_str = "<br><br>".join(news_display)

        return max(0, min(100, score)), latest_news_str, sector, days_to_earnings, news_list_raw, upside_str
    except Exception as e:
        return 50, "數據異常", "未知", "未知", [], "-"

def get_investment_strategy(ticker_obj, current_price, score, signal_type, days_to_earnings):
    try:
        exp_dates = ticker_obj.options
        target_date = [d for d in exp_dates if 25 <= (datetime.strptime(d, '%Y-%m-%d') - datetime.today()).days <= 45][0]
        chain = ticker_obj.option_chain(target_date)
        strike = chain.puts[chain.puts['strike'] <= current_price * 0.9].sort_values(by='strike', ascending=False).iloc[0]['strike']
        return {'綜合建議': '🟢 可操作期權', '期權履約價': f"Put ${strike} ({target_date})", '年化報酬': '15%+'}
    except: return {'綜合建議': '⚪ 僅限正股操作', '期權履約價': '-', '年化報酬': '-'}

# ==========================================
# 🎯 狙擊手核心掃描與全天候 SOP (導入紅綠燈)
# ==========================================
def scan_market_opportunities(tickers_list, portfolio_list, hot_sectors_list, macro_signal):
    all_tickers = list(set(tickers_list + portfolio_list + hot_sectors_list))
    df_data = yf.download(all_tickers, period="3mo", progress=False) 
    df_closes, df_volumes = df_data['Close'], df_data['Volume']
    
    raw_candidates = []
    for ticker in df_closes.columns:
        try:
            prices = df_closes[ticker].dropna()
            if len(prices) < 20: continue
            
            delta = prices.diff()
            gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
            loss = -delta.clip(upper=0).ewm(alpha=1/14, adjust=False).mean()
            rsi = (100 - (100 / (1 + gain / loss))).replace([np.inf, -np.inf], np.nan).fillna(100)
            latest_rsi = float(rsi.iloc[-1])
            latest_price = float(prices.iloc[-1])
            bias_20 = ((latest_price - float(prices.rolling(20).mean().iloc[-1])) / float(prices.rolling(20).mean().iloc[-1])) * 100
            
            volumes = df_volumes[ticker].dropna()
            rvol, vol_surge = 0.0, False
            if len(volumes) >= 20:
                vol_ma20 = float(volumes.rolling(20).mean().iloc[-1])
                if pd.notna(vol_ma20) and vol_ma20 > 0:
                    rvol = round(float(volumes.iloc[-1]) / vol_ma20, 2)
                    vol_surge = rvol > 1.2 
            
            is_portfolio, is_hot = ticker in portfolio_list, ticker in hot_sectors_list
            signal_type = None
            
            if is_hot:
                if latest_rsi >= 75 and bias_20 >= 10: signal_type = "極端超買"
                elif latest_rsi > 65 and bias_20 > 5 and vol_surge: signal_type = "動能突破"
                elif 55 <= latest_rsi <= 65 and abs(bias_20) <= 5 and not vol_surge: signal_type = "高檔震盪"
                elif 40 <= latest_rsi < 55 and bias_20 > -3: signal_type = "強勢回檔"
                elif latest_rsi < 40: signal_type = "趨勢破壞"
                elif rvol > 1.5: signal_type = "板塊異動(爆量)"
            else:
                if latest_rsi < 35 and bias_20 < -6: signal_type = "超跌反彈"
                elif latest_rsi > 65 and bias_20 > 5 and vol_surge: signal_type = "動能突破"

            if is_portfolio and not signal_type: signal_type = "持倉監控"
            
            if signal_type:
                stock_obj = yf.Ticker(ticker)
                f_score, l_news, sector, d_earn, raw_news, upside_val = get_fundamental_sentiment_score(stock_obj, ticker)
                strat = get_investment_strategy(stock_obj, latest_price, f_score, signal_type, d_earn)
                
                if signal_type == "持倉監控": strat['綜合建議'] = "🔹 日常追蹤" if f_score >= 50 else "⚠️ 留意停損"

                identity = '💼 我的持倉' if is_portfolio else ('🔥 動態熱門板塊' if is_hot else '🔍 掃描發現')
                
                weight_multiplier = 20 if signal_type in ["強勢回檔", "動能突破", "極端超買"] else 10
                sort_weight = (1000 if is_portfolio else 0) + f_score + (rvol * weight_multiplier)
                
                raw_candidates.append({
                    'sort_weight': sort_weight, 'raw_news': raw_news,
                    'data': {
                        '身份': identity, '代碼': ticker, '型態': signal_type, '現價': round(latest_price, 2),
                        'Upside': upside_val, 
                        'RSI': round(latest_rsi, 2), '熱度(RVOL)': f"{rvol}x", '財報日': d_earn, '評分': int(f_score),
                        '綜合建議': strat['綜合建議'], '期權履約價': strat['期權履約價'], '最新新聞': l_news
                    }
                })
        except: continue

    raw_candidates.sort(key=lambda x: x['sort_weight'], reverse=True)
    
    ai_target_candidates = [
        item for item in raw_candidates 
        if item['data']['型態'] not in ["持倉監控", "高檔震盪", "趨勢破壞", "極端超買"]
    ][:5]
    
    print(f"\n🎯 系統篩選出 {len(raw_candidates)} 檔標的，啟動 AI 狙擊分析...")
    
    results = []
    for item in raw_candidates:
        stock = item['data']
        if item in ai_target_candidates:
            stock['🤖 AI 投研觀點'] = analyze_stock_with_ai(stock['代碼'], stock['型態'], stock['熱度(RVOL)'], item['raw_news'])
        else:
            stock['🤖 AI 投研觀點'] = "⏸️ 系統已記錄 / 無需 AI 介入"
        
        # 基礎 SOP 判定
        sop = "⚪ 觀望或依原定策略"
        if "高風險" in stock['🤖 AI 投研觀點'] or "⚠️" in stock['🤖 AI 投研觀點']: 
            sop = "🛑 AI 否決：風險過高，直接放棄。"
        elif "⚠️" in stock['財報日']: 
            sop = "🛑 財報將近：禁止 Sell Put 避免跳空。"
        elif "趨勢破壞" in stock['型態']: 
            sop = "🛑 趨勢破壞：熱門股轉弱，無條件避開或停損。"
        elif "極端超買" in stock['型態']: 
            sop = "⚠️ 情境D (乖離過大)：鎖定利潤，或建倉 Bear Call Spread。"
        elif "高檔震盪" in stock['型態']: 
            sop = "⏳ 情境E (橫盤收斂)：持有正股可賣 Covered Call 收租。"
        elif "強勢回檔" in stock['型態']: 
            sop = "🥇 情境A (熱門回檔)：建倉 Bull Put Spread。"
        elif "動能突破" in stock['型態'] or "板塊異動" in stock['型態']: 
            sop = "🥈 情境B (帶量突破)：買入正股並設嚴格停損。"
        elif "超跌反彈" in stock['型態']: 
            sop = "🥉 情境C (優質錯殺)：保守 Sell Put 收租。"
            
        # 🚦 大盤紅綠燈強制覆寫 (Override Mechanism)
        if macro_signal == "RED":
            if "情境A" in sop or "情境B" in sop or "情境C" in
