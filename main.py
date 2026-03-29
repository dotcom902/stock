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
# 📈 大盤環境風向標 (修正 yfinance 抓取穩定度)
# ==========================================
def get_market_trend():
    try:
        status = []
        is_bearish = False
        for ticker in ['SPY', 'QQQ']:
            df = yf.download(ticker, period="2mo", progress=False)
            if df.empty or 'Close' not in df: continue
            
            # 處理 yfinance 可能返回的 Series 或 DataFrame 格式
            prices = df['Close'][ticker].dropna() if isinstance(df['Close'], pd.DataFrame) else df['Close'].dropna()
            
            if len(prices) < 20: continue
            current = float(prices.iloc[-1])
            ma20 = float(prices.rolling(20).mean().iloc[-1])
            
            if current < ma20:
                status.append(f"{ticker}: 🔴破月線")
                is_bearish = True
            else:
                status.append(f"{ticker}: 🟢多頭")
        return " | ".join(status) if status else "大盤狀態: 未知", is_bearish
    except Exception as e:
        return "大盤狀態: 未知", False

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
# 🎯 狙擊手核心掃描與全天候 SOP
# ==========================================
def scan_market_opportunities(tickers_list, portfolio_list, hot_sectors_list, is_market_bearish):
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
    
    # ✨ 終極修復：分離 AI 算力額度
    # 只把「真正觸發進出場訊號」的標的送給 AI，過濾掉單純的「持倉監控」與不需要 AI 的盤整
    ai_target_candidates = [
        item for item in raw_candidates 
        if item['data']['型態'] not in ["持倉監控", "高檔震盪", "趨勢破壞", "極端超買"]
    ][:5]
    
    print(f"\n🎯 系統篩選出 {len(raw_candidates)} 檔標的，僅針對最頂尖 {len(ai_target_candidates)} 檔觸發訊號標的啟動 AI 分析...")
    
    results = []
    for item in raw_candidates:
        stock = item['data']
        if item in ai_target_candidates:
            print(f"➤ 啟動 AI 狙擊分析: {stock['代碼']} ({stock['型態']})")
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
            
        # ✨ 大盤防禦機制介入
        if is_market_bearish:
            if "情境A" in sop or "情境B" in sop or "情境C" in sop:
                sop = sop.replace("🥇", "🛡️").replace("🥈", "🛡️").replace("🥉", "🛡️") + "<br><b>(🚨大盤轉弱，建倉必須減半)</b>"
            
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
    ndx, msg = get_nasdaq_100_tickers()
    hot_tickers, hot_desc = get_ai_dynamic_sectors()
    market_status, is_bearish = get_market_trend() 
    
    combined_hot_sectors_list = list(set(hot_tickers + CORE_WATCHLIST))
    target_df = scan_market_opportunities(ndx, MY_PORTFOLIO, combined_hot_sectors_list, is_bearish)
    
    if not target_df.empty:
        html_table = target_df.to_html(index=False, escape=False)
        
        html_style = """
        <style>
            body { font-family: 'Segoe UI', Arial, sans-serif; color: #2c3e50; }
            .status-box { background-color: #ecf0f1; border-left: 4px solid #3498db; padding: 12px 15px; margin-bottom: 20px; font-size: 13px; line-height: 1.5; }
            table { border-collapse: collapse; width: 100%; font-size: 12px; table-layout: fixed; }
            th { background-color: #2c3e50; color: white; padding: 10px 5px; text-align: center; }
            td { border: 1px solid #bdc3c7; padding: 8px 5px; text-align: center; word-wrap: break-word; vertical-align: middle; }
            tr:nth-child(even) { background-color: #f8f9fa; }
            th:nth-last-child(2), th:last-child { width: 15%; }
            th:last-child { background-color: #d35400; }
            td:nth-last-child(3), td:nth-last-child(2), td:last-child { text-align: left; font-size: 11px; line-height: 1.4; }
        </style>
        """
        
        status_html = f"<div class='status-box'><b>📊 大盤狀態：{market_status}</b><br>{msg}<br>💼 持倉監控：{len(MY_PORTFOLIO)} 檔 <br>🎯 核心必掃雷達：啟動 (光通信/半導體/太空)<br>🔥 AI 動態板塊：<b>{hot_desc}</b></div>"
        
        sop_reminder_html = """
        <div style="background-color: #fdfbf7; border: 1px solid #e8e0d5; padding: 15px; margin-top: 25px; border-radius: 5px;">
            <h3 style="color: #d35400; margin-top: 0;">🛡️ 熱門股全天候 SOP 實戰鐵律</h3>
            <ul style="font-size: 13px; color: #444; line-height: 1.6;">
                <li><b>📊 大盤防禦機制：</b>若 SPY/QQQ 跌破月線，系統會自動亮起 🛡️，強烈建議多頭部位減半。</li>
                <li><b>🎯 Upside (潛在空間)：</b>投行目標價距離現價的空間。若呈負數代表溢價嚴重，絕對禁止追高。</li>
                <li><b>🥇 情境 A (強勢回檔)：</b>採用 <b>Bull Put Spread</b> 鎖定下檔風險，勝率最高。</li>
                <li><b>🥈 情境 B (動能突破)：</b>追擊正股/買 Call，<b>絕對設定 8% 移動停損</b>，嚴防假突破。</li>
                <li><b>⚠️ 情境 D (極端超買)：</b>乖離過大絕對不追！考慮獲利了結或建倉 <b>Bear Call Spread</b>。</li>
                <li><b>🛑 絕對避開：</b>出現「趨勢破壞 (RSI<40)」、AI 判定風險、財報 5 天內，無條件空手觀望。</li>
            </ul>
        </div>
        """
        
        html_table = html_table.replace('🥇', '<span style="color:#d35400; font-weight:bold;">🥇</span>')
        html_table = html_table.replace('🥈', '<span style="color:#2980b9; font-weight:bold;">🥈</span>')
        html_table = html_table.replace('🥉', '<span style="color:#8e44ad; font-weight:bold;">🥉</span>')
        html_table = html_table.replace('🛡️', '<span style="color:#34495e; font-weight:bold;">🛡️</span>')
        html_table = html_table.replace('⚠️', '<span style="color:#e67e22; font-weight:bold;">⚠️</span>')
        html_table = html_table.replace('⏳', '<span style="color:#7f8c8d; font-weight:bold;">⏳</span>')
        html_table = html_table.replace('🛑', '<span style="color:#c0392b; font-weight:bold;">🛑</span>')

        body = f"{html_style}{status_html}<h2>🎯 發現 {len(target_df)} 檔異動標的：</h2>{html_table}{sop_reminder_html}"
        
        to_email = os.environ.get("MAIL_TO")
        from_email = os.environ.get("MAIL_USER")
        app_pass = os.environ.get("MAIL_PASS")
        
        if to_email and from_email and app_pass:
            send_scan_report_mail(f"🧠 量化早報 ({datetime.today().strftime('%Y-%m-%d')})", body, to_email, from_email, app_pass)
        else:
            print("❌ 找不到寄件人信箱、密碼或收件人(MAIL_TO)設定，請檢查 GitHub Secrets！")
