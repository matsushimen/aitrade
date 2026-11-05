import contextlib
import io
import json
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import llm_assist
import storage
from paper_quotes import apply_quote, replay, request_halt
from paper_trade import advance
from test_trading import START, candles
from trading_config import TradingConfig


class PersistenceTests(unittest.TestCase):
    def test_same_candle_concurrent_classification_calls_api_once(self):
        response = {'choices': [{'message': {'content': '{"regime":"neutral","summary":"fixture"}'}}]}
        barrier = threading.Barrier(2)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'decisions.jsonl'
            def worker(_):
                barrier.wait(timeout=5)
                try:
                    return llm_assist.classify_and_save(path, candles([100] * 36),
                        'https://example.invalid/v1/chat/completions', 'fixture', 'json_object')
                except ValueError as exc:
                    return str(exc)
            def reply(*args, **kwargs):
                return contextlib.closing(io.BytesIO(json.dumps(response).encode()))
            with patch.object(llm_assist, 'urlopen', side_effect=reply) as api:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    results = list(executor.map(worker, range(2)))
            self.assertEqual(api.call_count, 1)
            self.assertEqual(sum(isinstance(item, dict) for item in results), 1)
            self.assertEqual(len(llm_assist.load_decisions(path)), 1)

    def test_failed_atomic_write_preserves_original_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            storage.atomic_json(path, {'original': True})
            before = path.read_bytes()
            with patch.object(storage.os, 'fsync', side_effect=OSError('disk failure')):
                with self.assertRaises(OSError):
                    storage.atomic_json(path, {'replacement': True})
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_wrong_paper_mode_is_rejected_before_state_changes(self):
        config = TradingConfig()
        rows = candles([100] * 40)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'paper.json'
            delayed = advance(path, rows, config, now_ms=START + 50 * 300000)
            before = path.read_bytes()
            for operation in (lambda: request_halt(path, config),
                              lambda: replay(delayed),
                              lambda: apply_quote(delayed, rows, {}, config, START)):
                with self.assertRaisesRegex(ValueError, 'incompatible'):
                    operation()
                self.assertEqual(path.read_bytes(), before)
            storage.atomic_json(path, {'version': 1, 'mode': 'observed_bid_ask'})
            before = path.read_bytes()
            with self.assertRaisesRegex(ValueError, 'incompatible'):
                advance(path, rows, config)
            self.assertEqual(path.read_bytes(), before)

    def test_truncated_state_rejected_without_traceback_keys(self):
        for state in ([], {}, {'version': 1, 'mode': 'observed_bid_ask'}):
            with self.assertRaisesRegex(ValueError, 'incompatible'):
                replay(state)


if __name__ == '__main__':
    unittest.main()
