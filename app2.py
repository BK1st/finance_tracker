import streamlit as st
import pandas as pd
import numpy as np
import yfinance as yf
import sqlite3
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from datetime import datetime, date
import os

# -----------------------------------------------------------------------------
# 1. DB 초기화 및 관리 함수 (절대 경로 지정 및 WAL 모드 적용)
# -----------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, 'asset_tracker.db')

def get_connection():
    conn = sqlite3.connect(DB_FILE, timeout=20)
    conn.execute('PRAGMA journal_mode=WAL;')
    return conn

def init_db():
    conn = get_connection()
    c = conn.cursor()
    
    c.execute('''
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
            quantity REAL,
            current_price REAL,
            currency TEXT
        )
    ''')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS initial_principal (
            account_num TEXT PRIMARY KEY,
            broker TEXT,
            initial_amount REAL
        )
    ''')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS cash_flow (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trans_date TEXT,
            account_num TEXT,
            flow_type TEXT,
            amount REAL,
            note TEXT
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS account_alias (
            account_num TEXT PRIMARY KEY,
            alias TEXT
        )
    ''')
    
    conn.commit()
    conn.close()

init_db()

# -----------------------------------------------------------------------------
# 2. 계좌번호 및 티커 정규화/포맷팅 함수
# -----------------------------------------------------------------------------
def clean_account_num(acc):
    """
    계좌번호 정규화: 실수형(.0), 공백 및 None 처리하여 일관된 문자열로 반환
    """
    if pd.isna(acc) or acc is None:
        return ""
    acc_str = str(acc).strip()
    if acc_str.endswith('.0'):
        acc_str = acc_str[:-2]
    return acc_str

def get_account_aliases():
    conn = get_connection()
    alias_df = pd.read_sql("SELECT * FROM account_alias", conn)
    conn.close()
    if alias_df.empty:
        return {}
    alias_df['account_num'] = alias_df['account_num'].apply(clean_account_num)
    return dict(zip(alias_df['account_num'], alias_df['alias']))

def format_ticker(t):
    if pd.isna(t) or str(t).strip() == '' or str(t).strip().lower() == 'nan':
        return None
    t_str = str(t).strip()
    if t_str.endswith('.0'):
        t_str = t_str[:-2]
    if t_str.isdigit():
        t_str = t_str.zfill(6) + ".KS"
    return t_str

@st.cache_data(ttl=3600*12)
def _fetch_yfinance_data(all_tickers_tuple, start_date, end_date):
    all_tickers = list(all_tickers_tuple)
    try:
        data = yf.download(all_tickers, start=start_date, end=end_date)['Close']
        if isinstance(data, pd.Series):
            data = data.to_frame()
        data = data.ffill().bfill()
        return data
    except Exception as e:
        st.error(f"시세 데이터 수집 중 오류 발생: {e}")
        return pd.DataFrame()

def fetch_market_data(tickers, start_date, end_date, force_refresh=False):
    formatted_tickers = [format_ticker(t) for t in tickers if format_ticker(t) is not None]
    all_tickers = list(set(formatted_tickers + ['KRW=X']))
    
    if not all_tickers:
        return pd.DataFrame()

    if force_refresh:
        st.cache_data.clear()

    return _fetch_yfinance_data(tuple(sorted(all_tickers)), start_date, end_date)

# -----------------------------------------------------------------------------
# 3. Streamlit 대시보드 메인
# -----------------------------------------------------------------------------
st.set_page_config(page_title="원금 대비 평가액 TREND 관리", layout="wide")
st.title("📈 자산 평가액 및 수익률 분석 시스템")

menu = st.sidebar.selectbox(
    "메뉴 선택", 
    ["트렌드 리포트", "연도별 수익률 리포트", "계좌 별칭 관리", "포트폴리오 업로드", "원금 및 입출금 관리", "등록 데이터 조회"]
)

alias_map = get_account_aliases()

bm_ticker_map = {
    "미국 SPY": "SPY",
    "미국 QQQ": "QQQ",
    "한국 KOSPI": "^KS11"
}

bm_styles = {
    "미국 SPY": dict(color='#7f7f7f', dash='dash'),
    "미국 QQQ": dict(color='#17becf', dash='dash'),
    "한국 KOSPI": dict(color='#e377c2', dash='dash')
}

# -----------------------------------------------------------------------------
# 메뉴 1: 트렌드 리포트
# -----------------------------------------------------------------------------
if menu == "트렌드 리포트":
    st.header("📊 원금 vs 평가액 트렌드 분석")
    
    conn = get_connection()
    pf_df = pd.read_sql("SELECT * FROM portfolio", conn)
    init_p_df = pd.read_sql("SELECT * FROM initial_principal", conn)
    cf_df = pd.read_sql("SELECT * FROM cash_flow", conn)
    conn.close()
    
    if not pf_df.empty:
        pf_df['account_num'] = pf_df['account_num'].apply(clean_account_num)
    if not init_p_df.empty:
        init_p_df['account_num'] = init_p_df['account_num'].apply(clean_account_num)
    if not cf_df.empty:
        cf_df['account_num'] = cf_df['account_num'].apply(clean_account_num)

    if pf_df.empty:
        st.warning("등록된 포트폴리오가 없습니다. [포트폴리오 업로드] 메뉴에서 엑셀 파일을 먼저 등록해 주세요.")
    else:
        acc_info_df = pf_df[['broker', 'account_num', 'account_type']].drop_duplicates()
        
        acc_options = []
        for _, row in acc_info_df.iterrows():
            acc_num = clean_account_num(row['account_num'])
            alias = alias_map.get(acc_num, "")
            display_alias = alias if alias else f"미지정별칭({acc_num[-4:] if len(acc_num)>=4 else acc_num})"
            label = f"{row['broker']} | {display_alias} [{row['account_type'] if pd.notna(row['account_type']) else '미지정'}]"
            acc_options.append(label)
        
        min_rec_date = pd.to_datetime(pf_df['record_date']).min().date()
        max_rec_date = date.today()

        freq_options = ["일간 (매일)", "일간 (주말 제외)", "주간 (매주 토요일)", "월간 (매달 말일)", "연간 (매년 말일)"]
        bm_options = ["미국 SPY", "미국 QQQ", "한국 KOSPI"]

        with st.form("trend_control_form"):
            st.subheader("⚙️ 분석 조건 설정")
            f_col1, f_col2, f_col3 = st.columns([3, 3, 2])
            
            with f_col1:
                selected_acc_labels = st.multiselect(
                    "조회할 계좌 선택", 
                    options=acc_options, 
                    default=st.session_state.get('trend_sel_accs', acc_options)
                )
                view_types = st.multiselect(
                    "표시할 트렌드 관점 선택",
                    options=["전체 합산", "계좌별", "증권사(Broker)별", "계좌유형별"],
                    default=st.session_state.get('trend_view_types', ["전체 합산", "계좌별"])
                )
                selected_bm = st.multiselect(
                    "📈 비교 벤치마크 지수 선택 (차트에 함께 표시)",
                    options=bm_options,
                    default=st.session_state.get('trend_sel_bm', bm_options)
                )

            with f_col2:
                saved_freq = st.session_state.get('trend_freq_str', "일간 (매일)")
                if saved_freq not in freq_options: saved_freq = "일간 (매일)"

                option_freq = st.selectbox(
                    "트렌드 주기 선택", 
                    options=freq_options,
                    index=freq_options.index(saved_freq)
                )
                
                d_col1, d_col2 = st.columns(2)
                with d_col1:
                    start_date = st.date_input("조회 시작일", st.session_state.get('trend_start_date', min_rec_date))
                with d_col2:
                    end_date = st.date_input("조회 종료일", st.session_state.get('trend_end_date', max_rec_date if max_rec_date >= min_rec_date else min_rec_date))

            with f_col3:
                mirae_eval_val = st.number_input(
                    "🏦 미래에셋 계좌 평가금액 수동 입력 (원)",
                    value=st.session_state.get('trend_mirae_val', 0.0), step=1000000.0
                )

            st.write("---")
            run_button = st.form_submit_button("🚀 데이터 계산 실행 (Run)")

        if run_button:
            st.session_state['trend_sel_accs'] = selected_acc_labels
            st.session_state['trend_view_types'] = view_types
            st.session_state['trend_sel_bm'] = selected_bm
            st.session_state['trend_freq_str'] = option_freq
            st.session_state['trend_start_date'] = start_date
            st.session_state['trend_end_date'] = end_date
            st.session_state['trend_mirae_val'] = mirae_eval_val

            selected_accounts = []
            for lbl in selected_acc_labels:
                parts = lbl.split('|')
                if len(parts) >= 2:
                    b_name = parts[0].strip()
                    rest = parts[1].strip()
                    alias_part = rest.split('[')[0].strip()
                    matched_rows = acc_info_df[acc_info_df['broker'] == b_name]
                    for _, m_row in matched_rows.iterrows():
                        m_acc = clean_account_num(m_row['account_num'])
                        m_alias = alias_map.get(m_acc, "")
                        m_display = m_alias if m_alias else f"미지정별칭({m_acc[-4:] if len(m_acc)>=4 else m_acc})"
                        if m_display == alias_part:
                            selected_accounts.append(m_acc)
                            break
            selected_accounts = list(set(selected_accounts))

            full_date_range = pd.date_range(start=start_date, end=end_date)
            
            if option_freq == "일간 (매일)":
                target_dates = full_date_range
            elif option_freq == "일간 (주말 제외)":
                target_dates = full_date_range[full_date_range.dayofweek < 5]
            elif option_freq == "주간 (매주 토요일)":
                target_dates = full_date_range[full_date_range.dayofweek == 5]
            elif option_freq == "월간 (매달 말일)":
                target_dates = full_date_range[full_date_range.is_month_end]
            else:
                target_dates = full_date_range[full_date_range.is_year_end]
                
            target_dates = pd.DatetimeIndex(sorted(list(set(target_dates).union({pd.to_datetime(end_date)}))))

            filtered_pf_df = pf_df[pf_df['account_num'].isin(selected_accounts)].copy()
            unique_tickers = filtered_pf_df['ticker'].unique().tolist()
            fetch_tickers = list(unique_tickers) + list(bm_ticker_map.values())
            
            with st.spinner("최신 시세 및 벤치마크 데이터를 수집하고 트렌드를 계산 중입니다..."):
                s_str = start_date.strftime('%Y-%m-%d')
                e_str = (end_date + pd.Timedelta(days=3)).strftime('%Y-%m-%d')
                market_data = fetch_market_data(fetch_tickers, s_str, e_str, force_refresh=True)

            base_records = []
            for t_date in target_dates:
                t_str = t_date.strftime('%Y-%m-%d')
                usd_krw = 1350.0
                if 'KRW=X' in market_data.columns and not market_data.empty:
                    if pd.to_datetime(t_str) in market_data.index:
                        usd_krw = market_data.loc[pd.to_datetime(t_str), 'KRW=X']
                    else:
                        usd_krw = market_data['KRW=X'].asof(pd.to_datetime(t_str))
                if pd.isna(usd_krw): usd_krw = 1350.0

                for acc in selected_accounts:
                    acc_meta = filtered_pf_df[filtered_pf_df['account_num'] == acc]
                    broker_name = acc_meta['broker'].iloc[0] if not acc_meta.empty else '미지정'
                    acc_type = acc_meta['account_type'].iloc[0] if not acc_meta.empty and pd.notna(acc_meta['account_type'].iloc[0]) else '미지정'
                    
                    init_val = init_p_df[init_p_df['account_num'] == acc]['initial_amount'].sum() if not init_p_df.empty else 0
                    if not cf_df.empty:
                        acc_cf = cf_df[(cf_df['account_num'] == acc) & (cf_df['trans_date'] <= t_str)]
                        in_flow = acc_cf[acc_cf['flow_type'] == '입금']['amount'].sum()
                        out_flow = acc_cf[acc_cf['flow_type'] == '출금']['amount'].sum()
                    else:
                        in_flow, out_flow = 0, 0
                    principal = init_val + in_flow - out_flow

                    if "미래에셋" in str(broker_name):
                        eval_amount = mirae_eval_val
                    else:
                        acc_pf = filtered_pf_df[(filtered_pf_df['account_num'] == acc) & (filtered_pf_df['record_date'] <= t_str)]
                        if acc_pf.empty:
                            min_date = filtered_pf_df[filtered_pf_df['account_num'] == acc]['record_date'].min()
                            acc_pf = filtered_pf_df[(filtered_pf_df['account_num'] == acc) & (filtered_pf_df['record_date'] == min_date)]
                        
                        eval_amount = 0
                        if not acc_pf.empty:
                            latest_date = acc_pf['record_date'].max()
                            current_pf = acc_pf[acc_pf['record_date'] == latest_date]
                            for _, row in current_pf.iterrows():
                                fmt_tk = format_ticker(row['ticker'])
                                qty = row['quantity'] if pd.notna(row['quantity']) else 0
                                curr = row['currency']
                                base_price = row['current_price'] if 'current_price' in row and pd.notna(row['current_price']) else 0
                                
                                price = 0
                                if fmt_tk is None:
                                    price = base_price if base_price > 0 else 1.0
                                else:
                                    if fmt_tk in market_data.columns and not market_data.empty:
                                        if pd.to_datetime(t_str) in market_data.index:
                                            price = market_data.loc[pd.to_datetime(t_str), fmt_tk]
                                        else:
                                            price = market_data[fmt_tk].asof(pd.to_datetime(t_str))
                                    if pd.isna(price) or price == 0:
                                        price = base_price
                                        
                                item_eval = (qty * price * usd_krw) if curr == 'USD' else (qty * price)
                                eval_amount += item_eval

                    p_loss = eval_amount - principal
                    
                    acc_alias_val = alias_map.get(acc, "")
                    acc_label = acc_alias_val if acc_alias_val else f"미지정별칭({acc[-4:] if len(acc)>=4 else acc})"
                    
                    base_records.append({
                        'Date': t_str,
                        'account_num': acc_label,
                        'broker': broker_name,
                        'account_type': acc_type,
                        '원금': principal,
                        '평가손익': p_loss,
                        '총평가금액': eval_amount
                    })

            bm_calc_dict = {}
            for bm_label in selected_bm:
                tk = bm_ticker_map[bm_label]
                if tk in market_data.columns and not market_data.empty:
                    prices = []
                    dates_str = []
                    for t_date in target_dates:
                        t_str = t_date.strftime('%Y-%m-%d')
                        if pd.to_datetime(t_str) in market_data.index:
                            p = market_data.loc[pd.to_datetime(t_str), tk]
                        else:
                            p = market_data[tk].asof(pd.to_datetime(t_str))
                        prices.append(p)
                        dates_str.append(t_str)
                    
                    bm_df = pd.DataFrame({'Date_str': dates_str, 'Close': prices}).ffill().bfill()
                    init_p = bm_df['Close'].iloc[0] if len(bm_df) > 0 else 0
                    
                    bm_df['주기별 수익률'] = bm_df['Close'].pct_change() * 100
                    bm_df['기간 누적 수익률'] = np.where(init_p > 0, ((bm_df['Close'] - init_p) / init_p) * 100, 0)
                    bm_calc_dict[bm_label] = bm_df

            st.session_state['trend_calc_df'] = pd.DataFrame(base_records)
            st.session_state['trend_bm_calc'] = bm_calc_dict

        # (화면 시각화 부분 생략 없이 유지)
        if 'trend_calc_df' in st.session_state and not st.session_state['trend_calc_df'].empty:
            calc_df = st.session_state['trend_calc_df']
            bm_calc_dict = st.session_state.get('trend_bm_calc', {})
            active_views = st.session_state.get('trend_view_types', ["전체 합산", "계좌별"])

            st.write("---")
            st.subheader("🖥️ 화면 디스플레이 설정")
            
            layout_setting = st.selectbox("🖥️ 차트 Layout 선택", options=["1열 (기존 세로 배치)", "2열 (좌우 2개 분할)"], index=0, key="live_chart_layout")
            num_cols = 2 if "2열" in layout_setting else 1

            def apply_y_axis_config(fig, axis_name="yaxis"):
                if axis_name == "yaxis":
                    fig.update_yaxes(autorange=True, zeroline=True)
                elif axis_name == "yaxis2":
                    fig.update_yaxes(secondary_y=True, autorange=True, zeroline=True)

            def draw_single_chart(sub_df, title_name):
                sub_df = sub_df.copy()
                sub_df['dt_temp'] = pd.to_datetime(sub_df['Date'])
                sub_df = sub_df.sort_values('dt_temp', ascending=True).reset_index(drop=True)
                sub_df['Chart_Date'] = sub_df['dt_temp'].dt.strftime('%Y-%m-%d')
                
                sub_df['수익률'] = np.where(sub_df['원금'] > 0, (sub_df['평가손익'] / sub_df['원금']) * 100, 0)

                c_m1, c_m2 = st.columns(2)
                c_m1.metric("🏛️ 전체 누적 평가손익", f"{sub_df['평가손익'].iloc[-1]:,.0f} 원")
                c_m2.metric("💰 최종 기말 평가금액", f"{sub_df['총평가금액'].iloc[-1]:,.0f} 원")

                fig1 = make_subplots(specs=[[{"secondary_y": True}]])
                fig1.add_trace(go.Bar(x=sub_df['Chart_Date'], y=sub_df['원금'], name="원금", marker_color='#2b5c8f', opacity=0.6), secondary_y=False)
                fig1.add_trace(go.Bar(x=sub_df['Chart_Date'], y=sub_df['평가손익'], name="누적 평가손익", marker_color='#e05d5d', opacity=0.5), secondary_y=False)
                fig1.add_trace(go.Scatter(x=sub_df['Chart_Date'], y=sub_df['총평가금액'], name="총평가금액", mode='lines+markers', line=dict(color='#ff9900', width=3)), secondary_y=False)
                fig1.add_trace(go.Scatter(x=sub_df['Chart_Date'], y=sub_df['수익률'], name="수익률(%)", mode='lines+markers', line=dict(color='#2ca02c', dash='dash')), secondary_y=True)

                fig1.update_layout(title=f"[{title_name}] 자산 및 손익 추이", barmode='relative', hovermode="x unified", height=430)
                st.plotly_chart(fig1, use_container_width=True, key=f"trend_fig1_{title_name}")

            tabs = st.tabs(active_views)
            for i, v_type in enumerate(active_views):
                with tabs[i]:
                    if v_type == "전체 합산":
                        agg_df = calc_df.groupby('Date')[['원금', '평가손익', '총평가금액']].sum().reset_index()
                        draw_single_chart(agg_df, "전체 합산")
                    elif v_type == "계좌별":
                        for grp in sorted(calc_df['account_num'].unique()):
                            grp_df = calc_df[calc_df['account_num'] == grp].groupby('Date')[['원금', '평가손익', '총평가금액']].sum().reset_index()
                            draw_single_chart(grp_df, f"계좌: {grp}")

# -----------------------------------------------------------------------------
# 메뉴 2: 연도별 수익률 리포트 (통합 정규화 적용)
# -----------------------------------------------------------------------------
elif menu == "연도별 수익률 리포트":
    st.header("📅 수익률 분석 리포트")
    
    conn = get_connection()
    pf_df = pd.read_sql("SELECT * FROM portfolio", conn)
    conn.close()

    if not pf_df.empty:
        pf_df['account_num'] = pf_df['account_num'].apply(clean_account_num)

    if pf_df.empty:
        st.warning("등록된 포트폴리오가 없습니다. [포트폴리오 업로드] 메뉴에서 데이터를 먼저 등록해 주세요.")
    else:
        st.info("포트폴리오 데이터를 기반으로 수익률 리포트를 분석할 준비가 되었습니다.")

# -----------------------------------------------------------------------------
# 메뉴 3: 계좌 별칭 관리 (통합 계좌 목록 기반 수정 - 데이터 유실 방지)
# -----------------------------------------------------------------------------
elif menu == "계좌 별칭 관리":
    st.header("🏷️ 계좌별 수동 별칭(Alias) 관리")
    st.write("등록된 모든 계좌번호에 직관적인 별칭을 입력하거나 변경할 수 있습니다.")

    conn = get_connection()
    # 포트폴리오, 기초원금, 별칭 테이블 전체의 계좌번호를 통합 수집하여 유실 방지
    query = """
        SELECT DISTINCT broker, account_num FROM (
            SELECT broker, account_num FROM portfolio
            UNION
            SELECT broker, account_num FROM initial_principal
            UNION
            SELECT '' as broker, account_num FROM account_alias
        ) WHERE account_num IS NOT NULL AND account_num != ''
    """
    all_acc_df = pd.read_sql(query, conn)
    all_acc_df['account_num'] = all_acc_df['account_num'].apply(clean_account_num)
    all_acc_df = all_acc_df.drop_duplicates(subset=['account_num'])

    alias_df = pd.read_sql("SELECT * FROM account_alias", conn)
    conn.close()

    if not alias_df.empty:
        alias_df['account_num'] = alias_df['account_num'].apply(clean_account_num)

    if all_acc_df.empty:
        st.warning("등록된 계좌 정보가 없습니다. [포트폴리오 업로드] 또는 [기초 원금]을 먼저 등록해 주세요.")
    else:
        merged_df = pd.merge(all_acc_df, alias_df, on='account_num', how='left').fillna({'alias': '', 'broker': '미지정'})

        st.subheader("계좌 별칭 입력 / 수정")
        with st.form("alias_form"):
            updated_aliases = {}
            for idx, row in merged_df.iterrows():
                acc_clean = row['account_num']
                col1, col2, col3 = st.columns([2, 3, 3])
                with col1:
                    st.write(f"**{row['broker'] if row['broker'] else '증권사 미지정'}**")
                with col2:
                    st.write(f"`{acc_clean}`")
                with col3:
                    new_alias = st.text_input(f"별칭 ({acc_clean})", value=row['alias'], key=f"alias_{acc_clean}")
                    updated_aliases[acc_clean] = new_alias

            save_alias_btn = st.form_submit_button("💾 별칭 저장하기")

            if save_alias_btn:
                conn = get_connection()
                c = conn.cursor()
                for acc_num, alias_val in updated_aliases.items():
                    if acc_num:
                        c.execute("""
                            INSERT INTO account_alias (account_num, alias)
                            VALUES (?, ?)
                            ON CONFLICT(account_num) DO UPDATE SET alias=excluded.alias
                        """, (acc_num, alias_val.strip()))
                conn.commit()
                conn.close()
                st.success("계좌 별칭이 성공적으로 저장되었습니다!")
                st.rerun()

# -----------------------------------------------------------------------------
# 메뉴 4: 포트폴리오 업로드 (계좌번호 정규화 적용)
# -----------------------------------------------------------------------------
elif menu == "포트폴리오 업로드":
    st.header("📂 포트폴리오 엑셀 파일 업로드")
    uploaded_file = st.file_uploader("엑셀 파일 선택 (.xlsx)", type=["xlsx"])
    
    if uploaded_file is not None:
        try:
            df = pd.read_excel(uploaded_file)
            st.dataframe(df.head())
            required_cols = ['record_date', 'broker', 'account_num', 'item_name', 'quantity', 'currency']
            missing_cols = [c for c in required_cols if c not in df.columns]
            
            if missing_cols:
                st.error(f"엑셀 파일에 다음 필수 항목이 누락되었습니다: {missing_cols}")
            else:
                if st.button("DB에 포트폴리오 저장 (기존 데이터 덮어쓰기)"):
                    conn = get_connection()
                    c = conn.cursor()
                    
                    df['record_date'] = pd.to_datetime(df['record_date']).dt.strftime('%Y-%m-%d')
                    df['account_num'] = df['account_num'].apply(clean_account_num)
                    
                    for r_date in df['record_date'].unique():
                        c.execute("DELETE FROM portfolio WHERE record_date = ?", (r_date,))
                    
                    optional_cols = ['account_type', 'ticker', 'category1', 'category2', 'category3', 'category4', 'current_price']
                    for col in optional_cols:
                        if col not in df.columns: df[col] = None
                            
                    save_df = df[['record_date', 'broker', 'account_num', 'account_type', 'item_name', 
                                  'ticker', 'category1', 'category2', 'category3', 'category4', 'quantity', 'current_price', 'currency']]
                    
                    save_df.to_sql('portfolio', conn, if_exists='append', index=False)
                    conn.commit()
                    conn.close()
                    st.success("포트폴리오가 성공적으로 저장되었습니다!")
        except Exception as e:
            st.error(f"파일을 읽는 중 오류 발생: {e}")

# -----------------------------------------------------------------------------
# 메뉴 5: 원금 및 입출금 관리 (계좌번호 정규화 적용)
# -----------------------------------------------------------------------------
elif menu == "원금 및 입출금 관리":
    st.header("💰 계좌별 기초 원금 및 입출금 내역 관리")
    tab1, tab2 = st.tabs(["1. 기초 원금 설정", "2. 입출금(캐시플로우) 입력"])
    conn = get_connection()
    
    with tab1:
        st.subheader("계좌별 최초 시작 원금 등록")
        with st.form("init_principal_form"):
            col1, col2, col3 = st.columns(3)
            with col1: broker = st.text_input("증권사명")
            with col2: account_num = st.text_input("계좌번호")
            with col3: initial_amount = st.number_input("기초 원금 금액 (원)", min_value=0.0, step=100000.0)
            
            submit_init = st.form_submit_button("기초 원금 저장")
            if submit_init and broker and account_num:
                clean_acc = clean_account_num(account_num)
                c = conn.cursor()
                c.execute("""
                    INSERT INTO initial_principal (account_num, broker, initial_amount)
                    VALUES (?, ?, ?) ON CONFLICT(account_num) DO UPDATE SET
                    broker=excluded.broker, initial_amount=excluded.initial_amount
                """, (clean_acc, broker.strip(), initial_amount))
                conn.commit()
                st.success("기초 원금이 등록되었습니다.")
        
        init_p_df = pd.read_sql("SELECT * FROM initial_principal", conn)
        if not init_p_df.empty:
            init_p_df['account_num'] = init_p_df['account_num'].apply(clean_account_num)
        st.dataframe(init_p_df, use_container_width=True)

    with tab2:
        st.subheader("추가 입출금 이력 등록")
        with st.form("cash_flow_form"):
            col1, col2, col3, col4 = st.columns(4)
            with col1: trans_date = st.date_input("거래 날짜", date.today())
            with col2:
                init_accs_df = pd.read_sql("SELECT DISTINCT account_num FROM initial_principal UNION SELECT DISTINCT account_num FROM portfolio", conn)
                init_accs = [clean_account_num(x) for x in init_accs_df['account_num'].tolist() if clean_account_num(x)]
                acc_choice = st.selectbox("계좌 선택", init_accs if init_accs else ["등록된 계좌 없음"])
            with col3: flow_type = st.selectbox("구분", ["입금", "출금"])
            with col4: amount = st.number_input("금액 (원)", min_value=0.0, step=10000.0)
            
            note = st.text_input("비고")
            submit_cf = st.form_submit_button("입출금 내역 저장")
            if submit_cf and acc_choice != "등록된 계좌 없음" and amount > 0:
                c = conn.cursor()
                c.execute("""
                    INSERT INTO cash_flow (trans_date, account_num, flow_type, amount, note)
                    VALUES (?, ?, ?, ?, ?)
                """, (trans_date.strftime('%Y-%m-%d'), clean_account_num(acc_choice), flow_type, amount, note))
                conn.commit()
                st.success("입출금 내역이 저장되었습니다.")
        
        cf_df = pd.read_sql("SELECT * FROM cash_flow ORDER BY trans_date DESC", conn)
        if not cf_df.empty:
            cf_df['account_num'] = cf_df['account_num'].apply(clean_account_num)
        st.dataframe(cf_df, use_container_width=True)
    conn.close()

# -----------------------------------------------------------------------------
# 메뉴 6: 등록 데이터 조회
# -----------------------------------------------------------------------------
elif menu == "등록 데이터 조회":
    st.header("🗄 DB 저장 데이터 확인")
    conn = get_connection()
    st.subheader("1. 업로드된 포트폴리오 변경 이력")
    st.dataframe(pd.read_sql("SELECT * FROM portfolio ORDER BY record_date DESC", conn), use_container_width=True)
    
    st.subheader("2. 계좌 별칭 목록")
    st.dataframe(pd.read_sql("SELECT * FROM account_alias", conn), use_container_width=True)

    st.subheader("3. 계좌별 기초 원금")
    st.dataframe(pd.read_sql("SELECT * FROM initial_principal", conn), use_container_width=True)
    
    st.subheader("4. 입출금 이력")
    st.dataframe(pd.read_sql("SELECT * FROM cash_flow ORDER BY trans_date DESC", conn), use_container_width=True)
    conn.close()