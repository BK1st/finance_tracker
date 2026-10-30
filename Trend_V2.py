import os
import sqlite3
import io
import re
import unicodedata
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
BJ_YEAR_END_BASELINE_DATE = '2025-12-31'
BJ_PORTFOLIO_ASSUMED_FROM = '2026-07-31'


def init_db():
  conn = sqlite3.connect(DB_FILE)
  c = conn.cursor()

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
            currency TEXT,
            whose TEXT
        )
    ''')

  c.execute('PRAGMA table_info(portfolio)')
  existing_cols = [col[1] for col in c.fetchall()]

  new_cols = {
      'account_name': 'TEXT',
      'purchase_price': 'REAL',
      'eval_price': 'REAL',
      'eval_profit_loss': 'REAL',
      'return_rate': 'REAL',
      'portfolio_weight': 'REAL',
      'whose': 'TEXT'
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

  c.execute('''
        CREATE TABLE IF NOT EXISTS account_year_end_value (
            account_num TEXT PRIMARY KEY,
            broker TEXT,
            baseline_date TEXT NOT NULL,
            baseline_amount REAL NOT NULL
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

  # 종목 분류 마스터 테이블 (백업 시 함께 보존)
  c.execute('''
        CREATE TABLE IF NOT EXISTS item_category (
            ticker TEXT,
            item_name TEXT,
            category1 TEXT,
            category2 TEXT,
            category3 TEXT,
            category4 TEXT,
            updated_at TEXT,
            PRIMARY KEY (ticker, item_name)
        )
    ''')

  conn.commit()
  conn.close()


init_db()


def sync_latest_categories_to_portfolio():
  """DB 내의 가장 최근 record_date 기준으로 모든 종목의 category1~4를 과거 전체 portfolio 및 item_category에 일괄 동기화"""
  conn = get_connection()
  c = conn.cursor()
  try:
    pf_df = pd.read_sql(
        "SELECT record_date, ticker, item_name, category1, category2, category3, category4 FROM portfolio ORDER BY record_date DESC",
        conn,
    )
    if pf_df.empty:
      conn.close()
      return

    cat_master = pd.read_sql("SELECT * FROM item_category", conn)
    master_dict = {}
    for _, row in cat_master.iterrows():
      tk_val = str(row['ticker']).strip() if pd.notna(row['ticker']) and str(row['ticker']).strip().lower() not in ['', 'nan', 'none'] else ''
      name_val = str(row['item_name']).strip() if pd.notna(row['item_name']) and str(row['item_name']).strip().lower() not in ['', 'nan', 'none'] else ''
      master_dict[(tk_val, name_val)] = (
          str(row['category1']).strip() if pd.notna(row['category1']) and str(row['category1']).strip().lower() != 'nan' else '',
          str(row['category2']).strip() if pd.notna(row['category2']) and str(row['category2']).strip().lower() != 'nan' else '',
          str(row['category3']).strip() if pd.notna(row['category3']) and str(row['category3']).strip().lower() != 'nan' else '',
          str(row['category4']).strip() if pd.notna(row['category4']) and str(row['category4']).strip().lower() != 'nan' else '',
      )

    item_cat_map = {}
    for _, row in pf_df.iterrows():
      tk = str(row['ticker']).strip() if pd.notna(row['ticker']) and str(row['ticker']).strip().lower() not in ['', 'nan', 'none'] else ''
      name = str(row['item_name']).strip() if pd.notna(row['item_name']) and str(row['item_name']).strip().lower() not in ['', 'nan', 'none'] else ''
      key = (tk, name)
      if key not in item_cat_map:
        if key in master_dict and any(master_dict[key]):
          item_cat_map[key] = master_dict[key]
        else:
          c1 = str(row['category1']).strip() if pd.notna(row['category1']) and str(row['category1']).strip().lower() != 'nan' else ''
          c2 = str(row['category2']).strip() if pd.notna(row['category2']) and str(row['category2']).strip().lower() != 'nan' else ''
          c3 = str(row['category3']).strip() if pd.notna(row['category3']) and str(row['category3']).strip().lower() != 'nan' else ''
          c4 = str(row['category4']).strip() if pd.notna(row['category4']) and str(row['category4']).strip().lower() != 'nan' else ''
          item_cat_map[key] = (c1, c2, c3, c4)

    for (tk, name), (c1, c2, c3, c4) in item_cat_map.items():
      if tk:
        c.execute(
            "UPDATE portfolio SET category1=?, category2=?, category3=?, category4=? WHERE ticker=?",
            (c1, c2, c3, c4, tk),
        )
      elif name:
        c.execute(
            "UPDATE portfolio SET category1=?, category2=?, category3=?, category4=? WHERE item_name=? AND (ticker IS NULL OR ticker='' OR ticker='nan')",
            (c1, c2, c3, c4, name),
        )
      c.execute(
          """INSERT OR REPLACE INTO item_category (ticker, item_name, category1, category2, category3, category4, updated_at) 
             VALUES (?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))""",
          (tk, name, c1, c2, c3, c4),
      )
    conn.commit()
  except Exception as e:
    print(f"카테고리 자동 동기화 중 오류: {e}")
  finally:
    conn.close()


def save_item_categories(df_to_save):
  """사용자가 웹(data_editor)에서 수정한 종목 분류를 item_category 및 portfolio 전체(과거 모든 일자)에 일괄 반영"""
  conn = get_connection()
  c = conn.cursor()
  try:
    for _, row in df_to_save.iterrows():
      tk = str(row.get('ticker', '')).strip() if pd.notna(row.get('ticker')) and str(row.get('ticker')).strip().lower() not in ['', 'nan', 'none'] else ''
      name = str(row.get('item_name', '')).strip() if pd.notna(row.get('item_name')) and str(row.get('item_name')).strip().lower() not in ['', 'nan', 'none'] else ''
      c1 = str(row.get('category1', '')).strip() if pd.notna(row.get('category1')) and str(row.get('category1')).strip().lower() != 'nan' else ''
      c2 = str(row.get('category2', '')).strip() if pd.notna(row.get('category2')) and str(row.get('category2')).strip().lower() != 'nan' else ''
      c3 = str(row.get('category3', '')).strip() if pd.notna(row.get('category3')) and str(row.get('category3')).strip().lower() != 'nan' else ''
      c4 = str(row.get('category4', '')).strip() if pd.notna(row.get('category4')) and str(row.get('category4')).strip().lower() != 'nan' else ''

      if tk:
        c.execute(
            "UPDATE portfolio SET category1=?, category2=?, category3=?, category4=? WHERE ticker=?",
            (c1, c2, c3, c4, tk),
        )
      elif name:
        c.execute(
            "UPDATE portfolio SET category1=?, category2=?, category3=?, category4=? WHERE item_name=? AND (ticker IS NULL OR ticker='' OR ticker='nan')",
            (c1, c2, c3, c4, name),
        )
      c.execute(
          """INSERT OR REPLACE INTO item_category (ticker, item_name, category1, category2, category3, category4, updated_at) 
             VALUES (?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))""",
          (tk, name, c1, c2, c3, c4),
      )
    conn.commit()
  except Exception as e:
    st.error(f"종목 분류 저장 중 오류: {e}")
  finally:
    conn.close()


def get_all_item_categories():
  """DB에 등록된 모든 보유 종목의 유니크 목록 및 최신 분류 정보를 반환"""
  conn = get_connection()
  pf_df = pd.read_sql(
      "SELECT record_date, ticker, item_name, category1, category2, category3, category4 FROM portfolio ORDER BY record_date DESC",
      conn,
  )
  cat_master = pd.read_sql("SELECT * FROM item_category", conn)
  conn.close()

  if pf_df.empty and cat_master.empty:
    return pd.DataFrame(columns=['ticker', 'item_name', 'category1', 'category2', 'category3', 'category4'])

  master_dict = {}
  for _, row in cat_master.iterrows():
    tk = str(row['ticker']).strip() if pd.notna(row['ticker']) and str(row['ticker']).strip().lower() not in ['', 'nan', 'none'] else ''
    name = str(row['item_name']).strip() if pd.notna(row['item_name']) and str(row['item_name']).strip().lower() not in ['', 'nan', 'none'] else ''
    master_dict[(tk, name)] = {
        'category1': str(row['category1']).strip() if pd.notna(row['category1']) and str(row['category1']).strip().lower() != 'nan' else '',
        'category2': str(row['category2']).strip() if pd.notna(row['category2']) and str(row['category2']).strip().lower() != 'nan' else '',
        'category3': str(row['category3']).strip() if pd.notna(row['category3']) and str(row['category3']).strip().lower() != 'nan' else '',
        'category4': str(row['category4']).strip() if pd.notna(row['category4']) and str(row['category4']).strip().lower() != 'nan' else '',
    }

  items_list = []
  seen = set()
  for _, row in pf_df.iterrows():
    tk = str(row['ticker']).strip() if pd.notna(row['ticker']) and str(row['ticker']).strip().lower() not in ['', 'nan', 'none'] else ''
    name = str(row['item_name']).strip() if pd.notna(row['item_name']) and str(row['item_name']).strip().lower() not in ['', 'nan', 'none'] else ''
    key = (tk, name)
    if key not in seen:
      seen.add(key)
      if key in master_dict and any(master_dict[key].values()):
        c1 = master_dict[key]['category1']
        c2 = master_dict[key]['category2']
        c3 = master_dict[key]['category3']
        c4 = master_dict[key]['category4']
      else:
        c1 = str(row['category1']).strip() if pd.notna(row['category1']) and str(row['category1']).strip().lower() != 'nan' else ''
        c2 = str(row['category2']).strip() if pd.notna(row['category2']) and str(row['category2']).strip().lower() != 'nan' else ''
        c3 = str(row['category3']).strip() if pd.notna(row['category3']) and str(row['category3']).strip().lower() != 'nan' else ''
        c4 = str(row['category4']).strip() if pd.notna(row['category4']) and str(row['category4']).strip().lower() != 'nan' else ''

      items_list.append({
          'ticker': tk,
          'item_name': name,
          'category1': c1,
          'category2': c2,
          'category3': c3,
          'category4': c4,
      })

  for (tk, name), cats in master_dict.items():
    if (tk, name) not in seen:
      items_list.append({
          'ticker': tk,
          'item_name': name,
          'category1': cats['category1'],
          'category2': cats['category2'],
          'category3': cats['category3'],
          'category4': cats['category4'],
      })

  res_df = pd.DataFrame(items_list)
  if not res_df.empty:
    res_df = res_df.sort_values(by=['ticker', 'item_name']).reset_index(drop=True)
  return res_df


# 앱 시작 시 최신 기준 동기화 자동 1회 점검
try:
  sync_latest_categories_to_portfolio()
except Exception:
  pass


def get_connection():
  return sqlite3.connect(DB_FILE)


def get_account_aliases():
  conn = get_connection()
  alias_df = pd.read_sql('SELECT * FROM account_alias', conn)
  conn.close()
  return dict(zip(alias_df['account_num'], alias_df['alias']))


def resolve_account_whose(account_num, whose, broker, alias_map):
  if pd.notna(whose) and str(whose).strip():
    return str(whose).strip()

  alias = alias_map.get(str(account_num), '')
  combined_str = f'{account_num} {alias} {broker}'
  if '소희' in combined_str or 'SH' in combined_str or 'sh' in combined_str:
    return 'SH'
  return 'BJ'


def normalize_account_num(account_num):
  if pd.isna(account_num):
    return ''
  value = unicodedata.normalize('NFKC', str(account_num)).strip()
  if value.endswith('.0') and value[:-2].isdigit():
    value = value[:-2]
  return re.sub(r'[^0-9A-Za-z가-힣]', '', value)


def normalize_cash_flow_type(flow_type):
  if pd.isna(flow_type):
    return ''
  value = unicodedata.normalize('NFKC', str(flow_type))
  return ''.join(
      char for char in value
      if not char.isspace() and unicodedata.category(char) != 'Cf'
  )


def get_display_item_name(item_name, currency, category1='', category4=''):
  """현금성 자산의 경우 통화(USD/KRW)가 명시되지 않았으면 통화명을 부가하여 구분"""
  name_str = str(item_name).strip() if pd.notna(item_name) else '현금'
  curr_norm = normalize_currency(currency)
  is_cash = (
      any(k in name_str for k in ['현금', '예수금', 'CASH', 'Cash', 'cash'])
      or str(category1).strip() in ['현금', 'CASH', '단기자금', '예수금']
      or str(category4).strip() in ['현금', 'CASH', '단기자금', '예수금']
  )
  if is_cash:
    has_usd = any(k in name_str.upper() for k in ['USD', '달러', '$'])
    has_krw = any(k in name_str for k in ['KRW', '원화', '₩', '원'])
    if curr_norm == 'USD' and not has_usd:
      return f'{name_str} (USD)'
    elif curr_norm == 'KRW' and not has_krw:
      return f'{name_str} (KRW)'
  return name_str


def normalize_currency(currency):
  if pd.isna(currency):
    return 'KRW'
  value = unicodedata.normalize('NFKC', str(currency)).strip().upper()
  compact_value = re.sub(r'\s+', '', value)
  if compact_value in {
      'USD', 'US$', '$', 'DOLLAR', 'USDOLLAR',
      'UNITEDSTATESDOLLAR', '달러', '미국달러',
  }:
    return 'USD'
  if compact_value in {'KRW', '₩', '원', '원화', '한국원'}:
    return 'KRW'
  return value


def parse_cash_flow_amount(amount):
  if pd.isna(amount):
    return np.nan
  if isinstance(amount, (int, float, np.number)):
    return float(amount)
  value = unicodedata.normalize('NFKC', str(amount)).strip()
  is_negative = value.startswith('(') and value.endswith(')')
  value = re.sub(r'(?i)KRW|[,\s₩원]', '', value)
  if is_negative:
    value = value[1:-1]
  parsed_amount = pd.to_numeric(value, errors='coerce')
  if pd.isna(parsed_amount):
    return np.nan
  return -float(parsed_amount) if is_negative else float(parsed_amount)


def build_bj_account_performance(
    account_df, market_data, cash_flow_df, baseline_amount, target_dates,
    end_date, frequency, query_start_date, summary_mode=False,
    selection_only=False,
):
  query_start = pd.Timestamp(query_start_date)
  cutoff = (
      query_start - pd.Timedelta(days=1)
      if selection_only else pd.Timestamp(BJ_PORTFOLIO_ASSUMED_FROM)
  )
  end_ts = pd.Timestamp(end_date)
  if not selection_only and end_ts < cutoff:
    return {}
  calendar = pd.date_range(cutoff, end_ts, freq='D')

  if summary_mode:
    scheduled_dates = calendar[:0]
  elif frequency == '일간 (주말 제외)':
    scheduled_dates = calendar[calendar.dayofweek < 5]
  elif frequency == '주간 (매주 토요일)':
    scheduled_dates = calendar[calendar.dayofweek == 5]
  elif frequency == '월간 (매달 말일)':
    scheduled_dates = calendar[calendar.is_month_end]
  elif frequency == '연간 (매년 말일)':
    scheduled_dates = calendar[calendar.is_year_end]
  else:
    scheduled_dates = calendar

  calc_dates = set(scheduled_dates)
  calc_dates.update(
      pd.Timestamp(target_date)
      for target_date in target_dates
      if pd.Timestamp(target_date) >= cutoff
  )
  if query_start >= cutoff:
    calc_dates.add(query_start)
    calc_dates.add(query_start - pd.Timedelta(days=1))
  calc_dates.add(cutoff)
  calc_dates = sorted(calc_dates)

  account_df = account_df.copy()
  account_df['record_date'] = pd.to_datetime(account_df['record_date'])
  account_df = account_df.sort_values('record_date')
  account_num = normalize_account_num(account_df['account_num'].iloc[0])
  account_cash_flows = cash_flow_df[
      cash_flow_df['account_num'].map(normalize_account_num) == account_num
  ].copy() if not cash_flow_df.empty else pd.DataFrame()
  if not account_cash_flows.empty:
    account_cash_flows['trans_date'] = pd.to_datetime(
        account_cash_flows['trans_date'], errors='coerce'
    )
    account_cash_flows['_amount_krw'] = account_cash_flows[
        'amount'
    ].map(parse_cash_flow_amount)
    account_cash_flows = account_cash_flows.dropna(
        subset=['trans_date', '_amount_krw']
    )
    account_cash_flows['_flow_type'] = account_cash_flows[
        'flow_type'
    ].map(normalize_cash_flow_type)

  def market_price(ticker, target_date):
    if ticker not in market_data.columns:
      return None
    prices = market_data[ticker].dropna()
    if prices.empty:
      return None
    prices = prices[pd.to_datetime(prices.index) <= target_date]
    if prices.empty:
      return None
    return float(prices.iloc[-1])

  def cash_flows_between(start_date, finish_date):
    if account_cash_flows.empty:
      return pd.DataFrame()
    return account_cash_flows[
        (account_cash_flows['trans_date'] > start_date)
        & (account_cash_flows['trans_date'] <= finish_date)
        & account_cash_flows['_flow_type'].isin(['입금', '출금'])
    ].copy()

  def signed_net_flow(flows):
    if flows.empty:
      return 0.0
    signed_amounts = np.where(
        flows['_flow_type'] == '입금',
        flows['_amount_krw'],
        -flows['_amount_krw'],
    )
    return float(np.sum(signed_amounts))

  def weighted_net_flow(start_date, finish_date):
    flows = cash_flows_between(start_date, finish_date)
    if flows.empty:
      return 0.0
    duration_days = (finish_date - start_date).total_seconds() / 86400
    if duration_days <= 0:
      return 0.0
    weights = (
        (finish_date - flows['trans_date']).dt.total_seconds()
        / (duration_days * 86400)
    ).clip(0, 1)
    signed_amounts = np.where(
        flows['_flow_type'] == '입금',
        flows['_amount_krw'],
        -flows['_amount_krw'],
    )
    return float(np.sum(signed_amounts * weights.to_numpy()))

  def cumulative_net_flow(finish_date):
    return signed_net_flow(
        cash_flows_between(pd.Timestamp(BJ_YEAR_END_BASELINE_DATE), finish_date)
    )

  def adjusted_return_base(opening_value, start_date, finish_date):
    return opening_value + weighted_net_flow(start_date, finish_date)

  fixed_fx_date = max(query_start, cutoff)
  fixed_fx_rate = market_price('KRW=X', fixed_fx_date)
  if fixed_fx_rate is None or fixed_fx_rate <= 0:
    fixed_fx_rate = 1350.0

  performance = {}
  previous_eval = None
  previous_eval_ex_fx = None
  previous_date = None
  cumulative_profit_ex_fx = None
  selection_start_date = max(query_start, cutoff)
  selection_start_eval = None

  for target_date in calc_dates:
    holdings = account_df[account_df['record_date'] <= target_date]
    if holdings.empty:
      earliest_snapshot_date = account_df['record_date'].min()
      holdings = account_df[
          account_df['record_date'] == earliest_snapshot_date
      ]
    else:
      latest_snapshot_date = holdings['record_date'].max()
      holdings = holdings[holdings['record_date'] == latest_snapshot_date]

    fx_rate = market_price('KRW=X', target_date)
    if fx_rate is None or fx_rate <= 0:
      fx_rate = 1350.0

    total_eval = 0.0
    total_eval_ex_fx = 0.0
    for _, holding in holdings.iterrows():
      quantity = pd.to_numeric(holding.get('quantity'), errors='coerce')
      fallback_price = pd.to_numeric(holding.get('current_price'), errors='coerce')
      quantity = float(quantity) if pd.notna(quantity) else 0.0
      fallback_price = float(fallback_price) if pd.notna(fallback_price) else 0.0
      ticker = format_ticker(holding.get('ticker'))
      price = market_price(ticker, target_date) if ticker else None
      if price is None or price <= 0:
        price = fallback_price
      currency = normalize_currency(holding.get('currency'))
      multiplier = fx_rate if currency == 'USD' else 1.0
      multiplier_ex_fx = fixed_fx_rate if currency == 'USD' else 1.0
      total_eval += quantity * price * multiplier
      total_eval_ex_fx += quantity * price * multiplier_ex_fx

    if target_date == selection_start_date:
      selection_start_eval = (
          previous_eval
          if previous_eval is not None and previous_date < selection_start_date
          else total_eval
      )

    cumulative_profit = (
        np.nan
        if selection_only else
        total_eval
        - float(baseline_amount)
        - cumulative_net_flow(target_date)
    )
    selection_cumulative_profit = (
        total_eval
        - selection_start_eval
        - signed_net_flow(cash_flows_between(
            selection_start_date - pd.Timedelta(days=1), target_date
        ))
        if selection_start_eval is not None else np.nan
    )
    selection_return_base = (
        adjusted_return_base(
            selection_start_eval,
            selection_start_date - pd.Timedelta(days=1),
            target_date,
        )
        if selection_start_eval is not None else np.nan
    )

    if target_date == cutoff or previous_eval is None or previous_date is None:
      period_profit = np.nan
      period_base = np.nan
      period_profit_ex_fx = np.nan
      period_base_ex_fx = np.nan
    else:
      period_flows = cash_flows_between(previous_date, target_date)
      period_flow = signed_net_flow(period_flows)
      period_profit = total_eval - previous_eval - period_flow
      period_base = adjusted_return_base(
          previous_eval, previous_date, target_date
      )
      period_profit_ex_fx = (
          total_eval_ex_fx - previous_eval_ex_fx - period_flow
      )
      period_base_ex_fx = adjusted_return_base(
          previous_eval_ex_fx, previous_date, target_date
      )

    cumulative_return_base = (
        np.nan
        if selection_only else
        adjusted_return_base(
            float(baseline_amount),
            pd.Timestamp(BJ_YEAR_END_BASELINE_DATE),
            target_date,
        )
    )

    if target_date == cutoff:
      cumulative_profit_ex_fx = cumulative_profit
    elif target_date > cutoff:
      if cumulative_profit_ex_fx is None:
        raise RuntimeError(
            'BJ 환차손 제외 연 누적 추정치의 2026-07-31 기준값이 없습니다.'
        )
      cumulative_profit_ex_fx += period_profit_ex_fx
    else:
      cumulative_profit_ex_fx = cumulative_profit

    performance[target_date.strftime('%Y-%m-%d')] = {
        'period_profit': period_profit,
        'cumulative_profit': cumulative_profit,
        'period_base': period_base,
        'cumulative_base': cumulative_return_base,
        'selection_cumulative_profit': selection_cumulative_profit,
        'selection_base': selection_return_base,
        'period_profit_ex_fx': period_profit_ex_fx,
        'cumulative_profit_ex_fx': cumulative_profit_ex_fx,
        'period_base_ex_fx': period_base_ex_fx,
        'cumulative_base_ex_fx': cumulative_return_base,
    }
    previous_eval = total_eval
    previous_eval_ex_fx = total_eval_ex_fx
    previous_date = target_date

  return performance


def ensure_bj_selection_metrics(data, cumulative_profit_column, start_date):
  data = data.copy()
  selection_eval_column = (
      '총평가금액_bj_ex_fx'
      if cumulative_profit_column.endswith('_ex_fx')
      and '총평가금액_bj_ex_fx' in data.columns
      else '총평가금액'
  )
  account_rows = data[data['whose'] == 'BJ'].drop_duplicates(
      ['Date', 'account_num']
  ).copy()
  account_rows['_metric_date'] = pd.to_datetime(
      account_rows['Date'], errors='coerce'
  )
  selection_start = max(
      pd.Timestamp(start_date), pd.Timestamp(BJ_PORTFOLIO_ASSUMED_FROM)
  )
  profit_by_key = {}
  base_by_account = {}

  for account_num, rows in account_rows.groupby('account_num', dropna=False):
    rows = rows.sort_values('_metric_date')
    start_rows = rows[rows['_metric_date'] >= selection_start]
    if start_rows.empty:
      continue

    start_row = start_rows.iloc[0]
    start_profit = pd.to_numeric(
        start_row[cumulative_profit_column], errors='coerce'
    )
    start_eval = pd.to_numeric(start_row[selection_eval_column], errors='coerce')
    if pd.isna(start_profit) or pd.isna(start_eval):
      continue
    base_by_account[account_num] = float(start_eval)

    for _, row in rows.iterrows():
      current_profit = pd.to_numeric(
          row[cumulative_profit_column], errors='coerce'
      )
      if pd.notna(current_profit):
        profit_by_key[(row['Date'], account_num)] = float(
            current_profit - start_profit
        )

  data['BJ 선택구간_누적손익'] = [
      profit_by_key.get((row['Date'], row['account_num']), np.nan)
      if row['whose'] == 'BJ' else np.nan
      for _, row in data.iterrows()
  ]
  data['BJ 선택구간_기준액'] = data.apply(
      lambda row: base_by_account.get(row['account_num'], np.nan)
      if row['whose'] == 'BJ' else np.nan,
      axis=1,
  )
  return data


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


def get_summary_periods(as_of_date, market_sessions):
  as_of = pd.Timestamp(as_of_date).normalize()
  year_start = pd.Timestamp(as_of.year, 1, 1)
  current_week_start = as_of - pd.Timedelta(days=as_of.weekday())
  sessions = pd.DatetimeIndex(
      pd.to_datetime(market_sessions)
  ).normalize().unique().sort_values()
  completed_sessions = sessions[sessions < as_of]
  if len(completed_sessions):
    last_trading_day = completed_sessions[-1]
  else:
    last_trading_day = as_of - pd.offsets.BDay(1)

  return [
      ('일간', last_trading_day + pd.Timedelta(days=1), as_of),
      ('주간', current_week_start, as_of),
      ('월간', pd.Timestamp(as_of.year, as_of.month, 1), as_of),
      ('연간', year_start, as_of),
  ]


def get_summary_calculation_dates(
    start_date, end_date, portfolio_dates, market_sessions
):
  """Limit summary valuation points to holding changes, baselines, and the end date."""
  start = pd.Timestamp(start_date).normalize()
  end = pd.Timestamp(end_date).normalize()
  dates = {end}
  if start <= end:
    dates.add(start)
    portfolio_dates = pd.to_datetime(
        pd.Series(portfolio_dates), errors='coerce'
    ).dropna()
    for value in portfolio_dates:
      holding_change = pd.Timestamp(value).normalize()
      if start <= holding_change <= end:
        dates.add(holding_change)
        previous_day = holding_change - pd.Timedelta(days=1)
        if start <= previous_day <= end:
          dates.add(previous_day)
    dates.update(
        period_start - pd.Timedelta(days=1)
        for _, period_start, _ in get_summary_periods(end, market_sessions)
        if start <= period_start - pd.Timedelta(days=1) <= end
    )
  return pd.DatetimeIndex(sorted(dates))


def style_trend_report_table(table):
  numeric_columns = table.select_dtypes(include='number').columns
  column_config = {}
  scale_groups = {}
  group_max_abs = {}
  for column in numeric_columns:
    values = pd.to_numeric(table[column], errors='coerce')
    column_name = str(column)
    if '점유율' in column_name:
      scale_group = 'share'
    elif '수익률' in column_name:
      scale_group = 'return'
    elif '손익' in column_name:
      scale_group = 'profit'
    elif '평가금액' in column_name or '평가 금액' in column_name:
      scale_group = 'evaluation'
    else:
      scale_group = ('column', column)
    scale_groups[column] = scale_group
    max_abs = values.abs().max()
    if pd.notna(max_abs):
      group_max_abs[scale_group] = max(
          group_max_abs.get(scale_group, 0.0),
          float(max_abs),
      )

  styled_table = table.style

  def blend_with_white(hex_color, strength):
    red, green, blue = (
        int(hex_color[index:index + 2], 16)
        for index in (1, 3, 5)
    )
    red = round(255 + (red - 255) * strength)
    green = round(255 + (green - 255) * strength)
    blue = round(255 + (blue - 255) * strength)
    return f'#{red:02x}{green:02x}{blue:02x}'

  def color_value(value, scale_limit, scale_group):
    number = pd.to_numeric(value, errors='coerce')
    if pd.isna(number):
      return ''
    if scale_limit <= 0 or number == 0:
      return ''
    intensity = min(abs(number) / scale_limit, 1.0)
    if scale_group == 'return' or scale_group == 'profit':
      base_color = '#b42318' if number < 0 else '#137333'
    else:
      base_color = '#2563a6'
    strength = 0.12 + intensity * 0.48
    background_color = blend_with_white(base_color, strength)
    return f'background-color: {background_color};'

  for column in numeric_columns:
    column_name = str(column)
    scale_group = scale_groups[column]
    scale_limit = group_max_abs.get(scale_group, 0.0)
    styled_table = styled_table.apply(
        lambda cells, limit=scale_limit, group=scale_group: [
            color_value(value, limit, group) for value in cells
        ],
        subset=[column],
    )
    if '%' in column_name or '수익률' in column_name or '점유율' in column_name:
      number_format = '%.2f%%'
    elif '원' in column_name or '손익' in column_name or '금액' in column_name:
      number_format = '%,.0f'
    else:
      number_format = 'localized'
    column_config[column] = st.column_config.NumberColumn(
        format=number_format,
    )
  return styled_table, column_config


def render_summary_page(
    calc_df,
    benchmark_data,
    as_of_date,
    market_sessions,
):
  period_windows = get_summary_periods(as_of_date, market_sessions)
  summary_data = calc_df.copy()
  summary_data['Date'] = pd.to_datetime(summary_data['Date']).dt.normalize()
  summary_data['whose'] = summary_data['whose'].fillna('미지정').astype(str)
  summary_data['account_type'] = summary_data['account_type'].fillna(
      '미지정'
  ).astype(str)
  summary_data['account_num'] = summary_data['account_num'].fillna(
      '미지정'
  ).astype(str)
  summary_data['category4'] = summary_data['category4'].fillna(
      '미지정'
  ).astype(str)
  summary_data['item_name'] = summary_data['item_name'].fillna(
      '미지정'
  ).astype(str)

  def numeric_column(frame, column):
    if column not in frame.columns:
      return pd.Series(np.nan, index=frame.index, dtype=float)
    return pd.to_numeric(frame[column], errors='coerce')

  st.header('📊 SUMMARY | 기간별 자산·지수 비교')
  st.caption(
      '표의 체크박스로 WHOSE 또는 지수를 선택하면 하위 내용을 확인할 수 있습니다. '
      '일간·주간·월간은 직전 완료 구간, 연간은 전년도 말 기준 YTD입니다. '
      '지수는 수익률 기준 참조값이며 가격 수익률만 표시하고 배당·환율은 제외합니다. '
      '수익률·손익은 0 기준으로 양수는 녹색, 음수는 적색으로 표시하고, '
      '평가금액·점유율은 파란색으로 표시합니다. 색의 농도는 표 안의 같은 지표 단위 '
      '공통 기준이며, 숫자값 기준 정렬이 가능합니다. '
      '현금성 자산의 수익률·평가손익은 표시하지 않습니다.'
  )
  use_ex_fx = st.toggle(
      '🔀 환차손 제외 결과로 보기',
      key='summary_ex_fx',
      help='USD 보유자산을 고정 환율 기준으로 평가합니다.',
  )

  regular_eval = numeric_column(summary_data, '총평가금액')
  ex_fx_eval = numeric_column(summary_data, '총평가금액_ex_fx')
  bj_ex_fx_eval = numeric_column(summary_data, '총평가금액_bj_ex_fx')
  regular_profit = numeric_column(summary_data, '주기별 평가손익')
  ex_fx_profit = numeric_column(summary_data, '주기별 평가손익_ex_fx')
  bj_ex_fx_profit = numeric_column(summary_data, '주기별 평가손익_bj_ex_fx')
  if use_ex_fx:
    summary_data['_summary_eval'] = ex_fx_eval.where(
        summary_data['whose'] != 'BJ', bj_ex_fx_eval
    )
    summary_data['_summary_profit'] = ex_fx_profit.where(
        summary_data['whose'] != 'BJ', bj_ex_fx_profit
    )
  else:
    summary_data['_summary_eval'] = regular_eval
    summary_data['_summary_profit'] = regular_profit
  cash_asset_mask = summary_data['item_name'].str.contains('현금', na=False)
  summary_data.loc[cash_asset_mask, '_summary_profit'] = np.nan
  summary_data['_summary_eval'] = summary_data['_summary_eval'].fillna(0)

  account_daily = (
      summary_data.groupby(
          ['Date', 'account_num'], dropna=False
      ).agg(
          _summary_eval=('_summary_eval', 'sum'),
          _summary_profit=('_summary_profit', lambda values: values.sum(min_count=1)),
          whose=('whose', 'first'),
          account_type=('account_type', 'first'),
      )
      .reset_index()
  )
  account_metric_suffix = '_ex_fx' if use_ex_fx else ''
  account_period_col = f'계좌기준_주기손익{account_metric_suffix}'
  account_ytd_profit_col = f'계좌기준_누적손익{account_metric_suffix}'
  account_ytd_base_col = f'계좌기준_누적기준액{account_metric_suffix}'
  account_metrics = summary_data.drop_duplicates(
      ['Date', 'account_num']
  )[['Date', 'account_num']].copy()
  for target_column, source_column in (
      ('_bj_period_profit', account_period_col),
      ('_bj_ytd_profit', account_ytd_profit_col),
      ('_bj_ytd_base', account_ytd_base_col),
  ):
    account_metrics[target_column] = numeric_column(
        summary_data.drop_duplicates(['Date', 'account_num']),
        source_column,
    ).to_numpy()
  account_metrics['_sh_period_profit'] = numeric_column(
      summary_data.drop_duplicates(['Date', 'account_num']),
      '계좌기준_주기손익',
  ).to_numpy()
  account_daily = account_daily.merge(
      account_metrics, on=['Date', 'account_num'], how='left'
  )
  is_bj = account_daily['whose'] == 'BJ'
  account_daily.loc[is_bj, '_summary_profit'] = account_daily.loc[
      is_bj, '_bj_period_profit'
  ].where(
      account_daily.loc[is_bj, '_bj_period_profit'].notna(),
      account_daily.loc[is_bj, '_summary_profit'],
  )
  is_sh = account_daily['whose'] != 'BJ'
  account_daily.loc[is_sh, '_summary_profit'] = account_daily.loc[
      is_sh, '_sh_period_profit'
  ].where(
      account_daily.loc[is_sh, '_sh_period_profit'].notna(),
      account_daily.loc[is_sh, '_summary_profit'],
  )

  def benchmark_return(label, start_date, end_date):
    prices = benchmark_data.get(label)
    if prices is None or prices.empty:
      return np.nan
    price_series = prices.copy()
    price_series['Date'] = pd.to_datetime(price_series['Date_str']).dt.normalize()
    price_series['Close'] = pd.to_numeric(price_series['Close'], errors='coerce')
    price_series = price_series.dropna(subset=['Close']).sort_values('Date')
    baseline_date = start_date - pd.Timedelta(days=1)
    start_prices = price_series[price_series['Date'] <= baseline_date]
    end_prices = price_series[price_series['Date'] <= end_date]
    if start_prices.empty or end_prices.empty:
      return np.nan
    start_price = float(start_prices.iloc[-1]['Close'])
    end_price = float(end_prices.iloc[-1]['Close'])
    return (end_price / start_price - 1) * 100 if start_price > 0 else np.nan

  layout_columns = st.selectbox(
      '표 Layout 열 수 (조회된 데이터에 적용)',
      options=[1, 2, 3],
      key='summary_layout_columns',
  )

  def build_table(group_column, source, label, include_benchmarks=False):
    if source.empty:
      return pd.DataFrame()
    data = source.copy()
    data[group_column] = data[group_column].fillna('미지정').astype(str)
    data['Date'] = pd.to_datetime(data['Date']).dt.normalize()
    as_of = pd.Timestamp(as_of_date).normalize()
    latest_date = data.loc[data['Date'] <= as_of, 'Date'].max()
    if pd.isna(latest_date):
      return pd.DataFrame()

    latest_rows = data[data['Date'] == latest_date]
    current = latest_rows.groupby(group_column, dropna=False)[
        '_summary_eval'
    ].sum()
    table_rows = {
        str(group): {
            label: str(group),
            '평가금액 (원)': float(value),
        }
        for group, value in current.items()
    }

    for period_label, period_start, period_end in period_windows:
      cutoff = period_start - pd.Timedelta(days=1)
      baseline_rows = data[data['Date'] <= cutoff]
      if baseline_rows.empty:
        continue
      baseline_date = baseline_rows['Date'].max()
      start_values = baseline_rows[
          baseline_rows['Date'] == baseline_date
      ].groupby(group_column, dropna=False)['_summary_eval'].sum()
      interval_rows = data[
          (data['Date'] > baseline_date)
          & (data['Date'] <= period_end)
      ]
      profits = interval_rows.groupby(group_column, dropna=False)[
          '_summary_profit'
      ].sum(min_count=1)

      is_ytd = period_label == '연간'
      if is_ytd and group_column in (
          'whose', 'account_type', 'account_num', '_owner_total'
      ):
        account_end = account_daily[
            account_daily['Date'] <= period_end
        ].sort_values('Date').drop_duplicates('account_num', keep='last')
        bj_end = account_end[account_end['whose'] == 'BJ'].copy()
        if group_column == '_owner_total':
          bj_end['_owner_total'] = 'BJ+SH 전체 합산'
        if not bj_end.empty:
          bj_profit_by_group = bj_end.groupby(group_column)[
              '_bj_ytd_profit'
          ].sum(min_count=1)
          bj_base_by_group = bj_end.groupby(group_column)[
              '_bj_ytd_base'
          ].sum(min_count=1)
          non_bj_rows = interval_rows[interval_rows['whose'] != 'BJ']
          non_bj_profit = non_bj_rows.groupby(
              group_column, dropna=False
          )['_summary_profit'].sum(min_count=1)
          non_bj_start = baseline_rows[
              baseline_rows['whose'] != 'BJ'
          ].groupby(group_column, dropna=False)['_summary_eval'].sum()
          all_groups = (
              set(non_bj_profit.index)
              | set(bj_profit_by_group.index)
              | set(start_values.index)
              | set(table_rows)
          )
          profits = pd.Series({
              group: (
                  non_bj_profit.get(group, 0)
                  + bj_profit_by_group.get(group, 0)
              )
              for group in all_groups
          })
          start_values = pd.Series({
              group: (
                  non_bj_start.get(group, 0)
                  + bj_base_by_group.get(group, 0)
              )
              for group in all_groups
          })

      for group in set(start_values.index) | set(profits.index) | set(table_rows):
        start_value = pd.to_numeric(
            pd.Series([start_values.get(group, 0)]), errors='coerce'
        ).iloc[0]
        profit = pd.to_numeric(
            pd.Series([profits.get(group, np.nan)]), errors='coerce'
        ).iloc[0]
        row = table_rows.setdefault(
            str(group),
            {label: str(group), '평가금액 (원)': np.nan},
        )
        row[f'평가손익 ({period_label}) (원)'] = profit
        row[f'수익률 ({period_label}) (%)'] = (
            profit / start_value * 100
            if pd.notna(profit) and pd.notna(start_value) and start_value > 0
            else np.nan
        )

    benchmark_names = tuple(summary_benchmark_map)
    if include_benchmarks:
      for benchmark_label in benchmark_names:
        row = {
            label: benchmark_label,
            '평가금액 (원)': np.nan,
        }
        for period_label, period_start, period_end in period_windows:
          row[f'평가손익 ({period_label}) (원)'] = np.nan
          row[f'수익률 ({period_label}) (%)'] = benchmark_return(
              benchmark_label, period_start, period_end
          )
        table_rows[benchmark_label] = row

    ordered_groups = sorted(
        (group for group in table_rows if group not in benchmark_names),
        key=str,
    )
    order = (
        list(benchmark_names) + ordered_groups
        if include_benchmarks else ordered_groups
    )
    columns = [label, '평가금액 (원)']
    columns.extend(
        f'수익률 ({period_label}) (%)'
        for period_label, _, _ in period_windows
    )
    columns.extend(
        f'평가손익 ({period_label}) (원)'
        for period_label, _, _ in period_windows
    )
    return pd.DataFrame(
        [table_rows[group] for group in order if group in table_rows]
    ).reindex(columns=columns).reset_index(drop=True)

  def render_clickable_table(table, label, key, clickable_values=None):
    if table.empty:
      st.info('선택 기간의 비교 데이터가 없습니다.')
      return []
    display_table = table.copy()
    return_columns = [
        f'수익률 ({period_label}) (%)'
        for period_label, _, _ in period_windows
    ]
    profit_columns = [
        f'평가손익 ({period_label}) (원)'
        for period_label, _, _ in period_windows
    ]
    cash_rows = (
        display_table[label].astype(str).eq('현금')
        if label == 'Category4'
        else display_table[label].astype(str).str.contains('현금', na=False)
        if label == '보유종목'
        else pd.Series(False, index=display_table.index)
    )
    display_table.loc[cash_rows, return_columns + profit_columns] = np.nan
    styled_table, column_config = style_trend_report_table(display_table)
    st.caption('체크박스로 행을 선택하면 하위 분류를 확인할 수 있습니다.')
    result = st.dataframe(
        styled_table,
        column_config=column_config,
        use_container_width=True,
        hide_index=True,
        on_select='rerun',
        selection_mode='multi-row',
        key=key,
    )
    selected_values = []
    for row_index in result.selection.rows:
      if row_index >= len(table):
        continue
      selected_value = str(table.iloc[row_index][label])
      if clickable_values is None or selected_value in clickable_values:
        selected_values.append(selected_value)
    return selected_values

  run_key = st.session_state.get('summary_calc_id', 'initial')
  root_columns = st.columns(layout_columns)
  whose_table = build_table(
      'whose', account_daily, 'WHOSE', include_benchmarks=True
  )
  owner_total_source = account_daily[
      account_daily['whose'].isin(['BJ', 'SH'])
  ].copy()
  if not owner_total_source.empty:
    owner_total_source['_owner_total'] = 'BJ+SH 전체 합산'
    owner_total_table = build_table(
        '_owner_total', owner_total_source, 'WHOSE'
    )
    whose_table = pd.concat(
        [whose_table, owner_total_table], ignore_index=True
    )
  category4_table = build_table(
      'category4', summary_data, 'Category4', include_benchmarks=True
  )

  with root_columns[0]:
    st.subheader('A. WHOSE > 계좌유형 > 계좌 > 보유종목')
    selectable_whose = set(account_daily['whose'].unique())
    selectable_whose.add('BJ+SH 전체 합산')
    selected_whose = render_clickable_table(
        whose_table,
        'WHOSE',
        f'summary_whose_{run_key}',
        clickable_values=selectable_whose,
    )
    if selected_whose:
      selected_account_owners = (
          {'BJ', 'SH'}
          if 'BJ+SH 전체 합산' in selected_whose
          else set(selected_whose)
      )
      st.markdown(f"#### {', '.join(selected_whose)} > 계좌유형별")
      whose_accounts = account_daily[
          account_daily['whose'].isin(selected_account_owners)
      ]
      account_type_table = build_table(
          'account_type', whose_accounts, '계좌유형'
      )
      selected_type = render_clickable_table(
          account_type_table,
          '계좌유형',
          f'summary_type_{run_key}_{"_".join(selected_whose)}',
      )
      if selected_type:
        st.markdown(
            f"#### {', '.join(selected_whose)} > "
            f"{', '.join(selected_type)} > 계좌별"
        )
        typed_accounts = whose_accounts[
            whose_accounts['account_type'].isin(selected_type)
        ]
        account_table = build_table(
            'account_num', typed_accounts, '계좌'
        )
        selected_account = render_clickable_table(
            account_table,
            '계좌',
            f'summary_account_{run_key}_{"_".join(selected_whose)}_'
            f'{"_".join(selected_type)}',
        )
        if selected_account:
          st.markdown(
              f"#### {', '.join(selected_account)} > 보유종목별"
          )
          account_holdings = summary_data[
              summary_data['account_num'].isin(selected_account)
          ]
          holding_table = build_table(
              'item_name', account_holdings, '보유종목'
          )
          render_clickable_table(
              holding_table,
              '보유종목',
              f'summary_holding_{run_key}_{"_".join(selected_account)}',
          )

  if layout_columns > 1:
    category_root = root_columns[1]
  else:
    category_root = st.container()
  with category_root:
    st.subheader('B. Category4 > 보유종목')
    selected_category4 = render_clickable_table(
        category4_table,
        'Category4',
        f'summary_category4_{run_key}',
        clickable_values=set(category4_table['Category4'])
        - set(summary_benchmark_map),
    )
    if selected_category4:
      st.markdown(
          f"#### {', '.join(selected_category4)} > 보유종목별"
      )
      category_holdings = summary_data[
          summary_data['category4'].isin(selected_category4)
      ]
      holding_table = build_table(
          'item_name', category_holdings, '보유종목'
      )
      render_clickable_table(
          holding_table,
          '보유종목',
          f'summary_category_holding_{run_key}_'
          f'{"_".join(selected_category4)}',
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
        'SUMMARY',
        '계좌 별칭 관리',
        '종목 분류 관리',
        '포트폴리오 업로드',
        '원금 및 입출금 관리',
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

summary_benchmark_map = {
    'S&P 500': '^GSPC',
    '나스닥 종합': '^IXIC',
    '나스닥 100': '^NDX',
    '다우 존스': '^DJI',
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
if menu in ('트렌드 리포트', 'SUMMARY'):
  st.header(
      '📊 기간별 자산·지수 비교'
      if menu == 'SUMMARY' else '📊 원금 vs 평가액 트렌드 분석'
  )

  conn = get_connection()
  pf_df = pd.read_sql('SELECT * FROM portfolio', conn)
  init_p_df = pd.read_sql('SELECT * FROM initial_principal', conn)
  year_end_value_df = pd.read_sql('SELECT * FROM account_year_end_value', conn)
  cf_df = pd.read_sql('SELECT * FROM cash_flow', conn)
  conn.close()

  if pf_df.empty:
    st.warning(
        '등록된 포트폴리오가 없습니다. [포트폴리오 업로드] 메뉴에서 엑셀'
        ' 파일을 먼저 등록해 주세요.'
    )
  else:
    cols_to_extract = ['broker', 'account_num', 'account_type']
    if 'whose' in pf_df.columns:
        cols_to_extract.append('whose')
        
    acc_info_df = pf_df[cols_to_extract].drop_duplicates(subset=['account_num']).copy()
    
    whose_mapping = {}
    for _, row in acc_info_df.iterrows():
      acc_num = str(row['account_num'])
      whose_mapping[acc_num] = resolve_account_whose(
          acc_num, row.get('whose'), row.get('broker'), alias_map
      )
            
    acc_info_df['whose'] = acc_info_df['account_num'].map(whose_mapping)
    all_whose_options = sorted(acc_info_df['whose'].dropna().unique().tolist())

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
        'Category 2별',
        'Category 4별',
        '보유항목별',
    ]

    st.subheader('⚙ 분석 조건 설정')

    if 'trend_sel_whose' not in st.session_state:
      st.session_state['trend_sel_whose'] = all_whose_options

    selected_whose = st.multiselect(
        '👤 WHOSE 선택 (우선순위)',
        options=all_whose_options,
        key='trend_sel_whose',
        help='선택한 WHOSE 소유의 계좌만 아래 계좌 선택 목록에 표시됩니다.',
    )

    def normalize_filter_value(value, empty_label):
      if pd.isna(value) or str(value).strip().lower() in ('', 'nan'):
        return empty_label
      return str(value).strip()

    if 'currency' in pf_df.columns:
      pf_df['currency'] = pf_df['currency'].map(normalize_currency)
    item_inventory_df = (
        pf_df.sort_values('record_date', ascending=False)
        .drop_duplicates(subset=['account_num', 'item_name', 'ticker', 'currency'])
        .copy()
    )
    item_inventory_df['account_num'] = item_inventory_df['account_num'].astype(str)
    item_inventory_df['category2_filter'] = item_inventory_df['category2'].map(
        lambda value: normalize_filter_value(value, '미분류')
    )
    item_inventory_df['category4_filter'] = item_inventory_df['category4'].map(
        lambda value: normalize_filter_value(value, '미분류')
    )
    item_inventory_df['whose_filter'] = item_inventory_df['account_num'].map(
        whose_mapping
    )

    whose_sig = tuple(selected_whose)
    whose_items_df = item_inventory_df[
        item_inventory_df['whose_filter'].isin(selected_whose)
    ]
    whose_accounts_df = acc_info_df[
        acc_info_df['whose'].isin(selected_whose)
    ]
    account_type_options = sorted({
        normalize_filter_value(value, '미지정')
        for value in whose_accounts_df['account_type']
    })
    category2_options = sorted(whose_items_df['category2_filter'].unique().tolist())
    category4_options = sorted(whose_items_df['category4_filter'].unique().tolist())

    if st.session_state.get('_trend_filter_whose_sig') != whose_sig:
      st.session_state['trend_sel_account_types'] = account_type_options
      st.session_state['trend_sel_category2'] = category2_options
      st.session_state['trend_sel_category4'] = category4_options
      st.session_state['_trend_filter_whose_sig'] = whose_sig

    filter_options = {
        'trend_sel_account_types': account_type_options,
        'trend_sel_category2': category2_options,
        'trend_sel_category4': category4_options,
    }
    for key, options in filter_options.items():
      selected_values = st.session_state.get(key, options)
      st.session_state[key] = [value for value in selected_values if value in options]

    filter_cols = st.columns(3)
    with filter_cols[0]:
      selected_account_types = st.multiselect(
          '계좌유형 필터',
          options=account_type_options,
          key='trend_sel_account_types',
      )
    with filter_cols[1]:
      selected_category2 = st.multiselect(
          'Category 2 필터',
          options=category2_options,
          key='trend_sel_category2',
      )
    with filter_cols[2]:
      selected_category4 = st.multiselect(
          'Category 4 필터',
          options=category4_options,
          key='trend_sel_category4',
      )

    matching_items_df = whose_items_df[
        whose_items_df['category2_filter'].isin(selected_category2)
        & whose_items_df['category4_filter'].isin(selected_category4)
    ]
    eligible_accounts = set(matching_items_df['account_num'])
    selected_account_type_set = set(selected_account_types)
    filtered_acc_info = whose_accounts_df[
        whose_accounts_df['account_num'].astype(str).isin(eligible_accounts)
        & whose_accounts_df['account_type'].map(
            lambda value: normalize_filter_value(value, '미지정')
        ).isin(selected_account_type_set)
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

    account_filter_sig = (
        whose_sig,
        tuple(selected_account_types),
        tuple(selected_category2),
        tuple(selected_category4),
    )
    if st.session_state.get('_trend_account_filter_sig') != account_filter_sig:
      st.session_state['trend_sel_accs'] = acc_options
      st.session_state['_trend_account_filter_sig'] = account_filter_sig
    else:
      valid_accs = [
          lbl
          for lbl in st.session_state.get('trend_sel_accs', acc_options)
          if lbl in acc_options
      ]
      st.session_state['trend_sel_accs'] = valid_accs

    selected_acc_labels = st.multiselect(
        '조회할 계좌 선택 (WHOSE 연동)',
        options=acc_options,
        key='trend_sel_accs',
    )

    with st.form('trend_control_form'):
      if menu == 'SUMMARY':
        st.info(
            '비교 지수: S&P 500, 나스닥 종합, 나스닥 100, 다우 존스. '
            '지수 수익률은 배당·환율 제외입니다.'
        )
        view_types = st.session_state.get('trend_view_types', all_view_types)
        selected_bm = list(summary_benchmark_map)
        option_freq = '일간 (매일)'
      else:
        saved_benchmarks = st.session_state.get(
            'trend_sel_bm', bm_options
        )
        trend_benchmark_default = [
            benchmark
            for benchmark in saved_benchmarks
            if benchmark in bm_options
        ]
        if not trend_benchmark_default:
          trend_benchmark_default = bm_options
        f_col1, f_col2 = st.columns([3, 3])

        with f_col1:
          view_types = st.multiselect(
              '표시할 트렌드 관점 선택',
              options=all_view_types,
              default=st.session_state.get('trend_view_types', all_view_types),
          )
          selected_bm = st.multiselect(
              '📈 비교 벤치마크 지수 선택 (차트에 함께 표시)',
              options=bm_options,
              default=trend_benchmark_default,
              help='선택한 벤치마크 지수의 수익률 트렌드가 표시됩니다.',
          )

        with f_col2:
          saved_freq = st.session_state.get(
              'trend_freq_str', '일간 (주말 제외)'
          )
          if saved_freq not in freq_options:
            saved_freq = '일간 (주말 제외)'

          option_freq = st.selectbox(
              '트렌드 주기 선택',
              options=freq_options,
              index=freq_options.index(saved_freq),
          )

      d_col1, d_col2 = st.columns(2)
      with d_col1:
        if menu == 'SUMMARY':
          summary_start_default = date(today.year - 1, 12, 31)
          start_default = st.session_state.get(
              'summary_start_date', summary_start_default
          )
          start_label = '조회 시작일 (기본: 전년도 말)'
        else:
          start_default = st.session_state.get(
              'trend_start_date', prev_month_end
          )
          start_label = '조회 시작일'
        start_date = st.date_input(
            start_label,
            start_default,
            key='summary_start_date_input'
            if menu == 'SUMMARY' else 'trend_start_date_input',
        )
      with d_col2:
        if menu == 'SUMMARY':
          end_default = st.session_state.get('summary_end_date', today)
        else:
          end_default = st.session_state.get(
              'trend_end_date',
              max_rec_date if max_rec_date >= min_rec_date else min_rec_date,
          )
        end_date = st.date_input(
            '조회 종료일',
            end_default,
            key='summary_end_date_input'
            if menu == 'SUMMARY' else 'trend_end_date_input',
        )

      st.write('---')
      run_button = st.form_submit_button('🚀 데이터 계산 실행 (Run)')

    if run_button:
      st.session_state['trend_view_types'] = view_types
      if menu == 'SUMMARY':
        st.session_state['summary_sel_bm'] = selected_bm
        st.session_state['summary_start_date'] = start_date
        st.session_state['summary_end_date'] = end_date
        st.session_state['summary_calc_id'] = datetime.now().strftime(
            '%Y%m%d%H%M%S%f'
        )
      else:
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

      if menu == 'SUMMARY':
        target_dates = pd.DatetimeIndex([pd.Timestamp(end_date)])
      elif option_freq == '일간 (매일)':
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
      if menu == 'SUMMARY':
        fetch_tickers.extend(summary_benchmark_map.values())
      selected_bj_accounts = [
          account for account in selected_accounts
          if whose_mapping.get(account) == 'BJ'
      ]
      if selected_bj_accounts:
        diagnostics = cf_df.copy()
        if not diagnostics.empty:
          diagnostics['_account_key'] = diagnostics['account_num'].map(
              normalize_account_num
          )
          diagnostics['_flow_type'] = diagnostics['flow_type'].map(
              normalize_cash_flow_type
          )
          diagnostics['_trans_date'] = pd.to_datetime(
              diagnostics['trans_date'], errors='coerce'
          )
          diagnostics['_amount_krw'] = diagnostics['amount'].map(
              parse_cash_flow_amount
          )
          selected_account_keys = {
              normalize_account_num(account)
              for account in selected_bj_accounts
          }
          diagnostics = diagnostics[
              diagnostics['_account_key'].isin(selected_account_keys)
              & (diagnostics['_trans_date'] > pd.Timestamp(BJ_YEAR_END_BASELINE_DATE))
              & (diagnostics['_trans_date'] <= pd.Timestamp(end_date))
          ]
          valid_flows = diagnostics.dropna(
              subset=['_trans_date', '_amount_krw']
          )
          deposits = valid_flows.loc[
              valid_flows['_flow_type'] == '입금', '_amount_krw'
          ].sum()
          withdrawals = valid_flows.loc[
              valid_flows['_flow_type'] == '출금', '_amount_krw'
          ].sum()
          recognized_flow_count = valid_flows['_flow_type'].isin(
              ['입금', '출금']
          ).sum()
          ignored_flow_count = len(diagnostics) - recognized_flow_count
        else:
          deposits = withdrawals = 0.0
          recognized_flow_count = ignored_flow_count = 0
        st.info(
            'BJ 현금흐름 진단 (KRW): '
            f'{recognized_flow_count}건 매칭, '
            f'입금 {deposits:,.0f}원, 출금 {withdrawals:,.0f}원, '
            f'순입금 {deposits - withdrawals:,.0f}원. '
            '미매칭/금액 오류/알 수 없는 유형은 '
            f'{ignored_flow_count}건입니다. '
            '입출금 금액은 원화로 처리되며, USD 보유자산만 환율을 적용합니다.'
        )
      saved_baselines = dict(zip(
          year_end_value_df['account_num'].astype(str),
          year_end_value_df['baseline_amount'],
      ))
      missing_bj_baselines = [
          account for account in selected_bj_accounts
          if account not in saved_baselines or saved_baselines[account] <= 0
      ]
      if missing_bj_baselines:
        st.warning(
            '다음 BJ 계좌의 2025년 말 평가금액이 미등록입니다. '
            '전체·계좌·증권사·계좌유형별 기준 손익은 해당 BJ 계좌를 제외하고 '
            '계산됩니다. 원금 및 입출금 관리에서 기준액을 입력해 주세요: '
            + ', '.join(missing_bj_baselines)
        )
      if selected_bj_accounts and pd.Timestamp(end_date) >= pd.Timestamp(BJ_PORTFOLIO_ASSUMED_FROM):
        st.info(
            'BJ 계좌: 2025-12-31 기준액으로 올해 누적손익을 계산하고, '
            '2026-07-31 이전은 주기별 손익을 미관측으로 처리합니다. '
            'Category 2 손익은 2026-07-31 이후만 표시하며 연말~7월 구간은 '
            'Category 2에 배분하지 않습니다. Category 4·보유 항목별과 SH 계좌는 기존 방식입니다.'
        )

      with st.spinner('최신 시세를 수집하고 트렌드를 계산 중입니다...'):
        s_str = (start_date - pd.Timedelta(days=10)).strftime('%Y-%m-%d')
        if (
            selected_bj_accounts
            and pd.Timestamp(end_date) >= pd.Timestamp(BJ_PORTFOLIO_ASSUMED_FROM)
        ):
          bj_history_start = (
              pd.Timestamp(BJ_PORTFOLIO_ASSUMED_FROM) - pd.Timedelta(days=10)
          ).strftime('%Y-%m-%d')
          s_str = min(s_str, bj_history_start)
        e_str = (pd.to_datetime(end_date) + pd.Timedelta(days=2)).strftime(
            '%Y-%m-%d'
        )
        market_data = fetch_market_data(
            fetch_tickers, s_str, e_str, force_refresh=True
        )

      if menu == 'SUMMARY':
        target_dates = get_summary_calculation_dates(
            start_date,
            end_date,
            filtered_pf_df['record_date'],
            market_data.index,
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

      bj_fixed_fx_date = max(
          pd.Timestamp(start_date),
          pd.Timestamp(BJ_PORTFOLIO_ASSUMED_FROM),
      )
      bj_fixed_fx_rate = None
      if 'KRW=X' in market_data.columns and not market_data.empty:
        bj_fixed_fx_rate = market_data['KRW=X'].asof(
            bj_fixed_fx_date
        )
      if (
          bj_fixed_fx_rate is None
          or pd.isna(bj_fixed_fx_rate)
          or bj_fixed_fx_rate <= 0
      ):
        bj_fixed_fx_rate = usd_krw_first

      base_records = []
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

        whose_val = whose_mapping.get(acc, 'BJ')

        init_val = (
            init_p_df[init_p_df['account_num'].astype(str) == acc][
                'initial_amount'
            ].sum()
            if not init_p_df.empty
            else 0
        )

        item_prev_prices = {}
        item_prev_fx = {}
        item_cum_pl = {}
        item_cum_pl_ex = {}

        account_pf_all = filtered_pf_df[filtered_pf_df['account_num'].astype(str) == acc].copy()
        account_baseline = year_end_value_df[
            year_end_value_df['account_num'].astype(str) == acc
        ]
        account_performance = {}
        if whose_val == 'BJ' and not account_baseline.empty:
          baseline_row = account_baseline.iloc[0]
          account_performance = build_bj_account_performance(
              account_pf_all,
              market_data,
              cf_df,
              baseline_row['baseline_amount'],
              target_dates,
              end_date,
              option_freq,
              start_date,
              summary_mode=(menu == 'SUMMARY'),
          )
        elif whose_val != 'BJ':
          account_performance = build_bj_account_performance(
              account_pf_all,
              market_data,
              cf_df,
              0,
              target_dates,
              end_date,
              option_freq,
              start_date,
              summary_mode=(menu == 'SUMMARY'),
              selection_only=True,
          )

        account_pf_all_sorted = account_pf_all.sort_values('record_date', ascending=False)
        if 'currency' in account_pf_all_sorted.columns:
          account_pf_all_sorted['currency'] = account_pf_all_sorted['currency'].map(normalize_currency)
        unique_items_df = account_pf_all_sorted.drop_duplicates(subset=['item_name', 'ticker', 'currency'])
        all_account_items = unique_items_df[['item_name', 'ticker', 'category1', 'category2', 'category3', 'category4', 'currency']].to_dict('records')

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

          if not cf_df.empty:
            acc_cf = cf_df[
                cf_df['account_num'].map(normalize_account_num)
                == normalize_account_num(acc)
            ].copy()
            acc_cf['_flow_type'] = acc_cf['flow_type'].map(
                normalize_cash_flow_type
            )
            acc_cf['_trans_date'] = pd.to_datetime(
                acc_cf['trans_date'], errors='coerce'
            )
            acc_cf['_amount_krw'] = acc_cf['amount'].map(
                parse_cash_flow_amount
            )
            acc_cf = acc_cf[
                (acc_cf['_trans_date'] <= pd.Timestamp(t_str))
                & acc_cf['_amount_krw'].notna()
            ]
            in_flow = acc_cf.loc[
                acc_cf['_flow_type'] == '입금', '_amount_krw'
            ].sum()
            out_flow = acc_cf.loc[
                acc_cf['_flow_type'] == '출금', '_amount_krw'
            ].sum()
          else:
            in_flow, out_flow = 0, 0
          principal = init_val + in_flow - out_flow

          acc_pf = account_pf_all[account_pf_all['record_date'] <= t_str]
          if acc_pf.empty:
            earliest_snapshot_date = account_pf_all['record_date'].min()
            acc_pf = account_pf_all[
                account_pf_all['record_date'] == earliest_snapshot_date
            ]

          current_pf = pd.DataFrame()
          if not acc_pf.empty:
            latest_date = acc_pf['record_date'].max()
            current_pf = acc_pf[acc_pf['record_date'] == latest_date]

          total_acc_eval = 0
          total_acc_eval_ex_fx = 0
          item_eval_list = []

          current_pf_dict = {}
          for _, row in current_pf.iterrows():
            row_curr = normalize_currency(row.get('currency'))
            key = (str(row.get('item_name')), str(row.get('ticker')), row_curr)
            current_pf_dict[key] = row

          for item_info in all_account_items:
            iname = item_info['item_name']
            tk = item_info['ticker']
            cat2 = normalize_filter_value(item_info.get('category2'), '미분류')
            cat4 = normalize_filter_value(item_info.get('category4'), '미분류')
            curr = normalize_currency(item_info['currency'])
            key = (str(iname), str(tk), curr)
            display_iname = get_display_item_name(iname, curr, item_info.get('category1'), cat4)
            item_bj_ex_fx_rate = (
                bj_fixed_fx_rate
                if whose_val == 'BJ'
                else usd_krw_first
            )

            if key in current_pf_dict:
              row = current_pf_dict[key]
              qty = row.get('quantity', 0) if pd.notna(row.get('quantity')) else 0
              base_price = row.get('current_price', 0) if pd.notna(row.get('current_price')) else 0
            else:
              qty = 0
              base_price = 0

            fmt_tk = format_ticker(tk)
            price = 0
            if fmt_tk is None or pd.isna(fmt_tk):
              price = base_price if base_price > 0 else 0
            else:
              if fmt_tk in market_data.columns and not market_data.empty:
                if pd.to_datetime(t_str) in market_data.index:
                  price = market_data.loc[pd.to_datetime(t_str), fmt_tk]
                else:
                  price = market_data[fmt_tk].asof(pd.to_datetime(t_str))
                if (pd.isna(price) or price == 0) and not market_data[fmt_tk].dropna().empty:
                  price = float(market_data[fmt_tk].dropna().iloc[0])
              if pd.isna(price) or price == 0:
                price = base_price

            prev_p = item_prev_prices.get(key, price)
            prev_fx = item_prev_fx.get(key, usd_krw)
            price_diff = price - prev_p if prev_p is not None else 0
            fx_diff = usd_krw - prev_fx if prev_fx is not None else 0

            if curr == 'USD':
              period_pl = qty * (price_diff * usd_krw + prev_p * fx_diff)
            else:
              period_pl = price_diff * qty * 1.0

            period_pl_ex = price_diff * qty * usd_krw_first if curr == 'USD' else price_diff * qty * 1.0
            period_pl_bj_ex_fx = (
                price_diff * qty * item_bj_ex_fx_rate
                if curr == 'USD' else price_diff * qty * 1.0
            )

            cum_pl = item_cum_pl.get(key, 0.0) + period_pl
            cum_pl_ex = item_cum_pl_ex.get(key, 0.0) + period_pl_ex

            item_prev_prices[key] = price
            item_prev_fx[key] = usd_krw
            item_cum_pl[key] = cum_pl
            item_cum_pl_ex[key] = cum_pl_ex

            item_eval = qty * price * usd_krw if curr == 'USD' else qty * price
            item_eval_ex = qty * price * usd_krw_first if curr == 'USD' else qty * price
            item_eval_bj_ex_fx = (
                qty * price * item_bj_ex_fx_rate
                if curr == 'USD' else qty * price
            )

            item_return = ((price - prev_p) / prev_p * 100) if (prev_p and prev_p > 0) else 0.0

            total_acc_eval += item_eval
            total_acc_eval_ex_fx += item_eval_ex

            if qty > 0 or abs(cum_pl) > 1e-5:
              item_eval_list.append((
                  display_iname, cat2, cat4, item_eval, item_eval_ex,
                  item_eval_bj_ex_fx, cum_pl, cum_pl_ex, period_pl,
                  period_pl_bj_ex_fx, item_return, qty,
              ))

          item_count = len(item_eval_list)
          item_eval_list = [
              item
              for item in item_eval_list
              if item[1] in selected_category2 and item[2] in selected_category4
          ]
          for (
              item_name, cat2, cat4, item_eval, item_eval_ex,
              item_eval_bj_ex_fx, cum_pl, cum_pl_ex, period_pl,
              period_pl_bj_ex_fx, item_ret, qty,
          ) in item_eval_list:
            if total_acc_eval > 0:
              ratio = item_eval / total_acc_eval
            else:
              ratio = 1.0 / item_count if item_count > 0 else 0

            if total_acc_eval_ex_fx > 0:
              ratio_ex = item_eval_ex / total_acc_eval_ex_fx
            else:
              ratio_ex = 1.0 / item_count if item_count > 0 else 0

            item_principal = principal * ratio
            item_principal_ex = principal * ratio_ex
            account_metrics = account_performance.get(t_str, {})

            base_records.append({
                'Date': t_str,
                'account_num': acc_label,
                'broker': broker_name,
                'account_type': acc_type,
                'category2': cat2,
                'category4': cat4,
                'item_name': item_name,
                'whose': whose_val,
                '원금': item_principal,
                '평가손익': cum_pl,
                '총평가금액': item_eval,
                '주기별 평가손익': period_pl,
                '주기별 평가손익_ex_fx': period_pl_ex,
                '주기별 평가손익_bj_ex_fx': period_pl_bj_ex_fx,
                '주기별 수익률': item_ret,
                '원금_ex_fx': item_principal_ex,
                '평가손익_ex_fx': cum_pl_ex,
                '총평가금액_ex_fx': item_eval_ex,
                '총평가금액_bj_ex_fx': item_eval_bj_ex_fx,
                '계좌기준_주기손익': account_metrics.get('period_profit', np.nan),
                '계좌기준_누적손익': account_metrics.get('cumulative_profit', np.nan),
                '계좌기준_주기기준액': account_metrics.get('period_base', np.nan),
                '계좌기준_누적기준액': account_metrics.get('cumulative_base', np.nan),
                'BJ 선택구간_누적손익': account_metrics.get(
                    'selection_cumulative_profit', np.nan
                ),
                'BJ 선택구간_기준액': account_metrics.get(
                    'selection_base', np.nan
                ),
                '계좌기준_주기손익_ex_fx': account_metrics.get(
                    'period_profit_ex_fx', np.nan
                ),
                '계좌기준_누적손익_ex_fx': account_metrics.get(
                    'cumulative_profit_ex_fx', np.nan
                ),
                '계좌기준_주기기준액_ex_fx': account_metrics.get(
                    'period_base_ex_fx', np.nan
                ),
                '계좌기준_누적기준액_ex_fx': account_metrics.get(
                    'cumulative_base_ex_fx', np.nan
                ),
            })

      bm_calc_dict = {}
      for bm_label in selected_bm:
        tk = (
            summary_benchmark_map[bm_label]
            if menu == 'SUMMARY'
            else bm_ticker_map[bm_label]
        )
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

      result_prefix = 'summary' if menu == 'SUMMARY' else 'trend'
      st.session_state[f'{result_prefix}_calc_df'] = pd.DataFrame(base_records)
      st.session_state[f'{result_prefix}_bm_calc'] = bm_calc_dict
      st.session_state[f'{result_prefix}_market_sessions'] = [
          pd.Timestamp(session).strftime('%Y-%m-%d')
          for session in market_data.index
      ]

    if menu == 'SUMMARY':
      if (
          'summary_calc_df' not in st.session_state
          or st.session_state['summary_calc_df'].empty
      ):
        st.info('계좌와 비교 기준을 선택한 뒤 데이터 계산 실행을 눌러주세요.')
      else:
        render_summary_page(
            st.session_state['summary_calc_df'],
            st.session_state.get('summary_bm_calc', {}),
            st.session_state.get('summary_end_date', today),
            st.session_state.get('summary_market_sessions', []),
        )
      st.stop()

    if (
        'trend_calc_df' in st.session_state
        and not st.session_state['trend_calc_df'].empty
    ):
      calc_df = st.session_state['trend_calc_df']
      bm_calc_dict = st.session_state.get('trend_bm_calc', {})
      active_views = st.session_state.get('trend_view_types', all_view_types)

      st.write('---')
      st.subheader('🖥️ 화면 디스플레이 설정 (실시간 반영)')

      disp_col1, disp_col2 = st.columns([2.5, 2.5])

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

      def on_global_legend_change():
        new_val = st.session_state.get('live_legend_pos')
        if new_val:
          for k in list(st.session_state.keys()):
            if k.startswith('pos_'):
              st.session_state[k] = new_val
          st.session_state['_prev_live_legend_pos'] = new_val

      if '_prev_live_legend_pos' not in st.session_state:
        st.session_state['_prev_live_legend_pos'] = st.session_state.get('live_legend_pos', '하단 배치')
      elif (
          st.session_state.get('live_legend_pos')
          and st.session_state['live_legend_pos'] != st.session_state['_prev_live_legend_pos']
      ):
        on_global_legend_change()

      def get_chart_legend_idx(key_name, current_global_val, options_list):
        if key_name not in st.session_state:
          st.session_state[key_name] = current_global_val
        val = st.session_state.get(key_name, current_global_val)
        return options_list.index(val) if val in options_list else 0

      with disp_col2:
        global_legend_pos = st.selectbox(
            '📌 공통 범례(Legend) 기본 배치',
            options=['하단 배치', '우측 배치', '숨김'],
            index=0,
            key='live_legend_pos',
            on_change=on_global_legend_change,
            help='기본 범례 위치를 선택합니다. 각 차트별로 개별 변경도 가능합니다.',
        )

      num_cols = 1
      if '2열' in layout_setting:
        num_cols = 2
      elif '3열' in layout_setting:
        num_cols = 3

      # -------------------------------------------------------------------------
      # 그룹 요약 차트 (계좌별, 증권사별, 계좌유형별, Category 4별, 보유항목별)
      # -------------------------------------------------------------------------
      def draw_group_summary_charts(raw_df, group_col, prefix):
        st.markdown(f'### 📊 [{prefix}] 전체 종합 비교 분석')
        if (raw_df['whose'] == 'BJ').any():
          st.caption(
              '선택구간 누적손익은 조회 구간 첫 평가액 대비 증감입니다. '
              'BJ 올해 누적손익은 2025년 말 기준액으로 별도 표시합니다. '
              '환차손 제외 연 누적 추정치는 2026-07-31까지 기존 누적손익을 '
              '유지하고, 이후 주기 손익만 고정환율 기준으로 반영합니다. '
              '조회 시작일이 2026-07-31 이전이면 7/31 환율, 이후이면 '
              '조회 시작일 환율을 고정 기준으로 사용합니다.'
          )

        # 계좌 필터 및 Category4 필터 조건 다중 선택 지원 (보유항목별 등)
        if group_col in ['category4', 'item_name']:
          all_accs = sorted(raw_df['account_num'].dropna().unique().tolist())
          all_cat4 = sorted(raw_df['category4'].dropna().unique().tolist())

          col_f1, col_f2 = st.columns(2)
          with col_f1:
            selected_accounts_filter = st.multiselect(
                f'📌 [{prefix}] 계좌 필터 선택 (전체 선택 시 전체 조회)',
                options=all_accs,
                default=all_accs,
                key=f'acc_filter_{prefix}',
                help='특정 계좌를 선택하면 해당 계좌에 대한 정보만 필터링되어 보여집니다.'
            )
          with col_f2:
            selected_cat4_filter = st.multiselect(
                f'📌 [{prefix}] Category 4 필터 선택 (전체 선택 시 전체 조회)',
                options=all_cat4,
                default=all_cat4,
                key=f'cat4_filter_{prefix}',
                help='특정 Category 4 항목을 선택하면 해당되는 항목만 필터링되어 보여집니다.'
            )

          if selected_accounts_filter:
            raw_df = raw_df[raw_df['account_num'].isin(selected_accounts_filter)]
          if selected_cat4_filter:
            raw_df = raw_df[raw_df['category4'].isin(selected_cat4_filter)]

          st.write('---')

        def get_grp_agg(use_ex_fx):
          df_curr = raw_df.copy()
          if use_ex_fx and '원금_ex_fx' in df_curr.columns:
            df_curr['원금'] = df_curr['원금_ex_fx']
            df_curr['평가손익'] = df_curr['평가손익_ex_fx']
            df_curr['총평가금액'] = df_curr['총평가금액_ex_fx']
            if (
                group_col in ['account_num', 'broker', 'account_type', 'category2']
                and '총평가금액_bj_ex_fx' in df_curr.columns
            ):
              bj_mask = df_curr['whose'] == 'BJ'
              df_curr.loc[bj_mask, '총평가금액'] = df_curr.loc[
                  bj_mask, '총평가금액_bj_ex_fx'
              ]

          if group_col in ['item_name', 'category4']:
            df_curr = df_curr[~df_curr['item_name'].str.contains('현금', na=False)]

          if group_col == 'item_name':
            grp_agg = (
                df_curr.groupby(['Date', group_col])[
                    ['원금', '평가손익', '총평가금액', '주기별 평가손익', '주기별 수익률']
                ]
                .agg({
                    '원금': 'sum',
                    '평가손익': 'sum',
                    '총평가금액': 'sum',
                    '주기별 평가손익': 'sum',
                    '주기별 수익률': 'mean'
                })
                .reset_index()
            )
          elif group_col == 'category4':
            item_agg = (
                df_curr.groupby(['Date', 'category4', 'item_name'])[
                    ['원금', '평가손익', '총평가금액', '주기별 평가손익', '주기별 수익률']
                ]
                .sum()
                .reset_index()
            )
            grp_agg = (
                item_agg.groupby(['Date', group_col])[
                    ['원금', '평가손익', '총평가금액', '주기별 평가손익', '주기별 수익률']
                ]
                .agg({
                    '원금': 'sum',
                    '평가손익': 'sum',
                    '총평가금액': 'sum',
                    '주기별 평가손익': 'sum',
                    '주기별 수익률': 'mean'
                })
                .reset_index()
            )
          else:
            grp_agg = (
                df_curr.groupby(['Date', group_col])[
                    ['원금', '평가손익', '총평가금액']
                ]
                .sum()
                .reset_index()
            )
            grp_agg['주기별 평가손익'] = grp_agg.groupby(group_col)['총평가금액'].diff()
            
            first_eval_val = grp_agg.groupby(group_col)['총평가금액'].transform('first')
            grp_agg['선택구간 누적손익'] = grp_agg['총평가금액'] - first_eval_val

            grp_agg['prev_eval'] = grp_agg.groupby(group_col)['총평가금액'].shift(1)
            grp_agg['prev_principal'] = grp_agg.groupby(group_col)['원금'].shift(1)
            grp_agg['period_cash_flow'] = grp_agg['원금'] - grp_agg['prev_principal']
            grp_agg['period_start_base'] = grp_agg['prev_eval'].fillna(grp_agg['총평가금액']) + grp_agg['period_cash_flow'].fillna(0)
            grp_agg['주기별 수익률'] = np.where(
                grp_agg['period_start_base'] > 0,
                (grp_agg['주기별 평가손익'] / grp_agg['period_start_base']) * 100,
                0
            )

          grp_agg['dt_temp'] = pd.to_datetime(grp_agg['Date'])
          grp_agg = grp_agg.sort_values([group_col, 'dt_temp'], ascending=True).reset_index(drop=True)
          grp_agg['Chart_Date'] = grp_agg['dt_temp'].dt.strftime('%Y-%m-%d')
          grp_agg.drop(columns=['dt_temp'], inplace=True)

          if group_col in ['item_name', 'category4']:
            grp_agg['선택구간 누적손익'] = grp_agg['평가손익']
            grp_agg['growth_factor'] = 1 + (grp_agg['주기별 수익률'].fillna(0) / 100)
            grp_agg['누적_성장지수'] = grp_agg.groupby(group_col)['growth_factor'].cumprod()
            grp_agg['선택기간 누적 수익률'] = (grp_agg['누적_성장지수'] - 1) * 100
          else:
            grp_agg['growth_factor'] = 1 + (grp_agg['주기별 수익률'].fillna(0) / 100)
            grp_agg['누적_성장지수'] = grp_agg.groupby(group_col)['growth_factor'].cumprod()
            grp_agg['선택기간 누적 수익률'] = (grp_agg['누적_성장지수'] - 1) * 100

          if group_col in ['account_num', 'broker', 'account_type']:
            use_bj_ex_fx = use_ex_fx
            bj_period_col = (
                '계좌기준_주기손익_ex_fx'
                if use_bj_ex_fx else '계좌기준_주기손익'
            )
            bj_cumulative_col = (
                '계좌기준_누적손익_ex_fx'
                if use_bj_ex_fx else '계좌기준_누적손익'
            )
            bj_period_base_col = (
                '계좌기준_주기기준액_ex_fx'
                if use_bj_ex_fx else '계좌기준_주기기준액'
            )
            bj_cumulative_base_col = (
                '계좌기준_누적기준액_ex_fx'
                if use_bj_ex_fx else '계좌기준_누적기준액'
            )
            sh_rows = df_curr[df_curr['whose'] != 'BJ']
            sh_agg = (
                sh_rows.groupby(['Date', group_col])[
                    ['총평가금액', '원금']
                ]
                .sum()
                .reset_index()
                .sort_values([group_col, 'Date'])
            )
            sh_agg['sh_prev_eval'] = sh_agg.groupby(group_col)[
                '총평가금액'
            ].shift(1)
            sh_agg['sh_prev_principal'] = sh_agg.groupby(group_col)[
                '원금'
            ].shift(1)
            sh_agg['sh_period_cash_flow'] = (
                sh_agg['원금'] - sh_agg['sh_prev_principal']
            ).fillna(0)
            sh_agg['sh_period_profit'] = (
                sh_agg['총평가금액']
                - sh_agg['sh_prev_eval'].fillna(sh_agg['총평가금액'])
                - sh_agg['sh_period_cash_flow']
            )
            sh_agg['sh_cumulative_profit'] = (
                sh_agg['총평가금액']
                - sh_agg.groupby(group_col)['총평가금액'].transform('first')
                - (
                    sh_agg['원금']
                    - sh_agg.groupby(group_col)['원금'].transform('first')
                )
            )
            sh_agg['sh_start_value'] = sh_agg.groupby(group_col)[
                '총평가금액'
            ].transform('first')
            sh_agg['sh_period_base'] = (
                sh_agg['sh_prev_eval'].fillna(sh_agg['총평가금액'])
                + sh_agg['sh_period_cash_flow']
            )

            df_curr = ensure_bj_selection_metrics(
                df_curr, bj_cumulative_col, start_date
            )
            bj_rows = df_curr[
                (df_curr['whose'] == 'BJ')
                & df_curr[bj_cumulative_col].notna()
            ].drop_duplicates(['Date', 'account_num'])
            bj_agg = (
                bj_rows.groupby(['Date', group_col])[
                    [bj_period_col, bj_cumulative_col, bj_cumulative_base_col]
                ]
                .agg({
                    bj_period_col: lambda values: values.sum(min_count=1),
                    bj_cumulative_col: lambda values: values.sum(min_count=1),
                    bj_cumulative_base_col: lambda values: values.sum(min_count=1),
                })
                .reset_index()
                .rename(columns={
                    bj_period_col: 'bj_period_profit',
                    bj_cumulative_col: 'bj_cumulative_profit',
                    bj_cumulative_base_col: 'bj_start_value',
                })
            )
            bj_selection_metrics = (
                bj_rows.groupby(['Date', group_col])[
                    ['BJ 선택구간_누적손익', 'BJ 선택구간_기준액']
                ]
                .agg(lambda values: values.sum(min_count=1))
                .reset_index()
                .rename(columns={
                    'BJ 선택구간_누적손익': 'bj_selection_cumulative_profit',
                    'BJ 선택구간_기준액': 'bj_selection_base',
                })
            )
            bj_agg = bj_agg.merge(
                bj_selection_metrics[
                    [
                        'Date',
                        group_col,
                        'bj_selection_cumulative_profit',
                        'bj_selection_base',
                    ]
                ],
                on=['Date', group_col],
                how='left',
            )
            bj_period_bases = (
                bj_rows.groupby(['Date', group_col])[bj_period_base_col]
                .agg(lambda values: values.sum(min_count=1))
                .reset_index()
                .rename(columns={bj_period_base_col: 'bj_period_base'})
            )
            bj_agg = bj_agg.merge(
                bj_period_bases, on=['Date', group_col], how='left'
            )

            combined = sh_agg.merge(
                bj_agg, on=['Date', group_col], how='outer'
            )
            combined['주기별 평가손익'] = combined[
                ['sh_period_profit', 'bj_period_profit']
            ].sum(axis=1, min_count=1)
            use_bj_selection_period = (
                pd.Timestamp(start_date)
                >= pd.Timestamp(BJ_PORTFOLIO_ASSUMED_FROM)
            )
            bj_selected_profit_col = (
                'bj_selection_cumulative_profit'
                if use_bj_selection_period else 'bj_cumulative_profit'
            )
            bj_selected_base_col = (
                'bj_selection_base'
                if use_bj_selection_period else 'bj_start_value'
            )
            combined['선택구간 누적손익'] = combined[
                ['sh_cumulative_profit', bj_selected_profit_col]
            ].sum(axis=1, min_count=1)
            combined['선택구간 기준액'] = (
                combined['sh_start_value'].fillna(0)
                + combined[bj_selected_base_col].fillna(0)
            )
            combined['BJ 올해 누적손익'] = combined['bj_cumulative_profit']
            combined['BJ 올해 누적기준액'] = combined['bj_start_value']
            combined['BJ 올해 누적 수익률'] = np.where(
                combined['BJ 올해 누적기준액'] > 0,
                combined['BJ 올해 누적손익']
                / combined['BJ 올해 누적기준액'] * 100,
                np.nan,
            )
            combined['주기기준액'] = combined[
                ['sh_period_base', 'bj_period_base']
            ].sum(axis=1, min_count=1)
            combined['has_bj_baseline'] = combined['bj_cumulative_profit'].notna()

            grp_agg = grp_agg.merge(
                combined[
                    [
                        'Date',
                        group_col,
                        '주기별 평가손익',
                        '선택구간 누적손익',
                        '선택구간 기준액',
                        'BJ 올해 누적손익',
                        'BJ 올해 누적기준액',
                        'BJ 올해 누적 수익률',
                        '주기기준액',
                        'has_bj_baseline',
                    ]
                ],
                on=['Date', group_col],
                how='left',
                suffixes=('', '_corrected'),
            )
            grp_agg['주기별 평가손익'] = grp_agg[
                '주기별 평가손익_corrected'
            ].where(
                grp_agg['주기별 평가손익_corrected'].notna(),
                np.nan,
            )
            grp_agg['선택구간 누적손익'] = grp_agg[
                '선택구간 누적손익_corrected'
            ]

            grp_agg['prev_eval'] = grp_agg.groupby(group_col)[
                '총평가금액'
            ].shift(1)
            grp_agg['prev_principal'] = grp_agg.groupby(group_col)['원금'].shift(1)
            grp_agg['period_cash_flow'] = (
                grp_agg['원금'] - grp_agg['prev_principal']
            )
            grp_agg['period_start_base'] = (
                grp_agg['prev_eval'].fillna(grp_agg['총평가금액'])
                + grp_agg['period_cash_flow'].fillna(0)
            )
            grp_agg['주기별 수익률'] = np.where(
                grp_agg['주기기준액'] > 0,
                grp_agg['주기별 평가손익'] / grp_agg['주기기준액'] * 100,
                np.nan,
            )
            if use_bj_selection_period:
              corrected_growth_factor = 1 + (
                  grp_agg['주기별 수익률'].fillna(0) / 100
              )
              corrected_cumulative_return = (
                  grp_agg.assign(
                      _corrected_growth_factor=corrected_growth_factor
                  )
                  .groupby(group_col)['_corrected_growth_factor']
                  .cumprod()
                  .sub(1)
                  .mul(100)
              )
            else:
              corrected_cumulative_return = np.where(
                  grp_agg['선택구간 기준액'] > 0,
                  grp_agg['선택구간 누적손익']
                  / grp_agg['선택구간 기준액'] * 100,
                  0,
              )
            grp_agg['선택기간 누적 수익률'] = np.where(
                grp_agg['has_bj_baseline'],
                corrected_cumulative_return,
                grp_agg['선택기간 누적 수익률'],
            )
            grp_agg.drop(
                columns=[
                    '주기별 평가손익_corrected',
                    '선택구간 누적손익_corrected',
                ],
                inplace=True,
            )

          elif group_col == 'category2':
            category2_period_col = (
                '주기별 평가손익_bj_ex_fx'
                if use_ex_fx
                else '주기별 평가손익'
            )
            sh_rows = df_curr[df_curr['whose'] != 'BJ']
            sh_agg = (
                sh_rows.groupby(['Date', group_col])[
                    ['총평가금액', '원금']
                ]
                .sum()
                .reset_index()
                .sort_values([group_col, 'Date'])
            )
            sh_agg['sh_period_profit'] = sh_agg.groupby(group_col)[
                '총평가금액'
            ].diff()
            sh_agg['sh_cumulative_profit'] = (
                sh_agg['총평가금액']
                - sh_agg.groupby(group_col)['총평가금액'].transform('first')
            )

            bj_rows = df_curr[
                (df_curr['whose'] == 'BJ')
                & (df_curr['Date'] >= BJ_PORTFOLIO_ASSUMED_FROM)
            ]
            bj_agg = (
                bj_rows.groupby(['Date', group_col])[category2_period_col]
                .sum()
                .reset_index()
                .sort_values([group_col, 'Date'])
            )
            bj_agg['bj_cumulative_profit'] = bj_agg.groupby(group_col)[
                category2_period_col
            ].cumsum()
            bj_agg.loc[
                bj_agg['Date'] == BJ_PORTFOLIO_ASSUMED_FROM,
                category2_period_col,
            ] = np.nan
            bj_agg.rename(
                columns={category2_period_col: 'bj_period_profit'},
                inplace=True,
            )
            combined = sh_agg.merge(
                bj_agg, on=['Date', group_col], how='outer'
            )
            combined['주기별 평가손익'] = combined[
                ['sh_period_profit', 'bj_period_profit']
            ].sum(axis=1, min_count=1)
            combined['선택구간 누적손익'] = combined[
                ['sh_cumulative_profit', 'bj_cumulative_profit']
            ].sum(axis=1, min_count=1)
            grp_agg = grp_agg.merge(
                combined[
                    ['Date', group_col, '주기별 평가손익', '선택구간 누적손익']
                ],
                on=['Date', group_col],
                how='left',
                suffixes=('', '_corrected'),
            )
            grp_agg['주기별 평가손익'] = grp_agg[
                '주기별 평가손익_corrected'
            ]
            grp_agg['선택구간 누적손익'] = grp_agg[
                '선택구간 누적손익_corrected'
            ]
            grp_agg['prev_eval'] = grp_agg.groupby(group_col)[
                '총평가금액'
            ].shift(1)
            grp_agg['주기별 수익률'] = np.where(
                grp_agg['prev_eval'] > 0,
                grp_agg['주기별 평가손익'] / grp_agg['prev_eval'] * 100,
                0,
            )
            grp_agg.drop(
                columns=[
                    '주기별 평가손익_corrected',
                    '선택구간 누적손익_corrected',
                ],
                inplace=True,
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

        st.markdown(f'### 📋 [{prefix}] 항목별 최종 핵심지표 요약 표')
        st.caption(
            '수익률·손익은 0 기준으로 양수는 녹색, 음수는 적색으로 표시합니다. '
            '평가금액·점유율은 파란색으로 표시하고, 같은 지표 단위 안에서 값이 클수록 '
            '배경색이 진해집니다.'
        )
        ex_summary = st.toggle('🔀 환차손 제외 결과로 보기', key=f'ex_summary_{prefix}')
        show_bj_ex_fx_estimate = ex_summary
        bj_year_profit_label = (
            'BJ 올해 누적 평가손익 (7/31 이후 환차손 제외 추정)'
            if show_bj_ex_fx_estimate
            else 'BJ 올해 누적 평가손익'
        )
        bj_year_return_label = (
            'BJ 올해 누적 수익률 (7/31 이후 환차손 제외 추정)'
            if show_bj_ex_fx_estimate
            else 'BJ 올해 누적 수익률'
        )
        
        grp_agg_summary = get_grp_agg(ex_summary)

        summary_table_data = []
        total_eval_sum = 0
        total_pl_sum = 0
        total_return_base_sum = 0
        total_bj_year_base_sum = 0

        for grp in groups:
          sub = grp_agg_summary[grp_agg_summary[group_col] == grp]
          if not sub.empty:
            final_p_loss = sub['선택구간 누적손익'].iloc[-1]
            sub_latest = raw_df[(raw_df[group_col] == grp) & (raw_df['Date'] == latest_date)]
            if sub_latest.empty:
              final_eval = 0.0
            else:
              final_eval = sub['총평가금액'].iloc[-1]
              
            final_ret = sub['선택기간 누적 수익률'].iloc[-1]
            
            if ex_summary and '원금_ex_fx' in raw_df.columns:
              final_principal = sub_latest['원금_ex_fx'].sum() if not sub_latest.empty else 0
            else:
              final_principal = sub_latest['원금'].sum() if not sub_latest.empty else 0
            has_bj_baseline = (
                sub['has_bj_baseline'].iloc[-1]
                if 'has_bj_baseline' in sub.columns else False
            )
            if pd.notna(has_bj_baseline) and bool(has_bj_baseline):
              final_principal = sub['선택구간 기준액'].iloc[-1]

            final_bj_year_profit = (
                sub['BJ 올해 누적손익'].iloc[-1]
                if 'BJ 올해 누적손익' in sub.columns else np.nan
            )
            final_bj_year_return = (
                sub['BJ 올해 누적 수익률'].iloc[-1]
                if 'BJ 올해 누적 수익률' in sub.columns else np.nan
            )
            final_bj_year_base = (
                sub['BJ 올해 누적기준액'].iloc[-1]
                if 'BJ 올해 누적기준액' in sub.columns else np.nan
            )

            total_eval_sum += final_eval
            total_pl_sum += final_p_loss if pd.notna(final_p_loss) else 0
            total_return_base_sum += final_principal
            if pd.notna(final_bj_year_base):
              total_bj_year_base_sum += final_bj_year_base

            summary_table_data.append({
                prefix: grp,
                '선택구간 누적 평가 손익 (원)': final_p_loss,
                '최종 기말 평가 금액 (원)': final_eval,
                '선택구간 누적 수익률 (%)': final_ret,
                bj_year_profit_label: final_bj_year_profit,
                bj_year_return_label: final_bj_year_return,
                '_eval': final_eval,
                '_principal': final_principal,
            })

        total_bj_year_profit = sum(
            row[bj_year_profit_label]
            for row in summary_table_data
            if pd.notna(row[bj_year_profit_label])
        )
        total_bj_year_return = (
            total_bj_year_profit / total_bj_year_base_sum * 100
            if total_bj_year_base_sum > 0 else np.nan
        )

        for row in summary_table_data:
          row['점유율 (%)'] = (row['_eval'] / total_eval_sum * 100) if total_eval_sum > 0 else 0
          del row['_eval']
          del row['_principal']

        total_ret = (
            total_pl_sum / total_return_base_sum * 100
            if total_return_base_sum > 0 else 0
        )
        summary_table_data.append({
            prefix: '전체 합산',
            '선택구간 누적 평가 손익 (원)': total_pl_sum,
            '최종 기말 평가 금액 (원)': total_eval_sum,
            '선택구간 누적 수익률 (%)': total_ret,
            bj_year_profit_label: total_bj_year_profit,
            bj_year_return_label: total_bj_year_return,
            '점유율 (%)': 100.0 if total_eval_sum > 0 else 0.0,
        })

        summary_df = pd.DataFrame(summary_table_data)
        cols = [
            prefix,
            '선택구간 누적 평가 손익 (원)',
            '최종 기말 평가 금액 (원)',
            '점유율 (%)',
            '선택구간 누적 수익률 (%)',
            bj_year_profit_label,
            bj_year_return_label,
        ]
        summary_df = summary_df[[c for c in cols if c in summary_df.columns]]

        summary_table, summary_column_config = style_trend_report_table(
            summary_df
        )
        if group_col == 'category4':
          st.caption('Category 4 행을 선택하면 아래에 해당 보유종목별 내역이 표시됩니다.')
          summary_selection = st.dataframe(
              summary_table,
              column_config=summary_column_config,
              use_container_width=True,
              on_select='rerun',
              selection_mode='single-row',
              key=f'group_summary_selection_{prefix}',
          )
          selected_rows = summary_selection.selection.rows
          if selected_rows and selected_rows[0] < len(summary_df):
            selected_category = summary_df.iloc[selected_rows[0]][prefix]
            if selected_category != '전체 합산':
              st.markdown(f'#### 📦 [{selected_category}] 보유종목별 내역 ({latest_date})')
              holdings_df = latest_df[latest_df['category4'] == selected_category].copy()
              holdings_df = holdings_df[
                  ~holdings_df['item_name'].str.contains('현금', na=False)
              ]

              if ex_summary and '원금_ex_fx' in holdings_df.columns:
                period_return_col = '주기별 수익률'
                holdings_period_df = raw_df[
                    (raw_df['category4'] == selected_category)
                    & ~raw_df['item_name'].str.contains('현금', na=False)
                ].copy()
                holdings_period_df['평가손익'] = holdings_period_df['평가손익_ex_fx']
                holdings_period_df['총평가금액'] = holdings_period_df['총평가금액_ex_fx']
              else:
                period_return_col = '주기별 수익률'
                holdings_period_df = raw_df[
                    (raw_df['category4'] == selected_category)
                    & ~raw_df['item_name'].str.contains('현금', na=False)
                ].copy()

              if holdings_df.empty:
                st.info('선택한 Category 4에 표시할 보유종목이 없습니다.')
              else:
                holdings_period_df = (
                    holdings_period_df.groupby(
                        ['Date', 'item_name'], dropna=False
                    )[
                        [
                            '평가손익',
                            '총평가금액',
                            period_return_col,
                        ]
                    ]
                    .sum()
                    .reset_index()
                )
                holdings_period_df['growth_factor'] = (
                    1 + holdings_period_df[period_return_col].fillna(0) / 100
                )
                holdings_period_df = holdings_period_df.sort_values('Date')
                holdings_period_df['누적_성장지수'] = (
                    holdings_period_df.groupby('item_name', dropna=False)[
                        'growth_factor'
                    ].cumprod()
                )
                holdings_period_df['선택기간 누적 수익률'] = (
                    holdings_period_df['누적_성장지수'] - 1
                ) * 100

                holdings_summary = (
                    holdings_period_df.groupby('item_name', dropna=False).tail(1)
                    .rename(columns={
                        'item_name': '보유종목',
                        '평가손익': '선택구간 누적 평가 손익 (원)',
                        '총평가금액': '최종 기말 평가 금액 (원)',
                        '선택기간 누적 수익률': '선택구간 누적 수익률 (%)',
                    })
                    .copy()
                )
                holdings_summary['점유율 (%)'] = np.where(
                    total_eval_sum > 0,
                    holdings_summary['최종 기말 평가 금액 (원)']
                    / total_eval_sum
                    * 100,
                    0,
                )
                holdings_summary = holdings_summary.sort_values(
                    '최종 기말 평가 금액 (원)', ascending=False
                )
                holdings_summary = holdings_summary[
                    [
                        '보유종목',
                        '선택구간 누적 평가 손익 (원)',
                        '최종 기말 평가 금액 (원)',
                        '점유율 (%)',
                        '선택구간 누적 수익률 (%)',
                    ]
                ]
                holdings_summary_table, holdings_column_config = (
                    style_trend_report_table(holdings_summary)
                )
                st.dataframe(
                    holdings_summary_table,
                    column_config=holdings_column_config,
                    use_container_width=True,
                    hide_index=True,
                )
        else:
          st.dataframe(
              summary_table,
              column_config=summary_column_config,
              use_container_width=True,
          )
        st.write('---')

        st.markdown(f'##### ⚙️ [{prefix}] 종합 차트별 범주 및 환율 옵션 설정')

        cb_c1, cb_c2, cb_c3, cb_c4 = st.columns(4)
        leg_pos_options = ['하단 배치', '우측 배치', '숨김']

        with cb_c1:
          pos_g1 = st.selectbox(
              '선택누적손익',
              leg_pos_options,
              index=get_chart_legend_idx(f'pos_g1_{prefix}', global_legend_pos, leg_pos_options),
              key=f'pos_g1_{prefix}',
          )
          ex_g1 = st.toggle('🔀 환차손제외', key=f'ex_g1_{prefix}')
        with cb_c2:
          pos_g2 = st.selectbox(
              '주기별손익',
              leg_pos_options,
              index=get_chart_legend_idx(f'pos_g2_{prefix}', global_legend_pos, leg_pos_options),
              key=f'pos_g2_{prefix}',
          )
          ex_g2 = st.toggle('🔀 환차손제외', key=f'ex_g2_{prefix}')
        with cb_c3:
          pos_g3 = st.selectbox(
              '주기별수익률',
              leg_pos_options,
              index=get_chart_legend_idx(f'pos_g3_{prefix}', global_legend_pos, leg_pos_options),
              key=f'pos_g3_{prefix}',
          )
          ex_g3 = st.toggle('🔀 환차손제외', key=f'ex_g3_{prefix}')
        with cb_c4:
          pos_g4 = st.selectbox(
              '기간누적수익률',
              leg_pos_options,
              index=get_chart_legend_idx(f'pos_g4_{prefix}', global_legend_pos, leg_pos_options),
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
                    f'🔹 [{prefix}] 선택 기간 주기별 평가손익 Trend (그룹형'
                    f' 세로 막대){suf_g2}'
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
            margin=margin_g2,
            showlegend=show_g2,
            legend=leg_cfg_g2,
        )
        fig_period_p.update_xaxes(
            type='category',
            categoryorder='array',
            categoryarray=date_order_list,
        )
        fig_period_p.update_yaxes(title_text='손익금액 (원)', tickformat=',.0f')

        grp_agg_g3 = get_grp_agg(ex_g3)
        fig_period_ret = go.Figure()
        for grp in groups:
          sub = grp_agg_g3[grp_agg_g3[group_col] == grp].dropna(
              subset=['주기별 수익률']
          )
          fig_period_ret.add_trace(
              go.Bar(
                  x=sub['Chart_Date'],
                  y=sub['주기별 수익률'],
                  name=str(grp),
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
              go.Bar(
                  x=sub_bm['Chart_Date'],
                  y=sub_bm['주기별 수익률'],
                  name=f'📌 {bm_name}',
                  hovertemplate='%{y:.2f}%',
              )
          )

        leg_cfg_g3, show_g3, margin_g3 = build_legend_config(pos_g3)
        suf_g3 = ' (환차손제외)' if ex_g3 else ''
        fig_period_ret.update_layout(
            title=dict(
                text=(
                    f'🔹 [{prefix}] 선택기간 주기별 수익률 Trend (그룹형'
                    f' 세로 막대,'
                    f' 벤치마크 포함){suf_g3}'
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

        if group_col in [
            'account_num',
            'account_type',
            'category2',
            'category4',
        ]:
          st.write('---')
          st.markdown(f'### 📊 [{prefix}] 선택 기준 일자별 총 평가 금액 항목별 분석 (총 3개 개별 분석)')

          c_s_ctrl, _ = st.columns([3, 3])
          with c_s_ctrl:
            stack_layout_opt = st.radio(
                f'🖥️ [{prefix}] 분석 차트 Layout 선택',
                options=['1열 (기존 세로 배치)', '2열 (좌우 2개 분할 배치)'],
                horizontal=True,
                key=f'stack_layout_{prefix}',
            )

          all_filter_items = sorted(raw_df[group_col].dropna().unique().tolist())
          target_group_options = (
              ['보유 항목별', '계좌별', 'whose별', '계좌유형별']
              if group_col == 'category4'
              else ['category4별', '보유 항목별']
          )
          target_group_columns = {
              'category4별': 'category4',
              '보유 항목별': 'item_name',
              '계좌별': 'account_num',
              'whose별': 'whose',
              '계좌유형별': 'account_type',
          }

          # -------------------------------------------------------------------
          # 헬퍼 함수: 개별 차트 생성 및 렌더링 (막대 vs 꺾은선 지원)
          # -------------------------------------------------------------------
          def render_stacked_bar_chart(chart_idx):
            st.markdown(f'##### 📌 차트 #{chart_idx}')
            if '2열' in stack_layout_opt:
              c_sub1, c_sub2 = st.columns([1.2, 0.8])
              with c_sub1:
                selected_filter_items = st.multiselect(
                    f'특정 {prefix} 선택 (#{chart_idx})',
                    options=all_filter_items,
                    default=all_filter_items,
                    key=f'sub_filter_{prefix}_{chart_idx}',
                )
              with c_sub2:
                target_group_opt = st.radio(
                    f'분류 기준 선택 (#{chart_idx})',
                    options=target_group_options,
                    horizontal=True,
                    key=f'sub_group_opt_{prefix}_{chart_idx}',
                )
              
              c_sub3, c_sub4 = st.columns(2)
              with c_sub3:
                chart_type_opt = st.radio(
                    f'차트 유형 선택 (#{chart_idx})',
                    options=['누적 세로 막대', '꺾은선 그래프'],
                    horizontal=True,
                    key=f'sub_chart_type_{prefix}_{chart_idx}',
                )
              with c_sub4:
                pos_sub_stack = st.selectbox(
                    f'📌 범례 배치 (#{chart_idx})',
                    options=leg_pos_options,
                    index=get_chart_legend_idx(
                        f'pos_sub_stack_{prefix}_{chart_idx}',
                        global_legend_pos,
                        leg_pos_options,
                    ),
                    key=f'pos_sub_stack_{prefix}_{chart_idx}',
                )
            else:
              c_sub1, c_sub2, c_sub3, c_sub4 = st.columns([2, 1.8, 1.8, 1.6])
              with c_sub1:
                selected_filter_items = st.multiselect(
                    f'특정 {prefix} 선택 (#{chart_idx})',
                    options=all_filter_items,
                    default=all_filter_items,
                    key=f'sub_filter_{prefix}_{chart_idx}',
                )
              with c_sub2:
                target_group_opt = st.radio(
                    f'분류 기준 선택 (#{chart_idx})',
                    options=target_group_options,
                    horizontal=True,
                    key=f'sub_group_opt_{prefix}_{chart_idx}',
                )
              with c_sub3:
                chart_type_opt = st.radio(
                    f'차트 유형 선택 (#{chart_idx})',
                    options=['누적 세로 막대', '꺾은선 그래프'],
                    horizontal=True,
                    key=f'sub_chart_type_{prefix}_{chart_idx}',
                )
              with c_sub4:
                pos_sub_stack = st.selectbox(
                    f'📌 범례 배치 (#{chart_idx})',
                    options=leg_pos_options,
                    index=get_chart_legend_idx(
                        f'pos_sub_stack_{prefix}_{chart_idx}',
                        global_legend_pos,
                        leg_pos_options,
                    ),
                    key=f'pos_sub_stack_{prefix}_{chart_idx}',
                )

            target_col = target_group_columns[target_group_opt]

            if selected_filter_items:
              sub_raw = raw_df[raw_df[group_col].isin(selected_filter_items)].copy()
              sub_agg = sub_raw.groupby(['Date', target_col])['총평가금액'].sum().reset_index()

              sub_agg['dt_temp'] = pd.to_datetime(sub_agg['Date'])
              sub_agg = sub_agg.sort_values('dt_temp').reset_index(drop=True)
              sub_agg['Chart_Date'] = sub_agg['dt_temp'].dt.strftime('%Y-%m-%d')
              date_order = sorted(sub_agg['Chart_Date'].unique().tolist())

              fig_sub_stack = go.Figure()
              cat_list = sub_agg[target_col].unique().tolist()
              
              if chart_type_opt == '누적 세로 막대':
                for cat in cat_list:
                  c_df = sub_agg[sub_agg[target_col] == cat]
                  fig_sub_stack.add_trace(
                      go.Bar(
                          x=c_df['Chart_Date'],
                          y=c_df['총평가금액'],
                          name=str(cat),
                          hovertemplate='%{y:,.0f} 원'
                      )
                  )
                b_mode = 'stack'
              else:
                for cat in cat_list:
                  c_df = sub_agg[sub_agg[target_col] == cat]
                  fig_sub_stack.add_trace(
                      go.Scatter(
                          x=c_df['Chart_Date'],
                          y=c_df['총평가금액'],
                          name=str(cat),
                          mode='lines+markers',
                          hovertemplate='%{y:,.0f} 원'
                      )
                  )
                b_mode = None

              leg_cfg_sub, show_sub, margin_sub = build_legend_config(pos_sub_stack)
              
              chart_title_type = "누적 막대" if chart_type_opt == '누적 세로 막대' else "꺾은선"
              fig_sub_stack.update_layout(
                  title=dict(
                      text=f'🔹 #{chart_idx} 선택된 {prefix}의 일자별 총 평가 금액 ({target_group_opt} 기준 {chart_title_type})',
                      y=0.95, x=0.01, xanchor='left', yanchor='top', yref='container'
                  ),
                  barmode=b_mode,
                  hovermode='closest',
                  height=500,
                  margin=margin_sub,
                  showlegend=show_sub,
                  legend=leg_cfg_sub
              )
              fig_sub_stack.update_xaxes(type='category', categoryorder='array', categoryarray=date_order)
              fig_sub_stack.update_yaxes(title_text='총 평가금액 (원)', tickformat=',.0f')

              render_resizable_plotly_chart(fig_sub_stack, key=f'fig_sub_stack_{prefix}_{chart_idx}')
            else:
              st.info(f'분석할 {prefix}을 1개 이상 선택해 주세요.')

          # 분석 차트 3개 배치 (1열 vs 2열)
          if '2열' in stack_layout_opt:
            col_sb1, col_sb2 = st.columns(2)
            with col_sb1:
              render_stacked_bar_chart(1)
            with col_sb2:
              render_stacked_bar_chart(2)
            st.write('---')
            col_sb3, _ = st.columns(2)
            with col_sb3:
              render_stacked_bar_chart(3)
          else:
            for idx in range(1, 4):
              render_stacked_bar_chart(idx)
              if idx < 3:
                st.write('---')

      def render_total_whose_charts(raw_df):
        st.markdown('### 📊 [전체 합산 - WHOSE별 분석]')
        if (raw_df['whose'] == 'BJ').any():
          st.caption(
              '선택구간 누적손익은 조회 구간 첫 평가액 대비 증감입니다. '
              'BJ 올해 누적손익은 2025년 말 기준액으로 별도 표시합니다. '
              '환차손 제외 연 누적 추정치는 2026-07-31까지 기존 누적손익을 '
              '유지하고, 이후 주기 손익만 고정환율 기준으로 반영합니다. '
              '조회 시작일이 2026-07-31 이전이면 7/31 환율, 이후이면 '
              '조회 시작일 환율을 고정 기준으로 사용합니다.'
          )

        def get_whose_agg(use_ex_fx):
          df_curr = raw_df.copy()
          use_bj_ex_fx = use_ex_fx
          bj_period_col = (
              '계좌기준_주기손익_ex_fx'
              if use_bj_ex_fx else '계좌기준_주기손익'
          )
          bj_cumulative_col = (
              '계좌기준_누적손익_ex_fx'
              if use_bj_ex_fx else '계좌기준_누적손익'
          )
          bj_period_base_col = (
              '계좌기준_주기기준액_ex_fx'
              if use_bj_ex_fx else '계좌기준_주기기준액'
          )
          bj_cumulative_base_col = (
              '계좌기준_누적기준액_ex_fx'
              if use_bj_ex_fx else '계좌기준_누적기준액'
          )
          if use_ex_fx and '원금_ex_fx' in df_curr.columns:
            df_curr['원금'] = df_curr['원금_ex_fx']
            df_curr['평가손익'] = df_curr['평가손익_ex_fx']
            df_curr['총평가금액'] = df_curr['총평가금액_ex_fx']
            if (
                use_bj_ex_fx
                and '총평가금액_bj_ex_fx' in df_curr.columns
            ):
              bj_mask = df_curr['whose'] == 'BJ'
              df_curr.loc[bj_mask, '총평가금액'] = df_curr.loc[
                  bj_mask, '총평가금액_bj_ex_fx'
              ]

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
          
          first_eval_val = grp_agg.groupby('whose')['총평가금액'].transform('first')
          grp_agg['선택구간 누적손익'] = grp_agg['총평가금액'] - first_eval_val

          # SH 계좌는 계좌 기준 성과 컬럼에 이미 입출금 차감 결과가 있다.
          # 항목별 원본 손익을 WHOSE 단위로 다시 더하면 입금이 손익에 섞이므로,
          # 계좌별 중복을 제거한 조정 성과를 SH 합산에도 사용한다.
          sh_account_rows = df_curr[df_curr['whose'] != 'BJ'].drop_duplicates(
              ['Date', 'account_num']
          )
          sh_metrics = (
              sh_account_rows.groupby(['Date', 'whose'])[
                  [
                      '계좌기준_주기손익',
                      'BJ 선택구간_누적손익',
                      'BJ 선택구간_기준액',
                      '계좌기준_주기기준액',
                  ]
              ]
              .agg(lambda values: values.sum(min_count=1))
              .reset_index()
          )
          grp_agg = grp_agg.merge(
              sh_metrics,
              on=['Date', 'whose'],
              how='left',
              suffixes=('', '_sh_adjusted'),
          )
          sh_mask = grp_agg['whose'] != 'BJ'
          grp_agg.loc[sh_mask, '주기별 평가손익'] = grp_agg.loc[
              sh_mask, '계좌기준_주기손익'
          ].where(
              grp_agg.loc[sh_mask, '계좌기준_주기손익'].notna(),
              grp_agg.loc[sh_mask, '주기별 평가손익'],
          )
          grp_agg.loc[sh_mask, '선택구간 누적손익'] = grp_agg.loc[
              sh_mask, 'BJ 선택구간_누적손익'
          ].where(
              grp_agg.loc[sh_mask, 'BJ 선택구간_누적손익'].notna(),
              grp_agg.loc[sh_mask, '선택구간 누적손익'],
          )
          grp_agg.loc[sh_mask, 'sh_adjusted_period_base'] = grp_agg.loc[
              sh_mask, '계좌기준_주기기준액'
          ]

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
          grp_agg.loc[sh_mask, '주기별 수익률'] = np.where(
              grp_agg.loc[sh_mask, 'sh_adjusted_period_base'] > 0,
              grp_agg.loc[sh_mask, '주기별 평가손익']
              / grp_agg.loc[sh_mask, 'sh_adjusted_period_base'] * 100,
              np.nan,
          )
          
          grp_agg['growth_factor'] = 1 + (grp_agg['주기별 수익률'].fillna(0) / 100)
          grp_agg['누적_성장지수'] = grp_agg.groupby('whose')['growth_factor'].cumprod()
          grp_agg['구간별 누적수익률'] = (grp_agg['누적_성장지수'] - 1) * 100

          df_curr = ensure_bj_selection_metrics(
              df_curr, bj_cumulative_col, start_date
          )
          bj_rows = df_curr[
              (df_curr['whose'] == 'BJ')
              & df_curr[bj_cumulative_col].notna()
          ].drop_duplicates(['Date', 'account_num'])
          bj_agg = (
              bj_rows.groupby('Date')[
                  [bj_period_col, bj_cumulative_col, bj_period_base_col,
                   bj_cumulative_base_col]
                  ]
              .agg({
                  bj_period_col: lambda values: values.sum(min_count=1),
                  bj_cumulative_col: lambda values: values.sum(min_count=1),
                  bj_period_base_col: lambda values: values.sum(min_count=1),
                  bj_cumulative_base_col: lambda values: values.sum(min_count=1),
              })
              .rename(columns={
                  bj_period_col: 'bj_period_profit',
                  bj_cumulative_col: 'bj_cumulative_profit',
                  bj_period_base_col: 'bj_period_base',
                  bj_cumulative_base_col: 'bj_base',
              })
          )
          bj_selection_metrics = (
              bj_rows.groupby('Date')[
                  ['BJ 선택구간_누적손익', 'BJ 선택구간_기준액']
              ]
              .agg(lambda values: values.sum(min_count=1))
              .rename(columns={
                  'BJ 선택구간_누적손익': 'bj_selection_cumulative_profit',
                  'BJ 선택구간_기준액': 'bj_selection_base',
              })
          )
          bj_agg = bj_agg.join(bj_selection_metrics, how='left')
          bj_by_date = bj_agg.to_dict('index')
          grp_agg['bj_base'] = np.nan
          grp_agg['bj_selection_base'] = np.nan
          grp_agg['BJ 올해 누적손익'] = np.nan
          grp_agg['BJ 올해 누적기준액'] = np.nan
          grp_agg['BJ 올해 누적 수익률'] = np.nan
          for row_index, row in grp_agg[grp_agg['whose'] == 'BJ'].iterrows():
            bj_metrics = bj_by_date.get(row['Date'])
            if bj_metrics is None:
              grp_agg.at[row_index, '주기별 평가손익'] = np.nan
              grp_agg.at[row_index, '선택구간 누적손익'] = np.nan
              grp_agg.at[row_index, '주기별 수익률'] = np.nan
              grp_agg.at[row_index, '구간별 누적수익률'] = np.nan
              continue

            grp_agg.at[row_index, '주기별 평가손익'] = bj_metrics[
                'bj_period_profit'
            ]
            year_profit = bj_metrics['bj_cumulative_profit']
            selection_profit = bj_metrics['bj_selection_cumulative_profit']
            use_selection_period = (
                pd.Timestamp(start_date)
                >= pd.Timestamp(BJ_PORTFOLIO_ASSUMED_FROM)
            )
            if use_selection_period:
              interval_profit = selection_profit
              interval_base = bj_metrics['bj_selection_base']
            else:
              interval_profit = year_profit
              interval_base = bj_metrics['bj_base']
            grp_agg.at[row_index, '선택구간 누적손익'] = interval_profit
            grp_agg.at[row_index, 'bj_base'] = bj_metrics['bj_base']
            grp_agg.at[row_index, 'bj_selection_base'] = bj_metrics[
                'bj_selection_base'
            ]
            grp_agg.at[row_index, 'BJ 올해 누적손익'] = year_profit
            grp_agg.at[row_index, 'BJ 올해 누적기준액'] = bj_metrics['bj_base']
            grp_agg.at[row_index, 'BJ 올해 누적 수익률'] = (
                year_profit / bj_metrics['bj_base'] * 100
                if pd.notna(bj_metrics['bj_base'])
                and bj_metrics['bj_base'] > 0 else np.nan
            )
            period_base = bj_metrics['bj_period_base']
            grp_agg.at[row_index, '주기별 수익률'] = (
                bj_metrics['bj_period_profit'] / period_base * 100
                if pd.notna(period_base) and period_base > 0 else np.nan
            )
            grp_agg.at[row_index, '구간별 누적수익률'] = (
                interval_profit / interval_base * 100
                if pd.notna(interval_base) and interval_base > 0 else 0
            )

          if (
              pd.Timestamp(start_date)
              >= pd.Timestamp(BJ_PORTFOLIO_ASSUMED_FROM)
          ):
            bj_mask = grp_agg['whose'] == 'BJ'
            bj_growth_factor = 1 + (
                grp_agg.loc[bj_mask, '주기별 수익률'].fillna(0) / 100
            )
            grp_agg.loc[bj_mask, '구간별 누적수익률'] = (
                bj_growth_factor.groupby(grp_agg.loc[bj_mask, 'whose'])
                .cumprod()
                .sub(1)
                .mul(100)
            )

          return grp_agg

        agg1_def = get_whose_agg(False)
        whose_list = sorted(agg1_def['whose'].unique())

        st.markdown('### 📋 [전체 합산] WHOSE별 최종 핵심지표 요약 표')
        ex_whose_summary = st.toggle('🔀 환차손 제외 결과로 보기', key=f'ex_whose_summary_total')
        show_bj_ex_fx_estimate = ex_whose_summary
        bj_year_profit_label = (
            'BJ 올해 누적 평가손익 (7/31 이후 환차손 제외 추정)'
            if show_bj_ex_fx_estimate
            else 'BJ 올해 누적 평가손익'
        )
        bj_year_return_label = (
            'BJ 올해 누적 수익률 (7/31 이후 환차손 제외 추정)'
            if show_bj_ex_fx_estimate
            else 'BJ 올해 누적 수익률'
        )
        
        agg_whose_summary = get_whose_agg(ex_whose_summary)
        latest_date = raw_df['Date'].max()

        whose_summary_data = []
        total_eval_sum = 0
        total_pl_sum = 0
        total_principal_sum = 0
        total_bj_year_base_sum = 0

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
            if (
                w == 'BJ'
                and 'bj_selection_base' in sub.columns
                and pd.notna(sub['bj_selection_base'].iloc[-1])
            ):
              final_principal = (
                  sub['bj_selection_base'].iloc[-1]
                  if pd.Timestamp(start_date)
                  >= pd.Timestamp(BJ_PORTFOLIO_ASSUMED_FROM)
                  else sub['bj_base'].iloc[-1]
              )
            final_bj_year_profit = (
                sub['BJ 올해 누적손익'].iloc[-1]
                if 'BJ 올해 누적손익' in sub.columns else np.nan
            )
            final_bj_year_base = (
                sub['BJ 올해 누적기준액'].iloc[-1]
                if 'BJ 올해 누적기준액' in sub.columns else np.nan
            )
            final_bj_year_return = (
                sub['BJ 올해 누적 수익률'].iloc[-1]
                if 'BJ 올해 누적 수익률' in sub.columns else np.nan
            )

            total_eval_sum += final_eval
            total_pl_sum += final_p_loss if pd.notna(final_p_loss) else 0
            total_principal_sum += final_principal
            if pd.notna(final_bj_year_base):
              total_bj_year_base_sum += final_bj_year_base

            whose_summary_data.append({
                'WHOSE': w,
                '선택구간 누적 평가 손익 (원)': final_p_loss,
                '최종 기말 평가 금액 (원)': final_eval,
                '선택구간 누적 수익률 (%)': final_ret,
                bj_year_profit_label: final_bj_year_profit,
                bj_year_return_label: final_bj_year_return,
                '_eval': final_eval,
            })

        for row in whose_summary_data:
          row['점유율 (%)'] = (row['_eval'] / total_eval_sum * 100) if total_eval_sum > 0 else 0
          del row['_eval']

        total_ret = (total_pl_sum / total_principal_sum * 100) if total_principal_sum > 0 else 0
        total_bj_year_profit = sum(
            row[bj_year_profit_label]
            for row in whose_summary_data
            if pd.notna(row[bj_year_profit_label])
        )
        total_bj_year_return = (
            total_bj_year_profit / total_bj_year_base_sum * 100
            if total_bj_year_base_sum > 0 else np.nan
        )
        whose_summary_data.append({
            'WHOSE': '전체 합산',
            '선택구간 누적 평가 손익 (원)': total_pl_sum,
            '최종 기말 평가 금액 (원)': total_eval_sum,
            '선택구간 누적 수익률 (%)': total_ret,
            bj_year_profit_label: total_bj_year_profit,
            bj_year_return_label: total_bj_year_return,
            '점유율 (%)': 100.0 if total_eval_sum > 0 else 0.0,
        })

        whose_summary_df = pd.DataFrame(whose_summary_data)
        cols_w = [
            'WHOSE',
            '선택구간 누적 평가 손익 (원)',
            '최종 기말 평가 금액 (원)',
            '점유율 (%)',
            '선택구간 누적 수익률 (%)',
            bj_year_profit_label,
            bj_year_return_label,
        ]
        whose_summary_df = whose_summary_df[[c for c in cols_w if c in whose_summary_df.columns]]

        whose_summary_table, whose_column_config = style_trend_report_table(
            whose_summary_df
        )
        st.dataframe(
            whose_summary_table,
            column_config=whose_column_config,
            use_container_width=True,
        )
        st.write('---')

        st.markdown('##### ⚙️ [전체 합산] 차트별 범주 및 환율 옵션 설정')

        cb_w1, cb_w2, cb_w3, cb_w4 = st.columns(4)
        leg_pos_options = ['하단 배치', '우측 배치', '숨김']

        with cb_w1:
          pos_w1 = st.selectbox(
              '1. 전체 자산 평가 금액',
              leg_pos_options,
              index=get_chart_legend_idx('pos_w1_total', global_legend_pos, leg_pos_options),
              key='pos_w1_total',
          )
          ex_w1 = st.toggle('🔀 환차손제외 (1번)', key='ex_w1_total')
        with cb_w2:
          pos_w2 = st.selectbox(
              '2. 구간 손익 금액 추이',
              leg_pos_options,
              index=get_chart_legend_idx('pos_w2_total', global_legend_pos, leg_pos_options),
              key='pos_w2_total',
          )
          ex_w2 = st.toggle('🔀 환차손제외 (2번)', key='ex_w2_total')
        with cb_w3:
          pos_w3 = st.selectbox(
              '3-1. 구간 누적수익률',
              leg_pos_options,
              index=get_chart_legend_idx('pos_w3_total', global_legend_pos, leg_pos_options),
              key='pos_w3_total',
          )
          ex_w3 = st.toggle('🔀 환차손제외 (3-1번)', key='ex_w3_total')
        with cb_w4:
          pos_w4 = st.selectbox(
              '3-2. 주기별 수익률',
              leg_pos_options,
              index=get_chart_legend_idx('pos_w4_total', global_legend_pos, leg_pos_options),
              key='pos_w4_total',
          )
          ex_w4 = st.toggle('🔀 환차손제외 (3-2번)', key='ex_w4_total')

        agg1 = get_whose_agg(ex_w1)
        total_per_date1 = (
            agg1.groupby('Chart_Date')['총평가금액'].sum().reset_index()
        )
        date_order_list = sorted(agg1['Chart_Date'].unique().tolist())
        colors = {'BJ': '#2b5c8f', 'SH': '#ff7f0e'}

        fig1 = make_subplots(specs=[[{'secondary_y': True}]])
        for w in whose_list:
          sub = agg1[agg1['whose'] == w]
          fig1.add_trace(
              go.Bar(
                  x=sub['Chart_Date'],
                  y=sub['총평가금액'],
                  name=f'총평가금액 ({w})',
                  marker_color=colors.get(w, '#1f77b4'),
              ),
              secondary_y=False,
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
            ),
            secondary_y=True,
        )
        leg_cfg_w1, show_w1, margin_w1 = build_legend_config(pos_w1)
        suf_w1 = ' (환차손제외)' if ex_w1 else ''
        fig1.update_layout(
            title=dict(
                text=(
                    f'1. [전체 자산 평가 금액] WHOSE별 묶은 세로 막대 (주축) & 전체 합산'
                    f' 꺾은선 (보조축){suf_w1}'
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
            margin=margin_w1,
            showlegend=show_w1,
            legend=leg_cfg_w1,
        )
        fig1.update_xaxes(
            type='category',
            categoryorder='array',
            categoryarray=date_order_list,
        )
        fig1.update_yaxes(
            title_text='개별 평가금액 (원)', tickformat=',.0f', secondary_y=False
        )
        fig1.update_yaxes(
            title_text='전체 합산 평가금액 (원)', tickformat=',.0f', secondary_y=True
        )

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
                    f' 분리, 그룹형 세로 막대){suf_w2}'
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
              go.Bar(
                  x=sub['Chart_Date'],
                  y=sub['주기별 수익률'],
                  name=f'주기별 수익률 (%) ({w})',
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
              go.Bar(
                  x=sub_bm['Chart_Date'],
                  y=sub_bm['주기별 수익률'],
                  name=f'📌 {bm_name}',
                  hovertemplate='%{y:.2f}%',
              )
          )
        leg_cfg_w4, show_w4, margin_w4 = build_legend_config(pos_w4)
        suf_w4 = ' (환차손제외)' if ex_w4 else ''
        fig3b.update_layout(
            title=dict(
                text=(
                    f'3-2. [전체합산] 주기별 수익률 추이 (WHOSE 기준'
                    f' 그룹형 세로 막대){suf_w4}'
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

        st.write('---')
        st.markdown('### 📊 [전체 합산] 일자별 총 평가 금액 항목별 분석')
        
        group_opt_map = {
            '계좌별': 'account_num',
            '증권사별': 'broker',
            '계좌유형별': 'account_type',
            'Category 2별': 'category2',
            'Category 4별': 'category4',
            '보유 항목별': 'item_name'
        }
        
        c_tot1, c_tot2, c_tot3 = st.columns([2.5, 2.5, 2])
        with c_tot1:
          selected_total_group = st.radio(
              '분류 항목 선택',
              options=list(group_opt_map.keys()),
              horizontal=True,
              key='total_stack_group_opt'
          )
        with c_tot2:
          total_chart_type_opt = st.radio(
              '차트 유형 선택',
              options=['누적 세로 막대', '꺾은선 그래프'],
              horizontal=True,
              key='total_stack_chart_type_opt'
          )
        with c_tot3:
          pos_total_stack = st.selectbox(
              '📌 범례(Legend) 배치',
              options=leg_pos_options,
              index=get_chart_legend_idx('pos_total_stack', global_legend_pos, leg_pos_options),
              key='pos_total_stack'
          )
        
        target_col = group_opt_map[selected_total_group]
        
        total_agg = raw_df.groupby(['Date', target_col])['총평가금액'].sum().reset_index()
        total_agg['dt_temp'] = pd.to_datetime(total_agg['Date'])
        total_agg = total_agg.sort_values('dt_temp').reset_index(drop=True)
        total_agg['Chart_Date'] = total_agg['dt_temp'].dt.strftime('%Y-%m-%d')
        date_order = sorted(total_agg['Chart_Date'].unique().tolist())

        fig_total_stack = go.Figure()
        cat_list = total_agg[target_col].unique().tolist()
        
        if total_chart_type_opt == '누적 세로 막대':
          for cat in cat_list:
            c_df = total_agg[total_agg[target_col] == cat]
            fig_total_stack.add_trace(
                go.Bar(
                    x=c_df['Chart_Date'],
                    y=c_df['총평가금액'],
                    name=str(cat),
                    hovertemplate='%{y:,.0f} 원'
                )
            )
          tot_bmode = 'stack'
        else:
          for cat in cat_list:
            c_df = total_agg[total_agg[target_col] == cat]
            fig_total_stack.add_trace(
                go.Scatter(
                    x=c_df['Chart_Date'],
                    y=c_df['총평가금액'],
                    name=str(cat),
                    mode='lines+markers',
                    hovertemplate='%{y:,.0f} 원'
                )
            )
          tot_bmode = None

        leg_cfg_tot, show_tot, margin_tot = build_legend_config(pos_total_stack)
        tot_chart_title_type = "누적" if total_chart_type_opt == '누적 세로 막대' else "꺾은선"
        fig_total_stack.update_layout(
            title=dict(
                text=f'🔹 [전체 합산] 일자별 총 평가 금액 ({selected_total_group} 기준 {tot_chart_title_type})',
                y=0.95, x=0.01, xanchor='left', yanchor='top', yref='container'
            ),
            barmode=tot_bmode,
            hovermode='closest',
            height=500,
            margin=margin_tot,
            showlegend=show_tot,
            legend=leg_cfg_tot
        )
        fig_total_stack.update_xaxes(type='category', categoryorder='array', categoryarray=date_order)
        fig_total_stack.update_yaxes(title_text='총 평가금액 (원)', tickformat=',.0f')

        render_resizable_plotly_chart(fig_total_stack, key='fig_total_stack_chart')

      if active_views:
        tabs_key = 'trend_report_views'
        default_tab = st.session_state.get(tabs_key, active_views[0])
        if default_tab not in active_views:
          default_tab = active_views[0]
        st.session_state[tabs_key] = default_tab
        tabs = st.tabs(active_views, key=tabs_key, default=default_tab)
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
            elif v_type == 'Category 2별':
              draw_group_summary_charts(calc_df, 'category2', 'Category 2별')
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
  st.header('🏷 계좌 별칭 관리')
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
# 메뉴: 종목 분류 관리 (신규 기능)
# -----------------------------------------------------------------------------
elif menu == '종목 분류 관리':
  st.header('🏷️ 종목 분류(Category) 일괄 관리 및 수정')
  st.info(
      '💡 현재 DB에 등록된 모든 보유 종목의 분류(Category 1~4)를 한눈에 확인하고 웹에서 직접 수정할 수 있습니다.\n\n'
      '여기서 수정한 분류 정보는 **과거 모든 일자의 포트폴리오 데이터와 백업 DB에 일괄 자동 적용**됩니다.'
  )

  item_cat_df = get_all_item_categories()

  if item_cat_df.empty:
    st.warning('등록된 포트폴리오 및 종목 데이터가 없습니다. 먼저 포트폴리오를 업로드해 주세요.')
  else:
    f_c1, f_c2 = st.columns([2, 1])
    with f_c1:
      search_kw = st.text_input('🔍 종목명 또는 티커 검색', placeholder='예: LITE, IGM, BE, DRAM 등')
    with f_c2:
      st.write(f'총 등록 종목 수: **{len(item_cat_df)}**개')

    display_df = item_cat_df.copy()
    if search_kw.strip():
      kw = search_kw.strip().lower()
      display_df = display_df[
          display_df['item_name'].astype(str).str.lower().str.contains(kw) |
          display_df['ticker'].astype(str).str.lower().str.contains(kw)
      ]

    st.markdown('##### 📝 종목별 분류 정보 테이블 (수정 후 아래 저장 버튼 클릭)')

    edited_cat_df = st.data_editor(
        display_df,
        disabled=['ticker', 'item_name'],
        use_container_width=True,
        key='item_category_editor',
        column_config={
            'ticker': st.column_config.TextColumn('티커 (Ticker)', help='종목 티커 (식별자, 수정 불가)'),
            'item_name': st.column_config.TextColumn('종목명 (Item Name)', help='종목명 (식별자, 수정 불가)'),
            'category1': st.column_config.TextColumn('Category 1 (대분류)', help='예: 주식, ETF, 현금 등'),
            'category2': st.column_config.TextColumn('Category 2 (중분류)', help='예: 미국주식, 국내ETF 등'),
            'category3': st.column_config.TextColumn('Category 3 (소분류)', help='예: IT, 반도체 등'),
            'category4': st.column_config.TextColumn('Category 4 (세부분류)', help='예: 광통신, AI소프트웨어 등'),
        }
    )

    btn_c1, btn_c2 = st.columns([3, 2])
    with btn_c1:
      if st.button('💾 변경된 종목 분류 전체 DB에 일괄 저장 및 적용', type='primary'):
        with st.spinner('변경된 분류 정보를 전체 포트폴리오 DB에 반영 중입니다...'):
          save_item_categories(edited_cat_df)
          sync_latest_categories_to_portfolio()
        st.success('✅ 모든 종목의 분류 정보가 성공적으로 전체 포트폴리오(과거 전 일자)에 반영되었습니다!')
        st.rerun()

    with btn_c2:
      if st.button('🔄 최신 포트폴리오 기준으로 전체 재동기화'):
        with st.spinner('최신 포트폴리오 기준으로 동기화 중...'):
          sync_latest_categories_to_portfolio()
        st.success('최신 포트폴리오 기준 카테고리 동기화가 완료되었습니다!')
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
            'buy_price': 'purchase_price',
            '현재가': 'current_price',
            '평가금액': 'eval_price',
            '평가손익': 'eval_profit_loss',
            '수익률': 'return_rate',
            '비중': 'portfolio_weight',
            '통화': 'currency',
            'whose': 'whose',
            '소유자': 'whose',
            '소유주': 'whose'
        }

        rename_dict = {k: v for k, v in col_map.items() if k in df_upload.columns}
        df_db = df_upload.rename(columns=rename_dict)

        if 'record_date' in df_db.columns:
          df_db['record_date'] = pd.to_datetime(df_db['record_date']).dt.strftime('%Y-%m-%d')

        conn = get_connection()
        c = conn.cursor()

        target_cols = [
            'record_date', 'broker', 'account_num', 'account_name', 'account_type',
            'item_name', 'ticker', 'category1', 'category2', 'category3', 'category4',
            'quantity', 'purchase_price', 'current_price', 'eval_price',
            'eval_profit_loss', 'return_rate', 'portfolio_weight', 'currency', 'whose'
        ]

        for col in target_cols:
          if col not in df_db.columns:
            df_db[col] = None

        df_db = df_db[target_cols]

        # Replace only snapshots for accounts present in this upload.
        # Deleting by date alone removes other owners' accounts when an
        # owner-specific workbook (for example, SH only) is uploaded.
        account_keys = df_db[
            ['record_date', 'broker', 'account_num', 'whose']
        ].drop_duplicates()
        for _, account_key in account_keys.iterrows():
          account_num = account_key['account_num']
          if pd.isna(account_num) or not str(account_num).strip():
            continue
          key_values = tuple(
              None if pd.isna(account_key[column]) else account_key[column]
              for column in ('record_date', 'broker', 'account_num', 'whose')
          )
          c.execute(
              '''DELETE FROM portfolio
                 WHERE record_date IS ?
                   AND broker IS ?
                   AND account_num IS ?
                   AND whose IS ?''',
              key_values,
          )

        df_db.to_sql('portfolio', conn, if_exists='append', index=False)
        conn.commit()
        conn.close()

        # 업로드된 최신 포트폴리오의 분류 항목을 기존 DB 전체에도 일괄 동기화
        sync_latest_categories_to_portfolio()

        st.success('데이터베이스 적재가 완료되었습니다! (최신 입력된 종목 분류 항목이 기존 DB 전체에도 일괄 적용되었습니다)')
    except Exception as e:
      st.error(f'파일 처리 중 오류가 발생했습니다: {e}')

# -----------------------------------------------------------------------------
# 메뉴 4: 원금 및 입출금 관리
# -----------------------------------------------------------------------------
elif menu == '원금 및 입출금 관리':
  st.header('💰 최초 원금 및 현금흐름(입출금) 관리')

  tab1, tab2, tab3 = st.tabs([
      '🏦 계좌별 최초 원금 설정',
      '💸 계좌별 입출금 입력',
      '📅 BJ 2025년 말 평가액 기준',
  ])

  with tab1:
    st.subheader('계좌별 최초 시작 원금 등록 및 수정')
    conn = get_connection()
    pf_accs = pd.read_sql(
        'SELECT DISTINCT broker, account_num FROM portfolio', conn
    )
    init_p_df = pd.read_sql('SELECT * FROM initial_principal', conn)
    conn.close()

    init_p_dict = dict(
        zip(init_p_df['account_num'].astype(str), init_p_df['initial_amount'])
    )

    if pf_accs.empty:
      st.info('포트폴리오에 등록된 계좌가 없습니다.')
    else:
      with st.form('init_principal_form'):
        new_init_vals = {}
        for _, row in pf_accs.iterrows():
          b_name = row['broker']
          acc_num = str(row['account_num'])
          alias = alias_map.get(acc_num, '')
          disp_name = f'[{b_name}] {acc_num} ({alias})' if alias else f'[{b_name}] {acc_num}'
          curr_val = float(init_p_dict.get(acc_num, 0.0))

          val = st.number_input(
              f'{disp_name} 최초 원금 (원)',
              value=curr_val,
              step=100000.0,
              format='%.0f',
              key=f'init_p_{acc_num}',
          )
          new_init_vals[acc_num] = (b_name, val)

        sub_p = st.form_submit_button('💾 최초 원금 저장')
        if sub_p:
          conn = get_connection()
          c = conn.cursor()
          for acc_num, (b_name, val) in new_init_vals.items():
            c.execute(
                'INSERT OR REPLACE INTO initial_principal (account_num, broker, initial_amount) VALUES (?, ?, ?)',
                (acc_num, b_name, val),
            )
          conn.commit()
          conn.close()
          st.success('최초 원금이 저장되었습니다!')
          st.rerun()

  with tab2:
    st.subheader('💸 계좌별 입출금 내역 등록 및 관리')
    st.caption(
        '계좌, 거래일, 구분(입금/출금), 금액(원), 비고를 등록하고 관리합니다. '
        '거래일이 조회 구간의 시작일과 종료일 사이에 포함되면 평가손익과 올해 누적손익에 반영됩니다. '
        '하단의 다중 필터링(WHOSE, 계좌유형, 계좌, 연도, 월)을 통해 원하는 내역만 모아볼 수 있으며, '
        '필터를 선택하지 않으면 모든 내역이 기본 표시됩니다. '
        '웹 테이블(Data Editor) 내에서 거래일, 계좌번호, 구분, 금액, 비고를 직접 수정/추가/삭제할 수 있습니다.'
    )
    conn = get_connection()
    pf_accs = pd.read_sql(
        'SELECT DISTINCT broker, account_num, account_type, account_name, whose FROM portfolio', conn
    )
    cf_df = pd.read_sql(
        'SELECT * FROM cash_flow ORDER BY trans_date DESC, id DESC', conn
    )
    init_p_df = pd.read_sql('SELECT * FROM initial_principal', conn)
    conn.close()

    # 계좌 메타데이터 생성 및 연금/일반 유형 분류
    def get_acc_type_group(atype):
      atype_str = str(atype).strip() if pd.notna(atype) else ''
      if any(k in atype_str for k in ['연금', 'IRP', '퇴직', 'DC', 'DB', '개인연금']):
        return '연금'
      return '일반'

    acc_meta_map = {}
    account_options = []
    account_by_label = {}
    registered_acc_nums = []

    for _, account_row in pf_accs.iterrows():
      acc_num = str(account_row['account_num'])
      norm_acc = normalize_account_num(acc_num)
      broker = str(account_row['broker']) if pd.notna(account_row['broker']) else '증권사 미지정'
      raw_acc_type = str(account_row['account_type']) if pd.notna(account_row['account_type']) else '일반'
      type_group = get_acc_type_group(raw_acc_type)
      whose_val = resolve_account_whose(acc_num, account_row.get('whose'), broker, alias_map)
      alias = alias_map.get(acc_num, '')
      cash_label_text = f'{alias} {account_row.get("account_name", "")}'.upper()
      type_pivot = (
          'CASH'
          if any(token in cash_label_text for token in ['CASH', 'CMA', '현금'])
          else type_group
      )
      label = (
          f'[{broker}] {acc_num} ({alias}) [{type_group}|{whose_val}]'
          if alias else f'[{broker}] {acc_num} [{type_group}|{whose_val}]'
      )
      if norm_acc not in acc_meta_map:
        acc_meta_map[norm_acc] = {
            'account_num': acc_num,
            'broker': broker,
            'account_type': raw_acc_type,
            'account_type_group': type_group,
            'account_type_pivot': type_pivot,
            'whose': whose_val,
            'alias': alias,
            'label': label,
        }
        account_options.append(label)
        account_by_label[label] = acc_num
      if acc_num not in registered_acc_nums:
        registered_acc_nums.append(acc_num)

    # 신규 등록 폼
    with st.expander('➕ 신규 입출금 내역 간편 등록', expanded=False):
      with st.form('add_cash_flow_form'):
        col_cf1, col_cf2, col_cf3 = st.columns([2, 1, 1])
        with col_cf1:
          selected_account_label = st.selectbox(
              '계좌 선택',
              options=account_options if account_options else ['등록된 계좌 없음'],
              key='cash_flow_account_selector',
          )
          cf_note = st.text_input(
              '비고 (예: 추가 시드 입금, 생활비 출금 등)'
          )
        with col_cf2:
          cf_date = st.date_input('거래 일자', value=date.today())
          flow_type = st.radio(
              '구분', options=['입금', '출금'], horizontal=True
          )
        with col_cf3:
          cf_amount = st.number_input(
              '금액 (원)', min_value=0.0, value=0.0,
              step=100000.0, format='%.0f',
          )
          save_cash_flow = st.form_submit_button('➕ 입출금 내역 저장')

        if save_cash_flow:
          if not account_options or selected_account_label not in account_by_label:
            st.warning('유효한 계좌를 선택해 주세요.')
          elif cf_amount <= 0:
            st.warning('입출금 금액은 0보다 커야 합니다.')
          else:
            target_acc_num = account_by_label[selected_account_label]
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute(
                'INSERT INTO cash_flow '
                '(trans_date, account_num, flow_type, amount, note) '
                'VALUES (?, ?, ?, ?, ?)',
                (
                    cf_date.strftime('%Y-%m-%d'),
                    target_acc_num,
                    flow_type,
                    cf_amount,
                    cf_note.strip(),
                ),
            )
            conn.commit()
            conn.close()
            st.success('입출금 내역이 성공적으로 저장되었습니다.')
            st.rerun()

    st.write('---')
    st.subheader('📊 WHOSE·계좌별 입출금 및 원금 요약')
    st.caption(
        '선택한 연도와 월의 입금·출금·순입출금 합계와 월별 누적 순입출금을 확인할 수 있습니다. '
        '전체 원금은 최초 원금에 전체 기간의 순입출금을 더한 값으로 표시합니다.'
    )

    summary_cf = cf_df.copy()
    if summary_cf.empty:
      summary_cf = pd.DataFrame(
          columns=['account_num', 'trans_date', 'flow_type', 'amount']
      )
    summary_cf['_acc_norm'] = summary_cf['account_num'].map(normalize_account_num)
    summary_cf['whose'] = summary_cf['_acc_norm'].map(
        lambda key: acc_meta_map.get(key, {}).get('whose', '미지정')
    )
    summary_cf['broker'] = summary_cf['_acc_norm'].map(
        lambda key: acc_meta_map.get(key, {}).get('broker', '증권사 미지정')
    )
    summary_cf['account_type'] = summary_cf['_acc_norm'].map(
        lambda key: acc_meta_map.get(key, {}).get('account_type', '일반')
    )
    summary_cf['account_type_group'] = summary_cf['_acc_norm'].map(
        lambda key: acc_meta_map.get(key, {}).get('account_type_group', '일반')
    )
    summary_cf['account_type_pivot'] = summary_cf['_acc_norm'].map(
        lambda key: acc_meta_map.get(key, {}).get('account_type_pivot', '일반')
    )
    summary_cf['account_label'] = summary_cf['_acc_norm'].map(
        lambda key: acc_meta_map.get(key, {}).get('label', str(key))
    )
    summary_cf['dt'] = pd.to_datetime(summary_cf['trans_date'], errors='coerce')
    summary_cf['year'] = summary_cf['dt'].dt.year
    summary_cf['month'] = summary_cf['dt'].dt.month
    summary_cf['amount_clean'] = summary_cf['amount'].map(parse_cash_flow_amount)
    summary_cf['_flow_type'] = summary_cf['flow_type'].map(normalize_cash_flow_type)
    summary_cf['입금액'] = np.where(
        summary_cf['_flow_type'] == '입금', summary_cf['amount_clean'], 0.0
    )
    summary_cf['출금액'] = np.where(
        summary_cf['_flow_type'] == '출금', summary_cf['amount_clean'], 0.0
    )
    summary_cf['순입출금액'] = summary_cf['입금액'] - summary_cf['출금액']

    available_years = sorted(
        [int(year) for year in summary_cf['year'].dropna().unique()], reverse=True
    )
    filter_col1, filter_col2 = st.columns([2, 3])
    with filter_col1:
      selected_summary_years = st.multiselect(
          '조회 연도 (ALL 선택 시 전체 연도)',
          options=['ALL'] + available_years,
          default=['ALL'],
          key='cash_flow_summary_years',
      )
    with filter_col2:
      selected_summary_months = st.multiselect(
          '조회 월 (미선택 시 전체 월)',
          options=list(range(1, 13)),
          default=[],
          format_func=lambda month: f'{month}월',
          key='cash_flow_summary_months',
      )

    selected_year_values = (
        available_years
        if not selected_summary_years or 'ALL' in selected_summary_years
        else selected_summary_years
    )
    selected_month_values = selected_summary_months or list(range(1, 13))
    period_cf = summary_cf[
        summary_cf['year'].isin(selected_year_values)
        & summary_cf['month'].isin(selected_month_values)
        & summary_cf['amount_clean'].notna()
    ].copy()

    group_dim_labels = {
        'year': '연도',
        'month': '월',
        'whose': 'WHOSE',
        'account_type_pivot': '계좌유형(CASH/연금/일반)',
        'account_label': '계좌',
    }
    group_dim_options = list(group_dim_labels)
    selected_row_dims = st.multiselect(
        '행 구분 기준 (여러 개 선택 가능)',
        options=group_dim_options,
        default=['year', 'month', 'whose', 'account_label'],
        format_func=lambda key: group_dim_labels[key],
        key='cash_flow_summary_row_dims',
        help='행 기준을 여러 개 선택하면 계층별 부분합이 표시됩니다.',
    )
    column_dim_options = [
        key for key in group_dim_options if key not in selected_row_dims
    ]
    column_state_key = 'cash_flow_summary_column_dim'
    valid_column_options = ['없음'] + column_dim_options
    default_column_dim = (
        'account_type_pivot'
        if 'account_type_pivot' in column_dim_options else '없음'
    )
    if st.session_state.get(column_state_key) not in valid_column_options:
      st.session_state[column_state_key] = default_column_dim
    selected_column_dim = st.selectbox(
        '열 구분 기준 (피벗 열)',
        options=['없음'] + column_dim_options,
        format_func=lambda key: '없음' if key == '없음' else group_dim_labels[key],
        key=column_state_key,
        help='예: 계좌유형(CASH/연금/일반)을 선택하면 열에 유형별 금액과 총합계를 표시합니다.',
    )
    selected_column_dim = (
        None if selected_column_dim == '없음' else selected_column_dim
    )
    if selected_row_dims:
      first_group_state_key = 'cash_flow_summary_first_group_dim'
      if st.session_state.get(first_group_state_key) not in selected_row_dims:
        st.session_state[first_group_state_key] = selected_row_dims[0]
      first_group_dim = st.selectbox(
          '가장 먼저 표시할 행 기준',
          options=selected_row_dims,
          format_func=lambda key: group_dim_labels[key],
          key=first_group_state_key,
      )
      ordered_group_dims = [first_group_dim] + [
          key for key in selected_row_dims if key != first_group_dim
      ]
    else:
      ordered_group_dims = []

    display_dim_labels = {
        'year': '연도',
        'month': '월',
        'whose': 'WHOSE',
        'account_type_pivot': '계좌유형(CASH/연금/일반)',
        'account_label': '계좌',
    }

    def make_grouped_summary(
        source_df, row_dims, column_dim, metric_columns
    ):
      """Create pivot columns, hierarchical row subtotals, and grand totals."""
      display_columns = [display_dim_labels[key] for key in row_dims]
      output_rows = []
      column_values = []
      column_value_labels = {}
      if column_dim and column_dim in source_df.columns and not source_df.empty:
        column_values = sorted(
            source_df[column_dim].fillna('미분류').astype(str).unique().tolist()
        )
        for value in column_values:
          label = value
          if column_dim in ('year', 'month') and value != '미분류':
            try:
              label = str(int(float(value)))
              if column_dim == 'month':
                label = f'{label}월'
            except (TypeError, ValueError):
              pass
          column_value_labels[value] = label
      value_columns = (
          [f'{column_value_labels[column_value]} | {metric}'
           for column_value in column_values for metric in metric_columns]
          + [f'총합계 | {metric}' for metric in metric_columns]
          if column_dim else metric_columns
      )

      def append_summary_row(frame, prefix, row_label):
        row = {'집계구분': row_label}
        for index, key in enumerate(row_dims):
          label = display_dim_labels[key]
          row[label] = prefix[index] if index < len(prefix) else ''
        if column_dim:
          category_values = frame[column_dim].fillna('미분류').astype(str)
          for column_value in column_values:
            category_frame = frame[category_values == column_value]
            category_totals = category_frame[metric_columns].sum(numeric_only=True)
            for metric in metric_columns:
              row[f'{column_value_labels[column_value]} | {metric}'] = float(
                  category_totals.get(metric, 0.0)
              )
          totals = frame[metric_columns].sum(numeric_only=True)
          for metric in metric_columns:
            row[f'총합계 | {metric}'] = float(totals.get(metric, 0.0))
        else:
          totals = frame[metric_columns].sum(numeric_only=True)
          for metric in metric_columns:
            row[metric] = float(totals.get(metric, 0.0))
        output_rows.append(row)

      def add_group_rows(frame, depth, prefix):
        if depth >= len(row_dims):
          append_summary_row(frame, prefix, '상세')
          return
        key = row_dims[depth]
        for group_value, child in frame.groupby(key, dropna=False, sort=True):
          clean_value = '' if pd.isna(group_value) else group_value
          if key in ('year', 'month') and clean_value != '':
            try:
              clean_value = int(float(clean_value))
            except (TypeError, ValueError):
              pass
          child_prefix = prefix + [clean_value]
          add_group_rows(child, depth + 1, child_prefix)
          if depth < len(row_dims) - 1:
            append_summary_row(
                child, child_prefix,
                f'{display_dim_labels[key]} 소계',
            )

      if source_df.empty:
        empty_columns = ['집계구분'] + display_columns + value_columns
        empty_df = pd.DataFrame(columns=empty_columns)
        if column_dim:
          empty_df.columns = pd.MultiIndex.from_tuples(
              [('', '집계구분')]
              + [('행 레이블', label) for label in display_columns]
              + [
                  (column_value_labels[column_value], metric)
                  for column_value in column_values
                  for metric in metric_columns
              ]
              + [('총합계', metric) for metric in metric_columns]
          )
        return empty_df
      if row_dims:
        add_group_rows(source_df, 0, [])
      append_summary_row(source_df, [], '전체 합산')
      result_df = pd.DataFrame(output_rows, columns=(
          ['집계구분'] + display_columns + value_columns
      ))
      if column_dim:
        result_df.columns = pd.MultiIndex.from_tuples(
            [('', '집계구분')]
            + [('행 레이블', label) for label in display_columns]
            + [
                (column_value_labels[column_value], metric)
                for column_value in column_values
                for metric in metric_columns
            ]
            + [('총합계', metric) for metric in metric_columns]
        )
      return result_df

    def summary_metric_format(df, metric_names):
      format_map = {}
      for column in df.columns:
        metric_name = column[-1] if isinstance(column, tuple) else column
        if metric_name in metric_names:
          format_map[column] = '{:,.0f}원'
      return format_map

    # Account-level principal = initial principal + lifetime net cash flow.
    principal_rows = pf_accs.copy()
    if not principal_rows.empty:
      principal_rows['account_num'] = principal_rows['account_num'].astype(str)
      principal_rows = principal_rows.drop_duplicates('account_num')
      principal_rows['account_num_norm'] = principal_rows['account_num'].map(
          normalize_account_num
      )
      principal_rows['whose'] = principal_rows.apply(
          lambda row: resolve_account_whose(
              row['account_num'], row.get('whose'), row.get('broker'), alias_map
          ),
          axis=1,
      )
      initial_by_account = dict(zip(
          init_p_df['account_num'].astype(str),
          pd.to_numeric(init_p_df['initial_amount'], errors='coerce').fillna(0),
      )) if not init_p_df.empty else {}
      lifetime_flow = summary_cf.groupby('_acc_norm')['순입출금액'].sum()
      principal_rows['최초 원금'] = principal_rows['account_num'].map(
          lambda account: float(initial_by_account.get(account, 0.0))
      )
      principal_rows['전체 기간 순입출금'] = principal_rows[
          'account_num_norm'
      ].map(lifetime_flow).fillna(0.0)
      principal_rows['전체 원금'] = (
          principal_rows['최초 원금'] + principal_rows['전체 기간 순입출금']
      )
      principal_rows['계좌'] = principal_rows.apply(
          lambda row: (
              f"{row['broker']} | {row['account_num']}"
              if pd.notna(row.get('broker')) else str(row['account_num'])
          ),
          axis=1,
      )

    # Selected-period summary grouped by the chosen dimensions.
    st.markdown('##### 선택 기간 입출금 누적')
    summary_group_dims = ordered_group_dims + (
        [selected_column_dim] if selected_column_dim else []
    )
    period_summary_source = period_cf.rename(columns={
        '입금액': '입금 누적액',
        '출금액': '출금 누적액',
        '순입출금액': '순입출금 누적액',
    })
    whose_flow = make_grouped_summary(
        period_summary_source,
        ordered_group_dims,
        selected_column_dim,
        ['입금 누적액', '출금 누적액', '순입출금 누적액'],
    )
    st.dataframe(
        whose_flow.style.format(summary_metric_format(
            whose_flow,
            ['입금 누적액', '출금 누적액', '순입출금 누적액'],
        )),
        use_container_width=True,
        hide_index=True,
    )

    # Year/month cumulative summary uses the same selected dimensions/order.
    st.markdown('##### 연도·월별 누적 입출금')
    if period_cf.empty:
      monthly_summary = make_grouped_summary(
          period_cf,
          ordered_group_dims,
          selected_column_dim,
          ['입금액', '출금액', '순입출금액', '누적 순입출금액'],
      )
    elif 'month' in summary_group_dims:
      cumulative_group_dims = [
          key for key in summary_group_dims if key not in ('year', 'month')
      ]
      monthly_group_keys = list(dict.fromkeys(
          ['year'] + cumulative_group_dims + ['month']
      ))
      monthly_summary_source = (
          summary_cf[summary_cf['year'].isin(selected_year_values)]
          .groupby(monthly_group_keys, dropna=False)
          .agg(
              입금액=('입금액', 'sum'),
              출금액=('출금액', 'sum'),
              순입출금액=('순입출금액', 'sum'),
          )
          .reset_index()
          .sort_values(monthly_group_keys)
      )
      cumulative_keys = [key for key in monthly_group_keys if key != 'month']
      monthly_summary_source['누적 순입출금액'] = monthly_summary_source.groupby(
          cumulative_keys, dropna=False
      )['순입출금액'].cumsum()
      monthly_summary_source = monthly_summary_source[
          monthly_summary_source['month'].isin(selected_month_values)
      ]
      monthly_summary_source = (
          monthly_summary_source.groupby(summary_group_dims, dropna=False)
          [['입금액', '출금액', '순입출금액', '누적 순입출금액']]
          .sum()
          .reset_index()
      )
      monthly_summary = make_grouped_summary(
          monthly_summary_source,
          ordered_group_dims,
          selected_column_dim,
          ['입금액', '출금액', '순입출금액', '누적 순입출금액'],
      )
    else:
      monthly_summary_source = period_cf.copy()
      monthly_summary_source['누적 순입출금액'] = (
          monthly_summary_source['순입출금액']
      )
      monthly_summary = make_grouped_summary(
          monthly_summary_source,
          ordered_group_dims,
          selected_column_dim,
          ['입금액', '출금액', '순입출금액', '누적 순입출금액'],
      )
    monthly_month_column = (
        ('행 레이블', '월')
        if isinstance(monthly_summary.columns, pd.MultiIndex) else '월'
    )
    if monthly_month_column in monthly_summary.columns:
      monthly_summary[monthly_month_column] = monthly_summary[
          monthly_month_column
      ].map(
          lambda month: f'{int(month)}월' if pd.notna(month) and str(month).strip() else month
      )
    st.dataframe(
        monthly_summary.style.format(summary_metric_format(
            monthly_summary,
            ['입금액', '출금액', '순입출금액', '누적 순입출금액'],
        )),
        use_container_width=True,
        hide_index=True,
    )

    st.markdown('##### WHOSE 선택 후 계좌별 입출금 및 전체 원금')
    owner_options = sorted(set(
        (principal_rows['whose'].dropna().astype(str).tolist()
         if not principal_rows.empty else [])
        + (period_cf['whose'].dropna().astype(str).tolist()
           if not period_cf.empty else [])
    ))
    selected_summary_owner = st.selectbox(
        'WHOSE 선택',
        options=['전체'] + owner_options,
        key='cash_flow_summary_owner',
    )
    if selected_summary_owner == '전체':
      account_summary_source = principal_rows.copy()
    else:
      account_summary_source = principal_rows[
          principal_rows['whose'] == selected_summary_owner
      ].copy() if not principal_rows.empty else pd.DataFrame()

    if account_summary_source.empty:
      st.info('선택한 WHOSE에 표시할 계좌가 없습니다.')
    else:
      account_flow = (
          period_cf.groupby('_acc_norm', dropna=False)
          .agg(
              입금누적액=('입금액', 'sum'),
              출금누적액=('출금액', 'sum'),
              순입출금누적액=('순입출금액', 'sum'),
          )
      ) if not period_cf.empty else pd.DataFrame()
      account_summary_source['입금 누적액'] = account_summary_source[
          'account_num_norm'
      ].map(account_flow['입금누적액'] if not account_flow.empty else {}).fillna(0.0)
      account_summary_source['출금 누적액'] = account_summary_source[
          'account_num_norm'
      ].map(account_flow['출금누적액'] if not account_flow.empty else {}).fillna(0.0)
      account_summary_source['순입출금 누적액'] = account_summary_source[
          'account_num_norm'
      ].map(account_flow['순입출금누적액'] if not account_flow.empty else {}).fillna(0.0)
      account_summary_source = account_summary_source.rename(columns={
          'whose': 'WHOSE', 'account_num': '계좌번호', 'broker': '증권사',
          'account_type': '계좌유형', '최초 원금': '최초 원금',
          '전체 기간 순입출금': '전체 기간 순입출금', '전체 원금': '전체 원금',
      })
      account_summary_source = account_summary_source[[
          'WHOSE', '증권사', '계좌번호', '계좌유형', '입금 누적액',
          '출금 누적액', '순입출금 누적액', '최초 원금',
          '전체 기간 순입출금', '전체 원금',
      ]].sort_values(['WHOSE', '증권사', '계좌번호'])
      total_account_row = {'WHOSE': '전체 합산', '증권사': '', '계좌번호': '', '계좌유형': ''}
      for amount_col in account_summary_source.select_dtypes(include='number').columns:
        total_account_row[amount_col] = account_summary_source[amount_col].sum()
      account_summary_source = pd.concat(
          [account_summary_source, pd.DataFrame([total_account_row])],
          ignore_index=True,
      )
      st.dataframe(
          account_summary_source.style.format({
              col: '{:,.0f}원' for col in
              [
                  '입금 누적액', '출금 누적액', '순입출금 누적액',
                  '최초 원금', '전체 기간 순입출금', '전체 원금',
              ]
          }),
          use_container_width=True,
          hide_index=True,
      )

    st.write('---')

    if cf_df.empty:
      st.info('등록된 입출금 내역이 없습니다.')
    else:
      # cash_flow 데이터에 메타데이터 결합
      cf_df['_acc_norm'] = cf_df['account_num'].map(normalize_account_num)
      cf_df['whose'] = cf_df['_acc_norm'].map(lambda x: acc_meta_map.get(x, {}).get('whose', '미지정'))
      cf_df['broker'] = cf_df['_acc_norm'].map(lambda x: acc_meta_map.get(x, {}).get('broker', '증권사미지정'))
      cf_df['account_type_detail'] = cf_df['_acc_norm'].map(lambda x: acc_meta_map.get(x, {}).get('account_type', '일반'))
      cf_df['account_type_group'] = cf_df['_acc_norm'].map(lambda x: acc_meta_map.get(x, {}).get('account_type_group', '일반'))
      cf_df['account_label'] = cf_df['_acc_norm'].map(lambda x: acc_meta_map.get(x, {}).get('label', str(x)))

      cf_df['dt'] = pd.to_datetime(cf_df['trans_date'], errors='coerce')
      cf_df['year'] = cf_df['dt'].dt.year
      cf_df['month'] = cf_df['dt'].dt.month
      cf_df['amount_clean'] = cf_df['amount'].map(parse_cash_flow_amount)

      # 필터 옵션 생성
      whose_filter_opts = sorted([w for w in cf_df['whose'].dropna().unique() if w != '미지정'])
      if '미지정' in cf_df['whose'].values:
        whose_filter_opts.append('미지정')
      type_filter_opts = ['연금', '일반']
      acc_filter_opts = sorted(list(set(cf_df['account_label'].dropna().unique())))
      year_filter_opts = sorted([int(y) for y in cf_df['year'].dropna().unique() if pd.notna(y)], reverse=True)
      month_filter_opts = list(range(1, 13))

      st.markdown('##### 🔍 입출금 내역 다중 필터링 (필터 미선택 시 전체 표시)')
      flt_c1, flt_c2, flt_c3, flt_c4, flt_c5 = st.columns(5)
      with flt_c1:
        sel_cf_whose = st.multiselect('👤 WHOSE별', options=whose_filter_opts, default=[], key='cf_flt_whose', help='미선택 시 전체 대상')
      with flt_c2:
        sel_cf_type = st.multiselect('📋 계좌유형별 (연금/일반)', options=type_filter_opts, default=[], key='cf_flt_type', help='미선택 시 전체 대상')
      with flt_c3:
        sel_cf_accs = st.multiselect('🏦 계좌별', options=acc_filter_opts, default=[], key='cf_flt_accs', help='미선택 시 전체 대상')
      with flt_c4:
        sel_cf_years = st.multiselect('📅 연도별', options=year_filter_opts, default=[], key='cf_flt_years', help='미선택 시 전체 대상')
      with flt_c5:
        sel_cf_months = st.multiselect('🗓️ 월별', options=month_filter_opts, format_func=lambda m: f'{m}월', default=[], key='cf_flt_months', help='미선택 시 전체 대상')

      # 필터 적용 (미선택 시 전체 통과)
      filtered_cf = cf_df.copy()
      if sel_cf_whose:
        filtered_cf = filtered_cf[filtered_cf['whose'].isin(sel_cf_whose)]
      if sel_cf_type:
        filtered_cf = filtered_cf[filtered_cf['account_type_group'].isin(sel_cf_type)]
      if sel_cf_accs:
        filtered_cf = filtered_cf[filtered_cf['account_label'].isin(sel_cf_accs)]
      if sel_cf_years:
        filtered_cf = filtered_cf[filtered_cf['year'].isin(sel_cf_years)]
      if sel_cf_months:
        filtered_cf = filtered_cf[filtered_cf['month'].isin(sel_cf_months)]

      # 요약 메트릭
      dep_sum = filtered_cf.loc[filtered_cf['flow_type'].map(normalize_cash_flow_type) == '입금', 'amount_clean'].sum()
      wth_sum = filtered_cf.loc[filtered_cf['flow_type'].map(normalize_cash_flow_type) == '출금', 'amount_clean'].sum()
      net_sum = dep_sum - wth_sum

      sm1, sm2, sm3, sm4 = st.columns(4)
      sm1.metric('조회 내역', f'{len(filtered_cf):,}건')
      sm2.metric('총 입금액', f'{dep_sum:,.0f}원')
      sm3.metric('총 출금액', f'{wth_sum:,.0f}원')
      sm4.metric('순입금액 (입금-출금)', f'{net_sum:,.0f}원', delta=f'{net_sum:,.0f}원' if net_sum != 0 else None)

      st.write('---')
      st.markdown('##### 📝 입출금 내역 웹 직접 수정 (Data Editor)')
      st.caption(
          '💡 테이블 셀을 더블클릭하여 **거래일자, 계좌번호, 구분(입금/출금), 금액, 비고**를 웹에서 직접 수정할 수 있습니다. '
          '하단 빈 행에 입력하여 신규 추가하거나, 행을 선택(체크) 후 Delete 키로 삭제할 수 있습니다. '
          '수정 후 아래 **[💾 웹 수정사항 데이터베이스에 일괄 반영]** 버튼을 반드시 클릭해 주세요.'
      )

      editor_df = filtered_cf[['id', 'trans_date', 'whose', 'account_type_group', 'broker', 'account_num', 'flow_type', 'amount_clean', 'note']].copy()
      editor_df = editor_df.rename(columns={
          'amount_clean': 'amount',
          'account_type_group': '계좌유형'
      })

      # 선택 가능한 모든 계좌번호 목록
      all_possible_acc_nums = sorted(list(set(registered_acc_nums).union(set(cf_df['account_num'].astype(str)))))

      edited_cf = st.data_editor(
          editor_df,
          key='cash_flow_data_editor',
          use_container_width=True,
          num_rows='dynamic',
          disabled=['id', 'whose', '계좌유형', 'broker'],
          column_config={
              'id': st.column_config.NumberColumn('ID', help='고유 식별자 (수정 불가)', disabled=True),
              'trans_date': st.column_config.TextColumn('거래일자 (YYYY-MM-DD)', required=True),
              'whose': st.column_config.TextColumn('WHOSE (참조)'),
              '계좌유형': st.column_config.TextColumn('계좌유형 (참조)'),
              'broker': st.column_config.TextColumn('증권사 (참조)'),
              'account_num': st.column_config.SelectboxColumn('계좌번호', options=all_possible_acc_nums, required=True),
              'flow_type': st.column_config.SelectboxColumn('구분', options=['입금', '출금'], required=True),
              'amount': st.column_config.NumberColumn('금액 (원)', format='%,.0f', required=True),
              'note': st.column_config.TextColumn('비고'),
          }
      )

      btn_cf_col1, btn_cf_col2 = st.columns([3, 2])
      with btn_cf_col1:
        if st.button('💾 웹 수정사항 데이터베이스에 일괄 반영', type='primary'):
          orig_cf_ids = set(filtered_cf['id'].dropna().astype(int))
          current_cf_ids = set(pd.to_numeric(edited_cf['id'], errors='coerce').dropna().astype(int))
          deleted_cf_ids = orig_cf_ids - current_cf_ids

          conn = get_connection()
          c = conn.cursor()
          # 삭제된 행 처리
          for del_id in deleted_cf_ids:
            c.execute('DELETE FROM cash_flow WHERE id = ?', (del_id,))

          # 수정 및 추가 행 처리
          for _, e_row in edited_cf.iterrows():
            raw_id = e_row.get('id')
            row_id = int(raw_id) if pd.notna(raw_id) and str(raw_id).strip() != '' and str(raw_id).strip().isdigit() else None
            t_date = str(e_row.get('trans_date', '')).strip()
            acc = str(e_row.get('account_num', '')).strip()
            f_type = normalize_cash_flow_type(e_row.get('flow_type', ''))
            amt = parse_cash_flow_amount(e_row.get('amount', 0))
            note = str(e_row.get('note', '')).strip() if pd.notna(e_row.get('note')) else ''

            if not t_date or not acc or not f_type or pd.isna(amt):
              continue

            if row_id and row_id in orig_cf_ids:
              c.execute(
                  'UPDATE cash_flow SET trans_date=?, account_num=?, flow_type=?, amount=?, note=? WHERE id=?',
                  (t_date, acc, f_type, amt, note, row_id)
              )
            else:
              c.execute(
                  'INSERT INTO cash_flow (trans_date, account_num, flow_type, amount, note) VALUES (?, ?, ?, ?, ?)',
                  (t_date, acc, f_type, amt, note)
              )
          conn.commit()
          conn.close()
          st.success('✅ 모든 입출금 내역 수정/추가/삭제 사항이 데이터베이스에 저장되었습니다!')
          st.rerun()

      with btn_cf_col2:
        if not filtered_cf.empty:
          del_target_id = st.selectbox(
              '특정 ID 내역 바로 삭제',
              options=filtered_cf['id'].astype(int).tolist(),
              format_func=lambda rid: f"ID {rid} | {filtered_cf.loc[filtered_cf['id']==rid, 'trans_date'].iloc[0]} | {filtered_cf.loc[filtered_cf['id']==rid, 'flow_type'].iloc[0]} | {filtered_cf.loc[filtered_cf['id']==rid, 'amount_clean'].iloc[0]:,.0f}원 ({filtered_cf.loc[filtered_cf['id']==rid, 'account_num'].iloc[0]})",
              key='cf_direct_del_selector'
          )
          if st.button('🗑️ 선택한 ID 삭제'):
            conn = get_connection()
            c = conn.cursor()
            c.execute('DELETE FROM cash_flow WHERE id = ?', (int(del_target_id),))
            conn.commit()
            conn.close()
            st.success(f'ID {del_target_id} 입출금 내역이 삭제되었습니다.')
            st.rerun()

  with tab3:
    st.subheader('BJ 계좌별 2025년 12월 31일 최종 평가금액')
    st.caption(
        '입력한 금액은 BJ 계좌의 올해 누적 평가손익 기준으로만 사용됩니다. '
        'SH 계좌와 Category 4·보유 항목별 집계에는 적용되지 않습니다. '
        '0원은 미등록으로 처리됩니다.'
    )
    conn = get_connection()
    baseline_accounts = pd.read_sql(
        'SELECT DISTINCT broker, account_num, whose FROM portfolio', conn
    )
    existing_year_end_values = pd.read_sql(
        'SELECT account_num, baseline_amount FROM account_year_end_value', conn
    )
    conn.close()

    baseline_accounts = baseline_accounts.drop_duplicates(
        subset=['account_num']
    ).copy()
    baseline_accounts['account_num'] = baseline_accounts['account_num'].astype(str)
    baseline_accounts['resolved_whose'] = baseline_accounts.apply(
        lambda row: resolve_account_whose(
            row['account_num'], row['whose'], row['broker'], alias_map
        ),
        axis=1,
    )
    baseline_accounts = baseline_accounts[
        baseline_accounts['resolved_whose'] == 'BJ'
    ]
    baseline_values = dict(zip(
        existing_year_end_values['account_num'].astype(str),
        existing_year_end_values['baseline_amount'],
    ))

    if baseline_accounts.empty:
      st.info('등록된 BJ 계좌가 없습니다.')
    else:
      with st.form('bj_year_end_value_form'):
        account_values = {}
        for _, row in baseline_accounts.iterrows():
          account_num = row['account_num']
          broker = row['broker'] if pd.notna(row['broker']) else '증권사 미지정'
          alias = alias_map.get(account_num, '')
          account_label = (
              f'[{broker}] {account_num} ({alias})' if alias
              else f'[{broker}] {account_num}'
          )
          account_values[account_num] = (
              broker,
              st.number_input(
                  f'{account_label} 평가금액 (원)',
                  min_value=0.0,
                  value=float(baseline_values.get(account_num, 0.0)),
                  step=100000.0,
                  format='%.0f',
                  key=f'bj_year_end_value_{account_num}',
              ),
          )

        save_year_end_values = st.form_submit_button('💾 BJ 기준 평가액 저장')
        if save_year_end_values:
          conn = get_connection()
          cursor = conn.cursor()
          for account_num, (broker, amount) in account_values.items():
            if amount == 0:
              cursor.execute(
                  'DELETE FROM account_year_end_value WHERE account_num = ?',
                  (account_num,),
              )
            else:
              cursor.execute(
                  '''INSERT OR REPLACE INTO account_year_end_value
                     (account_num, broker, baseline_date, baseline_amount)
                     VALUES (?, ?, ?, ?)''',
                  (
                      account_num,
                      broker,
                      BJ_YEAR_END_BASELINE_DATE,
                      amount,
                  ),
              )
          conn.commit()
          conn.close()
          st.success('BJ 계좌별 2025년 말 평가금액을 저장했습니다.')
          st.rerun()

# -----------------------------------------------------------------------------
# 메뉴 5: 등록 데이터 조회 및 웹 수정
# -----------------------------------------------------------------------------
elif menu == '등록 데이터 조회 및 웹 수정':
  st.header('📝 데이터베이스 상세 조회 및 수정')

  conn = get_connection()
  pf_full = pd.read_sql('SELECT * FROM portfolio', conn)
  conn.close()

  if pf_full.empty:
    st.warning('등록된 포트폴리오 데이터가 없습니다.')
  else:
    st.subheader('📌 보유 포트폴리오 수정 (Data Editor)')
    dates = sorted(pf_full['record_date'].dropna().unique(), reverse=True)
    sel_date = st.selectbox('조회 및 수정할 기준일자 선택', options=dates)

    filtered_edit_df = pf_full[pf_full['record_date'] == sel_date].copy()

    edited_df = st.data_editor(
        filtered_edit_df,
        num_rows='dynamic',
        use_container_width=True,
        key='portfolio_editor',
    )

    if st.button('💾 수정 사항 데이터베이스에 반영'):
      conn = get_connection()
      c = conn.cursor()
      c.execute('DELETE FROM portfolio WHERE record_date = ?', (sel_date,))
      conn.commit()
      conn.close()

      save_cols = [c for c in edited_df.columns if c != 'id']
      to_save = edited_df[save_cols].copy()

      conn = get_connection()
      to_save.to_sql('portfolio', conn, if_exists='append', index=False)
      conn.commit()
      conn.close()

      # 수정된 포트폴리오의 분류 항목을 기존 DB 전체에도 일괄 동기화
      sync_latest_categories_to_portfolio()

      st.success('데이터베이스 수정 사항이 성공적으로 업데이트되었습니다!')
      st.rerun()

# -----------------------------------------------------------------------------
# 메뉴 6: 데이터 백업 및 복구
# -----------------------------------------------------------------------------
elif menu == '데이터 백업 및 복구':
  st.header('💾 DB 데이터 백업 및 복구')

  st.info('💡 백업 파일에는 포트폴리오 내역, 최초 원금, BJ 계좌별 2025년 말 평가액, 입출금 기록, 계좌 별칭 및 **종목 분류 마스터 정보(item_category)**가 모두 포함되어 함께 백업 및 복원됩니다.')
  st.subheader('📤 데이터베이스 파일 백업 (다운로드)')
  if os.path.exists(DB_FILE):
    with open(DB_FILE, 'rb') as f:
      db_bytes = f.read()

    st.download_button(
        label='💾 SQLite DB 파일 백업 다운로드 (.db)',
        data=db_bytes,
        file_name=f"asset_tracker_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db",
        mime='application/octet-stream',
    )
  else:
    st.error('데이터베이스 파일이 존재하지 않습니다.')

  st.write('---')
  st.subheader('📥 데이터베이스 파일 복구 (업로드)')
  uploaded_db = st.file_uploader(
      '복구할 SQLite DB 파일(.db)을 선택하세요', type=['db']
  )

  if uploaded_db is not None:
    if st.button('⚠️ DB 파일 덮어쓰기 복구 실행'):
      try:
        with open(DB_FILE, 'wb') as f:
          f.write(uploaded_db.getbuffer())
        st.success('데이터베이스 복구가 성공적으로 완료되었습니다! 페이지를 새로고침 해주세요.')
        st.rerun()
      except Exception as e:
        st.error(f'DB 복구 작업 실패: {e}')
