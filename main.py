import base64
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import json
import os
import re
import smtplib
import time
import urllib.parse
import warnings
import xml.etree.ElementTree as ET

from curl_cffi import requests as cffi_requests
from google import genai
import numpy as np
import pandas as pd
import requests
import yfinance as yf

warnings.filterwarnings("ignore")

try:
    yf.set_tz_cache_location("/tmp/yf_tz_cache")
except Exception:
    pass

# ==========================================
# 🔑 API 金鑰與環境變數設定
# ==========================================
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
FRED_API_KEY = os.environ.get("FRED_API_KEY")
FMP_API_KEY = os.environ.get("FMP_API_KEY")
AI_MODEL_NAME = "gemini-2.5-flash"

ai_client = None
if GEMINI_API_KEY:
    try:
        ai_client = genai.Client(api_key=GEMINI_API_KEY)
    except Exception as e:
        print(f"⚠️ Gemini Client 初始化失敗: {e}")

DAILY_QUOTA_EXHAUSTED = False
DATA_SOURCE_NOTES = []

# ==========================================
# 📡 OpenBB 資料層初始化（具備全自動相容與金鑰綁定）
# ==========================================
OBB_AVAILABLE = False
obb = None

try:
    from openbb import obb as _obb

    obb = _obb
    OBB_AVAILABLE = True
    if FRED_API_KEY:
        try:
            obb.user.credentials.fred_api_key = FRED_API_KEY
        except Exception:
            pass
    if FMP_API_KEY:
        try:
            obb.user.credentials.fmp_api_key = FMP_API_KEY
        except Exception:
            pass
except Exception as e:
    print(f"⚠️ OpenBB 核心未載入，改用原生 yfinance / API: {e}")


def _note(msg):
    if msg not in DATA_SOURCE_NOTES:
        DATA_SOURCE_NOTES.append(msg)
        print(msg)


def get_safe_url(b64_str):
    return base64.b64decode(b64_str).decode("utf-8")


# 核心自選與監控清單
MY_PORTFOLIO = ["NVDA", "TSM", "CRWV", "PLTR", "MSTR", "MU", "INTC"]
CORE_WATCHLIST = [
    "COHR", "LITE", "FN", "NTAP",
    "AMD", "ARM", "MU", "SNDK", "SMCI",
    "ASTS", "RKLB", "LUNR", "BKSY",
]


# ==========================================
# 📈 數據標準化與報價獲取模組
# ==========================================
def _period_to_start(period):
    days = {"5d": 8, "1mo": 32, "3mo": 95, "6mo": 190, "1y": 370}.get(period, 95)
    return (datetime.today() - timedelta(days=days)).date().isoformat()


def _normalize_ohlcv(df):
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    out.columns = [str(c).strip().lower() for c in out.columns]
    rename = {
        "open": "Open",
        "high": "High",
        "low": "Low",
        "close": "Close",
        "adj_close": "Adj Close",
        "adjclose": "Adj Close",
        "volume": "Volume",
        "symbol": "symbol",
        "date": "date",
    }
    out = out.rename(columns={c: rename.get(c, c) for c in out.columns})
    if "date" in out.columns:
        out["date"] = pd.to_datetime(out["date"])
        out = out.set_index("date")
    out.index = pd.to_datetime(out.index)
    return out.sort_index()


def _obb_historical_batch(symbols, period="3mo"):
    """使用 OpenBB 逐檔安全抓取，避免批次逗號拼接或單一標的報錯引發整體失敗"""
    start_date = _period_to_start(period)
    frames = []

    for sym in symbols:
        fetched = False
        # 依序測試 yfinance provider 與預設 provider
        for kwargs in [
            {"symbol": sym, "start_date": start_date, "provider": "yfinance"},
            {"symbol": sym, "start_date": start_date},
        ]:
            try:
                res = obb.equity.price.historical(**kwargs)
                raw = _normalize_ohlcv(res.to_dataframe())
                if not raw.empty and "Close" in raw.columns:
                    raw["symbol"] = sym
                    frames.append(raw)
                    fetched = True
                    break
            except Exception:
                continue
        if not fetched:
            pass

    if not frames:
        raise RuntimeError("OpenBB historical returned no valid frames")

    long_df = pd.concat(frames, axis=0)
    close_wide = long_df.pivot_table(index=long_df.index, columns="symbol", values="Close", aggfunc="last")
    vol_wide = long_df.pivot_table(index=long_df.index, columns="symbol", values="Volume", aggfunc="last")
    open_wide = long_df.pivot_table(index=long_df.index, columns="symbol", values="Open", aggfunc="last") if "Open" in long_df.columns else close_wide
    high_wide = long_df.pivot_table(index=long_df.index, columns="symbol", values="High", aggfunc="last") if "High" in long_df.columns else close_wide
    low_wide = long_df.pivot_table(index=long_df.index, columns="symbol", values="Low", aggfunc="last") if "Low" in long_df.columns else close_wide

    wide = pd.concat(
        {
            "Open": open_wide,
            "High": high_wide,
            "Low": low_wide,
            "Close": close_wide,
            "Volume": vol_wide,
        },
        axis=1,
    )
    return wide.sort_index()


def _yf_download(symbols, period="3mo"):
    try:
        data = yf.download(symbols, period=period, progress=False, threads=True, auto_adjust=False)
        return data
    except Exception as e:
        print(f"⚠️ yfinance 批次下載失敗: {e}")
        return pd.DataFrame()


def download_prices(symbols, period="3mo"):
    """優先嘗試 OpenBB，失敗或大批量時無縫切換 yfinance，返回相容 MultiIndex DataFrame"""
    symbols = list(dict.fromkeys([s.strip().upper() for s in symbols if s]))
    if not symbols:
        return pd.DataFrame()

    use_obb = OBB_AVAILABLE and len(symbols) <= 12
    if use_obb:
        try:
            data = _obb_historical_batch(symbols, period)
            if data is not None and not data.empty:
                _note("📡 價格來源: OpenBB")
                return data
        except Exception as e:
            _note(f"⚠️ OpenBB 價格失敗，改用 yfinance: {e}")

    data = _yf_download(symbols, period)
    _note("📡 價格來源: yfinance")
    return data


def _rsi(prices, n=14):
    prices = pd.Series(prices).dropna()
    if len(prices) < n:
        return pd.Series([50.0] * len(prices), index=prices.index)
    delta = prices.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = -delta.clip(upper=0).ewm(alpha=1 / n, adjust=False).mean()
    rsi = (100 - (100 / (1 + gain / loss))).replace([np.inf, -np.inf], np.nan).fillna(50)
    return rsi


# ==========================================
# 🚦 總經宏觀紅綠燈 (FRED + SMH/VIX/QQQ)
# ==========================================
def fetch_fred_data(series_id, limit=20):
    if OBB_AVAILABLE:
        for kwargs in (
            {"symbol": series_id, "provider": "fred"},
            {"symbol": series_id, "provider": "federal_reserve"},
            {"symbol": series_id},
        ):
            try:
                result = obb.economy.fred_series(**kwargs)
                df = result.to_dataframe()
                if df is None or df.empty:
                    continue
                val_col = None
                for c in df.columns:
                    if str(c).lower() in ("value", "close", series_id.lower()):
                        val_col = c
                        break
                if val_col is None:
                    num_cols = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
                    val_col = num_cols[-1] if num_cols else None
                if val_col is None:
                    continue
                vals = pd.to_numeric(df[val_col], errors="coerce").dropna().astype(float).tolist()
                if vals:
                    _note("🌍 FRED 來源: OpenBB")
                    return list(reversed(vals))[:limit]
            except Exception:
                continue

    if not FRED_API_KEY:
        return []
    try:
        url = "https://api.stlouisfed.org/fred/series/observations"
        params = {
            "series_id": series_id,
            "api_key": FRED_API_KEY,
            "file_type": "json",
            "sort_order": "desc",
            "limit": limit,
        }
        res = requests.get(url, params=params, timeout=10)
        if res.status_code == 200:
            data = res.json()
            _note("🌍 FRED 來源: FRED API")
            return [float(x["value"]) for x in data.get("observations", []) if x.get("value") != "."]
    except Exception as e:
        print(f"⚠️ FRED API 呼叫失敗 ({series_id}): {e}")
    return []


def get_macro_regime():
    try:
        print("🌍 正在掃描大盤動能 (QQQ/SMH/VIX) 與 總經利率/信用利差...")
        df_data = download_prices(["QQQ", "SMH", "^VIX"], period="3mo")
        df_closes = df_data["Close"] if "Close" in df_data else df_data

        qqq_prices = df_closes["QQQ"].dropna() if "QQQ" in df_closes else pd.Series(dtype=float)
        latest_qqq_rsi = float(_rsi(qqq_prices).iloc[-1]) if len(qqq_prices) else 50.0

        smh_prices = df_closes["SMH"].dropna() if "SMH" in df_closes else pd.Series(dtype=float)
        latest_smh_rsi = float(_rsi(smh_prices).iloc[-1]) if len(smh_prices) else 50.0

        vix_col = "^VIX" if "^VIX" in df_closes else ("VIX" if "VIX" in df_closes else None)
        if vix_col:
            vix_prices = df_closes[vix_col].ffill().dropna()
            latest_vix = float(vix_prices.iloc[-1]) if len(vix_prices) > 0 else 20.0
        else:
            latest_vix = 20.0

        fred_str = "▪️ 總經流動性: <i>未設定 FRED API 金鑰，略過利差監控</i>"
        fred_red_flag = False
        fred_yellow_flag = False

        yields = fetch_fred_data("DGS10")
        spreads = fetch_fred_data("BAMLH0A0HYM2")
        if yields and spreads and len(yields) >= 10 and len(spreads) >= 10:
            cur_yield, ma10_yield = yields[0], sum(yields[:10]) / 10
            cur_spread, ma10_spread = spreads[0], sum(spreads[:10]) / 10
            fred_str = f"▪️ 10年美債殖利率: <b>{cur_yield:.2f}%</b> (10日均: {ma10_yield:.2f}%) | 高收益信用利差: <b>{cur_spread:.2f}%</b>"
            if cur_spread > ma10_spread * 1.15 or cur_yield > ma10_yield * 1.08:
                fred_red_flag = True
            elif cur_spread > ma10_spread * 1.05 or cur_yield > ma10_yield * 1.03:
                fred_yellow_flag = True
        elif not FRED_API_KEY and not yields:
            fred_str = "▪️ 總經流動性: <i>未設定 FRED API 金鑰，略過利差監控</i>"

        signal = "GREEN"
        if latest_vix > 25 or latest_qqq_rsi < 30 or fred_red_flag:
            status_icon, status_text, signal = "🔴", "紅燈 (Risk-Off / 總經風險擴散與流動性緊縮)", "RED"
        elif fred_yellow_flag or latest_qqq_rsi > 75:
            status_icon, status_text, signal = "🟡", "黃燈 (高檔警報 / 資金成本上揚或短線過熱)", "YELLOW"
        elif latest_smh_rsi < 40:
            status_icon, status_text, signal = "🟠", "橘燈 (AI/半導體族群動能疲弱)", "ORANGE"
        else:
            status_icon, status_text, signal = "🟢", "綠燈 (Risk-On / 流動性與多頭動能健康)", "GREEN"

        desc = f"""
        <span style='font-size:16px;'>{status_icon}</span> <b>{status_text}</b> <br>
        ▪️ VIX 恐慌指數: <b>{latest_vix:.2f}</b> | QQQ 日線 RSI: <b>{latest_qqq_rsi:.1f}</b> | SMH 日線 RSI: <b>{latest_smh_rsi:.1f}</b><br>
        {fred_str}
        """
        return desc, signal
    except Exception as e:
        print(f"⚠️ 宏觀指標掃描失敗: {e}")
        return "大盤狀態: 未知", "UNKNOWN"


# ==========================================
# 📊 美股收盤總結與資金流向速報
# ==========================================
def get_market_closing_summary(hot_sectors_desc):
    try:
        indices = {"S&P 500": "^GSPC", "Nasdaq 100": "^NDX", "Dow Jones": "^DJI", "Russell 2000": "IWM"}
        df_indices = download_prices(list(indices.values()), period="5d")
        close_block = df_indices["Close"] if "Close" in df_indices else df_indices

        index_str = ""
        for name, ticker in indices.items():
            col = ticker if ticker in close_block else ticker.replace("^", "")
            if col not in close_block:
                continue
            closes = close_block[col].dropna() if isinstance(close_block, pd.DataFrame) else close_block.dropna()
            if len(closes) >= 2:
                today_c, yest_c = float(closes.iloc[-1]), float(closes.iloc[-2])
                pct_change = ((today_c - yest_c) / yest_c) * 100
                icon = "🟢" if pct_change > 0 else "🔴"
                index_str += f"{name}: {today_c:.2f} ({icon} {pct_change:+.2f}%) "

        if not ai_client or DAILY_QUOTA_EXHAUSTED:
            return f"<div style='padding:8px; background:#f0f0f0;'>{index_str}</div>"

        prompt = f"""
        現在是美股收盤後。你是華爾街量化避險基金首席交易員。
        四大指數今日表現：{index_str}
        今日量化系統偵測到資金異常湧入板塊：{hot_sectors_desc}
        請寫一段約 80-120 字的收盤資金流向速報。言簡意賅，點名資金抽離與聚集處。純文本，重點處用 <b> 加粗，不要使用 Markdown 程式碼區塊。
        """
        response = ai_client.models.generate_content(model=AI_MODEL_NAME, contents=prompt)
        summary_text = response.text.replace("\n", "<br>")

        return f"""
        <div style="background-color: #f4f8fb; border-left: 4px solid #2980b9; padding: 12px; margin-bottom: 15px; border-radius: 4px;">
            <div style="font-family: monospace; font-size: 13px; margin-bottom: 8px; font-weight: bold;">{index_str}</div>
            <div style="font-size: 13px; color: #34495e; line-height: 1.5;">{summary_text}</div>
        </div>
        """
    except Exception:
        return ""


# ==========================================
# 🧠 AI 動態板塊與投研觀點
# ==========================================
def get_ai_dynamic_sectors(max_retries=3):
    global DAILY_QUOTA_EXHAUSTED
    fallback_tickers = ["COHR", "LITE", "FN", "MU", "SNDK", "RKLB", "CRWV"]
    fallback_desc = "AI 算力與高速光互連、記憶體架構、航太防務"
    if not ai_client or DAILY_QUOTA_EXHAUSTED:
        return fallback_tickers, fallback_desc

    prompt = """
    你是美股板塊輪動量化分析師。請選出「本週資金流入最顯著、最受關注的 3 個美股細分題材」(例如: 矽光子/CPO、AI液冷、資料中心記憶體)。
    為這 3 個板塊各挑選 2~3 檔市值 > 20億美元的美股代表代碼。
    請嚴格只輸出 JSON:
    {"sector_names": "板塊A, 板塊B, 板塊C", "tickers": ["TICKER1", "TICKER2", "TICKER3"]}
    """
    for _ in range(max_retries):
        try:
            response = ai_client.models.generate_content(model=AI_MODEL_NAME, contents=prompt)
            json_match = re.search(r"\{.*\}", response.text, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group(0))
                valid_tickers = [
                    t.strip().upper()
                    for t in data.get("tickers", fallback_tickers)
                    if t.strip().isalpha() and len(t.strip()) <= 5
                ]
                if valid_tickers:
                    return valid_tickers, data.get("sector_names", fallback_desc)
        except Exception:
            time.sleep(2)
    return fallback_tickers, fallback_desc


def analyze_stock_with_ai(ticker, sector_name, signal_type, rvol_val, news_list_raw, max_retries=3):
    global DAILY_QUOTA_EXHAUSTED
    if DAILY_QUOTA_EXHAUSTED or not ai_client:
        return "⏸️ AI 暫停分析"

    news_text = "\n".join([f"- {item.get('title', '')}" for item in news_list_raw[:3]])
    prompt = f"""
    標的：{ticker}（產業：{sector_name}）
    技術狀態：觸發【{signal_type}】，相對量能 RVOL：{rvol_val}。
    最新新聞摘要：
    {news_text}

    請以華爾街交易員視角，用 50 字以內說明核心驅動力或潛在風險。
    文末必須以固定標籤總結，格式嚴格為：【建議：可以建倉】、【建議：觀望收租】、【建議：鎖定利潤】或【建議：高風險避開】四選一。
    """
    for _ in range(max_retries):
        try:
            response = ai_client.models.generate_content(model=AI_MODEL_NAME, contents=prompt)
            time.sleep(1.2)
            return response.text.strip().replace("\n", "<br>")
        except Exception as e:
            if "429" in str(e).lower() or "503" in str(e).lower():
                time.sleep(3)
            else:
                break
    return "⏸️ 伺服器繁忙"


def get_robust_news(ticker_symbol):
    news_items = []
    if OBB_AVAILABLE:
        for call in (
            lambda: obb.news.company(symbol=ticker_symbol, limit=3, provider="yfinance"),
            lambda: obb.news.company(symbol=ticker_symbol, limit=3),
        ):
            try:
                result = call()
                df = result.to_dataframe()
                if df is None or df.empty:
                    continue
                title_col = None
                for c in df.columns:
                    if str(c).lower() in ("title", "headline", "text"):
                        title_col = c
                        break
                if title_col is None:
                    continue
                for title in df[title_col].dropna().astype(str).tolist()[:3]:
                    news_items.append({"title": title.split(" - ")[0]})
                if news_items:
                    _note("📰 新聞來源: OpenBB")
                    return news_items
            except Exception:
                continue

    try:
        base_url = get_safe_url("aHR0cHM6Ly9uZXdzLmdvb2dsZS5jb20vcnNzL3NlYXJjaD9xPQ==")
        query = urllib.parse.quote(f"{ticker_symbol} stock")
        url = f"{base_url}{query}&hl=en-US&gl=US&ceid=US:en"
        response = cffi_requests.get(url, impersonate="chrome110", timeout=8)
        if response.status_code == 200:
            root = ET.fromstring(response.text)
            for item in root.findall(".//channel/item")[:3]:
                title_elem = item.find("title")
                if title_elem is not None and title_elem.text:
                    news_items.append({"title": title_elem.text.split(" - ")[0]})
            if news_items:
                _note("📰 新聞來源: Google News")
    except Exception:
        pass
    return news_items


def get_stock_info(ticker):
    info = {}
    if OBB_AVAILABLE:
        try:
            profile = obb.equity.profile(symbol=ticker, provider="yfinance").to_dataframe()
            if profile is not None and not profile.empty:
                row = profile.iloc[0].to_dict()
                lower = {str(k).lower(): v for k, v in row.items()}
                info["industry"] = lower.get("industry") or lower.get("industry_category") or lower.get("sector")
                info["currentPrice"] = lower.get("last_price") or lower.get("price") or lower.get("currentprice")
                info["targetMeanPrice"] = lower.get("target_price") or lower.get("targetmeanprice")
                info["recommendationKey"] = str(lower.get("recommendation") or lower.get("recommendationkey") or "").lower()
                info["earningsTimestamp"] = lower.get("earnings_timestamp") or lower.get("earningstimestamp")
        except Exception:
            pass

    if not info.get("industry"):
        try:
            yinfo = yf.Ticker(ticker).info or {}
            for k, v in yinfo.items():
                if k not in info or info.get(k) in (None, "", 0):
                    info[k] = v
        except Exception:
            pass
    return info


def get_fundamental_sentiment_score(ticker_symbol):
    score, sector, days_to_earnings, news_list_raw, upside_str = 50, "科技/電子", "近期已發布", [], "-"
    try:
        info = get_stock_info(ticker_symbol)
        if info:
            sector = info.get("industry") or info.get("sector") or "科技/電子"
            earn_ts = info.get("earningsTimestamp")
            if earn_ts:
                try:
                    ts = float(earn_ts)
                    days = (datetime.fromtimestamp(ts).date() - datetime.today().date()).days
                    days_to_earnings = f"⚠️ {days}天後" if 0 <= days <= 25 else (f"{days}天後" if days > 25 else "近期已公布")
                except Exception:
                    pass

            c_price = float(info.get("currentPrice", 0) or 0)
            t_price = float(info.get("targetMeanPrice", 0) or 0)
            if c_price > 0 and t_price > 0:
                upside = (t_price - c_price) / c_price
                upside_str = f"+{upside * 100:.1f}%" if upside > 0 else f"{upside * 100:.1f}%"
                if upside > 0.15:
                    score += 15
                elif upside < 0:
                    score -= 10

            rec = str(info.get("recommendationKey", "") or "").lower()
            if rec in ["buy", "strong_buy", "strongbuy"]:
                score += 10
            elif rec in ["sell", "underperform"]:
                score -= 15

        news_list_raw = get_robust_news(ticker_symbol)
        bull_k = ["upgrade", "beat", "growth", "surge", "buy", "record", "contract"]
        bear_k = ["downgrade", "miss", "cut", "drop", "lawsuit", "sell", "fall", "tariff"]
        for article in news_list_raw:
            title = article.get("title", "").lower()
            if any(k in title for k in bull_k):
                score += 4
            if any(k in title for k in bear_k):
                score -= 5

        return max(20, min(95, score)), sector, days_to_earnings, news_list_raw, upside_str
    except Exception:
        return 50, "未知", "近期已公布", [], "-"


# ==========================================
# 🎯 選擇權、真實 IV 計算與歷史波動率備援
# ==========================================
def get_investment_strategy(ticker, current_price, prices_series=None):
    try:
        puts = None
        target_date = None

        # 1. 優先嘗試 OpenBB 期權鏈
        if OBB_AVAILABLE:
            try:
                result = obb.derivatives.options.chains(symbol=ticker, provider="yfinance")
                df = result.to_dataframe()
                if df is not None and not df.empty:
                    cols = {str(c).lower(): c for c in df.columns}
                    strike_c = cols.get("strike")
                    bid_c = cols.get("bid")
                    iv_c = cols.get("implied_volatility") or cols.get("impliedvolatility") or cols.get("iv")
                    type_c = cols.get("option_type") or cols.get("type") or cols.get("optiontype")
                    exp_c = cols.get("expiration") or cols.get("expiry") or cols.get("expiration_date")

                    if all([strike_c, bid_c, iv_c, type_c, exp_c]):
                        work = df.copy()
                        work["_type"] = work[type_c].astype(str).str.lower()
                        work_puts = work[work["_type"].str.contains("put")].copy()
                        if not work_puts.empty:
                            work_puts["_exp"] = pd.to_datetime(work_puts[exp_c], errors="coerce")
                            work_puts["_strike"] = pd.to_numeric(work_puts[strike_c], errors="coerce")
                            work_puts["_bid"] = pd.to_numeric(work_puts[bid_c], errors="coerce")
                            work_puts["_iv"] = pd.to_numeric(work_puts[iv_c], errors="coerce")
                            work_puts = work_puts.dropna(subset=["_exp", "_strike"])
                            today = pd.Timestamp(datetime.today().date())
                            work_puts["_dte"] = (work_puts["_exp"] - today).dt.days

                            window = work_puts[(work_puts["_dte"] >= 20) & (work_puts["_dte"] <= 45)]
                            if window.empty:
                                window = work_puts
                            target_exp = window.sort_values("_dte")["_exp"].iloc[0]
                            chain = work_puts[work_puts["_exp"] == target_exp]
                            puts = pd.DataFrame({
                                "strike": chain["_strike"],
                                "bid": chain["_bid"],
                                "impliedVolatility": chain["_iv"],
                            })
                            target_date = target_exp.strftime("%Y-%m-%d")
                            _note("🎯 選擇權來源: OpenBB")
            except Exception:
                pass

        # 2. 回退 yfinance 期權鏈
        if puts is None or puts.empty:
            ticker_obj = yf.Ticker(ticker)
            exp_dates = ticker_obj.options
            if exp_dates:
                valid_dates = [
                    d for d in exp_dates
                    if 20 <= (datetime.strptime(d, "%Y-%m-%d") - datetime.today()).days <= 45
                ]
                target_date = valid_dates[0] if valid_dates else exp_dates[0]
                chain = ticker_obj.option_chain(target_date)
                puts = chain.puts
                _note("🎯 選擇權來源: yfinance")

        real_iv = None
        if puts is not None and not puts.empty:
            puts = puts.copy()
            puts["atm_diff"] = (puts["strike"] - current_price).abs()
            atm_puts = puts[puts["bid"] > 0].sort_values(by="atm_diff")
            if not atm_puts.empty and atm_puts.iloc[0]["impliedVolatility"] > 0.05:
                real_iv = float(atm_puts.iloc[0]["impliedVolatility"])

        # 3. 備援方案：若期權鏈缺失，計算 30 日歷史波動率 (HV30)
        if real_iv is None and prices_series is not None and len(prices_series) >= 20:
            returns = np.log(prices_series / prices_series.shift(1)).dropna()
            real_iv = float(returns.std() * np.sqrt(252))

        strike = round(current_price * 0.90, 1)
        if puts is not None and not puts.empty:
            target_puts = puts[puts["strike"] <= current_price * 0.92].sort_values(by="strike", ascending=False)
            if not target_puts.empty:
                strike = float(target_puts.iloc[0]["strike"])

        if real_iv:
            iv_pct = real_iv * 100
            if real_iv >= 0.55:
                iv_status = f"<span style='color:#c0392b;font-weight:bold;'>{iv_pct:.1f}% (🔥高IV)</span>"
                iv_level = "HIGH"
            elif real_iv <= 0.32:
                iv_status = f"<span style='color:#2980b9;'>{iv_pct:.1f}% (🧊低IV)</span>"
                iv_level = "LOW"
            else:
                iv_status = f"{iv_pct:.1f}% (適中)"
                iv_level = "MID"
        else:
            iv_status, iv_level = "約 35% (估計)", "MID"

        date_tail = target_date[-5:] if target_date else "1M"
        return {
            "has_options": True,
            "strike_desc": f"Put ${strike:g} ({date_tail})",
            "iv_status": iv_status,
            "iv_level": iv_level,
        }
    except Exception:
        return {"has_options": False, "strike_desc": "-", "iv_status": "-", "iv_level": "UNKNOWN"}


# ==========================================
# 🚀 整合掃描核心與決策引擎
# ==========================================
def scan_market_opportunities(tickers_list, portfolio_list, hot_sectors_list, macro_signal):
    all_tickers = list(dict.fromkeys(tickers_list + portfolio_list + hot_sectors_list))
    df_data = download_prices(all_tickers, period="3mo")
    if df_data.empty:
        return pd.DataFrame()

    df_closes = df_data["Close"] if "Close" in df_data else df_data
    df_volumes = df_data["Volume"] if "Volume" in df_data else pd.DataFrame()

    raw_candidates = []
    for ticker in df_closes.columns:
        try:
            prices = df_closes[ticker].dropna()
            if len(prices) < 20:
                continue

            rsi_series = _rsi(prices)
            latest_rsi = float(rsi_series.iloc[-1])
            latest_price = float(prices.iloc[-1])
            ma20 = float(prices.rolling(20).mean().iloc[-1])
            ma5 = float(prices.rolling(5).mean().iloc[-1])
            bias_20 = ((latest_price - ma20) / ma20) * 100

            rvol = 1.0
            if not df_volumes.empty and ticker in df_volumes.columns:
                vols = df_volumes[ticker].dropna()
                if len(vols) >= 20:
                    v_ma20 = float(vols.rolling(20).mean().iloc[-1])
                    if v_ma20 > 0:
                        rvol = round(float(vols.iloc[-1]) / v_ma20, 2)

            is_portfolio = ticker in portfolio_list
            is_hot = ticker in hot_sectors_list
            signal_type = None

            if is_hot:
                if latest_rsi >= 75:
                    signal_type = "高檔鈍化(軋空)" if latest_price > ma5 else "極端超買(破線)"
                elif latest_rsi > 62 and bias_20 > 4 and rvol > 1.2:
                    signal_type = "動能突破"
                elif 50 <= latest_rsi <= 62 and abs(bias_20) <= 5:
                    signal_type = "高檔震盪"
                elif 40 <= latest_rsi < 52 and bias_20 > -4:
                    signal_type = "強勢回檔"
                elif latest_rsi < 40:
                    signal_type = "趨勢破壞"
                elif rvol > 1.5:
                    signal_type = "板塊異動(爆量)"
            else:
                if latest_rsi < 35 and bias_20 < -6:
                    signal_type = "超跌反彈"
                elif latest_rsi > 65 and bias_20 > 5 and rvol > 1.2:
                    signal_type = "動能突破"

            if is_portfolio and not signal_type:
                signal_type = "持倉監控"

            if signal_type:
                f_score, sector, d_earn, raw_news, upside_str = get_fundamental_sentiment_score(ticker)
                strat = get_investment_strategy(ticker, latest_price, prices)

                weight_multiplier = 25 if signal_type in ["強勢回檔", "動能突破", "高檔鈍化(軋空)"] else 10
                sort_weight = (1000 if is_portfolio else 0) + f_score + (rvol * weight_multiplier)

                raw_candidates.append({
                    "ticker": ticker,
                    "is_portfolio": is_portfolio,
                    "is_hot": is_hot,
                    "rvol": rvol,
                    "signal_type": signal_type,
                    "latest_price": latest_price,
                    "latest_rsi": latest_rsi,
                    "upside_str": upside_str,
                    "sector": sector,
                    "d_earn": d_earn,
                    "f_score": f_score,
                    "strat": strat,
                    "raw_news": raw_news,
                    "sort_weight": sort_weight,
                })
        except Exception:
            continue

    raw_candidates.sort(key=lambda x: x["sort_weight"], reverse=True)
    ai_targets = [c for c in raw_candidates if c["signal_type"] not in ["持倉監控", "高檔震盪", "趨勢破壞"]][:8]

    print(f"\n🎯 系統篩選出 {len(raw_candidates)} 檔標的，啟動 AI 深度投研...")

    results = []
    for item in raw_candidates:
        ticker = item["ticker"]
        sig = item["signal_type"]
        strat = item["strat"]
        iv_lvl = strat["iv_level"]

        if item in ai_targets:
            ai_view = analyze_stock_with_ai(ticker, item["sector"], sig, f"{item['rvol']}x", item["raw_news"])
        else:
            ai_view = "<i>系統日常監控中</i>"

        sop = ""
        is_vetoed = "高風險避開" in ai_view or "⚠️" in ai_view
        near_earnings = "⚠️" in item["d_earn"]

        if is_vetoed:
            sop = "<span style='color:#c0392b;font-weight:bold;'>🛑 AI 否決</span>：技術/基本面風險偏高，嚴禁建倉。"
        elif near_earnings:
            sop = "<span style='color:#e67e22;font-weight:bold;'>⚠️ 財報臨近</span>：跳空風險高，禁止任何賣方 Sell Put；正股逢高防禦鎖定利潤。"
        elif sig == "趨勢破壞":
            sop = "<span style='color:#c0392b;'>🛑 破線轉弱</span>：RSI 破位，無條件避開，持股逢反彈減碼或購 Put 避險。"
        elif sig == "高檔鈍化(軋空)":
            sop = "🔥 <b>主升段軋空</b>：嚴禁逆勢猜頂！持股以 5 日線作移動停利續抱。"
        elif sig == "強勢回檔":
            if iv_lvl == "HIGH":
                sop = "🥇 <b>情境A (高IV回檔)</b>：權利金極肥厚，首選 <b>Sell Put / Bull Put Spread</b> 收租。"
            elif iv_lvl == "LOW":
                sop = "🥇 <b>情境A (低IV回檔)</b>：選擇權偏便宜，建議買入現貨或建倉 <b>Bull Call Spread</b>。"
            else:
                sop = "🥇 <b>情境A (標準回檔)</b>：支撐處分批建倉正股，或佈局保守 <b>Bull Put Spread</b>。"
        elif sig in ["動能突破", "板塊異動(爆量)"]:
            if iv_lvl == "HIGH":
                sop = "🥈 <b>情境B (帶量突破)</b>：高 IV 嚴防波動率收縮 (IV Crush)，<b>改買現貨</b>（設 5 日線停損）。"
            elif iv_lvl == "LOW":
                sop = "🥈 <b>情境B (帶量突破)</b>：IV 偏低期權划算，適合以 <b>Long Call</b> 槓桿參與主升浪。"
            else:
                sop = "🥈 <b>情境B (動能突破)</b>：買入現貨並設定移動停損，或做多垂直價差。"
        elif sig == "高檔震盪":
            sop = "⏳ <b>橫盤收斂</b>：持股者可做 <b>Covered Call</b> 賣出價外買權收取時間價值。"
        elif sig == "超跌反彈":
            sop = "🥉 <b>情境C (價值錯殺)</b>：保守者可於強支撐下方 <b>Sell Put</b> 收租，跌破前低嚴格停損。"
        else:
            sop = "🔹 <b>核心持股</b>：維持原定部位配置，跌破月線（MA20）才需減碼。"

        if macro_signal == "RED" and any(k in sop for k in ["情境A", "情境B", "情境C"]):
            sop = "🛑 <b>[紅燈警戒]</b> 流動性緊縮與大盤恐慌，全面沒收做多信號，僅限觀望或對沖！"
        elif (macro_signal == "YELLOW" or macro_signal == "ORANGE") and "情境B" in sop:
            sop = "⚠️ <b>[黃燈提示]</b> 大盤過熱或晶片動能減弱，<b>取消突破追高</b>，改為分批獲利了結。"

        sector_short = (item["sector"][:12] + "..") if len(item["sector"]) > 12 else item["sector"]
        badge = ""
        if item["is_portfolio"]:
            badge = "<span style='background:#2980b9; color:white; padding:1px 5px; border-radius:3px; font-size:10px;'>持倉</span> "
        if item["is_hot"]:
            badge += "<span style='background:#e67e22; color:white; padding:1px 5px; border-radius:3px; font-size:10px;'>熱門</span>"

        ticker_display = f"<b>{ticker}</b><br>{badge}"
        rvol_display = f"<b style='color:#c0392b;'>{item['rvol']}x 🔥</b>" if item["rvol"] >= 1.5 else f"{item['rvol']}x"
        opt_info = f"{strat['strike_desc']}<br>{strat['iv_status']}" if strat["has_options"] else "<span style='color:#95a5a6;'>無期權</span>"

        results.append({
            "標的 / 板塊": f"{ticker_display}<br><span style='color:#7f8c8d; font-size:11px;'>{sector_short}</span>",
            "現價 (Upside)": f"<b>${item['latest_price']:.2f}</b><br><span style='color:#27ae60; font-size:11px;'>{item['upside_str']}</span>",
            "型態與量能": f"<b>{sig}</b><br><span style='font-size:11px;'>RSI: {item['latest_rsi']:.1f} | 量: {rvol_display}</span>",
            "財報倒數": item["d_earn"],
            "期權策略 & IV": opt_info,
            "評分": f"<b>{int(item['f_score'])}</b>",
            "🤖 AI 投研觀點": ai_view,
            "🎯 實戰 SOP 操作指引": sop,
        })

    return pd.DataFrame(results)


# ==========================================
# 📧 郵件發送模組
# ==========================================
def send_scan_report_mail(subject, body, to_emails_str, from_email, app_password):
    recipient_list = [e.strip() for e in to_emails_str.split(",") if e.strip()]
    msg = MIMEMultipart()
    msg["From"] = from_email
    msg["To"] = to_emails_str
    msg["Subject"] = subject
    msg.attach(MIMEText(f"<html><body>{body}</body></html>", "html"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(from_email, app_password)
            server.sendmail(from_email, recipient_list, msg.as_string())
        print(f"📧 報告已成功寄送至 {len(recipient_list)} 位收件人!")
    except Exception as e:
        print(f"❌ 寄信失敗: {e}")


# ==========================================
# 🏁 主程式進入點
# ==========================================
if __name__ == "__main__":
    print(f"🔧 OpenBB 狀態: {'啟用' if OBB_AVAILABLE else '使用備援資料源'}")
    macro_desc, macro_signal = get_macro_regime()

    # Nasdaq 代表池
    ndx_pool = [
        "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "COST", "AMD",
        "ADBE", "NFLX", "QCOM", "TXN", "INTU", "AMAT", "BKNG", "MU", "LRCX", "ADI"
    ]
    hot_tickers, hot_desc = get_ai_dynamic_sectors()
    market_summary_html = get_market_closing_summary(hot_desc)

    combined_watchlist = list(set(hot_tickers + CORE_WATCHLIST))
    target_df = scan_market_opportunities(ndx_pool, MY_PORTFOLIO, combined_watchlist, macro_signal)

    if not target_df.empty:
        html_table = target_df.to_html(index=False, escape=False)

        html_style = """
        <style>
            body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; color: #2c3e50; line-height: 1.5; }
            .status-box { background-color: #f8f9fa; border-left: 4px solid #34495e; padding: 12px 16px; margin-bottom: 16px; font-size: 13px; border-radius: 4px; }
            table { border-collapse: collapse; width: 100%; font-size: 12px; table-layout: fixed; margin-top: 10px; }
            th { background-color: #2c3e50; color: #ffffff; padding: 8px 6px; text-align: center; font-weight: 600; }
            td { border: 1px solid #e1e8ed; padding: 8px 6px; text-align: center; word-wrap: break-word; vertical-align: middle; }
            tr:nth-child(even) { background-color: #fcfcfc; }
            tr:hover { background-color: #f5f8fa; }
            th:nth-child(1) { width: 10%; }
            th:nth-child(2) { width: 9%; }
            th:nth-child(3) { width: 13%; }
            th:nth-child(4) { width: 8%; }
            th:nth-child(5) { width: 14%; }
            th:nth-child(6) { width: 6%; }
            th:nth-child(7) { width: 18%; text-align: left; }
            th:nth-child(8) { width: 22%; text-align: left; background-color: #d35400; }
            td:nth-child(7), td:nth-child(8) { text-align: left; font-size: 11px; line-height: 1.4; }
        </style>
        """

        source_line = " | ".join(DATA_SOURCE_NOTES) if DATA_SOURCE_NOTES else "資料層待命"
        status_html = f"""
        <div class='status-box'>
            <h3 style="margin-top:0; margin-bottom:8px; color:#2c3e50;">📊 狙擊手儀表板：宏觀環境紅綠燈</h3>
            {macro_desc}
            <div style="margin-top: 8px; font-size: 12px; color: #555;">
                💼 持倉監控: <b>{len(MY_PORTFOLIO)} 檔</b> | 🎯 核心雷達: <b>光通信 / 記憶體 / 太空</b> | 🔥 今日熱門題材: <b>{hot_desc}</b>
            </div>
            <div style="margin-top: 6px; font-size: 11px; color: #7f8c8d;">資料層: {source_line}</div>
        </div>
        """

        sop_legend = """
        <div style="background-color: #fbfbfc; border: 1px solid #e2e8f0; padding: 12px 16px; margin-top: 20px; border-radius: 4px; font-size: 12px; color: #475569;">
            <b style="color: #d35400;">🛡 選擇權與實戰執行鐵律：</b>
            <ul style="margin: 5px 0 0 0; padding-left: 20px;">
                <li><b>高 IV (🔥 > 50%)：</b>權利金肥厚，強烈建議做賣方（Sell Put / Covered Call）收租，嚴防買方 IV Crush。</li>
                <li><b>低 IV (🧊 < 32%)：</b>期權極度便宜，適合買入正股或買權（Long Call）槓桿參與主升浪。</li>
                <li><b>財報小於 25 天：</b>一律封鎖 Sell Put，避免隔夜財報跳空遭遇黑天鵝。</li>
            </ul>
        </div>
        """

        body = f"{html_style}{status_html}{market_summary_html}<h3 style='margin-bottom:6px;'>🎯 發現異動標的量化雷達 ({len(target_df)} 檔)</h3>{html_table}{sop_legend}"

        to_email = os.environ.get("MAIL_TO")
        from_email = os.environ.get("MAIL_USER")
        app_pass = os.environ.get("MAIL_PASS")

        if to_email and from_email and app_pass:
            today_str = datetime.today().strftime("%m/%d")
            send_scan_report_mail(f"🚦 量化早報({today_str}) - {macro_signal}", body, to_email, from_email, app_pass)
        else:
            print("❌ 缺少郵件設定變數 (MAIL_TO / MAIL_USER / MAIL_PASS)")
            print(target_df.to_string(index=False))
    else:
        print("⚠️ 今日無符合條件的掃描結果")
