import sys
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from linux_service import commands


class LinuxServiceTests(unittest.TestCase):
    def test_collection_crosses_jst_midnight_for_warmup(self):
        now = datetime(2026, 11, 6, 1, 0, tzinfo=ZoneInfo('Asia/Tokyo'))
        result = commands('collect', environ={}, now_ms=int(now.timestamp() * 1000))
        self.assertEqual([cmd[-1] for cmd in result], ['20261105', '20261106'])
        now = datetime(2026, 11, 6, 12, 0, tzinfo=ZoneInfo('Asia/Tokyo'))
        result = commands('collect', environ={}, now_ms=int(now.timestamp() * 1000))
        self.assertEqual([cmd[-1] for cmd in result], ['20261106'])

    def test_classify_requires_explicit_endpoint_and_passes_no_secret_argument(self):
        with self.assertRaises(ValueError):
            commands('classify', environ={})
        env = {'AITRADE_LLM_BASE_URL': 'http://localhost:8000/v1',
               'AITRADE_LLM_MODEL': 'local-model', 'LLM_API_KEY': 'secret-fixture'}
        command = commands('classify', environ=env)[0]
        self.assertIn('http://localhost:8000/v1', command)
        self.assertIn('local-model', command)
        self.assertIn('LLM_API_KEY', command)
        self.assertNotIn('secret-fixture', command)
        env['AITRADE_LLM_NO_AUTH'] = '1'
        self.assertIn('--no-auth', commands('classify', environ=env)[0])
        for key, value in [('AITRADE_LLM_NO_AUTH', 'yes'), ('AITRADE_LLM_RESPONSE_FORMAT', 'arbitrary')]:
            with self.assertRaises(ValueError):
                commands('classify', environ=dict(env, **{key: value}))

    def test_paper_modes_are_separate_and_stop_uses_same_state(self):
        baseline = commands('paper')[0]
        assisted = commands('paper-llm')[0]
        self.assertIn('--watch', baseline)
        self.assertNotIn('--decisions', baseline)
        self.assertIn('--decisions', assisted)
        for role, command in [('halt', baseline), ('halt-llm', assisted)]:
            halt = commands(role)[0]
            self.assertIn('--halt', halt)
            self.assertNotIn('--watch', halt)
            self.assertEqual(halt[halt.index('--state') + 1], command[command.index('--state') + 1])


if __name__ == '__main__':
    unittest.main()
