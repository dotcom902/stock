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
import requests
import urllib.parse
import time  
# ✨ 關鍵升級：匯入全新的 Google GenAI 官方套件
from google import genai  

warnings.filterwarnings('ignore')

# ==========================================
# API 金鑰與 AI 模型設定 (全新寫法)
# ==========================================
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
ai_client = None
if GEMINI_API_KEY:
    ai_client = genai.Client(api_key=GEMINI_API_KEY)

# ==========================================
# 參數設定區 (加入你的專屬強勢板塊與持倉)
# ==========================================
MY_PORTFOLIO = ['NVDA', 'TSM', 'AVGO', 'PLTR', 'MSTR'] 

HOT_SECTORS = [
    'RKLB', 'ASTS', 'BKSY', 'SIDU', 'PLTR', 
    'LITE', 'COHR', 'FN', 'LUMN',           
    'AMD', 'MU', 'ARM', 'SMCI',             
    'ONDS', 'SERV', 'SYM', 'MARA' # ✨ 已修復：補上逗號，避免變成 SYMMARA
]

# ==========================================
# 網路連線設定 (偽裝成真人瀏覽器)
# ==========================================
REQ_SESSION = requests.Session()
REQ_SESSION.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
})

# ==========================================
# 🤖 AI 自動化審查代理 (全面升級版)
# ==========================================
def analyze_stock_with_ai(ticker, signal_type, rvol, news_list_raw):
    if not ai_client: return "⚠️ 未設定 API Key"
    if not news_list_raw: return "無足夠新聞資訊"
    
    news_text = "\n".join([f"- {item.get('title', '')}" for item in news_list_raw[:5]])
    
    prompt = f"""
    你是一位華爾街資深波段交易員與避險基金風險控制專家。
    目前美股代號 {ticker} 觸發了「{signal_type}」的技術面訊號，且近期的成交量熱度為 {rvol} 倍。
    
    以下是該公司最新的催化劑新聞標題：
    {news_text}

    請依據以上資訊簡短分析：
    1. 這是「結構性破壞（如掉單、假帳）」還是「情緒性錯殺/短期波動（如大盤拖累、短期降評、財報微幅不及預期）」？若是強勢突破，是因為什麼實質利多？
    2. 給出你的最終結論：【可以建倉】或【高風險避開】。

    請將字數嚴格限制在 50 個中文字以內，直接給出結論。
    """
    
    try:
        # ✨ 關鍵升級：使用最新語法與最新的 gemini-2.5-flash 引擎
        response = ai_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt
        )
        
        print(f"[{ticker}] AI 分析完成，冷卻 6 秒以保護免費額度...")
        time.sleep(6) 
        
        return response.text.replace('\n', '<br>')
    except Exception as e:
        print(f"[{ticker}] AI 分析失敗: {e}")
        time.sleep(6) 
        return "AI 伺服器忙線中"

# ==========================================
# 三層瀑布流新聞抓取模組
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
        url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker_symbol}&region=US&lang=en-US"
        response = REQ_SESSION.get(url, timeout=5)
        if response.status_code == 200:
            root = ET.fromstring(response.content)
            for item in root.findall('.//item')[:5]:
                title_elem = item.find('title')
                if title_elem is not None: news_items.append({'title': title_elem.text, 'publisher': 'Yahoo RSS'})
            if news_items: return news_items
    except: pass

    try:
        query = urllib.parse.quote(f"{ticker_symbol} stock")
        url = f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
        response = REQ_SESSION.get(url, timeout=5)
        if response.status_code == 200:
            root = ET.fromstring(response.content)
            for item in root.findall('.//channel/item')[:5]:
                title_elem = item.find('title')
                if title_elem is not None: news_items.append({'title': title_elem.text, 'publisher': 'Google News'})
            if news_items: return news_items
    except: pass
    return [] 

def get_nasdaq_100_tickers():
    print("正在獲取 Nasdaq 100 成分股...")
    try:
        url = 'https://en.wikipedia.org/wiki/Nasdaq-100'
        html_content = REQ_SESSION.get(url, timeout=10).text
        tables = pd.read_html(html_content)
        for table in tables:
            if 'Ticker' in table.columns: return table['Ticker'].tolist(), f"✅ 成功抓取 {len(table)} 檔 Nasdaq 100"
            elif 'Symbol' in table.columns: return table['Symbol'].tolist(), f"✅ 成功抓取 {len(table)} 檔 Nasdaq 100"
    except Exception as e:
        return ['AAPL', 'MSFT'], f"⚠️ 抓取失敗，使用備用清單 ({e})"
    return ['AAPL'], "⚠️ 發生未知錯誤"

# ==========================================
# 數據驗證與基本面抓取
# ==========================================
def get_fundamental_sentiment_score(ticker_obj, ticker_symbol):
    score = 50
    latest_news_str = "無最新新聞" 
    sector = "未知板塊"
    days_to_earnings = "未知"
    news_list_raw = [] 

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
                days_to_earnings = f"{days} 天後"
                if days <= 5: days_to_earnings = f"⚠️ {days}天後(避開Sell Put)"
            else:
                days_to_earnings = "近期已發布"

        current_price = info.get('currentPrice', 0)
        target_price = info.get('targetMeanPrice', 0)
        if current_price and target_price and current_price > 0 and target_price > 0:
            upside = (target_price - current_price) / current_price
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
                    publisher = article.get('publisher', '')
                    display_text = f"▪️ {title}" if " - " in title else f"▪️ {title} ({publisher})"
                    news_display.append(display_text)

            score += news_score
            if news_display: latest_news_str = "<br><br>".join(news_display)

        return max(0, min(100, score)), latest_news_str, sector, days_to_earnings, news_list_raw
    except Exception as e:
        return 50, "數據抓取異常", "未知", "未知", []

def get_investment_strategy(ticker_obj, current_price, score, signal_type, days_to_earnings):
    earnings_warning = "⚠️財報將近" in days_to_earnings

    if signal_type == "超跌反彈":
        if score < 40: return {'綜合建議': '🔴 觀望(基本面轉弱)', '期權履約價': '-', '年化報酬': '-'}
        try:
            exp_dates = ticker_obj.options
            if not exp_dates or len(exp_dates) == 0: raise ValueError("無期權")
                
            today = datetime.today()
            target_date, target_days = None, 0
            for date_str in exp_dates:
                days_to_exp = (datetime.strptime(date_str, '%Y-%m-%d') - today).days
                if 25 <= days_to_exp <= 45:
                    target_date, target_days = date_str, days_to_exp
                    break
                    
            if not target_date: return {'綜合建議': '🟢 買入正股(無合適期權)', '期權履約價': '-', '年化報酬': '-'}
            
            chain = ticker_obj.option_chain(target_date)
            if chain.puts.empty: raise ValueError("無 Put")
            
            cushion = 0.92 if score >= 60 else 0.85
            suitable_puts = chain.puts[chain.puts['strike'] <= current_price * cushion]
            if suitable_puts.empty: return {'綜合建議': '🟢 買入正股', '期權履約價': '-', '年化報酬': '-'}
            
            best_put = suitable_puts.sort_values(by='strike', ascending=False).iloc[0]
            premium = best_put['bid'] if best_put['bid'] > 0 else best_put['lastPrice']
            if premium <= 0 or best_put['strike'] - premium <= 0: raise ValueError("權利金異常")
                
            annual_roc = (premium / (best_put['strike'] - premium)) * 100 * (365 / target_days)
            
            action = '🟢 積極 Sell Put / 買正股' if score >= 60 else '🟡 保守 Sell Put'
            if earnings_warning: action = '🔴 避開期權(財報高風險)'

            return {'綜合建議': action, '期權履約價': f"Put ${best_put['strike']} ({target_date})", '年化報酬': f"{round(annual_roc, 1)}%"}
        except: return {'綜合建議': '🟢 買入正股', '期權履約價': '-', '年化報酬': '-'}
            
    elif signal_type == "動能突破":
        if score >= 65: return {'綜合建議': '🚀 順勢買正股 / Buy Call', '期權履約價': '-', '年化報酬': '-'}
        elif score < 40: return {'綜合建議': '⚠️ 估值過高，考慮獲利了結', '期權履約價': '-', '年化報酬': '-'}
        else: return {'綜合建議': '⚪ 持有觀望，設好移動停損', '期權履約價': '-', '年化報酬': '-'}

# ==========================================
# 主掃描函數 
# ==========================================
def scan_market_opportunities(tickers_list, portfolio_list, hot_sectors_list):
    all_tickers = list(set(tickers_list + portfolio_list + hot_sectors_list))
    
    print(f"開始下載 {len(all_tickers)} 檔股票歷史股價...")
    df_data = yf.download(all_tickers, period="3mo", progress=False) 
    
    if df_data.empty: return pd.DataFrame()
        
    df_closes = df_data['Close'].dropna(axis=1, how='all')
    df_volumes = df_data['Volume'].dropna(axis=1, how='all')
    
    results = []
    
    for ticker in df_closes.columns:
        try:
            close_prices = df_closes[ticker].dropna()
            if len(close_prices) < 20: continue 
                
            delta = close_prices.diff()
            gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
            loss = -delta.clip(upper=0).ewm(alpha=1/14, adjust=False).mean()
            rs = gain / loss
            rs = rs.replace([np.inf, -np.inf], np.nan).fillna(100)
            rsi = 100 - (100 / (1 + rs))
            latest_rsi = rsi.iloc[-1]
            
            ma20 = close_prices.rolling(window=20).mean()
            latest_price = close_prices.iloc[-1]
            bias_20 = ((latest_price - ma20.iloc[-1]) / ma20.iloc[-1]) * 100
            
            volumes = df_volumes[ticker].dropna()
            rvol = 0.0
            vol_surge = False
            if len(volumes) >= 20:
                vol_ma20 = volumes.rolling(window=20).mean().iloc[-1]
                latest_vol = volumes.iloc[-1]
                if pd.notna(vol_ma20) and vol_ma20 > 0:
                    rvol = round(latest_vol / vol_ma20, 2)
                    vol_surge = rvol > 1.2 
            
            signal_type = None
            if latest_rsi < 35 and bias_20 < -6: signal_type = "超跌反彈"
            elif latest_rsi > 65 and bias_20 > 5 and vol_surge: signal_type = "動能突破"
                
            is_portfolio = ticker in portfolio_list
            is_hot_sector = ticker in hot_sectors_list

            if is_portfolio and not signal_type: signal_type = "持倉監控"
            elif is_hot_sector and not signal_type and rvol > 1.5: signal_type = "板塊異動(爆量)"
                
            if signal_type:
                print(f"\n➤ 發現異動: {ticker} ({signal_type})... 準備提取資料與 AI 分析")
                stock_obj = yf.Ticker(ticker)
                
                fund_score, latest_news, sector, days_to_earnings, news_list_raw = get_fundamental_sentiment_score(stock_obj, ticker)
                strategy = get_investment_strategy(stock_obj, latest_price, fund_score, signal_type, days_to_earnings)
                
                if signal_type == "持倉監控":
                    strategy['綜合建議'] = "🔹 日常追蹤" if fund_score >= 50 else "⚠️ 基本面弱化，留意停損"

                # 呼叫 AI 進行深度判斷
                ai_verdict = analyze_stock_with_ai(ticker, signal_type, rvol, news_list_raw)

                identity = '🔍 掃描發現'
                if is_portfolio: identity = '💼 我的持倉'
                elif is_hot_sector: identity = '🔥 熱門板塊'

                stock_data = {
                    '身份': identity,
                    '代碼': ticker, 
                    '產業板塊': sector,           
                    '型態': signal_type, 
                    '現價': round(latest_price, 2),
                    'RSI': round(latest_rsi, 2), 
                    '熱度(RVOL)': f"{rvol}x",    
                    '財報日': days_to_earnings,  
                    '評分': f"{int(fund_score)}",
                    '綜合建議': strategy['綜合建議'], 
                    '期權履約價': strategy['期權履約價'],
                    '年化報酬': strategy['年化報酬'], 
                    '最新新聞': latest_news,
                    '🤖 AI 投研觀點': ai_verdict
                }
                results.append(stock_data)
        except Exception as e:
            continue

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
        th:nth-last-child(2) { width: 18%; } 
        th:last-child { width: 18%; background-color: #8e44ad; } 
        td:nth-last-child(2), td:last-child { text-align: left; font-size: 11px; color: #34495e; line-height: 1.4; }
        .high-rvol { color: #e74c3c; font-weight: bold; }
    </style>
    """
    full_html = f"<html><head>{html_style}</head><body>{body}</body></html>"
    msg.attach(MIMEText(full_html, 'html'))

    try:
        with smtplib.SMTP('smtp.gmail.com', 587) as server:
            server.starttls()
            server.login(from_email, app_password)
            server.sendmail(from_email, to_email, msg.as_string())
        print("📧 報告已成功寄出!")
    except Exception as e:
        print(f"❌ 寄信失敗: {e}")

if __name__ == "__main__":
    ndx_tickers, fetch_status_msg = get_nasdaq_100_tickers()
    target_df = scan_market_opportunities(ndx_tickers, MY_PORTFOLIO, HOT_SECTORS)
    
    subject = f"🧠 量化早報：多板塊熱力掃描與 AI 觀點 ({datetime.today().strftime('%Y-%m-%d')})"
    status_html = f"<div class='status-box'>{fetch_status_msg}<br>💼 持倉監控：{len(MY_PORTFOLIO)} 檔 | 🔥 熱門板塊池：{len(HOT_SECTORS)} 檔<br>🤖 AI Agent 已升級為 Gemini 2.5 引擎</div>"
    
    if target_df.empty:
        body = f"{status_html}<h3>今日無符合條件標的</h3>"
    else:
        target_df['身份權重'] = target_df['身份'].map({'💼 我的持倉': 1, '🔥 熱門板塊': 2, '🔍 掃描發現': 3})
        target_df['RVOL_num'] = target_df['熱度(RVOL)'].str.replace('x', '').astype(float)
        
        target_df = target_df.sort_values(by=['身份權重', '型態', 'RVOL_num'], ascending=[True, False, False])
        target_df = target_df.drop(columns=['身份權重', 'RVOL_num']) 
        
        pd.set_option('display.max_colwidth', None)
        html_table = target_df.to_html(index=False, escape=False)
        
        html_table = html_table.replace('<td>2.', '<td class="high-rvol">🔥 2.')
        html_table = html_table.replace('<td>3.', '<td class="high-rvol">🔥 3.')
        html_table = html_table.replace('<td>4.', '<td class="high-rvol">🔥 4.')
        
        body = (f"{status_html}"
                f"<h2>🎯 發現 {len(target_df)} 檔異動標的：</h2>"
                f"{html_table}")
        
        body = body.replace('⚠️', '<span style="color:#e67e22; font-weight:bold;">⚠️</span>')
        body = body.replace('🟢', '<span style="color:#27ae60; font-weight:bold;">🟢</span>')
        body = body.replace('🔴', '<span style="color:#c0392b; font-weight:bold;">🔴</span>')
        body = body.replace('🚀', '<span style="color:#8e44ad; font-weight:bold;">🚀</span>')
        body = body.replace('【可以建倉】', '<span style="color:#27ae60; font-weight:bold;">【可以建倉】</span>')
        body = body.replace('【高風險避開】', '<span style="color:#c0392b; font-weight:bold;">【高風險避開】</span>')

    to_email = os.environ.get("MAIL_TO")
    from_email = os.environ.get("MAIL_USER")
    app_password = os.environ.get("MAIL_PASS")

    if to_email and from_email and app_password:
        send_scan_report_mail(subject, body, to_email, from_email, app_password)
    else:
        print("\n=== 🎯 本地終端機預覽 ===")
        print(fetch_status_msg)
        if not target_df.empty: print(target_df.to_markdown(index=False))
