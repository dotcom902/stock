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
import re  
from google import genai  
from curl_cffi import requests as cffi_requests

warnings.filterwarnings('ignore')

def get_safe_url(b64_str):
    return base64.b64decode(b64_str).decode('utf-8')

# ==========================================
# 🔑 API Keys (從 GitHub Secrets 獲取)
# ==========================================
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
FRED_API_KEY = os.environ.get("FRED_API_KEY") # ✨ 新增：FRED 總經數據 API
AI_MODEL_NAME = 'gemini-2.5-flash'  

ai_client = None
if GEMINI_API_KEY:
    ai_client = genai.Client(api_key=GEMINI_API_KEY)

DAILY_QUOTA_EXHAUSTED = False

# CRWV 已依據先前的修正紀錄更新
MY_PORTFOLIO = ['NVDA', 'TSM', 'CRWV', 'PLTR', 'MSTR','MU','INTC'] 

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
# 🚦 升級版：宏觀紅綠燈 (FRED 流動性 + SMH/VIX/QQQ)
# ==========================================
def fetch_fred_data(series_id, limit=20):
    if not FRED_API_KEY: return []
    try:
        url = "https://api.stlouisfed.org/fred/series/observations"
        params = {
            'series_id': series_id,
            'api_key': FRED_API_KEY,
            'file_type': 'json',
            'sort_order': 'desc',
            'limit': limit
        }
        res = requests.get(url, params=params, timeout=10)
        if res.status_code == 200:
            data = res.json()
            # 排除 FRED 假日空值 '.'
            return [float(x['value']) for x in data['observations'] if x['value'] != '.']
    except Exception as e:
        print(f"⚠️ FRED 抓取 {series_id} 失敗: {e}")
    return []

def get_macro_regime():
    try:
        print("🌍 正在掃描總經指標 (FRED 利率/利差) 與 大盤動能 (QQQ/SMH/VIX)...")
        
        # 1. 抓取大盤與半導體 ETF
        df_data = yf.download(['QQQ', 'SMH', '^VIX'], period="3mo", progress=False)
        df_closes = df_data['Close']
        
        # 計算 QQQ RSI
        qqq_prices = df_closes['QQQ'].dropna()
        delta_q = qqq_prices.diff()
        gain_q = delta_q.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
        loss_q = -delta_q.clip(upper=0).ewm(alpha=1/14, adjust=False).mean()
        latest_qqq_rsi = float((100 - (100 / (1 + gain_q / loss_q))).fillna(100).iloc[-1])
        
        # ✨ 新增：計算 SMH (半導體) RSI
        smh_prices = df_closes['SMH'].dropna()
        delta_s = smh_prices.diff()
        gain_s = delta_s.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
        loss_s = -delta_s.clip(upper=0).ewm(alpha=1/14, adjust=False).mean()
        latest_smh_rsi = float((100 - (100 / (1 + gain_s / loss_s))).fillna(100).iloc[-1])

        # VIX
        vix_prices = df_closes['^VIX'].ffill().dropna()
        latest_vix = float(vix_prices.iloc[-1]) if len(vix_prices) > 0 else 20.0

        # 2. 抓取 FRED 總經數據
        fred_str = "▪️ 總經流動性: 未設定 FRED API 金鑰，略過分析"
        fred_red_flag = False
        fred_yellow_flag = False
        
        if FRED_API_KEY:
            yields = fetch_fred_data('DGS10')        # 10年期美債殖利率
            spreads = fetch_fred_data('BAMLH0A0HYM2') # 高收益債信用利差 (恐慌度)
            
            if yields and spreads and len(yields) >= 10 and len(spreads) >= 10:
                cur_yield = yields[0]
                ma10_yield = sum(yields[:10]) / 10
                cur_spread = spreads[0]
                ma10_spread = sum(spreads[:10]) / 10
                
                fred_str = f"▪️ 10年美債殖利率: <b>{cur_yield:.2f}%</b> (10日均: {ma10_yield:.2f}%)<br>"
                fred_str += f"▪️ 信用利差恐慌度: <b>{cur_spread:.2f}%</b> (10日均: {ma10_spread:.2f}%)"
                
                # 總經防禦邏輯
                if cur_spread > ma10_spread * 1.15 or cur_yield > ma10_yield * 1.08:
                    fred_red_flag = True
                elif cur_spread > ma10_spread * 1.05 or cur_yield > ma10_yield * 1.03:
                    fred_yellow_flag = True

        # 3. 綜合信號判定
        signal = "UNKNOWN"
        if latest_vix > 25 or latest_qqq_rsi < 30 or fred_red_flag:
            status_icon = "🔴"
            status_text = "紅燈 (Risk-Off / 總經風險蔓延)"
            signal = "RED"
        elif fred_yellow_flag or latest_qqq_rsi > 75:
            status_icon = "🟡"
            status_text = "黃燈 (高檔警報 / 資金成本上揚)"
            signal = "YELLOW"
        elif latest_smh_rsi < 40:
            status_icon = "🟠"
            status_text = "橘燈 (AI/半導體族群動能轉弱)"
            signal = "ORANGE"
        else:
            status_icon = "🟢"
            status_text = "綠燈 (Risk-On / 總經與流動性健康)"
            signal = "GREEN"
            
        desc = f"""
        <span style='font-size:16px;'>{status_icon}</span> <b>{status_text}</b> <br>
        ▪️ VIX 恐慌指數: <b>{latest_vix:.2f}</b> <br>
        ▪️ QQQ 日線 RSI: <b>{latest_qqq_rsi:.2f}</b> | SMH 日線 RSI: <b>{latest_smh_rsi:.2f}</b> <br>
        {fred_str}
        """
        return desc, signal
        
    except Exception as e:
        print(f"⚠️ 宏觀指標抓取失敗: {e}")
        return "大盤狀態: 未知", "UNKNOWN"

# ==========================================
# 📊 AI 美股收盤總結與資金流向分析
# ==========================================
def get_market_closing_summary(hot_sectors_desc):
    try:
        print("📰 正在生成美股收盤總結與資金流向報告...")
        indices = {'S&P 500': '^GSPC', 'Nasdaq 100': '^NDX', 'Dow Jones': '^DJI', 'Russell 2000 (中小盤)': 'IWM'}
        df_indices = yf.download(list(indices.values()), period="5d", progress=False)['Close']
        
        index_str = ""
        for name, ticker in indices.items():
            if ticker in df_indices:
                closes = df_indices[ticker].dropna()
                if len(closes) >= 2:
                    today_c = float(closes.iloc[-1])
                    yest_c = float(closes.iloc[-2])
                    pct_change = ((today_c - yest_c) / yest_c) * 100
                    icon = "🟢" if pct_change > 0 else "🔴"
                    index_str += f"{name}: {today_c:.2f} ({icon} {pct_change:+.2f}%)\n"
        
        if not ai_client or DAILY_QUOTA_EXHAUSTED:
            return f"<div style='padding:10px; background:#f0f0f0;'>{index_str.replace(chr(10), '<br>')}</div>"

        today_str = datetime.today().strftime('%Y-%m-%d')
        prompt = f"""
        現在是 {today_str}（美股收盤後）。你是華爾街頂級避險基金的首席策略師。
        以下是今日美股四大指數的收盤表現：
        {index_str}
        今日我們的 AI 量化系統偵測到「資金異常湧入」的熱門板塊為：{hot_sectors_desc}
        
        請寫一段約 100-150 字的「美股收盤總結與資金流向」速報，供交易員晨會閱讀。
        要求：冷靜犀利，精準指出資金流向。純文本，使用 <b> 加粗關鍵字。不要使用 ``` 標記包裝。
        """
        
        response = ai_client.models.generate_content(model=AI_MODEL_NAME, contents=prompt)
        summary_text = response.text.replace('\n', '<br>')
        
        html_output = f"""
        <div style="background-color: #e8f4f8; border-left: 5px solid #2980b9; padding: 15px; margin-bottom: 20px; border-radius: 5px;">
            <h3 style="margin-top:0; color:#2c3e50;">📰 華爾街收盤總結 & 資金流向</h3>
            <div style="font-family: monospace; font-size: 14px; margin-bottom: 12px; background: #fff; padding: 10px; border-radius: 4px; border: 1px solid #bdc3c7;">
                {index_str.replace(chr(10), '<br>')}
            </div>
            <div style="font-size: 14px; color: #34495e; line-height: 1.6;">
                {summary_text}
            </div>
        </div>
        """
        return html_output
    except Exception as e: return ""

# ==========================================
# 🧠 AI 熱門板塊動態偵測 
# ==========================================
def get_ai_dynamic_sectors(max_retries=3):
    global DAILY_QUOTA_EXHAUSTED
    fallback_tickers = ['MARA', 'IREN', 'SYM', 'PATH', 'CRWD']
    fallback_desc = "AI 應用, 區塊鏈, 網路安全 (備用預設)"

    if not ai_client or DAILY_QUOTA_EXHAUSTED: return fallback_tickers, fallback_desc

    today_str = datetime.today().strftime('%Y-%m-%d')
    prompt = f"""
    現在時間是 {today_str}。你是「板塊輪動與資金流向量化分析師」。
    請選出「本週資金最集中、最具爆發力的 3 個『細分』產業板塊」(如: 矽光子、液冷散熱、AI PC)。
    為這 3 個板塊各挑選 3~4 檔市值 > 20億美元的美股代碼。
    請只輸出 JSON 格式：
    {{"sector_names": "板塊A, 板塊B, 板塊C", "tickers": ["代碼1", "代碼2", "代碼3"]}}
    """
    
    backoff_time = 5
    for attempt in range(max_retries):
        try:
            print(f"🧠 正在請 AI 偵測今日熱門板塊... (嘗試 {attempt+1}/{max_retries})")
            response = ai_client.models.generate_content(model=AI_MODEL_NAME, contents=prompt)
            json_match = re.search(r'\{.*\}', response.text, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group(0))
                valid_tickers = [t.strip().upper() for t in data.get("tickers", fallback_tickers) if t.strip().isalpha() and len(t.strip()) <= 5]
                return valid_tickers, data.get("sector_names", fallback_desc)
        except Exception as e:
            if "429" in str(e) or "503" in str(e):
                time.sleep(backoff_time); backoff_time *= 2
            else: return fallback_tickers, fallback_desc
    return fallback_tickers, fallback_desc

# ==========================================
# 🤖 AI 個股投研分析 (✨ 升級：注入 CapEx 與產業邏輯)
# ==========================================
def analyze_stock_with_ai(ticker, sector_name, signal_type, rvol, news_list_raw, max_retries=4):
    global DAILY_QUOTA_EXHAUSTED
    if DAILY_QUOTA_EXHAUSTED or not ai_client: return "⚠️ AI 暫停分析"
    
    news_text = "\n".join([f"- {item.get('title', '')}" for item in news_list_raw[:5]])
    
    # ✨ 升級提示詞，加入 CSP 資本支出的考量
    prompt = f"""你是華爾街交易員。{ticker} (所屬板塊: {sector_name}) 觸發了 {signal_type}，量能為 {rvol}。
    最新新聞：{news_text}
    
    量化法則：
    1. 超跌/強勢回檔：大盤錯殺則【可以建倉】。
    2. 動能突破/軋空：實質利多（如雲端巨頭 CSP 增加資本支出 CapEx）則【可以建倉】或【持有】。
    3. 極端超買/高檔震盪：提示風險，結論為【鎖定利潤】或【觀望收租】。
    4. 結構/趨勢破壞：一律【高風險避開】。
    
    請結合該公司的「所屬板塊屬性」與「技術型態」給出 50 字內理由。結論：【上述四者擇一】。"""
    
    backoff_time = 5  
    for attempt in range(max_retries):
        try:
            response = ai_client.models.generate_content(model=AI_MODEL_NAME, contents=prompt)
            time.sleep(3) 
            return response.text.replace('\n', '<br>')
        except Exception as e:
            if "429" in str(e).lower() or "503" in str(e).lower():
                time.sleep(backoff_time); backoff_time *= 2 
            else: return f"⚠️ 伺服器異常或模型錯誤"
    DAILY_QUOTA_EXHAUSTED = True
    return "⚠️ AI 伺服器持續擁塞"

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
    score, latest_news_str, sector, days_to_earnings, news_list_raw, upside_str = 50, "無最新新聞", "未知板塊", "未知", [], "-"
    try:
        info = ticker_obj.info
        if not info: raise ValueError()

        fetched_industry = info.get('industry', '')
        fetched_sector = info.get('sector', '')
        sector = fetched_industry if fetched_industry else (fetched_sector if fetched_sector else "未知板塊")

        earn_ts = info.get('earningsTimestamp')
        if earn_ts:
            days = (datetime.fromtimestamp(earn_ts).date() - datetime.today().date()).days
            days_to_earnings = f"⚠️ {days}天後" if days >= 0 and days <= 30 else (f"{days}天後" if days > 30 else "近期已發布")

        current_price = info.get('currentPrice', 0)
        target_price = info.get('targetMeanPrice', 0)
        
        if current_price and target_price and current_price > 0 and target_price > 0:
            upside = (target_price - current_price) / current_price
            upside_str = f"<span style='color:#27ae60;font-weight:bold;'>+{round(upside*100, 1)}%</span>" if upside > 0 else f"<span style='color:#c0392b;'>{round(upside*100, 1)}%</span>"
            if upside > 0.15: score += 15
            elif upside < 0: score -= 15

        rec = info.get('recommendationKey', '')
        if rec in ['buy', 'strong_buy']: score += 10
        elif rec in ['sell', 'strong_sell', 'underperform']: score -= 15

        bull_keywords, bear_keywords = ['upgrade', 'beat', 'growth', 'surge', 'buy', 'higher', 'record'], ['downgrade', 'miss', 'cut', 'drop', 'lawsuit', 'sell', 'lower', 'weak']
        
        news_list_raw = get_robust_news(ticker_obj, ticker_symbol)
        if news_list_raw:
            news_score = 0
            news_display = [] 
            for i, article in enumerate(news_list_raw):
                title = article.get('title', '')
                if any(k in title.lower() for k in bull_keywords): news_score += 4
                if any(k in title.lower() for k in bear_keywords): news_score -= 5
                if i < 2 and title: news_display.append(f"▪️ {title}")
            score += news_score
            if news_display: latest_news_str = "<br><br>".join(news_display)

        return max(0, min(100, score)), latest_news_str, sector, days_to_earnings, news_list_raw, upside_str
    except: return 50, "數據異常", "未知", "未知", [], "-"

def get_investment_strategy(ticker_obj, current_price, score, signal_type, days_to_earnings):
    try:
        exp_dates = ticker_obj.options
        if not exp_dates: raise ValueError()
            
        valid_dates = [d for d in exp_dates if 25 <= (datetime.strptime(d, '%Y-%m-%d') - datetime.today()).days <= 45]
        target_date = valid_dates[0] if valid_dates else exp_dates[0]
        
        chain = ticker_obj.option_chain(target_date)
        target_puts = chain.puts[chain.puts['strike'] <= current_price * 0.9].sort_values(by='strike', ascending=False)
        
        if not target_puts.empty:
            selected_put = target_puts.iloc[0]
            strike = selected_put['strike']
            iv = selected_put['impliedVolatility']
            
            if pd.notna(iv) and iv > 0:
                iv_str = f"{iv*100:.1f}%"
                if iv > 0.6: iv_status = f"<span style='color:#c0392b;font-weight:bold;'>{iv_str} (🔥偏高)</span>"
                elif iv < 0.35: iv_status = f"<span style='color:#2980b9;'>{iv_str} (🧊偏低)</span>"
                else: iv_status = f"{iv_str} (適中)"
            else: iv_status = "-"

            return {'綜合建議': '🟢 可操作期權', '期權履約價': f"Put ${strike} ({target_date})", '當前 IV': iv_status}
    except: pass
    return {'綜合建議': '⚪ 僅限正股操作', '期權履約價': '-', '當前 IV': '-'}

# ==========================================
# 🎯 核心掃描 (包含 IV 與總經 SOP 調整)
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
            ma5 = float(prices.rolling(5).mean().iloc[-1])
            
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
                if latest_rsi >= 75: signal_type = "高檔鈍化(軋空)" if latest_price > ma5 else "極端超買(破線)"
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
                
                weight_multiplier = 20 if signal_type in ["強勢回檔", "動能突破", "高檔鈍化(軋空)"] else 10
                sort_weight = (1000 if is_portfolio else 0) + f_score + (rvol * weight_multiplier)
                
                sector_short = sector[:14] + ".." if len(sector) > 14 else sector
                if is_hot and rvol > 1.5: hot_label = "<span style='color:#c0392b; font-weight:bold;'>🔥 極熱 (資金湧入)</span>"
                elif is_hot: hot_label = "<span style='color:#e67e22; font-weight:bold;'>🔥 熱門板塊</span>"
                elif is_portfolio: hot_label = "<span style='color:#2980b9; font-weight:bold;'>💼 我的持倉</span>"
                else: hot_label = "<span style='color:#7f8c8d;'>🔍 異動掃描</span>"
                    
                rvol_str = f"<span style='color:#c0392b; font-weight:bold;'>RVOL: {rvol}x (爆量)</span>" if rvol > 1.5 else f"RVOL: {rvol}x"
                
                raw_candidates.append({
                    'sort_weight': sort_weight, 'raw_news': raw_news, 'sector_raw': sector,
                    'data': {
                        '代碼': f"<b>{ticker}</b>", 
                        '板塊與熱度': f"<b>{sector_short}</b><br>{hot_label}<br>{rvol_str}",
                        '型態': signal_type, '現價': round(latest_price, 2),
                        'Upside': upside_val, 'RSI': round(latest_rsi, 2), '財報日': d_earn, 
                        '當前 IV': strat['當前 IV'], '評分': int(f_score),
                        '綜合建議': strat['綜合建議'], '期權履約價': strat['期權履約價'], '最新新聞': l_news
                    }
                })
        except: continue

    raw_candidates.sort(key=lambda x: x['sort_weight'], reverse=True)
    
    ai_target_candidates = [item for item in raw_candidates if item['data']['型態'] not in ["持倉監控", "高檔震盪", "趨勢破壞", "極端超買(破線)"]][:8]
    print(f"\n🎯 系統篩選出 {len(raw_candidates)} 檔標的，啟動 AI 狙擊分析...")
    
    results = []
    for item in raw_candidates:
        stock = item['data']
        if item in ai_target_candidates:
            stock['🤖 AI 投研觀點'] = analyze_stock_with_ai(stock['代碼'], item['sector_raw'], stock['型態'], stock['板塊與熱度'], item['raw_news'])
        else:
            stock['🤖 AI 投研觀點'] = "⏸️ 系統已記錄 / 無需 AI 介入"
        
        sop = "⚪ 觀望或依原定策略"
        if "高風險" in stock['🤖 AI 投研觀點'] or "⚠️" in stock['🤖 AI 投研觀點']: sop = "🛑 AI 否決：風險過高，直接放棄。"
        elif "⚠️" in stock['財報日']: sop = "🛑 財報將近：禁止 Sell Put 避免跳空。"
        elif "趨勢破壞" in stock['型態']: sop = "🛑 趨勢破壞：熱門股轉弱，無條件避開或停損（買入 Put 避險）。"
        elif "高檔鈍化" in stock['型態']: sop = "🔥 情境D (主升段軋空)：RSI已鈍化。絕對禁止做空！持有正股並以 5 日均線作移動停損。"
        elif "極端超買" in stock['型態']: sop = "⚠️ 情境E (超買且破線)：高檔跌破 5 日線。鎖定利潤，或建倉 Bear Call Spread。"
        elif "高檔震盪" in stock['型態']: sop = "⏳ 情境F (橫盤收斂)：持有正股可賣 Covered Call 收租降低成本。"
        elif "強勢回檔" in stock['型態']: sop = "🥇 情境A (熱門回檔)：建倉 Bull Put Spread / Sell Put。"
        elif "動能突破" in stock['型態'] or "板塊異動" in stock['型態']: sop = "🥈 情境B (帶量突破)：買入正股並設嚴格停損 / 買入 Call。"
        elif "超跌反彈" in stock['型態']: sop = "🥉 情境C (優質錯殺)：保守 Sell Put 收租。"
            
        # 結合最新 FRED 宏觀信號
        if macro_signal == "RED":
            if any(s in sop for s in ["情境A", "情境B", "情境C"]):
                sop = "🛑 <b>[紅燈警戒]</b> 流動性緊縮/恐慌蔓延。禁止所有做多建倉！僅限觀望或買入 Put 避險。"
        elif macro_signal == "YELLOW" or macro_signal == "ORANGE":
            if "情境B" in sop:
                sop = "⚠️ <b>[黃燈/橘燈警戒]</b> 大盤過熱或晶片轉弱。禁止突破追高！改為鎖定利潤或觀望。"
        elif macro_signal == "GREEN":
            if any(s in sop for s in ["情境A", "情境B"]):
                sop += " <br><span style='color:#27ae60;'><b>(🟢 綠燈加持：總經健康，允許標準佈局)</b></span>"
                
        # 結合波動率 (IV) 策略組合
        iv_status_str = stock.get('當前 IV', '')
        if "🔥" in iv_status_str:
            if any(s in sop for s in ["情境A", "情境C", "情境F", "持倉監控"]):
                 sop += "<br><span style='color:#c0392b;'><b>(🔥 IV 高：權金極肥，強烈建議做賣方 Sell Put / Covered Call 收租)</b></span>"
            elif any(s in sop for s in ["情境B", "情境D"]):
                 sop += "<br><span style='color:#c0392b;'><b>(🔥 IV 高：嚴防 IV Crush，禁止單買 Call，改買正股或做價差)</b></span>"
        elif "🧊" in iv_status_str:
            if any(s in sop for s in ["情境B", "情境A"]):
                 sop += "<br><span style='color:#2980b9;'><b>(🧊 IV 低：選擇權便宜，適合直接買入 Long Call 參與突破)</b></span>"

        stock['🎯 SOP 操作提示'] = sop
        results.append(stock)
    return pd.DataFrame(results)

def send_scan_report_mail(subject, body, to_emails_str, from_email, app_password):
    recipient_list = [e.strip() for e in to_emails_str.split(',') if e.strip()]
    msg = MIMEMultipart()
    msg['From'] = from_email
    msg['To'] = to_emails_str
    msg['Subject'] = subject
    msg.attach(MIMEText(f"<html><body>{body}</body></html>", 'html'))
    
    try:
        with smtplib.SMTP('smtp.gmail.com', 587) as server:
            server.starttls()
            server.login(from_email, app_password)
            server.sendmail(from_email, recipient_list, msg.as_string())
        print(f"📧 報告已成功寄送至 {len(recipient_list)} 位收件人!")
    except Exception as e: print(f"❌ 寄信失敗: {e}")

if __name__ == "__main__":
    macro_desc, macro_signal = get_macro_regime()
    ndx, msg = get_nasdaq_100_tickers()
    hot_tickers, hot_desc = get_ai_dynamic_sectors()
    market_summary_html = get_market_closing_summary(hot_desc)
    
    combined_hot_sectors_list = list(set(hot_tickers + CORE_WATCHLIST))
    target_df = scan_market_opportunities(ndx, MY_PORTFOLIO, combined_hot_sectors_list, macro_signal)
    
    if not target_df.empty:
        html_table = target_df.to_html(index=False, escape=False)
        
        html_style = """
        <style>
            body { font-family: 'Segoe UI', Arial, sans-serif; color: #2c3e50; }
            .status-box { background-color: #ecf0f1; border-left: 4px solid #2c3e50; padding: 15px; margin-bottom: 20px; font-size: 14px; line-height: 1.6; border-radius: 4px; }
            table { border-collapse: collapse; width: 100%; font-size: 12px; table-layout: fixed; }
            th { background-color: #2c3e50; color: white; padding: 10px 5px; text-align: center; }
            td { border: 1px solid #bdc3c7; padding: 8px 5px; text-align: center; word-wrap: break-word; vertical-align: middle; }
            tr:nth-child(even) { background-color: #f8f9fa; }
            th:nth-last-child(2), th:last-child { width: 15%; }
            th:last-child { background-color: #d35400; }
            td:nth-last-child(3), td:nth-last-child(2), td:last-child { text-align: left; font-size: 11px; line-height: 1.4; }
        </style>
        """
        
        status_html = f"""
        <div class='status-box'>
            <h3 style="margin-top:0; color:#2c3e50;">📊 狙擊手儀表板：宏觀環境紅綠燈</h3>
            {macro_desc}
            <hr style="border-top: 1px dashed #bdc3c7; margin: 10px 0;">
            💼 持倉監控：{len(MY_PORTFOLIO)} 檔 <br>
            🎯 核心必掃雷達：啟動 (光通信/半導體/太空)<br>
            🔥 AI 動態板塊：<b>{hot_desc}</b>
        </div>
        """
        
        sop_reminder_html = """
        <div style="background-color: #fdfbf7; border: 1px solid #e8e0d5; padding: 15px; margin-top: 25px; border-radius: 5px;">
            <h3 style="color: #d35400; margin-top: 0;">🛡️ 宏觀防禦與 SOP 實戰鐵律</h3>
            <ul style="font-size: 13px; color: #444; line-height: 1.6;">
                <li><b>🔴 紅燈 (FRED殖利率/利差飆升 或 VIX>25)：</b>無條件沒收做多買點，嚴禁抄底接刀，考慮買入 Put 避險。</li>
                <li><b>🟡 黃/橘燈 (大盤貪婪 或 半導體 SMH 轉弱)：</b>取消動能突破追高策略，僅限收租或部位減碼。</li>
                <li><b>🟢 綠燈 (情緒與流動性穩定)：</b>允許全功率執行 🥇情境A(強勢回檔) 與 🥈情境B(動能突破)。</li>
                <li><b>🔥 IV 偏高策略：</b>權利金極度昂貴，絕對禁止單買期權 (Long Call/Put)，強烈建議當賣方 (Sell Put / Covered Call) 收租。</li>
                <li><b>🧊 IV 偏低策略：</b>選擇權便宜，遇到技術突破時，非常適合直接單買 Call (Long Call) 博取超額報酬。</li>
                <li><b>🛑 絕對避開：</b>趨勢破壞 (RSI<40)、AI 判定風險、財報 30 天內，無條件空手觀望。</li>
            </ul>
        </div>
        """
        
        html_table = html_table.replace('🥇', '<span style="color:#d35400; font-weight:bold;">🥇</span>')
        html_table = html_table.replace('🥈', '<span style="color:#2980b9; font-weight:bold;">🥈</span>')
        html_table = html_table.replace('🥉', '<span style="color:#8e44ad; font-weight:bold;">🥉</span>')
        html_table = html_table.replace('🔥', '<span style="color:#c0392b; font-weight:bold;">🔥</span>')
        html_table = html_table.replace('🛑', '<span style="color:#c0392b; font-weight:bold;">🛑</span>')
        html_table = html_table.replace('⚠️', '<span style="color:#e67e22; font-weight:bold;">⚠️</span>')

        body = f"{html_style}{status_html}{market_summary_html}<h2>🎯 發現 {len(target_df)} 檔異動標的：</h2>{html_table}{sop_reminder_html}"
        
        to_email = os.environ.get("MAIL_TO")
        from_email = os.environ.get("MAIL_USER")
        app_pass = os.environ.get("MAIL_PASS")
        
        if to_email and from_email and app_pass:
            send_scan_report_mail(f"🚦 量化早報 ({datetime.today().strftime('%m/%d')}) - {macro_signal}", body, to_email, from_email, app_pass)
        else:
            print("❌ 找不到寄件人信箱設定，請檢查環境變數！")
