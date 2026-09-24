import json
import sqlite3
import urllib.request
from datetime import datetime, timedelta
import pandas as pd
import plotly.express as px
import streamlit as st
import yfinance as yf

# ---------------------------------------------------------
# 모바일 전용 페이지 설정 (상단 1회만 호출)
# ---------------------------------------------------------
st.set_page_config(
    page_title="재정 관리 앱 (모바일)",
    layout="wide",
    initial_sidebar_state="collapsed"  # 모바일 화면을 위해 사이드바 기본 닫힘
)

# ---------------------------------------------------------
# 0. 배치 시세 수집 및 캐싱 함수
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


# 캐시 유효 시간을 12시간에서 30분(1800초)으로 축소하여 최신 시세 반영 지연 방지
@st.cache_data(ttl=1800)
def fetch_batch_market_data(ticker_tuple, start_date_str, end_date_str):
    """모든 종목의 시세를 yf.download로 단 1회 요청하여 캐싱"""
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
    """배치 수집된 메모리 데이터프레임에서 특정 티커 및 날짜의 종가 추출"""
    if market_data.empty or not ticker:
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

        # 시간대(tz-aware) 정보 제거하여 타겟 날짜 비교 오류(TypeError) 방지
        if hasattr(df_ticker.index, "tz") and df_ticker.index.tz is not None:
            df_ticker.index = df_ticker.index.tz_localize(None)

        target_dt = pd.to_datetime(target_date_str)
        
        if "Close" in df_ticker.columns:
            series = df_ticker["Close"]
        else:
            series = df_ticker

        if series.empty:
            return None

        valid_series = series[series.index <= target_dt].dropna()
        if not valid_series.empty:
            return round(float(valid_series.iloc[-1]), 2)
    except Exception:
        pass
    return None


# ---------------------------------------------------------
# 1. DB 설정 및 관리
# ---------------------------------------------------------
DB_FILE = "stock_data.db"


def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS portfolio (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            record_date TEXT,
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
    conn.close()


def get_connection():
    return sqlite3.connect(DB_FILE)


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
    return updated_count


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

# 사이드바에 즉시 캐시를 초기화하는 시세 강제 갱신 버튼 추가
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
            st.cache_data.clear()  # 수동 업데이트 시 캐시 강제 초기화
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
            "신규 데이터 개별 추가",
            "엑셀 파일로 일괄 추가",
            "기존 데이터 수정",
            "🗑️ 데이터 삭제 관리",
        ],
        horizontal=True,
    )

    if mode == "신규 데이터 개별 추가":
        currency = st.selectbox("통화 단위 선택", ["KRW (원화)", "USD (달러)"])
        is_usd = "USD" in currency
        default_ex_rate = current_rate if is_usd else 1.0

        with st.form("insert_form", clear_on_submit=True):
            st.subheader(f"새로운 자산 기록 추가 ({currency})")
            col1, col2, col3, col4 = st.columns(4)
            with col1:
                record_date = st.date_input("기준 날짜").strftime("%Y-%m-%d")
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
                    INSERT INTO portfolio (record_date, broker, account_num, account_type, item_name, ticker, category1, category2, category3, category4, buy_price, quantity, current_price, currency, exchange_rate)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        record_date,
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
                upload_df["record_date"] = pd.to_datetime(
                    upload_df["record_date"]
                ).dt.strftime("%Y-%m-%d")
                upload_df["currency"] = upload_df["currency"].fillna("KRW")
                upload_df["exchange_rate"] = upload_df["exchange_rate"].fillna(
                    1.0
                )
                st.dataframe(upload_df, width="stretch")

                if st.button("DB에 일괄 저장하기"):
                    conn = get_connection()
                    upload_df[required_cols].to_sql(
                        "portfolio", conn, if_exists="append", index=False
                    )
                    conn.close()
                    st.success("일괄 저장 완료!")
                    st.rerun()
            except Exception as e:
                st.error(f"오류: {e}")

    elif mode == "기존 데이터 수정":
        if not df_raw.empty:
            selected_id = st.selectbox("수정할 항목 ID", df_raw["id"].tolist())
            target = df_raw[df_raw["id"] == selected_id].iloc[0]
            with st.form("update_form"):
                col1, col2, col3, col4 = st.columns(4)
                with col1:
                    u_date = st.text_input(
                        "기준 날짜", value=str(target["record_date"])
                    )
                    u_broker = st.text_input(
                        "금융사", value=str(target["broker"] or "")
                    )
                    u_acc_num = st.text_input(
                        "계좌번호", value=str(target["account_num"] or "")
                    )
                    u_acc_type = st.text_input(
                        "구분", value=str(target["account_type"] or "")
                    )
                with col2:
                    u_item = st.text_input(
                        "보유항목", value=str(target["item_name"] or "")
                    )
                    u_ticker = st.text_input(
                        "티커", value=str(target["ticker"] or "")
                    )
                    u_curr = st.selectbox(
                        "통화",
                        ["KRW (원화)", "USD (달러)"],
                        index=1 if target["currency"] == "USD" else 0,
                    )
                    u_ex_rate = st.number_input(
                        "환율", value=float(target["exchange_rate"] or 1.0)
                    )
                with col3:
                    u_cat1 = st.text_input(
                        "분류1", value=str(target["category1"] or "")
                    )
                    u_cat2 = st.text_input(
                        "분류2", value=str(target["category2"] or "")
                    )
                    u_cat3 = st.text_input(
                        "분류3", value=str(target["category3"] or "")
                    )
                    u_cat4 = st.text_input(
                        "분류4", value=str(target["category4"] or "")
                    )
                with col4:
                    u_buy = st.number_input(
                        "매입단가", value=float(target["buy_price"] or 0.0)
                    )
                    u_qty = st.number_input(
                        "수량", value=float(target["quantity"] or 0.0)
                    )
                    u_curr_p = st.number_input(
                        "현재가", value=float(target["current_price"] or 0.0)
                    )

                if st.form_submit_button("수정 저장"):
                    conn = get_connection()
                    cursor = conn.cursor()
                    cursor.execute(
                        """
                        UPDATE portfolio SET record_date=?, broker=?, account_num=?, account_type=?, item_name=?, ticker=?,
                        category1=?, category2=?, category3=?, category4=?, buy_price=?, quantity=?, current_price=?, currency=?, exchange_rate=?
                        WHERE id=?
                    """,
                        (
                            u_date,
                            u_broker,
                            u_acc_num,
                            u_acc_type,
                            u_item,
                            u_ticker,
                            u_cat1,
                            u_cat2,
                            u_cat3,
                            u_cat4,
                            u_buy,
                            u_qty,
                            u_curr_p,
                            "USD" if "USD" in u_curr else "KRW",
                            u_ex_rate,
                            selected_id,
                        ),
                    )
                    conn.commit()
                    conn.close()
                    st.success("수정되었습니다.")
                    st.rerun()
        else:
            st.info("수정할 데이터가 없습니다.")

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
        col_d1, col_d2 = st.columns([2, 2])
        with col_d1:
            available_dates = sorted(df["record_date"].unique(), reverse=True)
            selected_date = st.selectbox(
                "조회할 입력 데이터 날짜", available_dates
            )

        with col_d2:
            use_historical_price = st.checkbox(
                "🗓️ 특정 날짜 기준 과거 시세로 조회하기"
            )
            if use_historical_price:
                target_eval_date = st.date_input(
                    "조회 기준 시세 날짜", datetime.now()
                ).strftime("%Y-%m-%d")
            else:
                target_eval_date = None

        sub_df = df[df["record_date"] == selected_date].copy()

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
            l1 = st.selectbox("1단계 (최상위)", list(cat_options.keys()), index=0)
        with col_t2:
            l2 = st.selectbox("2단계", ["없음"] + list(cat_options.keys()), index=2)
        with col_t3:
            l3 = st.selectbox("3단계", ["없음"] + list(cat_options.keys()), index=3)
        with col_t4:
            l4 = st.selectbox("4단계 (최하위)", ["없음"] + list(cat_options.keys()), index=0)

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
        if color_option == "총 누적 수익률 (%)":
            color_col = "수익률(%)"
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

                change_rates = []
                for _, row in sub_df.iterrows():
                    f_ticker = row["formatted_ticker"]
                    curr_p = row["current_price"]
                    rate = 0.0

                    if not f_ticker or m_data.empty:
                        change_rates.append(rate)
                        continue

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
                        else:
                            p_price = get_price_from_batch_data(m_data, f_ticker, past_date_str)
                            
                            if p_price is None and not valid_series.empty:
                                p_price = float(valid_series.iloc[0])

                            if curr_p and p_price and float(p_price) > 0:
                                rate = round(((float(curr_p) - float(p_price)) / float(p_price)) * 100, 2)

                    change_rates.append(rate)

                sub_df[color_option] = change_rates
            color_col = color_option

        selected_levels = [l1, l2, l3, l4]
        group_cols = []
        for lvl in selected_levels:
            if lvl != "없음":
                c_name = cat_options[lvl]
                if c_name not in group_cols:
                    group_cols.append(c_name)

        agg_dict = {
            "매입총액(원)": "sum",
            "평가액(원)": "sum",
            "평가손익(원)": "sum",
            "quantity": "sum",
        }
        if color_col != "수익률(%)":
            agg_dict[color_col] = "mean"

        tree_df = (
            sub_df.groupby(group_cols)
            .agg(agg_dict)
            .reset_index()
        )

        tree_df = tree_df[tree_df["평가액(원)"] > 0]

        tree_df["수익률(%)"] = (
            tree_df["평가손익(원)"] / tree_df["매입총액(원)"].replace(0, 1)
        ) * 100

        parent_cat = group_cols[0]
        parent_cat_label = l1

        parent_summary_metric = (
            sub_df.groupby(parent_cat)
            .agg({"평가액(원)": "sum"})
            .reset_index()
        )
        if not parent_summary_metric.empty:
            st.write(f"📌 **[{parent_cat_label}]별 요약 현황**")
            metric_cols = st.columns(len(parent_summary_metric) if len(parent_summary_metric) <= 4 else 4)
            for idx, row in parent_summary_metric.iterrows():
                p_name = row[parent_cat]
                p_val = row["평가액(원)"]
                p_share = (p_val / total_eval * 100) if total_eval != 0 else 0
                col_idx = idx % 4
                with metric_cols[col_idx]:
                    st.metric(
                        label=f"{p_name}",
                        value=f"₩{p_val:,.0f}",
                        delta=f"점유율 {p_share:.2f}%"
                    )
            st.markdown("")

        tree_df[color_col] = tree_df[color_col].fillna(0)

        c_vals = tree_df[color_col]
        max_abs_val = max(abs(c_vals.min()), abs(c_vals.max()), 1.0)
        
        if color_option == "일간 등락률 (1일)":
            dynamic_range = [-min(max_abs_val, 3.0), min(max_abs_val, 3.0)]
        elif color_option in ["주간 등락률 (1주일)", "월간 등락률 (1개월)", "월초 대비 등락률 (Month to Date)"]:
            dynamic_range = [-min(max_abs_val, 15.0), min(max_abs_val, 15.0)]
        else:
            dynamic_range = [-min(max_abs_val, 40.0), min(max_abs_val, 40.0)]

        # TREEMAP 생성
        fig_treemap = px.treemap(
            tree_df,
            path=group_cols,
            values="평가액(원)",
            color=color_col,
            custom_data=[color_col],
            color_continuous_scale=[
                [0.0, "#D32F2F"],   # 음수: 선명한 빨간색
                [0.5, "#455A64"],   # 0 부근: 회색빛 차콜
                [1.0, "#2E7D32"],   # 양수: 선명한 초록색
            ],
            color_continuous_midpoint=0,
            range_color=dynamic_range,
            title="계층별 다단계 TREEMAP 자산 분포",
            branchvalues="total",
        )

        fig_treemap.update_traces(
            texttemplate=(
                "<b>%{label}</b><br>"
                "<span style='font-size: 14px;'><b>₩%{value:,.0f}</b></span><br>"
                "<span style='font-size: 11px;'>점유율: %{percentRoot:.2%}</span><br>"
                "<span style='font-size: 11px;'><b>%{customdata[0]:+.2f}%</b></span>"
            ),
            hovertemplate=(
                "<span style='font-size: 18px;'><b>%{label}</b></span><br>"
                "<span style='font-size: 15px;'>"
                "• 평가금액: ₩%{value:,.0f}<br>"
                f"• {color_option}: %{{customdata[0]:+.2f}}%<br>"
                "• 전체 대비 점유율: %{percentRoot:.2%}<br>"
                "• 상위 그룹 대비 점유율: %{percentParent:.2%}</span><extra></extra>"
            ),
            hoverlabel=dict(font_size=15),
            textfont=dict(color="white"),
            insidetextfont=dict(color="white"),
            selector=dict(type="treemap"),
        )

        fig_treemap.update_layout(
            margin=dict(t=30, l=10, r=10, b=10),
            coloraxis_colorbar=dict(title=color_option),
            treemapcolorway=["#455A64"]
        )

        st.plotly_chart(fig_treemap, width="stretch")

        st.write("📋 **선택 계층(상위 및 하위 그룹)별 평가액 및 전체 점유율 상세 요약**")

        hierarchy_summary = (
            sub_df.groupby(group_cols)
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
            (hierarchy_summary["평가액(원)"] / total_eval * 100) if total_eval != 0 else 0
        )

        col_rename_map = {cat_options[k]: k for k in cat_options if cat_options[k] in group_cols}
        display_df = hierarchy_summary.rename(columns=col_rename_map)

        display_hierarchy_cols = [col_rename_map[c] for c in group_cols]
        final_cols = display_hierarchy_cols + [
            "매입총액(원)",
            "평가액(원)",
            "평가손익(원)",
            "수익률(%)",
            "점유율(%)",
        ]

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

        st.markdown("---")

        st.subheader("📋 선택 시점 상세 보유 목록")
        sub_df["점유율(%)"] = (
            (sub_df["평가액(원)"] / total_eval * 100) if total_eval != 0 else 0
        )
        sub_df["수익률(%)"] = (
            sub_df["평가손익(원)"] / sub_df["매입총액(원)"].replace(0, 1)
        ) * 100
        st.dataframe(
            sub_df[[
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
                    # 계산 실행 시 최신 시세를 수집하도록 캐시 초기화
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

        # ---------------------------------------------------------
        # TREND 연산 결과가 session_state에 존재하는 경우 차트 렌더링
        # ---------------------------------------------------------
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

            # --- Y축 조절 슬라이더 영역 ---
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