import io
import os
import shutil
import sqlite3
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import yfinance as yf
from plotly.subplots import make_subplots

# -----------------------------------------------------------------------------
# 1. DB 초기화 및 데이터 백업/관리 함수
# -----------------------------------------------------------------------------
DATA_DIR = os.path.join(os.path.dirname(__file__), '.data')
BACKUP_DIR = os.path.join(DATA_DIR, 'backups')
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(BACKUP_DIR, exist_ok=True)
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
            whose TEXT,
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

  # 기존 DB에 whose 컬럼이 없는 경우 자동 추가
  c.execute('PRAGMA table_info(portfolio)')
  columns = [column[1] for column in c.fetchall()]
  if 'whose' not in columns:
    c.execute("ALTER TABLE portfolio ADD COLUMN whose TEXT DEFAULT '미지정'")

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


# --- 백업 및 복원 헬퍼 함수 ---
def create_local_backup():
  """현재 DB 파일의 스냅샷 백업본을 생성합니다."""
  if os.path.exists(DB_FILE):
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_filepath = os.path.join(
        BACKUP_DIR, f'asset_tracker_backup_{timestamp}.db'
    )
    shutil.copy2(DB_FILE, backup_filepath)
    return backup_filepath
  return None


def get_backup_files():
  """로컬 백업 파일 목록을 최근 순으로 조회합니다."""
  if not os.path.exists(BACKUP_DIR):
    return []
  files = [
      os.path.join(BACKUP_DIR, f)
      for f in os.listdir(BACKUP_DIR)
      if f.endswith('.db')
  ]
  files.sort(key=os.path.getmtime, reverse=True)
  return files


def export_all_to_excel_bytes():
  """모든 DB 테이블을 Excel 바이너리로 내보냅니다."""
  output = io.BytesIO()
  conn = get_connection()
  with pd.ExcelWriter(output, engine='openpyxl') as writer:
    for table_name in [
        'portfolio',
        'initial_principal',
        'cash_flow',
        'account_alias',
    ]:
      try:
        df_tbl = pd.read_sql(f'SELECT * FROM {table_name}', conn)
        df_tbl.to_excel(writer, sheet_name=table_name, index=False)
      except Exception:
        pass
  conn.close()
  return output.getvalue()


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
        '등록 데이터 조회 및 관리',
        '데이터 백업 및 복원',
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
    # 소유자(whose) 목록 추출
    whose_list = (
        sorted(pf_df['whose'].dropna().unique().tolist())
        if 'whose' in pf_df.columns
        else []
    )
    if not whose_list:
      whose_list = ['미지정']

    # 계좌 기본 정보 추출 (중복 제거)
    acc_info_df = pf_df[
        ['broker', 'account_num', 'account_type', 'whose']
    ].drop_duplicates(subset=['account_num'])

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
      f_col1, f_col2 = st.columns([3, 3])

      with f_col1:
        # 1. whose(소유자) 필터 및 계좌 선택
        selected_whose = st.multiselect(
            '👤 소유자 (Whose) 필터 선택',
            options=whose_list,
            default=st.session_state.get('trend_sel_whose', whose_list),
        )

        filtered_acc_info = acc_info_df[
            acc_info_df['whose'].astype(str).isin(selected_whose)
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
          acc_type_str = row.get('account_type', '미지정')
          broker_str = row.get('broker', '증권사미지정')
          whose_str = row.get('whose', '미지정')
          label = f'[{whose_str}] {broker_str} | {display_alias} [{acc_type_str}] ({acc_num})'
          acc_options.append(label)

        # 소유자 변경 시 세션 상태에 저장된 계좌 목록 동적 검증 및 필터링
        saved_accs = st.session_state.get('trend_sel_accs', acc_options)
        valid_default_accs = [a for a in saved_accs if a in acc_options]
        if not valid_default_accs:
          valid_default_accs = acc_options

        selected_acc_labels = st.multiselect(
            '🏦 조회할 계좌 선택', options=acc_options, default=valid_default_accs
        )

        view_types = st.multiselect(
            '📊 표시할 트렌드 관점 선택',
            options=all_view_types,
            default=st.session_state.get('trend_view_types', all_view_types),
        )

      with f_col2:
        selected_bm = st.multiselect(
            '📈 비교 벤치마크 지수 선택 (차트에 함께 표시)',
            options=bm_options,
            default=st.session_state.get('trend_sel_bm', bm_options),
        )

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
        if '(' in lbl and ')' in lbl:
          acc_num_part = lbl.split('(')[-1].replace(')', '').strip()
          selected_accounts.append(acc_num_part)
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

          # 계좌별 누적 필터링 수정: 조회일(t_str) 이하 최신 데이터 사용, 없을 경우 가장 가까운 과거/미래 레코드 매칭
          acc_pf = filtered_pf_df[
              (filtered_pf_df['account_num'].astype(str) == acc)
              & (filtered_pf_df['record_date'] <= t_str)
          ]
          if acc_pf.empty:
            acc_pf = filtered_pf_df[
                filtered_pf_df['account_num'].astype(str) == acc
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
              if '펀드' in item_name or '펀드' in str(cat4) or fmt_tk is None:
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

      # -------------------------------------------------------------------------
      # [전체 합산] 전용 차트 렌더링 함수
      # -------------------------------------------------------------------------
      def draw_overall_total_charts(raw_df):
        st.markdown('##### ⚙️ [전체 합산] 차트별 범주 및 환율 옵션 설정')
        cb_col1, cb_col2, cb_col3, cb_col4 = st.columns(4)
        leg_pos_options = ['하단 배치', '우측 배치', '숨김']
        default_idx = 0 if global_legend_pos == '하단 배치' else 1

        with cb_col1:
          pos_fig1 = st.selectbox(
              '차트1 범주',
              leg_pos_options,
              index=default_idx,
              key='pos_f1_overall',
          )
          ex_fx1 = st.toggle('🔀 환차손제외 (차트1)', key='ex1_overall')
        with cb_col2:
          pos_fig2 = st.selectbox(
              '차트2 범주',
              leg_pos_options,
              index=default_idx,
              key='pos_f2_overall',
          )
          ex_fx2 = st.toggle('🔀 환차손제외 (차트2)', key='ex2_overall')
        with cb_col3:
          pos_fig3a = st.selectbox(
              '차트3-1 범주',
              leg_pos_options,
              index=default_idx,
              key='pos_f3a_overall',
          )
          ex_fx3a = st.toggle('🔀 환차손제외 (차트3-1)', key='ex3a_overall')
        with cb_col4:
          pos_fig3b = st.selectbox(
              '차트3-2 범주',
              leg_pos_options,
              index=default_idx,
              key='pos_f3b_overall',
          )
          ex_fx3b = st.toggle('🔀 환차손제외 (차트3-2)', key='ex3b_overall')

        def get_agg_df(use_ex_fx):
          sub_df = (
              raw_df.groupby('Date')[
                  [
                      '원금',
                      '평가손익',
                      '총평가금액',
                      '원금_ex_fx',
                      '평가손익_ex_fx',
                      '총평가금액_ex_fx',
                  ]
              ]
              .sum()
              .reset_index()
          )
          if use_ex_fx and '원금_ex_fx' in sub_df.columns:
            sub_df['원금'] = sub_df['원금_ex_fx']
            sub_df['평가손익'] = sub_df['평가손익_ex_fx']
            sub_df['총평가금액'] = sub_df['총평가금액_ex_fx']

          sub_df['dt_temp'] = pd.to_datetime(sub_df['Date'])
          sub_df = sub_df.sort_values('dt_temp', ascending=True).reset_index(
              drop=True
          )
          sub_df['Chart_Date'] = sub_df['dt_temp'].dt.strftime('%Y-%m-%d')
          sub_df.drop(columns=['dt_temp'], inplace=True)

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
          return sub_df

        sub_df_default = get_agg_df(False)

        selected_cum_p_loss = sub_df_default['선택구간 누적손익'].iloc[-1]
        total_cum_p_loss = sub_df_default['평가손익'].iloc[-1]

        c_m1, c_m2, c_m3 = st.columns(3)
        c_m1.metric('📌 선택 구간 누적 평가손익', f'{selected_cum_p_loss:,.0f} 원')
        c_m2.metric('🏛️ 전체 통산 누적 평가손익', f'{total_cum_p_loss:,.0f} 원')
        c_m3.metric(
            '💰 최종 기말 평가금액', f"{sub_df_default['총평가금액'].iloc[-1]:,.0f} 원"
        )

        date_order_list = sub_df_default['Chart_Date'].tolist()

        # --- Fig 1: 전체 자산 TREND (우측 수익률 축 제거 반영) ---
        eval_col = '총평가금액_ex_fx' if ex_fx1 else '총평가금액'

        acc_eval_df = (
            raw_df.groupby(['Date', 'account_num'])[eval_col]
            .sum()
            .reset_index()
        )
        acc_eval_df['dt_temp'] = pd.to_datetime(acc_eval_df['Date'])
        acc_eval_df = acc_eval_df.sort_values(
            ['account_num', 'dt_temp'], ascending=True
        )
        acc_eval_df['Chart_Date'] = acc_eval_df['dt_temp'].dt.strftime(
            '%Y-%m-%d'
        )

        latest_date = raw_df['Date'].max()
        latest_acc_df = raw_df[raw_df['Date'] == latest_date]
        acc_order = (
            latest_acc_df.groupby('account_num')[eval_col]
            .sum()
            .sort_values(ascending=False)
            .index.tolist()
        )
        all_accs = acc_eval_df['account_num'].unique().tolist()
        ordered_accs = acc_order + [a for a in all_accs if a not in acc_order]

        fig1 = go.Figure()

        for acc in ordered_accs:
          sub_acc = acc_eval_df[acc_eval_df['account_num'] == acc]
          fig1.add_trace(
              go.Bar(
                  x=sub_acc['Chart_Date'],
                  y=sub_acc[eval_col],
                  name=str(acc),
                  hovertemplate='%{y:,.0f} 원',
              )
          )

        sub1_tot = get_agg_df(ex_fx1)
        fig1.add_trace(
            go.Scatter(
                x=sub1_tot['Chart_Date'],
                y=sub1_tot['총평가금액'],
                name='총 평가 금액',
                mode='lines+markers+text',
                line=dict(color='#ff9900', width=3),
                marker=dict(size=7),
                text=[f'{v:,.0f}' for v in sub1_tot['총평가금액']],
                textposition='top center',
                hovertemplate='%{y:,.0f} 원',
            )
        )

        leg_cfg1, show_leg1, margin1 = build_legend_config(pos_fig1)
        title_suffix1 = ' (환차손제외)' if ex_fx1 else ''
        fig1.update_layout(
            title=dict(
                text=f'1. [전체 합산] 전체 자산 TREND{title_suffix1}',
                y=0.95,
                x=0.01,
                xanchor='left',
                yanchor='top',
                yref='container',
            ),
            barmode='stack',
            hovermode='closest',
            height=550,
            margin=margin1,
            showlegend=show_leg1,
            legend=leg_cfg1,
        )
        fig1.update_xaxes(
            type='category',
            categoryorder='array',
            categoryarray=date_order_list,
        )
        apply_y_axis_config(fig1, axis_name='yaxis', is_money=True)
        fig1.update_yaxes(title_text='평가금액 (원)', tickformat=',.0f')

        # Fig 2: 구간 손익 금액 추이
        sub2 = get_agg_df(ex_fx2)
        fig2 = go.Figure()
        valid_period_df = sub2.dropna(subset=['주기별 평가손익'])
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
            )
        )
        fig2.add_trace(
            go.Scatter(
                x=sub2['Chart_Date'],
                y=sub2['선택구간 누적손익'],
                name='선택구간 누적손익 (추이)',
                mode='lines+markers',
                line=dict(color='#9467bd', width=2.5),
                marker=dict(size=5),
            )
        )

        leg_cfg2, show_leg2, margin2 = build_legend_config(pos_fig2)
        title_suffix2 = ' (환차손제외)' if ex_fx2 else ''
        fig2.update_layout(
            title=dict(
                text=f'2. [전체 합산] 구간 손익 금액 추이{title_suffix2}',
                y=0.95,
                x=0.01,
                xanchor='left',
                yanchor='top',
                yref='container',
            ),
            hovermode='closest',
            height=500,
            margin=margin2,
            showlegend=show_leg2,
            legend=leg_cfg2,
        )
        fig2.update_xaxes(
            type='category',
            categoryorder='array',
            categoryarray=date_order_list,
        )
        apply_y_axis_config(fig2, axis_name='yaxis', is_money=True)
        fig2.update_yaxes(title_text='손익금액 (원)', tickformat=',.0f')

        # Fig 3a: 구간 누적수익률 추이
        sub3a = get_agg_df(ex_fx3a)
        fig3a = go.Figure()
        fig3a.add_trace(
            go.Scatter(
                x=sub3a['Chart_Date'],
                y=sub3a['구간별 누적수익률'],
                name='구간별 누적수익률 (%)',
                mode='lines+markers',
                line=dict(color='#1f77b4', width=2.5),
                marker=dict(size=5),
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
                  name=f'📌 {bm_name} 누적수익률 (%)',
                  line=dict(
                      color=bm_styles.get(bm_name, {}).get('color', '#7f7f7f'),
                      dash='dot',
                  ),
                  hovertemplate='%{y:.2f}%',
              )
          )

        leg_cfg3a, show_leg3a, margin3a = build_legend_config(pos_fig3a)
        title_suffix3a = ' (환차손제외)' if ex_fx3a else ''
        fig3a.update_layout(
            title=dict(
                text=(
                    '3-1. [전체 합산] 구간 누적수익률 추이 (벤치마크'
                    f' 비교){title_suffix3a}'
                ),
                y=0.95,
                x=0.01,
                xanchor='left',
                yanchor='top',
                yref='container',
            ),
            hovermode='closest',
            height=500,
            margin=margin3a,
            showlegend=show_leg3a,
            legend=leg_cfg3a,
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

        # Fig 3b: 주기별 수익률 추이
        sub3b = get_agg_df(ex_fx3b)
        fig3b = go.Figure()
        fig3b.add_trace(
            go.Scatter(
                x=sub3b['Chart_Date'],
                y=sub3b['구간별 수익률'],
                name='구간별 수익률 (%)',
                mode='lines+markers',
                line=dict(color='#17becf', width=2, dash='dot'),
                marker=dict(size=5),
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

          fig3b.add_trace(
              go.Scatter(
                  x=bm_df['Chart_Date'],
                  y=bm_df['주기별 수익률'],
                  mode='lines',
                  name=f'📌 {bm_name} 주기별수익률 (%)',
                  line=bm_styles.get(bm_name, dict(dash='dash')),
                  hovertemplate='%{y:.2f}%',
              )
          )

        leg_cfg3b, show_leg3b, margin3b = build_legend_config(pos_fig3b)
        title_suffix3b = ' (환차손제외)' if ex_fx3b else ''
        fig3b.update_layout(
            title=dict(
                text=(
                    '3-2. [전체 합산] 주기별 수익률 추이 (벤치마크'
                    f' 비교){title_suffix3b}'
                ),
                y=0.95,
                x=0.01,
                xanchor='left',
                yanchor='top',
                yref='container',
            ),
            hovermode='closest',
            height=500,
            margin=margin3b,
            showlegend=show_leg3b,
            legend=leg_cfg3b,
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
          render_resizable_plotly_chart(fig1, key='trend_fig1_overall')
          render_resizable_plotly_chart(fig2, key='trend_fig2_overall')
          render_resizable_plotly_chart(fig3a, key='trend_fig3a_overall')
          render_resizable_plotly_chart(fig3b, key='trend_fig3b_overall')
        elif num_cols == 2:
          col_a, col_b = st.columns(2)
          with col_a:
            render_resizable_plotly_chart(fig1, key='trend_fig1_overall')
          with col_b:
            render_resizable_plotly_chart(fig2, key='trend_fig2_overall')
          col_c, col_d = st.columns(2)
          with col_c:
            render_resizable_plotly_chart(fig3a, key='trend_fig3a_overall')
          with col_d:
            render_resizable_plotly_chart(fig3b, key='trend_fig3b_overall')
        else:
          col_a, col_b, col_c = st.columns(3)
          with col_a:
            render_resizable_plotly_chart(fig1, key='trend_fig1_overall')
          with col_b:
            render_resizable_plotly_chart(fig2, key='trend_fig2_overall')
          with col_c:
            render_resizable_plotly_chart(fig3a, key='trend_fig3a_overall')
          render_resizable_plotly_chart(fig3b, key='trend_fig3b_overall')

      def draw_single_chart(raw_df, title_name, force_single_col=False):
        st.markdown(f'##### ⚙️ [{title_name}] 개별 차트 범주 및 환율 옵션 설정')
        cb_col1, cb_col2, cb_col3, cb_col4 = st.columns(4)
        leg_pos_options = ['하단 배치', '우측 배치', '숨김']
        default_idx = 0 if global_legend_pos == '하단 배치' else 1

        with cb_col1:
          pos_fig1 = st.selectbox(
              '차트1 범주',
              leg_pos_options,
              index=default_idx,
              key=f'pos_f1_{title_name}_{force_single_col}',
          )
          ex_fx1 = st.toggle(
              '🔀 환차손제외 (차트1)',
              key=f'ex1_{title_name}_{force_single_col}',
          )
        with cb_col2:
          pos_fig2 = st.selectbox(
              '차트2 범주',
              leg_pos_options,
              index=default_idx,
              key=f'pos_f2_{title_name}_{force_single_col}',
          )
          ex_fx2 = st.toggle(
              '🔀 환차손제외 (차트2)',
              key=f'ex2_{title_name}_{force_single_col}',
          )
        with cb_col3:
          pos_fig3a = st.selectbox(
              '차트3-1 범주',
              leg_pos_options,
              index=default_idx,
              key=f'pos_f3a_{title_name}_{force_single_col}',
          )
          ex_fx3a = st.toggle(
              '🔀 환차손제외 (차트3-1)',
              key=f'ex3a_{title_name}_{force_single_col}',
          )
        with cb_col4:
          pos_fig3b = st.selectbox(
              '차트3-2 범주',
              leg_pos_options,
              index=default_idx,
              key=f'pos_f3b_{title_name}_{force_single_col}',
          )
          ex_fx3b = st.toggle(
              '🔀 환차손제외 (차트3-2)',
              key=f'ex3b_{title_name}_{force_single_col}',
          )

        def get_sub_df(use_ex_fx):
          sub_df = raw_df.copy()
          if use_ex_fx and '원금_ex_fx' in sub_df.columns:
            sub_df['원금'] = sub_df['원금_ex_fx']
            sub_df['평가손익'] = sub_df['평가손익_ex_fx']
            sub_df['총평가금액'] = sub_df['총평가금액_ex_fx']

          sub_df['dt_temp'] = pd.to_datetime(sub_df['Date'])
          sub_df = sub_df.sort_values('dt_temp', ascending=True).reset_index(
              drop=True
          )
          sub_df['Chart_Date'] = sub_df['dt_temp'].dt.strftime('%Y-%m-%d')
          sub_df.drop(columns=['dt_temp'], inplace=True)

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
          return sub_df

        sub_df_default = get_sub_df(False)

        selected_cum_p_loss = sub_df_default['선택구간 누적손익'].iloc[-1]
        total_cum_p_loss = sub_df_default['평가손익'].iloc[-1]

        c_m1, c_m2, c_m3 = st.columns(3)
        c_m1.metric('📌 선택 구간 누적 평가손익', f'{selected_cum_p_loss:,.0f} 원')
        c_m2.metric('🏛️ 전체 통산 누적 평가손익', f'{total_cum_p_loss:,.0f} 원')
        c_m3.metric(
            '💰 최종 기말 평가금액', f"{sub_df_default['총평가금액'].iloc[-1]:,.0f} 원"
        )

        date_order_list = sub_df_default['Chart_Date'].tolist()

        # Fig 1
        sub1 = get_sub_df(ex_fx1)
        fig1 = make_subplots(specs=[[{'secondary_y': True}]])
        fig1.add_trace(
            go.Bar(
                x=sub1['Chart_Date'],
                y=sub1['원금'],
                name='원금',
                marker_color='#2b5c8f',
                opacity=0.6,
            ),
            secondary_y=False,
        )
        fig1.add_trace(
            go.Bar(
                x=sub1['Chart_Date'],
                y=sub1['평가손익'],
                name='전체 누적 평가손익',
                marker_color='#e05d5d',
                opacity=0.5,
            ),
            secondary_y=False,
        )
        fig1.add_trace(
            go.Scatter(
                x=sub1['Chart_Date'],
                y=sub1['총평가금액'],
                name='총평가금액',
                mode='lines+markers+text',
                line=dict(color='#ff9900', width=3),
                marker=dict(size=6),
                text=[f'{v:,.0f}' for v in sub1['총평가금액']],
                textposition='top center',
            ),
            secondary_y=False,
        )
        fig1.add_trace(
            go.Scatter(
                x=sub1['Chart_Date'],
                y=sub1['수익률'],
                name='수익률(%)',
                mode='lines+markers',
                line=dict(color='#2ca02c', dash='dash', width=2),
                marker=dict(size=6),
                hovertemplate='%{y:.2f}%',
            ),
            secondary_y=True,
        )

        leg_cfg1, show_leg1, margin1 = build_legend_config(pos_fig1)
        title_suffix1 = ' (환차손제외)' if ex_fx1 else ''
        fig1.update_layout(
            title=dict(
                text=(
                    f'1. [{title_name}] 자산 및 전체 손익/수익률 추이{title_suffix1}'
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
            margin=margin1,
            showlegend=show_leg1,
            legend=leg_cfg1,
        )
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

        # Fig 2
        sub2 = get_sub_df(ex_fx2)
        fig2 = go.Figure()
        valid_period_df = sub2.dropna(subset=['주기별 평가손익'])
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
            )
        )
        fig2.add_trace(
            go.Scatter(
                x=sub2['Chart_Date'],
                y=sub2['선택구간 누적손익'],
                name='선택구간 누적손익 (추이)',
                mode='lines+markers',
                line=dict(color='#9467bd', width=2.5),
                marker=dict(size=5),
            )
        )

        leg_cfg2, show_leg2, margin2 = build_legend_config(pos_fig2)
        title_suffix2 = ' (환차손제외)' if ex_fx2 else ''
        fig2.update_layout(
            title=dict(
                text=f'2. [{title_name}] 구간 손익 금액 추이{title_suffix2}',
                y=0.95,
                x=0.01,
                xanchor='left',
                yanchor='top',
                yref='container',
            ),
            hovermode='closest',
            height=500,
            margin=margin2,
            showlegend=show_leg2,
            legend=leg_cfg2,
        )
        fig2.update_xaxes(
            type='category',
            categoryorder='array',
            categoryarray=date_order_list,
        )
        apply_y_axis_config(fig2, axis_name='yaxis', is_money=True)
        fig2.update_yaxes(title_text='손익금액 (원)', tickformat=',.0f')

        # Fig 3a
        sub3a = get_sub_df(ex_fx3a)
        fig3a = go.Figure()
        fig3a.add_trace(
            go.Scatter(
                x=sub3a['Chart_Date'],
                y=sub3a['구간별 누적수익률'],
                name='구간별 누적수익률 (%)',
                mode='lines+markers',
                line=dict(color='#1f77b4', width=2.5),
                marker=dict(size=5),
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
                  name=f'📌 {bm_name} 누적수익률 (%)',
                  line=dict(
                      color=bm_styles.get(bm_name, {}).get('color', '#7f7f7f'),
                      dash='dot',
                  ),
                  hovertemplate='%{y:.2f}%',
              )
          )

        leg_cfg3a, show_leg3a, margin3a = build_legend_config(pos_fig3a)
        title_suffix3a = ' (환차손제외)' if ex_fx3a else ''
        fig3a.update_layout(
            title=dict(
                text=(
                    f'3-1. [{title_name}] 구간 누적수익률 추이 (벤치마크'
                    f' 비교){title_suffix3a}'
                ),
                y=0.95,
                x=0.01,
                xanchor='left',
                yanchor='top',
                yref='container',
            ),
            hovermode='closest',
            height=500,
            margin=margin3a,
            showlegend=show_leg3a,
            legend=leg_cfg3a,
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

        # Fig 3b
        sub3b = get_sub_df(ex_fx3b)
        fig3b = go.Figure()
        fig3b.add_trace(
            go.Scatter(
                x=sub3b['Chart_Date'],
                y=sub3b['구간별 수익률'],
                name='구간별 수익률 (%)',
                mode='lines+markers',
                line=dict(color='#17becf', width=2, dash='dot'),
                marker=dict(size=5),
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

          fig3b.add_trace(
              go.Scatter(
                  x=bm_df['Chart_Date'],
                  y=bm_df['주기별 수익률'],
                  mode='lines',
                  name=f'📌 {bm_name} 주기별수익률 (%)',
                  line=bm_styles.get(bm_name, dict(dash='dash')),
                  hovertemplate='%{y:.2f}%',
              )
          )

        leg_cfg3b, show_leg3b, margin3b = build_legend_config(pos_fig3b)
        title_suffix3b = ' (환차손제외)' if ex_fx3b else ''
        fig3b.update_layout(
            title=dict(
                text=(
                    f'3-2. [{title_name}] 주기별 수익률 추이 (벤치마크'
                    f' 비교){title_suffix3b}'
                ),
                y=0.95,
                x=0.01,
                xanchor='left',
                yanchor='top',
                yref='container',
            ),
            hovermode='closest',
            height=500,
            margin=margin3b,
            showlegend=show_leg3b,
            legend=leg_cfg3b,
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

        effective_cols = 1 if force_single_col else num_cols

        if effective_cols == 1:
          render_resizable_plotly_chart(
              fig1, key=f'trend_fig1_{title_name}_{force_single_col}'
          )
          render_resizable_plotly_chart(
              fig2, key=f'trend_fig2_{title_name}_{force_single_col}'
          )
          render_resizable_plotly_chart(
              fig3a, key=f'trend_fig3a_{title_name}_{force_single_col}'
          )
          render_resizable_plotly_chart(
              fig3b, key=f'trend_fig3b_{title_name}_{force_single_col}'
          )
        elif effective_cols == 2:
          col_a, col_b = st.columns(2)
          with col_a:
            render_resizable_plotly_chart(
                fig1, key=f'trend_fig1_{title_name}_{force_single_col}'
            )
          with col_b:
            render_resizable_plotly_chart(
                fig2, key=f'trend_fig2_{title_name}_{force_single_col}'
            )
          col_c, col_d = st.columns(2)
          with col_c:
            render_resizable_plotly_chart(
                fig3a, key=f'trend_fig3a_{title_name}_{force_single_col}'
            )
          with col_d:
            render_resizable_plotly_chart(
                fig3b, key=f'trend_fig3b_{title_name}_{force_single_col}'
            )
        else:
          col_a, col_b, col_c = st.columns(3)
          with col_a:
            render_resizable_plotly_chart(
                fig1, key=f'trend_fig1_{title_name}_{force_single_col}'
            )
          with col_b:
            render_resizable_plotly_chart(
                fig2, key=f'trend_fig2_{title_name}_{force_single_col}'
            )
          with col_c:
            render_resizable_plotly_chart(
                fig3a, key=f'trend_fig3a_{title_name}_{force_single_col}'
            )
          render_resizable_plotly_chart(
              fig3b, key=f'trend_fig3b_{title_name}_{force_single_col}'
          )

        return sub_df_default

      def draw_group_summary_charts(raw_df, group_col, prefix):
        st.markdown(f'### 📊 [{prefix}] 전체 종합 비교 분석')
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

          grp_agg['주기별 수익률'] = (
              grp_agg.groupby(group_col)['총평가금액'].pct_change() * 100
          )
          first_eval = grp_agg.groupby(group_col)['총평가금액'].transform(
              'first'
          )
          grp_agg['선택기간 누적 수익률'] = np.where(
              first_eval > 0,
              ((grp_agg['총평가금액'] - first_eval) / first_eval) * 100,
              0,
          )
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
        fig_cum_ret = go.Figure()
        for grp in groups:
          sub = grp_agg_g4[grp_agg_g4[group_col] == grp]
          fig_cum_ret.add_trace(
              go.Scatter(
                  x=sub['Chart_Date'],
                  y=sub['선택기간 누적 수익률'],
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
          fig_cum_ret.add_trace(
              go.Scatter(
                  x=bm_df['Chart_Date'],
                  y=bm_df['기간 누적 수익률'],
                  mode='lines',
                  name=f'📌 {bm_name}',
                  line=bm_styles.get(bm_name, dict(dash='dot')),
                  hovertemplate='%{y:.2f}%',
              )
          )

        leg_cfg_g4, show_g4, margin_g4 = build_legend_config(pos_g4)
        suf_g4 = ' (환차손제외)' if ex_g4 else ''
        fig_cum_ret.update_layout(
            title=dict(
                text=(
                    f'🔹 [{prefix}] 선택기간 누적 수익률 Trend (꺾은선,'
                    f' 벤치마크 포함){suf_g4}'
                ),
                y=0.95,
                x=0.01,
                xanchor='left',
                yanchor='top',
                yref='container',
            ),
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
        fig_cum_ret.update_yaxes(
            title_text='누적 수익률 (%)',
            tickformat=',.2f',
            ticksuffix='%',
            zeroline=True,
        )

        if num_cols == 1:
          render_resizable_plotly_chart(fig_sel_p, key=f'grp_sel_{prefix}')
          render_resizable_plotly_chart(
              fig_period_p, key=f'grp_period_p_{prefix}'
          )
          render_resizable_plotly_chart(
              fig_period_ret, key=f'grp_period_ret_{prefix}'
          )
          render_resizable_plotly_chart(fig_cum_ret, key=f'grp_cum_{prefix}')
        elif num_cols == 2:
          ca, cb = st.columns(2)
          with ca:
            render_resizable_plotly_chart(fig_sel_p, key=f'grp_sel_{prefix}')
          with cb:
            render_resizable_plotly_chart(
                fig_period_p, key=f'grp_period_p_{prefix}'
            )
          cc, cd = st.columns(2)
          with cc:
            render_resizable_plotly_chart(
                fig_period_ret, key=f'grp_period_ret_{prefix}'
            )
          with cd:
            render_resizable_plotly_chart(fig_cum_ret, key=f'grp_cum_{prefix}')
        else:
          ca, cb, cc = st.columns(3)
          with ca:
            render_resizable_plotly_chart(fig_sel_p, key=f'grp_sel_{prefix}')
          with cb:
            render_resizable_plotly_chart(
                fig_period_p, key=f'grp_period_p_{prefix}'
            )
          with cc:
            render_resizable_plotly_chart(
                fig_period_ret, key=f'grp_period_ret_{prefix}'
            )
          render_resizable_plotly_chart(fig_cum_ret, key=f'grp_cum_{prefix}')

      def render_separate_charts(df, group_col, prefix):
        if group_col is None:
          draw_overall_total_charts(df)
        else:
          draw_group_summary_charts(df, group_col, prefix)
          st.write('---')
          st.markdown(f'### 📌 [{prefix}] 개별 상세 분석 차트')

          latest_d = df['Date'].max()
          latest_sub = df[df['Date'] == latest_d]
          item_order = (
              latest_sub.groupby(group_col)['총평가금액']
              .sum()
              .sort_values(ascending=False)
              .index.tolist()
          )
          all_items = df[group_col].unique().tolist()
          ordered_items = item_order + [
              i for i in all_items if i not in item_order
          ]

          tab_names = [f'{item}' for item in ordered_items]
          tabs = st.tabs(tab_names)

          for idx, item in enumerate(ordered_items):
            with tabs[idx]:
              sub_df = df[df[group_col] == item].copy()
              sub_agg = (
                  sub_df.groupby('Date')[
                      [
                          '원금',
                          '평가손익',
                          '총평가금액',
                          '원금_ex_fx',
                          '평가손익_ex_fx',
                          '총평가금액_ex_fx',
                      ]
                  ]
                  .sum()
                  .reset_index()
              )

              draw_single_chart(
                  sub_agg,
                  f'{prefix}: {item}',
                  force_single_col=(num_cols == 1),
              )

      tabs_list = [v for v in active_views]
      main_tabs = st.tabs(tabs_list)

      for i, v_type in enumerate(tabs_list):
        with main_tabs[i]:
          if v_type == '전체 합산':
            render_separate_charts(calc_df, None, '전체 합산')
          elif v_type == '계좌별':
            render_separate_charts(calc_df, 'account_num', '계좌별')
          elif v_type == '증권사(Broker)별':
            render_separate_charts(calc_df, 'broker', '증권사별')
          elif v_type == '계좌유형별':
            render_separate_charts(calc_df, 'account_type', '계좌유형별')
          elif v_type == 'Category 4별':
            render_separate_charts(calc_df, 'category4', 'Category 4별')
          elif v_type == '보유항목별':
            render_separate_charts(calc_df, 'item_name', '보유항목별')

# -----------------------------------------------------------------------------
# 메뉴 2: 계좌 별칭 관리
# -----------------------------------------------------------------------------
elif menu == '계좌 별칭 관리':
  st.header('🏷️ 계좌 별칭 관리')

  conn = get_connection()
  accounts_df = pd.read_sql(
      'SELECT DISTINCT broker, account_num, account_type, whose FROM'
      ' portfolio',
      conn,
  )
  alias_df = pd.read_sql('SELECT * FROM account_alias', conn)
  conn.close()

  if accounts_df.empty:
    st.info('포트폴리오에 등록된 계좌가 없습니다.')
  else:
    alias_dict = dict(zip(alias_df['account_num'], alias_df['alias']))

    merged_data = []
    for _, row in accounts_df.iterrows():
      acc = str(row['account_num'])
      merged_data.append({
          'whose': row.get('whose', '미지정'),
          'broker': row['broker'],
          'account_type': row['account_type'],
          'account_num': acc,
          'alias': alias_dict.get(acc, ''),
      })

    df_to_edit = pd.DataFrame(merged_data)

    st.write('등록된 계좌 리스트입니다. 별칭(Alias)을 수정 후 저장하세요.')
    edited_df = st.data_editor(
        df_to_edit,
        column_config={
            'whose': st.column_config.TextColumn(
                '소유자(Whose)', disabled=True
            ),
            'broker': st.column_config.TextColumn('증권사', disabled=True),
            'account_type': st.column_config.TextColumn(
                '계좌유형', disabled=True
            ),
            'account_num': st.column_config.TextColumn(
                '계좌번호', disabled=True
            ),
            'alias': st.column_config.TextColumn(
                '계좌 별칭 (수정가능)', help='구분하기 쉬운 명칭을 입력하세요'
            ),
        },
        hide_index=True,
        use_container_width=True,
    )

    if st.button('💾 별칭 저장하기'):
      conn = get_connection()
      c = conn.cursor()
      for _, row in edited_df.iterrows():
        acc = row['account_num']
        al = row['alias']
        c.execute(
            'INSERT OR REPLACE INTO account_alias (account_num, alias) VALUES'
            ' (?, ?)',
            (acc, al),
        )
      conn.commit()
      conn.close()
      create_local_backup()
      st.success('계좌 별칭이 성공적으로 저장되었습니다!')
      st.rerun()

# -----------------------------------------------------------------------------
# 메뉴 3: 포트폴리오 업로드
# -----------------------------------------------------------------------------
elif menu == '포트폴리오 업로드':
  st.header('📤 포트폴리오 엑셀 파일 업로드')

  uploaded_file = st.file_uploader(
      '포트폴리오 엑셀 파일 (.xlsx)', type=['xlsx']
  )

  if uploaded_file is not None:
    try:
      df_upload = pd.read_excel(uploaded_file)
      st.write('📋 업로드된 데이터 미리보기 (상위 5건):')
      st.dataframe(df_upload.head(), use_container_width=True)

      req_cols = [
          'record_date',
          'broker',
          'account_num',
          'account_type',
          'item_name',
          'quantity',
          'current_price',
      ]
      missing = [c for c in req_cols if c not in df_upload.columns]

      if missing:
        st.error(f'필수 컬럼이 누락되었습니다: {missing}')
      else:
        if 'whose' not in df_upload.columns:
          df_upload['whose'] = '미지정'

        for opt_col in [
            'ticker',
            'category1',
            'category2',
            'category3',
            'category4',
            'currency',
        ]:
          if opt_col not in df_upload.columns:
            df_upload[opt_col] = None

        if st.button('💾 DB에 저장하기'):
          create_local_backup()

          conn = get_connection()
          df_upload.to_sql('portfolio', conn, if_exists='append', index=False)
          conn.commit()
          conn.close()
          st.success('데이터가 성공적으로 저장 및 백업되었습니다!')
    except Exception as e:
      st.error(f'파일을 읽는 중 오류가 발생했습니다: {e}')

# -----------------------------------------------------------------------------
# 메뉴 4: 원금 및 입출금 관리
# -----------------------------------------------------------------------------
elif menu == '원금 및 입출금 관리':
  st.header('💵 초기 원금 및 입출금 관리')

  tab1, tab2 = st.tabs(['🏦 계좌별 초기 원금 설정', '💸 입출금 내역 등록'])

  conn = get_connection()
  acc_df = pd.read_sql(
      'SELECT DISTINCT account_num, broker FROM portfolio', conn
  )
  init_df = pd.read_sql('SELECT * FROM initial_principal', conn)
  cf_df = pd.read_sql('SELECT * FROM cash_flow', conn)
  conn.close()

  alias_dict = alias_map

  with tab1:
    st.subheader('계좌별 초기 투자 원금 관리')
    if acc_df.empty:
      st.info('등록된 계좌가 없습니다. 포트폴리오를 먼저 업로드해 주세요.')
    else:
      init_dict = dict(zip(init_df['account_num'], init_df['initial_amount']))

      merged_init = []
      for _, row in acc_df.iterrows():
        acc = str(row['account_num'])
        merged_init.append({
            'broker': row['broker'],
            'account_num': acc,
            'alias': alias_dict.get(acc, ''),
            'initial_amount': float(init_dict.get(acc, 0.0)),
        })

      init_edit_df = pd.DataFrame(merged_init)
      edited_init = st.data_editor(
          init_edit_df,
          column_config={
              'broker': st.column_config.TextColumn('증권사', disabled=True),
              'account_num': st.column_config.TextColumn(
                  '계좌번호', disabled=True
              ),
              'alias': st.column_config.TextColumn('별칭', disabled=True),
              'initial_amount': st.column_config.NumberColumn(
                  '초기 원금 (원)', format='%d', min_value=0
              ),
          },
          hide_index=True,
          use_container_width=True,
      )

      if st.button('💾 초기 원금 저장'):
        conn = get_connection()
        c = conn.cursor()
        for _, row in edited_init.iterrows():
          c.execute(
              'INSERT OR REPLACE INTO initial_principal (account_num, broker,'
              ' initial_amount) VALUES (?, ?, ?)',
              (row['account_num'], row['broker'], row['initial_amount']),
          )
        conn.commit()
        conn.close()
        create_local_backup()
        st.success('초기 원금이 저장 및 백업되었습니다!')
        st.rerun()

  with tab2:
    st.subheader('입출금 내역 등록 및 관리')
    with st.form('cf_form'):
      cf_date = st.date_input('거래일자', date.today())
      acc_list = acc_df['account_num'].tolist() if not acc_df.empty else []
      acc_labels = [
          f"{a} ({alias_dict.get(str(a), '별칭없음')})" for a in acc_list
      ]

      sel_acc_label = st.selectbox('계좌 선택', options=acc_labels)
      cf_type = st.radio('구분', ['입금', '출금'], horizontal=True)
      cf_amount = st.number_input('금액 (원)', value=0, step=10000)
      cf_note = st.text_input('비고 (선택사항)')

      submitted = st.form_submit_button('➕ 입출금 내역 추가')

      if submitted:
        if sel_acc_label:
          sel_acc_num = sel_acc_label.split(' (')[0]
          conn = get_connection()
          c = conn.cursor()
          c.execute(
              'INSERT INTO cash_flow (trans_date, account_num, flow_type,'
              ' amount, note) VALUES (?, ?, ?, ?, ?)',
              (
                  cf_date.strftime('%Y-%m-%d'),
                  sel_acc_num,
                  cf_type,
                  cf_amount,
                  cf_note,
              ),
          )
          conn.commit()
          conn.close()
          create_local_backup()
          st.success('입출금 내역이 등록되었습니다!')
          st.rerun()

    st.write('---')
    st.subheader('등록된 입출금 내역 목록')
    if not cf_df.empty:
      st.dataframe(cf_df, use_container_width=True)

# -----------------------------------------------------------------------------
# 메뉴 5: 등록 데이터 조회 및 관리
# -----------------------------------------------------------------------------
elif menu == '등록 데이터 조회 및 관리':
  st.header('🗂️ 등록 데이터 조회 및 관리')

  tab_pf, tab_init, tab_cf, tab_alias, tab_bak = st.tabs([
      '📊 포트폴리오 DB',
      '💵 초기 원금 DB',
      '💸 입출금 내역 DB',
      '🏷️ 계좌 별칭 DB',
      '💾 백업 및 복원',
  ])

  conn = get_connection()

  with tab_pf:
    pf_df = pd.read_sql('SELECT * FROM portfolio', conn)
    st.subheader(f'포트폴리오 레코드 (총 {len(pf_df)} 건)')
    st.dataframe(pf_df, use_container_width=True)
    if not pf_df.empty:
      if st.button('🗑️ 포트폴리오 데이터 전체 삭제', type='primary'):
        create_local_backup()
        c = conn.cursor()
        c.execute('DELETE FROM portfolio')
        conn.commit()
        st.success('포트폴리오 데이터가 초기화되었습니다.')
        st.rerun()

  with tab_init:
    init_df = pd.read_sql('SELECT * FROM initial_principal', conn)
    st.subheader(f'초기 원금 레코드 (총 {len(init_df)} 건)')
    st.dataframe(init_df, use_container_width=True)

  with tab_cf:
    cf_df = pd.read_sql('SELECT * FROM cash_flow', conn)
    st.subheader(f'입출금 레코드 (총 {len(cf_df)} 건)')
    st.dataframe(cf_df, use_container_width=True)

  with tab_alias:
    alias_df = pd.read_sql('SELECT * FROM account_alias', conn)
    st.subheader(f'계좌 별칭 레코드 (총 {len(alias_df)} 건)')
    st.dataframe(alias_df, use_container_width=True)

  with tab_bak:
    st.subheader('💾 데이터 백업 및 복원 관리')

    c1, c2 = st.columns(2)
    with c1:
      st.markdown('#### 📥 DB 및 엑셀 다운로드 백업')
      if os.path.exists(DB_FILE):
        with open(DB_FILE, 'rb') as f:
          db_bytes = f.read()
        today_str = datetime.now().strftime('%Y%m%d_%H%M%S')
        st.download_button(
            label='📥 SQLite DB 파일 다운로드 (.db)',
            data=db_bytes,
            file_name=f'asset_tracker_{today_str}.db',
            mime='application/x-sqlite3',
            use_container_width=True,
        )

      excel_bytes = export_all_to_excel_bytes()
      st.download_button(
          label='📊 전체 DB 엑셀 파일 다운로드 (.xlsx)',
          data=excel_bytes,
          file_name=f'asset_tracker_backup_{datetime.now().strftime("%Y%m%d_%H%M%S")}.xlsx',
          mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
          use_container_width=True,
      )

    with c2:
      st.markdown('#### 📂 DB 복원')
      uploaded_db = st.file_uploader(
          'SQLite DB 파일 (.db) 업로드', type=['db']
      )
      if uploaded_db is not None:
        if st.button('🔄 복원 실행'):
          create_local_backup()
          with open(DB_FILE, 'wb') as f:
            f.write(uploaded_db.getvalue())
          st.success('DB 파일이 성공적으로 복원되었습니다!')
          st.rerun()

  conn.close()

# -----------------------------------------------------------------------------
# 메뉴 6: 데이터 백업 및 복원
# -----------------------------------------------------------------------------
elif menu == '데이터 백업 및 복원':
  st.header('💾 백업 파일 히스토리 관리')
  backup_files = get_backup_files()

  if not backup_files:
    st.info('생성된 자동 로컬 백업 파일이 없습니다.')
  else:
    st.write('로컬 자동 백업 파일 히스토리 목록입니다.')
    for b_file in backup_files:
      st.text(
          f"📂 {os.path.basename(b_file)} (생성시각:"
          f" {datetime.fromtimestamp(os.path.getmtime(b_file)).strftime('%Y-%m-%d %H:%M:%S')})"
      )