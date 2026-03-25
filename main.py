import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime
import warnings
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

warnings.filterwarnings('ignore')

# ==========================================
# 動態抓取 Nasdaq 100 成分股
# ==========================================
def get_nasdaq_100_tickers():
    print("正在從維基百科獲取 Nasdaq 100 最新成分股...")
    try:
        tables = pd.read_html('https://en.wikipedia.org/wiki/Nasdaq-100')
        for table in tables:
            if 'Ticker' in table.columns:
                return table['Ticker'].tolist()
            elif 'Symbol' in table.columns:
                return table['Symbol'].tolist()
    except Exception as e:
        print(f"獲取 Nasdaq 100 失敗，使用備用清單: {e}")
        return ['AAPL', 'MSFT', 'NVDA', 'GOOGL', 'AMZN', 'META', 'TSLA', 'AVGO', 'COST', 'RKLB','CRCL']

# ==========================================
# 消息面與基本面綜合評分系統 + 提取最新2則新聞
# ==========================================
def get_fundamental_sentiment_score(ticker_obj):
    score = 50
    latest_news_str = "無最新新聞" # 預設值
    try:
        info = ticker_obj.info
        news = ticker_obj.news

        # 1. 基本面評估
        current_price = info.get('currentPrice', 0)
        target_price = info.get('targetMeanPrice', 0)
        
        if current_price > 0 and target_price > 0:
            upside = (target_price - current_price) / current_price
            if upside > 0.15: score += 15
            elif upside < 0: score -= 15

        rec = info.get('recommendationKey', '')
        if rec in ['buy', 'strong_buy']: score += 10
        elif rec in ['sell', 'strong_sell', 'underperform']: score -= 15

        # 2. 消息面評估與新聞提取
        bull_keywords = ['upgrade', 'beat', 'growth', 'surge', 'buy', 'higher', 'record', 'soar', 'jump']
        bear_keywords = ['downgrade', 'miss', 'cut', 'drop', 'lawsuit', 'sell', 'lower', 'weak', 'plunge', 'investigation']
        
        if news:
            news_score = 0
            news_list = [] # 用來存放要顯示的新聞標題
            
            for i, article in enumerate(news[:5]):
                title = article.get('title', '')
                title_lower = title.lower()
                
                # 計算情緒分數
                if any(k in title_lower for k in bull_keywords): news_score += 4
                if any(k in title_lower for k in bear_keywords): news_score -= 5
                
                # 提取前 2 篇新聞標題 (包含發布來源)
                if i < 2 and title:
                    publisher = article.get('publisher', 'News')
                    news_list.append(f"▪️ {title} ({publisher})")

            score += news_score
            
            # 將 2 則新聞組合成 HTML 換行格式
            if news_list:
                latest_news_str = "<br>".join(news_list)

        return max(0, min(100, score)), latest_news_str
    except Exception:
        return 50, "抓取新聞失敗"

# ==========================================
# 多面向投資策略建議 (包含期權與正股)
# ==========================================
def get_investment_strategy(ticker_obj, current_price, score, signal_type):
    if signal_type == "超跌反彈":
        if score < 40:
            return {'綜合策略建議': '🔴 觀望/避免接刀', '期權履約價': '-', '預估年化報酬': '-'}
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
            target_strike_max = current_price * cushion
            suitable_puts = chain.puts[chain.puts['strike'] <= target_strike_max]
            
            if suitable_puts.empty:
                return {'綜合策略建議': '🟢 逢低買入正股', '期權履約價': '-', '預估年化報酬': '-'}
                
            best_put = suitable_puts.sort_values(by='strike', ascending=False).iloc[0]
            premium = best_put['bid'] if best_put['bid'] > 0 else best_put['lastPrice']
            annual_roc = (premium / (best_put['strike'] - premium)) * 100 * (365 / target_days)

            action = '🟢 積極 Sell Put 或 買入正股' if score >= 60 else '🟡 保守 Sell Put'
            return {
                '綜合策略建議': action,
                '期權履約價': f"Put ${best_put['strike']} ({target_date})",
                '預估年化報酬': f"{round(annual_roc, 1)}%"
            }
        except Exception:
            return {'綜合策略建議': '🟢 逢低買入正股', '期權履約價': '-', '預估年化報酬': '-'}

    elif signal_type == "動能突破":
        if score >= 65:
            return {'綜合策略建議': '🚀 順勢買入正股 / Buy Call', '期權履約價': '-', '預估年化報酬': '-'}
        elif score < 40:
            return {'綜合策略建議': '⚠️ 估值過高，考慮獲利了結', '期權履約價': '-', '預估年化報酬': '-'}
        else:
            return {'綜合策略建議': '⚪ 持有觀望，設好移動停損', '期權履約價': '-', '預估年化報酬': '-'}

# ==========================================
# 主掃描函數 
# ==========================================
def scan_market_opportunities(tickers):
    print(f"啟動全方位雷達：批次下載 {len(tickers)} 檔技術線型 (這將花費幾秒鐘)...")
    
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
        if latest_rsi < 35 and bias_20 < -6:
            signal_type = "超跌反彈"
        elif latest_rsi > 65 and bias_20 > 5:
            signal_type = "動能突破"
            
        if signal_type:
            print(f"發現潛力股: {ticker} ({signal_type}) - 正在深度分析...")
            stock_obj = yf.Ticker(ticker)
            
            # 取得分數與最新兩則新聞
            fund_score, latest_news = get_fundamental_sentiment_score(stock_obj)
            strategy = get_investment_strategy(stock_obj, latest_price, fund_score, signal_type)
            
            stock_data = {
                '代碼': ticker,
                '型態': signal_type,
                '現價': round(latest_price, 2),
                'RSI': round(latest_rsi, 2),
                '基本面評分': f"{int(fund_score)}分",
                '綜合策略建議': strategy['綜合策略建議'],
                '期權履約價': strategy['期權履約價'],
                '預估年化報酬': strategy['預估年化報酬'],
                '最新新聞': latest_news  # 將新聞加在最後一欄
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
    
    # 優化 CSS：讓新聞欄位靠左且自動換行，不會把表格撐破
    html_style = """
    <style>
        table { border-collapse: collapse; width: 100%; font-family: Arial, sans-serif; font-size: 13px; table-layout: fixed; }
        th { background-color: #2c3e50; color: white; padding: 10px; text-align: center; }
        td { border: 1px solid #bdc3c7; padding: 8px; text-align: center; word-wrap: break-word; }
        tr:nth-child(even) { background-color: #f2f2f2; }
        /* 針對最後一個欄位 (最新新聞) 設定寬度與靠左對齊 */
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
    ndx_tickers = get_nasdaq_100_tickers()
    target_df = scan_market_opportunities(ndx_tickers)
    
    subject = f"🧠 量化早報：Nasdaq 100 掃描與最新新聞 ({datetime.today().strftime('%Y-%m-%d')})"
    
    if target_df.empty:
        body = "<h3>今日掃描結果</h3><p>目前 Nasdaq 100 中【沒有】標的符合極度超賣或強勢突破條件。<br>市場可能處於震盪無方向狀態，建議保持耐心觀望。</p>"
    else:
        target_df = target_df.sort_values(by=['型態', '基本面評分'], ascending=[False, False])
        
        # 允許 HTML 標籤在 DataFrame 轉換時不被 escape 掉 (這樣 <br> 才會生效)
        pd.set_option('display.max_colwidth', None)
        
        body = (f"<h2>🎯 發現 {len(target_df)} 檔 Nasdaq 100 潛力股：</h2>"
                f"<p>本次掃描包含<b>超跌反彈（逢低佈局/Sell Put）</b>與<b>動能突破（追強勢股）</b>，並附上最新催化劑新聞：</p>"
                f"{target_df.to_html(index=False, escape=False)}") # escape=False 確保 <br> 正常渲染
        
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
        if target_df.empty:
            print("目前沒有符合條件的標的。")
        else:
            print(target_df.to_markdown(index=False))
        print("\n❌ 未設定 MAIL 環境變數，無法寄信。")

