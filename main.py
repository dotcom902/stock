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
# ✅ 使用最新 2.5 Flash 混合推理模型
AI_MODEL_NAME = 'gemini-2.5-flash'  

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
    try:
        print("🌍 正在掃描總體經濟宏觀指標 (QQQ & VIX)...")
        df_data = yf.download(['QQQ', '^VIX'], period="3mo", progress=False)
        
        if df_data.empty or 'Close' not in df_data:
            return "大盤狀態: 未知", "UNKNOWN"
            
        df_closes = df_data['Close']
        
        qqq_prices = df_closes['QQQ'].dropna()
        if len(qqq_prices) < 20: return "大盤狀態: 數據不足", "UNKNOWN"
        
        delta = qqq_prices.diff()
        gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
        loss = -delta.clip(upper=0).ewm(alpha=1/14, adjust=False).mean()
        qqq_rsi = (100 - (100 / (1 + gain / loss))).fillna(100)
        latest_qqq_rsi = float(qqq_rsi.iloc[-1])
        
        vix_prices = df_closes['^VIX'].dropna()
        latest_vix = float(vix_prices.iloc[-1])
        
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
# 🧠 AI 熱門板塊動態偵測 (⚡ 支援 503 重試)
# ==========================================
def get_ai_dynamic_sectors(max_retries=3):
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
    backoff_time = 5
    for attempt in range(max_retries):
        try:
            print(f"🧠 正在請 AI 偵測今日熱門板塊... (嘗試 {attempt+1}/{max_retries})")
            response = ai_client.models.generate_content(model=AI_MODEL_NAME, contents=prompt)
            data = json.loads(response.text.replace('```json', '').replace('```', '').strip())
            return [t.strip().upper() for t in data.get("tickers", fallback_tickers)], data.get("sector_names", fallback_desc)
        except Exception as e:
            error_msg = str(e).lower()
            if "429" in error_msg or "503" in error_msg or "quota" in error_msg:
                print(f"⏳ AI 板塊掃描觸發伺服器擁塞 (429/503)，等待 {backoff_time} 秒...")
                time.sleep(backoff_time)
                backoff_time *= 2
            else:
                return fallback_tickers, fallback_desc
    return fallback_tickers, fallback_desc

# ==========================================
# 🤖 AI 個股投研分析 (⚡ 支援 503 重試)
# ==========================================
def analyze_stock_with_ai(ticker, signal_type, rvol, news_list_raw, max_retries=4):
    global DAILY_QUOTA_EXHAUSTED
    if DAILY_QUOTA_EXHAUSTED or not ai_client: return "⚠️ AI 暫停分析"
    
    news_text = "\n".join([f"- {item.get('title', '')}" for item in news_list_raw[:5]])
    prompt = f"""你是華爾街交易員。{ticker} 觸發 {signal_type}，RVOL {rvol}。新聞：{news_text}
    量化法則：
    1. 超跌/強勢回檔：大盤錯殺則【可以建倉】。
    2. 動能突破/軋空：實質利多則【可以建倉】或【持有】。
    3. 極端超買/高檔震盪：提示風險，結論為【鎖定利潤】或【觀望收租】。
    4. 結構/趨勢破壞：一律【高風險避開】。
    格式：50字內理由。結論：【上述四者擇一】。"""
    
    backoff_time = 5  
    
    for attempt in range(max_retries):
        try:
            response = ai_client.models.generate_content(model=AI_MODEL_NAME, contents=prompt)
            time.sleep(3) 
            return response.text.replace('\n', '<br>')
        except Exception as e:
            error_msg = str(e).lower()
            if "429" in error_msg or "quota" in error_msg or "503" in error_msg:
                print(f"⏳ {ticker} 觸發 AI 伺服器忙線 (429/503)，等待 {backoff_time} 秒... (嘗試 {attempt+1}/{max_retries})")
                time.sleep(backoff_time)
                backoff_time *= 2 
            else:
                return f"⚠️ 伺服器異常或模型錯誤: {str(e)[:25]}"
                
    DAILY_QUOTA_EXHAUSTED = True
    return "⚠️ AI 伺服器持續擁塞 (已達重試上限)"

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

# ==========================================
# 🎯 提取期權策略與 IV 數據
# ==========================================
def get_investment_strategy(ticker_obj, current_price, score, signal_type, days_to_earnings):
    try:
        exp_dates = ticker_obj.options
        if not exp_dates:
            raise ValueError("無期權數據")
            
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
                if iv > 0.6:
                    iv_status = f"<span style='color:#c0392b;font-weight:bold;'>{iv_str} (🔥偏高)</span>"
                elif iv < 0.35:
                    iv_status = f"<span style='color:#2980b9;'>{iv_str} (🧊偏低)</span>"
                else:
                    iv_status = f"{iv_str} (適中)"
            else:
                iv_status = "-"

            return {
                '綜合建議': '🟢 可操作期權', 
                '期權履約價': f"Put ${strike} ({target_date})", 
                '當前 IV': iv_status
            }
    except Exception as e: 
        pass
        
    return {'綜合建議': '⚪ 僅限正股操作', '期權履約價': '-', '當前 IV': '-'}

# ==========================================
# 🎯 狙擊手核心掃描與全天候 SOP (導入 IV、紅綠燈、RSI鈍化修復)
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
            
            # ✨ 新增 5 日均線作為「鈍化濾網」
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
                # ✨ RSI 鈍化與破線邏輯升級
                if latest_rsi >= 75:
                    if latest_price > ma5:
                        signal_type = "高檔鈍化(軋空)"  # RSI 極高但死守 5 日線，強勢主升段
                    else:
                        signal_type = "極端超買(破線)"  # RSI 極高且跌破 5 日線，動能竭盡
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
                
                weight_multiplier = 20 if signal_type in ["強勢回檔", "動能突破", "高檔鈍化(軋空)"] else 10
                sort_weight = (1000 if is_portfolio else 0) + f_score + (rvol * weight_multiplier)
                
                raw_candidates.append({
                    'sort_weight': sort_weight, 'raw_news': raw_news,
                    'data': {
                        '身份': identity, '代碼': ticker, '型態': signal_type, '現價': round(latest_price, 2),
                        'Upside': upside_val, 
                        'RSI': round(latest_rsi, 2), '熱度(RVOL)': f"{rvol}x", '財報日': d_earn, 
                        '當前 IV': strat['當前 IV'],
                        '評分': int(f_score),
                        '綜合建議': strat['綜合建議'], '期權履約價': strat['期權履約價'], '最新新聞': l_news
                    }
                })
        except: continue

    raw_candidates.sort(key=lambda x: x['sort_weight'], reverse=True)
    
    # 過濾掉不需要 AI 深入分析的標的
    ai_target_candidates = [
        item for item in raw_candidates 
        if item['data']['型態'] not in ["持倉監控", "高檔震盪", "趨勢破壞", "極端超買(破線)"]
    ][:5]
    
    print(f"\n🎯 系統篩選出 {len(raw_candidates)} 檔標的，啟動 AI 狙擊分析...")
    
    results = []
    for item in raw_candidates:
        stock = item['data']
        if item in ai_target_candidates:
            stock['🤖 AI 投研觀點'] = analyze_stock_with_ai(stock['代碼'], stock['型態'], stock['熱度(RVOL)'], item['raw_news'])
        else:
            stock['🤖 AI 投研觀點'] = "⏸️ 系統已記錄 / 無需 AI 介入"
        
        sop = "⚪ 觀望或依原定策略"
        if "高風險" in stock['🤖 AI 投研觀點'] or "⚠️" in stock['🤖 AI 投研觀點']: 
            sop = "🛑 AI 否決：風險過高，直接放棄。"
        elif "⚠️" in stock['財報日']: 
            sop = "🛑 財報將近：禁止 Sell Put 避免跳空。"
        elif "趨勢破壞" in stock['型態']: 
            sop = "🛑 趨勢破壞：熱門股轉弱，無條件避開或停損。"
        # ✨ 新增鈍化與破線的 SOP 判斷
        elif "高檔鈍化" in stock['型態']: 
            sop = "🔥 情境D (主升段軋空)：RSI已鈍化。絕對禁止做空！持有正股並以 5 日均線作移動停損。"
        elif "極端超買" in stock['型態']: 
            sop = "⚠️ 情境E (超買且破線)：高檔跌破 5 日線，動能竭盡。鎖定利潤，或建倉 Bear Call Spread。"
        elif "高檔震盪" in stock['型態']: 
            sop = "⏳ 情境F (橫盤收斂)：持有正股可賣 Covered Call 收租。"
        elif "強勢回檔" in stock['型態']: 
            sop = "🥇 情境A (熱門回檔)：建倉 Bull Put Spread。"
        elif "動能突破" in stock['型態'] or "板塊異動" in stock['型態']: 
            sop = "🥈 情境B (帶量突破)：買入正股並設嚴格停損。"
        elif "超跌反彈" in stock['型態']: 
            sop = "🥉 情境C (優質錯殺)：保守 Sell Put 收租。"
            
        # 🚦 大盤紅綠燈強制覆寫 (Override Mechanism)
        if macro_signal == "RED":
            if "情境A" in sop or "情境B" in sop or "情境C" in sop:
                sop = "🛑 <b>[紅燈警戒]</b> 系統性風險/恐慌蔓延。禁止所有做多建倉！僅限觀望或買入 Put 避險。"
        elif macro_signal == "YELLOW":
            if "情境B" in sop:
                sop = "⚠️ <b>[黃燈警戒]</b> 大盤極端貪婪。禁止突破追高！改為鎖定利潤或觀望。"
        elif macro_signal == "GREEN":
            if "情境A" in sop or "情境B" in sop:
                sop = sop + " <br><span style='color:#27ae60;'><b>(🟢 綠燈加持：動能健康，允許佈局)</b></span>"
                
        # ⚡ 隱含波動率 (IV) 策略強制寫入 SOP
        iv_status_str = stock.get('當前 IV', '')
        if "🔥" in iv_status_str:
            if "情境A" in sop or "情境C" in sop or "情境F" in sop or "持倉監控" in sop:
                 sop += "<br><span style='color:#c0392b;'><b>(🔥 IV 高：權金極肥，強烈建議做賣方 Sell Put / Covered Call)</b></span>"
            elif "情境B" in sop or "情境D" in sop:
                 sop += "<br><span style='color:#c0392b;'><b>(🔥 IV 高：嚴防 IV Crush，禁止單買 Call，改買正股或做價差)</b></span>"
        elif "🧊" in iv_status_str:
            if "情境B" in sop or "情境A" in sop:
                 sop += "<br><span style='color:#2980b9;'><b>(🧊 IV 低：選擇權便宜，適合直接買入 Call 或正股)</b></span>"

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
    except Exception as e: 
        print(f"❌ 寄信失敗: {e}")

if __name__ == "__main__":
    macro_desc, macro_signal = get_macro_regime()
    
    ndx, msg = get_nasdaq_100_tickers()
    hot_tickers, hot_desc = get_ai_dynamic_sectors()
    
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
            <h3 style="color: #d35400; margin-top: 0;">🛡️ 宏觀防禦與 SOP 實戰鐵律 (含 RSI 鈍化防護)</h3>
            <ul style="font-size: 13px; color: #444; line-height: 1.6;">
                <li><b>🔴 大盤紅燈 (VIX>25 或 QQQ RSI<30)：</b>無條件沒收所有做多買點，嚴禁抄底接刀。</li>
                <li><b>🟡 大盤黃燈 (QQQ RSI>75 極度貪婪)：</b>取消動能突破追高策略，僅限收租或部位減碼。</li>
                <li><b>🟢 大盤綠燈 (情緒穩定)：</b>允許全功率執行 🥇情境A(強勢回檔) 與 🥈情境B(動能突破)。</li>
                <li><b>🔥 情境 D (主升段軋空)：</b>RSI > 75 且股價穩站 5 日線上，代表極強勢。絕對禁止做空，沿 5 日線移動停損。</li>
                <li><b>⚠️ 情境 E (超買且破線)：</b>高檔跌破 5 日線，動能竭盡。立即鎖定利潤或建倉 Bear Call Spread。</li>
                <li><b>🔥 IV 偏高策略：</b>權利金極度昂貴，絕對禁止單買期權 (Long Call/Put)，強烈建議當賣方 (Sell Put) 收租。</li>
                <li><b>🛑 絕對避開：</b>出現「趨勢破壞 (個股RSI<40)」、AI 判定風險、財報 5 天內，無條件空手觀望。</li>
            </ul>
        </div>
        """
        
        html_table = html_table.replace('🥇', '<span style="color:#d35400; font-weight:bold;">🥇</span>')
        html_table = html_table.replace('🥈', '<span style="color:#2980b9; font-weight:bold;">🥈</span>')
        html_table = html_table.replace('🥉', '<span style="color:#8e44ad; font-weight:bold;">🥉</span>')
        html_table = html_table.replace('🔥', '<span style="color:#c0392b; font-weight:bold;">🔥</span>')
        html_table = html_table.replace('🛑', '<span style="color:#c0392b; font-weight:bold;">🛑</span>')
        html_table = html_table.replace('⚠️', '<span style="color:#e67e22; font-weight:bold;">⚠️</span>')

        body = f"{html_style}{status_html}<h2>🎯 發現 {len(target_df)} 檔異動標的：</h2>{html_table}{sop_reminder_html}"
        
        to_email = os.environ.get("MAIL_TO")
        from_email = os.environ.get("MAIL_USER")
        app_pass = os.environ.get("MAIL_PASS")
        
        if to_email and from_email and app_pass:
            send_scan_report_mail(f"🚦 量化早報 ({datetime.today().strftime('%m/%d')}) - {macro_signal}", body, to_email, from_email, app_pass)
        else:
            print("❌ 找不到寄件人信箱設定，請檢查環境變數！")
