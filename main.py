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

# ==========================================
# 🛡️ 網址防護函數
# ==========================================
def get_safe_url(b64_str):
    return base64.b64decode(b64_str).decode('utf-8')

# ==========================================
# API 金鑰與 AI 模型設定 
# ==========================================
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
AI_MODEL_NAME = 'gemini-2.0-flash'  

ai_client = None
if GEMINI_API_KEY:
    ai_client = genai.Client(api_key=GEMINI_API_KEY)

# ✨ 建立全域變數，記憶「今日額度是否已耗盡」
DAILY_QUOTA_EXHAUSTED = False

# ==========================================
# 參數設定區 (持倉)
# ==========================================
MY_PORTFOLIO = ['NVDA', 'TSM', 'AVGO', 'PLTR', 'MSTR'] 

REQ_SESSION = requests.Session()
REQ_SESSION.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
})

# ==========================================
# 🤖 AI 動態板塊尋標器
# ==========================================
def get_ai_dynamic_sectors(max_retries=2):
    global DAILY_QUOTA_EXHAUSTED
    fallback_tickers = ['RKLB', 'ASTS', 'BKSY', 'LITE', 'COHR', 'AMD', 'ARM', 'SMCI', 'MARA']
    fallback_desc = "太空, 矽光子, AI伺服器 (備用預設)"

    if not ai_client or DAILY_QUOTA_EXHAUSTED: return fallback_tickers, fallback_desc

    today_str = datetime.today().strftime('%Y-%m-%d')
    prompt = f"""
    現在是 {today_str}。你是一位華爾街頂尖的「板塊輪動與資金流向分析師」。
    請評估當前美股市場的最新動態，選出「當前資金最集中、最具爆發力的 3 個產業板塊」。
    然後，為這 3 個板塊各挑選 3~4 檔最具代表性、流動性佳的美股股票代碼（總共約 9~12 檔）。

    請嚴格以 JSON 格式輸出，不要有任何 Markdown 標記 (如 ```json) 或其他解釋文字，格式如下：
    {{
      "sector_names": "板塊A, 板塊B, 板塊C",
      "tickers": ["代碼1", "代碼2", "代碼3", "代碼4", "代碼5"]
    }}
    """
    for attempt in range(max_retries):
        try:
            print(f"🧠 正在請 AI ({AI_MODEL_NAME}) 偵測今日市場最熱門的 3 大板塊...")
            response = ai_client.models.generate_content(model=AI_MODEL_NAME, contents=prompt)
            data = json.loads(response.text.replace('```json', '').replace('```', '').strip())
            hot_tickers = [ticker.strip().upper() for ticker in data.get("tickers", fallback_tickers)]
            hot_sectors_desc = data.get("sector_names", fallback_desc)
            print(f"🔥 AI 動態精選板塊: {hot_sectors_desc}")
            return hot_tickers, hot_sectors_desc
        except Exception as e:
            error_msg = str(e)
            if "PerDay" in error_msg or ("limit" in error_msg.lower() and "429" in error_msg):
                print(f"🚨 偵測到 API 「每日」免費額度已耗盡！切換為靜默模式。")
                DAILY_QUOTA_EXHAUSTED = True
                return fallback_tickers, fallback_desc
            elif "429" in error_msg or "RESOURCE_EXHAUSTED" in error_msg:
                print(f"⚠️ 觸發頻率限制，等待 15 秒後重試...")
                time.sleep(15)
            else:
                return fallback_tickers, fallback_desc
    return fallback_tickers, fallback_desc

# ==========================================
# 🤖 AI 自動化審查代理
# ==========================================
def analyze_stock_with_ai(ticker, signal_type, rvol, news_list_raw, max_retries=2):
    global DAILY_QUOTA_EXHAUSTED
    if DAILY_QUOTA_EXHAUSTED: return "⚠️ 今日 API 免費額度已耗盡"
    if not ai_client: return "⚠️ 未設定 API Key"
    if not news_list_raw: return "無足夠新聞資訊"
    
    news_text = "\n".join([f"- {item.get('title', '')}" for item in news_list_raw[:5]])
    
    prompt = f"""
    你是一位華爾街資深波段交易員。美股 {ticker} 觸發了「{signal_type}」，近期成交量熱度 {rvol} 倍。
    以下是最新新聞：
    {news_text}

    量化法則：
    1. 【超跌反彈/強勢回檔】：若僅為大盤回調或情緒錯殺，判定【可以建倉】。
    2. 【動能突破/板塊異動】：若有實質利多（超預期、新訂單），判定【可以建倉】。
    3. 【一票否決】：若為結構性破壞（假帳、掉單、訴訟、高管離職），判定【高風險避開】。

    格式：50字內分析理由。結論：【可以建倉】或【高風險避開】。
    """
    
    for attempt in range(max_retries):
        try:
            response = ai_client.models.generate_content(model=AI_MODEL_NAME, contents=prompt)
            print(f"[{ticker}] AI 分析完成。")
            time.sleep(4) 
            return response.text.replace('\n', '<br>')
        except Exception as e:
            error_msg = str(e)
            if "PerDay" in error_msg or ("limit" in error_msg.lower() and "429" in error_msg):
                print(f"[{ticker}] 🚨 偵測到「每日」額度耗盡，停止今日 AI 呼叫。")
                DAILY_QUOTA_EXHAUSTED = True
                return "⚠️ 今日 API 免費額度已耗盡"
            elif "429" in error_msg or "RESOURCE_EXHAUSTED" in error_msg:
                print(f"[{ticker}] 觸發頻率限制，暫停 15 秒...")
                time.sleep(15)
            else:
                return "AI 伺服器忙線中"
    return "⚠️ 系統節流跳過"

# ==========================================
# 🏆 三層瀑布流新聞抓取模組
# ==========================================
def get_robust_news(ticker_obj, ticker_symbol):
    news_items = []
    try:
        news = ticker_obj.news
        if news and len(news) > 0:
            for item in news[:5]:
                if 'title' in item: news_items.append({'title': item['title'], 'publisher': item.get('publisher', 'Yahoo')})
            if news_items: return news_items
    except: pass

    try:
        base_url = get_safe_url('aHR0cHM6Ly9uZXdzLmdvb2dsZS5jb20vcnNzL3NlYXJjaD9xPQ==')
        query = urllib.parse.quote(f"{ticker_symbol} stock")
        url = f"{base_url}{query}&hl=en-US&gl=US&ceid=US:en"
        response = cffi_requests.get(url, impersonate="chrome110", timeout=10)
        if response.status_code == 200:
            root = ET.fromstring(response.text)
            for item in root.findall('.//channel/item')[:5]:
                title_elem = item.find('title')
                if title_elem is not None:
                    news_items.append({'title': title_elem.text.split(' - ')[0], 'publisher': 'Google News'})
            if news_items: return news_items
    except: pass
    return [] 

def get_nasdaq_100_tickers():
    try:
        url = get_safe_url('aHR0cHM6Ly9lbi53aWtpcGVkaWEub3JnL3dpa2kvTmFzZGFxLTEwMA==')
        response = cffi_requests.get(url, impersonate="chrome110", timeout=15)
        tables = pd.read_html(response.text)
        for table in tables:
            if 'Ticker' in table.columns: return table['Ticker'].tolist(), f"✅ 成功抓取 {len(table)} 檔 Nasdaq 100"
            elif 'Symbol' in table.columns: return table['Symbol'].tolist(), f"✅ 成功抓取 {len(table)} 檔 Nasdaq 100"
    except Exception as e: return ['AAPL', 'MSFT'], f"⚠️ 抓取失敗"

def get_fundamental_sentiment_score(ticker_obj, ticker_symbol):
    score = 50
    latest_news_str = "無最新新聞" 
    sector = "未知板塊"
    days_to_earnings = "未知"
    news_list_raw = [] 

    try:
        info = ticker_obj.info
        sector = info.get('sector', '未知板塊')
        if info.get('industry'): sector = f"{sector} ({info.get('industry')})"

        earn_ts = info.get('earningsTimestamp')
        if earn_ts:
            days = (datetime.fromtimestamp(earn_ts).date() - datetime.today().date()).days
            if days >= 0:
                days_to_earnings = f"⚠️ {days}天後" if days <= 5 else f"{days} 天後"
            else: days_to_earnings = "近期已發布"

        current_price = info.get('currentPrice', 0)
        target_price = info.get('targetMeanPrice', 0)
        if current_price and target_price and current_price > 0 and target_price > 0:
            upside = (target_price - current_price) / current_price
            if upside > 0.15: score += 15
            elif upside < 0: score -= 15

        rec = info.get('recommendationKey', '')
        if rec in ['buy', 'strong_buy']: score += 10
        elif rec in ['sell', 'strong_sell', 'underperform']: score -= 15
        
        news_list_raw = get_robust_news(ticker_obj, ticker_symbol)
        if news_list_raw:
            bull_keywords, bear_keywords = ['upgrade', 'beat', 'growth'], ['downgrade', 'miss', 'cut', 'lawsuit']
            news_score = 0
            news_display = []
            for i, article in enumerate(news_list_raw):
                title = article.get('title', '')
                if any(k in title.lower() for k in bull_keywords): news_score += 4
                if any(k in title.lower() for k in bear_keywords): news_score -= 5
                if i < 2: news_display.append(f"▪️ {title}")
            score += news_score
            if news_display: latest_news_str = "<br><br>".join(news_display)

        return max(0, min(100, score)), latest_news_str, sector, days_to_earnings, news_list_raw
    except: return 50, "數據抓取異常", "未知", "未知", []

def get_investment_strategy(ticker_obj, current_price, score, signal_type, days_to_earnings):
    earnings_warning = "⚠️" in days_to_earnings
    if signal_type == "超跌反彈" or signal_type == "強勢回檔":
        if score < 40: return {'綜合建議': '🔴 觀望(基本面轉弱)', '期權履約價': '-', '年化報酬': '-'}
        try:
            exp_dates = ticker_obj.options
            target_date, target_days = None, 0
            for date_str in exp_dates:
                days_to_exp = (datetime.strptime(date_str, '%Y-%m-%d') - datetime.today()).days
                if 25 <= days_to_exp <= 45:
                    target_date, target_days = date_str, days_to_exp
                    break
            if not target_date: raise ValueError()
            
            chain = ticker_obj.option_chain(target_date)
            cushion = 0.92 if score >= 60 else 0.85
            suitable_puts = chain.puts[chain.puts['strike'] <= current_price * cushion]
            if suitable_puts.empty: raise ValueError()
            
            best_put = suitable_puts.sort_values(by='strike', ascending=False).iloc[0]
            premium = best_put['bid'] if best_put['bid'] > 0 else best_put['lastPrice']
            if premium <= 0: raise ValueError()
                
            annual_roc = (premium / (best_put['strike'] - premium)) * 100 * (365 / target_days)
            action = '🟢 積極 Sell Put' if score >= 60 else '🟡 保守 Sell Put'
            if earnings_warning: action = '🔴 避開期權(財報風險)'
            return {'綜合建議': action, '期權履約價': f"Put ${best_put['strike']} ({target_date})", '年化報酬': f"{round(annual_roc, 1)}%"}
        except: return {'綜合建議': '🟢 買入正股', '期權履約價': '-', '年化報酬': '-'}
            
    elif signal_type == "動能突破" or signal_type == "板塊異動(爆量)":
        if score >= 65: return {'綜合建議': '🚀 順勢買正股 / Buy Call', '期權履約價': '-', '年化報酬': '-'}
        elif score < 40: return {'綜合建議': '⚠️ 估值偏高，設好停損', '期權履約價': '-', '年化報酬': '-'}
        else: return {'綜合建議': '⚪ 持有觀望', '期權履約價': '-', '年化報酬': '-'}

# ==========================================
# 主掃描函數 (Top 5 狙擊手過濾邏輯 + SOP 生成)
# ==========================================
def scan_market_opportunities(tickers_list, portfolio_list, hot_sectors_list):
    all_tickers = list(set(tickers_list + portfolio_list + hot_sectors_list))
    print(f"開始下載 {len(all_tickers)} 檔股票歷史股價...")
    df_data = yf.download(all_tickers, period="3mo", progress=False) 
    
    if df_data.empty: return pd.DataFrame()
    df_closes, df_volumes = df_data['Close'].dropna(axis=1, how='all'), df_data['Volume'].dropna(axis=1, how='all')
    
    raw_candidates = []
    
    for ticker in df_closes.columns:
        try:
            close_prices, volumes = df_closes[ticker].dropna(), df_volumes[ticker].dropna()
            if len(close_prices) < 20: continue 
                
            delta = close_prices.diff()
            gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
            loss = -delta.clip(upper=0).ewm(alpha=1/14, adjust=False).mean()
            rsi = (100 - (100 / (1 + gain / loss))).replace([np.inf, -np.inf], np.nan).fillna(100)
            latest_rsi, latest_price = rsi.iloc[-1], close_prices.iloc[-1]
            bias_20 = ((latest_price - close_prices.rolling(20).mean().iloc[-1]) / close_prices.rolling(20).mean().iloc[-1]) * 100
            
            rvol, vol_surge = 0.0, False
            if len(volumes) >= 20:
                vol_ma20 = volumes.rolling(20).mean().iloc[-1]
                if pd.notna(vol_ma20) and vol_ma20 > 0:
                    rvol = round(volumes.iloc[-1] / vol_ma20, 2)
                    vol_surge = rvol > 1.2 
            
            is_portfolio, is_hot_sector = ticker in portfolio_list, ticker in hot_sectors_list
            signal_type = None

            if is_hot_sector:
                if latest_rsi > 65 and bias_20 > 5 and vol_surge: signal_type = "動能突破"
                elif 40 <= latest_rsi <= 55 and bias_20 > -3 and vol_surge: signal_type = "強勢回檔"
                elif rvol > 1.5: signal_type = "板塊異動(爆量)"
            else:
                if latest_rsi < 35 and bias_20 < -6: signal_type = "超跌反彈"
                elif latest_rsi > 65 and bias_20 > 5 and vol_surge: signal_type = "動能突破"

            if is_portfolio and not signal_type: signal_type = "持倉監控"
                
            if signal_type:
                stock_obj = yf.Ticker(ticker)
                f_score, l_news, sector, d_earn, raw_news = get_fundamental_sentiment_score(stock_obj, ticker)
                strat = get_investment_strategy(stock_obj, latest_price, f_score, signal_type, d_earn)
                
                if signal_type == "持倉監控": strat['綜合建議'] = "🔹 日常追蹤" if f_score >= 50 else "⚠️ 留意停損"

                identity = '💼 我的持倉' if is_portfolio else ('🔥 動態熱門板塊' if is_hot_sector else '🔍 掃描發現')
                sort_weight = (1000 if is_portfolio else 0) + f_score + (rvol * 10)

                raw_candidates.append({
                    'sort_weight': sort_weight, 'raw_news': raw_news,
                    'data': {
                        '身份': identity, '代碼': ticker, '產業板塊': sector, '型態': signal_type, 
                        '現價': round(latest_price, 2), 'RSI': round(latest_rsi, 2), '熱度(RVOL)': f"{rvol}x",    
                        '財報日': d_earn, '評分': f"{int(f_score)}", '綜合建議': strat['綜合建議'], 
                        '期權履約價': strat['期權履約價'], '年化報酬': strat['年化報酬'], '最新新聞': l_news
                    }
                })
        except: continue

    raw_candidates.sort(key=lambda x: x['sort_weight'], reverse=True)
    top_candidates = raw_candidates[:5] 
    
    results = []
    print(f"\n🎯 系統篩選出 {len(raw_candidates)} 檔標的，僅針對最頂尖 Top 5 啟動 AI 深度分析與 SOP 判定...")
    
    for item in raw_candidates:
        stock_data = item['data']
        # 1. 呼叫 AI (僅限 Top 5 且非單純持倉監控)
        if item in top_candidates and stock_data['型態'] != "持倉監控": 
            print(f"➤ 啟動 AI 狙擊分析: {stock_data['代碼']} ({stock_data['型態']})")
            ai_verdict = analyze_stock_with_ai(stock_data['代碼'], stock_data['型態'], stock_data['熱度(RVOL)'], item['raw_news'])
        else:
            ai_verdict = "⏸️ 系統已記錄 (未達 AI 啟動門檻)"
            
        stock_data['🤖 AI 投研觀點'] = ai_verdict

        # 2. ✨ 新增：SOP 實戰提示自動判定邏輯
        sop_prompt = "⚪ 觀望或依原定策略"
        if "高風險避開" in ai_verdict or "⚠️" in ai_verdict:
            sop_prompt = "🛑 AI 否決：結構性風險，直接放棄。"
        elif "⚠️" in stock_data['財報日']:
            sop_prompt = "🛑 財報將近：禁止 Sell Put 避免跳空。"
        elif stock_data['型態'] == "強勢回檔" and "熱門板塊" in stock_data['身份']:
            sop_prompt = "🥇 情境A (熱門回檔)：建倉 Bull Put Spread，獲利 50%~70% 即平倉。"
        elif stock_data['型態'] in ["動能突破", "板塊異動(爆量)"] and float(stock_data['熱度(RVOL)'].replace('x', '')) > 1.5:
            sop_prompt = "🥈 情境B (帶量突破)：買入正股或 Call，🚨 嚴格設定 8%~10% 移動停損。"
        elif stock_data['型態'] == "超跌反彈" and "熱門板塊" not in stock_data['身份'] and int(stock_data['評分']) >= 70:
            sop_prompt = "🥉 情境C (優質錯殺)：保守 Sell Put 收租，正股跌破 20MA 即停損。"
        elif stock_data['型態'] == "持倉監控":
            sop_prompt = "🔹 紀律：正股跌破 20MA 無條件出場。"
            
        stock_data['🎯 SOP 操作提示'] = sop_prompt
        results.append(stock_data)

    return pd.DataFrame(results)

# ==========================================
# 郵件寄送模組
# ==========================================
def send_scan_report_mail(subject, body, to_email, from_email, app_password):
    msg = MIMEMultipart()
    msg['From'] = from_email
    msg['To'] = to_email
    msg['Subject'] = subject
    
    html_style = """
    <style>
        body { font-family: 'Segoe UI', Arial, sans-serif; color: #2c3e50; }
        .status-box { background-color: #ecf0f1; border-left: 4px solid #3498db; padding: 12px 15px; margin-bottom: 20px; font-size: 13px; line-height: 1.5; }
        table { border-collapse: collapse; width: 100%; font-size: 12px; table-layout: fixed; }
        th { background-color: #2c3e50; color: white; padding: 10px 5px; text-align: center; }
        td { border: 1px solid #bdc3c7; padding: 8px 5px; text-align: center; word-wrap: break-word; vertical-align: middle; }
        tr:nth-child(even) { background-color: #f8f9fa; }
        th:nth-last-child(2), th:last-child { width: 15%; } /* 調整欄寬以適應 SOP */
        th:last-child { background-color: #d35400; } /* SOP 欄位用亮色 */
        td:nth-last-child(3), td:nth-last-child(2), td:last-child { text-align: left; font-size: 11px; line-height: 1.4; }
        .high-rvol { color: #e74c3c; font-weight: bold; }
    </style>
    """
    msg.attach(MIMEText(f"<html><head>{html_style}</head><body>{body}</body></html>", 'html'))

    try:
        with smtplib.SMTP('smtp.gmail.com', 587) as server:
            server.starttls()
            server.login(from_email, app_password)
            server.sendmail(from_email, to_email, msg.as_string())
        print("📧 報告已成功寄出!")
    except Exception as e: print(f"❌ 寄信失敗: {e}")

if __name__ == "__main__":
    ndx_tickers, fetch_status_msg = get_nasdaq_100_tickers()
    dynamic_hot_sectors, hot_sectors_desc = get_ai_dynamic_sectors()
    
    target_df = scan_market_opportunities(ndx_tickers, MY_PORTFOLIO, dynamic_hot_sectors)
    
    subject = f"🧠 量化早報：Top 5 狙擊手策略與 AI 觀點 ({datetime.today().strftime('%Y-%m-%d')})"
    status_html = f"<div class='status-box'>{fetch_status_msg}<br>💼 持倉監控：{len(MY_PORTFOLIO)} 檔 <br>🔥 AI 動態板塊鎖定：<b>{hot_sectors_desc}</b><br>⚡ 已啟用 Sniper 漏斗機制與 SOP 實戰自動提示</div>"
    
    if target_df.empty:
        body = f"{status_html}<h3>今日無符合條件標的</h3>"
    else:
        target_df['身份權重'] = target_df['身份'].map({'💼 我的持倉': 1, '🔥 動態熱門板塊': 2, '🔍 掃描發現': 3})
        target_df['RVOL_num'] = target_df['熱度(RVOL)'].str.replace('x', '').astype(float)
        
        target_df = target_df.sort_values(by=['身份權重', '型態', 'RVOL_num'], ascending=[True, False, False])
        target_df = target_df.drop(columns=['身份權重', 'RVOL_num']) 
        
        pd.set_option('display.max_colwidth', None)
        html_table = target_df.to_html(index=False, escape=False)
        
        # 幫表格內的重點加上顏色
        html_table = html_table.replace('<td>2.', '<td class="high-rvol">🔥 2.')
        html_table = html_table.replace('<td>3.', '<td class="high-rvol">🔥 3.')
        html_table = html_table.replace('<td>4.', '<td class="high-rvol">🔥 4.')
        html_table = html_table.replace('🥇', '<span style="color:#d35400; font-weight:bold;">🥇</span>')
        html_table = html_table.replace('🥈', '<span style="color:#2980b9; font-weight:bold;">🥈</span>')
        html_table = html_table.replace('🥉', '<span style="color:#8e44ad; font-weight:bold;">🥉</span>')
        html_table = html_table.replace('🛑', '<span style="color:#c0392b; font-weight:bold;">🛑</span>')
        
        # ✨ 新增：信件最底部的 SOP 鐵律提醒區塊
        sop_reminder_html = """
        <div style="background-color: #fdfbf7; border: 1px solid #e8e0d5; padding: 15px; margin-top: 25px; border-radius: 5px;">
            <h3 style="color: #d35400; margin-top: 0;">🛡️ 狙擊手 SOP 實戰鐵律提醒</h3>
            <ul style="font-size: 13px; color: #444; line-height: 1.6;">
                <li><b>🥇 情境 A (熱門股強勢回檔)：</b>採用 <b>Bull Put Spread (賣權多頭價差)</b> 鎖定下檔風險，獲利 50%~70% 提早入袋。</li>
                <li><b>🥈 情境 B (帶量動能突破)：</b>追擊正股或買 Call，<b>絕對要設定 8%~10% 移動停損</b>，嚴防假突破。</li>
                <li><b>🥉 情境 C (優質權值股錯殺)：</b>執行 <b>Sell Put</b> 收取高額權利金，萬一正股跌破 20MA 則無條件出場觀望。</li>
                <li><b>🛑 絕對避開：</b>AI 判定【高風險避開】、財報 5 天內、無量假突破，一律管好手不碰。</li>
            </ul>
        </div>
        """
        
        body = (f"{status_html}<h2>🎯 發現 {len(target_df)} 檔異動標的：</h2>{html_table}{sop_reminder_html}")
        body = body.replace('⚠️', '<span style="color:#e67e22; font-weight:bold;">⚠️</span>')
        body = body.replace('🟢', '<span style="color:#27ae60; font-weight:bold;">🟢</span>')
        body = body.replace('🔴', '<span style="color:#c0392b; font-weight:bold;">🔴</span>')
        body = body.replace('🚀', '<span style="color:#8e44ad; font-weight:bold;">🚀</span>')
        body = body.replace('【可以建倉】', '<span style="color:#27ae60; font-weight:bold;">【可以建倉】</span>')
        body = body.replace('【高風險避開】', '<span style="color:#c0392b; font-weight:bold;">【高風險避開】</span>')

    to_email = os.environ.get("MAIL_TO")
    from_email = os.environ.get("MAIL_USER")
    app_password = os.environ.get("MAIL_PASS")

    if to_email and from_email and app_password: send_scan_report_mail(subject, body, to_email, from_email, app_password)
    else: print("報告產生完成")
