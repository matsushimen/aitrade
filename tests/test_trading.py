import json
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from backtest_baseline import run_backtest
from collect_bitbank import BAR_MS, JST
from evaluate import evaluate
from llm_assist import PROMPT_HASH, select_decision, validate_decision
from paper_trade import advance
from trading_config import TradingConfig

START = int(datetime(2026, 1, 1, tzinfo=JST).timestamp())*1000


def candles(prices):
    return [(START+i*BAR_MS, str(p), str(p), str(p), str(p), '1') for i,p in enumerate(prices)]


def simulate(rows, config=None, **kwargs):
    config = config or replace(TradingConfig(), fast_window=1, slow_window=2)
    return run_backtest(rows, config.fee_bps/D(10000), config.slippage_bps/D(10000), config=config, **kwargs)


class TradingTests(unittest.TestCase):
    def test_defaults_and_cost_compatibility(self):
        config = TradingConfig.load(Path('config/baseline.json'))
        self.assertEqual(config, TradingConfig())
        with self.assertRaises(ValueError):
            replace(config, max_position_jpy=D('10001'))
        with self.assertRaises(ValueError):
            replace(config, fee_bps=D('NaN'))
        with self.assertRaises(ValueError):
            replace(config, fast_window=True)

    def test_next_open_and_both_sides_cost(self):
        rows = candles([100, 90, 100, 110, 100, 100])
        report = simulate(rows)
        buy,sell = [e for e in report['events'] if e['event'].startswith('fill')][:2]
        self.assertEqual(buy['bar_jst'], datetime.fromtimestamp((START+3*BAR_MS)/1000,JST).isoformat())
        self.assertEqual(buy['reference_price_jpy'], '110')
        self.assertLessEqual(-D(buy['cash_flow_jpy']),5000)
        q = D(buy['quantity_btc'])
        self.assertEqual(-D(buy['cash_flow_jpy']), q*110*D('1.0005')*D('1.0015'))
        self.assertEqual(D(sell['cash_flow_jpy']), q*D(sell['reference_price_jpy'])*D('.9995')*D('.9985'))
        self.assertEqual(D(sell['pnl_jpy']), D(buy['cash_flow_jpy'])+D(sell['cash_flow_jpy']))

    def test_gap_stop_and_daily_loss_blocks_reentry(self):
        config = replace(TradingConfig(), fast_window=1, slow_window=2, trade_risk_jpy=D(10), daily_loss_jpy=D(20))
        report=simulate(candles([100,90,100,100,80,100,100,90,100,100]),config)
        self.assertEqual(report['trades'][0]['exit_reason'],'stop_at_open')
        self.assertLess(D(report['trades'][0]['pnl_jpy']),-20)
        self.assertEqual(report['trade_count'],1)
        self.assertTrue(any(e.get('reason')=='daily_loss' for e in report['events']))

    def test_intrabar_stop_and_risk_cap(self):
        rows=candles([100,90,100,100])
        rows[3]=(rows[3][0],'100','100','90','100','1')
        report=simulate(rows)
        self.assertEqual(report['trades'][0]['exit_reason'],'stop_intrabar')
        self.assertGreaterEqual(D(report['trades'][0]['pnl_jpy']),-100)

    def test_spread_reduces_return(self):
        rows=candles([100,90,100,110,110,110])
        a=simulate(rows); b=simulate(rows,replace(TradingConfig(),fast_window=1,slow_window=2,spread_bps=D(10)))
        self.assertLess(D(b['equity_jpy']),D(a['equity_jpy']))

    def test_session_end_and_balance_reconciliation(self):
        rows=candles([100]*282+[90,100,100,100,100,100])
        report=simulate(rows)
        self.assertEqual(report['trades'][-1]['exit_reason'],'session_end')
        self.assertTrue(report['trades'][-1]['exit_bar_jst'].endswith('23:55:00+09:00'))
        self.assertEqual(D(report['ending_cash_jpy'])-10000,sum(D(t['pnl_jpy']) for t in report['trades']))

    def test_llm_causality_and_invalid_output(self):
        decision={'asof_ms':START+BAR_MS,'available_ms':START+3*BAR_MS+1,'regime':'neutral','summary':'test','model':'test-only','prompt_sha256':PROMPT_HASH}
        validate_decision(decision)
        self.assertIsNone(select_decision({1:decision},START+3*BAR_MS))
        self.assertIsNotNone(select_decision({1:decision},START+4*BAR_MS))
        for field,value in [('available_ms',START),('regime','buy'),('summary','x'*501),('prompt_sha256','fake')]:
            with self.assertRaises(ValueError):
                validate_decision(dict(decision,**{field:value}))
        rows=candles([100,90,100,110,100,100])
        self.assertEqual(simulate(rows,decisions={})['trade_count'],0)
        self.assertEqual(simulate(rows,decisions={1:decision})['trade_count'],0)

    def test_same_data_neutral_matches_and_risk_off_vetoes(self):
        rows=candles([100,90,100,110,100,100])
        decision={'asof_ms':START,'available_ms':START+BAR_MS,'regime':'neutral','summary':'synthetic test fixture','model':'test-only','prompt_sha256':PROMPT_HASH}
        baseline=simulate(rows)
        assisted=simulate(rows,decisions={START:decision})
        self.assertEqual(baseline['trades'],assisted['trades'])
        self.assertEqual(baseline['net_pnl_jpy'],assisted['net_pnl_jpy'])
        self.assertEqual(simulate(rows,decisions={START:dict(decision,regime='risk_off')})['trade_count'],0)

    def test_disjoint_evaluation_required(self):
        rows=candles([100,90,100,100])
        with self.assertRaises(ValueError):
            evaluate(rows,rows,TradingConfig())

    def test_paper_restart_replay_and_atomic_rejection(self):
        config=replace(TradingConfig(),fast_window=1,slow_window=2)
        rows=candles([100,90,100,100,100,90,100,100])
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'paper.json'
            advance(path, rows[:4],config,now_ms=START+20*BAR_MS)
            state=advance(path, rows,config,now_ms=START+21*BAR_MS)
            self.assertEqual(state['report']['trades'],simulate(rows,config)['trades'])
            before=path.read_bytes()
            advance(path, rows,config,now_ms=START+22*BAR_MS)
            self.assertEqual(before,path.read_bytes())
            for bad in (rows[:3],rows[:4]+rows[5:]):
                with self.assertRaises(ValueError):
                    advance(path,bad,config,now_ms=START+22*BAR_MS)
                self.assertEqual(before,path.read_bytes())
            with self.assertRaises(ValueError):
                advance(path,rows,replace(config,fee_bps=D(20)))
            with self.assertRaises(ValueError):
                advance(path,rows,config,now_ms=START+BAR_MS)

    def test_paper_manual_stop_is_persistent_and_not_retroactive(self):
        config=replace(TradingConfig(),fast_window=1,slow_window=2)
        rows=candles([100,90,100,100,100,90,100,100])
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'state.json'
            first=advance(path,rows[:4],config,now_ms=START+20*BAR_MS)
            self.assertGreater(D(first['report']['open_quantity_btc']),0)
            stopped=advance(path,rows[:4],config,halt=True,now_ms=START+20*BAR_MS)
            self.assertEqual(stopped['status'],'halt_requested_waiting_for_next_bar')
            final=advance(path,rows,config,now_ms=START+20*BAR_MS)
            self.assertEqual(final['report']['trades'][0]['exit_reason'],'manual_halt')
            self.assertEqual(final['report']['trade_count'],1)
            self.assertEqual(final['status'],'halted')
            self.assertEqual(D(final['report']['open_quantity_btc']),0)


if __name__=='__main__':
    unittest.main()
