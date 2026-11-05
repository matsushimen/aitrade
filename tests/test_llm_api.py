import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import llm_assist
from collect_bitbank import BAR_MS
from test_trading import START, candles


class LLMAPITests(unittest.TestCase):
    def test_api_output_validation_and_actual_receipt_time(self):
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)/'decisions.jsonl'
            response={'choices':[{'message':{'content':json.dumps({'regime':'neutral','summary':'test fixture, not a real model response'})}}]}
            argv=['llm','example.csv','--model','test-fixture','--output',str(output)]
            import backtest_baseline
            with patch.dict(os.environ,{'OPENAI_API_KEY':'test-fixture'}), patch.object(sys,'argv',argv), patch.object(backtest_baseline,'load_candles',return_value=candles([100]*288)), patch.object(llm_assist.time,'time',return_value=(START+300*BAR_MS)/1000), patch.object(llm_assist,'urlopen',return_value=contextlib.closing(io.BytesIO(json.dumps(response).encode()))) as api, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(llm_assist.main(),0)
                request=api.call_args.args[0]
                body=json.loads(request.data)
                self.assertTrue(body['response_format']['json_schema']['strict'])
            item=list(llm_assist.load_decisions(output).values())[0]
            self.assertEqual(item['available_ms'],START+300*BAR_MS)
            self.assertIsNone(llm_assist.select_decision({item['asof_ms']:item},START+288*BAR_MS))

    def test_compatible_endpoint_auth_and_output_modes(self):
        import backtest_baseline
        for mode in ('json_schema', 'json_object', 'text'):
            for no_auth in (False, True):
                with self.subTest(mode=mode, no_auth=no_auth), tempfile.TemporaryDirectory() as directory:
                    output=Path(directory)/'decisions.jsonl'
                    response={'choices':[{'message':{'content':'{"regime":"neutral","summary":"fixture"}'}}]}
                    argv=['llm','example.csv','--model','provider/model','--output',str(output),
                          '--base-url','https://llm.example/custom/v1/', '--api-key-env','TEST_LLM_KEY',
                          '--response-format',mode]+(['--no-auth'] if no_auth else [])
                    with patch.dict(os.environ,{'TEST_LLM_KEY':'custom-fixture','OPENAI_API_KEY':'must-not-use'}), patch.object(sys,'argv',argv), patch.object(backtest_baseline,'load_candles',return_value=candles([100]*288)), patch.object(llm_assist.time,'time',return_value=(START+300*BAR_MS)/1000), patch.object(llm_assist,'urlopen',return_value=contextlib.closing(io.BytesIO(json.dumps(response).encode()))) as api, contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(llm_assist.main(),0)
                    request=api.call_args.args[0]
                    self.assertEqual(request.full_url,'https://llm.example/custom/v1/chat/completions')
                    self.assertEqual(request.get_header('Authorization'),None if no_auth else 'Bearer custom-fixture')
                    body=json.loads(request.data)
                    self.assertEqual(body['model'],'provider/model')
                    if mode=='text':
                        self.assertNotIn('response_format',body)
                    else:
                        self.assertEqual(body['response_format']['type'],mode)
                    self.assertEqual(len(llm_assist.load_decisions(output)),1)

    def test_base_url_and_local_validation(self):
        self.assertEqual(llm_assist.completion_url('http://localhost:8000/v1/'),'http://localhost:8000/v1/chat/completions')
        self.assertEqual(llm_assist.completion_url('https://llm.example/chat/completions'),'https://llm.example/chat/completions')
        for value in ('file:///tmp/model','https://key@llm.example/v1','https://llm.example/v1?token=key'):
            with self.assertRaises(ValueError):
                llm_assist.completion_url(value)
        import backtest_baseline
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)/'bad.jsonl'
            response={'choices':[{'message':{'content':'{"regime":"buy","summary":"invalid"}'}}]}
            with patch.object(sys,'argv',['llm','example.csv','--model','local','--output',str(output),'--no-auth','--response-format','text']), patch.object(backtest_baseline,'load_candles',return_value=candles([100]*288)), patch.object(llm_assist,'urlopen',return_value=contextlib.closing(io.BytesIO(json.dumps(response).encode()))), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(llm_assist.main(),1)
            self.assertFalse(output.exists())

    def test_missing_key_makes_no_network_call(self):
        with patch.dict(os.environ,{},clear=True), patch.object(sys,'argv',['llm','example.csv','--model','test','--output','unused.jsonl']), patch.object(llm_assist,'urlopen') as api, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(llm_assist.main(),1)
            api.assert_not_called()


if __name__=='__main__':
    unittest.main()
