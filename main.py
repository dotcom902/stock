import yfinance as yf
import pandas as pd
from datetime import datetime
import warnings
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

warnings.filterwarnings('ignore')

# 掃描股票池 (去重)
scan_universe = list(set([
    'NVDA', 'AMD', 'TSM', 'AVGO', 'MU', 'MSFT', 'GOOGL', 'META', 'PLTR', 'SNDK'
    'MSTR', 'COIN', 'IREN', 'CLSK', 'MARA', 'CRCA',
    'RKLB', 'ASTS', 'SIDU', 'BKSY', 'ONDS',
    'SERV', 'SYM', 'RR',
    'GDX', 'SLV',
    'TSLA', 'SMCI', 'ARM'
    'COHR', 'LITE', 'CRDO'
    'QQQ','TQQQ' ,
]))

# ==========================================
# 盤中防禦型期權策略 (邏輯不變，抓取 25~45 天 OTM 收租)
# ==========================================
def get_safe_put_strategy(ticker_obj, current_price):
    try:
        exp_dates = ticker_obj.options
        if not exp_dates:
            return None

        today = datetime.today()
        target_date = None
        target_days = 0

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
        
        # 盤中波動大，預設尋找 10% OTM (價外) 的合約
        target_strike_max = current_price * 0.90
        suitable_puts = puts[puts['strike'] <= target_strike_max]
        
        if suitable_puts.empty:
            target_strike_max = current_price * 0.95
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
            '策略': 'Sell Put',
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
# 盤中 15分鐘級別 掃描器
# ==========================================
def scan_intraday_oversold(tickers):
    results = []
    print(f"啟動盤中雷達：正在掃描 {len(tickers)} 檔潛力股 (15分鐘級別)...")
    
    for ticker in tickers:
        try:
            stock = yf.Ticker(ticker)
            # 取得最近 5 天的 15 分鐘 K 線數據
            df = stock.history(period="5d", interval="15m")
            
            if df.empty or len(df) < 20:
                continue
            
            close_prices = df['Close']

            # 計算 15m RSI (14)
            delta = close_prices.diff()
            gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
            loss = -delta.clip(upper=0).ewm(alpha=1/14, adjust=False).mean()
            rs = gain / loss
            rsi = 100 - (100 / (1 + rs))
            df['RSI_14'] = rsi

            # 計算 15m 20MA 與乖離率
            ma20 = close_prices.rolling(window=20).mean()
            df['MA_20'] = ma20
            bias_20 = ((close_prices - ma20) / ma20) * 100
            df['Bias_20'] = bias_20

            latest = df.iloc[-1]
            current_price = latest['Close']
            
            # 🎯 盤中當沖專用門檻：15m RSI < 30 且 15m 乖離率 < -3.5%
            # (15分鐘級別偏離 20MA 達 3.5% 以上已是非常強烈的日內急跌)
            if latest['RSI_14'] < 30 and latest['Bias_20'] < -3.5:
                
                opt_strategy = get_safe_put_strategy(stock, current_price)
                
                stock_data = {
                    '代碼': ticker,
                    '當下報價': round(current_price, 2),
                    '15m RSI': round(latest['RSI_14'], 2),
                    '15m 乖離率': f"{round(latest['Bias_20'], 2)}%",
                }
                
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
    
    html_style = """
    <style>
        table { border-collapse: collapse; width: 100%; font-family: Arial; }
        th { background-color: #D35400; color: white; padding: 10px; text-align: center; }
        td { border: 1px solid #ddd; padding: 8px; text-align: center; }
        tr:nth-child(even) { background-color: #fdf2e9; }
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
    target_df = scan_intraday_oversold(scan_universe)
    
    # 標題加上時間戳記，確保你知道這是幾點的快照
    now_str = datetime.now().strftime('%H:%M:%S')
    subject = f"⚡ 盤中動能警報 (22:30)：15分鐘級別超賣與 Sell Put 佈局"
    
    if target_df.empty:
        body = ("<h3>✅ 目前盤中情緒穩定</h3><p>這批名單在 15 分鐘級別【沒有】出現極端恐慌拋售。<br>"
                "建議耐心等待，或觀察盤後是否出現其他波段機會。</p>")
        as_html = True
    else:
        body = (f"<h2>⚠️ 發現 {len(target_df)} 檔盤中急跌錯殺標的：</h2>"
                f"<p>以下為 <b>15分鐘級別 (15m)</b> 的技術指標。盤中急跌會導致隱含波動率(IV)瞬間飆升，"
                f"此時賣出 Put (Sell Put) 收租性價比極高，也可密切觀察 15 分鐘線是否出現『長下影線破底翻』來搶短線多單。</p>"
                f"{target_df.to_html(index=False)}")
        as_html = True

    to_email = os.environ.get("MAIL_TO")
    from_email = os.environ.get("MAIL_USER")
    app_password = os.environ.get("MAIL_PASS")

    if to_email and from_email and app_password:
        send_scan_report_mail(subject, body, to_email, from_email, app_password, as_html=as_html)
        print("📧 盤中警報已發送!")
    else:
        print("\n=== ⚡ 本地終端機預覽 ===")
        if target_df.empty:
            print("目前盤中沒有極端超賣標的。")
        else:
            print(target_df.to_markdown(index=False))
        print("\n❌ 未設定 MAIL 環境變數，無法寄信。")
