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

def scan_oversold_opportunities(tickers):
    results = []
    end_date = datetime.today()
    start_date = end_date - timedelta(days=90)

    print(f"啟動雷達：正在掃描 {len(tickers)} 檔潛力股...")
    for ticker in tickers:
        try:
            stock = yf.Ticker(ticker)
            df = stock.history(start=start_date, end=end_date)
            if df.empty or len(df) < 20:
                continue
            close_prices = df['Close']

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
            if latest['RSI_14'] < 35 and latest['Bias_20'] < -8:
                results.append({
                    '代碼 (Ticker)': ticker,
                    '日期': latest.name.strftime('%Y-%m-%d'),
                    '最新收盤價': round(latest['Close'], 2),
                    'RSI (14)': round(latest['RSI_14'], 2),
                    '20日乖離率 (%)': f"{round(latest['Bias_20'], 2)}%",
                    '狀態': '🔥 極度超賣，關注破底翻訊號'
                })
        except Exception:
            pass

    return pd.DataFrame(results)

def send_scan_report_mail(subject, body, to_email, from_email, app_password, as_html=False):
    msg = MIMEMultipart()
    msg['From'] = from_email
    msg['To'] = to_email
    msg['Subject'] = subject
    subtype = 'html' if as_html else 'plain'
    msg.attach(MIMEText(body, subtype))

    smtp_server = 'smtp.gmail.com'
    smtp_port = 587

    with smtplib.SMTP(smtp_server, smtp_port) as server:
        server.starttls()
        server.login(from_email, app_password)
        server.sendmail(from_email, to_email, msg.as_string())

if __name__ == "__main__":
    target_df = scan_oversold_opportunities(scan_universe)
    subject = "📊 今日極度超賣掃描報告"
    if target_df.empty:
        body = ("目前市場情緒穩定，這批名單中【沒有】標的符合極度超賣條件。\n"
                "建議保持耐心，或切換至『波幅突破 (VCP)』策略尋找機會。")
        as_html = False
    else:
        body = (f"🎯 發現 {len(target_df)} 檔潛在的錯殺反彈標的：<br><br>"
                f"{target_df.to_html(index=False)}")
        as_html = True

    # 下方四個變數建議放於GitHub Secrets/Actions ENV，勿直接寫死
    to_email = os.environ.get("MAIL_TO")        # 寄給誰
    from_email = os.environ.get("MAIL_USER")    # 寄出信箱 (Gmail)
    app_password = os.environ.get("MAIL_PASS")  # Gmail application password

    if to_email and from_email and app_password:
        send_scan_report_mail(subject, body, to_email, from_email, app_password, as_html=as_html)
        print("📧 報告已寄出!")
    else:
        print("❌ 未設定郵件環境參數，無法自動寄發。請設 MAIL_TO, MAIL_USER, MAIL_PASS 環境變數。")