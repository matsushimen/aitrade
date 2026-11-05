import sys
import contextlib
import io
from unittest.mock import patch
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from collect_bitbank import BAR_MS
from paper_quotes import apply_quote, live_decisions, quote_data, replay, request_halt
from paper_trade import atomic_json
from trading_config import TradingConfig
from test_trading import START, candles


class QuotePaperTests(unittest.TestCase):
    def setUp(self):
        self.config=replace(TradingConfig(),fast_window=1,slow_window=2)
        self.rows=candles([100,90,100])
        self.timestamp=START+3*BAR_MS+1000

    def payload(self,timestamp=None,bid='99',ask='101'):
        return {'success':1,'data':{'timestamp':timestamp or self.timestamp,'buy':bid,'sell':ask}}

    def test_observed_ask_entry_and_bid_stop(self):
        state=apply_quote(None,self.rows,self.payload(),self.config,self.timestamp)
        buy=[e for e in state['events'] if e['event']=='fill_buy'][0]
        self.assertEqual(buy['ask_jpy'],'101')
        self.assertEqual(state['last_quote_ms'],self.timestamp)
        self.assertGreater(D(state['quantity_btc']),0)
        self.assertLessEqual(-D(buy['cash_flow_jpy']),5000)
        duplicate=apply_quote(state,self.rows,self.payload(),self.config,self.timestamp+1000)
        self.assertEqual(state,duplicate)
        stopped=apply_quote(state,self.rows,self.payload(self.timestamp+10000,bid='90',ask='91'),self.config,self.timestamp+10000)
        self.assertEqual(stopped['trades'][0]['exit_reason'],'stop_observed_quote')
        self.assertEqual(D(stopped['quantity_btc']),0)
        self.assertEqual(D(stopped['cash_jpy'])-10000,D(stopped['trades'][0]['pnl_jpy']))
        self.assertEqual(replay(stopped),stopped)

    def test_stale_quotes_gap_and_revised_candles_rejected(self):
        with self.assertRaises(ValueError):
            quote_data(self.payload(),self.timestamp+91000,self.config)
        with self.assertRaises(ValueError):
            quote_data(self.payload(bid='102',ask='101'),self.timestamp,self.config)
        with self.assertRaises(ValueError):
            apply_quote(None,self.rows[:-1],self.payload(),self.config,self.timestamp)
        state=apply_quote(None,self.rows,self.payload(),self.config,self.timestamp)
        revised=self.rows[:-1]+[(self.rows[-1][0],'101','101','101','101','1')]
        with self.assertRaises(ValueError):
            apply_quote(state,revised,self.payload(self.timestamp+1000),self.config,self.timestamp+1000)

    def test_revision_of_older_sma_candle_is_also_rejected(self):
        state=apply_quote(None,self.rows,self.payload(),self.config,self.timestamp)
        revised=list(self.rows)
        revised[0]=(revised[0][0],'101','101','101','101','1')
        with self.assertRaisesRegex(ValueError,'candle changed'):
            apply_quote(state,revised,self.payload(self.timestamp+1000),self.config,self.timestamp+1000)
        self.assertEqual(replay(state),state)

    def test_late_and_llm_missing_signal_skips(self):
        late=self.timestamp+100000
        state=apply_quote(None,self.rows,self.payload(late),self.config,late)
        self.assertEqual(D(state['quantity_btc']),0)
        self.assertTrue(any(e['reason']=='late_signal' for e in state['events']))
        blocked=apply_quote(None,self.rows,self.payload(),self.config,self.timestamp,decisions={})
        self.assertEqual(D(blocked['quantity_btc']),0)
        self.assertEqual(replay(blocked),blocked)

    def test_missing_llm_log_blocks_buys_without_blocking_stop(self):
        from llm_assist import PROMPT_HASH
        decision={'asof_ms':START,'available_ms':START+BAR_MS,'regime':'neutral',
                  'summary':'fixture','model':'fixture','prompt_sha256':PROMPT_HASH}
        state=apply_quote(None,self.rows,self.payload(),self.config,self.timestamp,
                          decisions={START:decision})
        self.assertGreater(D(state['quantity_btc']),0)
        with patch('paper_quotes.load_decisions',side_effect=ValueError('bad JSON')), contextlib.redirect_stderr(io.StringIO()):
            gate=live_decisions(Path('unused.jsonl'))
        self.assertEqual(gate,{})
        stopped=apply_quote(state,self.rows,self.payload(self.timestamp+2000,bid='90',ask='91'),
                            self.config,self.timestamp+2000,decisions=gate)
        self.assertEqual(stopped['trades'][0]['exit_reason'],'stop_observed_quote')
        self.assertEqual(D(stopped['quantity_btc']),0)
        self.assertEqual(replay(stopped),stopped)
        fresh=apply_quote(None,self.rows,self.payload(),self.config,self.timestamp,decisions=gate)
        self.assertEqual(D(fresh['quantity_btc']),0)

    def test_halt_persists_and_exits_at_next_fresh_quote(self):
        state=apply_quote(None,self.rows,self.payload(),self.config,self.timestamp)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'quotes.json'
            atomic_json(path,state)
            request_halt(path,self.config)
            import json
            saved=json.loads(path.read_text())
            self.assertTrue(saved['halted'])
            result=apply_quote(saved,self.rows,self.payload(self.timestamp+2000),self.config,self.timestamp+2000)
            self.assertEqual(result['trades'][0]['exit_reason'],'manual_halt')
            self.assertEqual(D(result['quantity_btc']),0)


if __name__=='__main__':
    unittest.main()
