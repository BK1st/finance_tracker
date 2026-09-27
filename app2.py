import os
import sqlite3
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
import yfinance as yf
from plotly.subplots import make_subplots

# -----------------------------------------------------------------------------
# 1. DB 초기화 및 관리 함수
# -----------------------------------------------------------------------------
DATA_DIR = os.path.join(os.path.dirname(__file__), '.data')
os.makedirs(DATA_DIR, exist_ok=True)
DB_FILE = os.path.join(DATA_DIR, 'asset_tracker.db')


def init_db():
  conn = sqlite3.connect(DB_FILE)
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


def get_connection():
  return sqlite3.connect(DB_FILE)


def get_account_aliases():
  conn = get_connection()
  alias_df = pd.read_sql('SELECT * FROM account_alias', conn)
  conn.close()
  return dict(zip(alias_df['account_num'], alias_df['alias']))


# -----------------------------------------------------------------------------
# 2. 티커 포맷팅 및 시세 수집 함수
# -----------------------------------------------------------------------------
def format_ticker(t):
  if pd.isna(t) or str(t).strip() == '' or str(t).strip().lower() == 'nan':
    return None
  t_str = str(t).strip()
  if t_str.endswith('.0'):
    t_str = t_str[:-2]
  if t_str.isdigit():
    t_str = t_str.zfill(6) + '.KS'
  return t_str


def get_latest_price_single(ticker_symbol):
  try:
    tk = yf.Ticker(ticker_symbol)
    fast_info = tk.fast_info
    if hasattr(fast_info, 'last_price') and fast_info.last_price is not None:
      if not np.isnan(fast_info.last_price) and fast_info.last_price > 0:
        return float(fast_info.last_price)

    info = tk.info
    if 'postMarketPrice' in info and info['postMarketPrice']:
      return float(info['postMarketPrice'])
    if 'regularMarketPrice' in info and info['regularMarketPrice']:
      return float(info['regularMarketPrice'])

    hist = tk.history(period='5d')
    if not hist.empty and 'Close' in hist.columns:
      return float(hist['Close'].iloc[-1])
  except Exception:
    pass
  return None


@st.cache_data(ttl=900)
def _fetch_yfinance_data(all_tickers_tuple, start_date, end_date):
  all_tickers = list(all_tickers_tuple)
  if not all_tickers:
    return pd.DataFrame()
  try:
    df = yf.download(
        all_tickers,
        start=start_date,
        end=end_date,
        progress=False,
        ignore_tz=True,
    )
    if df.empty:
      return pd.DataFrame()

    if 'Close' in df:
      data = df['Close']
    elif 'Adj Close' in df:
      data = df['Adj Close']
    else:
      data = df

    if isinstance(data, pd.Series):
      data = data.to_frame(name=all_tickers[0])

    data = data.ffill()

    today_str = datetime.now().strftime('%Y-%m-%d')
    today_dt = pd.to_datetime(today_str)

    latest_row = {}
    for tk_sym in all_tickers:
      lp = get_latest_price_single(tk_sym)
      if lp is not None:
        latest_row[tk_sym] = lp

    if latest_row:
      if today_dt in data.index:
        for k, v in latest_row.items():
          data.loc[today_dt, k] = v
      else:
        new_row_df = pd.DataFrame(latest_row, index=[today_dt])
        data = pd.concat([data, new_row_df])
        data = data.sort_index().ffill()

    return data
  except Exception as e:
    st.error(f'시세 데이터 수집 중 오류 발생: {e}')
    return pd.DataFrame()


def fetch_market_data(tickers, start_date, end_date, force_refresh=False):
  formatted_tickers = [
      format_ticker(t) for t in tickers if format_ticker(t) is not None
  ]
  all_tickers = list(set(formatted_tickers + ['KRW=X']))

  if not all_tickers:
    return pd.DataFrame()

  if force_refresh:
    st.cache_data.clear()

  return _fetch_yfinance_data(tuple(sorted(all_tickers)), start_date, end_date)


# -----------------------------------------------------------------------------
# 3. Streamlit 대시보드 메인 설정 및 CSS (크기 조절 및 반응형 설정)
# -----------------------------------------------------------------------------
st.set_page_config(page_title='원금 대비 평가액 TREND 관리', layout='wide')

# 요구사항 2 반영: 차트 우측 하단 드래그를 통한 리사이즈 및 레이아웃 밀림 방지 CSS 설정
st.markdown(
    """
    <style>
    /* Streamlit Plotly 차트 컨테이너 드래그 조절 가능 처리 */
    div[data-testid="stPlotlyChart"] {
        resize: both !important;
        overflow: auto !important;
        min-height: 450px;
        min-width: 300px;
        max-width: 100%;
        padding: 12px;
        border: 1px solid #e2e8f0;
        border-radius: 10px;
        background-color: #ffffff;
        display: block !important;
        position: relative !important;
        margin-bottom: 24px !important;
        box-sizing: border-box !important;
    }
    div[data-testid="stPlotlyChart"] > div {
        height: 100% !important;
        width: 100% !important;
    }
    </style>
""",
    unsafe_allow_html=True,
)

st.title('📈 자산 평가액 및 수익률 분석 시스템')

menu = st.sidebar.selectbox(
    '메뉴 선택',
    [
        '트렌드 리포트',
        '계좌 별칭 관리',
        '포트폴리오 업로드',
        '원금 및 입출금 관리',
        '등록 데이터 조회',
    ],
)

alias_map = get_account_aliases()

bm_ticker_map = {
    '미국 SPY': 'SPY',
    '미국 QQQ': 'QQQ',
    '한국 KOSPI': '^KS11',
}

bm_styles = {
    '미국 SPY': dict(color='#7f7f7f', dash='dash'),
    '미국 QQQ': dict(color='#17becf', dash='dash'),
    '한국 KOSPI': dict(color='#e377c2', dash='dash'),
}

PLOTLY_CONFIG = {
    'responsive': True,
    'scrollZoom': True,
    'displayModeBar': True,
    'modeBarButtonsToAdd': ['drawline', 'drawopenpath', 'eraseshape'],
}

DEFAULT_PALETTE = px.colors.qualitative.Plotly + px.colors.qualitative.Set1


def apply_chart_standard_layout(fig, title_text):
  """요구사항 1 반영: 차트와 범주의 완전 물리적 분리 레이아웃 함수

  - orientation='h' : 가로형 범주 배치
  - y=-0.3, yanchor='top' : 차트의 x축 아래 바깥쪽 영역으로 범주를 밀어냄
  - margin(b=140) : 범주 항목이 늘어나 줄바꿈되어도 차트를 가리지 않도록 하단 여백 대폭 확보
  """
  fig.update_layout(
      showlegend=True,
      legend=dict(
          orientation='h',
          yanchor='top',
          y=-0.3,  # 차트 시각화 영역 밑으로 완전 분리
          xanchor='center',
          x=0.5,
          itemclick='toggle',
          itemdoubleclick='toggleothers',
          bgcolor='rgba(255, 255, 255, 0.9)',
          bordercolor='#e5e7eb',
          borderwidth=1,
      ),
      hovermode='closest',
      title=dict(
          text=title_text,
          x=0.0,
          xanchor='left',
          y=0.98,
          font=dict(size=15, color='#1f2937'),
      ),
      margin=dict(
          t=50, b=140, l=20, r=20
      ),  # 하단 여백을 충분히 확보하여 차트 축 및 그래프 훼손 방지
      autosize=True,
  )


def render_chart_with_click_event(fig, chart_key, calc_df=None):
  """Plotly 차트 출력 함수 (컨테이너 크기 변경 시 자동 재렌더링 적용)"""
  st.plotly_chart(
      fig, use_container_width=True, config=PLOTLY_CONFIG, key=chart_key
  )


# -----------------------------------------------------------------------------
# 메뉴 1: 트렌드 리포트
# -----------------------------------------------------------------------------
if menu == '트렌드 리포트':
  st.header('📊 원금 vs 평가액 트렌드 분석')

  conn = get_connection()
  pf_df = pd.read_sql('SELECT * FROM portfolio', conn)
  init_p_df = pd.read_sql('SELECT * FROM initial_principal', conn)
  cf_df = pd.read_sql('SELECT * FROM cash_flow', conn)
  conn.close()

  if pf_df.empty:
    st.warning(
        '등록된 포트폴리오가 없습니다. [포트폴리오 업로드] 메뉴에서 엑셀'
        ' 파일을 먼저 등록해 주세요.'
    )
  else:
    acc_info_df = pf_df[
        ['broker', 'account_num', 'account_type']
    ].drop_duplicates()

    acc_options = []
    for _, row in acc_info_df.iterrows():
      acc_num = str(row['account_num'])
      alias = alias_map.get(acc_num, '')
      display_alias = (
          alias
          if alias
          else f'미지정별칭({acc_num[-4:] if len(acc_num)>=4 else acc_num})'
      )
      acc_type_str = (
          row['account_type']
          if ('account_type' in row and pd.notna(row['account_type']))
          else '미지정'
      )
      broker_str = (
          row['broker']
          if ('broker' in row and pd.notna(row['broker']))
          else '증권사미지정'
      )
      label = f'{broker_str} | {display_alias} [{acc_type_str}]'
      acc_options.append(label)

    min_rec_date = pd.to_datetime(pf_df['record_date']).min().date()
    max_rec_date = date.today()

    today = date.today()
    first_day_of_curr_month = date(today.year, today.month, 1)
    prev_month_end = first_day_of_curr_month - timedelta(days=1)

    freq_options = [
        '일간 (매일)',
        '일간 (주말 제외)',
        '주간 (매주 토요일)',
        '월간 (매달 말일)',
        '연간 (매년 말일)',
    ]
    bm_options = ['미국 SPY', '미국 QQQ', '한국 KOSPI']

    all_view_types = [
        '전체 합산',
        '계좌별',
        '증권사(Broker)별',
        '계좌유형별',
        'Category 4별',
        '보유항목별',
    ]

    with st.form('trend_control_form'):
      st.subheader('⚙️ 분석 조건 설정')
      f_col1, f_col2, f_col3 = st.columns([3, 3, 2])

      with f_col1:
        selected_acc_labels = st.multiselect(
            '조회할 계좌 선택',
            options=acc_options,
            default=st.session_state.get('trend_sel_accs', acc_options),
        )
        view_types = st.multiselect(
            '표시할 트렌드 관점 선택',
            options=all_view_types,
            default=st.session_state.get('trend_view_types', all_view_types),
        )
        selected_bm = st.multiselect(
            '📈 비교 벤치마크 지수 선택 (차트에 함께 표시)',
            options=bm_options,
            default=st.session_state.get('trend_sel_bm', bm_options),
        )

      with f_col2:
        saved_freq = st.session_state.get('trend_freq_str', '일간 (매일)')
        if saved_freq not in freq_options:
          saved_freq = '일간 (매일)'

        option_freq = st.selectbox(
            '트렌드 주기 선택',
            options=freq_options,
            index=freq_options.index(saved_freq),
        )

        d_col1, d_col2 = st.columns(2)
        with d_col1:
          start_date = st.date_input(
              '조회 시작일',
              st.session_state.get('trend_start_date', prev_month_end),
          )
        with d_col2:
          end_date = st.date_input(
              '조회 종료일',
              st.session_state.get(
                  'trend_end_date',
                  max_rec_date if max_rec_date >= min_rec_date else min_rec_date,
              ),
          )

      with f_col3:
        mirae_eval_val = st.number_input(
            '🏦 미래에셋 계좌 평가금액 수동 입력 (원)',
            value=st.session_state.get('trend_mirae_val', 55500000.0),
            step=1000000.0,
        )

      st.write('---')
      run_button = st.form_submit_button('🚀 데이터 계산 실행 (Run)')

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
            m_acc = str(m_row['account_num'])
            m_alias = alias_map.get(m_acc, '')
            m_display = (
                m_alias
                if m_alias
                else f'미지정별칭({m_acc[-4:] if len(m_acc)>=4 else m_acc})'
            )
            if m_display == alias_part:
              selected_accounts.append(m_acc)
              break
      selected_accounts = list(set(selected_accounts))

      full_date_range = pd.date_range(start=start_date, end=end_date)

      if option_freq == '일간 (매일)':
        target_dates = full_date_range
      elif option_freq == '일간 (주말 제외)':
        target_dates = full_date_range[full_date_range.dayofweek < 5]
      elif option_freq == '주간 (매주 토요일)':
        target_dates = full_date_range[full_date_range.dayofweek == 5]
      elif option_freq == '월간 (매달 말일)':
        target_dates = full_date_range[full_date_range.is_month_end]
      else:
        target_dates = full_date_range[full_date_range.is_year_end]

      target_dates = pd.DatetimeIndex(
          sorted(list(set(target_dates).union({pd.to_datetime(end_date)})))
      )

      filtered_pf_df = pf_df[
          pf_df['account_num'].astype(str).isin(selected_accounts)
      ].copy()
      unique_tickers = filtered_pf_df['ticker'].unique().tolist()
      fetch_tickers = list(unique_tickers) + list(bm_ticker_map.values())

      with st.spinner(
          '최신 시세 및 벤치마크 데이터를 수집하고 트렌드를 계산 중입니다...'
      ):
        s_str = start_date.strftime('%Y-%m-%d')
        e_str = (end_date + pd.Timedelta(days=3)).strftime('%Y-%m-%d')
        market_data = fetch_market_data(
            fetch_tickers, s_str, e_str, force_refresh=True
        )

      base_records = []
      for t_date in target_dates:
        t_str = t_date.strftime('%Y-%m-%d')

        usd_krw = None
        if 'KRW=X' in market_data.columns and not market_data.empty:
          if pd.to_datetime(t_str) in market_data.index:
            usd_krw = market_data.loc[pd.to_datetime(t_str), 'KRW=X']
          else:
            usd_krw = market_data['KRW=X'].asof(pd.to_datetime(t_str))

        if pd.isna(usd_krw) or usd_krw is None or usd_krw <= 0:
          if (
              'KRW=X' in market_data.columns
              and not market_data['KRW=X'].dropna().empty
          ):
            usd_krw = float(market_data['KRW=X'].dropna().iloc[-1])
          else:
            usd_krw = 1350.0

        for acc in selected_accounts:
          acc_meta = filtered_pf_df[
              filtered_pf_df['account_num'].astype(str) == acc
          ]
          broker_name = (
              acc_meta['broker'].iloc[0]
              if (not acc_meta.empty and 'broker' in acc_meta.columns)
              else '미지정'
          )
          acc_type = (
              acc_meta['account_type'].iloc[0]
              if (
                  not acc_meta.empty
                  and 'account_type' in acc_meta.columns
                  and pd.notna(acc_meta['account_type'].iloc[0])
              )
              else '미지정'
          )

          acc_alias_val = alias_map.get(acc, '')
          acc_label = (
              acc_alias_val
              if acc_alias_val
              else f'미지정별칭({acc[-4:] if len(acc)>=4 else acc})'
          )

          init_val = (
              init_p_df[init_p_df['account_num'].astype(str) == acc][
                  'initial_amount'
              ].sum()
              if not init_p_df.empty
              else 0
          )
          if not cf_df.empty:
            acc_cf = cf_df[
                (cf_df['account_num'].astype(str) == acc)
                & (cf_df['trans_date'] <= t_str)
            ]
            in_flow = acc_cf[acc_cf['flow_type'] == '입금']['amount'].sum()
            out_flow = acc_cf[acc_cf['flow_type'] == '출금']['amount'].sum()
          else:
            in_flow, out_flow = 0, 0
          principal = init_val + in_flow - out_flow

          if '미래에셋' in str(broker_name):
            base_records.append({
                'Date': t_str,
                'account_num': acc_label,
                'broker': broker_name,
                'account_type': acc_type,
                'category4': '미래에셋 수동',
                'item_name': '미래에셋 수동자산',
                '원금': principal,
                '평가손익': mirae_eval_val - principal,
                '총평가금액': mirae_eval_val,
            })
          else:
            acc_pf = filtered_pf_df[
                (filtered_pf_df['account_num'].astype(str) == acc)
                & (filtered_pf_df['record_date'] <= t_str)
            ]
            if acc_pf.empty:
              min_date = filtered_pf_df[
                  filtered_pf_df['account_num'].astype(str) == acc
              ]['record_date'].min()
              acc_pf = filtered_pf_df[
                  (filtered_pf_df['account_num'].astype(str) == acc)
                  & (filtered_pf_df['record_date'] == min_date)
              ]

            if not acc_pf.empty:
              latest_date = acc_pf['record_date'].max()
              current_pf = acc_pf[acc_pf['record_date'] == latest_date]
              total_acc_eval = 0
              item_eval_list = []

              for _, row in current_pf.iterrows():
                fmt_tk = format_ticker(row.get('ticker'))
                qty = (
                    row['quantity']
                    if ('quantity' in row and pd.notna(row['quantity']))
                    else 0
                )
                curr = row.get('currency', 'KRW')
                base_price = (
                    row['current_price']
                    if ('current_price' in row and pd.notna(row['current_price']))
                    else 0
                )

                item_name = (
                    row['item_name']
                    if (
                        'item_name' in row
                        and pd.notna(row['item_name'])
                        and str(row['item_name']).strip() != ''
                    )
                    else '미지정종목'
                )
                cat4 = (
                    row['category4']
                    if (
                        'category4' in row
                        and pd.notna(row['category4'])
                        and str(row['category4']).strip() != ''
                    )
                    else '미지정'
                )

                price = 0
                if fmt_tk is None:
                  price = base_price if base_price > 0 else 1.0
                else:
                  if fmt_tk in market_data.columns and not market_data.empty:
                    if pd.to_datetime(t_str) in market_data.index:
                      price = market_data.loc[pd.to_datetime(t_str), fmt_tk]
                    else:
                      price = market_data[fmt_tk].asof(pd.to_datetime(t_str))

                    if (
                        (pd.isna(price) or price == 0)
                        and fmt_tk in market_data.columns
                        and not market_data[fmt_tk].dropna().empty
                    ):
                      price = float(market_data[fmt_tk].dropna().iloc[-1])

                  if pd.isna(price) or price == 0:
                    price = base_price

                item_eval = (
                    (qty * price * usd_krw) if curr == 'USD' else (qty * price)
                )
                total_acc_eval += item_eval
                item_eval_list.append((item_name, cat4, item_eval))

              for item_name, cat4, item_eval in item_eval_list:
                ratio = (
                    (item_eval / total_acc_eval) if total_acc_eval > 0 else 0
                )
                item_principal = principal * ratio
                item_p_loss = item_eval - item_principal

                base_records.append({
                    'Date': t_str,
                    'account_num': acc_label,
                    'broker': broker_name,
                    'account_type': acc_type,
                    'category4': cat4,
                    'item_name': item_name,
                    '원금': item_principal,
                    '평가손익': item_p_loss,
                    '총평가금액': item_eval,
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

            if (
                (pd.isna(p) or p == 0)
                and tk in market_data.columns
                and not market_data[tk].dropna().empty
            ):
              p = float(market_data[tk].dropna().iloc[-1])

            prices.append(p)
            dates_str.append(t_str)

          bm_df = (
              pd.DataFrame({'Date_str': dates_str, 'Close': prices})
              .ffill()
              .bfill()
          )
          init_p = bm_df['Close'].iloc[0] if len(bm_df) > 0 else 0

          bm_df['주기별 수익률'] = bm_df['Close'].pct_change() * 100
          bm_df['기간 누적 수익률'] = np.where(
              init_p > 0, ((bm_df['Close'] - init_p) / init_p) * 100, 0
          )
          bm_calc_dict[bm_label] = bm_df

      st.session_state['trend_calc_df'] = pd.DataFrame(base_records)
      st.session_state['trend_bm_calc'] = bm_calc_dict

    if (
        'trend_calc_df' in st.session_state
        and not st.session_state['trend_calc_df'].empty
    ):
      calc_df = st.session_state['trend_calc_df']
      bm_calc_dict = st.session_state.get('trend_bm_calc', {})
      active_views = st.session_state.get('trend_view_types', all_view_types)

      st.write('---')
      st.subheader('🖥️ 화면 디스플레이 설정 (실시간 반영)')

      disp_col1, disp_col2, disp_col3 = st.columns([3, 3, 4])

      with disp_col1:
        layout_setting = st.selectbox(
            '🖥️ 차트 Layout 선택',
            options=[
                '1열 (기존 세로 배치)',
                '2열 (좌우 2개 분할)',
                '3열 (좌우 3개 분할)',
            ],
            index=0,
            key='live_chart_layout',
        )

      with disp_col2:
        y_range_mode = st.radio(
            '🎯 Y축 Range(범위) 지정',
            options=['자동 (Auto)', '수동 지정 (Manual)'],
            horizontal=True,
            key='live_y_range_mode',
        )

      y_min_val, y_max_val = None, None
      with disp_col3:
        if y_range_mode == '수동 지정 (Manual)':
          r_c1, r_c2 = st.columns(2)
          with r_c1:
            y_min_val = st.number_input(
                'Y축 최소값 (원)', value=0.0, step=1000000.0, key='live_ymin'
            )
          with r_c2:
            y_max_val = st.number_input(
                'Y축 최대값 (원)',
                value=100000000.0,
                step=1000000.0,
                key='live_ymax',
            )

      num_cols = 1
      if '2열' in layout_setting:
        num_cols = 2
      elif '3열' in layout_setting:
        num_cols = 3

      def apply_y_axis_config(fig, axis_name='yaxis', is_money=True):
        kwargs = dict(type='linear', zeroline=True)
        if (
            is_money
            and y_range_mode == '수동 지정 (Manual)'
            and y_min_val is not None
            and y_max_val is not None
        ):
          kwargs['range'] = [y_min_val, y_max_val]
          kwargs['autorange'] = False
        else:
          kwargs['autorange'] = True

        if axis_name == 'yaxis':
          fig.update_yaxes(**kwargs)
        elif axis_name == 'yaxis2':
          fig.update_yaxes(secondary_y=True, **kwargs)

      def draw_single_chart(sub_df, title_name, force_single_col=False):
        sub_df = sub_df.copy()
        sub_df['dt_temp'] = pd.to_datetime(sub_df['Date'])
        sub_df = sub_df.sort_values('dt_temp', ascending=True).reset_index(
            drop=True
        )
        sub_df['Chart_Date'] = sub_df['dt_temp'].dt.strftime('%Y-%m-%d')
        sub_df.drop(columns=['dt_temp'], inplace=True)

        date_order_list = sub_df['Chart_Date'].tolist()

        sub_df['수익률'] = np.where(
            sub_df['원금'] > 0, (sub_df['평가손익'] / sub_df['원금']) * 100, 0
        )
        sub_df['주기별 평가손익'] = sub_df['총평가금액'].diff()
        base_start_p_loss = sub_df['평가손익'].iloc[0]
        sub_df['선택구간 누적손익'] = sub_df['평가손익'] - base_start_p_loss
        sub_df['구간별 수익률'] = sub_df['총평가금액'].pct_change() * 100
        initial_eval = sub_df['총평가금액'].iloc[0]
        sub_df['구간별 누적수익률'] = np.where(
            initial_eval > 0,
            ((sub_df['총평가금액'] - initial_eval) / initial_eval) * 100,
            0,
        )

        selected_cum_p_loss = sub_df['선택구간 누적손익'].iloc[-1]
        total_cum_p_loss = sub_df['평가손익'].iloc[-1]

        c_m1, c_m2, c_m3 = st.columns(3)
        c_m1.metric('📌 선택 구간 누적 평가손익', f'{selected_cum_p_loss:,.0f} 원')
        c_m2.metric('🏛️ 전체 통산 누적 평가손익', f'{total_cum_p_loss:,.0f} 원')
        c_m3.metric('💰 최종 기말 평가금액', f"{sub_df['총평가금액'].iloc[-1]:,.0f} 원")

        fig1 = make_subplots(specs=[[{'secondary_y': True}]])
        fig1.add_trace(
            go.Bar(
                x=sub_df['Chart_Date'],
                y=sub_df['원금'],
                name='원금',
                marker_color='#2b5c8f',
                opacity=0.6,
                hovertemplate='%{y:,.0f} 원',
            ),
            secondary_y=False,
        )
        fig1.add_trace(
            go.Bar(
                x=sub_df['Chart_Date'],
                y=sub_df['평가손익'],
                name='전체 누적 평가손익',
                marker_color='#e05d5d',
                opacity=0.5,
                hovertemplate='%{y:,.0f} 원',
            ),
            secondary_y=False,
        )
        fig1.add_trace(
            go.Scatter(
                x=sub_df['Chart_Date'],
                y=sub_df['총평가금액'],
                name='총평가금액',
                mode='lines+markers+text',
                line=dict(color='#ff9900', width=3),
                marker=dict(size=6, color='#ff9900'),
                text=[f'{v:,.0f}' for v in sub_df['총평가금액']],
                textposition='top center',
                hovertemplate='%{y:,.0f} 원',
            ),
            secondary_y=False,
        )
        fig1.add_trace(
            go.Scatter(
                x=sub_df['Chart_Date'],
                y=sub_df['수익률'],
                name='수익률(%)',
                mode='lines+markers',
                line=dict(color='#2ca02c', dash='dash', width=2),
                marker=dict(size=6, color='#2ca02c'),
                hovertemplate='%{y:.2f}%',
            ),
            secondary_y=True,
        )

        apply_chart_standard_layout(
            fig1, f'1. [{title_name}] 자산 및 전체 손익/수익률 추이'
        )
        fig1.update_layout(barmode='relative', height=400)
        fig1.update_xaxes(
            type='category',
            categoryorder='array',
            categoryarray=date_order_list,
        )
        apply_y_axis_config(fig1, axis_name='yaxis', is_money=True)
        fig1.update_yaxes(
            title_text='금액 (원)', tickformat=',.0f', secondary_y=False
        )
        fig1.update_yaxes(
            title_text='수익률 (%)',
            tickformat=',.2f',
            ticksuffix='%',
            zeroline=True,
            secondary_y=True,
        )

        fig2 = go.Figure()
        valid_period_df = sub_df.dropna(subset=['주기별 평가손익'])
        period_colors = [
            '#2ca02c' if v >= 0 else '#d62728'
            for v in valid_period_df['주기별 평가손익']
        ]
        fig2.add_trace(
            go.Bar(
                x=valid_period_df['Chart_Date'],
                y=valid_period_df['주기별 평가손익'],
                name='주기별 평가손익',
                marker_color=period_colors,
                opacity=0.85,
                hovertemplate='%{y:,.0f} 원',
            )
        )
        fig2.add_trace(
            go.Scatter(
                x=sub_df['Chart_Date'],
                y=sub_df['선택구간 누적손익'],
                name='선택구간 누적손익 (추이)',
                mode='lines+markers',
                line=dict(color='#9467bd', width=2.5),
                marker=dict(size=5, color='#9467bd'),
                hovertemplate='%{y:,.0f} 원',
            )
        )

        apply_chart_standard_layout(fig2, f'2. [{title_name}] 구간 손익 금액 추이')
        fig2.update_layout(height=400)
        fig2.update_xaxes(
            type='category',
            categoryorder='array',
            categoryarray=date_order_list,
        )
        apply_y_axis_config(fig2, axis_name='yaxis', is_money=True)
        fig2.update_yaxes(title_text='손익금액 (원)', tickformat=',.0f')

        fig3a = go.Figure()
        fig3a.add_trace(
            go.Scatter(
                x=sub_df['Chart_Date'],
                y=sub_df['구간별 누적수익률'],
                name='구간별 누적수익률 (%)',
                mode='lines+markers',
                line=dict(color='#1f77b4', width=2.5),
                marker=dict(size=5, color='#1f77b4'),
                hovertemplate='%{y:.2f}%',
            )
        )

        for bm_name, bm_df in bm_calc_dict.items():
          bm_df['dt_temp'] = pd.to_datetime(bm_df['Date_str'])
          bm_df = bm_df.sort_values('dt_temp', ascending=True).reset_index(
              drop=True
          )
          bm_df['Chart_Date'] = bm_df['dt_temp'].dt.strftime('%Y-%m-%d')
          bm_df.drop(columns=['dt_temp'], inplace=True)

          bm_color = bm_styles.get(bm_name, {}).get('color', '#7f7f7f')
          fig3a.add_trace(
              go.Scatter(
                  x=bm_df['Chart_Date'],
                  y=bm_df['기간 누적 수익률'],
                  mode='lines',
                  name=f'📌 {bm_name} 누적수익률 (%)',
                  line=dict(color=bm_color, dash='dot'),
                  hovertemplate='%{y:.2f}%',
              )
          )

        apply_chart_standard_layout(
            fig3a,
            f'3-1. [{title_name}] 구간 누적수익률 추이 (벤치마크 비교)',
        )
        fig3a.update_layout(height=400)
        fig3a.update_xaxes(
            type='category',
            categoryorder='array',
            categoryarray=date_order_list,
        )
        fig3a.update_yaxes(
            title_text='수익률 (%)',
            tickformat=',.2f',
            ticksuffix='%',
            zeroline=True,
        )

        fig3b = go.Figure()
        fig3b.add_trace(
            go.Scatter(
                x=sub_df['Chart_Date'],
                y=sub_df['구간별 수익률'],
                name='구간별 수익률 (%)',
                mode='lines+markers',
                line=dict(color='#17becf', width=2, dash='dot'),
                marker=dict(size=5, color='#17becf'),
                hovertemplate='%{y:.2f}%',
            )
        )

        for bm_name, bm_df in bm_calc_dict.items():
          bm_df['dt_temp'] = pd.to_datetime(bm_df['Date_str'])
          bm_df = bm_df.sort_values('dt_temp', ascending=True).reset_index(
              drop=True
          )
          bm_df['Chart_Date'] = bm_df['dt_temp'].dt.strftime('%Y-%m-%d')
          bm_df.drop(columns=['dt_temp'], inplace=True)

          bm_color = bm_styles.get(bm_name, {}).get('color', '#7f7f7f')
          fig3b.add_trace(
              go.Scatter(
                  x=bm_df['Chart_Date'],
                  y=bm_df['주기별 수익률'],
                  mode='lines',
                  name=f'📌 {bm_name} 주기별수익률 (%)',
                  line=dict(color=bm_color, dash='dash'),
                  hovertemplate='%{y:.2f}%',
              )
          )

        apply_chart_standard_layout(
            fig3b, f'3-2. [{title_name}] 주기별 수익률 추이 (벤치마크 비교)'
        )
        fig3b.update_layout(height=400)
        fig3b.update_xaxes(
            type='category',
            categoryorder='array',
            categoryarray=date_order_list,
        )
        fig3b.update_yaxes(
            title_text='수익률 (%)',
            tickformat=',.2f',
            ticksuffix='%',
            zeroline=True,
        )

        effective_cols = 1 if force_single_col else num_cols

        if effective_cols == 1:
          render_chart_with_click_event(
              fig1, f'trend_fig1_{title_name}_{force_single_col}', calc_df
          )
          render_chart_with_click_event(
              fig2, f'trend_fig2_{title_name}_{force_single_col}', calc_df
          )
          render_chart_with_click_event(
              fig3a, f'trend_fig3a_{title_name}_{force_single_col}', calc_df
          )
          render_chart_with_click_event(
              fig3b, f'trend_fig3b_{title_name}_{force_single_col}', calc_df
          )
        elif effective_cols == 2:
          col_a, col_b = st.columns(2)
          with col_a:
            render_chart_with_click_event(
                fig1, f'trend_fig1_{title_name}_{force_single_col}', calc_df
            )
          with col_b:
            render_chart_with_click_event(
                fig2, f'trend_fig2_{title_name}_{force_single_col}', calc_df
            )
          col_c, col_d = st.columns(2)
          with col_c:
            render_chart_with_click_event(
                fig3a, f'trend_fig3a_{title_name}_{force_single_col}', calc_df
            )
          with col_d:
            render_chart_with_click_event(
                fig3b, f'trend_fig3b_{title_name}_{force_single_col}', calc_df
            )
        else:
          col_a, col_b, col_c = st.columns(3)
          with col_a:
            render_chart_with_click_event(
                fig1, f'trend_fig1_{title_name}_{force_single_col}', calc_df
            )
          with col_b:
            render_chart_with_click_event(
                fig2, f'trend_fig2_{title_name}_{force_single_col}', calc_df
            )
          with col_c:
            render_chart_with_click_event(
                fig3a, f'trend_fig3a_{title_name}_{force_single_col}', calc_df
            )
          render_chart_with_click_event(
              fig3b, f'trend_fig3b_{title_name}_{force_single_col}', calc_df
          )

        return sub_df

      def draw_group_summary_charts(df, group_col, prefix):
        st.markdown(f'### 📊 [{prefix}] 전체 종합 비교 분석')
        grp_agg = (
            df.groupby(['Date', group_col])[['원금', '평가손익', '총평가금액']]
            .sum()
            .reset_index()
        )

        grp_agg['dt_temp'] = pd.to_datetime(grp_agg['Date'])
        grp_agg = grp_agg.sort_values(
            [group_col, 'dt_temp'], ascending=True
        ).reset_index(drop=True)
        grp_agg['Chart_Date'] = grp_agg['dt_temp'].dt.strftime('%Y-%m-%d')
        grp_agg.drop(columns=['dt_temp'], inplace=True)

        date_order_list = sorted(grp_agg['Chart_Date'].unique().tolist())

        grp_agg['수익률'] = np.where(
            grp_agg['원금'] > 0, (grp_agg['평가손익'] / grp_agg['원금']) * 100, 0
        )
        grp_agg['주기별 평가손익'] = grp_agg.groupby(group_col)[
            '총평가금액'
        ].diff()
        first_p_loss = grp_agg.groupby(group_col)['평가손익'].transform('first')
        grp_agg['선택구간 누적손익'] = grp_agg['평가손익'] - first_p_loss

        grp_agg['주기별 수익률'] = (
            grp_agg.groupby(group_col)['총평가금액'].pct_change() * 100
        )
        first_eval = grp_agg.groupby(group_col)['총평가금액'].transform('first')
        grp_agg['선택기간 누적 수익률'] = np.where(
            first_eval > 0,
            ((grp_agg['총평가금액'] - first_eval) / first_eval) * 100,
            0,
        )

        latest_date = df['Date'].max()
        latest_df = df[df['Date'] == latest_date]
        group_order = (
            latest_df.groupby(group_col)['총평가금액']
            .sum()
            .sort_values(ascending=False)
            .index.tolist()
        )
        all_groups = grp_agg[group_col].unique()
        groups = group_order + [g for g in all_groups if g not in group_order]

        group_color_map = {
            grp: DEFAULT_PALETTE[i % len(DEFAULT_PALETTE)]
            for i, grp in enumerate(groups)
        }

        fig_sel_p = go.Figure()
        for grp in groups:
          sub = grp_agg[grp_agg[group_col] == grp]
          c = group_color_map[grp]
          fig_sel_p.add_trace(
              go.Scatter(
                  x=sub['Chart_Date'],
                  y=sub['선택구간 누적손익'],
                  name=f'{grp}',
                  mode='lines+markers',
                  line=dict(color=c, width=2),
                  marker=dict(size=5, color=c),
                  hovertemplate='%{y:,.0f} 원',
              )
          )

        apply_chart_standard_layout(
            fig_sel_p, f'🔹 [{prefix}] 선택 구간 누적 평가손익 Trend'
        )
        fig_sel_p.update_layout(height=400)
        fig_sel_p.update_xaxes(
            type='category',
            categoryorder='array',
            categoryarray=date_order_list,
        )
        apply_y_axis_config(fig_sel_p, axis_name='yaxis', is_money=True)
        fig_sel_p.update_yaxes(
            title_text='선택구간 누적손익 (원)', tickformat=',.0f'
        )

        fig_period_p = go.Figure()
        for grp in groups:
          sub = grp_agg[grp_agg[group_col] == grp].dropna(
              subset=['주기별 평가손익']
          )
          c = group_color_map[grp]
          fig_period_p.add_trace(
              go.Bar(
                  x=sub['Chart_Date'],
                  y=sub['주기별 평가손익'],
                  name=str(grp),
                  marker_color=c,
                  hovertemplate='%{y:,.0f} 원',
              )
          )
        apply_chart_standard_layout(
            fig_period_p,
            f'🔹 [{prefix}] 선택 기간 주기별 평가손익 Trend (세로 누적 막대)',
        )
        fig_period_p.update_layout(barmode='relative', height=400)
        fig_period_p.update_xaxes(
            type='category',
            categoryorder='array',
            categoryarray=date_order_list,
        )
        apply_y_axis_config(fig_period_p, is_money=True)
        fig_period_p.update_yaxes(title_text='손익금액 (원)', tickformat=',.0f')

        fig_period_ret = go.Figure()
        for grp in groups:
          sub = grp_agg[grp_agg[group_col] == grp].dropna(
              subset=['주기별 수익률']
          )
          c = group_color_map[grp]
          fig_period_ret.add_trace(
              go.Scatter(
                  x=sub['Chart_Date'],
                  y=sub['주기별 수익률'],
                  name=str(grp),
                  mode='lines+markers',
                  line=dict(color=c, width=2),
                  marker=dict(size=5, color=c),
                  hovertemplate='%{y:.2f}%',
              )
          )

        for bm_name, bm_df in bm_calc_dict.items():
          bm_df['dt_temp'] = pd.to_datetime(bm_df['Date_str'])
          bm_df = bm_df.sort_values('dt_temp', ascending=True).reset_index(
              drop=True
          )
          bm_df['Chart_Date'] = bm_df['dt_temp'].dt.strftime('%Y-%m-%d')
          bm_df.drop(columns=['dt_temp'], inplace=True)
          sub_bm = bm_df.dropna(subset=['주기별 수익률'])

          bm_color = bm_styles.get(bm_name, {}).get('color', '#7f7f7f')
          fig_period_ret.add_trace(
              go.Scatter(
                  x=sub_bm['Chart_Date'],
                  y=sub_bm['주기별 수익률'],
                  mode='lines',
                  name=f'📌 {bm_name}',
                  line=dict(color=bm_color, dash='dot'),
                  hovertemplate='%{y:.2f}%',
              )
          )

        apply_chart_standard_layout(
            fig_period_ret,
            f'🔹 [{prefix}] 선택기간 주기별 수익률 Trend (꺾은선, 벤치마크'
            ' 포함)',
        )
        fig_period_ret.update_layout(height=400)
        fig_period_ret.update_xaxes(
            type='category',
            categoryorder='array',
            categoryarray=date_order_list,
        )
        fig_period_ret.update_yaxes(
            title_text='주기별 수익률 (%)',
            tickformat=',.2f',
            ticksuffix='%',
            zeroline=True,
        )

        fig_cum_ret = make_subplots(specs=[[{'secondary_y': True}]])
        for grp in groups:
          sub = grp_agg[grp_agg[group_col] == grp]
          c = group_color_map[grp]
          fig_cum_ret.add_trace(
              go.Bar(
                  x=sub['Chart_Date'],
                  y=sub['선택구간 누적손익'],
                  name=f'[누적손익] {grp}',
                  marker_color=c,
                  opacity=0.6,
                  hovertemplate='%{y:,.0f} 원',
              ),
              secondary_y=True,
          )

        for grp in groups:
          sub = grp_agg[grp_agg[group_col] == grp]
          c = group_color_map[grp]
          fig_cum_ret.add_trace(
              go.Scatter(
                  x=sub['Chart_Date'],
                  y=sub['선택기간 누적 수익률'],
                  name=f'[누적수익률] {grp}',
                  mode='lines+markers',
                  line=dict(color=c, width=2.5),
                  marker=dict(size=5, color=c),
                  hovertemplate='%{y:.2f}%',
              ),
              secondary_y=False,
          )

        for bm_name, bm_df in bm_calc_dict.items():
          bm_df['dt_temp'] = pd.to_datetime(bm_df['Date_str'])
          bm_df = bm_df.sort_values('dt_temp', ascending=True).reset_index(
              drop=True
          )
          bm_df['Chart_Date'] = bm_df['dt_temp'].dt.strftime('%Y-%m-%d')
          bm_df.drop(columns=['dt_temp'], inplace=True)

          bm_color = bm_styles.get(bm_name, {}).get('color', '#7f7f7f')
          fig_cum_ret.add_trace(
              go.Scatter(
                  x=bm_df['Chart_Date'],
                  y=bm_df['기간 누적 수익률'],
                  mode='lines',
                  name=f'📌 {bm_name}',
                  line=dict(color=bm_color, dash='dot'),
                  hovertemplate='%{y:.2f}%',
              ),
              secondary_y=False,
          )

        apply_chart_standard_layout(
            fig_cum_ret,
            f'🔹 [{prefix}] 선택기간 누적 수익률 Trend (좌축) & 선택기간'
            ' 누적평가 손익 (우측 보조축 그룹 막대)',
        )
        fig_cum_ret.update_layout(barmode='group', height=400)
        fig_cum_ret.update_xaxes(
            type='category',
            categoryorder='array',
            categoryarray=date_order_list,
        )
        apply_y_axis_config(fig_cum_ret, axis_name='yaxis', is_money=False)
        fig_cum_ret.update_yaxes(
            title_text='누적 수익률 (%)',
            tickformat=',.2f',
            ticksuffix='%',
            zeroline=True,
            secondary_y=False,
        )
        fig_cum_ret.update_yaxes(
            title_text='선택기간 누적평가 손익 (원)',
            tickformat=',.0f',
            secondary_y=True,
        )

        fig_p = go.Figure()
        for grp in groups:
          sub = grp_agg[grp_agg[group_col] == grp]
          c = group_color_map[grp]
          fig_p.add_trace(
              go.Bar(
                  x=sub['Chart_Date'],
                  y=sub['평가손익'],
                  name=str(grp),
                  marker_color=c,
                  hovertemplate='%{y:,.0f} 원',
              )
          )
        apply_chart_standard_layout(
            fig_p,
            f'🔹 [{prefix}] 전체 통산 누적 평가손익 Trend (세로 누적 막대)',
        )
        fig_p.update_layout(barmode='relative', height=400)
        fig_p.update_xaxes(
            type='category',
            categoryorder='array',
            categoryarray=date_order_list,
        )
        apply_y_axis_config(fig_p, is_money=True)
        fig_p.update_yaxes(title_text='손익금액 (원)', tickformat=',.0f')

        fig_r = go.Figure()
        for grp in groups:
          sub = grp_agg[grp_agg[group_col] == grp]
          c = group_color_map[grp]
          fig_r.add_trace(
              go.Scatter(
                  x=sub['Chart_Date'],
                  y=sub['수익률'],
                  name=str(grp),
                  mode='lines+markers',
                  line=dict(color=c, width=2),
                  marker=dict(size=5, color=c),
                  hovertemplate='%{y:.2f}%',
              )
          )
        apply_chart_standard_layout(
            fig_r, f'🔹 [{prefix}] 통산 수익률 Trend (원금대비 꺾은선)'
        )
        fig_r.update_layout(height=400)
        fig_r.update_xaxes(
            type='category',
            categoryorder='array',
            categoryarray=date_order_list,
        )
        fig_r.update_yaxes(
            title_text='수익률 (%)',
            tickformat=',.2f',
            ticksuffix='%',
            zeroline=True,
        )

        if num_cols == 1:
          render_chart_with_click_event(
              fig_sel_p, f'trend_grp_sel_p_{prefix}', calc_df
          )
          render_chart_with_click_event(
              fig_period_p, f'trend_grp_period_p_{prefix}', calc_df
          )
          render_chart_with_click_event(
              fig_period_ret, f'trend_grp_period_ret_{prefix}', calc_df
          )
          render_chart_with_click_event(
              fig_cum_ret, f'trend_grp_cum_ret_{prefix}', calc_df
          )
          render_chart_with_click_event(
              fig_p, f'trend_grp_p_{prefix}', calc_df
          )
          render_chart_with_click_event(
              fig_r, f'trend_grp_r_{prefix}', calc_df
          )
        elif num_cols == 2:
          col1, col2 = st.columns(2)
          with col1:
            render_chart_with_click_event(
                fig_sel_p, f'trend_grp_sel_p_{prefix}', calc_df
            )
          with col2:
            render_chart_with_click_event(
                fig_period_p, f'trend_grp_period_p_{prefix}', calc_df
            )
          col3, col4 = st.columns(2)
          with col3:
            render_chart_with_click_event(
                fig_period_ret, f'trend_grp_period_ret_{prefix}', calc_df
            )
          with col4:
            render_chart_with_click_event(
                fig_cum_ret, f'trend_grp_cum_ret_{prefix}', calc_df
            )
          col5, col6 = st.columns(2)
          with col5:
            render_chart_with_click_event(
                fig_p, f'trend_grp_p_{prefix}', calc_df
            )
          with col6:
            render_chart_with_click_event(
                fig_r, f'trend_grp_r_{prefix}', calc_df
            )
        else:
          col1, col2, col3 = st.columns(3)
          with col1:
            render_chart_with_click_event(
                fig_sel_p, f'trend_grp_sel_p_{prefix}', calc_df
            )
          with col2:
            render_chart_with_click_event(
                fig_period_p, f'trend_grp_period_p_{prefix}', calc_df
            )
          with col3:
            render_chart_with_click_event(
                fig_period_ret, f'trend_grp_period_ret_{prefix}', calc_df
            )
          col4, col5, col6 = st.columns(3)
          with col4:
            render_chart_with_click_event(
                fig_cum_ret, f'trend_grp_cum_ret_{prefix}', calc_df
            )
          with col5:
            render_chart_with_click_event(
                fig_p, f'trend_grp_p_{prefix}', calc_df
            )
          with col6:
            render_chart_with_click_event(
                fig_r, f'trend_grp_r_{prefix}', calc_df
            )

      def render_separate_charts(df, group_col, prefix):
        if group_col is None:
          agg_df = (
              df.groupby('Date')[['원금', '평가손익', '총평가금액']]
              .sum()
              .reset_index()
          )
          draw_single_chart(agg_df, '전체 합산')
        else:
          draw_group_summary_charts(df, group_col, prefix)
          st.write('---')

          latest_date = df['Date'].max()
          latest_df = df[df['Date'] == latest_date]
          group_order = (
              latest_df.groupby(group_col)['총평가금액']
              .sum()
              .sort_values(ascending=False)
              .index.tolist()
          )
          all_groups = df[group_col].unique()
          groups = group_order + [
              g for g in all_groups if g not in group_order
          ]

          if num_cols > 1 and group_col is not None:
            st.markdown(f'#### 📌 그룹별 상세 분석 ({prefix})')
            cols = st.columns(num_cols)
            for idx, grp in enumerate(groups):
              with cols[idx % num_cols]:
                grp_df = (
                    df[df[group_col] == grp]
                    .groupby('Date')[['원금', '평가손익', '총평가금액']]
                    .sum()
                    .reset_index()
                )
                st.markdown(f'##### 🎯 {prefix}: {grp}')
                draw_single_chart(
                    grp_df, f'{prefix} [{grp}]', force_single_col=True
                )
          else:
            for grp in groups:
              grp_df = (
                  df[df[group_col] == grp]
                  .groupby('Date')[['원금', '평가손익', '총평가금액']]
                  .sum()
                  .reset_index()
              )
              st.markdown(f'#### 📌 {prefix}: {grp}')
              draw_single_chart(grp_df, f'{prefix} [{grp}]')
              st.write('---')

      tabs = st.tabs(active_views)
      for i, v_type in enumerate(active_views):
        with tabs[i]:
          if v_type == '전체 합산':
            render_separate_charts(calc_df, None, '전체 합산')
          elif v_type == '계좌별':
            render_separate_charts(calc_df, 'account_num', '계좌별')
          elif v_type == '증권사(Broker)별':
            render_separate_charts(calc_df, 'broker', '증권사')
          elif v_type == '계좌유형별':
            render_separate_charts(calc_df, 'account_type', '계좌유형')
          elif v_type == 'Category 4별':
            render_separate_charts(calc_df, 'category4', 'Category 4')
          elif v_type == '보유항목별':
            render_separate_charts(calc_df, 'item_name', '보유항목')

# -----------------------------------------------------------------------------
# 메뉴 2: 계좌 별칭 관리
# -----------------------------------------------------------------------------
elif menu == '계좌 별칭 관리':
  st.header('🏷️ 계좌별 수동 별칭(Alias) 관리')
  conn = get_connection()
  pf_df = pd.read_sql(
      'SELECT DISTINCT broker, account_num FROM portfolio', conn
  )
  alias_df = pd.read_sql('SELECT * FROM account_alias', conn)
  conn.close()

  if pf_df.empty:
    st.warning(
        '등록된 계좌 정보가 없습니다. [포트폴리오 업로드]를 먼저 진행해 주세요.'
    )
  else:
    pf_df['account_num'] = pf_df['account_num'].astype(str)
    alias_df['account_num'] = alias_df['account_num'].astype(str)
    merged_df = pd.merge(pf_df, alias_df, on='account_num', how='left').fillna(
        {'alias': '', 'broker': '증권사미지정'}
    )

    st.subheader('계좌 별칭 입력 / 수정')
    with st.form('alias_form'):
      updated_aliases = {}
      for idx, row in merged_df.iterrows():
        acc_str = str(row['account_num'])
        col1, col2, col3 = st.columns([2, 3, 3])
        with col1:
          st.write(f"**{row.get('broker', '증권사미지정')}**")
        with col2:
          st.write(f'`{acc_str}`')
        with col3:
          new_alias = st.text_input(
              f'별칭 ({acc_str})', value=row['alias'], key=f'alias_{acc_str}'
          )
          updated_aliases[acc_str] = new_alias

      save_alias_btn = st.form_submit_button('💾 별칭 저장하기')
      if save_alias_btn:
        conn = get_connection()
        c = conn.cursor()
        for acc_num, alias_val in updated_aliases.items():
          c.execute(
              """
                        INSERT INTO account_alias (account_num, alias)
                        VALUES (?, ?)
                        ON CONFLICT(account_num) DO UPDATE SET alias=excluded.alias
                    """,
              (str(acc_num), alias_val.strip()),
          )
        conn.commit()
        conn.close()
        st.success('계좌 별칭이 저장되었습니다!')
        st.rerun()

# -----------------------------------------------------------------------------
# 메뉴 3: 포트폴리오 업로드
# -----------------------------------------------------------------------------
elif menu == '포트폴리오 업로드':
  st.header('📂 포트폴리오 엑셀 파일 업로드')
  uploaded_file = st.file_uploader('엑셀 파일 선택 (.xlsx)', type=['xlsx'])
  if uploaded_file is not None:
    try:
      df = pd.read_excel(uploaded_file)
      st.subheader('👀 업로드 데이터 미리보기')
      st.dataframe(df.head(10), use_container_width=True)

      required_cols = [
          'record_date',
          'broker',
          'account_num',
          'item_name',
          'quantity',
          'currency',
      ]
      missing_cols = [c for c in required_cols if c not in df.columns]

      if missing_cols:
        st.error(f'필수 항목 누락: {missing_cols}')
      else:
        if st.button('💾 DB에 포트폴리오 저장', type='primary'):
          df['record_date'] = pd.to_datetime(df['record_date']).dt.strftime(
              '%Y-%m-%d'
          )
          df['account_num'] = df['account_num'].astype(str).str.strip()
          df['broker'] = df['broker'].astype(str).str.strip()

          conn = get_connection()
          c = conn.cursor()
          optional_cols = [
              'account_type',
              'ticker',
              'category1',
              'category2',
              'category3',
              'category4',
              'current_price',
          ]
          for col in optional_cols:
            if col not in df.columns:
              df[col] = None

          for _, row in df.iterrows():
            c.execute(
                '''
                            INSERT INTO portfolio (
                                record_date, broker, account_num, account_type,
                                item_name, ticker, category1, category2,
                                category3, category4, quantity, current_price, currency
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ''',
                (
                    row['record_date'],
                    row['broker'],
                    row['account_num'],
                    row['account_type'],
                    row['item_name'],
                    row['ticker'],
                    row['category1'],
                    row['category2'],
                    row['category3'],
                    row['category4'],
                    row['quantity'],
                    row['current_price'],
                    row['currency'],
                ),
            )
          conn.commit()
          conn.close()
          st.success(f'총 {len(df)}건의 데이터가 성공적으로 저장되었습니다!')
    except Exception as e:
      st.error(f'엑셀 파싱 오류: {e}')

# -----------------------------------------------------------------------------
# 메뉴 4: 원금 및 입출금 관리
# -----------------------------------------------------------------------------
elif menu == '원금 및 입출금 관리':
  st.header('💰 원금 및 입출금 관리')
  tab1, tab2 = st.tabs(['1. 계좌별 최초 원금 설정', '2. 입출금 내역 관리'])

  with tab1:
    conn = get_connection()
    pf_df = pd.read_sql(
        'SELECT DISTINCT broker, account_num FROM portfolio', conn
    )
    init_df = pd.read_sql('SELECT * FROM initial_principal', conn)
    conn.close()

    pf_df['account_num'] = pf_df['account_num'].astype(str)
    init_df['account_num'] = init_df['account_num'].astype(str)
    merged_init = pd.merge(
        pf_df, init_df, on='account_num', how='outer', suffixes=('', '_init')
    )

    if 'broker_init' in merged_init.columns:
      merged_init['broker'] = merged_init['broker'].fillna(
          merged_init['broker_init']
      )
      merged_init.drop(columns=['broker_init'], inplace=True)

    merged_init['broker'] = merged_init['broker'].fillna('증권사미지정')
    merged_init['initial_amount'] = merged_init['initial_amount'].fillna(0.0)

    if not merged_init.empty:
      with st.form('init_principal_form'):
        input_amounts = {}
        for idx, row in merged_init.iterrows():
          acc_str = str(row['account_num'])
          broker_str = row.get('broker', '증권사미지정')
          alias_val = alias_map.get(acc_str, '')
          disp_name = (
              f"{broker_str} |"
              f" {alias_val if alias_val else '별칭미지정'} ({acc_str})"
          )

          col_a, col_b = st.columns([3, 2])
          with col_a:
            st.write(f'**{disp_name}**')
          with col_b:
            val = st.number_input(
                f'최초 원금 (원) - {acc_str}',
                value=float(row['initial_amount']),
                step=1000000.0,
                key=f'init_val_{acc_str}',
            )
            input_amounts[acc_str] = (broker_str, val)

        if st.form_submit_button('💾 최초 원금 저장'):
          conn = get_connection()
          c = conn.cursor()
          for acc_num, (b_name, amount) in input_amounts.items():
            c.execute(
                """
                            INSERT INTO initial_principal (account_num, broker, initial_amount)
                            VALUES (?, ?, ?)
                            ON CONFLICT(account_num) DO UPDATE SET
                            broker=excluded.broker,
                            initial_amount=excluded.initial_amount
                        """,
                (str(acc_num), b_name, amount),
            )
          conn.commit()
          conn.close()
          st.success('저장되었습니다.')
          st.rerun()

  with tab2:
    conn = get_connection()
    pf_df = pd.read_sql(
        'SELECT DISTINCT broker, account_num FROM portfolio', conn
    )
    cf_df = pd.read_sql(
        'SELECT * FROM cash_flow ORDER BY trans_date DESC', conn
    )
    conn.close()

    if not pf_df.empty:
      acc_dict = {
          f"{row.get('broker', '증권사미지정')} | {alias_map.get(str(row['account_num']), '별칭미지정')} ({row['account_num']})": str(
              row['account_num']
          )
          for _, row in pf_df.iterrows()
      }
      with st.form('cash_flow_form'):
        st.markdown('##### ➕ 신규 입출금 내역 등록')
        c1, c2, c3, c4 = st.columns([3, 2, 2, 3])
        with c1:
          sel_acc_label = st.selectbox('계좌 선택', options=list(acc_dict.keys()))
        with c2:
          cf_date = st.date_input('거래일자', date.today())
        with c3:
          cf_type = st.selectbox('구분', ['입금', '출금'])
        with c4:
          cf_amount = st.number_input('금액 (원)', value=0.0, step=100000.0)
        cf_note = st.text_input('비고', '')

        if st.form_submit_button('📥 입출금 내역 저장'):
          if cf_amount > 0:
            conn = get_connection()
            c = conn.cursor()
            c.execute(
                '''
                            INSERT INTO cash_flow (trans_date, account_num, flow_type, amount, note)
                            VALUES (?, ?, ?, ?, ?)
                        ''',
                (
                    cf_date.strftime('%Y-%m-%d'),
                    acc_dict[sel_acc_label],
                    cf_type,
                    cf_amount,
                    cf_note,
                ),
            )
            conn.commit()
            conn.close()
            st.success('등록 완료')
            st.rerun()

      if not cf_df.empty:
        cf_df['account_label'] = cf_df['account_num'].map(
            lambda x: f"{alias_map.get(str(x), '별칭미지정')} ({x})"
        )
        st.dataframe(
            cf_df[
                [
                    'id',
                    'trans_date',
                    'account_label',
                    'flow_type',
                    'amount',
                    'note',
                ]
            ],
            use_container_width=True,
        )

# -----------------------------------------------------------------------------
# 메뉴 5: 등록 데이터 조회
# -----------------------------------------------------------------------------
elif menu == '등록 데이터 조회':
  st.header('🔍 DB에 등록된 데이터 조회 및 관리')
  conn = get_connection()
  pf_df = pd.read_sql(
      'SELECT * FROM portfolio ORDER BY record_date DESC', conn
  )
  init_df = pd.read_sql('SELECT * FROM initial_principal', conn)
  cf_df = pd.read_sql('SELECT * FROM cash_flow', conn)
  alias_df = pd.read_sql('SELECT * FROM account_alias', conn)
  conn.close()

  tab1, tab2, tab3, tab4 = st.tabs(
      ['포트폴리오 데이터', '최초 원금 데이터', '입출금 데이터', '계좌 별칭 데이터']
  )
  with tab1:
    if not pf_df.empty:
      st.dataframe(pf_df, use_container_width=True)
  with tab2:
    if not init_df.empty:
      st.dataframe(init_df, use_container_width=True)
  with tab3:
    if not cf_df.empty:
      st.dataframe(cf_df, use_container_width=True)
  with tab4:
    if not alias_df.empty:
      st.dataframe(alias_df, use_container_width=True)