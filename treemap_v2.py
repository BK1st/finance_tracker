import html
import io
import json
import os
import sqlite3
import urllib.request
from datetime import date, datetime, timedelta
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components
import yfinance as yf

# ---------------------------------------------------------
# 모바일 전용 페이지 설정 (상단 1회만 호출)
# ---------------------------------------------------------
st.set_page_config(
    page_title="재정 관리 앱 (모바일)",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# ---------------------------------------------------------
# DB 및 백업 설정
# ---------------------------------------------------------
DB_FILE = "stock_data.db"
BACKUP_FILE = "portfolio_backup.json"
TREEMAP_PRESET_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".data", "treemap_presets"
)
TREEMAP_CAT_OPTIONS = {
    "소유자(WHOSE)": "whose",
    "구분": "account_type",
    "금융사": "broker",
    "보유항목(ITEM)": "item_name",
    "계좌번호": "account_num",
    "분류1": "category1",
    "분류2": "category2",
    "분류3": "category3",
    "분류4": "category4",
}
TREEMAP_COLOR_OPTIONS = [
    "총 누적 수익률 (%)",
    "1) 일간 : 전일 종가 대비 현재 최신 가격 등락률",
    "2) 주간 : 전주 종가 대비 현재 최신 가격 등락률",
    "3) 월간 : 전월 종가 대비 현재 최신 가격 등락률",
    "4) 연간 : 전년 종가 대비 현재 최신 가격 등락률",
    "5) 특정 지정 날짜 이후 : 해당일 종가 대비 현재 최신 가격 등락률",
]


def get_connection():
    return sqlite3.connect(DB_FILE, timeout=10.0)


def export_backup_json():
    """DB 내의 데이터 및 분석 조건(Treemap 프리셋)을 하나의 JSON 백업 파일로 저장"""
    conn = get_connection()
    try:
        df = pd.read_sql("""
            SELECT record_date, whose, broker, account_num, account_type, item_name, ticker,
                   category1, category2, category3, category4, buy_price, quantity,
                   current_price, currency, exchange_rate
            FROM portfolio
        """, conn)
        
        portfolio_records = df.to_dict(orient="records")
        
        # 분석 조건(프리셋) 정보 함께 읽기
        presets_data = {}
        _ensure_treemap_preset_dir()
        if os.path.exists(TREEMAP_PRESET_DIR):
            for fname in os.listdir(TREEMAP_PRESET_DIR):
                if fname.endswith(".json"):
                    preset_name = fname[:-5]
                    fpath = os.path.join(TREEMAP_PRESET_DIR, fname)
                    try:
                        with open(fpath, "r", encoding="utf-8") as pf:
                            presets_data[preset_name] = json.load(pf)
                    except Exception:
                        pass

        # 통합 백업 페이로드 생성
        backup_payload = {
            "portfolio": portfolio_records,
            "treemap_presets": presets_data
        }
        
        json_bytes = json.dumps(backup_payload, ensure_ascii=False, indent=2)
        with open(BACKUP_FILE, "w", encoding="utf-8") as f:
            f.write(json_bytes)
        return json_bytes
    except Exception:
        return None
    finally:
        conn.close()


def import_backup_json(json_content, replace=True):
    """JSON 백업 데이터를 DB 및 분석 조건(Treemap 프리셋) 파일로 복원"""
    conn = get_connection()
    try:
        if isinstance(json_content, bytes):
            json_content = json_content.decode("utf-8")
        data = json.loads(json_content)
        if not data:
            return 0
        
        # 하위 호환성 처리 (기존의 리스트 형태 백업 파일인 경우)
        if isinstance(data, list):
            portfolio_data = data
            presets_data = {}
        elif isinstance(data, dict):
            portfolio_data = data.get("portfolio", [])
            presets_data = data.get("treemap_presets", {})
        else:
            return 0

        df = pd.DataFrame(portfolio_data)
        required_cols = [
            "record_date", "whose", "broker", "account_num", "account_type", "item_name",
            "ticker", "category1", "category2", "category3", "category4",
            "buy_price", "quantity", "current_price", "currency", "exchange_rate"
        ]
        for col in required_cols:
            if col not in df.columns:
                df[col] = None
        df["currency"] = df["currency"].fillna("KRW")
        df["exchange_rate"] = pd.to_numeric(df["exchange_rate"], errors="coerce").fillna(1.0)
        df["whose"] = df["whose"].fillna("본인")
        df["buy_price"] = pd.to_numeric(df["buy_price"], errors="coerce").fillna(0.0)
        df["quantity"] = pd.to_numeric(df["quantity"], errors="coerce").fillna(0.0)
        df["current_price"] = pd.to_numeric(df["current_price"], errors="coerce").fillna(0.0)
        
        cursor = conn.cursor()
        if replace:
            cursor.execute("DELETE FROM portfolio")
        
        df[required_cols].to_sql("portfolio", conn, if_exists="append", index=False)
        conn.commit()

        # 분석 조건(프리셋) 복원
        if presets_data:
            _ensure_treemap_preset_dir()
            for p_name, p_payload in presets_data.items():
                save_treemap_preset(p_name, p_payload)
        
        export_backup_json()
        return len(df)
    except Exception:
        conn.rollback()
        return 0
    finally:
        conn.close()


def init_db():
    """DB 초기화 및 whose 컬럼 마이그레이션 적용"""
    conn = get_connection()
    count = 0
    try:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS portfolio (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                record_date TEXT,
                whose TEXT DEFAULT '본인',
                broker TEXT,
                account_num TEXT,
                account_type TEXT,
                item_name TEXT,
                ticker TEXT,
                category1 TEXT,
                category2 TEXT,
                category3 TEXT,
                category4 TEXT,
                buy_price REAL,
                quantity REAL,
                current_price REAL,
                currency TEXT DEFAULT 'KRW',
                exchange_rate REAL DEFAULT 1.0
            )
        """)
        conn.commit()
        
        cursor.execute("PRAGMA table_info(portfolio)")
        columns = [column[1] for column in cursor.fetchall()]
        if "whose" not in columns:
            cursor.execute("ALTER TABLE portfolio ADD COLUMN whose TEXT DEFAULT '본인'")
            conn.commit()

        cursor.execute("SELECT COUNT(*) FROM portfolio")
        count = cursor.fetchone()[0]
    finally:
        conn.close()

    if count == 0 and os.path.exists(BACKUP_FILE):
        try:
            with open(BACKUP_FILE, "r", encoding="utf-8") as f:
                content = f.read()
            import_backup_json(content, replace=False)
        except Exception:
            pass


# ---------------------------------------------------------
# Treemap 분석 조건 저장/불러오기
# ---------------------------------------------------------
def _ensure_treemap_preset_dir():
    os.makedirs(TREEMAP_PRESET_DIR, exist_ok=True)


def _safe_preset_filename(name):
    cleaned = "".join(
        ch for ch in str(name).strip() if ch.isalnum() or ch in " _-().[]"
    ).strip()
    return cleaned[:80]


def list_treemap_presets():
    _ensure_treemap_preset_dir()
    names = []
    for fname in sorted(os.listdir(TREEMAP_PRESET_DIR)):
        if fname.endswith(".json"):
            names.append(fname[:-5])
    return names


def save_treemap_preset(name, payload):
    safe = _safe_preset_filename(name)
    if not safe:
        return None
    _ensure_treemap_preset_dir()
    path = os.path.join(TREEMAP_PRESET_DIR, f"{safe}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return safe


def load_treemap_preset(name):
    safe = _safe_preset_filename(name)
    path = os.path.join(TREEMAP_PRESET_DIR, f"{safe}.json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def delete_treemap_preset(name):
    safe = _safe_preset_filename(name)
    path = os.path.join(TREEMAP_PRESET_DIR, f"{safe}.json")
    if os.path.exists(path):
        os.remove(path)
        return True
    return False


def _to_date_str(val):
    if val is None or val == "":
        return None
    if isinstance(val, datetime):
        return val.strftime("%Y-%m-%d")
    if isinstance(val, date):
        return val.strftime("%Y-%m-%d")
    return str(val)[:10]


def _parse_iso_date(val):
    text = _to_date_str(val)
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except Exception:
        return None


def apply_treemap_preset(payload, available_whose_list, whose_date_map):
    """위젯 생성 전에 session_state에 조건을 반영한다."""
    cat_keys = list(TREEMAP_CAT_OPTIONS.keys())
    l234 = ["없음"] + cat_keys

    whose = [
        w
        for w in payload.get("whose", available_whose_list)
        if w in available_whose_list
    ]
    st.session_state["treemap_sel_whose"] = whose if whose else list(available_whose_list)

    owner_dates = payload.get("owner_dates", {}) or {}
    for w, d in owner_dates.items():
        options = whose_date_map.get(w, [])
        if d in options:
            st.session_state[f"select_date_{w}"] = d

    st.session_state["treemap_use_hist"] = bool(payload.get("use_historical_price", False))
    hist_date = _parse_iso_date(payload.get("target_eval_date"))
    if hist_date:
        st.session_state["treemap_hist_date"] = hist_date

    l1 = payload.get("l1")
    l2 = payload.get("l2")
    l3 = payload.get("l3")
    l4 = payload.get("l4")
    if l1 in cat_keys:
        st.session_state["treemap_l1"] = l1
    if l2 in l234:
        st.session_state["treemap_l2"] = l2
    if l3 in l234:
        st.session_state["treemap_l3"] = l3
    if l4 in l234:
        st.session_state["treemap_l4"] = l4

    color_option = payload.get("color_option")
    if color_option in TREEMAP_COLOR_OPTIONS:
        st.session_state["treemap_color_option"] = color_option
    custom_base = _parse_iso_date(payload.get("custom_base_date"))
    if custom_base:
        st.session_state["treemap_custom_base_date"] = custom_base

    drill = payload.get("drilldown")
    if drill:
        st.session_state["treemap_drilldown_selector"] = drill


def collect_treemap_preset_payload(
    selected_whose_list,
    owner_selected_dates,
    use_historical_price,
    target_eval_date,
    l1,
    l2,
    l3,
    l4,
    color_option,
    custom_base_date,
    drilldown,
):
    return {
        "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "whose": list(selected_whose_list),
        "owner_dates": {w: owner_selected_dates.get(w) for w in selected_whose_list},
        "use_historical_price": bool(use_historical_price),
        "target_eval_date": _to_date_str(target_eval_date) if use_historical_price else None,
        "l1": l1,
        "l2": l2,
        "l3": l3,
        "l4": l4,
        "color_option": color_option,
        "custom_base_date": _to_date_str(custom_base_date),
        "drilldown": drilldown,
    }


def render_expandable_tree_table(rows, profit_col_label, rate_col_label):
    """상위 행 더블클릭(또는 ▶ 클릭) 시 하위 행을 펼치는 계층 표."""
    header_html = "".join(
        f"<th>{html.escape(col)}</th>"
        for col in ["구분 항목", "평가액(원)", profit_col_label, rate_col_label, "점유율(%)"]
    )
    body_parts = []
    for row in rows:
        depth = int(row["depth"])
        has_children = bool(row["has_children"])
        hidden_class = " tree-hidden" if depth >= 1 else ""
        parent_cls = " tree-parent" if has_children else ""
        caret = "▶" if has_children else ""
        indent_px = 8 + max(depth, 0) * 18
        label = html.escape(str(row["label"]))
        eval_txt = html.escape(f"₩{row['eval']:,.0f}")
        profit_txt = html.escape(f"₩{row['profit']:,.0f}")
        rate_txt = html.escape(f"{row['rate']:+.2f}%")
        share_txt = html.escape(f"{row['share']:.2f}%")
        parent_id = "" if row["parent_id"] is None else str(row["parent_id"])
        title = "더블클릭하면 하위 항목이 펼쳐집니다." if has_children else ""
        body_parts.append(
            f'<tr class="{parent_cls}{hidden_class}" data-id="{row["id"]}" '
            f'data-parent="{parent_id}" data-has-children="{str(has_children).lower()}" '
            f'title="{html.escape(title)}">'
            f'<td class="label-cell" style="padding-left:{indent_px}px;">'
            f'<span class="caret" data-id="{row["id"]}">{caret}</span> {label}</td>'
            f'<td class="num">{eval_txt}</td>'
            f'<td class="num">{profit_txt}</td>'
            f'<td class="num">{rate_txt}</td>'
            f'<td class="num">{share_txt}</td>'
            f"</tr>"
        )

    visible_count = sum(1 for r in rows if int(r["depth"]) < 1)
    height = min(760, 90 + 36 * max(visible_count + 2, 6))

    html_doc = f"""
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8"/>
<style>
  body {{ margin: 0; font-family: "Segoe UI", sans-serif; color: #1f1f1f; }}
  .hint {{ font-size: 12px; color: #666; margin: 0 0 8px 0; }}
  table.tree-table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  table.tree-table th {{
    background: #f0f2f6; text-align: right; padding: 8px 10px;
    border-bottom: 1px solid #d0d5dd; white-space: nowrap;
  }}
  table.tree-table th:first-child {{ text-align: left; }}
  table.tree-table td {{
    padding: 7px 10px; border-bottom: 1px solid #ececec; white-space: nowrap;
  }}
  table.tree-table td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  table.tree-table tr.tree-parent {{ cursor: pointer; }}
  table.tree-table tr.tree-parent:hover {{ background: #eef4ff; }}
  table.tree-table tr.tree-hidden {{ display: none; }}
  .caret {{
    display: inline-block; width: 14px; color: #3366cc; font-size: 11px;
    user-select: none;
  }}
</style>
</head>
<body>
  <p class="hint">상위 항목을 더블클릭하거나 ▶ 를 누르면 하위 세부 항목이 펼쳐집니다.</p>
  <table class="tree-table">
    <thead><tr>{header_html}</tr></thead>
    <tbody>
      {''.join(body_parts)}
    </tbody>
  </table>
  <script>
    function childrenOf(id) {{
      return Array.from(document.querySelectorAll('tr[data-parent="' + id + '"]'));
    }}
    function hideDescendants(id) {{
      childrenOf(id).forEach(function(child) {{
        child.classList.add('tree-hidden');
        var caret = child.querySelector('.caret');
        if (caret && child.dataset.hasChildren === 'true') caret.textContent = '▶';
        hideDescendants(child.dataset.id);
      }});
    }}
    function toggleRow(id) {{
      var kids = childrenOf(id);
      if (!kids.length) return;
      var expand = kids[0].classList.contains('tree-hidden');
      var row = document.querySelector('tr[data-id="' + id + '"]');
      var caret = row ? row.querySelector('.caret') : null;
      if (expand) {{
        kids.forEach(function(child) {{ child.classList.remove('tree-hidden'); }});
        if (caret) caret.textContent = '▼';
      }} else {{
        hideDescendants(id);
        if (caret) caret.textContent = '▶';
      }}
    }}
    document.querySelectorAll('tr.tree-parent').forEach(function(row) {{
      row.addEventListener('dblclick', function() {{ toggleRow(row.dataset.id); }});
    }});
    document.querySelectorAll('.caret').forEach(function(el) {{
      if (!el.textContent) return;
      el.addEventListener('click', function(ev) {{
        ev.stopPropagation();
        toggleRow(el.dataset.id);
      }});
    }});
  </script>
</body>
</html>
"""
    components.html(html_doc, height=height, scrolling=True)


# ---------------------------------------------------------
# 배치 시세 수집 및 캐싱 함수
# ---------------------------------------------------------
@st.cache_data(ttl=3600)
def get_exchange_rate():
    try:
        url = "https://open.er-api.com/v6/latest/USD"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        response = urllib.request.urlopen(req)
        data = json.loads(response.read().decode("utf-8"))
        return round(float(data["rates"]["KRW"]), 2)
    except Exception:
        return 1350.0


def fetch_live_exchange_rate():
    try:
        ticker = yf.Ticker("KRW=X")
        price = ticker.fast_info.get("lastPrice") or ticker.fast_info.get("previousClose")
        if price:
            return round(float(price), 2)
        df = ticker.history(period="1d")
        if not df.empty:
            return round(float(df["Close"].iloc[-1]), 2)
        raise Exception("가격 정보를 찾을 수 없습니다.")
    except Exception:
        st.warning("환율 조회 실패. 기존 사이드바 환율을 유지합니다.")
        return st.session_state.get("live_rate_store", 1350.0)


def normalize_ticker(ticker, currency):
    """국내 주식 및 미국 주식 티커 규격화"""
    if not ticker or pd.isna(ticker) or str(ticker).strip() == "":
        return None
    t_str = str(ticker).strip().upper()
    if currency == "KRW":
        code = t_str.zfill(6)
        if not (code.endswith(".KS") or code.endswith(".KQ")):
            return f"{code}.KS"
        return code
    return t_str


def fetch_live_ticker_price(ticker, regular_only=None):
    """단일 티커의 최신 시세 가져오기 (정규장만 반영 옵션 지원)"""
    if not ticker:
        return None
    if regular_only is None:
        regular_only = st.session_state.get("regular_market_only", False)
    try:
        t_str = str(ticker).strip().upper()
        tk = yf.Ticker(t_str)

        # 1. 국내 주식(.KS, .KQ)은 프리/애프터마켓이 없으므로 fast_info로 즉시 반환
        if t_str.endswith(".KS") or t_str.endswith(".KQ"):
            fast_info = tk.fast_info
            for key in ["lastPrice", "regularMarketPrice", "previousClose"]:
                val = fast_info.get(key)
                if val is not None and not pd.isna(val) and float(val) > 0:
                    return round(float(val), 2)
            hist = tk.history(period="1d")
            if not hist.empty and not pd.isna(hist["Close"].iloc[-1]):
                return round(float(hist["Close"].iloc[-1]), 2)
            return None

        # 2. 미국 주식 등 해외 주식
        fast_info = tk.fast_info
        pre_price = fast_info.get("preMarketPrice")
        post_price = fast_info.get("postMarketPrice")
        last_price = fast_info.get("lastPrice") or fast_info.get("regularMarketPrice")

        info = {}
        try:
            info = tk.info or {}
        except Exception:
            pass

        pre_price = pre_price or info.get("preMarketPrice")
        post_price = post_price or info.get("postMarketPrice")
        reg_price = last_price or info.get("currentPrice") or info.get("regularMarketPrice")
        market_state = str(info.get("marketState", "")).upper()

        # 정규장만 반영 옵션이 활성화된 경우: 프리/애프터마켓 가격 및 prepost 1분봉 조회 안 함
        if regular_only:
            if reg_price and not pd.isna(reg_price) and float(reg_price) > 0:
                return round(float(reg_price), 2)
            hist = tk.history(period="1d", prepost=False)
            if not hist.empty and not pd.isna(hist["Close"].iloc[-1]):
                return round(float(hist["Close"].iloc[-1]), 2)
            for p in [reg_price, fast_info.get("previousClose"), info.get("previousClose")]:
                if p is not None and not pd.isna(p) and float(p) > 0:
                    return round(float(p), 2)
            return None

        # (1) 프리마켓 시간대인 경우: Pre-market 가격 최우선
        if market_state in ["PRE", "PREPRE"] and pre_price and not pd.isna(pre_price) and float(pre_price) > 0:
            return round(float(pre_price), 2)

        # (2) 애프터마켓 시간대인 경우: Post-market 가격 최우선
        if market_state in ["POST", "POSTPOST"] and post_price and not pd.isna(post_price) and float(post_price) > 0:
            return round(float(post_price), 2)

        # (3) 장마감(CLOSED) 상태: 당일 애프터마켓 거래가가 남아있으면 정규장 종가보다 최신이므로 우선 채택
        if market_state == "CLOSED" and post_price and not pd.isna(post_price) and float(post_price) > 0:
            return round(float(post_price), 2)

        # (4) 정규장(REGULAR) 시간대: 실시간 정규 체결가 우선
        if market_state == "REGULAR" and reg_price and not pd.isna(reg_price) and float(reg_price) > 0:
            return round(float(reg_price), 2)

        # (5) 시장 상태 구분이 모호한 경우: 1분봉(prepost=True)으로 가장 최근 체결 틱 확인
        try:
            hist_1m = tk.history(period="1d", interval="1m", prepost=True)
            if not hist_1m.empty and not pd.isna(hist_1m["Close"].iloc[-1]):
                return round(float(hist_1m["Close"].iloc[-1]), 2)
        except Exception:
            pass

        # (6) Fallback: 유효한 가격 순차 탐색
        for p in [post_price, pre_price, reg_price, fast_info.get("previousClose"), info.get("previousClose")]:
            if p is not None and not pd.isna(p) and float(p) > 0:
                return round(float(p), 2)

    except Exception:
        pass
    return None


@st.cache_data(ttl=300)
def fetch_batch_market_data(ticker_tuple, start_date_str, end_date_str, regular_only=None):
    """모든 종목의 시세를 yf.download로 요청하여 캐싱"""
    tickers = [t for t in ticker_tuple if t]
    if not tickers:
        return pd.DataFrame()

    if regular_only is None:
        regular_only = st.session_state.get("regular_market_only", False)

    ticker_str = " ".join(list(set(tickers)))
    try:
        data = yf.download(
            ticker_str,
            start=start_date_str,
            end=end_date_str,
            group_by="ticker",
            auto_adjust=True,
            progress=False,
            prepost=not regular_only,
        )
        return data
    except Exception:
        return pd.DataFrame()


def _extract_ticker_series(market_data, ticker):
    """배치 데이터프레임에서 특정 티커의 Close 시리즈를 안전하게 추출 (MultiIndex/SingleIndex 일관 처리)"""
    if market_data is None or market_data.empty or not ticker:
        return None
    try:
        df_ticker = market_data
        if isinstance(market_data.columns, pd.MultiIndex):
            if ticker in market_data.columns.levels[0]:
                df_ticker = market_data[ticker]
            elif ticker in market_data.columns.levels[1]:
                df_ticker = market_data.xs(ticker, axis=1, level=1)
            else:
                return None
        
        if hasattr(df_ticker.index, "tz") and df_ticker.index.tz is not None:
            df_ticker.index = df_ticker.index.tz_localize(None)

        if "Close" in df_ticker.columns:
            series = df_ticker["Close"]
        elif isinstance(df_ticker, pd.Series):
            series = df_ticker
        else:
            return None
        return series.dropna()
    except Exception:
        return None


def get_price_from_batch_data(market_data, ticker, target_date_str=None, regular_only=None):
    """배치 수집된 데이터프레임에서 우선 종가를 찾고, 없을 때만 개별 실시간 시세 조회"""
    if not ticker:
        return None

    if regular_only is None:
        regular_only = st.session_state.get("regular_market_only", False)

    series = _extract_ticker_series(market_data, ticker)
    if series is not None and not series.empty:
        if target_date_str:
            target_dt = pd.to_datetime(target_date_str)
            valid = series[series.index <= target_dt]
            if not valid.empty:
                return round(float(valid.iloc[-1]), 2)
        else:
            return round(float(series.iloc[-1]), 2)

    # 배치 데이터에 없거나 날짜 데이터가 비어 있는 경우에만 개별 실시간 시세 조회 fallback
    return fetch_live_ticker_price(ticker, regular_only=regular_only)


def _get_last_trading_day_before(m_data, ticker, target_date_dt):
    """지정 날짜 이전의 가장 최근 거래일 종가를 가져옴"""
    series = _extract_ticker_series(m_data, ticker)
    if series is None or series.empty:
        return None
    try:
        target_ts = pd.to_datetime(target_date_dt)
        valid = series[series.index <= target_ts]
        if not valid.empty:
            return float(valid.iloc[-1])
    except Exception:
        pass
    return None


def update_all_prices_and_rate_batch(curr_rate, regular_only=None):
    """배치 수집 방식으로 전체 시세 및 환율 일괄 업데이트 (티커별 단 1회 조회 최적화)"""
    if regular_only is None:
        regular_only = st.session_state.get("regular_market_only", False)

    conn = get_connection()
    try:
        df = pd.read_sql("SELECT id, ticker, currency FROM portfolio", conn)
        if df.empty:
            return 0

        df["formatted_ticker"] = df.apply(
            lambda r: normalize_ticker(r["ticker"], r["currency"]), axis=1
        )
        valid_tickers = tuple(df["formatted_ticker"].dropna().unique().tolist())

        today = datetime.now()
        start_date_str = (today - timedelta(days=10)).strftime("%Y-%m-%d")
        end_date_str = (today + timedelta(days=2)).strftime("%Y-%m-%d")

        market_data = fetch_batch_market_data(valid_tickers, start_date_str, end_date_str, regular_only=regular_only)

        # 티커별 최신 시세를 사전 계산하여 메모리 캐싱
        price_cache = {}
        for f_ticker in valid_tickers:
            is_kr = f_ticker.endswith(".KS") or f_ticker.endswith(".KQ")
            if not is_kr:
                # 미국/해외 주식: 정규장만 반영 여부에 따라 시세 수집
                live_p = fetch_live_ticker_price(f_ticker, regular_only=regular_only)
                if live_p is not None:
                    price_cache[f_ticker] = live_p
                else:
                    price_cache[f_ticker] = get_price_from_batch_data(market_data, f_ticker, today.strftime("%Y-%m-%d"), regular_only=regular_only)
            else:
                # 국내 주식: 일봉 배치 데이터 우선 (빠름)
                batch_p = get_price_from_batch_data(market_data, f_ticker, today.strftime("%Y-%m-%d"), regular_only=regular_only)
                price_cache[f_ticker] = batch_p if batch_p is not None else fetch_live_ticker_price(f_ticker, regular_only=regular_only)

        cursor = conn.cursor()
        updated_count = 0
        
        for _, row in df.iterrows():
            p_id = row["id"]
            curr = row["currency"]
            f_ticker = row["formatted_ticker"]
            ex_rate = curr_rate if curr == "USD" else 1.0
            new_price = price_cache.get(f_ticker)

            if new_price is not None:
                cursor.execute(
                    "UPDATE portfolio SET current_price = ?, exchange_rate = ? WHERE id = ?",
                    (new_price, ex_rate, p_id),
                )
                updated_count += 1
            else:
                cursor.execute(
                    "UPDATE portfolio SET exchange_rate = ? WHERE id = ?",
                    (ex_rate, p_id),
                )

        conn.commit()
        export_backup_json()
        return updated_count
    finally:
        conn.close()


# ---------------------------------------------------------
# 매매(트레이딩) 전용 다이얼로그
# ---------------------------------------------------------
@st.dialog("📈 매매(트레이딩) 입력 - 신규 매수 / 추가 매수 / 매도")
def open_trading_dialog():
    conn = get_connection()
    try:
        df = pd.read_sql("SELECT * FROM portfolio", conn)
    finally:
        conn.close()

    trade_type = st.radio("거래 종류 선택", ["신규 매수", "기존 종목 추가 매수 (물타기/불타기)", "기존 종목 매도 (부분/전량)"], horizontal=True)

    if trade_type == "신규 매수":
        st.caption("새로운 종목이나 계좌를 포트폴리오에 신규 추가합니다.")
        with st.form("dialog_new_buy_form"):
            c1, c2 = st.columns(2)
            with c1:
                t_date = st.date_input("거래일", datetime.now()).strftime("%Y-%m-%d")
                t_whose = st.text_input("소유자", value="본인")
                t_broker = st.text_input("증권사/금융사", value="키움증권")
                t_acc_num = st.text_input("계좌번호")
                t_acc_type = st.selectbox("계좌구분", ["일반", "연금", "ISA", "IRP", "기타"])
            with c2:
                t_item = st.text_input("보유항목명 (예: 삼성전자, Apple)")
                t_ticker = st.text_input("티커/종목코드")
                t_curr = st.selectbox("통화", ["KRW", "USD"])
                t_price = st.number_input("매수 단가", min_value=0.0, step=1.0)
                t_qty = st.number_input("매수 수량", min_value=0.0, step=1.0)

            submitted = st.form_submit_button("🚀 신규 매수 반영")
            if submitted:
                if not t_item or t_price <= 0 or t_qty <= 0:
                    st.error("보유항목명, 단가, 수량을 정확히 입력해 주세요.")
                else:
                    conn = get_connection()
                    try:
                        cursor = conn.cursor()
                        ex_r = st.session_state.get("live_rate_store", 1350.0) if t_curr == "USD" else 1.0
                        cursor.execute("""
                            INSERT INTO portfolio (record_date, whose, broker, account_num, account_type, item_name, ticker, buy_price, quantity, current_price, currency, exchange_rate)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (t_date, t_whose, t_broker, t_acc_num, t_acc_type, t_item, t_ticker, t_price, t_qty, t_price, t_curr, ex_r))
                        conn.commit()
                        export_backup_json()
                        st.success(f"🎉 '{t_item}' 신규 매수가 성공적으로 반영되었습니다!")
                        st.rerun()
                    finally:
                        conn.close()

    else:
        if df.empty:
            st.warning("등록된 보유 포트폴리오 항목이 없습니다.")
            return

        df["record_date"] = df["record_date"].fillna("미지정").replace("", "미지정")
        df["broker"] = df["broker"].fillna("미지정").replace("", "미지정")
        df["account_num"] = df["account_num"].fillna("미지정").replace("", "미지정")
        df["item_name"] = df["item_name"].fillna("미지정").replace("", "미지정")
        df["whose"] = df["whose"].fillna("본인").replace("", "본인")

        st.markdown("#### 🎯 거래 대상 종목 선택 (기준날짜 ➔ 증권사 ➔ 계좌번호 ➔ 종목)")

        available_dates = sorted(df["record_date"].unique(), reverse=True)
        selected_date = st.selectbox("1️⃣ 기준날짜 선택", available_dates, key="tr_sel_date")

        df_filtered_date = df[df["record_date"] == selected_date]
        available_brokers = sorted(df_filtered_date["broker"].unique())
        selected_broker = st.selectbox("2️⃣ 증권사 선택", available_brokers, key="tr_sel_broker")

        df_filtered_broker = df_filtered_date[df_filtered_date["broker"] == selected_broker]
        available_accounts = sorted(df_filtered_broker["account_num"].unique())
        selected_account = st.selectbox("3️⃣ 계좌번호 선택", available_accounts, key="tr_sel_acc")

        df_filtered_account = df_filtered_broker[df_filtered_broker["account_num"] == selected_account].copy()

        df_filtered_account["item_label"] = df_filtered_account.apply(
            lambda r: f"[{r['id']}] {r['item_name']} ({r['ticker'] if r['ticker'] else '티커없음'}) | 소유: {r['whose']} | 수량: {r['quantity']:,.2f}개 | 평단가: {r['buy_price']:,.2f}",
            axis=1
        )

        selected_item_label = st.selectbox("4️⃣ 종목 선택", df_filtered_account["item_label"].tolist(), key="tr_sel_item")

        target_row = df_filtered_account[df_filtered_account["item_label"] == selected_item_label].iloc[0]

        target_id = int(target_row["id"])
        old_qty = float(target_row["quantity"])
        old_buy_price = float(target_row["buy_price"])
        item_name = target_row["item_name"]

        st.markdown("---")
        st.markdown(f"📌 **선택 종목 정보**: `{item_name}` | **기존 수량**: `{old_qty:,.2f}` | **기존 평단가**: `{old_buy_price:,.2f}`")

        with st.form("dialog_trade_edit_form"):
            t_date = st.date_input("거래일자", datetime.now()).strftime("%Y-%m-%d")
            c1, c2 = st.columns(2)
            with c1:
                trade_price = st.number_input("거래 단가", min_value=0.0, step=1.0)
            with c2:
                trade_qty = st.number_input("거래 수량", min_value=0.0, step=1.0)

            btn_label = "➕ 추가 매수(평단가 가중 재계산) 반영" if "추가 매수" in trade_type else "➖ 매도(수량 감축/삭제) 반영"
            submitted = st.form_submit_button(btn_label)

            if submitted:
                if trade_price <= 0 or trade_qty <= 0:
                    st.error("단가와 수량을 0보다 크게 입력해 주세요.")
                elif "기존 종목 매도" in trade_type and trade_qty > old_qty:
                    st.error(f"매도 수량({trade_qty:,.2f})이 현재 보유 수량({old_qty:,.2f})보다 많을 수 없습니다.")
                else:
                    conn = get_connection()
                    try:
                        cursor = conn.cursor()

                        if "추가 매수" in trade_type:
                            new_qty = old_qty + trade_qty
                            new_buy_price = ((old_qty * old_buy_price) + (trade_qty * trade_price)) / new_qty
                            cursor.execute("""
                                UPDATE portfolio 
                                SET quantity = ?, buy_price = ?, record_date = ? 
                                WHERE id = ?
                            """, (new_qty, new_buy_price, t_date, target_id))
                            st.success(f"🎉 '{item_name}' 추가 매수가 완료되었습니다! (신규 수량: {new_qty:,.2f}, 신규 평단가: {new_buy_price:,.2f})")

                        else:
                            if trade_qty == old_qty:
                                cursor.execute("DELETE FROM portfolio WHERE id = ?", (target_id,))
                                realized_profit = (trade_price - old_buy_price) * old_qty
                                st.success(f"🎉 '{item_name}' 전량 매도가 완료되어 해당 항목이 포트폴리오에서 삭제되었습니다. (실현손익: {realized_profit:+,.0f})")
                            else:
                                new_qty = old_qty - trade_qty
                                cursor.execute("""
                                    UPDATE portfolio 
                                    SET quantity = ?, record_date = ? 
                                    WHERE id = ?
                                """, (new_qty, t_date, target_id))
                                realized_profit = (trade_price - old_buy_price) * trade_qty
                                st.success(f"🎉 '{item_name}' 부분 매도가 완료되었습니다! (잔여 수량: {new_qty:,.2f}, 실현손익: {realized_profit:+,.0f})")

                        conn.commit()
                        export_backup_json()
                        st.rerun()
                    finally:
                        conn.close()


init_db()

st.markdown(
    """
    <style>
    [data-testid="stMetricValue"] { font-size: 1.4rem !important; }
    [data-testid="stMetricLabel"] { font-size: 0.85rem !important; }
    </style>
""",
    unsafe_allow_html=True,
)

st.title("📈 나만의 주식/자산 관리 프로그램")

# ---------------------------------------------------------
# 좌측 사이드바 설정
# ---------------------------------------------------------
st.sidebar.header("💱 환율 정보")
if "live_rate_store" not in st.session_state:
    st.session_state.live_rate_store = get_exchange_rate()

current_rate = st.sidebar.number_input(
    "현재 원/달러 환율 (KRW/USD)", value=st.session_state.live_rate_store, step=1.0
)

st.sidebar.header("⚙️ 시세 수집 설정")
regular_market_only = st.sidebar.checkbox(
    "정규장만 반영",
    value=st.session_state.get("regular_market_only", False),
    key="regular_market_only",
    help="체크 시 미국 주식 등의 프리마켓/애프터마켓 시세를 제외하고 정규장 시세만 반영합니다."
)

if st.sidebar.button("🔄 시세 캐시 초기화 & 갱신"):
    st.cache_data.clear()
    st.session_state.live_rate_store = fetch_live_exchange_rate()
    st.sidebar.success("시세 캐시가 초기화되고 최신 환율이 반영되었습니다!")
    st.rerun()

if st.sidebar.button("⚡ 실시간 환율 및 전체 최신 시세 일괄 업데이트"):
    with st.spinner("실시간 환율 조회 및 전체 종목 최신 시세 업데이트 중..."):
        st.cache_data.clear()
        target_rate = fetch_live_exchange_rate()
        st.session_state.live_rate_store = target_rate
        cnt = update_all_prices_and_rate_batch(target_rate, regular_only=regular_market_only)
        st.sidebar.success(f"업데이트 완료! (적용 환율: {target_rate}원 / 총 {cnt}개 항목 최신화)")
        st.rerun()

menu = st.sidebar.selectbox(
    "메뉴 선택",
    [
        "자산 입력 및 관리",
        "일별/시점별 보유 현황 분석",
        "💾 데이터 백업 및 복구",
    ],
)

# ---------------------------------------------------------
# 메뉴 1: 자산 입력 및 관리
# ---------------------------------------------------------
if menu == "자산 입력 및 관리":
    st.header("📝 자산 데이터 입력 & 수정")
    conn = get_connection()
    try:
        df_raw = pd.read_sql(
            "SELECT * FROM portfolio ORDER BY record_date DESC, id DESC", conn
        )
    finally:
        conn.close()

    st.subheader("🔄 일괄 업데이트 설정")
    rate_option = st.radio(
        "환율 적용 기준 선택",
        ["사이드바 지정 환율 사용", "실시간 환율 API 조회하여 사용"],
        horizontal=True
    )

    if st.button("🔄 전체 데이터 최신 시세로 무조건 일괄 업데이트"):
        with st.spinner("배치 일괄 시세 및 환율을 반영 중..."):
            st.cache_data.clear()
            if "실시간" in rate_option:
                target_rate = fetch_live_exchange_rate()
                st.session_state.live_rate_store = target_rate
            else:
                target_rate = current_rate

            cnt = update_all_prices_and_rate_batch(target_rate, regular_only=regular_market_only)
            st.success(f"업데이트 완료! (적용된 환율: {target_rate}원 / 총 {cnt}개 항목 최신화)")
            st.rerun()

    st.markdown("---")
    mode = st.radio(
        "작업 선택",
        [
            "🖥️ 웹 화면 직접 수정/편집 (추천)",
            "신규 데이터 개별 추가",
            "엑셀 파일로 일괄 추가",
            "🗑 데이터 삭제 관리",
        ],
        horizontal=True,
    )

    if mode == "🖥️ 웹 화면 직접 수정/편집 (추천)":
        st.subheader("🖥 웹 스프레드시트 편집기 (직접 수정/행 추가/선택 삭제)")
        
        if st.button("⚡ 매매(트레이딩) 입력 다이얼로그 열기", type="primary"):
            open_trading_dialog()

        st.info("💡 **사용 방법**: 아래 표에서 셀을 직접 수정하거나, 체크박스로 삭제할 행을 선택하고, 하단 버튼으로 저장 및 삭제를 수행할 수 있습니다.")

        required_cols = [
            "record_date", "whose", "broker", "account_num", "account_type", "item_name",
            "ticker", "category1", "category2", "category3", "category4",
            "buy_price", "quantity", "current_price", "currency", "exchange_rate"
        ]

        if not df_raw.empty:
            edit_df = df_raw.copy()
        else:
            edit_df = pd.DataFrame(columns=["id"] + required_cols)

        if "select_all_flag" not in st.session_state:
            st.session_state["select_all_flag"] = False

        col_select_all1, col_select_all2, _ = st.columns([1.5, 1.5, 5])
        with col_select_all1:
            if st.button("✅ 전체 선택"):
                st.session_state["select_all_flag"] = True
                st.rerun()
        with col_select_all2:
            if st.button("⬜ 전체 해제"):
                st.session_state["select_all_flag"] = False
                st.rerun()

        default_select_val = st.session_state.get("select_all_flag", False)
        edit_df.insert(0, "선택(삭제)", default_select_val)

        edited_data = st.data_editor(
            edit_df,
            num_rows="dynamic",
            use_container_width=True,
            key="web_data_editor",
            column_config={
                "선택(삭제)": st.column_config.CheckboxColumn("선택(삭제)", help="삭제할 줄을 체크하세요"),
                "id": st.column_config.NumberColumn("ID", disabled=True),
                "record_date": st.column_config.TextColumn("기준 날짜"),
                "whose": st.column_config.TextColumn("소유자(WHOSE)"),
                "currency": st.column_config.SelectboxColumn("통화", options=["KRW", "USD"]),
            },
            hide_index=True
        )

        col_ed1, col_ed2 = st.columns([1, 1])

        with col_ed1:
            if st.button("🗑️ 선택한 행(체크박스) 일괄 삭제"):
                selected_to_delete = edited_data[edited_data["선택(삭제)"] == True]
                if not selected_to_delete.empty:
                    ids_to_delete = selected_to_delete["id"].dropna().tolist()
                    if ids_to_delete:
                        conn = get_connection()
                        try:
                            cursor = conn.cursor()
                            cursor.executemany("DELETE FROM portfolio WHERE id = ?", [(i,) for i in ids_to_delete])
                            conn.commit()
                            export_backup_json()
                            st.session_state["select_all_flag"] = False
                            st.success(f"선택한 {len(ids_to_delete)}개 항목이 성공적으로 삭제되었습니다!")
                            st.rerun()
                        finally:
                            conn.close()
                    else:
                        st.warning("새로 입력되어 ID가 없는 행은 저장 시 반영되지 않습니다.")
                else:
                    st.warning("삭제할 행의 '선택(삭제)' 체크박스를 지정해 주세요.")

        with col_ed2:
            if st.button("💾 표 수정 및 변경사항 DB에 일괄 저장"):
                save_df = edited_data.drop(columns=["선택(삭제)", "id"], errors="ignore")
                
                # 빈 행 및 유령 행 필터링 (보유항목명이 없는 행은 저장하지 않음)
                if "item_name" in save_df.columns:
                    save_df = save_df[save_df["item_name"].notna() & (save_df["item_name"].astype(str).str.strip() != "")]

                for col in required_cols:
                    if col not in save_df.columns:
                        save_df[col] = None

                save_df["record_date"] = save_df["record_date"].fillna(datetime.now().strftime("%Y-%m-%d"))
                save_df["whose"] = save_df["whose"].fillna("본인")
                save_df["currency"] = save_df["currency"].fillna("KRW")
                save_df["exchange_rate"] = pd.to_numeric(save_df["exchange_rate"], errors="coerce").fillna(1.0)
                save_df["buy_price"] = pd.to_numeric(save_df["buy_price"], errors="coerce").fillna(0.0)
                save_df["quantity"] = pd.to_numeric(save_df["quantity"], errors="coerce").fillna(0.0)
                save_df["current_price"] = pd.to_numeric(save_df["current_price"], errors="coerce").fillna(0.0)

                conn = get_connection()
                try:
                    cursor = conn.cursor()
                    cursor.execute("DELETE FROM portfolio")
                    save_df[required_cols].to_sql("portfolio", conn, if_exists="append", index=False)
                    conn.commit()
                    export_backup_json()
                    st.session_state["select_all_flag"] = False
                    st.success("🎉 표 전체 변경사항이 성공적으로 저장되었습니다!")
                    st.rerun()
                except Exception as e:
                    conn.rollback()
                    st.error(f"저장 중 오류가 발생하여 롤백되었습니다: {e}")
                finally:
                    conn.close()

    elif mode == "신규 데이터 개별 추가":
        currency = st.selectbox("통화 단위 선택", ["KRW (원화)", "USD (달러)"])
        is_usd = "USD" in currency
        default_ex_rate = current_rate if is_usd else 1.0

        with st.form("insert_form", clear_on_submit=True):
            st.subheader(f"새로운 자산 기록 추가 ({currency})")
            col1, col2, col3, col4 = st.columns(4)
            with col1:
                record_date = st.date_input("기준 날짜").strftime("%Y-%m-%d")
                whose = st.text_input("소유자 (WHOSE)", value="본인")
                broker = st.text_input("금융사")
                account_num = st.text_input("계좌번호")
                account_type = st.selectbox(
                    "구분", ["연금", "일반", "ISA", "IRP", "기타"]
                )
            with col2:
                item_name = st.text_input("보유항목")
                ticker = st.text_input("종목코드/티커")
                ex_rate = st.number_input(
                    "적용 환율", value=default_ex_rate, disabled=not is_usd
                )
            with col3:
                category1 = st.text_input("분류1")
                category2 = st.text_input("분류2")
                category3 = st.text_input("분류3")
                category4 = st.text_input("분류4")
            with col4:
                buy_price = st.number_input("매입단가", min_value=0.0)
                quantity = st.number_input("개수(수량)", min_value=0.0)
                current_price = st.number_input(
                    "현재가 [0=자동수집]", min_value=0.0
                )

            submitted = st.form_submit_button("저장하기")
            if submitted:
                curr_code = "USD" if is_usd else "KRW"
                final_rate = ex_rate if is_usd else 1.0
                if current_price == 0.0 and ticker:
                    f_ticker = normalize_ticker(ticker, curr_code)
                    fetched = fetch_live_ticker_price(f_ticker, regular_only=regular_market_only)
                    if fetched:
                        current_price = fetched

                conn = get_connection()
                try:
                    cursor = conn.cursor()
                    cursor.execute(
                        """
                        INSERT INTO portfolio (record_date, whose, broker, account_num, account_type, item_name, ticker, category1, category2, category3, category4, buy_price, quantity, current_price, currency, exchange_rate)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                        (
                            record_date,
                            whose,
                            broker,
                            account_num,
                            account_type,
                            item_name,
                            ticker,
                            category1,
                            category2,
                            category3,
                            category4,
                            buy_price,
                            quantity,
                            current_price,
                            curr_code,
                            final_rate,
                        ),
                    )
                    conn.commit()
                    export_backup_json()
                    st.success("저장되었습니다.")
                    st.rerun()
                finally:
                    conn.close()

    elif mode == "엑셀 파일로 일괄 추가":
        st.subheader("📁 엑셀 / CSV 파일 업로드")
        uploaded_file = st.file_uploader("파일 선택", type=["xlsx", "csv"])
        if uploaded_file is not None:
            try:
                upload_df = (
                    pd.read_csv(uploaded_file)
                    if uploaded_file.name.endswith(".csv")
                    else pd.read_excel(uploaded_file)
                )
                
                required_cols = [
                    "record_date",
                    "whose",
                    "broker",
                    "account_num",
                    "account_type",
                    "item_name",
                    "ticker",
                    "category1",
                    "category2",
                    "category3",
                    "category4",
                    "buy_price",
                    "quantity",
                    "current_price",
                    "currency",
                    "exchange_rate",
                ]

                if "whose" not in upload_df.columns:
                    upload_df["whose"] = "본인"

                for col in required_cols:
                    if col not in upload_df.columns:
                        upload_df[col] = None

                upload_df["record_date"] = pd.to_datetime(
                    upload_df["record_date"]
                ).dt.strftime("%Y-%m-%d")
                upload_df["currency"] = upload_df["currency"].fillna("KRW")
                upload_df["exchange_rate"] = pd.to_numeric(upload_df["exchange_rate"], errors="coerce").fillna(1.0)
                upload_df["whose"] = upload_df["whose"].fillna("본인")
                upload_df["buy_price"] = pd.to_numeric(upload_df["buy_price"], errors="coerce").fillna(0.0)
                upload_df["quantity"] = pd.to_numeric(upload_df["quantity"], errors="coerce").fillna(0.0)
                upload_df["current_price"] = pd.to_numeric(upload_df["current_price"], errors="coerce").fillna(0.0)

                # 빈 행 필터링
                upload_df = upload_df[upload_df["item_name"].notna() & (upload_df["item_name"].astype(str).str.strip() != "")]

                st.dataframe(upload_df[required_cols], width="stretch")

                if st.button("DB에 일괄 저장하기"):
                    conn = get_connection()
                    try:
                        upload_df[required_cols].to_sql(
                            "portfolio", conn, if_exists="append", index=False
                        )
                        conn.commit()
                        export_backup_json()
                        st.success("WHOSE 포함 일괄 저장 완료!")
                        st.rerun()
                    finally:
                        conn.close()
            except Exception as e:
                st.error(f"오류: {e}")

    elif mode == "🗑 데이터 삭제 관리":
        if not df_raw.empty:
            st.subheader("🗑️ 데이터 삭제 관리 (체크박스 다중 선택 삭제)")
            st.caption("삭제를 원하시는 데이터 행의 **'삭제 선택'** 체크박스를 클릭한 후 아래 삭제 버튼을 눌러주세요.")

            del_df = df_raw.copy()
            del_df.insert(0, "삭제 선택", False)

            del_edited = st.data_editor(
                del_df,
                key="delete_management_editor",
                use_container_width=True,
                column_config={
                    "삭제 선택": st.column_config.CheckboxColumn("삭제 선택", help="이 행을 삭제하려면 체크하세요"),
                    "id": st.column_config.NumberColumn("ID", disabled=True),
                },
                disabled=[col for col in del_df.columns if col != "삭제 선택"],
                hide_index=True
            )

            if st.button("❌ 체크된 선택 항목 삭제 실행", type="primary"):
                selected_del_df = del_edited[del_edited["삭제 선택"] == True]
                if not selected_del_df.empty:
                    ids_to_delete = selected_del_df["id"].tolist()
                    conn = get_connection()
                    try:
                        cursor = conn.cursor()
                        cursor.executemany("DELETE FROM portfolio WHERE id = ?", [(i,) for i in ids_to_delete])
                        conn.commit()
                        export_backup_json()
                        st.success(f"총 {len(ids_to_delete)}개 항목이 성공적으로 삭제되었습니다!")
                        st.rerun()
                    finally:
                        conn.close()
                else:
                    st.warning("삭제할 항목을 최소 1개 이상 체크해 주세요.")
        else:
            st.info("삭제할 데이터가 없습니다.")

    st.markdown("---")
    st.dataframe(df_raw, width="stretch")

# ---------------------------------------------------------
# 메뉴 2: 일별/시점별 보유 현황 분석
# ---------------------------------------------------------
elif menu == "일별/시점별 보유 현황 분석":
    st.header("🔍 시점별 자산 보유 현황")
    conn = get_connection()
    try:
        df = pd.read_sql("SELECT * FROM portfolio", conn)
    finally:
        conn.close()

    if df.empty:
        st.info("데이터가 없습니다.")
    else:
        df["whose"] = df["whose"].fillna("본인").replace("", "본인")
        available_whose_list = sorted(df["whose"].unique())
        whose_date_map = {
            w: sorted(df[df["whose"] == w]["record_date"].unique(), reverse=True)
            for w in available_whose_list
        }

        pending_preset = st.session_state.pop("_treemap_preset_pending", None)
        if pending_preset:
            apply_treemap_preset(pending_preset, available_whose_list, whose_date_map)

        if "treemap_sel_whose" not in st.session_state:
            st.session_state["treemap_sel_whose"] = available_whose_list
        valid_whose = [
            w
            for w in st.session_state.get("treemap_sel_whose", available_whose_list)
            if w in available_whose_list
        ]
        st.session_state["treemap_sel_whose"] = (
            valid_whose if valid_whose else list(available_whose_list)
        )

        col_filter1, col_filter2, col_filter3 = st.columns([2, 2, 2])

        with col_filter1:
            selected_whose_list = st.multiselect(
                "1. 소유자(WHOSE) 선택 (복수 선택 가능)",
                options=available_whose_list,
                key="treemap_sel_whose",
            )

        if not selected_whose_list:
            st.warning("소유자(WHOSE)를 최소 1개 이상 선택해 주세요.")
        else:
            owner_selected_dates = {}
            with col_filter2:
                st.write("2. 소유자별 입력 데이터 날짜 선택")
                for w in selected_whose_list:
                    w_dates = whose_date_map.get(w, [])
                    if w_dates:
                        if st.session_state.get(f"select_date_{w}") not in w_dates:
                            st.session_state[f"select_date_{w}"] = w_dates[0]
                        owner_selected_dates[w] = st.selectbox(
                            f"[{w}] 기준 날짜",
                            options=w_dates,
                            key=f"select_date_{w}"
                        )

            with col_filter3:
                use_historical_price = st.checkbox(
                    "🗓️ 특정 날짜 기준 과거 시세로 조회하기",
                    key="treemap_use_hist",
                )
                if use_historical_price:
                    target_eval_date = st.date_input(
                        "조회 기준 시세 날짜",
                        datetime.now(),
                        key="treemap_hist_date",
                    ).strftime("%Y-%m-%d")
                else:
                    target_eval_date = None

            sub_dfs = []
            for w, d in owner_selected_dates.items():
                w_sub = df[(df["whose"] == w) & (df["record_date"] == d)].copy()
                sub_dfs.append(w_sub)

            if sub_dfs:
                sub_df = pd.concat(sub_dfs, ignore_index=True)
            else:
                sub_df = pd.DataFrame(columns=df.columns)

            sub_df["formatted_ticker"] = sub_df.apply(
                lambda r: normalize_ticker(r["ticker"], r["currency"]), axis=1
            )
            tickers = tuple(sub_df["formatted_ticker"].dropna().unique().tolist())

            if use_historical_price and target_eval_date:
                with st.spinner(f"[{target_eval_date}] 배치 시세 데이터를 조회 중..."):
                    target_dt = pd.to_datetime(target_eval_date)
                    start_dt_str = (target_dt - timedelta(days=7)).strftime("%Y-%m-%d")
                    end_dt_str = (target_dt + timedelta(days=2)).strftime("%Y-%m-%d")
                    
                    m_data = fetch_batch_market_data(tickers, start_dt_str, end_dt_str, regular_only=regular_market_only)

                    # 티커별 가격 맵을 생성하여 O(N) 순회 최적화
                    hist_price_map = {}
                    for tk in tickers:
                        hist_price_map[tk] = get_price_from_batch_data(m_data, tk, target_eval_date, regular_only=regular_market_only)

                    for idx, row in sub_df.iterrows():
                        tk = row["formatted_ticker"]
                        if tk in hist_price_map and hist_price_map[tk] is not None:
                            sub_df.at[idx, "current_price"] = hist_price_map[tk]

            sub_df["rate_multiplier"] = sub_df.apply(
                lambda r: r["exchange_rate"] if r["currency"] == "USD" else 1.0,
                axis=1,
            )
            sub_df["매입총액(원)"] = (
                sub_df["buy_price"]
                * sub_df["quantity"]
                * sub_df["rate_multiplier"]
            )
            sub_df["평가액(원)"] = (
                sub_df["current_price"]
                * sub_df["quantity"]
                * sub_df["rate_multiplier"]
            )
            sub_df["평가손익(원)"] = sub_df["평가액(원)"] - sub_df["매입총액(원)"]

            for cat in [
                "whose",
                "category1",
                "category2",
                "category3",
                "category4",
                "account_type",
                "broker",
                "account_num",
                "item_name",
            ]:
                sub_df[cat] = sub_df[cat].fillna("미지정").replace("", "미지정")

            total_buy = sub_df["매입총액(원)"].sum()
            total_eval = sub_df["평가액(원)"].sum()
            total_profit = sub_df["평가손익(원)"].sum()
            total_rate = (total_profit / total_buy * 100) if total_buy != 0 else 0

            col1, col2, col3, col4 = st.columns(4)
            col1.metric("총 평가액 (원화)", f"{total_eval:,.0f} 원")
            col2.metric("총 매입금액 (원화)", f"{total_buy:,.0f} 원")
            col3.metric("총 평가손익 (원화)", f"{total_profit:,.0f} 원")
            col4.metric("전체 수익률", f"{total_rate:.2f} %")

            st.markdown("---")

            st.subheader("🗺️ 포트폴리오 TREEMAP 분석 (최대 4단계 계층 선택)")
            cat_options = TREEMAP_CAT_OPTIONS
            cat_keys = list(cat_options.keys())
            l234_options = ["없음"] + cat_keys

            if "treemap_l1" not in st.session_state:
                st.session_state["treemap_l1"] = cat_keys[1]
            if "treemap_l2" not in st.session_state:
                st.session_state["treemap_l2"] = l234_options[1]
            if "treemap_l3" not in st.session_state:
                st.session_state["treemap_l3"] = l234_options[3]
            if "treemap_l4" not in st.session_state:
                st.session_state["treemap_l4"] = l234_options[4]
            if "treemap_color_option" not in st.session_state:
                st.session_state["treemap_color_option"] = TREEMAP_COLOR_OPTIONS[1]

            if st.session_state.get("treemap_l1") not in cat_keys:
                st.session_state["treemap_l1"] = cat_keys[1]
            if st.session_state.get("treemap_l2") not in l234_options:
                st.session_state["treemap_l2"] = l234_options[1]
            if st.session_state.get("treemap_l3") not in l234_options:
                st.session_state["treemap_l3"] = l234_options[3]
            if st.session_state.get("treemap_l4") not in l234_options:
                st.session_state["treemap_l4"] = l234_options[4]
            if st.session_state.get("treemap_color_option") not in TREEMAP_COLOR_OPTIONS:
                st.session_state["treemap_color_option"] = TREEMAP_COLOR_OPTIONS[1]

            col_t1, col_t2, col_t3, col_t4 = st.columns(4)
            with col_t1:
                l1 = st.selectbox("1단계 (최상위)", cat_keys, key="treemap_l1")
            with col_t2:
                l2 = st.selectbox("2단계", l234_options, key="treemap_l2")
            with col_t3:
                l3 = st.selectbox("3단계", l234_options, key="treemap_l3")
            with col_t4:
                l4 = st.selectbox("4단계 (최하위)", l234_options, key="treemap_l4")

            col_c1, col_c2 = st.columns([2, 1])
            with col_c1:
                color_option = st.selectbox(
                    "🗺 트리맵 색상 기준 선택",
                    TREEMAP_COLOR_OPTIONS,
                    key="treemap_color_option",
                )

            custom_base_date = None
            if "5)" in color_option:
                with col_c2:
                    custom_base_date = st.date_input(
                        "기준 날짜 선택",
                        value=datetime.now() - timedelta(days=30),
                        max_value=datetime.now(),
                        key="treemap_custom_base_date",
                    )

            preset_names = list_treemap_presets()
            st.markdown("##### 💾 분석 조건 저장 / 불러오기")
            p_col1, p_col2, p_col3, p_col4 = st.columns([2.2, 1, 2.2, 1.6])
            with p_col1:
                save_name = st.text_input(
                    "저장할 조건 이름",
                    placeholder="예: BJ_월간_계좌계층",
                    key="treemap_preset_save_name",
                )
            with p_col2:
                st.write("")
                st.write("")
                do_save = st.button("조건 저장", key="treemap_preset_save_btn")
            with p_col3:
                load_name = st.selectbox(
                    "저장된 조건 불러오기",
                    options=["(선택)"] + preset_names,
                    key="treemap_preset_load_name",
                )
            with p_col4:
                st.write("")
                st.write("")
                load_c, del_c = st.columns(2)
                with load_c:
                    do_load = st.button("불러오기", key="treemap_preset_load_btn")
                with del_c:
                    do_delete = st.button("삭제", key="treemap_preset_del_btn")

            if do_save:
                if not str(save_name).strip():
                    st.warning("저장할 조건 이름을 입력해 주세요.")
                else:
                    payload = collect_treemap_preset_payload(
                        selected_whose_list,
                        owner_selected_dates,
                        use_historical_price,
                        target_eval_date,
                        l1,
                        l2,
                        l3,
                        l4,
                        color_option,
                        custom_base_date,
                        st.session_state.get(
                            "treemap_drilldown_selector", "🌐 전체 (Root - 100% 점유)"
                        ),
                    )
                    saved = save_treemap_preset(save_name, payload)
                    if saved:
                        export_backup_json()
                        st.success(f"분석 조건을 저장했습니다: {saved}")
                        st.rerun()
                    else:
                        st.error("조건 이름에 사용할 수 없는 문자가 있습니다.")

            if do_load:
                if load_name == "(선택)":
                    st.warning("불러올 조건을 선택해 주세요.")
                else:
                    loaded = load_treemap_preset(load_name)
                    if not loaded:
                        st.error("조건 파일을 찾지 못했습니다.")
                    else:
                        st.session_state["_treemap_preset_pending"] = loaded
                        st.success(f"조건을 불러옵니다: {load_name}")
                        st.rerun()

            if do_delete:
                if load_name == "(선택)":
                    st.warning("삭제할 조건을 선택해 주세요.")
                elif delete_treemap_preset(load_name):
                    export_backup_json()
                    st.success(f"조건을 삭제했습니다: {load_name}")
                    st.rerun()
                else:
                    st.error("조건 파일을 찾지 못했습니다.")

            today = datetime.now()
            change_rates = []
            period_profits = []

            if color_option == "총 누적 수익률 (%)":
                color_col = "수익률(%)"
                for _, row in sub_df.iterrows():
                    buy_val = row["매입총액(원)"]
                    profit_val = row["평가손익(원)"]
                    rate_val = (profit_val / buy_val * 100) if buy_val != 0 else 0.0
                    change_rates.append(rate_val)
                    period_profits.append(profit_val)
                sub_df[color_col] = change_rates
                sub_df["선택기준_평가손익(원)"] = period_profits
            else:
                if "1) 일간" in color_option:
                    start_fetch_dt = (today - timedelta(days=10)).strftime("%Y-%m-%d")
                    target_base_dt = today - timedelta(days=1)
                elif "2) 주간" in color_option:
                    start_fetch_dt = (today - timedelta(days=20)).strftime("%Y-%m-%d")
                    target_base_dt = today - timedelta(days=7)
                elif "3) 월간" in color_option:
                    start_fetch_dt = (today - timedelta(days=50)).strftime("%Y-%m-%d")
                    first_day_of_this_month = today.replace(day=1)
                    target_base_dt = first_day_of_this_month - timedelta(days=1)
                elif "4) 연간" in color_option:
                    start_fetch_dt = (today - timedelta(days=385)).strftime("%Y-%m-%d")
                    target_base_dt = datetime(today.year - 1, 12, 31)
                elif "5) 특정" in color_option and custom_base_date:
                    start_fetch_dt = (custom_base_date - timedelta(days=10)).strftime("%Y-%m-%d")
                    target_base_dt = datetime.combine(custom_base_date, datetime.min.time())
                else:
                    start_fetch_dt = (today - timedelta(days=10)).strftime("%Y-%m-%d")
                    target_base_dt = today - timedelta(days=1)

                end_fetch_dt = (today + timedelta(days=2)).strftime("%Y-%m-%d")

                with st.spinner(f"[{color_option}] 기준 종가 배치 계산 중..."):
                    m_data = fetch_batch_market_data(tickers, start_fetch_dt, end_fetch_dt, regular_only=regular_market_only)

                    # 티커별 기준가를 사전에 한 번만 계산 (N+1 반복 조회 방지)
                    ticker_base_prices = {}
                    for tk in tickers:
                        if tk:
                            ticker_base_prices[tk] = _get_last_trading_day_before(m_data, tk, target_base_dt)

                    for _, row in sub_df.iterrows():
                        f_ticker = row["formatted_ticker"]
                        curr_p = row["current_price"]
                        qty = row["quantity"]
                        ex_r = row["rate_multiplier"]
                        rate = 0.0
                        profit_amt = 0.0

                        if f_ticker and curr_p:
                            base_price = ticker_base_prices.get(f_ticker)
                            if base_price and float(base_price) > 0:
                                rate = round(((float(curr_p) - float(base_price)) / float(base_price)) * 100, 2)
                                profit_amt = (float(curr_p) - float(base_price)) * ex_r * qty

                        change_rates.append(rate)
                        period_profits.append(profit_amt)

                    sub_df[color_option] = change_rates
                    sub_df["선택기준_평가손익(원)"] = period_profits
                color_col = color_option

            selected_levels = [l1, l2, l3, l4]
            group_cols = []
            for lvl in selected_levels:
                if lvl != "없음":
                    c_name = cat_options[lvl]
                    if c_name not in group_cols:
                        group_cols.append(c_name)

            col_rename_map = {cat_options[k]: k for k in cat_options if cat_options[k] in group_cols}

            if "1) 일간" in color_option:
                profit_col_label = "평가손익 (일간)"
                rate_col_label = "등락율 (일간)"
            elif "2) 주간" in color_option:
                profit_col_label = "평가손익 (주간)"
                rate_col_label = "등락율 (주간)"
            elif "3) 월간" in color_option:
                profit_col_label = "평가손익 (월간)"
                rate_col_label = "등락율 (월간)"
            elif "4) 연간" in color_option:
                profit_col_label = "평가손익 (연간)"
                rate_col_label = "등락율 (연간)"
            elif "5) 특정" in color_option:
                profit_col_label = "평가손익 (지정일)"
                rate_col_label = "등락율 (지정일)"
            else:
                profit_col_label = "평가손익(원)"
                rate_col_label = "수익률(%)"

            path_options = ["🌐 전체 (Root - 100% 점유)"]
            
            if group_cols:
                path_set = set()
                for _, r in sub_df.iterrows():
                    curr_p = ""
                    for col in group_cols:
                        val = str(r[col])
                        curr_p = f"{curr_p} > {val}" if curr_p else val
                        path_set.add(curr_p)
                path_options.extend(sorted(list(path_set)))

            if st.session_state.get("treemap_drilldown_selector") not in path_options:
                st.session_state["treemap_drilldown_selector"] = path_options[0]

            col_drill1, col_drill2 = st.columns([2.5, 1.5])
            with col_drill1:
                selected_drill_path = st.selectbox(
                    "🔍 TREEMAP 계층 드릴다운 / 하위 분류 화면 선택 (상단 표 100% 점유 연동)",
                    options=path_options,
                    key="treemap_drilldown_selector"
                )
            with col_drill2:
                st.caption("💡 특정 하위 분류를 선택하면 해당 분류의 총액을 100% 점유율로 자동 계산하여 상단 표와 Treemap이 완벽하게 연동됩니다.")

            if selected_drill_path == "🌐 전체 (Root - 100% 점유)":
                filtered_df = sub_df.copy()
                view_root_label = "🌐 전체 총합"
                active_group_cols = group_cols
            else:
                path_parts = [p.strip() for p in selected_drill_path.split(">")]
                filter_mask = pd.Series(True, index=sub_df.index)
                for idx, part in enumerate(path_parts):
                    if idx < len(group_cols):
                        filter_mask &= (sub_df[group_cols[idx]].astype(str) == part)
                filtered_df = sub_df[filter_mask].copy()
                view_root_label = f"🎯 [{selected_drill_path}] 총합"
                active_group_cols = group_cols[len(path_parts):]

            active_total_eval = filtered_df["평가액(원)"].sum()

            st.write("📌 **현재 화면 기준 계층 요약 현황 표 (화면 점유율: 100.00% 기준)**")
            
            col_s1, col_s2 = st.columns([2, 1])
            with col_s1:
                sort_by_col = st.selectbox(
                    "📊 정렬 기준 항목 선택",
                    options=["평가액(원)", profit_col_label, rate_col_label, "점유율(%)", "구분 항목"],
                    index=0,
                    key="tree_sort_by_col"
                )
            with col_s2:
                sort_order = st.radio(
                    "정렬 순서",
                    options=["내림차순 ⬇️", "오름차순 ⬆️"],
                    horizontal=True,
                    key="tree_sort_order"
                )

            is_reverse = ("내림차순" in sort_order)

            def _tree_metrics(group_df):
                group_eval = group_df["평가액(원)"].sum()
                group_buy = group_df["매입총액(원)"].sum()
                group_profit = group_df["선택기준_평가손익(원)"].sum()
                if color_option == "총 누적 수익률 (%)":
                    group_rate = (group_profit / group_buy * 100) if group_buy != 0 else 0.0
                else:
                    past_eval = group_eval - group_profit
                    group_rate = (group_profit / past_eval * 100) if past_eval != 0 else 0.0
                group_share = (
                    (group_eval / active_total_eval * 100) if active_total_eval != 0 else 0
                )
                return group_eval, group_profit, group_rate, group_share

            def build_tree_nodes(df_sub, active_cols):
                if not active_cols:
                    return []
                curr_col = active_cols[0]
                rem_cols = active_cols[1:]
                nodes = []
                for name, group in df_sub.groupby(curr_col):
                    group_eval, group_profit, group_rate, group_share = _tree_metrics(group)
                    nodes.append({
                        "label": str(name),
                        "eval": group_eval,
                        "profit": group_profit,
                        "rate": group_rate,
                        "share": group_share,
                        "children": build_tree_nodes(group, rem_cols) if rem_cols else [],
                    })
                return nodes

            nested_nodes = build_tree_nodes(filtered_df, active_group_cols) if active_group_cols else []

            def sort_tree_nodes(nodes, target_sort_col, reverse_flag):
                key_map = {
                    "평가액(원)": lambda x: x["eval"],
                    profit_col_label: lambda x: x["profit"],
                    rate_col_label: lambda x: x["rate"],
                    "점유율(%)": lambda x: x["share"],
                    "구분 항목": lambda x: x["label"],
                }
                getter = key_map.get(target_sort_col, lambda x: x["eval"])
                nodes.sort(key=getter, reverse=reverse_flag)
                for node in nodes:
                    if node["children"]:
                        sort_tree_nodes(node["children"], target_sort_col, reverse_flag)

            sort_tree_nodes(nested_nodes, sort_by_col, is_reverse)

            flat_rows = []

            def flatten_tree_nodes(nodes, parent_id=None, depth=0):
                for idx, node in enumerate(nodes):
                    node_id = f"{parent_id}_{idx}" if parent_id else f"node_{idx}"
                    has_children = len(node["children"]) > 0
                    flat_rows.append({
                        "id": node_id,
                        "parent_id": parent_id,
                        "depth": depth,
                        "label": node["label"],
                        "eval": node["eval"],
                        "profit": node["profit"],
                        "rate": node["rate"],
                        "share": node["share"],
                        "has_children": has_children,
                    })
                    if has_children:
                        flatten_tree_nodes(node["children"], parent_id=node_id, depth=depth + 1)

            # 최상위 Root 요약 행 추가
            if color_option == "총 누적 수익률 (%)":
                active_profit = filtered_df["평가손익(원)"].sum()
                active_buy = filtered_df["매입총액(원)"].sum()
                active_rate = (active_profit / active_buy * 100) if active_buy != 0 else 0.0
            else:
                active_profit = filtered_df["선택기준_평가손익(원)"].sum()
                past_eval = active_total_eval - active_profit
                active_rate = (active_profit / past_eval * 100) if past_eval != 0 else 0.0

            root_row = {
                "id": "root_0",
                "parent_id": None,
                "depth": 0,
                "label": view_root_label,
                "eval": active_total_eval,
                "profit": active_profit,
                "rate": active_rate,
                "share": 100.0,
                "has_children": len(nested_nodes) > 0,
            }

            flat_rows.append(root_row)
            flatten_tree_nodes(nested_nodes, parent_id="root_0", depth=1)

            render_expandable_tree_table(flat_rows, profit_col_label, rate_col_label)

            st.markdown("---")

            # Treemap 시각화
            if not filtered_df.empty and active_group_cols:
                fig_df = filtered_df.copy()
                fig = px.treemap(
                    fig_df,
                    path=active_group_cols,
                    values="평가액(원)",
                    color=color_col,
                    color_continuous_scale="RdYlGn",
                    color_continuous_midpoint=0,
                    title=f"📊 Treemap 계층 분석 ({view_root_label})",
                )
                fig.update_traces(
                    texttemplate="<b>%{label}</b><br>평가액: ₩%{value:,.0f}<br>등락률: %{color:+.2f}%",
                    hovertemplate="<b>%{label}</b><br>평가액: ₩%{value:,.0f}<br>수익/등락률: %{color:+.2f}%<extra></extra>",
                )
                fig.update_layout(margin=dict(t=40, l=10, r=10, b=10), height=600)
                st.plotly_chart(fig, use_container_width=True)

# ---------------------------------------------------------
# 메뉴 3: 데이터 백업 및 복구
# ---------------------------------------------------------
elif menu == "💾 데이터 백업 및 복구":
    st.header("💾 데이터 백업 및 복구")

    col_b1, col_b2 = st.columns(2)

    with col_b1:
        st.subheader("📥 백업 데이터 다운로드")
        st.caption("현재 데이터베이스 내의 포트폴리오 데이터와 분석 조건(프리셋)을 통합 JSON 파일로 다운로드합니다.")
        json_str = export_backup_json()
        if json_str:
            st.download_button(
                label="💾 백업 JSON 파일 다운로드",
                data=json_str,
                file_name=f"portfolio_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
                mime="application/json",
            )
        else:
            st.error("백업 데이터를 생성하는 중 오류가 발생했습니다.")

    with col_b2:
        st.subheader("📤 백업 데이터 복원 (업로드)")
        st.caption("기존 백업 JSON 파일을 업로드하여 데이터를 복원합니다.")
        restore_mode = st.radio("복원 방식 선택", ["기존 데이터 덮어쓰기 (초기화 후 복원)", "기존 데이터에 추가하기"])
        uploaded_backup = st.file_uploader("백업 JSON 파일 업로드", type=["json"])

        if uploaded_backup is not None:
            if st.button("🚀 백업 복원 실행"):
                replace_flag = "덮어쓰기" in restore_mode
                content = uploaded_backup.read()
                count = import_backup_json(content, replace=replace_flag)
                if count > 0:
                    st.success(f"🎉 성공적으로 {count}건의 데이터가 복원되었습니다!")
                    st.rerun()
                else:
                    st.error("복원에 실패했습니다. 파일 형식을 확인해 주세요.")