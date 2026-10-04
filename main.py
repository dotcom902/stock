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
# 📡 OpenBB 資料層初始化
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
    print("✅ OpenBB 核心與擴展模組載入成功。")
except Exception as e:
    print(f"⚠️ OpenBB 核心未載入，改用備援方案: {e}")

def _note(msg):
    if msg not in DATA_SOURCE_NOTES:
        DATA_SOURCE_NOTES.append(msg)
        print(msg)

def get_safe_url(b64_str):
    return base64.b64decode(b64_str).decode("utf-8")

# 核心自選與監控清單
MY_PORTFOLIO = ["NVDA", "TSM", "CRWV", "PLTR", "MSTR", "MU", "INTC"]
CORE_WATCHLIST = ["COHR", "LITE", "FN", "NTAP", "AMD", "ARM", "MU", "SNDK", "SMCI", "ASTS", "RKLB", "LUNR", "BKSY"]

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
    rename = {"open": "Open", "high": "High", "low": "Low", "close": "Close", "adj_close": "Adj Close", "adjclose": "Adj Close", "volume": "Volume", "symbol": "symbol", "date": "date"}
    out = out.rename(columns={c: rename.get(c, c) for c in out.columns})
    if "date" in out.columns:
        out["date"] = pd.to_datetime(out["date"])
        out = out.set_index("date")
    out.index = pd.to_datetime(out.index)
    return out.sort_index()

def _obb_historical_batch(symbols, period="3mo"):
    start_date = _period_to_start(period)
    frames = []
    for sym in symbols:
        fetched = False
        for kwargs in [{"symbol": sym, "start_date": start_date, "provider": "yfinance"}, {"symbol": sym, "start_date": start_date}]:
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
    if not frames:
        raise RuntimeError("OpenBB historical returned no valid frames")

    long_df = pd.concat(frames, axis=0)
    close_wide = long_df.pivot_table(index=long_df.index, columns="symbol", values="Close", aggfunc="last")
    vol_wide = long_df.pivot_table(index=long_df.index, columns="symbol", values="Volume", aggfunc="last")
    open_wide = long_df.pivot_table(index=long_df.index, columns="symbol", values="Open", aggfunc="last") if "Open" in long_df.columns else close_wide
    high_wide = long_df.pivot_table(index=long_df.index, columns="symbol", values="High", aggfunc="last") if "High" in long_df.columns else close_wide
    low_wide = long_df.pivot_table(index=long_df.index, columns="symbol", values="Low", aggfunc="last") if "Low" in long_df.columns else close_wide

    wide = pd.concat({"Open": open_wide, "High": high_wide, "Low": low_wide, "Close": close_wide, "Volume": vol_wide}, axis=1)
    return wide.sort_index()

def _yf_download(symbols, period="3mo"):
    try:
        return yf.download(symbols, period=period, progress=False, threads=True, auto_adjust=False)
    except Exception as e:
        print(f"⚠️ yfinance 批次下載失敗: {e}")
        return pd.DataFrame()

def download_prices(symbols, period="3mo"):
    symbols = list(dict.fromkeys([s.strip().upper() for s in symbols if s]))
    if not symbols:
        return pd.DataFrame()
    if OBB_AVAILABLE and len(symbols) <= 12:
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
    return (100 - (100 / (1 + gain / loss))).replace([np.inf, -np.inf], np.nan).fillna(50)

# ==========================================
# 🚦 總經宏觀紅綠燈
# ==========================================
def fetch_fred_data(series_id, limit=20):
    if OBB_AVAILABLE:
        for kwargs in [{"symbol": series_id, "provider": "fred"}, {"symbol": series_id, "provider": "federal_reserve"}, {"symbol": series_id}]:
            try:
                result = obb.economy.fred_series(**kwargs)
                df = result.to_dataframe()
                if df is None or df.empty:
                    continue
                val_col = next((c for c in df.columns if str(c).lower() in ("value", "close", series_id.lower())), None)
                if not val_col:
                    num_cols = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
                    val_col = num_cols[-1] if num_cols else None
                if val_col:
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
        params = {"series_id": series_id, "api_key": FRED_API_KEY, "file_type": "json", "sort_order": "desc", "limit": limit}
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
        df_data = download_prices(["QQQ", "SMH", "^VIX"], period="3mo")
        df_closes = df_data["Close"] if "Close" in df_data else df_data
        qqq_prices = df_closes["QQQ"].dropna() if "QQQ" in df_closes else pd.Series(dtype=float)
        latest_qqq_rsi = float(_rsi(qqq_prices).iloc[-1]) if len(qqq_prices) else 50.0
        smh_prices = df_closes["SMH"].dropna() if "SMH" in df_closes else pd.Series(dtype=float)
        latest_smh_rsi = float(_rsi(smh_prices).iloc[-1]) if len(smh_prices) else 50.0
        vix_col = "^VIX" if "^VIX" in df_closes else ("VIX" if "VIX" in df_closes else None)
        latest_vix = float(df_closes[vix_col].ffill().dropna().iloc[-1]) if vix_col and not df_closes[vix_col].dropna().empty else 20.0

        fred_str = "▪️ 總經流動性: <i>未設定 FRED API 金鑰，略過利差監控</i>"
        fred_red_flag = fred_yellow_flag = False
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

        if latest_vix > 25 or latest_qqq_rsi < 30 or fred_red_flag:
            status_icon, status_text, signal = "🔴", "紅燈 (Risk-Off / 總經風險擴散與流動性緊縮)", "RED"
        elif fred_yellow_flag or latest_qqq_rsi > 75:
            status_icon, status_text, signal = "🟡", "黃燈 (高檔警報 / 資金成本上揚或短線過熱)", "YELLOW"
        elif latest_smh_rsi < 40:
            status_icon, status_text, signal = "🟠", "橘燈 (AI/半導體族群動能疲弱)", "ORANGE"
        else:
            status_icon, status_text, signal = "🟢", "綠燈 (Risk-On / 流動性與多頭動能健康)", "GREEN"

        desc = f"<span style='font-size:16px;'>{status_icon}</span> <b>{status_text}</b> <br>▪️ VIX 恐慌指數: <b>{latest_vix:.2f}</b> | QQQ 日線 RSI: <b>{latest_qqq_rsi:.1f}</b> | SMH 日線 RSI: <b>{latest_smh_rsi:.1f}</b><br>{fred_str}"
        return desc, signal
    except Exception as e:
        print(f"⚠️ 宏觀指標掃描失敗: {e}")
        return "大盤狀態: 未知", "UNKNOWN"

# ==========================================
# 📰 新聞抓取模組 (強制覆蓋 OpenBB API)
# ==========================================
def get_robust_news(ticker_symbol):
    news_items = []
    # 首選 OpenBB 新聞模組
    if OBB_AVAILABLE:
        try:
            # 使用預設或 fmp provider，確保能抓到財經新聞
            result = obb.news.company(symbol=ticker_symbol, limit=3, provider="fmp" if FMP_API_KEY else "yfinance")
            df = result.to_dataframe()
            if df is not None and not df.empty:
                title_col = next((c for c in df.columns if str(c).lower() in ("title", "headline", "text")), None)
                if title_col:
                    for title in df[title_col].dropna().astype(str).tolist()[:3]:
                        news_items.append({"title": title.split(" - ")[0]})
                    if news_items:
                        _note("📰 新聞來源: OpenBB")
                        return news_items
        except Exception as e:
            print(f"⚠️ OpenBB 新聞抓取失敗 ({ticker_symbol}): {e}")

    # 備援：Google News RSS
    try:
        base_url = get_safe_url("aHR0cHM6Ly9uZXdzLmdvb2dsZS5jb20vcnNzL3NlYXJjaD9xPQ==")
        url = f"{base_url}{urllib.parse.quote(f'{ticker_symbol} stock')}&hl=en-US&gl=US&ceid=US:en"
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
                row = {str(k).lower(): v for k, v in profile.iloc[0].to_dict().items()}
                info["industry"] = row.get("industry") or row.get("sector")
                info["currentPrice"] = row.get("last_price") or row.get("price")
                info["targetMeanPrice"] = row.get("target_price")
                info["recommendationKey"] = str(row.get("recommendation", "")).lower()
                info["earningsTimestamp"] = row.get("earnings_timestamp")
        except Exception:
            pass
    if not info.get("industry"):
        try:
            yinfo = yf.Ticker(ticker).info or {}
            for k, v in yinfo.items():
                if k not in info or not info[k]:
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
                    days = (datetime.fromtimestamp(float(earn_ts)).date() - datetime.today().date()).days
                    days_to_earnings = f"⚠️ {days}天後" if 0 <= days <= 25 else (f"{days}天後" if days > 25 else "近期已公布")
                except Exception:
                    pass

            c_price = float(info.get("currentPrice", 0) or 0)
            t_price = float(info.get("targetMeanPrice", 0) or 0)
            if c_price > 0 and t_price > 0:
                upside = (t_price - c_price) / c_price
                upside_str = f"+{upside * 100:.1f}%" if upside > 0 else f"{upside * 100:.1f}%"
                score += 15 if upside > 0.15 else (-10 if upside < 0 else 0)

            rec = str(info.get("recommendationKey", "")).lower()
            score += 10 if rec in ["buy", "strong_buy", "strongbuy"] else (-15 if rec in ["sell", "underperform"] else 0)

        news_list_raw = get_robust_news(ticker_symbol)
        for article in news_list_raw:
            title = article.get("title", "").lower()
            if any(k in title for k in ["upgrade", "beat", "growth", "surge", "buy", "contract"]):
                score += 4
            if any(k in title for k in ["downgrade", "miss", "cut", "drop", "lawsuit", "sell", "tariff"]):
                score -= 5

        return max(20, min(95, score)), sector, days_to_earnings, news_list_raw, upside_str
    except Exception:
        return 50, "未知", "近期已公布", [], "-"

# ==========================================
# 🎯 期權與策略模組
# ==========================================
def get_investment_strategy(ticker, current_price, prices_series=None):
    try:
        puts, target_date, real_iv = None, None, None

        if OBB_AVAILABLE:
            try:
                res = obb.derivatives.options.chains(symbol=ticker, provider="yfinance").to_dataframe()
                if not res.empty:
                    cols = {str(c).lower(): c for c in res.columns}
                    if all(k in cols for k in ["strike", "bid", "implied_volatility", "option_type", "expiration"]):
                        res["_type"] = res[cols["option_type"]].astype(str).str.lower()
                        work = res[res["_type"].str.contains("put")].copy()
                        if not work.empty:
                            work["_exp"] = pd.to_datetime(work[cols["expiration"]], errors="coerce")
                            work["_dte"] = (work["_exp"] - pd.Timestamp(datetime.today().date())).dt.days
                            window = work[(work["_dte"] >= 20) & (work["_dte"] <= 45)]
                            if not window.empty:
                                target_exp = window.sort_values("_dte")["_exp"].iloc[0]
                                chain = work[work["_exp"] == target_exp]
                                puts = pd.DataFrame({
                                    "strike": pd.to_numeric(chain[cols["strike"]], errors="coerce"),
                                    "bid": pd.to_numeric(chain[cols["bid"]], errors="coerce"),
                                    "impliedVolatility": pd.to_numeric(chain[cols["implied_volatility"]], errors="coerce"),
                                }).dropna()
                                target_date = target_exp.strftime("%Y-%m-%d")
                                _note("🎯 選擇權來源: OpenBB")
            except Exception:
                pass

        if puts is None or puts.empty:
            tk = yf.Ticker(ticker)
            exp_dates = tk.options
            if exp_dates:
                valid_dates = [d for d in exp_dates if 20 <= (datetime.strptime(d, "%Y-%m-%d") - datetime.today()).days <= 45]
                target_date = valid_dates[0] if valid_dates else exp_dates[0]
                puts = tk.option_chain(target_date).puts
                _note("🎯 選擇權來源: yfinance")

        if puts is not None and not puts.empty:
            puts["atm_diff"] = (puts["strike"] - current_price).abs()
            atm_puts = puts[puts["bid"] > 0].sort_values(by="atm_diff")
            if not atm_puts.empty and atm_puts.iloc[0]["impliedVolatility"] > 0.05:
                real_iv = float(atm_puts.iloc[0]["impliedVolatility"])

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
            iv_status = f"<span style='color:#c0392b;font-weight:bold;'>{iv_pct:.1f}% (🔥高IV)</span>" if real_iv >= 0.55 else (f"<span style='color:#2980b9;'>{iv_pct:.1f}% (🧊低IV)</span>" if real_iv <= 0.32 else f"{iv_pct:.1f}% (適中)")
            iv_level = "HIGH" if real_iv >= 0.55 else ("LOW" if real_iv <= 0.32 else "MID")
        else:
            iv_status, iv_level = "約 35% (估計)", "MID"

        return {"has_options": True, "strike_desc": f"Put ${strike:g} ({target_date[-5:] if target_date else '1M'})", "iv_status": iv_status, "iv_level": iv_level}
    except Exception:
        return {"has_options": False, "strike_desc": "-", "iv_status": "-", "iv_level": "UNKNOWN"}

# ==========================================
# 🧠 AI 模組
# ==========================================
def get_market_closing_summary(hot_sectors_desc):
    try:
        indices = {"S&P 500": "^GSPC", "Nasdaq 100": "^NDX", "Dow Jones": "^DJI", "Russell 2000": "IWM"}
        df_indices = download_prices(list(indices.values()), period="5d")
        close_block = df_indices["Close"] if "Close" in df_indices else df_indices
        index_str = ""
        for name, ticker in indices.items():
            col = ticker if ticker in close_block else ticker.replace("^", "")
            if col in close_block:
                closes = close_block[col].dropna()
                if len(closes) >= 2:
                    today_c, yest_c = float(closes.iloc[-1]), float(closes.iloc[-2])
                    pct = ((today_c - yest_c) / yest_c) * 100
                    index_str += f"{name}: {today_c:.2f} ({'🟢' if pct > 0 else '🔴'} {pct:+.2f}%) "

        if not ai_client or DAILY_QUOTA_EXHAUSTED:
            return f"<div style='padding:8px; background:#f0f0f0;'>{index_str}</div>"

        res = ai_client.models.generate_content(
            model=AI_MODEL_NAME, 
            contents=f"美股收盤表現：{index_str}\n今日資金湧入板塊：{hot_sectors_desc}\n寫一段約 80 字收盤速報。重點加 <b>，無 Markdown 碼塊。"
        )
        return f"<div style='background-color:#f4f8fb; border-left:4px solid #2980b9; padding:12px; margin-bottom:15px; border-radius:4px;'><div style='font-family:monospace; font-size:13px; font-weight:bold;'>{index_str}</div><div style='font-size:13px; color:#34495e; line-height:1.5;'>{res.text.replace(chr(10), '<br>')}</div></div>"
    except Exception:
        return ""

def get_ai_dynamic_sectors():
    global DAILY_QUOTA_EXHAUSTED
    fb_tickers, fb_desc = ["COHR", "LITE", "FN", "MU", "SNDK", "RKLB"], "AI 算力光互連、記憶體架構、航太防務"
    if not ai_client or DAILY_QUOTA_EXHAUSTED: return fb_tickers, fb_desc
    try:
        res = ai_client.models.generate_content(
            model=AI_MODEL_NAME, 
            contents='請選出本週資金流入最顯著的3個美股題材，各挑2~3檔市值>20億代表股。嚴格輸出JSON: {"sector_names": "板塊...", "tickers": ["TICKER1", ...]}'
        )
        match = re.search(r"\{.*\}", res.text, re.DOTALL)
        if match:
            data = json.loads(match.group(0))
            valid = [t.strip().upper() for t in data.get("tickers", []) if t.strip().isalpha() and len(t) <= 5]
            if valid: return valid, data.get("sector_names", fb_desc)
    except Exception:
        pass
    return fb_tickers, fb_desc

def analyze_stock_with_ai(ticker, sector, sig, rvol, news):
    global DAILY_QUOTA_EXHAUSTED
    if DAILY_QUOTA_EXHAUSTED or not ai_client: return "⏸️ AI 暫停分析"
    news_text = "\n".join([f"- {item.get('title', '')}" for item in news[:3]])
    prompt = f"標的：{ticker}({sector})\n狀態：{sig}, RVOL:{rvol}\n新聞：\n{news_text}\n以交易員視角用 50 字內說明風險/驅動。結尾限填四選一：【建議：可以建倉/觀望收租/鎖定利潤/高風險避開】。"
    try:
        res = ai_client.models.generate_content(model=AI_MODEL_NAME, contents=prompt)
        time.sleep(1)
        return res.text.strip().replace("\n", "<br>")
    except Exception as e:
        if "429" in str(e): DAILY_QUOTA_EXHAUSTED = True
        return "⏸️ 伺服器繁忙"

# ==========================================
# 🚀 掃描與決策
# ==========================================
def scan_market_opportunities(tickers_list, portfolio_list, hot_sectors_list, macro_signal):
    all_tickers = list(dict.fromkeys(tickers_list + portfolio_list + hot_sectors_list))
    df_data = download_prices(all_tickers, period="3mo")
    if df_data.empty: return pd.DataFrame()
    df_closes = df_data["Close"] if "Close" in df_data else df_data
    df_volumes = df_data["Volume"] if "Volume" in df_data else pd.DataFrame()

    raw_cands = []
    for ticker in df_closes.columns:
        try:
            prices = df_closes[ticker].dropna()
            if len(prices) < 20: continue
            latest_price, ma20 = float(prices.iloc[-1]), float(prices.rolling(20).mean().iloc[-1])
            bias_20 = ((latest_price - ma20) / ma20) * 100
            latest_rsi = float(_rsi(prices).iloc[-1])
            
            rvol = 1.0
            if not df_volumes.empty and ticker in df_volumes.columns:
                vols = df_volumes[ticker].dropna()
                if len(vols) >= 20 and float(vols.rolling(20).mean().iloc[-1]) > 0:
                    rvol = round(float(vols.iloc[-1]) / float(vols.rolling(20).mean().iloc[-1]), 2)
            
            is_port, is_hot = ticker in portfolio_list, ticker in hot_sectors_list
            sig = None
            if is_hot:
                if latest_rsi >= 75: sig = "極端超買(破線)"
                elif latest_rsi > 62 and bias_20 > 4 and rvol > 1.2: sig = "動能突破"
                elif 50 <= latest_rsi <= 62 and abs(bias_20) <= 5: sig = "高檔震盪"
                elif 40 <= latest_rsi < 52 and bias_20 > -4: sig = "強勢回檔"
                elif latest_rsi < 40: sig = "趨勢破壞"
                elif rvol > 1.5: sig = "板塊異動(爆量)"
            else:
                if latest_rsi < 35 and bias_20 < -6: sig = "超跌反彈"
                elif latest_rsi > 65 and bias_20 > 5 and rvol > 1.2: sig = "動能突破"
            
            if is_port and not sig: sig = "持倉監控"
            
            if sig:
                f_score, sector, d_earn, raw_news, up_str = get_fundamental_sentiment_score(ticker)
                strat = get_investment_strategy(ticker, latest_price, prices)
                raw_cands.append({
                    "ticker": ticker, "is_portfolio": is_port, "is_hot": is_hot, "rvol": rvol, "sig": sig,
                    "price": latest_price, "rsi": latest_rsi, "upside": up_str, "sector": sector, "d_earn": d_earn,
                    "f_score": f_score, "strat": strat, "news": raw_news,
                    "weight": (1000 if is_port else 0) + f_score + (rvol * (25 if sig in ["強勢回檔", "動能突破"] else 10))
                })
        except Exception: continue

    raw_cands.sort(key=lambda x: x["weight"], reverse=True)
    ai_targets = [c for c in raw_cands if c["sig"] not in ["持倉監控", "高檔震盪"]][:8]

    res = []
    for item in raw_cands:
        ai_view = analyze_stock_with_ai(item["ticker"], item["sector"], item["sig"], f"{item['rvol']}x", item["news"]) if item in ai_targets else "<i>監控中</i>"
        sop = "🔹 <b>核心持股</b>：維持部位，破月線減碼。"
        if "高風險避開" in ai_view or "⚠️" in ai_view: sop = "<span style='color:#c0392b;font-weight:bold;'>🛑 AI 否決</span>：風險偏高，嚴禁建倉。"
        elif "⚠️" in item["d_earn"]: sop = "<span style='color:#e67e22;font-weight:bold;'>⚠️ 財報臨近</span>：禁做賣方 Sell Put；正股防禦。"
        elif item["sig"] == "趨勢破壞": sop = "<span style='color:#c0392b;'>🛑 破線轉弱</span>：RSI 破位，避開。"
        elif item["sig"] == "強勢回檔": sop = "🥇 <b>情境A (回檔)</b>：首選 <b>Sell Put / Bull Put Spread</b> 收租。" if item["strat"]["iv_level"] == "HIGH" else "🥇 <b>情境A (回檔)</b>：建議買入現貨。"
        elif item["sig"] in ["動能突破", "板塊異動(爆量)"]: sop = "🥈 <b>情境B (突破)</b>：改買現貨防 IV Crush。" if item["strat"]["iv_level"] == "HIGH" else "🥈 <b>情境B (突破)</b>：買入現貨並設停損。"
        
        if macro_signal == "RED" and "情境" in sop: sop = "🛑 <b>[紅燈警戒]</b> 流動性緊縮，全面觀望！"

        badge = ("<span style='background:#2980b9; color:white; padding:1px 5px; border-radius:3px; font-size:10px;'>持倉</span> " if item["is_portfolio"] else "") + \
                ("<span style='background:#e67e22; color:white; padding:1px 5px; border-radius:3px; font-size:10px;'>熱門</span>" if item["is_hot"] else "")
        
        res.append({
            "標的 / 板塊": f"<b>{item['ticker']}</b><br>{badge}<br><span style='color:#7f8c8d; font-size:11px;'>{item['sector'][:12]}</span>",
            "現價 (Upside)": f"<b>${item['price']:.2f}</b><br><span style='color:#27ae60; font-size:11px;'>{item['upside']}</span>",
            "型態與量能": f"<b>{item['sig']}</b><br><span style='font-size:11px;'>RSI: {item['rsi']:.1f} | 量: {item['rvol']}x</span>",
            "財報倒數": item["d_earn"],
            "期權策略 & IV": f"{item['strat']['strike_desc']}<br>{item['strat']['iv_status']}" if item["strat"]["has_options"] else "<span style='color:#95a5a6;'>無期權</span>",
            "評分": f"<b>{int(item['f_score'])}</b>",
            "🤖 AI 投研觀點": ai_view,
            "🎯 實戰 SOP 操作指引": sop,
        })
    return pd.DataFrame(res)

def send_scan_report_mail(subject, body, to_emails_str, from_email, app_password):
    msg = MIMEMultipart()
    msg["From"], msg["To"], msg["Subject"] = from_email, to_emails_str, subject
    msg.attach(MIMEText(f"<html><body>{body}</body></html>", "html"))
    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(from_email, app_password)
            server.sendmail(from_email, [e.strip() for e in to_emails_str.split(",")], msg.as_string())
        print("📧 報告發送成功")
    except Exception as e: print(f"❌ 寄信失敗: {e}")

if __name__ == "__main__":
    macro_desc, macro_signal = get_macro_regime()
    ndx_pool = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "COST", "AMD"]
    hot_tickers, hot_desc = get_ai_dynamic_sectors()
    market_summary_html = get_market_closing_summary(hot_desc)
    target_df = scan_market_opportunities(ndx_pool, MY_PORTFOLIO, list(set(hot_tickers + CORE_WATCHLIST)), macro_signal)
    
    if not target_df.empty:
        html_style = "<style>body{font-family:sans-serif;color:#2c3e50;line-height:1.5}.status-box{background:#f8f9fa;border-left:4px solid #34495e;padding:12px;margin-bottom:16px;font-size:13px;border-radius:4px}table{border-collapse:collapse;width:100%;font-size:12px}th{background:#2c3e50;color:white;padding:8px}td{border:1px solid #e1e8ed;padding:8px;text-align:center}</style>"
        status_html = f"<div class='status-box'><h3>📊 宏觀儀表板</h3>{macro_desc}<div style='font-size:11px;color:#7f8c8d'>資料層: {' | '.join(DATA_SOURCE_NOTES)}</div></div>"
        body = f"{html_style}{status_html}{market_summary_html}<h3>🎯 異動量化雷達</h3>{target_df.to_html(index=False, escape=False)}"
        if os.environ.get("MAIL_TO"): send_scan_report_mail(f"🚦 量化早報 - {macro_signal}", body, os.environ.get("MAIL_TO"), os.environ.get("MAIL_USER"), os.environ.get("MAIL_PASS"))
        else: print("❌ 缺少郵件設定")
