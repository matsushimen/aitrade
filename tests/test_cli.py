"""Exercise public command boundaries with synthetic SQLite data, no network."""
import csv
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'scripts'


class CLITests(unittest.TestCase):
    def run_cli(self, script, *arguments, ok=True):
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / script), *map(str, arguments)],
            cwd=ROOT, text=True, capture_output=True, timeout=30,
            env={**os.environ, 'OPENAI_API_KEY': '', 'OPENAI_BASE_URL': ''},
        )
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def test_all_commands_have_help(self):
        for script in sorted(SCRIPTS.glob('*.py')):
            if script.name in ('trading_config.py', 'storage.py'):
                continue
            with self.subTest(script=script.name):
                self.assertIn('usage:', self.run_cli(script.name, '--help').stdout)

    def test_evaluation_rejects_malformed_csv_without_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'malformed.csv'
            path.write_text('open_time_ms,open,high,low,close,volume\n"unterminated\n',encoding='utf-8')
            result=self.run_cli('evaluate.py','--development',path,'--holdout',path,ok=False)
            self.assertIn('Evaluation failed:',result.stderr)
            self.assertNotIn('Traceback',result.stderr)

    def test_export_backtest_evaluate_and_restart(self):
        sys.path.insert(0, str(SCRIPTS))
        from collect_bitbank import ensure_schema

        start = int(datetime(2020, 1, 1, tzinfo=ZoneInfo('Asia/Tokyo')).timestamp()) * 1000
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            database = work / 'market.sqlite3'
            with sqlite3.connect(database) as connection:
                ensure_schema(connection)
                rows = []
                for i in range(576):
                    price = str(1000000 + (i % 48) * 1000)
                    rows.append(('bitbank', 'btc_jpy', '5min', start + i * 300000,
                                 price, price, price, price, '1', '2020-01-03T00:00:00Z'))
                connection.executemany('INSERT INTO candles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', rows)
            development, holdout = work / 'dev.csv', work / 'holdout.csv'
            for date, path in [('20200101', development), ('20200102', holdout)]:
                self.run_cli('export_candles.py', '--start', date, '--end', date,
                             '--db', database, '--output', path)
                with path.open(newline='') as stream:
                    self.assertEqual(len(list(csv.DictReader(stream))), 288)
            baseline = json.loads(self.run_cli('backtest_baseline.py', holdout,
                '--config', ROOT / 'config/baseline.json').stdout)
            self.assertGreater(baseline['trade_count'], 0)
            self.assertEqual(Decimal(baseline['ending_cash_jpy']) - 10000,
                             sum(Decimal(t['pnl_jpy']) for t in baseline['trades']))
            evaluation = json.loads(self.run_cli('evaluate.py', '--development', development,
                '--holdout', holdout, '--config', ROOT / 'config/baseline.json').stdout)
            self.assertEqual(evaluation['holdout']['baseline']['trades'], baseline['trades'])
            self.run_cli('evaluate.py', '--development', holdout, '--holdout', development, ok=False)
            state = work / 'paper.json'
            self.run_cli('paper_trade.py', '--csv', holdout, '--state', state,
                         '--until-ms', start + (288 + 144) * 300000)
            self.run_cli('paper_trade.py', '--csv', holdout, '--state', state)
            paper = json.loads(state.read_text())['report']
            self.assertEqual(paper['trades'], baseline['trades'])
            self.assertEqual(paper['net_pnl_jpy'], baseline['net_pnl_jpy'])
            # A failed export preserves the last valid output.
            original = development.read_bytes()
            with sqlite3.connect(database) as connection:
                connection.execute('DELETE FROM candles WHERE open_time_ms = ?', (start,))
            self.run_cli('export_candles.py', '--start', '20200101', '--end', '20200101',
                         '--db', database, '--output', development, ok=False)
            self.assertEqual(development.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
