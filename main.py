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
# 新增：自動抓取並定立「安全收租期權策略 (Sell Put)」
# ==========================================
def get_safe_put_strategy(ticker_obj, current_price):
    try:
        exp_dates = ticker_obj.options
        if not exp_dates:
            return None

        today = datetime.today()
        target_date = None
        target_days = 0

        # 1. 尋找距離 25~45 天到期的合約 (Theta 流失最快的甜蜜點)
        for date_str in exp_dates:
            exp_date = datetime.strptime(date_str, '%Y-%m-%d')
            days_to_exp = (exp_date - today).days
            if 25 <= days_to_exp <= 45:
                target_date = date_str
                target_days = days_to_exp
                break
        
        # 如果沒有 25-45 天的，退而求其次抓近期的
        if not target_date and len(exp_dates) > 1:
            target_date = exp_dates[min(2, len(exp_dates)-1)]
            target_days = (datetime.strptime(target_date, '%Y-%m-%d') - today).days

        if not target_date or target_days <= 0:
            return None

        # 2. 獲取該到期日的期權鏈
        chain = ticker_obj.option_chain(target_date)
        puts = chain.puts
        
        # 3. 設定安全氣囊：尋找履約價在現價 10% 以下的合約 (10% OTM)
        target_strike_max = current_price * 0.90
        suitable_puts = puts[puts['strike'] <= target_strike_max]
        
        # 如果跌太深沒有 10% OTM 的報價，放寬到 5%
        if suitable_puts.empty:
            target_strike_max = current_price * 0.95
            suitable_puts = puts[puts['strike'] <= target_strike_max]
            if suitable_puts.empty: 
                return None

        # 取得最接近我們設定防守線的合約 (Sort by strike descending)
        best_put = suitable_puts.sort_values(by='strike', ascending=False).iloc[0]
        
        strike = best_put['strike']
        bid = best_put['bid']
        last_price = best_put['lastPrice']
        iv = best_put['impliedVolatility']
        
        # 盤後有時候 Bid 會是 0，改用 last_price 代替計算
        premium = bid if (bid > 0 and not pd.isna(bid)) else last_price
        if premium == 0 or pd.isna(premium):
            return None
        
        # 4. 關鍵指標計算
        cushion = ((current_price - strike) / current_price) * 100  # 安全墊比例
        capital_required = strike - premium # 現金擔保所需本金 (每股)
        roc = (premium / capital_required) * 100 # 區間報酬率
        annual_roc = roc * (365 / target_days) # 年化報酬率
        
        return {
            '策略': 'Sell Put',
            '到期日': target_date,
            '履約價': f"${strike}",
            '權利金預估': f"${round(premium, 2)}",
            '隱含波動率(IV)': f"{round(iv*100, 1)}%",
            '安全墊': f"{round(cushion, 1)}%",
            '預估年化報酬': f"{round(annual_roc, 1)}%"
        }
    except Exception as e:
        print(f"抓取期權資料時發生錯誤: {e}")
        return None

# ==========================================
# 主掃描函數 (結合技術面與期權面)
# ==========================================
def scan_oversold_opportunities(tickers):
    results = []
    end_date = datetime.today()
    start_date = end_date - timedelta(days=90)

    print(f"啟動雷達：正在掃描 {len(tickers)} 檔潛力股，並計算防禦型期權策略...")
    for ticker in tickers:
        try:
            stock = yf.Ticker(ticker)
            df = stock.history(start=start_date, end=end_date)
            if df.empty or len(df) < 20:
                continue
            close_prices = df['Close']

            # 計算 RSI
            delta = close_prices.diff()
            gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
            loss = -delta.clip(upper=0).ewm(alpha=1/14, adjust=False).mean()
            rs = gain / loss
            rsi = 100 - (100 / (1 + rs))
            df['RSI_14'] = rsi

            # 計算 20MA 與乖離率
            ma20 = close_prices.rolling(window=20).mean()
            df['MA_20'] = ma20
            bias_20 = ((close_prices - ma20) / ma20) * 100
            df['Bias_20'] = bias_20

            latest = df.iloc[-1]
            current_price = latest['Close']
            
            # 🎯 條件：RSI < 35 且 乖離率 < -8%
            if latest['RSI_14'] < 35 and latest['Bias_20'] < -8:
                
                # 若符合超賣，進一步抓取期權策略
                opt_strategy = get_safe_put_strategy(stock, current_price)
                
                # 組合數據字典
                stock_data = {
                    '代碼': ticker,
                    '現價': round(current_price, 2),
                    'RSI': round(latest['RSI_14'], 2),
                    '乖離率': f"{round(latest['Bias_20'], 2)}%",
                }
                
                # 如果有期權數據，合併進去；如果沒有，填入 N/A
                if opt_strategy:
                    stock_data.update(opt_strategy)
                else:
                    stock_data.update({
                        '策略': '資料不足', '到期日': '-', '履約價': '-', 
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
    
    # 加上簡單的 CSS 讓郵件表格變好看
    html_style = """
    <style>
        table { border-collapse: collapse; width: 100%; font-family: Arial; }
        th { background-color: #2C3E50; color: white; padding: 10px; text-align: center; }
        td { border: 1px solid #ddd; padding: 8px; text-align: center; }
        tr:nth-child(even) { background-color: #f2f2f2; }
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
    subject = "📊 量化早報：超賣雷達與 Sell Put 期權佈局"
    
    if target_df.empty:
        body = ("目前市場情緒穩定，這批名單中【沒有】標的符合極度超賣條件。<br>"
                "資金處於安全水位，建議保持耐心。")
        as_html = True
    else:
        body = (f"<h2>🎯 發現 {len(target_df)} 檔潛在錯殺標的與收租策略：</h2>"
                f"<p>以下策略以 <b>Sell Put (賣出賣權)</b> 構建，預設距離現價有約 10% 的安全緩衝區。</p>"
                f"{target_df.to_html(index=False)}")
        as_html = True

    # 讀取環境變數發信
    to_email = os.environ.get("MAIL_TO")
    from_email = os.environ.get("MAIL_USER")
    app_password = os.environ.get("MAIL_PASS")

    if to_email and from_email and app_password:
        send_scan_report_mail(subject, body, to_email, from_email, app_password, as_html=as_html)
        print("📧 報告與期權策略已寄出!")
    else:
        print("\n=== 🎯 本地終端機預覽 ===")
        if target_df.empty:
            print("目前沒有超賣標的。")
        else:
            print(target_df.to_markdown(index=False))
        print("\n❌ 未設定 MAIL 環境變數，無法寄信。")
