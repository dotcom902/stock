import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime
import warnings
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import urllib.request
import xml.etree.ElementTree as ET

warnings.filterwarnings('ignore')

# ==========================================
# 新增：備用 RSS 新聞爬蟲 (突破 GitHub IP 限制)
# ==========================================
def get_yahoo_news_rss(ticker):
    try:
        # 偽裝成一般使用者的瀏覽器發送請求
        url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
        with urllib.request.urlopen(req, timeout=5) as response:
            xml_data = response.read()
            
        root = ET.fromstring(xml_data)
        news_items = []
        for item in root.findall('.//item')[:5]:
            title = item.find('title')
            if title is not None:
                news_items.append({'title': title.text, 'publisher': 'Yahoo RSS'})
        return news_items
    except Exception as e:
        print(f"[{ticker}] RSS 新聞抓取失敗: {e}")
        return []

# ==========================================
# 優化：動態抓取 Nasdaq 100 (回傳狀態給 Email)
# ==========================================
def get_nasdaq_100_tickers():
    print("正在從維基百科獲取 Nasdaq 100 最新成分股...")
    try:
        tables = pd.read_html('https://en.wikipedia.org/wiki/Nasdaq-100')
        for table in tables:
            if 'Ticker' in table.columns:
                tickers = table['Ticker'].tolist()
                status_msg = f"✅ <b>資料庫狀態</b>：成功從維基百科抓取 <b>{len(tickers)}</b> 檔 Nasdaq 100 最新成分股。"
                return tickers, status_msg
            elif 'Symbol' in table.columns:
                tickers = table['Symbol'].tolist()
                status_msg = f"✅ <b>資料庫狀態</b>：成功從維基百科抓取 <b>{len(tickers)}</b> 檔 Nasdaq 100 最新成分股。"
                return tickers, status_msg
    except Exception as e:
        tickers = ['AAPL', 'MSFT', 'NVDA', 'GOOGL', 'AMZN', 'META', 'TSLA', 'AVGO', 'COST', 'PEP']
        status_msg = f"⚠️ <b>資料庫狀態</b>：維基百科抓取失敗，目前使用 <b>{len(tickers)}</b> 檔備用大型指標股。"
        return tickers, status_msg
    
    return ['AAPL'], "⚠️ 發生未知錯誤，僅提供預設標的測試。"

# ==========================================
# 優化：消息面與基本面綜合評分系統 (加入 RSS Fallback)
# ==========================================
def get_fundamental_sentiment_score(ticker_obj, ticker_symbol):
    score = 50
    latest_news_str = "無最新新聞" 
    try:
        info = ticker_obj.info
        
        # --- 基本面評估 ---
        current_price = info.get('currentPrice', 0)
        target_price = info.get('targetMeanPrice', 0)
        
        if current_price > 0 and target_price > 0:
            upside = (target_price - current_price) / current_price
            if upside > 0.15: score += 15
            elif upside < 0: score -= 15

        rec = info.get('recommendationKey', '')
        if rec in ['buy', 'strong_buy']: score += 10
        elif rec in ['sell', 'strong_sell', 'underperform']: score -= 15

        # --- 消息面評估與新聞提取 ---
        bull_keywords = ['upgrade', 'beat', 'growth', 'surge', 'buy', 'higher', 'record', 'soar', 'jump']
        bear_keywords = ['downgrade', 'miss', 'cut', 'drop', 'lawsuit', 'sell', 'lower', 'weak', 'plunge', 'investigation']
        
        # 1. 先嘗試用原本的 API 抓取
        news = ticker_obj.news
        
        # 2. 如果被 GitHub 擋下 (news 為空)，啟動備用 RSS 爬蟲
        if not news:
            news = get_yahoo_news_rss(ticker_symbol)
            
        if news:
            news_score = 0
            news_list = [] 
            
            for i, article in enumerate(news[:5]):
                title = article.get('title', '')
                title_lower = title.lower()
                
                if any(k in title_lower for k in bull_keywords): news_score += 4
                if any(k in title_lower for k in bear_keywords): news_score -= 5
                
                if i < 2 and title:
                    publisher = article.get('publisher', 'News')
                    news_list.append(f"▪️ {title} ({publisher})")

            score += news_score
            
            if news_list:
                latest_news_str = "<br>".join(news_list)

        return max(0, min(100, score)), latest_news_str
    except Exception as e:
        print(f"評分系統發生錯誤: {e}")
        return 50, "抓取新聞失敗"

# ==========================================
# 策略與主掃描函數 (維持不變，僅傳遞 ticker_symbol)
# ==========================================
def get_investment_strategy(ticker_obj, current_price, score, signal_type):
    if signal_type == "超跌反彈":
        if score < 40: return {'綜合策略建議': '🔴 觀望/避免接刀', '期權履約價': '-', '預估年化報酬': '-'}
        try:
            exp_dates = ticker_obj.options
            today = datetime.today()
            target_date, target_days = None, 0
            for date_str in exp_dates:
                days_to_exp = (datetime.strptime(date_str, '%Y-%m-%d') - today).days
                if 25 <= days_to_exp <= 45:
                    target_date, target_days = date_str, days_to_exp
                    break
            if not target_date: return {'綜合策略建議': '🟢 買入正股 (無期權)', '期權履約價': '-', '預估年化報酬': '-'}
            chain = ticker_obj.option_chain(target_date)
            cushion = 0.92 if score >= 60 else 0.85
            suitable_puts = chain.puts[chain.puts['strike'] <= current_price * cushion]
            if suitable_puts.empty: return {'綜合策略建議': '🟢 逢低買入正股', '期權履約價': '-', '預估年化報酬': '-'}
            best_put = suitable_puts.sort_values(by='strike', ascending=False).iloc[0]
            premium = best_put['bid'] if best_put['bid'] > 0 else best_put['lastPrice']
            annual_roc = (premium / (best_put['strike'] - premium)) * 100 * (365 / target_days)
            action = '🟢 積極 Sell Put 或 買入正股' if score >= 60 else '🟡 保守 Sell Put'
            return {'綜合策略建議': action, '期權履約價': f"Put ${best_put['strike']} ({target_date})", '預估年化報酬': f"{round(annual_roc, 1)}%"}
        except: return {'綜合策略建議': '🟢 逢低買入正股', '期權履約價': '-', '預估年化報酬': '-'}
    elif signal_type == "動能突破":
        if score >= 65: return {'綜合策略建議': '🚀 順勢買入正股 / Buy Call', '期權履約價': '-', '預估年化報酬': '-'}
        elif score < 40: return {'綜合策略建議': '⚠️ 估值過高，考慮獲利了結', '期權履約價': '-', '預估年化報酬': '-'}
        else: return {'綜合策略建議': '⚪ 持有觀望，設好移動停損', '期權履約價': '-', '預估年化報酬': '-'}

def scan_market_opportunities(tickers):
    df_closes = yf.download(tickers, period="3mo", progress=False)['Close']
    df_closes = df_closes.dropna(axis=1, how='all')
    results = []
    
    for ticker in df_closes.columns:
        close_prices = df_closes[ticker].dropna()
        if len(close_prices) < 20: continue
            
        delta = close_prices.diff()
        gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
        loss = -delta.clip(upper=0).ewm(alpha=1/14, adjust=False).mean()
        rsi = 100 - (100 / (1 + gain / loss))
        latest_rsi = rsi.iloc[-1]
        
        ma20 = close_prices.rolling(window=20).mean()
        latest_price = close_prices.iloc[-1]
        bias_20 = ((latest_price - ma20.iloc[-1]) / ma20.iloc[-1]) * 100
        
        signal_type = None
        if latest_rsi < 35 and bias_20 < -6: signal_type = "超跌反彈"
        elif latest_rsi > 65 and bias_20 > 5: signal_type = "動能突破"
            
        if signal_type:
            stock_obj = yf.Ticker(ticker)
            # 注意這裡多傳入了 ticker 字串，給 RSS 爬蟲使用
            fund_score, latest_news = get_fundamental_sentiment_score(stock_obj, ticker)
            strategy = get_investment_strategy(stock_obj, latest_price, fund_score, signal_type)
            
            stock_data = {
                '代碼': ticker, '型態': signal_type, '現價': round(latest_price, 2),
                'RSI': round(latest_rsi, 2), '基本面評分': f"{int(fund_score)}分",
                '綜合策略建議': strategy['綜合策略建議'], '期權履約價': strategy['期權履約價'],
                '預估年化報酬': strategy['預估年化報酬'], '最新新聞': latest_news
            }
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
    # 取得清單，並同時取得抓取狀態的字串
    ndx_tickers, fetch_status_msg = get_nasdaq_100_tickers()
    target_df = scan_market_opportunities(ndx_tickers)
    
    subject = f"🧠 量化早報：Nasdaq 100 掃描與最新新聞 ({datetime.today().strftime('%Y-%m-%d')})"
    
    # 將抓取狀態包裝成漂亮的 HTML 區塊
    status_html = f"<div class='status-box'>{fetch_status_msg}</div>"
    
    if target_df.empty:
        body = f"{status_html}<h3>今日掃描結果</h3><p>目前 Nasdaq 100 中【沒有】標的符合條件。<br>市場可能處於震盪無方向狀態，建議保持耐心觀望。</p>"
    else:
        target_df = target_df.sort_values(by=['型態', '基本面評分'], ascending=[False, False])
        pd.set_option('display.max_colwidth', None)
        
        body = (f"{status_html}"
                f"<h2>🎯 發現 {len(target_df)} 檔 Nasdaq 100 潛力股：</h2>"
                f"<p>本次掃描包含<b>超跌反彈（逢低佈局/Sell Put）</b>與<b>動能突破（追強勢股）</b>，並附上最新催化劑新聞：</p>"
                f"{target_df.to_html(index=False, escape=False)}")
        
        body = body.replace('⚠️', '<span style="color:#e67e22; font-weight:bold;">⚠️</span>')
        body = body.replace('🟢', '<span style="color:#27ae60; font-weight:bold;">🟢</span>')
        body = body.replace('🔴', '<span style="color:#c0392b; font-weight:bold;">🔴</span>')
        body = body.replace('🚀', '<span style="color:#8e44ad; font-weight:bold;">🚀</span>')

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
