import os
import sqlite3
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
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
# 2. 티커 포맷팅 및 개선된 시세 수집 함수
# -----------------------------------------------------------------------------
def format_ticker(t):
  """연금 계좌 종목 및 국내 ETF/주식 Ticker 포맷 표준화 함수"""
  if pd.isna(t) or str(t).strip() == '' or str(t).strip().lower() == 'nan':
    return None
  t_str = str(t).strip()

  if t_str.endswith('.0'):
    t_str = t_str[:-2]

  if t_str.isdigit():
    t_str = t_str.zfill(6) + '.KS'

  return t_str


def get_latest_price_single(ticker_symbol):
  """개별 티커의 최신 가격을 가져옵니다."""
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

    data = data.bfill().ffill()

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
# 3. Plotly 레이아웃 및 범주 헬퍼 함수
# -----------------------------------------------------------------------------
def build_legend_config(mode_str):
  """모바일 가독성 향상 레이아웃 설정"""
  if mode_str == '우측 배치':
    return (
        dict(
            orientation='v',
            yanchor='top',
            y=1.0,
            xanchor='left',
            x=1.02,
            font=dict(size=10),
        ),
        True,
        dict(t=120, b=50, l=10, r=140),
    )
  elif mode_str == '하단 배치':
    return (
        dict(
            orientation='h',
            yanchor='top',
            y=-0.35,
            xanchor='left',
            x=0,
            entrywidthmode='pixels',
            entrywidth=100,
            font=dict(size=10),
        ),
        True,
        dict(t=120, b=140, l=10, r=20),
    )
  else:  # '숨김'
    return dict(), False, dict(t=120, b=50, l=10, r=20)


def render_resizable_plotly_chart(fig, key):
  st.plotly_chart(
      fig,
      use_container_width=True,
      key=key,
      config={
          'responsive': True,
          'displayModeBar': True,
          'displaylogo': False,
      },
  )


# -----------------------------------------------------------------------------
# 4. Streamlit 대시보드 메인
# -----------------------------------------------------------------------------
st.set_page_config(page_title='원금 대비 평가액 TREND 관리', layout='wide')
st.title('📈 자산 평가액 및 수익률 분석 시스템')

menu = st.sidebar.selectbox(
    '메뉴 선택',
    [
        '트렌드 리포트',
        '계좌 별칭 관리',
        '포트폴리오 업로드',
        '원금 및 입출금 관리',
        '등록 데이터 조회 및 웹 수정',
    ],
)

alias_map = get_account_aliases()

bm_ticker_map = {
    '미국 SPY': 'SPY',
    '미국 QQQ': 'QQQ',
    '한국 KOSPI': '^KS11',
    'KRW/USD 환율': 'KRW=X',
}

bm_styles = {
    '미국 SPY': dict(color='#7f7f7f', dash='dash'),
    '미국 QQQ': dict(color='#17becf', dash='dash'),
    '한국 KOSPI': dict(color='#e377c2', dash='dash'),
    'KRW/USD 환율': dict(color='#2ca02c', dash='dashdot'),
}

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
    whose_mapping = {}
    for _, row in acc_info_df.iterrows():
      acc_num = str(row['account_num'])
      alias = alias_map.get(acc_num, '')
      broker_str = row['broker']
      combined_str = f'{acc_num} {alias} {broker_str}'
      if '소희' in combined_str or 'SH' in combined_str or 'sh' in combined_str:
        whose_mapping[acc_num] = 'SH'
      else:
        whose_mapping[acc_num] = 'BJ'
    acc_info_df['whose'] = acc_info_df['account_num'].map(whose_mapping)
    all_whose_options = sorted(acc_info_df['whose'].unique().tolist())

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
    bm_options = ['미국 SPY', '미국 QQQ', '한국 KOSPI', 'KRW/USD 환율']

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

      selected_whose = st.multiselect(
          '👤 WHOSE 선택 (우선순위)',
          options=all_whose_options,
          default=st.session_state.get(
              'trend_sel_whose', all_whose_options
          ),
          help='선택한 WHOSE 소유의 계좌만 아래 계좌 선택 목록에 표시됩니다.',
      )

      filtered_acc_info = acc_info_df[
          acc_info_df['whose'].isin(selected_whose)
      ]

      acc_options = []
      for _, row in filtered_acc_info.iterrows():
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

      f_col1, f_col2 = st.columns([3, 3])

      with f_col1:
        default_accs = [
            lbl
            for lbl in st.session_state.get('trend_sel_accs', acc_options)
            if lbl in acc_options
        ]
        if not default_accs:
          default_accs = acc_options

        selected_acc_labels = st.multiselect(
            '조회할 계좌 선택 (WHOSE 연동)',
            options=acc_options,
            default=default_accs,
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
            help='선택한 벤치마크 지수의 수익률 트렌드가 표시됩니다.',
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

      st.write('---')
      run_button = st.form_submit_button('🚀 데이터 계산 실행 (Run)')

    if run_button:
      st.session_state['trend_sel_whose'] = selected_whose
      st.session_state['trend_sel_accs'] = selected_acc_labels
      st.session_state['trend_view_types'] = view_types
      st.session_state['trend_sel_bm'] = selected_bm
      st.session_state['trend_freq_str'] = option_freq
      st.session_state['trend_start_date'] = start_date
      st.session_state['trend_end_date'] = end_date

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
      unique_tickers = filtered_pf_df['ticker'].dropna().unique().tolist()
      fetch_tickers = list(unique_tickers) + list(bm_ticker_map.values())

      with st.spinner('최신 시세를 수집하고 트렌드를 계산 중입니다...'):
        s_str = (start_date - pd.Timedelta(days=10)).strftime('%Y-%m-%d')
        e_str = (pd.to_datetime(end_date) + pd.Timedelta(days=2)).strftime(
            '%Y-%m-%d'
        )
        market_data = fetch_market_data(
            fetch_tickers, s_str, e_str, force_refresh=True
        )

      first_t_str = target_dates[0].strftime('%Y-%m-%d')
      usd_krw_first = None
      if 'KRW=X' in market_data.columns and not market_data.empty:
        if pd.to_datetime(first_t_str) in market_data.index:
          usd_krw_first = market_data.loc[pd.to_datetime(first_t_str), 'KRW=X']
        else:
          usd_krw_first = market_data['KRW=X'].asof(pd.to_datetime(first_t_str))

      if (
          pd.isna(usd_krw_first)
          or usd_krw_first is None
          or usd_krw_first <= 0
      ):
        if (
            'KRW=X' in market_data.columns
            and not market_data['KRW=X'].dropna().empty
        ):
          usd_krw_first = float(market_data['KRW=X'].dropna().iloc[0])
        else:
          usd_krw_first = 1350.0

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

          combined_str = f'{acc} {acc_alias_val} {broker_name}'
          if (
              '소희' in combined_str
              or 'SH' in combined_str
              or 'sh' in combined_str
          ):
            whose_val = 'SH'
          else:
            whose_val = 'BJ'

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
            total_acc_eval_ex_fx = 0
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
                price = base_price if base_price > 0 else 0
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
                    price = float(market_data[fmt_tk].dropna().iloc[0])

                if pd.isna(price) or price == 0:
                  price = base_price

              item_eval = (
                  (qty * price * usd_krw) if curr == 'USD' else (qty * price)
              )
              item_eval_ex_fx = (
                  (qty * price * usd_krw_first)
                  if curr == 'USD'
                  else (qty * price)
              )

              total_acc_eval += item_eval
              total_acc_eval_ex_fx += item_eval_ex_fx
              item_eval_list.append(
                  (item_name, cat4, item_eval, item_eval_ex_fx)
              )

            item_count = len(item_eval_list)
            for item_name, cat4, item_eval, item_eval_ex in item_eval_list:
              if total_acc_eval > 0:
                ratio = item_eval / total_acc_eval
              else:
                ratio = 1.0 / item_count if item_count > 0 else 0

              if total_acc_eval_ex_fx > 0:
                ratio_ex = item_eval_ex / total_acc_eval_ex_fx
              else:
                ratio_ex = 1.0 / item_count if item_count > 0 else 0

              item_principal = principal * ratio
              item_p_loss = item_eval - item_principal

              item_principal_ex = principal * ratio_ex
              item_p_loss_ex = item_eval_ex - item_principal_ex

              base_records.append({
                  'Date': t_str,
                  'account_num': acc_label,
                  'broker': broker_name,
                  'account_type': acc_type,
                  'category4': cat4,
                  'item_name': item_name,
                  'whose': whose_val,
                  '원금': item_principal,
                  '평가손익': item_p_loss,
                  '총평가금액': item_eval,
                  '원금_ex_fx': item_principal_ex,
                  '평가손익_ex_fx': item_p_loss_ex,
                  '총평가금액_ex_fx': item_eval_ex,
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
              p = float(market_data[tk].dropna().iloc[0])

            prices.append(p)
            dates_str.append(t_str)

          bm_df = (
              pd.DataFrame({'Date_str': dates_str, 'Close': prices})
              .bfill()
              .ffill()
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

      disp_col1, disp_col2, disp_col3, disp_col4 = st.columns([2.5, 2.5, 2, 3])

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
        global_legend_pos = st.selectbox(
            '📌 공통 범례(Legend) 기본 배치',
            options=['하단 배치', '우측 배치'],
            index=0,
            key='live_legend_pos',
            help='기본 범례 위치를 선택합니다. 각 차트별로 개별 변경도 가능합니다.',
        )

      with disp_col3:
        y_range_mode = st.radio(
            '🎯 Y축 Range(범위) 지정',
            options=['자동 (Auto)', '수동 지정 (Manual)'],
            horizontal=True,
            key='live_y_range_mode',
        )

      y_min_val, y_max_val = None, None
      with disp_col4:
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

      def draw_group_summary_charts(raw_df, group_col, prefix):
        st.markdown(f'### 📊 [{prefix}] 전체 종합 비교 분석')

        def get_grp_agg(use_ex_fx):
          df_curr = raw_df.copy()
          if use_ex_fx and '원금_ex_fx' in df_curr.columns:
            df_curr['원금'] = df_curr['원금_ex_fx']
            df_curr['평가손익'] = df_curr['평가손익_ex_fx']
            df_curr['총평가금액'] = df_curr['총평가금액_ex_fx']

          grp_agg = (
              df_curr.groupby(['Date', group_col])[
                  ['원금', '평가손익', '총평가금액']
              ]
              .sum()
              .reset_index()
          )

          grp_agg['dt_temp'] = pd.to_datetime(grp_agg['Date'])
          grp_agg = grp_agg.sort_values(
              [group_col, 'dt_temp'], ascending=True
          ).reset_index(drop=True)
          grp_agg['Chart_Date'] = grp_agg['dt_temp'].dt.strftime('%Y-%m-%d')
          grp_agg.drop(columns=['dt_temp'], inplace=True)

          grp_agg['수익률'] = np.where(
              grp_agg['원금'] > 0, (grp_agg['평가손익'] / grp_agg['원금']) * 100, 0
          )
          grp_agg['주기별 평가손익'] = grp_agg.groupby(group_col)[
              '총평가금액'
          ].diff()
          first_p_loss = grp_agg.groupby(group_col)['평가손익'].transform(
              'first'
          )
          grp_agg['선택구간 누적손익'] = grp_agg['평가손익'] - first_p_loss

          grp_agg['prev_eval'] = grp_agg.groupby(group_col)['총평가금액'].shift(1)
          grp_agg['prev_principal'] = grp_agg.groupby(group_col)['원금'].shift(1)
          grp_agg['period_cash_flow'] = grp_agg['원금'] - grp_agg['prev_principal']
          grp_agg['period_start_base'] = grp_agg['prev_eval'].fillna(grp_agg['총평가금액']) + grp_agg['period_cash_flow'].fillna(0)
          
          grp_agg['주기별 수익률'] = np.where(
              grp_agg['period_start_base'] > 0,
              (grp_agg['주기별 평가손익'] / grp_agg['period_start_base']) * 100,
              0
          )
          
          grp_agg['growth_factor'] = 1 + (grp_agg['주기별 수익률'].fillna(0) / 100)
          grp_agg['누적_성장지수'] = grp_agg.groupby(group_col)['growth_factor'].cumprod()
          grp_agg['선택기간 누적 수익률'] = (grp_agg['누적_성장지수'] - 1) * 100

          return grp_agg

        grp_agg_def = get_grp_agg(False)
        date_order_list = sorted(grp_agg_def['Chart_Date'].unique().tolist())

        latest_date = raw_df['Date'].max()
        latest_df = raw_df[raw_df['Date'] == latest_date]
        group_order = (
            latest_df.groupby(group_col)['총평가금액']
            .sum()
            .sort_values(ascending=False)
            .index.tolist()
        )
        all_groups = grp_agg_def[group_col].unique()
        groups = group_order + [g for g in all_groups if g not in group_order]

        # -------------------------------------------------------------------------
        # 각 항목별 최종 핵심지표 요약 표 (환차손 제외 보기 토글 추가)
        # -------------------------------------------------------------------------
        st.markdown(f'### 📋 [{prefix}] 항목별 최종 핵심지표 요약 표')
        ex_summary = st.toggle('🔀 환차손 제외 결과로 보기', key=f'ex_summary_{prefix}')
        
        grp_agg_summary = get_grp_agg(ex_summary)
        latest_df_summary = grp_agg_summary[grp_agg_summary['Date'] == latest_date]

        summary_table_data = []
        total_eval_sum = 0
        total_pl_sum = 0
        total_principal_sum = 0

        for grp in groups:
          sub = grp_agg_summary[grp_agg_summary[group_col] == grp]
          if not sub.empty:
            final_p_loss = sub['선택구간 누적손익'].iloc[-1]
            final_eval = sub['총평가금액'].iloc[-1]
            final_ret = sub['선택기간 누적 수익률'].iloc[-1]
            
            # 환차손 제외 여부에 따른 원금 참조 분기
            if ex_summary and '원금_ex_fx' in raw_df.columns:
              sub_raw = raw_df[(raw_df[group_col] == grp) & (raw_df['Date'] == latest_date)]
              final_principal = sub_raw['원금_ex_fx'].sum() if not sub_raw.empty else 0
            else:
              sub_raw = raw_df[(raw_df[group_col] == grp) & (raw_df['Date'] == latest_date)]
              final_principal = sub_raw['원금'].sum() if not sub_raw.empty else 0

            total_eval_sum += final_eval
            total_pl_sum += final_p_loss
            total_principal_sum += final_principal

            summary_table_data.append({
                prefix: grp,
                '선택구간 누적 평가 손익 (원)': final_p_loss,
                '최종 기말 평가 금액 (원)': final_eval,
                '선택구간 누적 수익률 (%)': final_ret,
                '_eval': final_eval,
                '_principal': final_principal,
            })

        for row in summary_table_data:
          row['점유율 (%)'] = (row['_eval'] / total_eval_sum * 100) if total_eval_sum > 0 else 0
          del row['_eval']
          del row['_principal']

        total_ret = (total_pl_sum / total_principal_sum * 100) if total_principal_sum > 0 else 0
        summary_table_data.append({
            prefix: '전체 합산',
            '선택구간 누적 평가 손익 (원)': total_pl_sum,
            '최종 기말 평가 금액 (원)': total_eval_sum,
            '선택구간 누적 수익률 (%)': total_ret,
            '점유율 (%)': 100.0 if total_eval_sum > 0 else 0.0,
        })

        summary_df = pd.DataFrame(summary_table_data)
        cols = [prefix, '선택구간 누적 평가 손익 (원)', '최종 기말 평가 금액 (원)', '점유율 (%)', '선택구간 누적 수익률 (%)']
        summary_df = summary_df[[c for c in cols if c in summary_df.columns]]

        table_title_suffix = ' (환차손 제외)' if ex_summary else ''
        st.dataframe(
            summary_df.style.format({
                '선택구간 누적 평가 손익 (원)': '{:,.0f}',
                '최종 기말 평가 금액 (원)': '{:,.0f}',
                '점유율 (%)': '{:.2f}%',
                '선택구간 누적 수익률 (%)': '{:.2f}%',
            }),
            use_container_width=True,
        )
        st.write('---')

        st.markdown(f'##### ⚙️ [{prefix}] 종합 차트별 범주 및 환율 옵션 설정')

        cb_c1, cb_c2, cb_c3, cb_c4 = st.columns(4)
        leg_pos_options = ['하단 배치', '우측 배치', '숨김']
        default_idx = 0 if global_legend_pos == '하단 배치' else 1

        with cb_c1:
          pos_g1 = st.selectbox(
              '선택누적손익',
              leg_pos_options,
              index=default_idx,
              key=f'pos_g1_{prefix}',
          )
          ex_g1 = st.toggle('🔀 환차손제외', key=f'ex_g1_{prefix}')
        with cb_c2:
          pos_g2 = st.selectbox(
              '주기별손익',
              leg_pos_options,
              index=default_idx,
              key=f'pos_g2_{prefix}',
          )
          ex_g2 = st.toggle('🔀 환차손제외', key=f'ex_g2_{prefix}')
        with cb_c3:
          pos_g3 = st.selectbox(
              '주기별수익률',
              leg_pos_options,
              index=default_idx,
              key=f'pos_g3_{prefix}',
          )
          ex_g3 = st.toggle('🔀 환차손제외', key=f'ex_g3_{prefix}')
        with cb_c4:
          pos_g4 = st.selectbox(
              '기간누적수익률',
              leg_pos_options,
              index=default_idx,
              key=f'pos_g4_{prefix}',
          )
          ex_g4 = st.toggle('🔀 환차손제외', key=f'ex_g4_{prefix}')

        grp_agg_g1 = get_grp_agg(ex_g1)
        fig_sel_p = go.Figure()
        for grp in groups:
          sub = grp_agg_g1[grp_agg_g1[group_col] == grp]
          fig_sel_p.add_trace(
              go.Scatter(
                  x=sub['Chart_Date'],
                  y=sub['선택구간 누적손익'],
                  name=f'{grp}',
                  mode='lines+markers',
                  hovertemplate='%{y:,.0f} 원',
              )
          )

        leg_cfg_g1, show_g1, margin_g1 = build_legend_config(pos_g1)
        suf_g1 = ' (환차손제외)' if ex_g1 else ''
        fig_sel_p.update_layout(
            title=dict(
                text=f'🔹 [{prefix}] 선택 구간 누적 평가손익 Trend{suf_g1}',
                y=0.95,
                x=0.01,
                xanchor='left',
                yanchor='top',
                yref='container',
            ),
            hovermode='closest',
            height=500,
            margin=margin_g1,
            showlegend=show_g1,
            legend=leg_cfg_g1,
        )
        fig_sel_p.update_xaxes(
            type='category',
            categoryorder='array',
            categoryarray=date_order_list,
        )
        apply_y_axis_config(fig_sel_p, axis_name='yaxis', is_money=True)
        fig_sel_p.update_yaxes(
            title_text='선택구간 누적손익 (원)', tickformat=',.0f'
        )

        grp_agg_g2 = get_grp_agg(ex_g2)
        fig_period_p = go.Figure()
        for grp in groups:
          sub = grp_agg_g2[grp_agg_g2[group_col] == grp].dropna(
              subset=['주기별 평가손익']
          )
          fig_period_p.add_trace(
              go.Bar(
                  x=sub['Chart_Date'], y=sub['주기별 평가손익'], name=str(grp)
              )
          )

        leg_cfg_g2, show_g2, margin_g2 = build_legend_config(pos_g2)
        suf_g2 = ' (환차손제외)' if ex_g2 else ''
        fig_period_p.update_layout(
            title=dict(
                text=(
                    f'🔹 [{prefix}] 선택 기간 주기별 평가손익 Trend (세로 누적'
                    f' 막대){suf_g2}'
                ),
                y=0.95,
                x=0.01,
                xanchor='left',
                yanchor='top',
                yref='container',
            ),
            barmode='relative',
            hovermode='closest',
            height=500,
            margin=margin_g2,
            showlegend=show_g2,
            legend=leg_cfg_g2,
        )
        fig_period_p.update_xaxes(
            type='category',
            categoryorder='array',
            categoryarray=date_order_list,
        )
        apply_y_axis_config(fig_period_p, is_money=True)
        fig_period_p.update_yaxes(title_text='손익금액 (원)', tickformat=',.0f')

        grp_agg_g3 = get_grp_agg(ex_g3)
        fig_period_ret = go.Figure()
        for grp in groups:
          sub = grp_agg_g3[grp_agg_g3[group_col] == grp].dropna(
              subset=['주기별 수익률']
          )
          fig_period_ret.add_trace(
              go.Scatter(
                  x=sub['Chart_Date'],
                  y=sub['주기별 수익률'],
                  name=str(grp),
                  mode='lines+markers',
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
          fig_period_ret.add_trace(
              go.Scatter(
                  x=sub_bm['Chart_Date'],
                  y=sub_bm['주기별 수익률'],
                  mode='lines',
                  name=f'📌 {bm_name}',
                  line=bm_styles.get(bm_name, dict(dash='dot')),
                  hovertemplate='%{y:.2f}%',
              )
          )

        leg_cfg_g3, show_g3, margin_g3 = build_legend_config(pos_g3)
        suf_g3 = ' (환차손제외)' if ex_g3 else ''
        fig_period_ret.update_layout(
            title=dict(
                text=(
                    f'🔹 [{prefix}] 선택기간 주기별 수익률 Trend (꺾은선,'
                    f' 벤치마크 포함){suf_g3}'
                ),
                y=0.95,
                x=0.01,
                xanchor='left',
                yanchor='top',
                yref='container',
            ),
            hovermode='closest',
            height=500,
            margin=margin_g3,
            showlegend=show_g3,
            legend=leg_cfg_g3,
        )
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

        grp_agg_g4 = get_grp_agg(ex_g4)
        fig_cum_ret = make_subplots(specs=[[{'secondary_y': True}]])
        for grp in groups:
          sub = grp_agg_g4[grp_agg_g4[group_col] == grp]
          fig_cum_ret.add_trace(
              go.Bar(
                  x=sub['Chart_Date'],
                  y=sub['선택구간 누적손익'],
                  name=f'[손익] {grp}',
                  opacity=0.7,
                  hovertemplate='%{y:,.0f} 원',
              ),
              secondary_y=True,
          )

        for grp in groups:
          sub = grp_agg_g4[grp_agg_g4[group_col] == grp]
          fig_cum_ret.add_trace(
              go.Scatter(
                  x=sub['Chart_Date'],
                  y=sub['선택기간 누적 수익률'],
                  name=f'[수익률] {grp}',
                  mode='lines+markers',
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
          fig_cum_ret.add_trace(
              go.Scatter(
                  x=bm_df['Chart_Date'],
                  y=bm_df['기간 누적 수익률'],
                  mode='lines',
                  name=f'📌 {bm_name}',
                  line=bm_styles.get(bm_name, dict(dash='dot')),
                  hovertemplate='%{y:.2f}%',
              ),
              secondary_y=False,
          )

        leg_cfg_g4, show_g4, margin_g4 = build_legend_config(pos_g4)
        suf_g4 = ' (환차손제외)' if ex_g4 else ''
        fig_cum_ret.update_layout(
            title=dict(
                text=(
                    f'🔹 [{prefix}] 선택기간 누적 수익률 Trend (좌축) &'
                    f' 선택기간 누적평가 손익 (우측 보조축 그룹 막대){suf_g4}'
                ),
                y=0.95,
                x=0.01,
                xanchor='left',
                yanchor='top',
                yref='container',
            ),
            barmode='group',
            hovermode='closest',
            height=500,
            margin=margin_g4,
            showlegend=show_g4,
            legend=leg_cfg_g4,
        )
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

        if num_cols == 1:
          render_resizable_plotly_chart(
              fig_sel_p, key=f'trend_grp_sel_p_{prefix}'
          )
          render_resizable_plotly_chart(
              fig_period_p, key=f'trend_grp_period_p_{prefix}'
          )
          render_resizable_plotly_chart(
              fig_period_ret, key=f'trend_grp_period_ret_{prefix}'
          )
          render_resizable_plotly_chart(
              fig_cum_ret, key=f'trend_grp_cum_ret_{prefix}'
          )
        elif num_cols == 2:
          col1, col2 = st.columns(2)
          with col1:
            render_resizable_plotly_chart(
                fig_sel_p, key=f'trend_grp_sel_p_{prefix}'
            )
          with col2:
            render_resizable_plotly_chart(
                fig_period_p, key=f'trend_grp_period_p_{prefix}'
            )
          col3, col4 = st.columns(2)
          with col3:
            render_resizable_plotly_chart(
                fig_period_ret, key=f'trend_grp_period_ret_{prefix}'
            )
          with col4:
            render_resizable_plotly_chart(
                fig_cum_ret, key=f'trend_grp_cum_ret_{prefix}'
            )
        else:
          col1, col2, col3 = st.columns(3)
          with col1:
            render_resizable_plotly_chart(
                fig_sel_p, key=f'trend_grp_sel_p_{prefix}'
            )
          with col2:
            render_resizable_plotly_chart(
                fig_period_p, key=f'trend_grp_period_p_{prefix}'
            )
          with col3:
            render_resizable_plotly_chart(
                fig_period_ret, key=f'trend_grp_period_ret_{prefix}'
            )
          render_resizable_plotly_chart(
              fig_cum_ret, key=f'trend_grp_cum_ret_{prefix}'
          )

      def render_total_whose_charts(raw_df):
        st.markdown('### 📊 [전체 합산 - WHOSE별 분석]')

        def get_whose_agg(use_ex_fx):
          df_curr = raw_df.copy()
          if use_ex_fx and '원금_ex_fx' in df_curr.columns:
            df_curr['원금'] = df_curr['원금_ex_fx']
            df_curr['평가손익'] = df_curr['평가손익_ex_fx']
            df_curr['총평가금액'] = df_curr['총평가금액_ex_fx']

          grp_agg = (
              df_curr.groupby(['Date', 'whose'])[
                  ['원금', '평가손익', '총평가금액']
              ]
              .sum()
              .reset_index()
          )
          grp_agg['dt_temp'] = pd.to_datetime(grp_agg['Date'])
          grp_agg = grp_agg.sort_values(
              ['whose', 'dt_temp'], ascending=True
          ).reset_index(drop=True)
          grp_agg['Chart_Date'] = grp_agg['dt_temp'].dt.strftime('%Y-%m-%d')
          grp_agg.drop(columns=['dt_temp'], inplace=True)

          grp_agg['주기별 평가손익'] = grp_agg.groupby('whose')[
              '총평가금액'
          ].diff()
          first_p_loss = grp_agg.groupby('whose')['평가손익'].transform(
              'first'
          )
          grp_agg['선택구간 누적손익'] = grp_agg['평가손익'] - first_p_loss

          grp_agg['수익률'] = np.where(
              grp_agg['원금'] > 0, (grp_agg['평가손익'] / grp_agg['원금']) * 100, 0
          )
          
          grp_agg['prev_eval'] = grp_agg.groupby('whose')['총평가금액'].shift(1)
          grp_agg['prev_principal'] = grp_agg.groupby('whose')['원금'].shift(1)
          grp_agg['period_cash_flow'] = grp_agg['원금'] - grp_agg['prev_principal']
          grp_agg['period_start_base'] = grp_agg['prev_eval'].fillna(grp_agg['총평가금액']) + grp_agg['period_cash_flow'].fillna(0)
          
          grp_agg['주기별 수익률'] = np.where(
              grp_agg['period_start_base'] > 0,
              (grp_agg['주기별 평가손익'] / grp_agg['period_start_base']) * 100,
              0
          )
          
          grp_agg['growth_factor'] = 1 + (grp_agg['주기별 수익률'].fillna(0) / 100)
          grp_agg['누적_성장지수'] = grp_agg.groupby('whose')['growth_factor'].cumprod()
          grp_agg['구간별 누적수익률'] = (grp_agg['누적_성장지수'] - 1) * 100

          return grp_agg

        agg1_def = get_whose_agg(False)
        whose_list = sorted(agg1_def['whose'].unique())

        # -------------------------------------------------------------------------
        # [전체 합산] WHOSE별 최종 핵심지표 요약 표 (환차손 제외 보기 토글 추가)
        # -------------------------------------------------------------------------
        st.markdown('### 📋 [전체 합산] WHOSE별 최종 핵심지표 요약 표')
        ex_whose_summary = st.toggle('🔀 환차손 제외 결과로 보기', key='ex_whose_summary_total')
        
        agg_whose_summary = get_whose_agg(ex_whose_summary)
        latest_date = raw_df['Date'].max()

        whose_summary_data = []
        total_eval_sum = 0
        total_pl_sum = 0
        total_principal_sum = 0

        for w in whose_list:
          sub = agg_whose_summary[agg_whose_summary['whose'] == w]
          if not sub.empty:
            final_p_loss = sub['선택구간 누적손익'].iloc[-1]
            final_eval = sub['총평가금액'].iloc[-1]
            final_ret = sub['구간별 누적수익률'].iloc[-1]
            
            if ex_whose_summary and '원금_ex_fx' in raw_df.columns:
              sub_raw = raw_df[(raw_df['whose'] == w) & (raw_df['Date'] == latest_date)]
              final_principal = sub_raw['원금_ex_fx'].sum() if not sub_raw.empty else 0
            else:
              sub_raw = raw_df[(raw_df['whose'] == w) & (raw_df['Date'] == latest_date)]
              final_principal = sub_raw['원금'].sum() if not sub_raw.empty else 0

            total_eval_sum += final_eval
            total_pl_sum += final_p_loss
            total_principal_sum += final_principal

            whose_summary_data.append({
                'WHOSE': w,
                '선택구간 누적 평가 손익 (원)': final_p_loss,
                '최종 기말 평가 금액 (원)': final_eval,
                '선택구간 누적 수익률 (%)': final_ret,
                '_eval': final_eval,
            })

        for row in whose_summary_data:
          row['점유율 (%)'] = (row['_eval'] / total_eval_sum * 100) if total_eval_sum > 0 else 0
          del row['_eval']

        total_ret = (total_pl_sum / total_principal_sum * 100) if total_principal_sum > 0 else 0
        whose_summary_data.append({
            'WHOSE': '전체 합산',
            '선택구간 누적 평가 손익 (원)': total_pl_sum,
            '최종 기말 평가 금액 (원)': total_eval_sum,
            '선택구간 누적 수익률 (%)': total_ret,
            '점유율 (%)': 100.0 if total_eval_sum > 0 else 0.0,
        })

        whose_summary_df = pd.DataFrame(whose_summary_data)
        cols_w = ['WHOSE', '선택구간 누적 평가 손익 (원)', '최종 기말 평가 금액 (원)', '점유율 (%)', '선택구간 누적 수익률 (%)']
        whose_summary_df = whose_summary_df[[c for c in cols_w if c in whose_summary_df.columns]]

        st.dataframe(
            whose_summary_df.style.format({
                '선택구간 누적 평가 손익 (원)': '{:,.0f}',
                '최종 기말 평가 금액 (원)': '{:,.0f}',
                '점유율 (%)': '{:.2f}%',
                '선택구간 누적 수익률 (%)': '{:.2f}%',
            }),
            use_container_width=True,
        )
        st.write('---')

        st.markdown('##### ⚙️ [전체 합산] 차트별 범주 및 환율 옵션 설정')

        cb_w1, cb_w2, cb_w3, cb_w4 = st.columns(4)
        leg_pos_options = ['하단 배치', '우측 배치', '숨김']
        default_idx = 0 if global_legend_pos == '하단 배치' else 1

        with cb_w1:
          pos_w1 = st.selectbox(
              '1. 전체 자산 평가 금액',
              leg_pos_options,
              index=default_idx,
              key='pos_w1_total',
          )
          ex_w1 = st.toggle('🔀 환차손제외 (1번)', key='ex_w1_total')
        with cb_w2:
          pos_w2 = st.selectbox(
              '2. 구간 손익 금액 추이',
              leg_pos_options,
              index=default_idx,
              key='pos_w2_total',
          )
          ex_w2 = st.toggle('🔀 환차손제외 (2번)', key='ex_w2_total')
        with cb_w3:
          pos_w3 = st.selectbox(
              '3-1. 구간 누적수익률',
              leg_pos_options,
              index=default_idx,
              key='pos_w3_total',
          )
          ex_w3 = st.toggle('🔀 환차손제외 (3-1번)', key='ex_w3_total')
        with cb_w4:
          pos_w4 = st.selectbox(
              '3-2. 주기별 수익률',
              leg_pos_options,
              index=default_idx,
              key='pos_w4_total',
          )
          ex_w4 = st.toggle('🔀 환차손제외 (3-2번)', key='ex_w4_total')

        agg1 = get_whose_agg(ex_w1)
        total_per_date1 = (
            agg1.groupby('Chart_Date')['총평가금액'].sum().reset_index()
        )
        date_order_list = sorted(agg1['Chart_Date'].unique().tolist())
        colors = {'BJ': '#2b5c8f', 'SH': '#ff7f0e'}

        fig1 = go.Figure()
        for w in whose_list:
          sub = agg1[agg1['whose'] == w]
          fig1.add_trace(
              go.Bar(
                  x=sub['Chart_Date'],
                  y=sub['총평가금액'],
                  name=f'총평가금액 ({w})',
                  marker_color=colors.get(w, '#1f77b4'),
              )
          )
        fig1.add_trace(
            go.Scatter(
                x=total_per_date1['Chart_Date'],
                y=total_per_date1['총평가금액'],
                name='전체 합산 총평가금액',
                mode='lines+markers+text',
                line=dict(color='#ff9900', width=3),
                marker=dict(size=6),
                text=[f'{v:,.0f}' for v in total_per_date1['총평가금액']],
                textposition='top center',
            )
        )
        leg_cfg_w1, show_w1, margin_w1 = build_legend_config(pos_w1)
        suf_w1 = ' (환차손제외)' if ex_w1 else ''
        fig1.update_layout(
            title=dict(
                text=(
                    f'1. [전체 자산 평가 금액] WHOSE별 누적 막대 & 전체 합산'
                    f' 꺾은선{suf_w1}'
                ),
                y=0.95,
                x=0.01,
                xanchor='left',
                yanchor='top',
                yref='container',
            ),
            barmode='stack',
            hovermode='closest',
            height=500,
            margin=margin_w1,
            showlegend=show_w1,
            legend=leg_cfg_w1,
        )
        fig1.update_xaxes(
            type='category',
            categoryorder='array',
            categoryarray=date_order_list,
        )
        apply_y_axis_config(fig1, is_money=True)
        fig1.update_yaxes(title_text='금액 (원)', tickformat=',.0f')

        agg2 = get_whose_agg(ex_w2)
        fig2 = make_subplots(specs=[[{'secondary_y': True}]])
        for w in whose_list:
          sub = agg2[agg2['whose'] == w].dropna(subset=['주기별 평가손익'])
          fig2.add_trace(
              go.Bar(
                  x=sub['Chart_Date'],
                  y=sub['주기별 평가손익'],
                  name=f'주기별 평가손익 ({w})',
              ),
              secondary_y=False,
          )
        for w in whose_list:
          sub = agg2[agg2['whose'] == w]
          fig2.add_trace(
              go.Scatter(
                  x=sub['Chart_Date'],
                  y=sub['선택구간 누적손익'],
                  name=f'선택구간 누적손익 ({w})',
                  mode='lines+markers',
              ),
              secondary_y=True,
          )
        leg_cfg_w2, show_w2, margin_w2 = build_legend_config(pos_w2)
        suf_w2 = ' (환차손제외)' if ex_w2 else ''
        fig2.update_layout(
            title=dict(
                text=(
                    f'2. [전체합산] 구간 손익 금액 추이 (WHOSE 기준'
                    f' 분리){suf_w2}'
                ),
                y=0.95,
                x=0.01,
                xanchor='left',
                yanchor='top',
                yref='container',
            ),
            barmode='stack',
            hovermode='closest',
            height=500,
            margin=margin_w2,
            showlegend=show_w2,
            legend=leg_cfg_w2,
        )
        fig2.update_xaxes(
            type='category',
            categoryorder='array',
            categoryarray=date_order_list,
        )
        fig2.update_yaxes(
            title_text='주기별 평가손익 (원)',
            tickformat=',.0f',
            secondary_y=False,
        )
        fig2.update_yaxes(
            title_text='선택구간 누적손익 (원)',
            tickformat=',.0f',
            secondary_y=True,
        )

        agg3a = get_whose_agg(ex_w3)
        fig3a = go.Figure()
        for w in whose_list:
          sub = agg3a[agg3a['whose'] == w]
          fig3a.add_trace(
              go.Scatter(
                  x=sub['Chart_Date'],
                  y=sub['구간별 누적수익률'],
                  name=f'구간별 누적수익률 (%) ({w})',
                  mode='lines+markers',
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
          fig3a.add_trace(
              go.Scatter(
                  x=bm_df['Chart_Date'],
                  y=bm_df['기간 누적 수익률'],
                  mode='lines',
                  name=f'📌 {bm_name}',
                  line=bm_styles.get(bm_name, dict(dash='dot')),
                  hovertemplate='%{y:.2f}%',
              )
          )
        leg_cfg_w3, show_w3, margin_w3 = build_legend_config(pos_w3)
        suf_w3 = ' (환차손제외)' if ex_w3 else ''
        fig3a.update_layout(
            title=dict(
                text=(
                    f'3-1. [전체합산] 구간 누적수익률 추이 (WHOSE 기준'
                    f' 분리){suf_w3}'
                ),
                y=0.95,
                x=0.01,
                xanchor='left',
                yanchor='top',
                yref='container',
            ),
            hovermode='closest',
            height=500,
            margin=margin_w3,
            showlegend=show_w3,
            legend=leg_cfg_w3,
        )
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

        agg3b = get_whose_agg(ex_w4)
        fig3b = go.Figure()
        for w in whose_list:
          sub = agg3b[agg3b['whose'] == w].dropna(subset=['주기별 수익률'])
          fig3b.add_trace(
              go.Scatter(
                  x=sub['Chart_Date'],
                  y=sub['주기별 수익률'],
                  name=f'주기별 수익률 (%) ({w})',
                  mode='lines+markers',
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
          fig3b.add_trace(
              go.Scatter(
                  x=sub_bm['Chart_Date'],
                  y=sub_bm['주기별 수익률'],
                  mode='lines',
                  name=f'📌 {bm_name}',
                  line=bm_styles.get(bm_name, dict(dash='dash')),
                  hovertemplate='%{y:.2f}%',
              )
          )
        leg_cfg_w4, show_w4, margin_w4 = build_legend_config(pos_w4)
        suf_w4 = ' (환차손제외)' if ex_w4 else ''
        fig3b.update_layout(
            title=dict(
                text=(
                    f'3-2. [전체합산] 주기별 수익률 추이 (WHOSE 기준'
                    f' 분리){suf_w4}'
                ),
                y=0.95,
                x=0.01,
                xanchor='left',
                yanchor='top',
                yref='container',
            ),
            hovermode='closest',
            height=500,
            margin=margin_w4,
            showlegend=show_w4,
            legend=leg_cfg_w4,
        )
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

        if num_cols == 1:
          render_resizable_plotly_chart(fig1, key='trend_w_fig1')
          render_resizable_plotly_chart(fig2, key='trend_w_fig2')
          render_resizable_plotly_chart(fig3a, key='trend_w_fig3a')
          render_resizable_plotly_chart(fig3b, key='trend_w_fig3b')
        elif num_cols == 2:
          c_a, c_b = st.columns(2)
          with c_a:
            render_resizable_plotly_chart(fig1, key='trend_w_fig1')
          with c_b:
            render_resizable_plotly_chart(fig2, key='trend_w_fig2')
          c_c, c_d = st.columns(2)
          with c_c:
            render_resizable_plotly_chart(fig3a, key='trend_w_fig3a')
          with c_d:
            render_resizable_plotly_chart(fig3b, key='trend_w_fig3b')
        else:
          c_a, c_b, c_c = st.columns(3)
          with c_a:
            render_resizable_plotly_chart(fig1, key='trend_w_fig1')
          with c_b:
            render_resizable_plotly_chart(fig2, key='trend_w_fig2')
          with c_c:
            render_resizable_plotly_chart(fig3a, key='trend_w_fig3a')
          render_resizable_plotly_chart(fig3b, key='trend_w_fig3b')

      if active_views:
        tabs = st.tabs(active_views)
        for tab, v_type in zip(tabs, active_views):
          with tab:
            if v_type == '전체 합산':
              render_total_whose_charts(calc_df)
            elif v_type == '계좌별':
              draw_group_summary_charts(calc_df, 'account_num', '계좌별')
            elif v_type == '증권사(Broker)별':
              draw_group_summary_charts(calc_df, 'broker', '증권사별')
            elif v_type == '계좌유형별':
              draw_group_summary_charts(calc_df, 'account_type', '계좌유형별')
            elif v_type == 'Category 4별':
              draw_group_summary_charts(calc_df, 'category4', 'Category 4별')
            elif v_type == '보유항목별':
              draw_group_summary_charts(calc_df, 'item_name', '보유항목별')
      else:
        st.info('표시할 트렌드 관점을 선택해 주세요.')

# -----------------------------------------------------------------------------
# 메뉴 2: 계좌 별칭 관리
# -----------------------------------------------------------------------------
elif menu == '계좌 별칭 관리':
  st.header('🏷️ 계좌 별칭 관리')
  conn = get_connection()
  pf_df = pd.read_sql('SELECT DISTINCT broker, account_num, account_type FROM portfolio', conn)
  conn.close()

  if pf_df.empty:
    st.warning('등록된 포트폴리오 데이터가 없습니다.')
  else:
    conn = get_connection()
    current_aliases = pd.read_sql('SELECT * FROM account_alias', conn)
    conn.close()
    alias_dict = dict(zip(current_aliases['account_num'], current_aliases['alias']))

    st.markdown('##### 각 계좌 번호별로 직관적인 별칭을 부여할 수 있습니다.')
    with st.form('alias_form'):
      updated_aliases = {}
      for _, row in pf_df.iterrows():
        b_name = row['broker']
        acc_num = str(row['account_num'])
        acc_type = row['account_type']
        current_val = alias_dict.get(acc_num, '')
        new_val = st.text_input(
            f'[{b_name}] 계좌번호: {acc_num} (유형: {acc_type})',
            value=current_val,
            key=f'alias_{acc_num}',
        )
        updated_aliases[acc_num] = new_val

      submitted = st.form_submit_button('💾 별칭 저장')
      if submitted:
        conn = get_connection()
        c = conn.cursor()
        for acc_num, alias in updated_aliases.items():
          c.execute(
              'INSERT OR REPLACE INTO account_alias (account_num, alias) VALUES'
              ' (?, ?)',
              (acc_num, alias),
          )
        conn.commit()
        conn.close()
        st.success('계좌 별칭이 성공적으로 저장되었습니다!')
        st.rerun()

# -----------------------------------------------------------------------------
# 메뉴 3: 포트폴리오 업로드
# -----------------------------------------------------------------------------
elif menu == '포트폴리오 업로드':
  st.header('📤 포트폴리오 엑셀 업로드')
  uploaded_file = st.file_uploader(
      '포트폴리오 엑셀 파일(.xlsx)을 업로드하세요', type=['xlsx']
  )

  if uploaded_file is not None:
    try:
      df_upload = pd.read_excel(uploaded_file)
      st.write('미리보기 (상위 5행):', df_upload.head())

      if st.button('📥 데이터베이스에 적재하기'):
        conn = get_connection()
        df_upload.to_sql('portfolio', conn, if_exists='append', index=False)
        conn.close()
        st.success('포트폴리오 데이터가 성공적으로 적재되었습니다!')
    except Exception as e:
      st.error(f'파일 업로드 및 적재 중 오류 발생: {e}')

# -----------------------------------------------------------------------------
# 메뉴 4: 원금 및 입출금 관리
# -----------------------------------------------------------------------------
elif menu == '원금 및 입출금 관리':
  st.header('💰 초기 원금 및 추가 입출금 관리')
  conn = get_connection()
  acc_df = pd.read_sql('SELECT DISTINCT broker, account_num FROM portfolio', conn)
  init_df = pd.read_sql('SELECT * FROM initial_principal', conn)
  cf_df = pd.read_sql('SELECT * FROM cash_flow', conn)
  conn.close()

  if acc_df.empty:
    st.warning('등록된 계좌 정보가 없습니다.')
  else:
    tab1, tab2 = st.tabs(['초기 원금 설정', '추가 입출금 내역 관리'])

    with tab1:
      st.subheader('📌 계좌별 초기 원금 설정')
      init_dict = dict(zip(init_df['account_num'], init_df['initial_amount']))
      with st.form('init_principal_form'):
        new_init_data = []
        for _, row in acc_df.iterrows():
          b = row['broker']
          acc = str(row['account_num'])
          cur_val = init_dict.get(acc, 0.0)
          val = st.number_input(
              f'[{b}] {acc}', value=float(cur_val), step=100000.0, key=f'init_{acc}'
          )
          new_init_data.append((acc, b, val))

        if st.form_submit_button('💾 초기 원금 저장'):
          conn = get_connection()
          c = conn.cursor()
          for acc, b, val in new_init_data:
            c.execute(
                'INSERT OR REPLACE INTO initial_principal (account_num, broker,'
                ' initial_amount) VALUES (?, ?, ?)',
                (acc, b, val),
            )
          conn.commit()
          conn.close()
          st.success('초기 원금이 저장되었습니다!')
          st.rerun()

    with tab2:
      st.subheader('➕ 추가 입출금 내역 등록 및 조회')
      with st.form('cash_flow_form'):
        cf_date = st.date_input('거래일자', value=date.today())
        acc_list = acc_df['account_num'].astype(str).tolist()
        cf_acc = st.selectbox('대상 계좌번호', options=acc_list)
        cf_type = st.selectbox('구분', options=['입금', '출금'])
        cf_amount = st.number_input('금액 (원)', value=0.0, step=100000.0)
        cf_note = st.text_input('적요 / 메모')

        if st.form_submit_button('➕ 입출금 내역 추가'):
          conn = get_connection()
          c = conn.cursor()
          c.execute(
              'INSERT INTO cash_flow (trans_date, account_num, flow_type,'
              ' amount, note) VALUES (?, ?, ?, ?, ?)',
              (
                  cf_date.strftime('%Y-%m-%d'),
                  cf_acc,
                  cf_type,
                  cf_amount,
                  cf_note,
              ),
          )
          conn.commit()
          conn.close()
          st.success('입출금 내역이 추가되었습니다!')
          st.rerun()

      st.markdown('##### 📋 등록된 입출금 내역 목록')
      if not cf_df.empty:
        st.dataframe(cf_df, use_container_width=True)
        del_id = st.number_input('삭제할 내역 ID 입력', value=0, step=1)
        if st.button('🗑️ 선택 내역 삭제'):
          conn = get_connection()
          c = conn.cursor()
          c.execute('DELETE FROM cash_flow WHERE id = ?', (del_id,))
          conn.commit()
          conn.close()
          st.success(f'ID {del_id} 내역이 삭제되었습니다.')
          st.rerun()
      else:
        st.info('등록된 입출금 내역이 없습니다.')

# -----------------------------------------------------------------------------
# 메뉴 5: 등록 데이터 조회 및 웹 수정
# -----------------------------------------------------------------------------
elif menu == '등록 데이터 조회 및 웹 수정':
  st.header('🔍 등록 데이터 조회 및 웹 수정')
  conn = get_connection()
  pf_full_df = pd.read_sql('SELECT * FROM portfolio', conn)
  conn.close()

  if pf_full_df.empty:
    st.warning('등록된 포트폴리오 데이터가 없습니다.')
  else:
    st.markdown('##### 포트폴리오 데이터를 조회하고 웹 화면에서 직접 수정/저장할 수 있습니다.')
    
    edited_df = st.data_editor(pf_full_df, num_rows='dynamic', use_container_width=True, key='portfolio_data_editor')

    if st.button('💾 수정 사항 데이터베이스에 반영'):
      try:
        conn = get_connection()
        edited_df.to_sql('portfolio', conn, if_exists='replace', index=False)
        conn.close()
        st.success('포트폴리오 데이터가 성공적으로 갱신되었습니다!')
        st.rerun()
      except Exception as e:
        st.error(f'데이터 갱신 중 오류 발생: {e}')