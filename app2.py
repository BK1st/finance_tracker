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
DB_FILE = 'asset_tracker.db'


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
  if alias_df.empty:
    return {}
  alias_df['account_num'] = alias_df['account_num'].astype(str).str.strip()
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


@st.cache_data(ttl=3600 * 12)
def _fetch_yfinance_data(all_tickers_tuple, start_date, end_date):
  all_tickers = list(all_tickers_tuple)
  if not all_tickers:
    return pd.DataFrame()
  try:
    df = yf.download(
        all_tickers, start=start_date, end=end_date, progress=False
    )
    if df.empty:
      return pd.DataFrame()

    if 'Adj Close' in df:
      data = df['Adj Close']
    elif 'Close' in df:
      data = df['Close']
    else:
      data = df

    if isinstance(data, pd.Series):
      data = data.to_frame(name=all_tickers[0])

    data = data.ffill().bfill()
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
# 3. Streamlit 대시보드 메인
# -----------------------------------------------------------------------------
st.set_page_config(page_title='원금 대비 평가액 TREND 관리', layout='wide')
st.title('📈 자산 평가액 및 수익률 분석 시스템')

menu = st.sidebar.selectbox(
    '메뉴 선택',
    [
        '트렌드 리포트',
        '연도별 수익률 리포트',
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

# -----------------------------------------------------------------------------
# 메뉴 1: 트렌드 리포트 (app2_1006 트렌드 구성 적용)
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
    pf_df['account_num'] = pf_df['account_num'].astype(str).str.strip()
    pf_df['record_date'] = pd.to_datetime(pf_df['record_date']).dt.strftime(
        '%Y-%m-%d'
    )

    if not init_p_df.empty:
      init_p_df['account_num'] = (
          init_p_df['account_num'].astype(str).str.strip()
      )
    if not cf_df.empty:
      cf_df['account_num'] = cf_df['account_num'].astype(str).str.strip()

    acc_info_df = pf_df[
        ['broker', 'account_num', 'account_type']
    ].drop_duplicates()

    acc_options = []
    for _, row in acc_info_df.iterrows():
      acc_num = row['account_num']
      alias = alias_map.get(acc_num, '')
      display_alias = (
          alias
          if alias
          else f"미지정별칭({acc_num[-4:] if len(acc_num)>=4 else acc_num})"
      )
      acc_type_str = (
          row['account_type'] if pd.notna(row['account_type']) else '미지정'
      )
      label = f"{row['broker']} | {display_alias} [{acc_type_str}]"
      acc_options.append(label)

    min_rec_date = pd.to_datetime(pf_df['record_date']).min().date()
    max_rec_date = date.today()

    freq_options = [
        '일간 (매일)',
        '일간 (주말 제외)',
        '주간 (매주 토요일)',
        '월간 (매달 말일)',
        '연간 (매년 말일)',
    ]
    bm_options = ['미국 SPY', '미국 QQQ', '한국 KOSPI']
    all_view_types = ['전체 합산', '계좌별', '증권사(Broker)별', '계좌유형별']

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
            default=st.session_state.get('trend_view_types', ['전체 합산', '계좌별']),
        )
        selected_bm = st.multiselect(
            '📈 비교 벤치마크 지수 선택 (차트에 함께 표시)',
            options=bm_options,
            default=st.session_state.get('trend_sel_bm', bm_options),
            help=(
                '선택한 벤치마크 지수의 수익률 트렌드가 수익률 그래프에 점선으로'
                ' 표시됩니다.'
            ),
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
              st.session_state.get('trend_start_date', min_rec_date),
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
            value=st.session_state.get('trend_mirae_val', 0.0),
            step=1000000.0,
            help=(
                '미래에셋 증권 계좌는 수동 입력 가액이 전 분석 기간에 공통'
                ' 반영됩니다.'
            ),
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
            m_acc = str(m_row['account_num']).strip()
            m_alias = alias_map.get(m_acc, '')
            m_display = (
                m_alias
                if m_alias
                else f"미지정별칭({m_acc[-4:] if len(m_acc)>=4 else m_acc})"
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
          pf_df['account_num'].isin(selected_accounts)
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

        usd_krw = 1350.0
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
          acc = str(acc).strip()
          acc_meta = filtered_pf_df[filtered_pf_df['account_num'] == acc]
          broker_name = (
              acc_meta['broker'].iloc[0] if not acc_meta.empty else '미지정'
          )
          acc_type = (
              acc_meta['account_type'].iloc[0]
              if not acc_meta.empty
              and pd.notna(acc_meta['account_type'].iloc[0])
              else '미지정'
          )

          init_val = (
              init_p_df[init_p_df['account_num'] == acc]['initial_amount'].sum()
              if not init_p_df.empty
              else 0
          )
          if not cf_df.empty:
            acc_cf = cf_df[
                (cf_df['account_num'] == acc) & (cf_df['trans_date'] <= t_str)
            ]
            in_flow = acc_cf[acc_cf['flow_type'] == '입금']['amount'].sum()
            out_flow = acc_cf[acc_cf['flow_type'] == '출금']['amount'].sum()
          else:
            in_flow, out_flow = 0, 0
          principal = init_val + in_flow - out_flow

          if '미래에셋' in str(broker_name):
            eval_amount = mirae_eval_val
          else:
            acc_pf = filtered_pf_df[
                (filtered_pf_df['account_num'] == acc)
                & (filtered_pf_df['record_date'] <= t_str)
            ]
            if acc_pf.empty:
              min_date = filtered_pf_df[filtered_pf_df['account_num'] == acc][
                  'record_date'
              ].min()
              acc_pf = filtered_pf_df[
                  (filtered_pf_df['account_num'] == acc)
                  & (filtered_pf_df['record_date'] == min_date)
              ]

            eval_amount = 0
            if not acc_pf.empty:
              latest_date = acc_pf['record_date'].max()
              current_pf = acc_pf[acc_pf['record_date'] == latest_date]
              for _, row in current_pf.iterrows():
                fmt_tk = format_ticker(row['ticker'])
                qty = row['quantity'] if pd.notna(row['quantity']) else 0
                curr = row['currency']
                base_price = (
                    row['current_price']
                    if 'current_price' in row and pd.notna(row['current_price'])
                    else 0
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
                eval_amount += item_eval

          p_loss = eval_amount - principal

          acc_alias_val = alias_map.get(acc, '')
          acc_label = (
              acc_alias_val
              if acc_alias_val
              else f"미지정별칭({acc[-4:] if len(acc)>=4 else acc})"
          )

          base_records.append({
              'Date': t_str,
              'account_num': acc_label,
              'broker': broker_name,
              'account_type': acc_type,
              '원금': principal,
              '평가손익': p_loss,
              '총평가금액': eval_amount,
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
      active_views = st.session_state.get(
          'trend_view_types', ['전체 합산', '계좌별']
      )

      st.write('---')
      st.subheader('🖥️ 화면 디스플레이 및 Y축 범주 설정 (실시간 반영)')

      disp_col1, disp_col2, disp_col3, disp_col4 = st.columns([2, 2, 2, 3])

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
        y_scale_choice = st.selectbox(
            '📏 Y축 Scale 타입',
            options=['Linear (선형)', 'Logarithmic (로그)'],
            index=0,
            key='live_y_scale',
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
                'Y축 최소값 (원)',
                value=0.0,
                step=1000000.0,
                key='live_ymin',
            )
          with r_c2:
            y_max_val = st.number_input(
                'Y축 최대값 (원)',
                value=100000000.0,
                step=1000000.0,
                key='live_ymax',
            )

      num_cols = (
          1 if '1열' in layout_setting else (2 if '2열' in layout_setting else 3)
      )
      y_scale_setting = (
          'log' if y_scale_choice == 'Logarithmic (로그)' else 'linear'
      )

      def apply_y_axis_config(fig, axis_name='yaxis', is_money=True):
        kwargs = dict(type=y_scale_setting, zeroline=True)
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

        # app2_1006 지표 데이터 계산 로직
        sub_df['수익률'] = np.where(
            sub_df['원금'] > 0, (sub_df['평가손익'] / sub_df['원금']) * 100, 0
        )
        sub_df['주기별 평가손익'] = sub_df['총평가금액'].diff()
        base_start_p_loss = (
            sub_df['평가손익'].iloc[0] if len(sub_df) > 0 else 0
        )
        sub_df['선택구간 누적손익'] = sub_df['평가손익'] - base_start_p_loss
        sub_df['구간별 수익률'] = sub_df['총평가금액'].pct_change() * 100
        initial_eval = sub_df['총평가금액'].iloc[0] if len(sub_df) > 0 else 0
        sub_df['구간별 누적수익률'] = np.where(
            initial_eval > 0,
            ((sub_df['총평가금액'] - initial_eval) / initial_eval) * 100,
            0,
        )

        selected_cum_p_loss = (
            sub_df['선택구간 누적손익'].iloc[-1] if len(sub_df) > 0 else 0
        )
        total_cum_p_loss = sub_df['평가손익'].iloc[-1] if len(sub_df) > 0 else 0

        c_m1, c_m2, c_m3 = st.columns(3)
        c_m1.metric(
            '📌 선택 구간 누적 평가손익', f'{selected_cum_p_loss:,.0f} 원'
        )
        c_m2.metric(
            '🏛️ 전체 통산 누적 평가손익', f'{total_cum_p_loss:,.0f} 원'
        )
        c_m3.metric(
            '💰 최종 기말 평가금액',
            f"{sub_df['총평가금액'].iloc[-1] if len(sub_df)>0 else 0:,.0f} 원",
        )

        top_legend_config = dict(
            orientation='h', yanchor='bottom', y=1.02, xanchor='center', x=0.5
        )

        # ---------------------------------------------------------------------
        # 1. 자산 및 전체 손익/수익률 추이 (app2_1006 동일)
        # ---------------------------------------------------------------------
        fig1 = make_subplots(specs=[[{'secondary_y': True}]])
        fig1.add_trace(
            go.Bar(
                x=sub_df['Chart_Date'],
                y=sub_df['원금'],
                name='원금',
                marker_color='#2b5c8f',
                opacity=0.6,
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
                marker=dict(size=6),
                text=[f'{v:,.0f}' for v in sub_df['총평가금액']],
                textposition='top center',
            ),
            secondary_y=False,
        )
        fig1.add_trace(
            go.Scatter(
                x=sub_df['Chart_Date'],
                y=sub_df['수익률'],
                name='원금 대비 수익률 (%)',
                mode='lines+markers',
                line=dict(color='#2ca02c', width=2, dash='dash'),
                marker=dict(size=5),
                hovertemplate='%{y:.2f}%',
            ),
            secondary_y=True,
        )

        fig1.update_layout(
            title=f'1. [{title_name}] 자산 및 전체 손익/수익률 추이',
            barmode='relative',
            hovermode='x unified',
            height=430,
            legend=top_legend_config,
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

        # ---------------------------------------------------------------------
        # 2. 구간 손익 금액 추이 (app2_1006 동일)
        # ---------------------------------------------------------------------
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
            )
        )
        fig2.add_trace(
            go.Scatter(
                x=sub_df['Chart_Date'],
                y=sub_df['선택구간 누적손익'],
                name='선택구간 누적손익 (추이)',
                mode='lines+markers',
                line=dict(color='#9467bd', width=2.5),
                marker=dict(size=6),
            )
        )

        fig2.update_layout(
            title=f'2. [{title_name}] 구간 손익 금액 추이',
            hovermode='x unified',
            height=430,
            legend=top_legend_config,
        )
        fig2.update_xaxes(
            type='category',
            categoryorder='array',
            categoryarray=date_order_list,
        )
        apply_y_axis_config(fig2, axis_name='yaxis', is_money=True)
        fig2.update_yaxes(title_text='손익금액 (원)', tickformat=',.0f')

        # ---------------------------------------------------------------------
        # 3-1. 구간 누적수익률 추이 (벤치마크 비교) (app2_1006 동일)
        # ---------------------------------------------------------------------
        fig3a = go.Figure()
        fig3a.add_trace(
            go.Scatter(
                x=sub_df['Chart_Date'],
                y=sub_df['구간별 누적수익률'],
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

        fig3a.update_layout(
            title=f'3-1. [{title_name}] 구간 누적수익률 추이 (벤치마크 비교)',
            hovermode='x unified',
            height=430,
            legend=top_legend_config,
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

        # ---------------------------------------------------------------------
        # 3-2. 주기별 수익률 추이 (벤치마크 비교) (app2_1006 동일)
        # ---------------------------------------------------------------------
        fig3b = go.Figure()
        fig3b.add_trace(
            go.Scatter(
                x=sub_df['Chart_Date'],
                y=sub_df['구간별 수익률'],
                name='주기별 수익률 (%)',
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

        fig3b.update_layout(
            title=f'3-2. [{title_name}] 주기별 수익률 추이 (벤치마크 비교)',
            hovermode='x unified',
            height=430,
            legend=top_legend_config,
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

        # ---------------------------------------------------------------------
        # 레이아웃 옵션에 따른 차트 화면 배치 렌더링
        # ---------------------------------------------------------------------
        effective_cols = 1 if force_single_col else num_cols

        if effective_cols == 1:
          st.plotly_chart(
              fig1, use_container_width=True, key=f'trend_fig1_{title_name}'
          )
          st.plotly_chart(
              fig2, use_container_width=True, key=f'trend_fig2_{title_name}'
          )
          st.plotly_chart(
              fig3a, use_container_width=True, key=f'trend_fig3a_{title_name}'
          )
          st.plotly_chart(
              fig3b, use_container_width=True, key=f'trend_fig3b_{title_name}'
          )
        elif effective_cols == 2:
          col_a, col_b = st.columns(2)
          with col_a:
            st.plotly_chart(
                fig1, use_container_width=True, key=f'trend_fig1_{title_name}'
            )
          with col_b:
            st.plotly_chart(
                fig2, use_container_width=True, key=f'trend_fig2_{title_name}'
            )
          col_c, col_d = st.columns(2)
          with col_c:
            st.plotly_chart(
                fig3a,
                use_container_width=True,
                key=f'trend_fig3a_{title_name}',
            )
          with col_d:
            st.plotly_chart(
                fig3b,
                use_container_width=True,
                key=f'trend_fig3b_{title_name}',
            )
        else:
          col_a, col_b, col_c = st.columns(3)
          with col_a:
            st.plotly_chart(
                fig1, use_container_width=True, key=f'trend_fig1_{title_name}'
            )
          with col_b:
            st.plotly_chart(
                fig2, use_container_width=True, key=f'trend_fig2_{title_name}'
            )
          with col_c:
            st.plotly_chart(
                fig3a,
                use_container_width=True,
                key=f'trend_fig3a_{title_name}',
            )
          st.plotly_chart(
              fig3b, use_container_width=True, key=f'trend_fig3b_{title_name}'
          )

        return sub_df

      def render_separate_charts(df, group_col, prefix):
        if group_col is None:
          agg_df = (
              df.groupby('Date')[['원금', '평가손익', '총평가금액']]
              .sum()
              .reset_index()
          )
          draw_single_chart(agg_df, '전체 합산')
        else:
          groups = sorted(df[group_col].unique())
          if num_cols > 1:
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

# -----------------------------------------------------------------------------
# 메뉴 2: 연도별 수익률 리포트 (기존 기능 유지)
# -----------------------------------------------------------------------------
elif menu == '연도별 수익률 리포트':
  st.header('📅 수익률 분석 리포트 (기간 자유 선택 및 다차원 관점)')

  conn = get_connection()
  pf_df = pd.read_sql('SELECT * FROM portfolio', conn)
  conn.close()

  if pf_df.empty:
    st.warning(
        '등록된 포트폴리오가 없습니다. [포트폴리오 업로드] 메뉴에서 데이터를'
        ' 먼저 등록해 주세요.'
    )
  else:
    pf_df['account_num'] = pf_df['account_num'].astype(str).str.strip()
    pf_df['record_date'] = pd.to_datetime(pf_df['record_date']).dt.strftime(
        '%Y-%m-%d'
    )

    acc_info_df = pf_df[
        ['broker', 'account_num', 'account_type']
    ].drop_duplicates()

    acc_options = []
    for _, row in acc_info_df.iterrows():
      acc_num = row['account_num']
      alias = alias_map.get(acc_num, '')
      display_alias = (
          alias
          if alias
          else f"미지정별칭({acc_num[-4:] if len(acc_num)>=4 else acc_num})"
      )
      acc_type_str = (
          row['account_type'] if pd.notna(row['account_type']) else '미지정'
      )
      label = f"{row['broker']} | {display_alias} [{acc_type_str}]"
      acc_options.append(label)

    min_rec_date = pd.to_datetime(pf_df['record_date']).min().date()
    max_rec_date = date.today()

    freq_ret_options = [
        '일간 (매일)',
        '일간 (주말 제외)',
        '주간 (매주 토요일)',
        '월간 (매달 말일)',
    ]
    bm_options = ['미국 SPY', '미국 QQQ', '한국 KOSPI']
    view_options = [
        '전체 합산',
        '계좌별',
        '증권사(Broker)별',
        '계좌유형별',
        'Category 1별',
        'Category 4별',
    ]

    with st.form('return_report_form'):
      st.subheader('⚙️ 분석 조건 설정')
      c1, c2 = st.columns(2)
      with c1:
        selected_acc_labels = st.multiselect(
            '조회할 계좌 선택',
            options=acc_options,
            default=st.session_state.get('ret_sel_accs', acc_options),
        )
        view_types = st.multiselect(
            '표시할 관점 선택',
            options=view_options,
            default=st.session_state.get(
                'ret_view_types',
                ['전체 합산', '계좌별', '증권사(Broker)별', '계좌유형별'],
            ),
        )
        selected_bm = st.multiselect(
            '📈 비교 벤치마크 지수 선택 (차트에 함께 표시)',
            options=bm_options,
            default=st.session_state.get('ret_sel_bm', bm_options),
        )

      with c2:
        saved_ret_freq = st.session_state.get(
            'ret_freq_str', '주간 (매주 토요일)'
        )
        if saved_ret_freq not in freq_ret_options:
          saved_ret_freq = '주간 (매주 토요일)'

        freq_type = st.radio(
            '주기 선택',
            options=freq_ret_options,
            index=freq_ret_options.index(saved_ret_freq),
            horizontal=True,
        )

        d1, d2 = st.columns(2)
        with d1:
          start_date = st.date_input(
              '조회 시작일',
              st.session_state.get('ret_start_date', min_rec_date),
          )
        with d2:
          end_date = st.date_input(
              '조회 종료일',
              st.session_state.get(
                  'ret_end_date',
                  max_rec_date if max_rec_date >= min_rec_date else min_rec_date,
              ),
          )

      st.write('---')
      mirae_val = st.number_input(
          '🏦 미래에셋 계좌 평가금액 수동 입력 (원)',
          value=st.session_state.get('ret_mirae_val', 0.0),
          step=1000000.0,
      )

      run_returns = st.form_submit_button('🚀 수익률 분석 실행 (Run)')

    if run_returns:
      st.session_state['ret_sel_accs'] = selected_acc_labels
      st.session_state['ret_view_types'] = view_types
      st.session_state['ret_sel_bm'] = selected_bm
      st.session_state['ret_freq_str'] = freq_type
      st.session_state['ret_start_date'] = start_date
      st.session_state['ret_end_date'] = end_date
      st.session_state['ret_mirae_val'] = mirae_val

      selected_accounts = []
      for lbl in selected_acc_labels:
        parts = lbl.split('|')
        if len(parts) >= 2:
          b_name = parts[0].strip()
          rest = parts[1].strip()
          alias_part = rest.split('[')[0].strip()
          matched_rows = acc_info_df[acc_info_df['broker'] == b_name]
          for _, m_row in matched_rows.iterrows():
            m_acc = str(m_row['account_num']).strip()
            m_alias = alias_map.get(m_acc, '')
            m_display = (
                m_alias
                if m_alias
                else f"미지정별칭({m_acc[-4:] if len(m_acc)>=4 else m_acc})"
            )
            if m_display == alias_part:
              selected_accounts.append(m_acc)
              break
      selected_accounts = list(set(selected_accounts))

      full_dates = pd.date_range(start=start_date, end=end_date)
      if freq_type == '일간 (매일)':
        target_dates = full_dates
      elif freq_type == '일간 (주말 제외)':
        target_dates = full_dates[full_dates.dayofweek < 5]
      elif freq_type == '주간 (매주 토요일)':
        target_dates = full_dates[full_dates.dayofweek == 5]
      else:
        target_dates = full_dates[full_dates.is_month_end]

      target_dates = pd.DatetimeIndex(
          sorted(list(set(target_dates).union({pd.to_datetime(end_date)})))
      )

      filtered_pf_df = pf_df[
          pf_df['account_num'].isin(selected_accounts)
      ].copy()
      fetch_tickers = list(filtered_pf_df['ticker'].unique()) + list(
          bm_ticker_map.values()
      )

      with st.spinner(
          '최신 시세 및 벤치마크 데이터를 수집하고 수익률을 산출 중입니다...'
      ):
        s_str = start_date.strftime('%Y-%m-%d')
        e_str = (end_date + pd.Timedelta(days=3)).strftime('%Y-%m-%d')
        market_data = fetch_market_data(
            fetch_tickers, s_str, e_str, force_refresh=True
        )

      eval_records = []
      for t_date in target_dates:
        t_str = t_date.strftime('%Y-%m-%d')

        usd_krw = 1350.0
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
          acc = str(acc).strip()
          acc_meta = filtered_pf_df[filtered_pf_df['account_num'] == acc]
          broker_name = (
              acc_meta['broker'].iloc[0] if not acc_meta.empty else '미지정'
          )
          acc_type = (
              acc_meta['account_type'].iloc[0]
              if not acc_meta.empty
              and pd.notna(acc_meta['account_type'].iloc[0])
              else '미지정'
          )

          acc_alias_val = alias_map.get(acc, '')
          acc_label = (
              acc_alias_val
              if acc_alias_val
              else f"미지정별칭({acc[-4:] if len(acc)>=4 else acc})"
          )

          if '미래에셋' in str(broker_name):
            eval_records.append({
                'Date': t_date,
                'Date_str': t_str,
                'account_num': acc_label,
                'broker': broker_name,
                'account_type': acc_type,
                'category1': '미래에셋 수동',
                'category4': '미래에셋 수동',
                '평가금액': mirae_val,
            })
          else:
            acc_pf = filtered_pf_df[
                (filtered_pf_df['account_num'] == acc)
                & (filtered_pf_df['record_date'] <= t_str)
            ]
            if acc_pf.empty:
              min_date = filtered_pf_df[filtered_pf_df['account_num'] == acc][
                  'record_date'
              ].min()
              acc_pf = filtered_pf_df[
                  (filtered_pf_df['account_num'] == acc)
                  & (filtered_pf_df['record_date'] == min_date)
              ]

            if not acc_pf.empty:
              latest_date = acc_pf['record_date'].max()
              current_pf = acc_pf[acc_pf['record_date'] == latest_date]
              for _, row in current_pf.iterrows():
                fmt_tk = format_ticker(row['ticker'])
                qty = row['quantity'] if pd.notna(row['quantity']) else 0
                curr = row['currency']
                base_price = (
                    row['current_price']
                    if 'current_price' in row and pd.notna(row['current_price'])
                    else 0
                )

                cat1 = (
                    row['category1']
                    if pd.notna(row['category1'])
                    and str(row['category1']).strip() != ''
                    else '미지정'
                )
                cat4 = (
                    row['category4']
                    if pd.notna(row['category4'])
                    and str(row['category4']).strip() != ''
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

                eval_records.append({
                    'Date': t_date,
                    'Date_str': t_str,
                    'account_num': acc_label,
                    'broker': broker_name,
                    'account_type': acc_type,
                    'category1': cat1,
                    'category4': cat4,
                    '평가금액': item_eval,
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

      st.session_state['returns_calc_df'] = pd.DataFrame(eval_records)
      st.session_state['returns_bm_calc'] = bm_calc_dict

    if (
        'returns_calc_df' in st.session_state
        and not st.session_state['returns_calc_df'].empty
    ):
      raw_returns_df = st.session_state['returns_calc_df']
      bm_calc_dict = st.session_state.get('returns_bm_calc', {})
      active_ret_views = st.session_state.get(
          'ret_view_types',
          ['전체 합산', '계좌별', '증권사(Broker)별', '계좌유형별'],
      )

      st.write('---')
      st.subheader('🖥️ 화면 디스플레이 설정')

      def calculate_group_returns(df, group_col=None):
        if group_col is None:
          grp_df = (
              df.groupby(['Date', 'Date_str'])['평가금액']
              .sum()
              .reset_index()
              .sort_values('Date')
          )
          grp_df['Group'] = '전체 합산'
        else:
          grp_df = (
              df.groupby(['Date', 'Date_str', group_col])['평가금액']
              .sum()
              .reset_index()
              .sort_values(['Date'])
          )
          grp_df.rename(columns={group_col: 'Group'}, inplace=True)

        res_list = []
        for grp_name, group_data in grp_df.groupby('Group'):
          group_data = group_data.sort_values('Date').reset_index(drop=True)
          initial_base_eval = (
              group_data.loc[0, '평가금액'] if len(group_data) > 0 else 0
          )
          group_data['주기별 수익률'] = np.nan
          group_data['기간 누적 수익률'] = np.nan

          for i in range(len(group_data)):
            curr_eval = group_data.loc[i, '평가금액']
            prev_eval = (
                group_data.loc[i - 1, '평가금액']
                if i > 0
                else initial_base_eval
            )

            if i > 0 and prev_eval > 0:
              group_data.loc[i, '주기별 수익률'] = (
                  (curr_eval - prev_eval) / prev_eval
              ) * 100
            if initial_base_eval > 0:
              group_data.loc[i, '기간 누적 수익률'] = (
                  (curr_eval - initial_base_eval) / initial_base_eval
              ) * 100

          res_list.append(group_data)

        return (
            pd.concat(res_list, ignore_index=True)
            if res_list
            else pd.DataFrame()
        )

      tabs = st.tabs(active_ret_views)
      for i, v_type in enumerate(active_ret_views):
        with tabs[i]:
          col_mapping = {
              '전체 합산': None,
              '계좌별': 'account_num',
              '증권사(Broker)별': 'broker',
              '계좌유형별': 'account_type',
              'Category 1별': 'category1',
              'Category 4별': 'category4',
          }
          g_col = col_mapping.get(v_type)
          res_df = calculate_group_returns(raw_returns_df, g_col)

          if not res_df.empty:
            fig_cum = go.Figure()
            for grp in res_df['Group'].unique():
              sub = res_df[res_df['Group'] == grp]
              fig_cum.add_trace(
                  go.Scatter(
                      x=sub['Date_str'],
                      y=sub['기간 누적 수익률'],
                      mode='lines+markers',
                      name=str(grp),
                  )
              )

            for bm_name, bm_df in bm_calc_dict.items():
              fig_cum.add_trace(
                  go.Scatter(
                      x=bm_df['Date_str'],
                      y=bm_df['기간 누적 수익률'],
                      mode='lines',
                      name=f'📌 {bm_name}',
                      line=bm_styles.get(bm_name, dict(dash='dot')),
                  )
              )

            fig_cum.update_layout(
                title=f'[{v_type}] 누적 수익률 추이',
                hovermode='x unified',
                height=450,
                legend=dict(
                    orientation='h',
                    yanchor='bottom',
                    y=1.02,
                    xanchor='center',
                    x=0.5,
                ),
            )
            fig_cum.update_yaxes(
                title_text='수익률 (%)', tickformat=',.2f', ticksuffix='%'
            )
            st.plotly_chart(
                fig_cum, use_container_width=True, key=f'ret_cum_{v_type}'
            )

# -----------------------------------------------------------------------------
# 메뉴 3: 계좌 별칭 관리 (기존 기능 유지)
# -----------------------------------------------------------------------------
elif menu == '계좌 별칭 관리':
  st.header('🏷️ 계좌 별칭(Alias) 지정 및 관리')

  conn = get_connection()
  pf_df = pd.read_sql(
      'SELECT DISTINCT broker, account_num FROM portfolio', conn
  )
  alias_df = pd.read_sql('SELECT * FROM account_alias', conn)
  conn.close()

  if pf_df.empty:
    st.info('등록된 포트폴리오 계좌 정보가 없습니다.')
  else:
    pf_df['account_num'] = pf_df['account_num'].astype(str).str.strip()
    pf_df = pf_df.drop_duplicates(subset=['account_num'])

    if not alias_df.empty:
      alias_df['account_num'] = alias_df['account_num'].astype(str).str.strip()

    merged_df = pd.merge(pf_df, alias_df, on='account_num', how='left').fillna(
        {'alias': ''}
    )

    st.write('등록된 계좌에 표시할 별칭을 설정하세요.')

    updated_aliases = {}
    with st.form('alias_form'):
      for _, row in merged_df.iterrows():
        acc_num = str(row['account_num']).strip()
        broker = row['broker']
        curr_alias = row['alias']

        new_alias = st.text_input(
            f'[{broker}] 계좌번호: {acc_num}',
            value=curr_alias,
            key=f'alias_{acc_num}',
        )
        updated_aliases[acc_num] = new_alias

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
            (str(acc_num).strip(), alias_val.strip()),
        )
      conn.commit()
      conn.close()
      st.success('계좌 별칭이 성공적으로 저장되었습니다!')
      st.rerun()

# -----------------------------------------------------------------------------
# 메뉴 4: 포트폴리오 업로드 (기존 기능 유지)
# -----------------------------------------------------------------------------
elif menu == '포트폴리오 업로드':
  st.header('📥 포트폴리오 엑셀 업로드')

  uploaded_file = st.file_uploader(
      '포트폴리오 엑셀 파일 (.xlsx, .xls)을 선택하세요.', type=['xlsx', 'xls']
  )

  if uploaded_file is not None:
    try:
      df = pd.read_excel(uploaded_file)
      required_cols = [
          'record_date',
          'broker',
          'account_num',
          'account_type',
          'item_name',
          'ticker',
          'quantity',
          'current_price',
          'currency',
      ]

      missing_cols = [c for c in required_cols if c not in df.columns]
      if missing_cols:
        st.error(f"필수 컬럼이 누락되었습니다: {', '.join(missing_cols)}")
      else:
        df['record_date'] = pd.to_datetime(df['record_date']).dt.strftime(
            '%Y-%m-%d'
        )
        df['account_num'] = df['account_num'].astype(str).str.strip()

        st.subheader('📄 업로드 데이터 미리보기')
        st.dataframe(df.head(10))

        if st.button('💾 DB에 저장하기'):
          conn = get_connection()
          c = conn.cursor()

          for r_date in df['record_date'].unique():
            c.execute(
                'DELETE FROM portfolio WHERE record_date = ?', (r_date,)
            )

          save_df = df.copy()
          save_df.to_sql('portfolio', conn, if_exists='append', index=False)

          new_accs = save_df[['account_num', 'broker']].drop_duplicates()
          for _, acc_row in new_accs.iterrows():
            acc_num = str(acc_row['account_num']).strip()
            broker_name = (
                str(acc_row['broker']).strip()
                if pd.notna(acc_row['broker'])
                else ''
            )
            c.execute(
                """
                            INSERT INTO initial_principal (account_num, broker, initial_amount)
                            VALUES (?, ?, 0.0)
                            ON CONFLICT(account_num) DO NOTHING
                        """,
                (acc_num, broker_name),
            )
            c.execute(
                """
                            INSERT INTO account_alias (account_num, alias)
                            VALUES (?, '')
                            ON CONFLICT(account_num) DO NOTHING
                        """,
                (acc_num,),
            )

          conn.commit()
          conn.close()
          st.success('포트폴리오 데이터가 성공적으로 저장되었습니다!')
          st.rerun()
    except Exception as e:
      st.error(f'파일 처리 중 오류가 발생했습니다: {e}')

# -----------------------------------------------------------------------------
# 메뉴 5: 원금 및 입출금 관리 (기존 기능 유지)
# -----------------------------------------------------------------------------
elif menu == '원금 및 입출금 관리':
  st.header('💰 계좌별 기초 원금 및 입출금 이력 관리')

  tab1, tab2 = st.tabs(['🏛️ 기초 원금 설정', '💸 추가 입출금 이력 등록'])

  with tab1:
    conn = get_connection()
    init_df = pd.read_sql('SELECT * FROM initial_principal', conn)
    conn.close()

    if not init_df.empty:
      init_df['account_num'] = init_df['account_num'].astype(str).str.strip()

    st.subheader('기초 원금 설정 목록')
    st.dataframe(init_df)

    with st.form('init_principal_form'):
      st.write('계좌별 기초 원금 입력/수정')
      acc_num_in = st.text_input('계좌번호')
      broker_in = st.text_input('증권사명')
      init_amt_in = st.number_input('기초 원금 (원)', value=0.0, step=100000.0)

      save_init_p = st.form_submit_button('저장')

    if save_init_p:
      if acc_num_in.strip() != '':
        conn = get_connection()
        c = conn.cursor()
        c.execute(
            """
                    INSERT INTO initial_principal (account_num, broker, initial_amount)
                    VALUES (?, ?, ?)
                    ON CONFLICT(account_num) DO UPDATE SET broker=excluded.broker, initial_amount=excluded.initial_amount
                """,
            (acc_num_in.strip(), broker_in.strip(), init_amt_in),
        )
        conn.commit()
        conn.close()
        st.success('기초 원금이 저장되었습니다!')
        st.rerun()

  with tab2:
    st.subheader('추가 입출금 이력 등록')
    conn = get_connection()
    init_accs = pd.read_sql(
        'SELECT account_num FROM initial_principal', conn
    )['account_num'].astype(str).tolist()
    cf_df = pd.read_sql('SELECT * FROM cash_flow', conn)
    conn.close()

    if not cf_df.empty:
      cf_df['account_num'] = cf_df['account_num'].astype(str).str.strip()

    with st.form('cash_flow_form'):
      trans_date_in = st.date_input('입출금 날짜', date.today())
      acc_num_cf = st.selectbox('계좌번호', options=init_accs if init_accs else [''])
      flow_type_in = st.selectbox('구분', options=['입금', '출금'])
      amount_in = st.number_input('금액 (원)', value=0.0, step=100000.0)
      note_in = st.text_input('비고')

      save_cf = st.form_submit_button('등록')

    if save_cf:
      if acc_num_cf:
        conn = get_connection()
        c = conn.cursor()
        c.execute(
            """
                    INSERT INTO cash_flow (trans_date, account_num, flow_type, amount, note)
                    VALUES (?, ?, ?, ?, ?)
                """,
            (
                trans_date_in.strftime('%Y-%m-%d'),
                str(acc_num_cf).strip(),
                flow_type_in,
                amount_in,
                note_in,
            ),
        )
        conn.commit()
        conn.close()
        st.success('입출금 내역이 등록되었습니다!')
        st.rerun()

    st.write('---')
    st.subheader('📋 등록된 입출금 이력')
    st.dataframe(cf_df)

# -----------------------------------------------------------------------------
# 메뉴 6: 등록 데이터 조회 (기존 기능 유지)
# -----------------------------------------------------------------------------
elif menu == '등록 데이터 조회':
  st.header('🔍 DB 등록 데이터 조회 및 삭제')

  conn = get_connection()
  pf_df = pd.read_sql('SELECT * FROM portfolio', conn)
  init_df = pd.read_sql('SELECT * FROM initial_principal', conn)
  cf_df = pd.read_sql('SELECT * FROM cash_flow', conn)
  alias_df = pd.read_sql('SELECT * FROM account_alias', conn)
  conn.close()

  st.subheader('1. 포트폴리오 데이터 (`portfolio`)')
  st.dataframe(pf_df)

  st.subheader('2. 계좌 별칭 (`account_alias`)')
  st.dataframe(alias_df)

  st.subheader('3. 기초 원금 (`initial_principal`)')
  st.dataframe(init_df)

  st.subheader('4. 입출금 이력 (`cash_flow`)')
  st.dataframe(cf_df)