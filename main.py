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

warnings.filterwarnings('ignore')

# ==========================================
# 參數設定區
# ==========================================
MY_PORTFOLIO = ['NVDA', 'TSM', 'AVGO', 'PLTR', 'MSTR'] 

# ==========================================
# 網路連線設定 (偽裝成真人瀏覽器，突破 GitHub IP 限制)
# ==========================================
# 建立一個全域的 requests Session，所有的抓取都透過這個「假瀏覽器」進行
REQ_SESSION = requests.Session()
REQ_SESSION.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5'
})

# ==========================================
# 新增：三層瀑布流新聞抓取模組 (終極防擋機制)
# ==========================================
def get_robust_news(ticker_obj, ticker_symbol):
    news_items = []
    
    # [第一層] 嘗試 yfinance 原生 API (最高品質)
    try:
        news = ticker_obj.news
        if news and len(news) > 0:
            for item in news[:5]:
                if 'title' in item:
                    news_items.append({'title': item['title'], 'publisher': item.get('publisher', 'Yahoo')})
            if news_items: return news_items
    except: pass

    # [第二層] 嘗試 Yahoo Finance 隱藏版 RSS (原生新聞)
    try:
        url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker_symbol}&region=US&lang=en-US"
        response = REQ_SESSION.get(url, timeout=5)
        if response.status_code == 200:
            root = ET.fromstring(response.content)
            for item in root.findall('.//item')[:5]:
                title_elem = item.find('title')
                if title_elem is not None:
                    news_items.append({'title': title_elem.text, 'publisher': 'Yahoo RSS'})
            if news_items: return news_items
    except: pass

    # [第三層] 嘗試 Google News RSS (最強備援)
    try:
        query = urllib.parse.quote(f"{ticker_symbol} stock")
        url = f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
        response = REQ_SESSION.get(url, timeout=5)
        if response.status_code == 200:
            root = ET.fromstring(response.content)
            for item in root.findall('.//channel/item')[:5]:
                title_elem = item.find('title')
                if title_elem is not None:
                    news_items.append({'title': title_elem.text, 'publisher': 'Google News'})
            if news_items: return news_items
    except: pass

    return [] # 三層都失敗才回傳空值

# ==========================================
# 動態抓取 Nasdaq 100
# ==========================================
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
        return ['AAPL', 'MSFT', 'NVDA', 'GOOGL', 'AMZN'], f"⚠️ 抓取失敗，使用備用清單 ({e})"
    return ['AAPL'], "⚠️ 發生未知錯誤"

# ==========================================
# 模組化：數據驗證與基本面抓取
# ==========================================
def get_fundamental_sentiment_score(ticker_obj, ticker_symbol):
    score = 50
    latest_news_str = "無最新新聞" 
    try:
        info = ticker_obj.info
        if not info or not isinstance(info, dict):
            raise ValueError("無法獲取股票 info 數據")

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
        
        # 呼叫三層瀑布流抓新聞
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
            if news_display:
                latest_news_str = "<br><br>".join(news_display)

        return max(0, min(100, score)), latest_news_str
    except Exception as e:
        print(f"[{ticker_symbol}] 評分系統錯誤: {e}")
        return 50, "數據抓取異常"

# ==========================================
# 模組化：期權策略建議 
# ==========================================
def get_investment_strategy(ticker_obj, current_price, score, signal_type):
    if signal_type == "超跌反彈":
        if score < 40: return {'綜合建議': '🔴 觀望(基本面轉弱)', '期權履約價': '-', '年化報酬': '-'}
        try:
            exp_dates = ticker_obj.options
            if not exp_dates or len(exp_dates) == 0: raise ValueError("無期權日期")
                
            today = datetime.today()
            target_date, target_days = None, 0
            for date_str in exp_dates:
                days_to_exp = (datetime.strptime(date_str, '%Y-%m-%d') - today).days
                if 25 <= days_to_exp <= 45:
                    target_date, target_days = date_str, days_to_exp
                    break
                    
            if not target_date: return {'綜合建議': '🟢 買入正股(無合適期權)', '期權履約價': '-', '年化報酬': '-'}
            
            chain = ticker_obj.option_chain(target_date)
            if chain.puts.empty: raise ValueError("無 Put 報價")
            
            cushion = 0.92 if score >= 60 else 0.85
            suitable_puts = chain.puts[chain.puts['strike'] <= current_price * cushion]
            
            if suitable_puts.empty: return {'綜合建議': '🟢 買入正股(防守空間不足)', '期權履約價': '-', '年化報酬': '-'}
            
            best_put = suitable_puts.sort_values(by='strike', ascending=False).iloc[0]
            premium = best_put['bid'] if best_put['bid'] > 0 else best_put['lastPrice']
            
            if premium <= 0 or best_put['strike'] - premium <= 0: raise ValueError("權利金異常")
                
            annual_roc = (premium / (best_put['strike'] - premium)) * 100 * (365 / target_days)
            action = '🟢 積極 Sell Put / 買入正股' if score >= 60 else '🟡 保守 Sell Put'
            return {'綜合建議': action, '期權履約價': f"Put ${best_put['strike']} ({target_date})", '年化報酬': f"{round(annual_roc, 1)}%"}
        except: 
            return {'綜合建議': '🟢 買入正股', '期權履約價': '-', '年化報酬': '-'}
            
    elif signal_type == "動能突破":
        if score >= 65: return {'綜合建議': '🚀 順勢買入正股 / Buy Call', '期權履約價': '-', '年化報酬': '-'}
        elif score < 40: return {'綜合建議': '⚠️ 估值過高，考慮獲利了結', '期權履約價': '-', '年化報酬': '-'}
        else: return {'綜合建議': '⚪ 持有觀望，設好移動停損', '期權履約價': '-', '年化報酬': '-'}

# ==========================================
# 主掃描函數 
# ==========================================
def scan_market_opportunities(tickers_list, portfolio_list):
    all_tickers = list(set(tickers_list + portfolio_list))
    
    df_data = yf.download(all_tickers, period="3mo", progress=False)
    if df_data.empty:
        print("❌ 歷史股價下載完全失敗！")
        return pd.DataFrame()
        
    df_closes = df_data['Close'].dropna(axis=1, how='all')
    df_volumes = df_data['Volume'].dropna(axis=1, how='all')
    
    results = []
    
    for ticker in df_closes.columns:
        close_prices = df_closes[ticker].dropna()
        if len(close_prices) < 20: continue 
            
        try:
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
            vol_ma20 = volumes.rolling(window=20).mean().iloc[-1]
            latest_vol = volumes.iloc[-1]
            vol_surge = latest_vol > vol_ma20 * 1.2 
            
            signal_type = None
            if latest_rsi < 35 and bias_20 < -6: signal_type = "超跌反彈"
            elif latest_rsi > 65 and bias_20 > 5 and vol_surge: signal_type = "動能突破"
                
            is_portfolio = ticker in portfolio_list
            if is_portfolio and not signal_type: signal_type = "持倉監控"
                
            if signal_type:
                print(f"分析中: {ticker} ({signal_type})...")
                # ✨ 關鍵點：在這裡套用我們建構好的「偽裝 Session」給 yfinance
                stock_obj = yf.Ticker(ticker, session=REQ_SESSION)
                fund_score, latest_news = get_fundamental_sentiment_score(stock_obj, ticker)
                strategy = get_investment_strategy(stock_obj, latest_price, fund_score, signal_type)
                
                if signal_type == "持倉監控":
                    strategy['綜合建議'] = "🔹 日常追蹤 (未觸發極端訊號)" if fund_score >= 50 else "⚠️ 基本面弱化，留意停損"

                stock_data = {
                    '身份': '💼 我的持倉' if is_portfolio else '🔍 掃描發現',
                    '代碼': ticker, '型態': signal_type, '現價': round(latest_price, 2),
                    'RSI': round(latest_rsi, 2), '基本面評分': f"{int(fund_score)}分",
                    '綜合建議': strategy['綜合建議'], '期權履約價': strategy['期權履約價'],
                    '年化報酬': strategy['年化報酬'], '最新新聞': latest_news
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
        body { font-family: Arial, sans-serif; color: #333; }
        .status-box { background-color: #f8f9fa; border-left: 4px solid #3498db; padding: 10px 15px; margin-bottom: 20px; font-size: 14px; }
        table { border-collapse: collapse; width: 100%; font-size: 13px; table-layout: fixed; }
        th { background-color: #2c3e50; color: white; padding: 10px; text-align: center; }
        td { border: 1px solid #bdc3c7; padding: 8px; text-align: center; word-wrap: break-word; }
        tr:nth-child(even) { background-color: #f2f2f2; }
        th:last-child { width: 35%; }
        td:last-child { text-align: left; font-size: 12px; color: #34495e; line-height: 1.4; }
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
    target_df = scan_market_opportunities(ndx_tickers, MY_PORTFOLIO)
    
    subject = f"🧠 量化早報：波段掃描與持倉監控 ({datetime.today().strftime('%Y-%m-%d')})"
    status_html = f"<div class='status-box'>{fetch_status_msg}<br>💼 已載入持倉股票監控：{', '.join(MY_PORTFOLIO)}</div>"
    
    if target_df.empty:
        body = f"{status_html}<h3>今日掃描結果</h3><p>系統無回傳任何資料，請檢查日誌。</p>"
    else:
        target_df = target_df.sort_values(by=['身份', '型態', '基本面評分'], ascending=[False, False, False])
        pd.set_option('display.max_colwidth', None)
        
        body = (f"{status_html}"
                f"<h2>🎯 發現 {len(target_df)} 檔關注標的：</h2>"
                f"<p>已套用成交量濾網與三層新聞防護機制。</p>"
                f"{target_df.to_html(index=False, escape=False)}")
        
        body = body.replace('⚠️', '<span style="color:#e67e22; font-weight:bold;">⚠️</span>')
        body = body.replace('🟢', '<span style="color:#27ae60; font-weight:bold;">🟢</span>')
        body = body.replace('🔴', '<span style="color:#c0392b; font-weight:bold;">🔴</span>')
        body = body.replace('🚀', '<span style="color:#8e44ad; font-weight:bold;">🚀</span>')
        body = body.replace('💼', '<span style="color:#2980b9; font-weight:bold;">💼</span>')
        body = body.replace('🔹', '<span style="color:#3498db; font-weight:bold;">🔹</span>')

    to_email = os.environ.get("MAIL_TO")
    from_email = os.environ.get("MAIL_USER")
    app_password = os.environ.get("MAIL_PASS")

    if to_email and from_email and app_password:
        send_scan_report_mail(subject, body, to_email, from_email, app_password)
    else:
        print("\n=== 🎯 本地終端機預覽 ===")
        print(fetch_status_msg)
        if target_df.empty: print("目前沒有符合條件的標的。")
        else: print(target_df.to_markdown(index=False))
