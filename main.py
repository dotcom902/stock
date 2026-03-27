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
    請以 JSON 格式輸出：{{"sector_names": "名稱", "tickers": ["代碼1"]}}
    """
    for attempt in range(max_retries):
        try:
            print(f"🧠 正在請 AI ({AI_MODEL_NAME}) 偵測今日熱門板塊...")
            response = ai_client.models.generate_content(model=AI_MODEL_NAME, contents=prompt)
            data = json.loads(response.text.replace('```json', '').replace('```', '').strip())
            return [t.strip().upper() for t in data.get("tickers", fallback_tickers)], data.get("sector_names", fallback_desc)
        except Exception as e:
            if "429" in str(e): time.sleep(15)
            else: return fallback_tickers, fallback_desc
    return fallback_tickers, fallback_desc

# ==========================================
# 🤖 AI 自動化審查代理
# ==========================================
def analyze_stock_with_ai(ticker, signal_type, rvol, news_list_raw, max_retries=2):
    global DAILY_QUOTA_EXHAUSTED
    if DAILY_QUOTA_EXHAUSTED or not ai_client: return "⚠️ AI 暫停分析"
    
    news_text = "\n".join([f"- {item.get('title', '')}" for item in news_list_raw[:5]])
    prompt = f"你是華爾街交易員。{ticker} 觸發 {signal_type}，RVOL {rvol}。新聞：{news_text}\n量化法則：1.超跌/強勢回檔：大盤錯殺則【可以建倉】。2.動能突破：實質利多則【可以建倉】。3.一票否決：結構破壞則【高風險避開】。\n格式：50字內理由。結論：【可以建倉】或【高風險避開】。"
    
    for attempt in range(max_retries):
        try:
            response = ai_client.models.generate_content(model=AI_MODEL_NAME, contents=prompt)
            time.sleep(4) 
            return response.text.replace('\n', '<br>')
        except Exception as e:
            if "429" in str(e): time.sleep(15)
            else: return "AI 伺服器忙線"
    return "⚠️ 系統節流跳過"

# ==========================================
# 🏆 新聞與 Nasdaq 100 抓取
# ==========================================
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

# ==========================================
# 基本面與策略邏輯
# ==========================================
def get_fundamental_sentiment_score(ticker_obj, ticker_symbol):
    try:
        info = ticker_obj.info
        score = 50
        sector = info.get('sector', '未知')
        earn_ts = info.get('earningsTimestamp')
        d_earn = "近期發布"
        if earn_ts:
            days = (datetime.fromtimestamp(earn_ts).date() - datetime.today().date()).days
            d_earn = f"⚠️ {days}天後" if 0 <= days <= 5 else f"{days}天後"
        
        raw_news = get_robust_news(ticker_obj, ticker_symbol)
        l_news = "<br>".join([f"▪️ {n['title']}" for n in raw_news[:2]]) if raw_news else "無新聞"
        return 75, l_news, sector, d_earn, raw_news
    except: return 50, "數據異常", "未知", "未知", []

def get_investment_strategy(ticker_obj, current_price, score, signal_type, days_to_earnings):
    try:
        exp_dates = ticker_obj.options
        target_date = [d for d in exp_dates if 25 <= (datetime.strptime(d, '%Y-%m-%d') - datetime.today()).days <= 45][0]
        chain = ticker_obj.option_chain(target_date)
        strike = chain.puts[chain.puts['strike'] <= current_price * 0.9].sort_values(by='strike', ascending=False).iloc[0]['strike']
        return {'綜合建議': '🟢 積極 Sell Put', '期權履約價': f"Put ${strike} ({target_date})", '年化報酬': '15%+'}
    except: return {'綜合建議': '⚪ 觀望', '期權履約價': '-', '年化報酬': '-'}

# ==========================================
# 🎯 狙擊手核心掃描與 SOP
# ==========================================
def scan_market_opportunities(tickers_list, portfolio_list, hot_sectors_list):
    all_tickers = list(set(tickers_list + portfolio_list + hot_sectors_list))
    df_data = yf.download(all_tickers, period="3mo", progress=False) 
    df_closes, df_volumes = df_data['Close'], df_data['Volume']
    
    raw_candidates = []
    for ticker in df_closes.columns:
        try:
            prices = df_closes[ticker].dropna()
            if len(prices) < 20: continue
            rsi = 50 
            latest_price = prices.iloc[-1]
            rvol = 1.2
            
            is_portfolio, is_hot = ticker in portfolio_list, ticker in hot_sectors_list
            signal_type = "強勢回檔" if is_hot else "超跌反彈" 
            
            stock_obj = yf.Ticker(ticker)
            f_score, l_news, sector, d_earn, raw_news = get_fundamental_sentiment_score(stock_obj, ticker)
            strat = get_investment_strategy(stock_obj, latest_price, f_score, signal_type, d_earn)
            
            identity = '💼 我的持倉' if is_portfolio else ('🔥 動態熱門板塊' if is_hot else '🔍 掃描發現')
            sort_weight = (1000 if is_portfolio else 0) + f_score + (rvol * 10)
            
            raw_candidates.append({
                'sort_weight': sort_weight, 'raw_news': raw_news,
                'data': {
                    '身份': identity, '代碼': ticker, '型態': signal_type, '現價': round(latest_price, 2),
                    'RSI': rsi, '熱度(RVOL)': f"{rvol}x", '財報日': d_earn, '評分': f_score,
                    '綜合建議': strat['綜合建議'], '期權履約價': strat['期權履約價'], '年化報酬': strat['年化報酬'], '最新新聞': l_news
                }
            })
        except: continue

    raw_candidates.sort(key=lambda x: x['sort_weight'], reverse=True)
    top_5 = raw_candidates[:5]
    
    results = []
    for item in raw_candidates:
        stock = item['data']
        if item in top_5:
            stock['🤖 AI 投研觀點'] = analyze_stock_with_ai(stock['代碼'], stock['型態'], stock['熱度(RVOL)'], item['raw_news'])
        else:
            stock['🤖 AI 投研觀點'] = "⏸️ 系統已記錄"
        
        sop = "⚪ 觀望"
        if "高風險" in stock['🤖 AI 投研觀點']: sop = "🛑 AI 否決：直接放棄。"
        elif "強勢回檔" in stock['型態']: sop = "🥇 情境A：建倉 Bull Put Spread。"
        elif "動能突破" in stock['型態']: sop = "🥈 情境B：買入正股並設停損。"
        stock['🎯 SOP 操作提示'] = sop
        results.append(stock)
    return pd.DataFrame(results)

# ==========================================
# 📧 寄送模組 (支援多人)
# ==========================================
def send_scan_report_mail(subject, body, to_emails_str, from_email, app_password):
    # 將逗號分隔的字串拆解成名單
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
            # 使用 recipient_list 一次寄送給所有人
            server.sendmail(from_email, recipient_list, msg.as_string())
        print(f"📧 報告已成功寄送至 {len(recipient_list)} 位收件人!")
    except Exception as e: 
        print(f"❌ 寄信失敗: {e}")

if __name__ == "__main__":
    ndx, msg = get_nasdaq_100_tickers()
    hot_tickers, hot_desc = get_ai_dynamic_sectors()
    target_df = scan_market_opportunities(ndx, MY_PORTFOLIO, hot_tickers)
    
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
        
        status_html = f"<div class='status-box'>{msg}<br>💼 持倉監控：{len(MY_PORTFOLIO)} 檔 <br>🔥 AI 動態板塊鎖定：<b>{hot_desc}</b></div>"
        
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
        
        body = f"{html_style}{status_html}<h2>🎯 發現 {len(target_df)} 檔異動標的：</h2>{html_table}{sop_reminder_html}"
        
        # ✨ 資安升級：完全移除明文信箱，100% 依賴 GitHub Secrets
        to_email = os.environ.get("MAIL_TO")
        from_email = os.environ.get("MAIL_USER")
        app_pass = os.environ.get("MAIL_PASS")
        
        if to_email and from_email and app_pass:
            send_scan_report_mail(f"🧠 量化早報 ({datetime.today().strftime('%Y-%m-%d')})", body, to_email, from_email, app_pass)
        else:
            print("❌ 找不到寄件人信箱、密碼或收件人(MAIL_TO)設定，請檢查 GitHub Secrets！")
