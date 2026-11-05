"""Real-time paper market orders at observed public bid/ask; never sends orders."""
import argparse
import fcntl
import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, time as day_time, timedelta
from decimal import Decimal as D, InvalidOperation, ROUND_DOWN
from pathlib import Path

from collect_bitbank import BAR_MS, BASE_URL, JST, ROOT, fetch_json
from export_candles import read_candles, validate_candles
from llm_assist import load_decisions, select_decision
from storage import atomic_json, require_state
from trading_config import TradingConfig


def validate_quote_state(state):
    require_state(state, 'observed_bid_ask', (
        'config', 'llm_enabled', 'observations', 'cash_jpy', 'quantity_btc',
        'entry_cost_jpy', 'entry_ms', 'entry_session', 'stop_price_jpy',
        'session', 'session_pnl_jpy', 'peak_equity_jpy', 'max_drawdown_jpy',
        'halted', 'last_signal_ms', 'events', 'trades', 'last_quote_ms', 'last_candle'))


def live_decisions(path):
    if path is None:
        return None
    try:
        return load_decisions(path)
    except (OSError, ValueError) as exc:
        # Keep exits/stop processing active; an empty gate forbids new buys.
        print(f'LLM decisions unavailable; new entries blocked: {exc}', file=sys.stderr)
        return {}


def quote_data(payload, now_ms, config):
    try:
        if payload['success'] != 1:
            raise ValueError('API error.')
        data = payload['data']
        timestamp = data['timestamp']
        bid, ask = D(data['buy']), D(data['sell'])
        if type(timestamp) is not int or not (0 <= now_ms-timestamp <= config.max_feed_age_seconds*1000):
            raise ValueError('Stale or future quote timestamp.')
        if not bid.is_finite() or not ask.is_finite() or not 0 < bid <= ask:
            raise ValueError('Invalid bid/ask.')
        return timestamp, bid, ask
    except (KeyError, TypeError, InvalidOperation) as exc:
        raise ValueError('Malformed ticker response.') from exc


def apply_quote(state, rows, payload, config, now_ms, decisions=None):
    if state is not None:
        validate_quote_state(state)
    timestamp, bid, ask = quote_data(payload, now_ms, config)
    end_ms = timestamp//BAR_MS*BAR_MS
    if len(rows) < config.slow_window+1:
        raise ValueError('Not enough completed candles for SMA crossover.')
    rows = rows[-config.slow_window-1:]
    validate_candles(rows, end_ms-(config.slow_window+1)*BAR_MS, end_ms)
    if state is not None and state['llm_enabled'] != (decisions is not None):
        raise ValueError('LLM mode changed; use a new quote paper state.')
    if state is not None and state['config'] != config.to_dict():
        raise ValueError('Configuration changed; use a new quote paper state.')
    if state is not None and timestamp <= state['last_quote_ms']:
        return state
    state = json.loads(json.dumps(state)) if state is not None else {
        'version': 1, 'mode': 'observed_bid_ask', 'config': config.to_dict(),
        'llm_enabled': decisions is not None, 'observations': [],
        'cash_jpy': str(config.starting_cash_jpy), 'quantity_btc': '0',
        'entry_cost_jpy': '0', 'entry_ms': None, 'entry_session': None,
        'stop_price_jpy': '0', 'session': None, 'session_pnl_jpy': '0',
        'peak_equity_jpy': str(config.starting_cash_jpy), 'max_drawdown_jpy': '0',
        'halted': False, 'last_signal_ms': None, 'events': [], 'trades': [],
        'last_quote_ms': 0, 'last_candle': None}
    # Check the whole overlapping SMA window, not just the last signal candle.
    previous_rows = next((entry['rows'] for entry in reversed(state['observations'])
                          if entry['rows'] is not None), [])
    previous_by_time = {row[0]: list(row) for row in previous_rows}
    for row in rows:
        if row[0] in previous_by_time and list(row) != previous_by_time[row[0]]:
            raise ValueError('Previously observed signal candle changed.')
    bar_time = datetime.fromtimestamp(timestamp/1000,JST)
    session = bar_time.date().isoformat()
    if state['session'] != session:
        state['session'] = session
        state['session_pnl_jpy'] = '0'
    cash, quantity, entry_cost = map(D, (state['cash_jpy'],state['quantity_btc'],state['entry_cost_jpy']))
    sell_factor = (1-config.slippage_bps/D(10000))*(1-config.fee_bps/D(10000))
    buy_factor = (1+config.slippage_bps/D(10000))*(1+config.fee_bps/D(10000))
    closes=[D(row[4]) for row in rows]
    def spread(values):
        return sum(values[-config.fast_window:])*config.slow_window - sum(values[-config.slow_window:])*config.fast_window
    previous,current=spread(closes[:-1]),spread(closes[1:])
    signal = 1 if previous<=0<current else -1 if previous>=0>current else 0
    signal_ms=rows[-1][0]
    new_signal=state['last_signal_ms'] != signal_ms
    age=timestamp-end_ms
    fresh_signal=age <= config.max_feed_age_seconds*1000
    missed=state['last_signal_ms'] is not None and signal_ms-state['last_signal_ms']>BAR_MS
    at_end=bar_time.time()>=day_time(23,55)
    def event(kind, reason, **fields):
        item={'event':kind,'reason':reason,'quote_ms':timestamp,'recorded_at_ms':now_ms,
              'signal_ms':signal_ms,'bid_jpy':str(bid),'ask_jpy':str(ask),**fields}
        state['events'].append(item)
        return item
    exit_reason = None
    if quantity:
        if state['halted']:
            exit_reason='manual_halt'
        elif state['entry_session'] != session or at_end:
            exit_reason='session_end'
        elif bid<=D(state['stop_price_jpy']):
            exit_reason='stop_observed_quote'
        elif new_signal and signal==-1:
            exit_reason='cross_down'
    if exit_reason:
        proceeds=quantity*bid*sell_factor
        pnl=proceeds-entry_cost
        cash+=proceeds
        state['session_pnl_jpy']=str(D(state['session_pnl_jpy'])+pnl)
        event('fill_sell',exit_reason,quantity_btc=str(quantity),cash_flow_jpy=str(proceeds),pnl_jpy=str(pnl))
        state['trades'].append({'entry_ms':state['entry_ms'],'exit_ms':timestamp,'exit_reason':exit_reason,'pnl_jpy':str(pnl)})
        quantity=D(0)
    if new_signal and signal==1 and quantity==0 and exit_reason is None:
        decision=None if decisions is None else select_decision(decisions,timestamp)
        reason = ('manual_halt' if state['halted'] else 'session_end' if at_end else
                  'daily_loss' if D(state['session_pnl_jpy'])<=-config.daily_loss_jpy else
                  'missed_signal' if missed else 'late_signal' if not fresh_signal else
                  'llm_missing_or_risk_off' if decisions is not None and (decision is None or decision['regime']=='risk_off') else 'cross_up')
        event('entry_decision',reason,llm=decision)
        if reason=='cross_up':
            stop=ask*(1-config.stop_fraction)
            unit=ask*buy_factor
            risk=min(config.trade_risk_jpy,config.daily_loss_jpy+D(state['session_pnl_jpy']))
            quantity=min(min(cash,config.max_position_jpy)/unit,risk/(unit-stop*sell_factor)).quantize(D('.00000001'),rounding=ROUND_DOWN)
            if quantity>0:
                entry_cost=quantity*unit
                cash-=entry_cost
                state.update(entry_cost_jpy=str(entry_cost),entry_ms=timestamp,entry_session=session,stop_price_jpy=str(stop))
                event('fill_buy','cross_up',quantity_btc=str(quantity),cash_flow_jpy=str(-entry_cost),stop_price_jpy=str(stop))
    equity=cash+quantity*bid*sell_factor
    peak=max(D(state['peak_equity_jpy']),equity)
    state.update(cash_jpy=str(cash),quantity_btc=str(quantity),equity_jpy=str(equity),
                 net_pnl_jpy=str(equity-config.starting_cash_jpy),peak_equity_jpy=str(peak),
                 max_drawdown_jpy=str(max(D(state['max_drawdown_jpy']),peak-equity)),
                 last_quote_ms=timestamp,last_signal_ms=signal_ms,last_candle=list(rows[-1]))
    event('mark','observed_quote',equity_jpy=str(equity),quantity_btc=str(quantity))
    selected = None if decisions is None else select_decision(decisions,timestamp)
    state['observations'].append({'quote': {'success': 1, 'data': {
        'timestamp': timestamp, 'buy': str(bid), 'sell': str(ask)}},
        'received_ms': now_ms, 'rows': [list(row) for row in rows] if new_signal else None,
        'decision': selected, 'halted': state['halted']})
    return state


def replay(state):
    """Reproduce paper fills using only saved quotes, candles, and decisions."""
    validate_quote_state(state)
    config=TradingConfig.from_dict(state['config'])
    result=None
    rows=None
    for observation in state['observations']:
        if observation['rows'] is not None:
            rows=observation['rows']
        if result is not None:
            result['halted']=observation['halted']
        decision=observation['decision']
        decisions=({decision['asof_ms']:decision} if decision else {}) if state['llm_enabled'] else None
        result=apply_quote(result,rows,observation['quote'],config,observation['received_ms'],decisions)
    if result is None:
        raise ValueError('No quote observations to replay.')
    # A halt requested after the last quote has no fill until the next quote.
    result['halted']=state['halted']
    return result


def request_halt(path, config):
    with path.with_suffix(path.suffix+'.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        state=json.loads(path.read_text())
        validate_quote_state(state)
        if state['config'] != config.to_dict():
            raise ValueError('Configuration changed.')
        state['halted']=True
        atomic_json(path,state)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state',type=Path,required=True)
    parser.add_argument('--config',type=Path)
    parser.add_argument('--db',type=Path,default=ROOT/'data/aitrade.sqlite3')
    parser.add_argument('--decisions',type=Path)
    parser.add_argument('--watch',action='store_true')
    parser.add_argument('--replay',action='store_true',help='Verify saved observations offline, without network calls.')
    parser.add_argument('--collect',action='store_true',help='Refresh required public candles once per bar.')
    parser.add_argument('--halt',action='store_true',help='Persist a halt request, then exit at the next fresh observed quote.')
    parser.add_argument('--interval',type=int,default=15)
    args=parser.parse_args()
    if args.interval<1 or any(p and args.state.resolve()==p.resolve() for p in (args.db,args.config,args.decisions)):
        parser.error('Invalid interval or state path conflicts with source.')
    args.state.parent.mkdir(parents=True,exist_ok=True)
    try:
        config=TradingConfig.load(args.config)
        if args.replay:
            saved=json.loads(args.state.read_text())
            result=replay(saved)
            if result != saved:
                raise ValueError('Recorded state does not match quote replay.')
            print(json.dumps({'replay': 'matched', 'quote_count': len(saved['observations']), 'trade_count': len(saved['trades']), 'net_pnl_jpy': saved['net_pnl_jpy']}))
            return 0
        if args.halt:
            request_halt(args.state,config)
        refreshed_end = None
        while True:
            payload=fetch_json(f'{BASE_URL}/btc_jpy/ticker')
            now_ms=int(time.time()*1000)
            timestamp,_,_=quote_data(payload,now_ms,config)
            end_ms=timestamp//BAR_MS*BAR_MS
            if args.collect and refreshed_end != end_ms:
                first_day=datetime.fromtimestamp((end_ms-(config.slow_window+1)*BAR_MS)/1000,JST).date()
                last_day=datetime.fromtimestamp((end_ms-1)/1000,JST).date()
                day=first_day
                while day<=last_day:
                    subprocess.run([sys.executable,str(ROOT/'scripts/collect_bitbank.py'),'--date',day.strftime('%Y%m%d')],check=True)
                    day+=timedelta(days=1)
                refreshed_end=end_ms
            rows=read_candles(args.db,end_ms-(config.slow_window+1)*BAR_MS,end_ms)
            now_ms=int(time.time()*1000)
            decisions=live_decisions(args.decisions)
            with args.state.with_suffix(args.state.suffix+'.lock').open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX)
                old=json.loads(args.state.read_text()) if args.state.exists() else None
                state=apply_quote(old,rows,payload,config,now_ms,decisions)
                atomic_json(args.state,state)
            print(json.dumps({key:state[key] for key in ('mode','cash_jpy','quantity_btc','equity_jpy','net_pnl_jpy','halted')}),flush=True)
            if not args.watch:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0
    except (OSError,ValueError,sqlite3.Error,subprocess.CalledProcessError) as exc:
        print(f'Quote paper stopped: {exc}; existing positions remain in the saved state.',file=sys.stderr)
        return 1
    return 0


if __name__=='__main__':
    raise SystemExit(main())
