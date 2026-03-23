import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
import warnings
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

warnings.filterwarnings('ignore')

# 掃描股票池 (去重)
scan_universe = list(set([
    'NVDA', 'AMD', 'TSM', 'AVGO', 'MU', 'MSFT', 'GOOGL', 'META', 'PLTR',
    'MSTR', 'COIN', 'IREN', 'CLSK', 'MARA', 'CRCA',
    'RKLB', 'ASTS', 'SIDU', 'BKSY', 'ONDS',
    'SERV', 'SYM', 'RR',
    'GDX', 'SLV',
    'TSLA', 'SMCI', 'ARM'
]))

# ==========================================
# 新增 1：消息面與基本面綜合評分系統 (0~100分)
# ==========================================
def get_fundamental_sentiment_score(ticker_obj):
    score = 50  # 基礎分數：中性 50分
    try:
        info = ticker_obj.info
        news = ticker_obj.news

        # 1. 基本面評估：分析師目標價與評級
        current_price = info.get('currentPrice', 0)
        target_price = info.get('targetMeanPrice', 0)
        
        # 如果目標價高於現價 15% 以上，加分；反之扣分
        if current_price > 0 and target_price > 0:
            upside = (target_price - current_price) / current_price
            if upside > 0.15:
                score += 15
            elif upside < 0:
                score -= 15

        # 分析師推薦評級
        rec = info.get('recommendationKey', '')
        if rec in ['buy', 'strong_buy']:
            score += 10
        elif rec in ['sell', 'strong_sell', 'underperform']:
            score -= 15

        # 2. 消息面評估：輕量級 NLP 標題情緒分析
        bull_keywords = ['upgrade', 'beat', 'growth', 'surge', 'buy', 'higher', 'record', 'soar', 'jump']
        bear_keywords = ['downgrade', 'miss', 'cut', 'drop', 'lawsuit', 'sell', 'lower', 'weak', 'plunge', 'investigation']
        
        if news:
            news_score = 0
            # 抓取最新的前 5 篇新聞標題
            for article in news[:5]:
                title = article.get('title', '').lower()
                if any(k in title for k in bull_keywords):
                    news_score += 4  # 發現利多字眼加 4 分
                if any(k in title for k in bear_keywords):
                    news_score -= 5  # 發現利空字眼扣 5 分
            score += news_score

        # 確保分數落在 0 到 100 之間
        score = max(0, min(100, score))
        return score
    except Exception as e:
        print(f"抓取基本面數據時出錯: {e}")
        return 50 # 若抓取失敗則給予中性 50 分

# ==========================================
# 新增 2：結合評分系統的「動態期權策略」
# ==========================================
def get_dynamic_option_strategy(ticker_obj, current_price, score):
    try:
        # 分數太低 (< 40)，基本面惡化，直接不推薦 Sell Put
        if score < 40:
            return {
                '綜合策略建議': '⚠️ 觀望/避免接刀 (基本面惡化)',
                '到期日': '-', '履約價': '-', '權利金預估': '-', 
                '隱含波動率(IV)': '-', '安全墊': '-', '預估年化報酬': '-'
            }

        exp_dates = ticker_obj.options
        if not exp_dates:
            return None

        today = datetime.today()
        target_date = None
        target_days = 0

        # 尋找 25~45 天 Theta 甜蜜點
        for date_str in exp_dates:
            exp_date = datetime.strptime(date_str, '%Y-%m-%d')
            days_to_exp = (exp_date - today).days
            if 25 <= days_to_exp <= 45:
                target_date = date_str
                target_days = days_to_exp
                break
        
        if not target_date and len(exp_dates) > 1:
            target_date = exp_dates[min(2, len(exp_dates)-1)]
            target_days = (datetime.strptime(target_date, '%Y-%m-%d') - today).days

        if not target_date or target_days <= 0:
            return None

        chain = ticker_obj.option_chain(target_date)
        puts = chain.puts
        
        # 🎯 動態調整安全氣囊：
        # 分數高 (>60)，防守距離可以近一點 (5%-8%)，賺多點權利金
        # 分數中 (40-60)，防守距離拉遠 (10%-15%)，保守收租
        cushion_multiplier = 0.92 if score >= 60 else 0.85
        strategy_name = '🟢 積極 Sell Put' if score >= 60 else '🟡 保守 Sell Put'

        target_strike_max = current_price * cushion_multiplier
        suitable_puts = puts[puts['strike'] <= target_strike_max]
        
        if suitable_puts.empty:
            return None

        best_put = suitable_puts.sort_values(by='strike', ascending=False).iloc[0]
        
        strike = best_put['strike']
        bid = best_put['bid']
        last_price = best_put['lastPrice']
        iv = best_put['impliedVolatility']
        
        premium = bid if (bid > 0 and not pd.isna(bid)) else last_price
        if premium == 0 or pd.isna(premium):
            return None
        
        cushion = ((current_price - strike) / current_price) * 100
        capital_required = strike - premium
        roc = (premium / capital_required) * 100
        annual_roc = roc * (365 / target_days)
        
        return {
            '綜合策略建議': strategy_name,
            '到期日': target_date,
            '履約價': f"${strike}",
            '權利金預估': f"${round(premium, 2)}",
            '隱含波動率(IV)': f"{round(iv*100, 1)}%",
            '安全墊': f"{round(cushion, 1)}%",
            '預估年化報酬': f"{round(annual_roc, 1)}%"
        }
    except Exception as e:
        return None

# ==========================================
# 主掃描函數 (技術面 + 基本面 + 期權)
# ==========================================
def scan_oversold_opportunities(tickers):
    results = []
    end_date = datetime.today()
    start_date = end_date - timedelta(days=90)

    print(f"啟動全方位雷達：正在掃描 {len(tickers)} 檔潛力股 (技術+基本+期權)...")
    for ticker in tickers:
        try:
            stock = yf.Ticker(ticker)
            df = stock.history(start=start_date, end=end_date)
            if df.empty or len(df) < 20:
                continue
            close_prices = df['Close']

            # 計算 RSI & 乖離率
            delta = close_prices.diff()
            gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
            loss = -delta.clip(upper=0).ewm(alpha=1/14, adjust=False).mean()
            rs = gain / loss
            rsi = 100 - (100 / (1 + rs))
            df['RSI_14'] = rsi

            ma20 = close_prices.rolling(window=20).mean()
            df['MA_20'] = ma20
            bias_20 = ((close_prices - ma20) / ma20) * 100
            df['Bias_20'] = bias_20

            latest = df.iloc[-1]
            current_price = latest['Close']
            
            # 🎯 條件：RSI < 35 且 乖離率 < -8%
            if latest['RSI_14'] < 35 and latest['Bias_20'] < -8:
                
                # 1. 取得基本面與消息面評分
                fund_score = get_fundamental_sentiment_score(stock)
                
                # 2. 結合評分，取得動態期權策略
                opt_strategy = get_dynamic_option_strategy(stock, current_price, fund_score)
                
                stock_data = {
                    '代碼': ticker,
                    '現價': round(current_price, 2),
                    'RSI': round(latest['RSI_14'], 2),
                    '基本面評分': f"{int(fund_score)}分",
                }
                
                if opt_strategy:
                    stock_data.update(opt_strategy)
                else:
                    stock_data.update({
                        '綜合策略建議': '無合適期權報價', '到期日': '-', '履約價': '-', 
                        '權利金預估': '-', '隱含波動率(IV)': '-', 
                        '安全墊': '-', '預估年化報酬': '-'
                    })
                    
                results.append(stock_data)
        except Exception:
            pass

    return pd.DataFrame(results)

# ==========================================
# 郵件寄送模組
# ==========================================
def send_scan_report_mail(subject, body, to_email, from_email, app_password, as_html=False):
    msg = MIMEMultipart()
    msg['From'] = from_email
    msg['To'] = to_email
    msg['Subject'] = subject
    subtype = 'html' if as_html else 'plain'
    
    html_style = """
    <style>
        table { border-collapse: collapse; width: 100%; font-family: Arial; font-size: 14px; }
        th { background-color: #1a252f; color: white; padding: 12px; text-align: center; }
        td { border: 1px solid #bdc3c7; padding: 10px; text-align: center; }
        tr:nth-child(even) { background-color: #ecf0f1; }
        .score-high { color: #27ae60; font-weight: bold; }
        .score-low { color: #e74c3c; font-weight: bold; }
    </style>
    """
    if as_html:
        body = f"<html><head>{html_style}</head><body>{body}</body></html>"

    msg.attach(MIMEText(body, subtype))

    smtp_server = 'smtp.gmail.com'
    smtp_port = 587

    with smtplib.SMTP(smtp_server, smtp_port) as server:
        server.starttls()
        server.login(from_email, app_password)
        server.sendmail(from_email, to_email, msg.as_string())

if __name__ == "__main__":
    target_df = scan_oversold_opportunities(scan_universe)
    subject = "🧠 量化早報：超賣雷達 x 基本面評分 x 動態期權策略"
    
    if target_df.empty:
        body = ("目前市場情緒穩定，這批名單中【沒有】標的符合極度超賣條件。<br>"
                "資金處於安全水位，建議保持耐心。")
        as_html = True
    else:
        # 美化郵件輸出
        body = (f"<h2>🎯 發現 {len(target_df)} 檔技術面超賣標的：</h2>"
                f"<p>系統已結合分析師目標價與最新新聞標題進行綜合評分：<br>"
                f"▪️ <b>>60分</b>：基本面優良，錯殺機率高，建議積極 Sell Put。<br>"
                f"▪️ <b>40-60分</b>：震盪中性，建議拉大安全墊保守收租。<br>"
                f"▪️ <b><40分</b>：基本面惡化（財報暴雷/利空），建議避開接飛刀。</p>"
                f"{target_df.to_html(index=False)}")
        
        # 針對分數進行 HTML 顏色標記，信件內文更直觀
        body = body.replace('⚠️', '<span style="color:red; font-weight:bold;">⚠️</span>')
        body = body.replace('🟢', '<span style="color:green; font-weight:bold;">🟢</span>')
        as_html = True

    # 讀取環境變數發信
    to_email = os.environ.get("MAIL_TO")
    from_email = os.environ.get("MAIL_USER")
    app_password = os.environ.get("MAIL_PASS")

    if to_email and from_email and app_password:
        send_scan_report_mail(subject, body, to_email, from_email, app_password, as_html=as_html)
        print("📧 報告已寄出!")
    else:
        print("\n=== 🎯 本地終端機預覽 ===")
        if target_df.empty:
            print("目前沒有超賣標的。")
        else:
            print(target_df.to_markdown(index=False))
        print("\n❌ 未設定 MAIL 環境變數，無法寄信。")
