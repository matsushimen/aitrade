import contextlib
import importlib.util
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('collector', Path(__file__).resolve().parents[1] / 'scripts/collect_bitbank.py')
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)


class CollectionTests(unittest.TestCase):
    def test_jst_day_combines_utc_responses_and_is_repeatable(self):
        start = int(datetime(2026, 11, 5, tzinfo=collector.JST).timestamp()) * 1000
        candles = [['100', '110', '90', '105', '1', start + i * collector.BAR_MS]
                   for i in range(-108, 468)]
        responses = [{'success': 1, 'data': {'candlestick': [
            {'type': '5min', 'ohlcv': candles[:288]}]}},
            {'success': 1, 'data': {'candlestick': [
                {'type': '5min', 'ohlcv': candles[288:]}]}}]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(collector, 'ROOT', root), patch.object(sys, 'argv', ['collect', '--date', '20261105']), patch.object(collector.time, 'time', return_value=(start + 1000 * collector.BAR_MS) / 1000), contextlib.redirect_stdout(io.StringIO()):
                for _ in range(2):
                    with patch.object(collector, 'fetch_json', side_effect=responses) as fetch:
                        self.assertEqual(collector.main(), 0)
                        self.assertEqual([call.args[0].rsplit('/', 1)[-1] for call in fetch.call_args_list], ['20261104', '20261105'])
            with sqlite3.connect(root / 'data/aitrade.sqlite3') as db:
                timestamps = [row[0] for row in db.execute('SELECT open_time_ms FROM candles ORDER BY open_time_ms')]
            self.assertEqual(timestamps, [start + i * collector.BAR_MS for i in range(288)])
            for day, payload in zip(('20261104', '20261105'), responses):
                self.assertEqual(json.loads((root / f'data/raw/bitbank/btc_jpy/5min/{day}.json').read_text()), payload)

    def test_current_jst_day_before_utc_midnight_skips_future_api_date(self):
        now = datetime(2026,11,6,1,0,tzinfo=collector.JST)
        class FrozenDatetime(datetime):
            @classmethod
            def now(cls,tz=None):
                return now.astimezone(tz or timezone.utc)
        start = int(datetime(2026,11,6,tzinfo=collector.JST).timestamp())*1000
        payload = {'success':1,'data':{'candlestick':[{'type':'5min','ohlcv':[
            ['100','100','100','100','1',start+i*collector.BAR_MS] for i in range(12)]}]}}
        with tempfile.TemporaryDirectory() as directory, patch.object(collector,'ROOT',Path(directory)), patch.object(collector,'datetime',FrozenDatetime), patch.object(collector.time,'time',return_value=now.timestamp()), patch.object(sys,'argv',['collect','--date','20261106']), patch.object(collector,'fetch_json',return_value=payload) as fetch, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(collector.main(),0)
            self.assertEqual(fetch.call_count,1)
            self.assertTrue(fetch.call_args.args[0].endswith('/20261105'))

    def test_malformed_api_candle_is_not_silently_discarded(self):
        payload={'success':1,'data':{'candlestick':[{'type':'5min','ohlcv':[['bad']]}]}}
        with tempfile.TemporaryDirectory() as directory, patch.object(collector,'ROOT',Path(directory)), patch.object(sys,'argv',['collect','--date','20261105']), patch.object(collector,'fetch_json',return_value=payload), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(collector.main(),1)
            self.assertFalse((Path(directory)/'data/aitrade.sqlite3').exists())


if __name__ == '__main__':
    unittest.main()
