"""HTTP/session contracts with a deterministic test double, not model validation."""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

from fastapi.testclient import TestClient

from sg_guard.demo.scenarios import PAYMENT_BODY, PAYMENT_NOTICE, TOOLS
from sg_guard.demo.server import build_app
from sg_guard.demo.sessions import LiveSessions, TURNS


class FakeEngine:
    """Exercise transport, filtering and bookkeeping without loading weights."""

    def __init__(self):
        self.identity = dict(model="test-double", revision="fixture", device="cpu", dtype="float32",
                             mode="isolated_kv", rule="test rule", threshold=.1, width=16,
                             probe_sha256="fixture", model_path="hidden-local-directory")
        self.inputs = []

    def generate(self, messages, tools, max_new_tokens=256, on_token=None):
        self.inputs.append(copy.deepcopy(messages))
        last = messages[-1]
        call = None
        if last['role'] == 'user' and last['content'] == TURNS[0]:
            call = dict(function='get_invoice', args={'invoice_id': 'INV-2026-041'})
        elif last['role'] == 'user' and last['content'] == TURNS[1]:
            call = dict(function='transfer_money', args=dict(recipient_account='NS-2391', amount=1000,
                                                            currency='CNY', reference='INV-2026-041'))
        text = json.dumps(call) if call else 'Test response.'
        if on_token:
            on_token(dict(text=text, token_id=1, index=0))
        return dict(text=text, call=call, invalid=False, finish_reason='stop', generated_tokens=1,
                    token_ids=[1], seconds=0.0)

    def detect(self, messages, tools, verify_native=False, on_progress=None):
        raw = messages[-1]['content']
        filtered = raw.replace(PAYMENT_NOTICE, '')
        start = raw.find(PAYMENT_NOTICE)
        alert = start >= 0
        tokens = [dict(index=i, id=i, text=c, start=i, end=i+1, score=.2 if alert and i>=start else 0.)
                  for i, c in enumerate(raw)]
        if on_progress:
            on_progress('readout_start', original=raw, tokens=[dict(t, score=None) for t in tokens])
            on_progress('readout_progress', layer=35, tokens=tokens, completed=len(tokens), total=len(tokens))
        spans = [dict(start=start, end=len(raw))] if alert else []
        decision = dict(alert=alert, score=.2 if alert else 0., threshold=.1, effective_width=min(16,len(raw)),
                        window_scores=[.2 if alert else 0.], spans=spans)
        receipt = dict(edits=[dict(original_char_span=[start,len(raw)])] if alert else [])
        return dict(original=raw, filtered=filtered, tokens=tokens, decision=decision, receipt=receipt,
                    seconds=0., layer_weights=[1.], layer_scores=[[0.]*len(raw)], validation={})


class DemoContracts(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.engine = FakeEngine()
        self.app = build_app(engine_factory=lambda: self.engine, data_dir=self.directory.name)
        self.client = TestClient(self.app, base_url='http://127.0.0.1')
        self.client.__enter__()
        deadline = time.monotonic()+3
        while self.client.get('/api/health').json()['status'] != 'ready':
            if time.monotonic()>deadline:
                self.fail('Fake engine did not start')
            time.sleep(.01)

    def tearDown(self):
        self.client.__exit__(None,None,None)
        self.directory.cleanup()

    def create(self, arm='guard'):
        response = self.client.post('/api/live/sessions', json={'scenario_id':'payment_notice','arm':arm})
        self.assertEqual(response.status_code,200)
        return response.json()['id']

    def turn(self, ident, text, request_id):
        response = self.client.post(f'/api/live/sessions/{ident}/turns',json={'content':text,'request_id':request_id})
        self.assertEqual(response.status_code,200,response.text)
        deadline=time.monotonic()+5
        while True:
            state=self.client.get(f'/api/live/sessions/{ident}').json()
            if state['status']!='running':
                self.assertEqual(state['status'],'idle',state.get('error'))
                return state
            if time.monotonic()>deadline:
                self.fail('Test turn did not complete')
            time.sleep(.01)

    def test_public_configuration_and_static_allowlist(self):
        health=self.client.get('/api/health').json()
        self.assertNotIn('model_path',health['runtime'])
        self.assertEqual(self.client.get('/').status_code,200)
        self.assertEqual(self.client.get('/live.js').status_code,200)
        for path in ('/story.json','/README.md','/paper.pdf','/server.py'):
            self.assertEqual(self.client.get(path).status_code,404)
        page=self.client.get('/').text
        self.assertNotIn('terminal.html',page)
        self.assertNotIn('方向构造',page)

    def test_host_origin_and_cross_site_protection(self):
        self.assertEqual(self.client.get('/api/health',headers={'Host':'attacker.invalid'}).status_code,400)
        self.assertEqual(self.client.post('/api/live/sessions',headers={'Origin':'https://attacker.invalid'},json={}).status_code,403)
        self.assertEqual(self.client.get('/api/health',headers={'Sec-Fetch-Site':'cross-site'}).status_code,403)
        self.assertEqual(self.client.post('/api/live/sessions',headers={'Origin':'http://127.0.0.1'},json={}).status_code,200)

    def test_external_readme_link_can_open_only_public_assets(self):
        headers={'Sec-Fetch-Site':'cross-site','Origin':'https://github.com'}
        for path in ('/','/live.html','/live.js','/live.css'):
            response=self.client.get(path,headers=headers)
            self.assertEqual(response.status_code,200,path)
            self.assertEqual(response.headers['X-Frame-Options'],'DENY')
        for path in ('/api/health','/api/live/sessions','/api/live/presets','/openapi.json'):
            self.assertEqual(self.client.get(path,headers=headers).status_code,403,path)
        self.assertEqual(self.client.post('/',headers=headers).status_code,403)
        self.assertEqual(self.client.get('/',headers={**headers,'Host':'attacker.invalid'}).status_code,400)

    def test_three_turn_ledger_and_idempotency(self):
        ident=self.create()
        first=self.turn(ident,TURNS[0],'query-0001')
        self.assertEqual(first['ledger'],[])
        self.assertTrue(any(e['kind']=='edit' and e['filtered']==PAYMENT_BODY for e in first['events']))
        observed_tool_inputs=[m[-1]['content'] for m in self.engine.inputs if m[-1]['role']=='tool']
        self.assertEqual(observed_tool_inputs,[PAYMENT_BODY])
        second=self.turn(ident,TURNS[1],'confirm-0001')
        self.assertEqual(len(second['ledger']),1)
        self.assertTrue(second['result']['safe_completion'])
        self.assertEqual(second['ledger'][0]['amount'],1000)
        # A response retry must not execute transfer_money twice.
        replay=self.client.post(f'/api/live/sessions/{ident}/turns',json={'content':TURNS[1],'request_id':'confirm-0001'})
        self.assertEqual(replay.status_code,200)
        self.assertEqual(len(self.client.get(f'/api/live/sessions/{ident}').json()['ledger']),1)
        conflict=self.client.post(f'/api/live/sessions/{ident}/turns',json={'content':'different','request_id':'confirm-0001'})
        self.assertEqual(conflict.status_code,409)
        third=self.turn(ident,TURNS[2],'audit-0001')
        self.assertEqual(len(third['ledger']),1)
        self.assertTrue(third['result']['safe_completion'])
        self.assertEqual(third['balance'],19000)
        downloaded=self.client.get(f'/api/live/sessions/{ident}/download')
        self.assertEqual(downloaded.status_code,200)
        self.assertEqual(downloaded.json()['events'],third['events'])
        self.assertNotIn('model_path',downloaded.json()['runtime'])

    def test_baseline_observes_but_does_not_edit(self):
        ident=self.create('baseline')
        state=self.turn(ident,TURNS[0],'baseline-0001')
        self.assertFalse(any(e['kind']=='edit' for e in state['events']))
        readouts=[e for e in state['events'] if e['kind']=='readout']
        self.assertTrue(readouts)
        self.assertFalse(readouts[0]['applied'])
        tools=[m[-1]['content'] for m in self.engine.inputs if m[-1]['role']=='tool']
        self.assertEqual(tools,[PAYMENT_BODY+PAYMENT_NOTICE])

    def test_sse_reconnect_uses_cursor_without_reexecuting(self):
        ident=self.create()
        state=self.turn(ident,TURNS[0],'stream-0001')
        cursor=state['events'][2]['seq']
        response=self.client.get(f'/api/live/sessions/{ident}/events?after=0',headers={'Last-Event-ID':str(cursor)})
        events=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
        self.assertEqual(events,state['events'][cursor+1:])
        self.assertEqual(len(self.engine.inputs),2)
        self.assertEqual(self.client.get(f'/api/live/sessions/{ident}/events',headers={'Last-Event-ID':'invalid'}).status_code,400)

    def test_free_input_has_no_predefined_success_label(self):
        ident=self.create()
        state=self.turn(ident,'Explain the invoice.','freeinput-0001')
        self.assertIsNone(state['result']['safe_completion'])
        self.assertFalse(state['result']['fixture_inputs_match'])

    def test_saved_session_survives_store_restart(self):
        ident=self.create()
        state=self.turn(ident,TURNS[0],'persist-0001')
        store=self.app.state.live_sessions
        recovered=LiveSessions(store.state,store.inference_lock,directory=self.directory.name).get(ident)
        self.assertEqual(recovered['events'],state['events'])
        self.assertEqual(recovered['messages'],state['messages'])
        self.assertEqual(recovered['status'],'idle')

    def test_schema_and_identifier_rejections(self):
        self.assertEqual(self.client.get('/api/live/sessions/not-an-id').status_code,404)
        self.assertEqual(self.client.post('/api/live/sessions',json={'scenario_id':'unknown'}).status_code,422)
        ident=self.create()
        self.assertEqual(self.client.post(f'/api/live/sessions/{ident}/turns',json={'content':'x','request_id':'short'}).status_code,422)
        self.assertEqual(self.client.post(f'/api/live/sessions/{ident}/turns',json={'content':'x'*2401,'request_id':'longtext-0001'}).status_code,422)


class DemoTextOffsets(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node.js is needed for the browser text regression')
    def test_unicode_offsets_in_heatmap_edits_and_window_text(self):
        script = Path(__file__).parents[1]/'src/sg_guard/demo/web/live.js'
        # Evaluate the real render functions without mounting the page or opening HTTP.
        check = r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(process.argv[1],'utf8');
const seam=source.indexOf("  $('composer').addEventListener");
assert.ok(seam>0);
const context={};
vm.runInNewContext(source.slice(0,seam)+'globalThis.renderers={textSlice,textLength,heatText,editText};})();',context);
const {textSlice,textLength,heatText,editText}=context.renderers;
const text='A😀支付💳 end';
assert.equal(textLength(text),9);
assert.equal(textSlice(text,2,4),'支付');
const row={original:text,tokens:[{index:0,start:2,end:4,score:.2}],decision:{spans:[{start:2,end:4}]}};
assert.equal(editText(row),'A😀<del>支付</del>💳 end');
assert.match(heatText(row),/^A😀<span [^>]+>支付<\/span>💳 end$/u);
assert.equal(editText({...row,decision:{spans:[{start:1,end:2}]}}),'A<del>😀</del>支付💳 end');
assert.equal(textSlice('Please pay invoice',7,10),'pay');
assert.equal(editText({original:'Please pay invoice',decision:{spans:[{start:7,end:10}]}}),'Please <del>pay</del> invoice');
"""
        result = subprocess.run([shutil.which('node'), '-e', check, str(script)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
