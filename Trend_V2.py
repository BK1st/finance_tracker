import io
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

  # 포트폴리오 테이블
  c.execute('''
        CREATE TABLE IF NOT EXISTS portfolio (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            record_date TEXT,
            broker TEXT,
            account_num TEXT,
            account_name TEXT,
            account_type TEXT,
            item_name TEXT,
            ticker TEXT,
            category1 TEXT,
            category2 TEXT,
            category3 TEXT,
            category4 TEXT,
            quantity REAL,
            purchase_price REAL,
            current_price REAL,
            eval_price REAL,
            eval_profit_loss REAL,
            return_rate REAL,
            portfolio_weight REAL,
            currency TEXT
        )
    ''')

  # 스키마 마이그레이션
  c.execute('PRAGMA table_info(portfolio)')
  existing_cols = [col[1] for col in c.fetchall()]
  new_cols = {
      'account_name': 'TEXT',
      'purchase_price': 'REAL',
      'eval_price': 'REAL',
      'eval_profit_loss': 'REAL',
      'return_rate': 'REAL',
      'portfolio_weight': 'REAL',
  }
  for col, dtype in new_cols.items():
    if col not in existing_cols:
      c.execute(f'ALTER TABLE portfolio ADD COLUMN {col} {dtype}')

  c.execute('''
        CREATE TABLE IF NOT EXISTS initial_principal (
            account_num TEXT PRIMARY KEY,
            broker TEXT,
            initial_amount REAL
        )
    ''')

  # cash_flow 테이블 (입출금/매매/배당)
  c.execute('''
        CREATE TABLE IF NOT EXISTS cash_flow (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trans_date TEXT,
            owner TEXT,
            broker TEXT,
            account_num TEXT,
            account_name TEXT,
            account_type TEXT,
            flow_type TEXT,
            item_name TEXT,
            ticker TEXT,
            category1 TEXT,
            category2 TEXT,
            category3 TEXT,
            category4 TEXT,
            quantity REAL,
            price REAL,
            amount REAL,
            currency TEXT,
            note TEXT
        )
    ''')

  c.execute('PRAGMA table_info(cash_flow)')
  cf_existing_cols = [col[1] for col in c.fetchall()]
  cf_new_cols = {
      'owner': 'TEXT',
      'broker': 'TEXT',
      'account_name': 'TEXT',
      'account_type': 'TEXT',
      'ticker': 'TEXT',
      'item_name': 'TEXT',
      'category1': 'TEXT',
      'category2': 'TEXT',
      'category3': 'TEXT',
      'category4': 'TEXT',
      'quantity': 'REAL',
      'price': 'REAL',
      'currency': 'TEXT',
  }
  for col, dtype in cf_new_cols.items():
    if col not in cf_existing_cols:
      c.execute(f'ALTER TABLE cash_flow ADD COLUMN {col} {dtype}')

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
# 3. Plotly 레이아웃 및 헬퍼 함수
# -----------------------------------------------------------------------------
def build_legend_config(mode_str):
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
  else:
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
# 4. 매수/매도 거래 이력 반영 보유 주식 계산 함수
# -----------------------------------------------------------------------------
def compute_holdings_from_history(account_filter=None):
  """포트폴리오 기본 데이터와 거래 내역(매수/매도/배당)을 연동하여 계좌/종목별 보유 현황 계산"""
  conn = get_connection()
  pf_df = pd.read_sql('SELECT * FROM portfolio', conn)
  cf_df = pd.read_sql(
      'SELECT * FROM cash_flow ORDER BY trans_date ASC, id ASC', conn
  )
  conn.close()

  if pf_df.empty:
    return pd.DataFrame()

  if account_filter:
    pf_df = pf_df[pf_df['account_num'].astype(str).isin(account_filter)]
    if not cf_df.empty:
      cf_df = cf_df[cf_df['account_num'].astype(str).isin(account_filter)]

  # 가장 최근 포트폴리오 기준일자의 종목 스냅샷 가져오기
  latest_pf_date = pf_df['record_date'].max()
  latest_pf = pf_df[pf_df['record_date'] == latest_pf_date].copy()

  holdings = {}
  for _, row in latest_pf.iterrows():
    acc_num = str(row['account_num'])
    item_name = str(row['item_name'])
    key = (acc_num, item_name)

    holdings[key] = {
        'broker': row.get('broker', ''),
        'account_num': acc_num,
        'account_name': row.get('account_name', ''),
        'account_type': row.get('account_type', ''),
        'item_name': item_name,
        'ticker': row.get('ticker', ''),
        'quantity': float(row.get('quantity', 0) or 0),
        'purchase_price': float(row.get('purchase_price', 0) or 0),
        'current_price': float(row.get('current_price', 0) or 0),
        'currency': row.get('currency', 'KRW'),
        'category1': row.get('category1', ''),
        'category2': row.get('category2', ''),
        'category3': row.get('category3', ''),
        'category4': row.get('category4', ''),
        'realized_pnl': 0.0,
        'dividends': 0.0,
    }

  # cash_flow 상의 매수, 매도, 배당 반영
  if not cf_df.empty:
    for _, row in cf_df.iterrows():
      acc_num = str(row['account_num'])
      item_name = (
          str(row['item_name'])
          if pd.notna(row['item_name']) and str(row['item_name']).strip()
          else None
      )
      flow_type = str(row['flow_type']).strip()
      qty = float(row.get('quantity', 0) or 0)
      price = float(row.get('price', 0) or 0)
      amt = float(row.get('amount', 0) or 0)

      if not item_name or flow_type in ['입금', '출금']:
        continue

      key = (acc_num, item_name)

      if key not in holdings:
        holdings[key] = {
            'broker': row.get('broker', ''),
            'account_num': acc_num,
            'account_name': row.get('account_name', ''),
            'account_type': row.get('account_type', ''),
            'item_name': item_name,
            'ticker': row.get('ticker', ''),
            'quantity': 0.0,
            'purchase_price': price,
            'current_price': price,
            'currency': row.get('currency', 'KRW'),
            'category1': row.get('category1', ''),
            'category2': row.get('category2', ''),
            'category3': row.get('category3', ''),
            'category4': row.get('category4', ''),
            'realized_pnl': 0.0,
            'dividends': 0.0,
        }

      target = holdings[key]

      if flow_type == '매수':
        prev_qty = target['quantity']
        prev_price = target['purchase_price']
        new_qty = prev_qty + qty
        if new_qty > 0:
          new_avg_price = (
              (prev_qty * prev_price) + (qty * price)
          ) / new_qty
          target['purchase_price'] = new_avg_price
        target['quantity'] = new_qty

      elif flow_type == '매도':
        sell_qty = qty
        avg_price = target['purchase_price']
        sell_price = price if price > 0 else (amt / sell_qty if sell_qty else 0)
        realized = sell_qty * (sell_price - avg_price)
        target['realized_pnl'] += realized
        target['quantity'] = max(0.0, target['quantity'] - sell_qty)

      elif flow_type == '배당금':
        target['dividends'] += amt

  records = list(holdings.values())
  res_df = pd.DataFrame(records)

  if res_df.empty:
    return res_df

  # 실시간 최신가 업데이트 적용
  tickers = res_df['ticker'].dropna().unique().tolist()
  if tickers:
    s_date = (date.today() - timedelta(days=5)).strftime('%Y-%m-%d')
    e_date = (date.today() + timedelta(days=1)).strftime('%Y-%m-%d')
    m_data = fetch_market_data(tickers, s_date, e_date)

    usd_krw = 1350.0
    if 'KRW=X' in m_data.columns and not m_data['KRW=X'].dropna().empty:
      usd_krw = float(m_data['KRW=X'].dropna().iloc[-1])

    for idx, row in res_df.iterrows():
      fmt_tk = format_ticker(row['ticker'])
      curr_p = row['current_price']
      if fmt_tk and fmt_tk in m_data.columns and not m_data[fmt_tk].dropna().empty:
        curr_p = float(m_data[fmt_tk].dropna().iloc[-1])
        res_df.loc[idx, 'current_price'] = curr_p

      q = row['quantity']
      p_price = row['purchase_price']
      curr = row['currency']

      multiplier = usd_krw if curr == 'USD' else 1.0

      eval_p = q * curr_p * multiplier
      invested = q * p_price * multiplier
      eval_pnl = eval_p - invested
      ret_rate = ((curr_p - p_price) / p_price * 100) if p_price > 0 else 0.0

      res_df.loc[idx, 'eval_amount'] = eval_p
      res_df.loc[idx, 'invested_amount'] = invested
      res_df.loc[idx, 'eval_pnl'] = eval_pnl
      res_df.loc[idx, 'return_rate'] = ret_rate

  return res_df


# -----------------------------------------------------------------------------
# 5. Streamlit 대시보드 메인
# -----------------------------------------------------------------------------
st.set_page_config(page_title='원금 대비 평가액 TREND 관리', layout='wide')
st.title('📈 자산 평가액 및 수익률 분석 시스템')

menu = st.sidebar.selectbox(
    '메뉴 선택',
    [
        '트렌드 리포트',
        '계좌 별칭 관리',
        '포트폴리오 업로드',
        '원금 및 통합 거래 관리',
        '등록 데이터 조회 및 웹 수정',
        '데이터 백업 및 복구',
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
      st.subheader('⚙️️ 분석 조건 설정')

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
          whose_val = (
              'SH'
              if (
                  '소희' in combined_str
                  or 'SH' in combined_str
                  or 'sh' in combined_str
              )
              else 'BJ'
          )

          init_val = (
              init_p_df[init_p_df['account_num'].astype(str) == acc][
                  'initial_amount'
              ].sum()
              if not init_p_df.empty
              else 0
          )

          in_flow, out_flow, acc_dividends = 0, 0, 0
          if not cf_df.empty:
            acc_cf = cf_df[
                (cf_df['account_num'].astype(str) == acc)
                & (cf_df['trans_date'] <= t_str)
            ]
            in_flow = acc_cf[acc_cf['flow_type'] == '입금']['amount'].sum()
            out_flow = acc_cf[acc_cf['flow_type'] == '출금']['amount'].sum()
            acc_dividends = acc_cf[acc_cf['flow_type'] == '배당금'][
                'amount'
            ].sum()

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
            total_acc_eval, total_acc_eval_ex_fx = 0, 0
            item_eval_list = []

            for _, row in current_pf.iterrows():
              fmt_tk = format_ticker(row.get('ticker'))
              item_name = row.get('item_name', '미지정종목')
              base_qty = (
                  row['quantity']
                  if ('quantity' in row and pd.notna(row['quantity']))
                  else 0
              )

              # 거래 이력(매수/매도) 수량 반영
              add_buy_qty, add_sell_qty = 0, 0
              if not cf_df.empty:
                item_cf = cf_df[
                    (cf_df['account_num'].astype(str) == acc)
                    & (cf_df['item_name'] == item_name)
                    & (cf_df['trans_date'] <= t_str)
                ]
                add_buy_qty = item_cf[item_cf['flow_type'] == '매수'][
                    'quantity'
                ].sum()
                add_sell_qty = item_cf[item_cf['flow_type'] == '매도'][
                    'quantity'
                ].sum()

              qty = max(0.0, base_qty + add_buy_qty - add_sell_qty)
              curr = row.get('currency', 'KRW')
              base_price = (
                  row['current_price']
                  if ('current_price' in row and pd.notna(row['current_price']))
                  else 0
              )
              cat4 = row.get('category4', '미지정')

              price = 0
              if fmt_tk is None:
                price = base_price if base_price > 0 else 0
              else:
                if fmt_tk in market_data.columns and not market_data.empty:
                  if pd.to_datetime(t_str) in market_data.index:
                    price = market_data.loc[pd.to_datetime(t_str), fmt_tk]
                  else:
                    price = market_data[fmt_tk].asof(pd.to_datetime(t_str))

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

            total_acc_eval += acc_dividends
            total_acc_eval_ex_fx += acc_dividends

            item_count = len(item_eval_list)
            for item_name, cat4, item_eval, item_eval_ex in item_eval_list:
              ratio = (
                  (item_eval / total_acc_eval)
                  if total_acc_eval > 0
                  else (1.0 / item_count if item_count > 0 else 0)
              )
              ratio_ex = (
                  (item_eval_ex / total_acc_eval_ex_fx)
                  if total_acc_eval_ex_fx > 0
                  else (1.0 / item_count if item_count > 0 else 0)
              )

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
          prices, dates_str = [], []
          for t_date in target_dates:
            t_str = t_date.strftime('%Y-%m-%d')
            p = (
                market_data.loc[pd.to_datetime(t_str), tk]
                if pd.to_datetime(t_str) in market_data.index
                else market_data[tk].asof(pd.to_datetime(t_str))
            )
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
      st.subheader('🖥️ 화면 디스플레이 설정')

      disp_col1, disp_col2, disp_col3, disp_col4 = st.columns([2.5, 2.5, 2, 3])
      with disp_col1:
        layout_setting = st.selectbox(
            '🖥️ 차트 Layout 선택',
            options=[
                '1열 (기존 세로 배치)',
                '2열 (좌우 2개 분할)',
                '3열 (좌우 3개 분할)',
            ],
            key='live_chart_layout',
        )
      with disp_col2:
        global_legend_pos = st.selectbox(
            '📌 공통 범례(Legend) 기본 배치',
            options=['하단 배치', '우측 배치'],
            key='live_legend_pos',
        )
      with disp_col3:
        y_range_mode = st.radio(
            '🎯 Y축 Range 지정',
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

      num_cols = (
          1
          if '1열' in layout_setting
          else (2 if '2열' in layout_setting else 3)
      )

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

      # -------------------------------------------------------------------------
      # 그룹 요약 차트
      # -------------------------------------------------------------------------
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
          all_target_dates = sorted(raw_df['Date'].unique().tolist())
          filled_groups = []

          for grp in grp_agg[group_col].unique():
            g_df = grp_agg[grp_agg[group_col] == grp].copy()
            g_df['dt'] = pd.to_datetime(g_df['Date'])
            g_df = g_df.sort_values('dt')

            grp_min_date, grp_max_date = g_df['dt'].min(), g_df['dt'].max()
            date_template = pd.DataFrame({'Date': all_target_dates})
            date_template['dt'] = pd.to_datetime(date_template['Date'])

            m = pd.merge(
                date_template,
                g_df,
                on=['Date', 'dt'],
                how='left',
                suffixes=('', '_dup'),
            )
            m[group_col] = grp

            active_mask = (m['dt'] >= grp_min_date) & (m['dt'] <= grp_max_date)
            m['원금'] = pd.Series(
                np.where(active_mask, m['원금'], 0)
            ).ffill().fillna(0)
            m['총평가금액'] = pd.Series(
                np.where(active_mask, m['총평가금액'], 0)
            ).ffill().fillna(0)
            m['평가손익'] = pd.Series(
                np.where(active_mask, m['평가손익'], 0)
            ).ffill().fillna(0)
            filled_groups.append(m)

          grp_agg = pd.concat(filled_groups, ignore_index=True)
          grp_agg['dt_temp'] = pd.to_datetime(grp_agg['Date'])
          grp_agg = grp_agg.sort_values(
              [group_col, 'dt_temp'], ascending=True
          ).reset_index(drop=True)
          grp_agg['Chart_Date'] = grp_agg['dt_temp'].dt.strftime('%Y-%m-%d')
          grp_agg.drop(columns=['dt_temp'], inplace=True)

          grp_agg['주기별_총평가변화'] = (
              grp_agg.groupby(group_col)['총평가금액'].diff().fillna(0)
          )
          grp_agg['주기별_원금변화'] = (
              grp_agg.groupby(group_col)['원금'].diff().fillna(0)
          )
          grp_agg['주기별_순수손익'] = (
              grp_agg['주기별_총평가변화'] - grp_agg['주기별_원금변화']
          )
          grp_agg['선택구간 누적손익'] = grp_agg.groupby(group_col)[
              '주기별_순수손익'
          ].cumsum()

          grp_agg['prev_eval'] = grp_agg.groupby(group_col)['총평가금액'].shift(1)
          grp_agg['prev_principal'] = grp_agg.groupby(group_col)['원금'].shift(1)
          grp_agg['period_cash_flow'] = (
              grp_agg['원금'] - grp_agg['prev_principal']
          )
          grp_agg['period_start_base'] = (
              grp_agg['prev_eval'].fillna(grp_agg['총평가금액'])
              + grp_agg['period_cash_flow'].fillna(0)
          )

          grp_agg['주기별 수익률'] = np.where(
              grp_agg['period_start_base'] > 0,
              (grp_agg['주기별_순수손익'] / grp_agg['period_start_base']) * 100,
              0,
          )
          grp_agg['growth_factor'] = 1 + (
              grp_agg['주기별 수익률'].fillna(0) / 100
          )
          grp_agg['누적_성장지수'] = grp_agg.groupby(group_col)[
              'growth_factor'
          ].cumprod()
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
        groups = group_order + [
            g for g in grp_agg_def[group_col].unique() if g not in group_order
        ]

        st.markdown(f'### 📋 [{prefix}] 항목별 최종 핵심지표 요약 표')
        ex_summary = st.toggle(
            '🔀 환차손 제외 결과로 보기', key=f'ex_summary_{prefix}'
        )
        grp_agg_summary = get_grp_agg(ex_summary)

        summary_table_data = []
        total_eval_sum, total_pl_sum, total_principal_sum = 0, 0, 0

        for grp in groups:
          raw_latest = raw_df[
              (raw_df[group_col] == grp) & (raw_df['Date'] == latest_date)
          ]
          if raw_latest.empty or raw_latest['총평가금액'].sum() <= 0:
            continue

          sub = grp_agg_summary[grp_agg_summary[group_col] == grp]
          if not sub.empty:
            final_eval = sub['총평가금액'].iloc[-1]
            if final_eval <= 0 or pd.isna(final_eval):
              continue

            final_p_loss = sub['선택구간 누적손익'].iloc[-1]
            final_ret = sub['선택기간 누적 수익률'].iloc[-1]
            final_principal = (
                raw_latest['원금_ex_fx'].sum()
                if (ex_summary and '원금_ex_fx' in raw_df.columns)
                else raw_latest['원금'].sum()
            )

            total_eval_sum += final_eval
            total_pl_sum += final_p_loss
            total_principal_sum += final_principal

            summary_table_data.append({
                prefix: grp,
                '선택구간 누적 평가 손익 (원)': final_p_loss,
                '최종 기말 평가 금액 (원)': final_eval,
                '선택구간 누적 수익률 (%)': final_ret,
                '_eval': final_eval,
            })

        for row in summary_table_data:
          row['점유율 (%)'] = (
              (row['_eval'] / total_eval_sum * 100)
              if total_eval_sum > 0
              else 0
          )
          del row['_eval']

        total_ret = (
            (total_pl_sum / total_principal_sum * 100)
            if total_principal_sum > 0
            else 0
        )
        summary_table_data.append({
            prefix: '전체 합산',
            '선택구간 누적 평가 손익 (원)': total_pl_sum,
            '최종 기말 평가 금액 (원)': total_eval_sum,
            '선택구간 누적 수익률 (%)': total_ret,
            '점유율 (%)': 100.0 if total_eval_sum > 0 else 0.0,
        })

        summary_df = pd.DataFrame(summary_table_data)
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
        fig_sel_p.update_layout(
            title=dict(
                text=(
                    f'🔹 [{prefix}] 선택 구간 누적 평가손익 Trend'
                    f' {"(환차손제외)" if ex_g1 else ""}'
                ),
                y=0.95,
                x=0.01,
            ),
            hovermode='closest',
            height=500,
            margin=margin_g1,
            showlegend=show_g1,
            legend=leg_cfg_g1,
        )
        apply_y_axis_config(fig_sel_p, is_money=True)

        grp_agg_g2 = get_grp_agg(ex_g2)
        fig_period_p = go.Figure()
        for grp in groups:
          sub = grp_agg_g2[grp_agg_g2[group_col] == grp]
          fig_period_p.add_trace(
              go.Bar(
                  x=sub['Chart_Date'], y=sub['주기별_순수손익'], name=str(grp)
              )
          )

        leg_cfg_g2, show_g2, margin_g2 = build_legend_config(pos_g2)
        fig_period_p.update_layout(
            title=dict(
                text=(
                    f'🔹 [{prefix}] 선택 기간 주기별 평가손익 Trend'
                    f' {"(환차손제외)" if ex_g2 else ""}'
                ),
                y=0.95,
                x=0.01,
            ),
            barmode='relative',
            hovermode='closest',
            height=500,
            margin=margin_g2,
            showlegend=show_g2,
            legend=leg_cfg_g2,
        )
        apply_y_axis_config(fig_period_p, is_money=True)

        if num_cols == 1:
          render_resizable_plotly_chart(
              fig_sel_p, key=f'trend_grp_sel_p_{prefix}'
          )
          render_resizable_plotly_chart(
              fig_period_p, key=f'trend_grp_period_p_{prefix}'
          )
        else:
          col1, col2 = st.columns(2)
          with col1:
            render_resizable_plotly_chart(
                fig_sel_p, key=f'trend_grp_sel_p_{prefix}'
            )
          with col2:
            render_resizable_plotly_chart(
                fig_period_p, key=f'trend_grp_period_p_{prefix}'
            )

      # 전체 합산 차트
      def render_total_whose_charts(raw_df):
        st.markdown('### 📊 [전체 합산 - WHOSE별 분석]')
        # 총 자산 및 수익률 시각화
        tot_agg = raw_df.groupby('Date')[['원금', '총평가금액', '평가손익']].sum().reset_index()
        fig_tot = go.Figure()
        fig_tot.add_trace(
            go.Scatter(
                x=tot_agg['Date'],
                y=tot_agg['총평가금액'],
                mode='lines+markers',
                name='총 평가금액',
            )
        )
        fig_tot.add_trace(
            go.Scatter(
                x=tot_agg['Date'],
                y=tot_agg['원금'],
                mode='lines',
                name='원금',
                line=dict(dash='dash'),
            )
        )
        fig_tot.update_layout(
            title='전체 자산 평가액 및 원금 추이', height=450
        )
        render_resizable_plotly_chart(fig_tot, key='render_tot_chart')

      if active_views:
        tabs = st.tabs(active_views)
        for tab, v_type in zip(tabs, active_views):
          with tab:
            if v_type == '전체 합산':
              render_total_whose_charts(calc_df)
            elif v_type == '계좌별':
              draw_group_summary_charts(calc_df, 'account_num', '계좌별')
            elif v_type == '증권사(Broker)별':
              draw_group_summary_charts(calc_df, 'broker', '증권사(Broker)별')
            elif v_type == '계좌유형별':
              draw_group_summary_charts(calc_df, 'account_type', '계좌유형별')
            elif v_type == 'Category 4별':
              draw_group_summary_charts(calc_df, 'category4', 'Category 4별')
            elif v_type == '보유항목별':
              draw_group_summary_charts(calc_df, 'item_name', '보유항목별')

# -----------------------------------------------------------------------------
# 메뉴 2: 계좌 별칭 관리
# -----------------------------------------------------------------------------
elif menu == '계좌 별칭 관리':
  st.header('🏷️ 계좌 별칭 관리')
  conn = get_connection()
  pf_df = pd.read_sql(
      'SELECT DISTINCT broker, account_num, account_type FROM portfolio', conn
  )
  conn.close()

  if pf_df.empty:
    st.warning('등록된 포트폴리오 데이터가 없습니다.')
  else:
    conn = get_connection()
    current_aliases = pd.read_sql('SELECT * FROM account_alias', conn)
    conn.close()
    alias_dict = dict(
        zip(current_aliases['account_num'], current_aliases['alias'])
    )

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

      if st.form_submit_button('💾 별칭 저장'):
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
        st.success('계좌 별칭이 저장되었습니다!')
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
        col_map = {
            '기준일자': 'record_date',
            '증권사': 'broker',
            '계좌번호': 'account_num',
            '계좌명': 'account_name',
            '계좌유형': 'account_type',
            '종목명': 'item_name',
            '티커': 'ticker',
            'category1': 'category1',
            'category2': 'category2',
            'category3': 'category3',
            'category4': 'category4',
            '수량': 'quantity',
            '매수단가': 'purchase_price',
            '현재가': 'current_price',
            '평가금액': 'eval_price',
            '평가손익': 'eval_profit_loss',
            '수익률': 'return_rate',
            '포트폴리오비중': 'portfolio_weight',
            '통화': 'currency',
        }
        renamed_df = df_upload.rename(columns=col_map)
        conn = get_connection()
        renamed_df.to_sql('portfolio', conn, if_exists='append', index=False)
        conn.close()
        st.success('포트폴리오 데이터가 데이터베이스에 추가되었습니다!')
    except Exception as e:
      st.error(f'업로드 오류: {e}')

# -----------------------------------------------------------------------------
# 메뉴 4: 원금 및 통합 거래 관리 (엑셀 업로드 & 매수/매도 이력 보유 관리)
# -----------------------------------------------------------------------------
elif menu == '원금 및 통합 거래 관리':
  st.header('💰 원금 및 통합 거래(입출금/매매/배당) 관리')

  conn = get_connection()
  acc_df = pd.read_sql(
      'SELECT DISTINCT broker, account_num, account_name, account_type FROM'
      ' portfolio',
      conn,
  )
  init_df = pd.read_sql('SELECT * FROM initial_principal', conn)
  cf_df = pd.read_sql('SELECT * FROM cash_flow', conn)
  pf_items_df = pd.read_sql(
      'SELECT DISTINCT account_num, item_name, ticker, category1, category2,'
      ' category3, category4, currency FROM portfolio',
      conn,
  )
  conn.close()

  tab1, tab2, tab3, tab4 = st.tabs([
      '📥 거래 내역 엑셀 업로드',
      '📝 통합 거래 내역 Web 관리',
      '📊 계좌/종목별 보유 및 수익률 현황',
      '📌 계좌별 초기 원금 설정',
  ])

  # ---------------------------------------------------------------------------
  # TAB 1: 거래 내역 엑셀 업로드
  # ---------------------------------------------------------------------------
  with tab1:
    st.subheader('📥 입출금/매매/배당 거래 내역 엑셀 파일 업로드')
    st.caption(
        '💡 엑셀 파일(.xlsx, .csv)을 업로드하면 기존 데이터베이스에 안전하게'
        ' 적재되며 포트폴리오와 연동됩니다.'
    )

    uploaded_cf_file = st.file_uploader(
        '거래 내역 엑셀 파일을 선택하세요',
        type=['xlsx', 'xls', 'csv'],
        key='cf_excel_uploader',
    )

    if uploaded_cf_file is not None:
      try:
        if uploaded_cf_file.name.endswith('.csv'):
          df_cf_raw = pd.read_csv(uploaded_cf_file)
        else:
          df_cf_raw = pd.read_excel(uploaded_cf_file)

        st.markdown('##### 🔍 업로드 파일 미리보기 (상위 5행)')
        st.dataframe(df_cf_raw.head(), use_container_width=True)

        cf_col_map = {
            '거래일자': 'trans_date',
            '일자': 'trans_date',
            '날짜': 'trans_date',
            'trans_date': 'trans_date',
            '소유주': 'owner',
            'owner': 'owner',
            '증권사': 'broker',
            'broker': 'broker',
            '계좌번호': 'account_num',
            '계좌': 'account_num',
            'account_num': 'account_num',
            '계좌명': 'account_name',
            'account_name': 'account_name',
            '계좌유형': 'account_type',
            'account_type': 'account_type',
            '구분': 'flow_type',
            '거래구분': 'flow_type',
            '거래유형': 'flow_type',
            'flow_type': 'flow_type',
            '종목명': 'item_name',
            '종목': 'item_name',
            'item_name': 'item_name',
            '티커': 'ticker',
            'Ticker': 'ticker',
            'ticker': 'ticker',
            '수량': 'quantity',
            'quantity': 'quantity',
            '단가': 'price',
            '매수가': 'price',
            '매도가': 'price',
            'price': 'price',
            '거래금액': 'amount',
            '금액': 'amount',
            'amount': 'amount',
            '통화': 'currency',
            '통화단위': 'currency',
            'currency': 'currency',
            'category1': 'category1',
            'category2': 'category2',
            'category3': 'category3',
            'category4': 'category4',
            '적요': 'note',
            '메모': 'note',
            '비고': 'note',
            'note': 'note',
        }

        df_cf_mapped = df_cf_raw.rename(columns=cf_col_map)

        db_target_cols = [
            'trans_date',
            'owner',
            'broker',
            'account_num',
            'account_name',
            'account_type',
            'flow_type',
            'item_name',
            'ticker',
            'category1',
            'category2',
            'category3',
            'category4',
            'quantity',
            'price',
            'amount',
            'currency',
            'note',
        ]

        for col in db_target_cols:
          if col not in df_cf_mapped.columns:
            df_cf_mapped[col] = None

        df_upload_clean = df_cf_mapped[db_target_cols].copy()

        # 데이터 변환 및 매핑 보완
        df_upload_clean['trans_date'] = pd.to_datetime(
            df_upload_clean['trans_date']
        ).dt.strftime('%Y-%m-%d')
        df_upload_clean['account_num'] = (
            df_upload_clean['account_num'].astype(str).str.strip()
        )

        for idx, row in df_upload_clean.iterrows():
          acc_n = str(row['account_num'])
          item_n = str(row['item_name']) if pd.notna(row['item_name']) else ''

          matched_acc = acc_df[acc_df['account_num'].astype(str) == acc_n]
          if not matched_acc.empty:
            if pd.isna(row['broker']) or not row['broker']:
              df_upload_clean.loc[idx, 'broker'] = matched_acc['broker'].iloc[
                  0
              ]
            if pd.isna(row['account_name']) or not row['account_name']:
              df_upload_clean.loc[idx, 'account_name'] = matched_acc[
                  'account_name'
              ].iloc[0]
            if pd.isna(row['account_type']) or not row['account_type']:
              df_upload_clean.loc[idx, 'account_type'] = matched_acc[
                  'account_type'
              ].iloc[0]

          matched_item = pf_items_df[
              (pf_items_df['account_num'].astype(str) == acc_n)
              & (pf_items_df['item_name'] == item_n)
          ]
          if not matched_item.empty:
            if pd.isna(row['ticker']) or not row['ticker']:
              df_upload_clean.loc[idx, 'ticker'] = matched_item['ticker'].iloc[
                  0
              ]
            for cat_i in ['category1', 'category2', 'category3', 'category4']:
              if pd.isna(row[cat_i]) or not row[cat_i]:
                df_upload_clean.loc[idx, cat_i] = matched_item[cat_i].iloc[0]

          # 거래금액 계산 자동 보정
          qty = float(row['quantity']) if pd.notna(row['quantity']) else 0.0
          prc = float(row['price']) if pd.notna(row['price']) else 0.0
          amt = float(row['amount']) if pd.notna(row['amount']) else 0.0

          if amt == 0.0 and qty > 0 and prc > 0:
            df_upload_clean.loc[idx, 'amount'] = qty * prc

        save_option = st.radio(
            '저장 방식을 선택하세요:',
            ['기존 거래내역에 추가 (Append)', '기존 거래내역 전체 삭제 후 새로 저장 (Replace)'],
            horizontal=True,
        )

        if st.button('💾 거래내역 데이터베이스 저장'):
          conn = get_connection()
          if '전체 삭제' in save_option:
            conn.execute('DELETE FROM cash_flow')

          df_upload_clean.to_sql(
              'cash_flow', conn, if_exists='append', index=False
          )
          conn.commit()
          conn.close()

          st.success('🎉 성공적으로 거래내역이 데이터베이스에 저장되었습니다!')
          st.rerun()

      except Exception as e:
        st.error(f'엑셀 업로드 처리 중 오류 발생: {e}')

  # ---------------------------------------------------------------------------
  # TAB 2: 통합 거래 내역 Web 관리
  # ---------------------------------------------------------------------------
  with tab2:
    st.subheader('📝 통합 거래 내역 Web 관리')
    st.caption('💡 아래 테이블에서 직접 수정/삭제/추가할 수 있습니다.')

    all_accounts = acc_df['account_num'].astype(str).unique().tolist()
    all_brokers = acc_df['broker'].dropna().unique().tolist()
    all_owners = ['BJ', 'SH']
    all_flow_types = ['입금', '출금', '매수', '매도', '배당금']
    all_currencies = ['KRW', 'USD']
    all_items = pf_items_df['item_name'].dropna().unique().tolist()

    target_cols = [
        'trans_date',
        'owner',
        'account_num',
        'broker',
        'account_name',
        'account_type',
        'flow_type',
        'item_name',
        'ticker',
        'quantity',
        'price',
        'amount',
        'currency',
        'category1',
        'category2',
        'category3',
        'category4',
        'note',
    ]

    edit_cf_df = (
        cf_df[target_cols].copy()
        if not cf_df.empty
        else pd.DataFrame(columns=target_cols)
    )

    column_config = {
        'trans_date': st.column_config.DateColumn(
            '거래일자', format='YYYY-MM-DD', required=True
        ),
        'owner': st.column_config.SelectboxColumn(
            '소유주', options=all_owners, required=True
        ),
        'account_num': st.column_config.SelectboxColumn(
            '계좌번호', options=all_accounts, required=True
        ),
        'broker': st.column_config.SelectboxColumn('증권사', options=all_brokers),
        'flow_type': st.column_config.SelectboxColumn(
            '거래 구분', options=all_flow_types, required=True
        ),
        'item_name': st.column_config.SelectboxColumn(
            '종목명', options=all_items
        ),
        'quantity': st.column_config.NumberColumn('수량', format='%.4f'),
        'price': st.column_config.NumberColumn('단가', format='%.2f'),
        'amount': st.column_config.NumberColumn('거래총액', format='%,.0f'),
        'currency': st.column_config.SelectboxColumn(
            '통화', options=all_currencies, default='KRW'
        ),
    }

    edited_cf = st.data_editor(
        edit_cf_df,
        num_rows='dynamic',
        use_container_width=True,
        column_config=column_config,
        key='cash_flow_editor',
    )

    if st.button('💾 Web 수정 내역 저장'):
      conn = get_connection()
      conn.execute('DELETE FROM cash_flow')
      edited_cf.to_sql('cash_flow', conn, if_exists='append', index=False)
      conn.commit()
      conn.close()
      st.success('수정 사항이 성공적으로 저장되었습니다!')
      st.rerun()

  # ---------------------------------------------------------------------------
  # TAB 3: 계좌/종목별 보유 및 수익률 현황
  # ---------------------------------------------------------------------------
  with tab3:
    st.subheader('📊 거래 이력(매수/매도) 반영 실시간 잔고 및 수익률')

    holdings_df = compute_holdings_from_history()

    if holdings_df.empty:
      st.info('보유 종목 내역이 없습니다.')
    else:
      acc_filter = st.multiselect(
          '조회할 계좌 선택',
          options=holdings_df['account_num'].unique().tolist(),
          default=holdings_df['account_num'].unique().tolist(),
          key='holding_acc_filter',
      )

      filtered_h = holdings_df[holdings_df['account_num'].isin(acc_filter)]

      # 주요 핵심 지표 카드
      tot_inv = filtered_h['invested_amount'].sum()
      tot_eval = filtered_h['eval_amount'].sum()
      tot_eval_pnl = filtered_h['eval_pnl'].sum()
      tot_realized = filtered_h['realized_pnl'].sum()
      tot_div = filtered_h['dividends'].sum()
      tot_ret = ((tot_eval - tot_inv) / tot_inv * 100) if tot_inv > 0 else 0.0

      m1, m2, m3, m4, m5 = st.columns(5)
      m1.metric('총 매수 원금', f'₩{tot_inv:,.0f}')
      m2.metric('총 평가 금액', f'₩{tot_eval:,.0f}')
      m3.metric('총 평가 손익', f'₩{tot_eval_pnl:,.0f}', f'{tot_ret:.2f}%')
      m4.metric('매도 실현 손익', f'₩{tot_realized:,.0f}')
      m5.metric('누적 배당 수령액', f'₩{tot_div:,.0f}')

      st.write('---')

      disp_h = filtered_h[[
          'broker',
          'account_num',
          'item_name',
          'ticker',
          'quantity',
          'purchase_price',
          'current_price',
          'eval_amount',
          'eval_pnl',
          'return_rate',
          'realized_pnl',
          'dividends',
          'currency',
      ]].copy()

      disp_h.columns = [
          '증권사',
          '계좌번호',
          '종목명',
          '티커',
          '잔여수량',
          '평균매수단가',
          '현재가',
          '평가금액(원)',
          '평가손익(원)',
          '수익률(%)',
          '매도실현손익(원)',
          '누적배당금(원)',
          '통화',
      ]

      st.dataframe(
          disp_h.style.format({
              '잔여수량': '{:,.2f}',
              '평균매수단가': '{:,.2f}',
              '현재가': '{:,.2f}',
              '평가금액(원)': '{:,.0f}',
              '평가손익(원)': '{:,.0f}',
              '수익률(%)': '{:.2f}%',
              '매도실현손익(원)': '{:,.0f}',
              '누적배당금(원)': '{:,.0f}',
          }),
          use_container_width=True,
      )

  # ---------------------------------------------------------------------------
  # TAB 4: 계좌별 초기 원금 설정
  # ---------------------------------------------------------------------------
  with tab4:
    st.subheader('📌 계좌별 초기 원금 설정')
    init_dict = dict(zip(init_df['account_num'], init_df['initial_amount']))
    with st.form('init_principal_form'):
      new_init_data = []
      for _, row in acc_df.iterrows():
        b = row['broker']
        acc = str(row['account_num'])
        cur_val = init_dict.get(acc, 0.0)
        val = st.number_input(
            f'[{b}] {acc}',
            value=float(cur_val),
            step=100000.0,
            key=f'init_{acc}',
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

# -----------------------------------------------------------------------------
# 메뉴 5: 등록 데이터 조회 및 웹 수정
# -----------------------------------------------------------------------------
elif menu == '등록 데이터 조회 및 웹 수정':
  st.header('🔍 등록 데이터 조회 및 웹 수정')
  table_choice = st.selectbox(
      '조회 및 수정할 데이터베이스 테이블 선택',
      ['portfolio', 'cash_flow', 'initial_principal', 'account_alias'],
  )

  conn = get_connection()
  table_df = pd.read_sql(f'SELECT * FROM {table_choice}', conn)
  conn.close()

  st.write(f'총 {len(table_df)}개의 레코드가 검색되었습니다.')
  edited_table = st.data_editor(
      table_df, num_rows='dynamic', use_container_width=True
  )

  if st.button('💾 테이블 변경사항 반영하기'):
    conn = get_connection()
    conn.execute(f'DELETE FROM {table_choice}')
    edited_table.to_sql(table_choice, conn, if_exists='append', index=False)
    conn.commit()
    conn.close()
    st.success(f'{table_choice} 테이블이 성공적으로 업데이트되었습니다!')
    st.rerun()

# -----------------------------------------------------------------------------
# 메뉴 6: 데이터 백업 및 복구
# -----------------------------------------------------------------------------
elif menu == '데이터 백업 및 복구':
  st.header('💾 데이터 백업 및 복구')

  b_col1, b_col2 = st.columns(2)

  with b_col1:
    st.subheader('📥 DB 파일 다운로드 백업')
    if os.path.exists(DB_FILE):
      with open(DB_FILE, 'rb') as f:
        st.download_button(
            label='💾 asset_tracker.db 파일 다운로드',
            data=f,
            file_name='asset_tracker_backup.db',
            mime='application/x-sqlite3',
        )

  with b_col2:
    st.subheader('📤 DB 파일 업로드 복구')
    uploaded_db = st.file_uploader(
        '복구할 .db 파일을 업로드하세요', type=['db', 'sqlite3']
    )
    if uploaded_db is not None:
      if st.button('⚠️ DB 파일 덮어쓰기 복구 실행'):
        with open(DB_FILE, 'wb') as f:
          f.write(uploaded_db.getbuffer())
        st.success('데이터베이스가 업로드한 파일로 복구되었습니다!')
        st.rerun()