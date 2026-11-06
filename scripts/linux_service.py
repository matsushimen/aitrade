"""Run aitrade roles for the supplied Linux user services (paper trading only)."""
import argparse
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta

from collect_bitbank import BAR_MS, JST, ROOT
from trading_config import TradingConfig


def commands(role, environ=None, now_ms=None):
    environ = os.environ if environ is None else environ
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    config_path = ROOT / 'config/baseline.json'
    python = sys.executable
    def script(name):
        return [python, str(ROOT / 'scripts' / name)]
    if role == 'collect':
        config = TradingConfig.load(config_path)
        end_ms = now_ms // BAR_MS * BAR_MS
        start_ms = end_ms - max(36, config.slow_window + 1) * BAR_MS
        first = datetime.fromtimestamp(start_ms / 1000, JST).date()
        last = datetime.fromtimestamp((end_ms - 1) / 1000, JST).date()
        result = []
        day = first
        while day <= last:
            result.append(script('collect_bitbank.py') + ['--date', day.strftime('%Y%m%d')])
            day += timedelta(days=1)
        return result
    if role == 'classify':
        base_url = environ.get('AITRADE_LLM_BASE_URL', '').strip()
        model = environ.get('AITRADE_LLM_MODEL', '').strip()
        if not base_url or not model:
            raise ValueError('Set AITRADE_LLM_BASE_URL and AITRADE_LLM_MODEL in the external environment file.')
        no_auth = environ.get('AITRADE_LLM_NO_AUTH', '0')
        if no_auth not in ('0', '1'):
            raise ValueError('AITRADE_LLM_NO_AUTH must be 0 or 1.')
        mode = environ.get('AITRADE_LLM_RESPONSE_FORMAT', 'json_object')
        if mode not in ('json_schema', 'json_object', 'text'):
            raise ValueError('Invalid AITRADE_LLM_RESPONSE_FORMAT.')
        command = script('llm_assist.py') + [
            '--db', str(ROOT / 'data/aitrade.sqlite3'), '--base-url', base_url,
            '--model', model, '--response-format', mode,
            '--output', str(ROOT / 'data/llm_decisions.jsonl')]
        command += ['--no-auth'] if no_auth == '1' else ['--api-key-env', 'LLM_API_KEY']
        return [command]
    assisted = role in ('paper-llm', 'halt-llm')
    state = ROOT / 'data' / ('paper_llm.json' if assisted else 'paper_baseline.json')
    command = script('paper_quotes.py') + ['--config', str(config_path), '--state', str(state)]
    if role in ('halt', 'halt-llm'):
        command += ['--halt']
    else:
        command += ['--watch']
    if assisted:
        command += ['--decisions', str(ROOT / 'data/llm_decisions.jsonl')]
    return [command]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('role', choices=('collect', 'classify', 'paper', 'paper-llm', 'halt', 'halt-llm'))
    args = parser.parse_args()
    try:
        for command in commands(args.role):
            subprocess.run(command, cwd=ROOT, check=True)
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f'Linux service failed: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
