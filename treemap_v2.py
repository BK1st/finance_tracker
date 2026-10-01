import io
import json
import os
import sqlite3
import urllib.request
from datetime import datetime, timedelta
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
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


def get_connection():
    return sqlite3.connect(DB_FILE)


def export_backup_json():
    """DB 내의 데이터를 JSON 백업 파일로 자동 저장 (whose 포함)"""
    try:
        conn = get_connection()
        df = pd.read_sql("""
            SELECT record_date, whose, broker, account_num, account_type, item_name, ticker,
                   category1, category2, category3, category4, buy_price, quantity,
                   current_price, currency, exchange_rate
            FROM portfolio
        """, conn)
        conn.close()
        
        records = df.to_dict(orient="records")
        json_bytes = json.dumps(records, ensure_ascii=False, indent=2)
        with open(BACKUP_FILE, "w", encoding="utf-8") as f:
            f.write(json_bytes)
        return json_bytes
    except Exception as e:
        return None


def import_backup_json(json_content, replace=True):
    """JSON 백업 데이터를 DB로 복원 (whose 포함)"""
    try:
        if isinstance(json_content, bytes):
            json_content = json_content.decode("utf-8")
        data = json.loads(json_content)
        if not data:
            return 0
        df = pd.DataFrame(data)
        required_cols = [
            "record_date", "whose", "broker", "account_num", "account_type", "item_name",
            "ticker", "category1", "category2", "category3", "category4",
            "buy_price", "quantity", "current_price", "currency", "exchange_rate"
        ]
        for col in required_cols:
            if col not in df.columns:
                df[col] = None
        df["currency"] = df["currency"].fillna("KRW")
        df["exchange_rate"] = df["exchange_rate"].fillna(1.0)
        df["whose"] = df["whose"].fillna("본인")
        
        conn = get_connection()
        cursor = conn.cursor()
        if replace:
            cursor.execute("DELETE FROM portfolio")
        
        df[required_cols].to_sql("portfolio", conn, if_exists="append", index=False)
        conn.commit()
        conn.close()
        
        export_backup_json()
        return len(df)
    except Exception as e:
        return 0


def init_db():
    """DB 초기화 및 whose 컬럼 마이그레이션 적용"""
    conn = get_connection()
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
    conn.close()

    if count == 0 and os.path.exists(BACKUP_FILE):
        try:
            with open(BACKUP_FILE, "r", encoding="utf-8") as f:
                content = f.read()
            import_backup_json(content, replace=False)
        except Exception:
            pass


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


def fetch_live_ticker_price(ticker):
    """단일 티커의 최신 시세 가져오기"""
    if not ticker:
        return None
    try:
        tk = yf.Ticker(ticker)
        fast_info = tk.fast_info
        
        post_price = fast_info.get("postMarketPrice")
        if post_price and not pd.isna(post_price):
            return round(float(post_price), 2)
            
        last_price = fast_info.get("lastPrice")
        if last_price and not pd.isna(last_price):
            return round(float(last_price), 2)
            
        prev_close = fast_info.get("previousClose")
        if prev_close and not pd.isna(prev_close):
            return round(float(prev_close), 2)

        hist = tk.history(period="5d")
        if not hist.empty:
            return round(float(hist["Close"].iloc[-1]), 2)
    except Exception:
        pass
    return None


@st.cache_data(ttl=300)
def fetch_batch_market_data(ticker_tuple, start_date_str, end_date_str):
    """모든 종목의 시세를 yf.download로 요청하여 캐싱"""
    tickers = [t for t in ticker_tuple if t]
    if not tickers:
        return pd.DataFrame()

    ticker_str = " ".join(list(set(tickers)))
    try:
        data = yf.download(
            ticker_str,
            start=start_date_str,
            end=end_date_str,
            group_by="ticker",
            auto_adjust=True,
            progress=False,
        )
        return data
    except Exception:
        return pd.DataFrame()


def get_price_from_batch_data(market_data, ticker, target_date_str):
    """배치 수집된 데이터프레임에서 특정 티커 및 날짜의 종가 추출"""
    if not ticker:
        return None

# 수정 코드
today_dt = pd.to_datetime(datetime.now().strftime("%Y-%m-%d"))
target_dt = pd.to_datetime(target_date_str)

# 지정한 날짜가 오늘이거나 미래인 경우에만 실시간 시세 사용
if target_dt >= today_dt:
    live_p = fetch_live_ticker_price(ticker)
    if live_p is not None:
        return live_p

    if market_data.empty:
        return fetch_live_ticker_price(ticker)

    try:
        df_ticker = market_data
        if isinstance(market_data.columns, pd.MultiIndex):
            if ticker in market_data.columns.levels[0]:
                df_ticker = market_data[ticker]
            elif ticker in market_data.columns.levels[1]:
                df_ticker = market_data.xs(ticker, axis=1, level=1)
            else:
                return fetch_live_ticker_price(ticker)

        if hasattr(df_ticker.index, "tz") and df_ticker.index.tz is not None:
            df_ticker.index = df_ticker.index.tz_localize(None)

        if "Close" in df_ticker.columns:
            series = df_ticker["Close"]
        else:
            series = df_ticker

        if series.empty:
            return fetch_live_ticker_price(ticker)

        valid_series = series[series.index <= target_dt].dropna()
        if not valid_series.empty:
            return round(float(valid_series.iloc[-1]), 2)
    except Exception:
        pass

    return fetch_live_ticker_price(ticker)


def update_all_prices_and_rate_batch(curr_rate):
    """배치 수집 방식으로 전체 시세 및 환율 일괄 업데이트"""
    conn = get_connection()
    df = pd.read_sql("SELECT id, ticker, currency FROM portfolio", conn)
    
    if df.empty:
        conn.close()
        return 0

    df["formatted_ticker"] = df.apply(
        lambda r: normalize_ticker(r["ticker"], r["currency"]), axis=1
    )
    valid_tickers = tuple(df["formatted_ticker"].dropna().unique().tolist())

    today = datetime.now()
    start_date_str = (today - timedelta(days=10)).strftime("%Y-%m-%d")
    end_date_str = (today + timedelta(days=2)).strftime("%Y-%m-%d")

    market_data = fetch_batch_market_data(valid_tickers, start_date_str, end_date_str)

    cursor = conn.cursor()
    updated_count = 0
    
    for _, row in df.iterrows():
        p_id = row["id"]
        curr = row["currency"]
        f_ticker = row["formatted_ticker"]
        ex_rate = curr_rate if curr == "USD" else 1.0

        new_price = get_price_from_batch_data(market_data, f_ticker, today.strftime("%Y-%m-%d"))

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
    conn.close()
    
    export_backup_json()
    return updated_count


# ---------------------------------------------------------
# 매매(트레이딩) 전용 다이얼로그
# ---------------------------------------------------------
@st.dialog("📈 매매(트레이딩) 입력 - 신규 매수 / 추가 매수 / 매도")
def open_trading_dialog():
    conn = get_connection()
    df = pd.read_sql("SELECT * FROM portfolio", conn)
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
                    cursor = conn.cursor()
                    ex_r = st.session_state.get("live_rate_store", 1350.0) if t_curr == "USD" else 1.0
                    cursor.execute("""
                        INSERT INTO portfolio (record_date, whose, broker, account_num, account_type, item_name, ticker, buy_price, quantity, current_price, currency, exchange_rate)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (t_date, t_whose, t_broker, t_acc_num, t_acc_type, t_item, t_ticker, t_price, t_qty, t_price, t_curr, ex_r))
                    conn.commit()
                    conn.close()
                    export_backup_json()
                    st.success(f"🎉 '{t_item}' 신규 매수가 성공적으로 반영되었습니다!")
                    st.rerun()

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
                else:
                    conn = get_connection()
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
                        if trade_qty >= old_qty:
                            cursor.execute("DELETE FROM portfolio WHERE id = ?", (target_id,))
                            st.success(f"🎉 '{item_name}' 전량 매도가 완료되어 해당 항목이 포트폴리오에서 삭제되었습니다.")
                        else:
                            new_qty = old_qty - trade_qty
                            cursor.execute("""
                                UPDATE portfolio 
                                SET quantity = ?, record_date = ? 
                                WHERE id = ?
                            """, (new_qty, t_date, target_id))
                            st.success(f"🎉 '{item_name}' 부분 매도가 완료되었습니다! (잔여 수량: {new_qty:,.2f})")

                    conn.commit()
                    conn.close()
                    export_backup_json()
                    st.rerun()


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

st.sidebar.header("💱 환율 정보")
if "live_rate_store" not in st.session_state:
    st.session_state.live_rate_store = get_exchange_rate()

current_rate = st.sidebar.number_input(
    "현재 원/달러 환율 (KRW/USD)", value=st.session_state.live_rate_store, step=1.0
)

if st.sidebar.button("🔄 시세 캐시 초기화 & 갱신"):
    st.cache_data.clear()
    st.sidebar.success("시세 캐시가 초기화되었습니다!")
    st.rerun()

menu = st.sidebar.selectbox(
    "메뉴 선택",
    [
        "자산 입력 및 관리",
        "일별/시점별 보유 현황 분석",
        "기간별 성과 및 추이 분석",
        "💾 데이터 백업 및 복구",
    ],
)

# ---------------------------------------------------------
# 메뉴 1: 자산 입력 및 관리
# ---------------------------------------------------------
if menu == "자산 입력 및 관리":
    st.header("📝 자산 데이터 입력 & 수정")
    conn = get_connection()
    df_raw = pd.read_sql(
        "SELECT * FROM portfolio ORDER BY record_date DESC, id DESC", conn
    )
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

            cnt = update_all_prices_and_rate_batch(target_rate)
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

    if mode == "🖥 웹 화면 직접 수정/편집 (추천)":
        st.subheader("🖥️️ 웹 스프레드시트 편집기 (직접 수정/행 추가/선택 삭제)")
        
        if st.button("⚡ 매매(트레이딩) 입력 다이얼로그 열기", type="primary"):
            open_trading_dialog()

        st.info("💡 **사용 방법**: 아래 표에서 셀을 직접 수정하거나, 체크박스로 삭제할 행을 선택하고, 하단 버튼으로 줄을 추가하거나 일괄 저장할 수 있습니다.")

        required_cols = [
            "record_date", "whose", "broker", "account_num", "account_type", "item_name",
            "ticker", "category1", "category2", "category3", "category4",
            "buy_price", "quantity", "current_price", "currency", "exchange_rate"
        ]

        if not df_raw.empty:
            edit_df = df_raw.copy()
        else:
            edit_df = pd.DataFrame(columns=["id"] + required_cols)

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
                        cursor = conn.cursor()
                        cursor.executemany("DELETE FROM portfolio WHERE id = ?", [(i,) for i in ids_to_delete])
                        conn.commit()
                        conn.close()
                        export_backup_json()
                        st.session_state["select_all_flag"] = False
                        st.success(f"선택한 {len(ids_to_delete)}개 항목이 성공적으로 삭제되었습니다!")
                        st.rerun()
                    else:
                        st.warning("새로 입력되어 ID가 없는 행은 저장 시 반영되지 않습니다.")
                else:
                    st.warning("삭제할 행의 '선택(삭제)' 체크박스를 지정해 주세요.")

        with col_ed2:
            if st.button("💾 표 수정 및 변경사항 DB에 일괄 저장"):
                conn = get_connection()
                cursor = conn.cursor()

                cursor.execute("DELETE FROM portfolio")
                
                save_df = edited_data.drop(columns=["선택(삭제)", "id"], errors="ignore")
                
                for col in required_cols:
                    if col not in save_df.columns:
                        save_df[col] = None

                save_df["record_date"] = save_df["record_date"].fillna(datetime.now().strftime("%Y-%m-%d"))
                save_df["whose"] = save_df["whose"].fillna("본인")
                save_df["currency"] = save_df["currency"].fillna("KRW")
                save_df["exchange_rate"] = save_df["exchange_rate"].fillna(1.0)
                save_df["buy_price"] = pd.to_numeric(save_df["buy_price"], errors="coerce").fillna(0.0)
                save_df["quantity"] = pd.to_numeric(save_df["quantity"], errors="coerce").fillna(0.0)
                save_df["current_price"] = pd.to_numeric(save_df["current_price"], errors="coerce").fillna(0.0)

                save_df[required_cols].to_sql("portfolio", conn, if_exists="append", index=False)
                conn.commit()
                conn.close()
                export_backup_json()
                st.session_state["select_all_flag"] = False
                st.success("🎉 표 전체 변경사항이 성공적으로 저장되었습니다!")
                st.rerun()

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
                    today_dt = datetime.now()
                    today_str = today_dt.strftime("%Y-%m-%d")
                    start_str = (today_dt - timedelta(days=7)).strftime("%Y-%m-%d")
                    end_str = (today_dt + timedelta(days=2)).strftime("%Y-%m-%d")
                    m_data = fetch_batch_market_data((f_ticker,), start_str, end_str)
                    fetched = get_price_from_batch_data(m_data, f_ticker, today_str)
                    if fetched:
                        current_price = fetched

                conn = get_connection()
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
                conn.close()
                export_backup_json()
                st.success("저장되었습니다.")
                st.rerun()

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
                upload_df["exchange_rate"] = upload_df["exchange_rate"].fillna(1.0)
                upload_df["whose"] = upload_df["whose"].fillna("본인")

                st.dataframe(upload_df[required_cols], width="stretch")

                if st.button("DB에 일괄 저장하기"):
                    conn = get_connection()
                    upload_df[required_cols].to_sql(
                        "portfolio", conn, if_exists="append", index=False
                    )
                    conn.close()
                    export_backup_json()
                    st.success("WHOSE 포함 일괄 저장 완료!")
                    st.rerun()
            except Exception as e:
                st.error(f"오류: {e}")

    elif mode == "🗑️ 데이터 삭제 관리":
        if not df_raw.empty:
            st.subheader("🗑️ 데이터 삭제 관리")
            all_ids = df_raw["id"].tolist()
            ids_to_del = st.multiselect("삭제할 ID 선택", all_ids)
            if st.button("선택 삭제"):
                if ids_to_del:
                    conn = get_connection()
                    cursor = conn.cursor()
                    cursor.executemany(
                        "DELETE FROM portfolio WHERE id = ?",
                        [(i,) for i in ids_to_del],
                    )
                    conn.commit()
                    conn.close()
                    export_backup_json()
                    st.success("삭제 완료!")
                    st.rerun()
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
    df = pd.read_sql("SELECT * FROM portfolio", conn)
    conn.close()

    if df.empty:
        st.info("데이터가 없습니다.")
    else:
        df["whose"] = df["whose"].fillna("본인").replace("", "본인")
        available_whose_list = sorted(df["whose"].unique())

        col_filter1, col_filter2, col_filter3 = st.columns([2, 2, 2])

        with col_filter1:
            selected_whose_list = st.multiselect(
                "1. 소유자(WHOSE) 선택 (복수 선택 가능)",
                options=available_whose_list,
                default=available_whose_list
            )

        if not selected_whose_list:
            st.warning("소유자(WHOSE)를 최소 1개 이상 선택해 주세요.")
        else:
            owner_selected_dates = {}
            with col_filter2:
                st.write("2. 소유자별 입력 데이터 날짜 선택")
                for w in selected_whose_list:
                    w_dates = sorted(df[df["whose"] == w]["record_date"].unique(), reverse=True)
                    if w_dates:
                        owner_selected_dates[w] = st.selectbox(
                            f"[{w}] 기준 날짜",
                            options=w_dates,
                            key=f"select_date_{w}"
                        )

            with col_filter3:
                use_historical_price = st.checkbox(
                    "🗓️ 특정 날짜 기준 과거 시세로 조회하기"
                )
                if use_historical_price:
                    target_eval_date = st.date_input(
                        "조회 기준 시세 날짜", datetime.now()
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

            if use_historical_price and target_eval_date:
                with st.spinner(f"[{target_eval_date}] 배치 시세 데이터를 조회 중..."):
                    sub_df["formatted_ticker"] = sub_df.apply(
                        lambda r: normalize_ticker(r["ticker"], r["currency"]), axis=1
                    )
                    tickers = tuple(sub_df["formatted_ticker"].dropna().unique().tolist())
                    
                    target_dt = pd.to_datetime(target_eval_date)
                    start_dt_str = (target_dt - timedelta(days=7)).strftime("%Y-%m-%d")
                    end_dt_str = (target_dt + timedelta(days=2)).strftime("%Y-%m-%d")
                    
                    m_data = fetch_batch_market_data(tickers, start_dt_str, end_dt_str)

                    for idx, row in sub_df.iterrows():
                        h_price = get_price_from_batch_data(
                            m_data, row["formatted_ticker"], target_eval_date
                        )
                        if h_price is not None:
                            sub_df.at[idx, "current_price"] = h_price

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
            cat_options = {
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

            col_t1, col_t2, col_t3, col_t4 = st.columns(4)
            with col_t1:
                l1 = st.selectbox("1단계 (최상위)", list(cat_options.keys()), index=1)
            with col_t2:
                l2 = st.selectbox("2단계", ["없음"] + list(cat_options.keys()), index=1)
            with col_t3:
                l3 = st.selectbox("3단계", ["없음"] + list(cat_options.keys()), index=3)
            with col_t4:
                l4 = st.selectbox("4단계 (최하위)", ["없음"] + list(cat_options.keys()), index=4)

            col_c1, col_c2 = st.columns([2, 1])
            with col_c1:
                color_option = st.selectbox(
                    "🗺️ 트리맵 색상 기준 선택",
                    [
                        "총 누적 수익률 (%)",
                        "일간 등락률 (1일)",
                        "주간 등락률 (1주일)",
                        "월간 등락률 (1개월)",
                        "월초 대비 등락률 (Month to Date)",
                        "연초 대비 등락률 (YTD)",
                        "특정 날짜 지정 등락률",
                    ],
                    index=1
                )

            custom_base_date = None
            if color_option == "특정 날짜 지정 등락률":
                with col_c2:
                    custom_base_date = st.date_input(
                        "기준 날짜 선택",
                        value=datetime.now() - timedelta(days=30),
                        max_value=datetime.now(),
                    )

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
                if color_option == "일간 등락률 (1일)":
                    start_fetch_dt = (today - timedelta(days=14)).strftime("%Y-%m-%d")
                    past_date_str = None
                elif color_option == "주간 등락률 (1주일)":
                    start_fetch_dt = (today - timedelta(days=15)).strftime("%Y-%m-%d")
                    past_date_str = (today - timedelta(days=7)).strftime("%Y-%m-%d")
                elif color_option == "월간 등락률 (1개월)":
                    start_fetch_dt = (today - timedelta(days=45)).strftime("%Y-%m-%d")
                    past_date_str = (today - timedelta(days=30)).strftime("%Y-%m-%d")
                elif color_option == "월초 대비 등락률 (Month to Date)":
                    mtd_dt = datetime(today.year, today.month, 1)
                    start_fetch_dt = (mtd_dt - timedelta(days=10)).strftime("%Y-%m-%d")
                    past_date_str = mtd_dt.strftime("%Y-%m-%d")
                elif color_option == "연초 대비 등락률 (YTD)":
                    ytd_dt = datetime(today.year, 1, 1)
                    start_fetch_dt = (ytd_dt - timedelta(days=10)).strftime("%Y-%m-%d")
                    past_date_str = ytd_dt.strftime("%Y-%m-%d")
                elif color_option == "특정 날짜 지정 등락률" and custom_base_date:
                    start_fetch_dt = (custom_base_date - timedelta(days=10)).strftime("%Y-%m-%d")
                    past_date_str = custom_base_date.strftime("%Y-%m-%d")
                else:
                    start_fetch_dt = (today - timedelta(days=14)).strftime("%Y-%m-%d")
                    past_date_str = None

                end_fetch_dt = (today + timedelta(days=2)).strftime("%Y-%m-%d")

                with st.spinner(f"[{color_option}] 배치 계산 중..."):
                    sub_df["formatted_ticker"] = sub_df.apply(
                        lambda r: normalize_ticker(r["ticker"], r["currency"]), axis=1
                    )
                    tickers = tuple(sub_df["formatted_ticker"].dropna().unique().tolist())
                    
                    m_data = fetch_batch_market_data(tickers, start_fetch_dt, end_fetch_dt)

                    for _, row in sub_df.iterrows():
                        f_ticker = row["formatted_ticker"]
                        curr_p = row["current_price"]
                        qty = row["quantity"]
                        ex_r = row["rate_multiplier"]
                        rate = 0.0
                        profit_amt = 0.0

                        if not f_ticker:
                            change_rates.append(rate)
                            period_profits.append(profit_amt)
                            continue

                        if not m_data.empty:
                            if isinstance(m_data.columns, pd.MultiIndex):
                                if f_ticker in m_data.columns.levels[0]:
                                    df_ticker = m_data[f_ticker]
                                elif f_ticker in m_data.columns.levels[1]:
                                    df_ticker = m_data.xs(f_ticker, axis=1, level=1)
                                else:
                                    df_ticker = pd.DataFrame()
                            else:
                                df_ticker = m_data

                            if not df_ticker.empty:
                                if hasattr(df_ticker.index, "tz") and df_ticker.index.tz is not None:
                                    df_ticker.index = df_ticker.index.tz_localize(None)

                                if "Close" in df_ticker.columns:
                                    valid_series = df_ticker["Close"].dropna()
                                else:
                                    valid_series = df_ticker.dropna()

                                if color_option == "일간 등락률 (1일)":
                                    if len(valid_series) >= 2:
                                        latest_price = float(valid_series.iloc[-1])
                                        prev_price = float(valid_series.iloc[-2])
                                        if prev_price > 0:
                                            rate = round(((latest_price - prev_price) / prev_price) * 100, 2)
                                            profit_amt = (latest_price - prev_price) * qty * ex_r
                                else:
                                    p_price = get_price_from_batch_data(m_data, f_ticker, past_date_str)
                                    
                                    if p_price is None and not valid_series.empty:
                                        p_price = float(valid_series.iloc[0])

                                    if curr_p and p_price and float(p_price) > 0:
                                        rate = round(((float(curr_p) - float(p_price)) / float(p_price)) * 100, 2)
                                        profit_amt = (float(curr_p) - float(p_price)) * ex_r * qty

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
            
            profit_col_label = f"평가손익({color_option})" if color_option != "총 누적 수익률 (%)" else "평가손익(원)"
            rate_col_label = f"등락률({color_option})" if color_option != "총 누적 수익률 (%)" else "수익률(%)"

            # ---------------------------------------------------------
            # Treemap 드릴다운 상태 연동 동적 경로 옵션 수집 및 선택
            # ---------------------------------------------------------
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

            col_drill1, col_drill2 = st.columns([2.5, 1.5])
            with col_drill1:
                selected_drill_path = st.selectbox(
                    "🔍 TREEMAP 계층 드릴다운 / 하위 분류 화면 선택 (상단 표 100% 점유 연동)",
                    options=path_options,
                    index=0,
                    key="treemap_drilldown_selector"
                )
            with col_drill2:
                st.caption("💡 특정 하위 분류를 선택하면 해당 분류의 총액을 100% 점유율로 자동 계산하여 상단 표와 Treemap이 완벽하게 연동됩니다.")

            # 선택한 드릴다운 경로에 따른 데이터 프레임 필터링
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

            # ---------------------------------------------------------
            # [수정 연동] Treemap 화면 상의 100% 점유 기준 표 생성
            # ---------------------------------------------------------
            st.write(f"📌 **현재 화면 기준 계층 요약 현황 표 (화면 점유율: 100.00% 기준)**")

            tree_rows = []

            def build_tree_summary_filtered(df_sub, active_cols, depth=0):
                if not active_cols:
                    return

                curr_col = active_cols[0]
                rem_cols = active_cols[1:]

                grouped = df_sub.groupby(curr_col)

                for name, group in grouped:
                    group_eval = group["평가액(원)"].sum()
                    group_buy = group["매입총액(원)"].sum()
                    group_profit = group["선택기준_평가손익(원)"].sum()

                    if color_option == "총 누적 수익률 (%)":
                        group_rate = (group_profit / group_buy * 100) if group_buy != 0 else 0.0
                    else:
                        past_eval = group_eval - group_profit
                        group_rate = (group_profit / past_eval * 100) if past_eval != 0 else 0.0

                    # 현재 화면의 총 평가액 기준 100% 점유율 계산
                    group_share = (group_eval / active_total_eval * 100) if active_total_eval != 0 else 0

                    indent = "└─ " * depth if depth > 0 else ""
                    label_display = f"{indent}{name}"

                    tree_rows.append({
                        "구분 항목": label_display,
                        "평가액(원)": group_eval,
                        profit_col_label: group_profit,
                        rate_col_label: group_rate,
                        "점유율(%)": group_share
                    })

                    if rem_cols:
                        build_tree_summary_filtered(group, rem_cols, depth + 1)

            if active_group_cols:
                build_tree_summary_filtered(filtered_df, active_group_cols)

            total_row_profit = filtered_df["선택기준_평가손익(원)"].sum()
            total_row_buy = filtered_df["매입총액(원)"].sum()
            if color_option == "총 누적 수익률 (%)":
                total_row_rate = (total_row_profit / total_row_buy * 100) if total_row_buy != 0 else 0.0
            else:
                past_total_eval = active_total_eval - total_row_profit
                total_row_rate = (total_row_profit / past_total_eval * 100) if past_total_eval != 0 else 0.0

            total_tree_row = {
                "구분 항목": view_root_label,
                "평가액(원)": active_total_eval,
                profit_col_label: total_row_profit,
                rate_col_label: total_row_rate,
                "점유율(%)": 100.0
            }

            tree_summary_df = pd.DataFrame([total_tree_row] + tree_rows)

            st.dataframe(
                tree_summary_df[[
                    "구분 항목",
                    "평가액(원)",
                    profit_col_label,
                    rate_col_label,
                    "점유율(%)"
                ]]
                .style.format({
                    "평가액(원)": "₩{:,.0f}",
                    profit_col_label: "₩{:,.0f}",
                    rate_col_label: "{:+.2f}%",
                    "점유율(%)": "{:.2f}%"
                }),
                width="stretch",
                hide_index=True
            )
            st.markdown("")

            # ---------------------------------------------------------
            # TREEMAP 그래프 생성 (선택한 드릴다운 범위 연동)
            # ---------------------------------------------------------
            ids, labels, parents, values = [], [], [], []
            custom_rates, custom_prices, custom_profits = [], [], []

            ids.append("Root")
            labels.append(view_root_label)
            parents.append("")
            values.append(active_total_eval)
            custom_rates.append(total_row_rate)
            custom_prices.append("-")
            custom_profits.append(total_row_profit)

            built_nodes = set(["Root"])

            if active_group_cols:
                for idx_row, row in filtered_df.iterrows():
                    current_parent = "Root"
                    current_id_path = ""
                    
                    for depth, col in enumerate(active_group_cols):
                        val_str = str(row[col])
                        current_id_path = f"{current_id_path}/{val_str}" if current_id_path else val_str
                        
                        if current_id_path not in built_nodes:
                            built_nodes.add(current_id_path)
                            
                            filter_mask = pd.Series(True, index=filtered_df.index)
                            for k in range(depth + 1):
                                filter_mask &= (filtered_df[active_group_cols[k]] == row[active_group_cols[k]])
                            
                            sub_grp = filtered_df[filter_mask]
                            
                            grp_eval = sub_grp["평가액(원)"].sum()
                            grp_buy = sub_grp["매입총액(원)"].sum()
                            grp_profit = sub_grp["선택기준_평가손익(원)"].sum()
                            
                            if color_option == "총 누적 수익률 (%)":
                                grp_rate = (grp_profit / grp_buy * 100) if grp_buy != 0 else 0.0
                            else:
                                grp_past_eval = grp_eval - grp_profit
                                grp_rate = (grp_profit / grp_past_eval * 100) if grp_past_eval != 0 else 0.0

                            if depth == len(active_group_cols) - 1:
                                price_sym = "$" if row["currency"] == "USD" else "₩"
                                disp_price = f"{price_sym}{row['current_price']:,.2f}" if row["currency"] == "USD" else f"{price_sym}{row['current_price']:,.0f}"
                            else:
                                disp_price = "-"

                            ids.append(current_id_path)
                            labels.append(val_str)
                            parents.append(current_parent)
                            values.append(grp_eval)
                            custom_rates.append(grp_rate)
                            custom_prices.append(disp_price)
                            custom_profits.append(grp_profit)

                        current_parent = current_id_path

            c_rates_arr = [r for r in custom_rates if r is not None]
            max_abs_val = max(abs(min(c_rates_arr, default=1.0)), abs(max(c_rates_arr, default=1.0)), 1.0)

            if color_option == "일간 등락률 (1일)":
                dynamic_range = [-min(max_abs_val, 3.0), min(max_abs_val, 3.0)]
            elif color_option in ["주간 등락률 (1주일)", "월간 등락률 (1개월)", "월초 대비 등락률 (Month to Date)"]:
                dynamic_range = [-min(max_abs_val, 15.0), min(max_abs_val, 15.0)]
            else:
                dynamic_range = [-min(max_abs_val, 40.0), min(max_abs_val, 40.0)]

            fig_treemap = go.Figure(
                go.Treemap(
                    ids=ids,
                    labels=labels,
                    parents=parents,
                    values=values,
                    branchvalues="total",
                    marker=dict(
                        colors=custom_rates,
                        colorscale=[
                            [0.0, "#D32F2F"],
                            [0.5, "#455A64"],
                            [1.0, "#2E7D32"],
                        ],
                        cmid=0,
                        cmin=dynamic_range[0],
                        cmax=dynamic_range[1],
                        colorbar=dict(title=color_option),
                    ),
                    customdata=list(zip(custom_rates, custom_prices, custom_profits)),
                    texttemplate=(
                        "<b>%{label}</b><br>"
                        "<span style='font-size: 14px;'><b>₩%{value:,.0f}</b></span><br>"
                        "<span style='font-size: 11px;'>현재가: %{customdata[1]}</span><br>"
                        "<span style='font-size: 11px;'>점유율: %{percentRoot:.2%}</span><br>"
                        "<span style='font-size: 11px;'><b>%{customdata[0]:+.2f}%</b></span>"
                    ),
                    hovertemplate=(
                        "<span style='font-size: 18px;'><b>%{label}</b></span><br>"
                        "<span style='font-size: 15px;'>"
                        "• 평가금액: ₩%{value:,.0f}<br>"
                        "• 현재가: %{customdata[1]}<br>"
                        f"• {profit_col_label}: ₩%{{customdata[2]:,.0f}}<br>"
                        f"• {color_option}: %{{customdata[0]:+.2f}}%<br>"
                        "• 선택 화면 대비 점유율: %{percentRoot:.2%}<br>"
                        "• 상위 그룹 대비 점유율: %{percentParent:.2%}</span><extra></extra>"
                    ),
                    hoverlabel=dict(font_size=15),
                    textfont=dict(color="white"),
                    insidetextfont=dict(color="white"),
                )
            )

            fig_treemap.update_layout(
                title=f"계층별 다단계 TREEMAP 자산 분포 ({view_root_label})",
                margin=dict(t=30, l=10, r=10, b=10),
            )

            st.plotly_chart(fig_treemap, width="stretch")

            st.write("📋 **선택 계층(상위 및 하위 그룹)별 평가액 및 전체 점유율 상세 요약**")

            hierarchy_summary = (
                filtered_df.groupby(group_cols)
                .agg({
                    "매입총액(원)": "sum",
                    "평가액(원)": "sum",
                    "평가손익(원)": "sum",
                })
                .reset_index()
            )

            hierarchy_summary["수익률(%)"] = (
                hierarchy_summary["평가손익(원)"] / hierarchy_summary["매입총액(원)"].replace(0, 1)
            ) * 100
            hierarchy_summary["점유율(%)"] = (
                (hierarchy_summary["평가액(원)"] / active_total_eval * 100) if active_total_eval != 0 else 0
            )

            display_df = hierarchy_summary.rename(columns=col_rename_map)

            display_hierarchy_cols = [col_rename_map[c] for c in group_cols]
            final_cols = display_hierarchy_cols + [
                "매입총액(원)",
                "평가액(원)",
                "평가손익(원)",
                "수익률(%)",
                "점유율(%)",
            ]

            col_h1, col_h2 = st.columns([1.3, 1])

            with col_h1:
                st.dataframe(
                    display_df[final_cols]
                    .sort_values(by="평가액(원)", ascending=False)
                    .style.format({
                        "매입총액(원)": "₩{:,.0f}",
                        "평가액(원)": "₩{:,.0f}",
                        "평가손익(원)": "₩{:,.0f}",
                        "수익률(%)": "{:.2f}%",
                        "점유율(%)": "{:.2f}%"
                    }),
                    width="stretch"
                )

            with col_h2:
                pie_tab1, pie_tab2 = st.tabs(["🥧 계층/분류 기준별 점유율", "🍩 보유 항목(ITEM)별 점유율"])

                with pie_tab1:
                    group_mode_options = ["전체 계층 경로 (A > B > C)"] + list(cat_options.keys())

                    pie_group_mode = st.selectbox(
                        "🎯 계층 도넛 그래프 구분 기준 선택",
                        options=group_mode_options,
                        index=0,
                        key="pie_hierarchy_mode_select"
                    )

                    if pie_group_mode == "전체 계층 경로 (A > B > C)":
                        hierarchy_summary["계층경로"] = hierarchy_summary[group_cols].astype(str).agg(" > ".join, axis=1)
                        pie_chart_df = hierarchy_summary.groupby("계층경로")["평가액(원)"].sum().reset_index()
                        names_col = "계층경로"
                    else:
                        target_col = cat_options[pie_group_mode]
                        pie_chart_df = filtered_df.groupby(target_col)["평가액(원)"].sum().reset_index()
                        names_col = target_col

                    fig_pie_hierarchy = px.pie(
                        pie_chart_df,
                        values="평가액(원)",
                        names=names_col,
                        title=f"선택 계층 점유율 ({pie_group_mode})",
                        hole=0.35,
                    )
                    fig_pie_hierarchy.update_traces(
                        textposition="inside",
                        texttemplate="<b>%{label}</b><br><b>₩%{value:,.0f}</b><br>(%{percent})",
                        insidetextfont=dict(size=14),
                        hovertemplate="<b>%{label}</b><br>평가액: ₩%{value:,.0f}<br>점유율: %{percent}<extra></extra>"
                    )
                    fig_pie_hierarchy.update_layout(margin=dict(t=30, l=10, r=10, b=10), showlegend=False)
                    st.plotly_chart(fig_pie_hierarchy, width="stretch")

                with pie_tab2:
                    item_summary = filtered_df.groupby("item_name")["평가액(원)"].sum().reset_index()
                    fig_pie_item = px.pie(
                        item_summary,
                        values="평가액(원)",
                        names="item_name",
                        title="보유 항목별 점유율",
                        hole=0.35,
                    )
                    fig_pie_item.update_traces(
                        textposition="inside",
                        texttemplate="<b>%{label}</b><br><b>₩%{value:,.0f}</b><br>(%{percent})",
                        insidetextfont=dict(size=14),
                        hovertemplate="<b>%{label}</b><br>평가액: ₩%{value:,.0f}<br>점유율: %{percent}<extra></extra>"
                    )
                    fig_pie_item.update_layout(margin=dict(t=30, l=10, r=10, b=10), showlegend=False)
                    st.plotly_chart(fig_pie_item, width="stretch")

            st.markdown("---")

            st.subheader("📋 선택 시점 상세 보유 목록")
            filtered_df["점유율(%)"] = (
                (filtered_df["평가액(원)"] / active_total_eval * 100) if active_total_eval != 0 else 0
            )
            filtered_df["수익률(%)"] = (
                filtered_df["평가손익(원)"] / filtered_df["매입총액(원)"].replace(0, 1)
            ) * 100
            st.dataframe(
                filtered_df[[
                    "whose",
                    "broker",
                    "account_num",
                    "account_type",
                    "item_name",
                    "ticker",
                    "currency",
                    "exchange_rate",
                    "buy_price",
                    "quantity",
                    "current_price",
                    "매입총액(원)",
                    "평가액(원)",
                    "평가손익(원)",
                    "수익률(%)",
                    "점유율(%)",
                ]]
            )

# ---------------------------------------------------------
# 메뉴 3: 기간별 성과 및 추이 분석
# ---------------------------------------------------------
elif menu == "기간별 성과 및 추이 분석":
    st.header("📈 지정 포트폴리오 기준 날짜별 시세 반영 TREND 분석")
    conn = get_connection()
    df = pd.read_sql("SELECT * FROM portfolio", conn)
    conn.close()

    if df.empty:
        st.info("데이터가 없습니다.")
    else:
        st.subheader("🎯 포트폴리오 데이터 선택 & 시세 반영 비교 날짜 지정")

        available_port_dates = sorted(df["record_date"].unique(), reverse=True)

        col_p1, col_p2 = st.columns([1.2, 1])
        with col_p1:
            selected_port_dates = st.multiselect(
                "기준 포트폴리오 저장 날짜 선택 (다중 선택 가능)",
                available_port_dates,
                default=[available_port_dates[0]] if available_port_dates else []
            )

        group_options = {
            "없음 (전체 총액)": "NONE",
            "소유자 (WHOSE)": "whose",
            "대분류 (분류1)": "category1",
            "중분류 (분류2)": "category2",
            "소분류 (분류3)": "category3",
            "세분류 (분류4)": "category4",
            "계좌번호": "account_num",
            "금융사": "broker",
            "구분": "account_type",
            "보유항목 (ITEM)": "item_name",
        }

        with col_p2:
            selected_group_label = st.selectbox(
                "비교 분류 기준", list(group_options.keys()), index=0
            )

        group_col = group_options[selected_group_label]

        st.markdown("---")
        st.subheader("🗓️ 시세 반영 비교 날짜 범위를 생성 및 선택하세요")

        if "custom_dates" not in st.session_state:
            st.session_state.custom_dates = selected_port_dates.copy() if selected_port_dates else [datetime.now().strftime("%Y-%m-%d")]

        tab_gen1, tab_gen2 = st.tabs(["📅 주기별 날짜 일괄 생성", "📌 임의 개별/다중 날짜 추가"])

        with tab_gen1:
            col_gen1, col_gen2, col_gen3, col_gen4 = st.columns([1.5, 1.5, 1.5, 1])

            with col_gen1:
                start_d = st.date_input("조회 시작일", value=datetime.now() - timedelta(days=30), key="start_d")
            with col_gen2:
                end_d = st.date_input("조회 종료일", value=datetime.now(), key="end_d")
            with col_gen3:
                freq_option = st.selectbox(
                    "생성 주기 선택",
                    ["매일", "매주 (월요일)", "매월 (월말)", "매년 (연말)"],
                    key="freq_opt"
                )

            with col_gen4:
                st.write("")
                st.write("")
                if st.button("📅 일괄 생성", key="btn_gen_batch"):
                    if start_d > end_d:
                        st.error("시작일은 종료일보다 이전이어야 합니다.")
                    else:
                        if freq_option == "매일":
                            gen_dates = pd.date_range(start=start_d, end=end_d, freq="D")
                        elif freq_option == "매주 (월요일)":
                            gen_dates = pd.date_range(start=start_d, end=end_d, freq="W-MON")
                        elif freq_option == "매월 (월말)":
                            gen_dates = pd.date_range(start=start_d, end=end_d, freq="ME")
                        elif freq_option == "매년 (연말)":
                            gen_dates = pd.date_range(start=start_d, end=end_d, freq="YE")

                        formatted_gen_dates = [d.strftime("%Y-%m-%d") for d in gen_dates]

                        merged_set = set(st.session_state.custom_dates).union(set(formatted_gen_dates))
                        st.session_state.custom_dates = sorted(list(merged_set))
                        st.success(f"{len(formatted_gen_dates)}개의 날짜가 성공적으로 목록에 추가되었습니다!")
                        st.rerun()

        with tab_gen2:
            col_add1, col_add2, col_add3 = st.columns([1.5, 2.5, 1])
            
            with col_add1:
                single_picker = st.date_input("달력에서 날짜 선택", value=datetime.now(), key="single_picker_date")
            
            with col_add2:
                text_dates_input = st.text_input(
                    "직접 날짜 입력 (여러 개일 경우 쉼표, 띄어쓰기로 구분)",
                    placeholder="예: 2024-01-15, 2024-03-20 2024-05-10",
                    key="text_dates_input"
                )
            
            with col_add3:
                st.write("")
                st.write("")
                if st.button("➕ 날짜 추가", key="btn_add_custom_dates"):
                    to_add = set()
                    to_add.add(single_picker.strftime("%Y-%m-%d"))
                    
                    if text_dates_input.strip():
                        raw_tokens = text_dates_input.replace(",", " ").split()
                        for token in raw_tokens:
                            token = token.strip()
                            try:
                                parsed_d = pd.to_datetime(token).strftime("%Y-%m-%d")
                                to_add.add(parsed_d)
                            except Exception:
                                st.warning(f"'{token}'은(는) 유효한 날짜 형식이 아니어서 제외되었습니다.")

                    merged_set = set(st.session_state.custom_dates).union(to_add)
                    st.session_state.custom_dates = sorted(list(merged_set))
                    st.success(f"{len(to_add)}개의 날짜가 성공적으로 반영되었습니다!")
                    st.rerun()

        st.markdown("---")

        target_dates = st.multiselect(
            "시세 조회 날짜 목록 (상단에서 생성/추가된 날짜 중 최종 계산에 반영할 날짜들을 선택/해제하세요)",
            options=st.session_state.custom_dates,
            default=st.session_state.custom_dates,
        )

        if st.button("🚀 선택한 날짜별 시세 수집 & TREND 계산 실행"):
            if not selected_port_dates:
                st.warning("기준 포트폴리오 저장 날짜를 1개 이상 선택해 주세요.")
            elif not target_dates:
                st.warning("비교할 시세 날짜를 1개 이상 선택해 주세요.")
            else:
                trend_records = []
                
                all_tickers = []
                for p_date in selected_port_dates:
                    temp_df = df[df["record_date"] == p_date]
                    for _, r in temp_df.iterrows():
                        all_tickers.append(normalize_ticker(r["ticker"], r["currency"]))
                
                valid_tickers = tuple(set([t for t in all_tickers if t]))
                
                sorted_dates = sorted(target_dates)
                min_date = (pd.to_datetime(sorted_dates[0]) - timedelta(days=7)).strftime("%Y-%m-%d")
                max_date = (pd.to_datetime(sorted_dates[-1]) + timedelta(days=2)).strftime("%Y-%m-%d")

                with st.spinner("🚀 모든 비교 날짜의 시세 데이터를 배치로 수집하는 중..."):
                    st.cache_data.clear()
                    market_batch_data = fetch_batch_market_data(valid_tickers, min_date, max_date)

                for port_date in selected_port_dates:
                    base_portfolio = df[df["record_date"] == port_date].copy()
                    base_portfolio["formatted_ticker"] = base_portfolio.apply(
                        lambda r: normalize_ticker(r["ticker"], r["currency"]), axis=1
                    )

                    for t_date in sorted_dates:
                        for _, row in base_portfolio.iterrows():
                            f_ticker = row["formatted_ticker"]
                            qty = row["quantity"]
                            curr = row["currency"]
                            ex_r = row["exchange_rate"] if curr == "USD" else 1.0

                            p = get_price_from_batch_data(market_batch_data, f_ticker, t_date)
                            if p is None:
                                p = row["current_price"]

                            eval_val = p * qty * ex_r

                            if group_col == "NONE":
                                category_label = "전체 총액"
                            else:
                                cat_val = row[group_col] if pd.notna(row[group_col]) else "미지정"
                                category_label = "미지정" if str(cat_val).strip() == "" else str(cat_val)

                            if len(selected_port_dates) > 1:
                                display_series_name = f"[{port_date}] {category_label}"
                            else:
                                display_series_name = category_label

                            trend_records.append({
                                "기준포트폴리오": port_date,
                                "시세반영일": t_date,
                                "구분": display_series_name,
                                "평가액(원)": eval_val,
                            })

                st.session_state.calculated_trend_df = pd.DataFrame(trend_records)
                st.success("✅ 배치 시세 수집 및 Trend 연산 완료!")

        if "calculated_trend_df" in st.session_state and not st.session_state.calculated_trend_df.empty:
            trend_df = st.session_state.calculated_trend_df

            summary_trend = (
                trend_df.groupby(["시세반영일", "구분"])["평가액(원)"]
                .sum()
                .reset_index()
            )

            chart_title = (
                f"날짜별 시세 반영 TREND 분석 (분류: {selected_group_label})"
            )

            fig_trend = px.line(
                summary_trend,
                x="시세반영일",
                y="평가액(원)",
                color="구분",
                markers=True,
                title=chart_title,
                labels={
                    "시세반영일": "시세 적용 날짜",
                    "평가액(원)": "평가액 (원)",
                    "구분": "분류 / 포트폴리오",
                },
            )

            st.markdown("### 🎚️ Y축 범위(스케일) 실시간 조절")
            
            min_val = float(summary_trend["평가액(원)"].min())
            max_val = float(summary_trend["평가액(원)"].max())
            
            margin = (max_val - min_val) * 0.2 if max_val != min_val else min_val * 0.1
            s_min = float(min_val - margin) if min_val - margin > 0 else 0.0
            s_max = float(max_val + margin)

            col_sc1, col_sc2 = st.columns([3, 1])
            with col_sc2:
                auto_scale = st.checkbox("Y축 범위 자동 맞춤", value=True)

            with col_sc1:
                if not auto_scale:
                    step_val = (s_max - s_min) / 100 if s_max > s_min else 1.0

                    if "y_range_slider" not in st.session_state or st.session_state.get("last_s_min") != s_min or st.session_state.get("last_s_max") != s_max:
                        st.session_state.y_range_slider = (min_val, max_val)
                        st.session_state.last_s_min = s_min
                        st.session_state.last_s_max = s_max

                    y_range = st.slider(
                        "Y축 평가액 표시 범위 (원)",
                        min_value=s_min,
                        max_value=s_max,
                        key="y_range_slider",
                        step=step_val,
                        format="₩%'.0f"
                    )
                    fig_trend.update_yaxes(range=[y_range[0], y_range[1]])

            fig_trend.update_layout(height=550)
            st.plotly_chart(fig_trend, width="stretch")

            pivot_df = summary_trend.pivot(
                index="구분", columns="시세반영일", values="평가액(원)"
            ).fillna(0)
            
            st.write(f"📋 **[{selected_group_label}] 지정 날짜별 시세 반영 평가액 비교표**")
            st.dataframe(pivot_df.style.format("₩{:,.0f}"), width="stretch")

        st.markdown("---")

        st.subheader("📊 전체 DB 히스토리 총 자산 추이")
        df_hist = df.copy()
        df_hist["record_date"] = pd.to_datetime(df_hist["record_date"])
        df_hist["rate_multiplier"] = df_hist.apply(
            lambda r: r["exchange_rate"] if r["currency"] == "USD" else 1.0,
            axis=1,
        )
        df_hist["매입총액(원)"] = (
            df_hist["buy_price"]
            * df_hist["quantity"]
            * df_hist["rate_multiplier"]
        )
        df_hist["평가액(원)"] = (
            df_hist["current_price"]
            * df_hist["quantity"]
            * df_hist["rate_multiplier"]
        )
        df_hist["평가손익(원)"] = (
            df_hist["평가액(원)"] - df_hist["매입총액(원)"]
        )

        daily_summary = (
            df_hist.groupby("record_date")[
                ["매입총액(원)", "평가액(원)", "평가손익(원)"]
            ]
            .sum()
            .reset_index()
        )
        daily_summary["수익률(%)"] = (
            daily_summary["평가손익(원)"]
            / daily_summary["매입총액(원)"].replace(0, 1)
        ) * 100

        period = st.radio(
            "보기 단위 선택", ["일별", "주별", "월별", "년별"], horizontal=True
        )
        if period == "일별":
            chart_df = daily_summary.set_index("record_date")
        elif period == "주별":
            chart_df = (
                daily_summary.resample("W-MON", on="record_date")
                .last()
                .dropna()
            )
        elif period == "월별":
            chart_df = (
                daily_summary.resample("ME", on="record_date").last().dropna()
            )
        elif period == "년별":
            chart_df = (
                daily_summary.resample("YE", on="record_date").last().dropna()
            )

        chart_df = chart_df.reset_index()

        fig_eval = px.line(
            chart_df,
            x="record_date",
            y="평가액(원)",
            markers=True,
            title=f"{period} 기록 기반 총 자산 평가액 추이",
        )
        st.plotly_chart(fig_eval, width="stretch")

# ---------------------------------------------------------
# 메뉴 4: 데이터 백업 및 복구
# ---------------------------------------------------------
elif menu == "💾 데이터 백업 및 복구":
    st.header("💾 데이터 백업 및 완전 복구")
    st.info("클라우드 서버 재부팅이나 비활성화 후에도 데이터를 안전하게 보존하고 백업할 수 있습니다.")

    tab_bk1, tab_bk2 = st.tabs(["📥 내보내기 (백업 다운로드)", "📤 불러오기 (백업 파일 복원)"])

    with tab_bk1:
        st.subheader("📥 현재 저장된 자산 데이터 백업 파일 다운로드")
        conn = get_connection()
        df_all = pd.read_sql("SELECT * FROM portfolio", conn)
        conn.close()

        if df_all.empty:
            st.warning("백업할 자산 데이터가 없습니다.")
        else:
            json_data = export_backup_json()
            
            col_b1, col_b2 = st.columns(2)
            with col_b1:
                st.download_button(
                    label="💾 JSON 백업 파일 다운로드",
                    data=json_data or "",
                    file_name=f"portfolio_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
                    mime="application/json",
                    use_container_width=True
                )
            
            with col_b2:
                excel_df = df_all.drop(columns=["id"], errors="ignore")
                buffer = io.BytesIO()
                with pd.ExcelWriter(buffer, engine="xlsxwriter") as writer:
                    excel_df.to_excel(writer, index=False, sheet_name="Portfolio")
                
                st.download_button(
                    label="📊 Excel 백업 파일 다운로드",
                    data=buffer.getvalue(),
                    file_name=f"portfolio_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    use_container_width=True
                )

    with tab_bk2:
        st.subheader("📤 백업 파일 업로드 및 데이터 복원")
        st.write("저장해둔 JSON 백업 파일 또는 Excel 파일을 업로드하면 데이터베이스로 즉시 복원됩니다.")
        
        uploaded_backup = st.file_uploader(
            "백업 파일 선택 (.json 또는 .xlsx)",
            type=["json", "xlsx", "csv"],
            key="backup_uploader"
        )

        restore_mode = st.radio(
            "복원 방식 선택",
            [" 기존 데이터 전체 덮어쓰기 (기존 데이터 삭제 후 복원)", "➕ 기존 데이터에 추가하기"],
            index=0
        )

        if uploaded_backup is not None:
            if st.button("🚀 백업 데이터 복원 실행"):
                try:
                    replace_flag = "덮어쓰기" in restore_mode
                    if uploaded_backup.name.endswith(".json"):
                        content = uploaded_backup.read()
                        restored_cnt = import_backup_json(content, replace=replace_flag)
                    else:
                        u_df = pd.read_csv(uploaded_backup) if uploaded_backup.name.endswith(".csv") else pd.read_excel(uploaded_backup)
                        required_cols = [
                            "record_date", "whose", "broker", "account_num", "account_type", "item_name",
                            "ticker", "category1", "category2", "category3", "category4",
                            "buy_price", "quantity", "current_price", "currency", "exchange_rate"
                        ]
                        if "whose" not in u_df.columns:
                            u_df["whose"] = "본인"

                        for col in required_cols:
                            if col not in u_df.columns:
                                u_df[col] = None
                        
                        conn = get_connection()
                        cursor = conn.cursor()
                        if replace_flag:
                            cursor.execute("DELETE FROM portfolio")
                        
                        u_df[required_cols].to_sql("portfolio", conn, if_exists="append", index=False)
                        conn.commit()
                        conn.close()
                        export_backup_json()
                        restored_cnt = len(u_df)

                    st.success(f"🎉 성공적으로 {restored_cnt}개 항목이 복원되었습니다!")
                    st.rerun()
                except Exception as e:
                    st.error(f"복원 중 오류 발생: {e}")