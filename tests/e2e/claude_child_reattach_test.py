"""A compacted parent reply cannot strand its child, saved answer or queued input.

The AWS fixture lowers only the runner's compaction size trigger to one frame.
Its real Result/store checkpoint still decides what can be removed.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import uuid

import httpx
import pytest

from tests.e2e import _sandbox_helpers as h

pytestmark = [pytest.mark.e2e, pytest.mark.deployment_shape]


def _boundary(sid: str, action: str) -> dict:
    program = """
import json,os,pathlib,sys
sid,action=sys.argv[1:]
assert os.environ.get('ASTRABOX_E2E_FAULTS')=='1'
p=pathlib.Path(os.environ['ASTRABOX_E2E_FAULT_FILE']+'.d')/('child-reattach-'+sid+'.json')
if action=='clear': p.unlink(missing_ok=True)
elif action in ('arm','release'):
 p.parent.mkdir(parents=True,exist_ok=True)
 value={'faults':{'hold_reader_boundary':1},'match':{'session_id':sid,'phase':'connected'},'release':False,'consumed':[]} if action=='arm' else {**json.loads(p.read_text()),'release':True}
 t=p.with_suffix('.tmp');t.write_text(json.dumps(value));t.chmod(0o644);t.replace(p)
print(p.read_text() if p.exists() else '{}')
"""
    return json.loads(subprocess.check_output([
        'docker', 'exec', os.environ['ASTRABOX_E2E_SERVER_CONTAINER'],
        'python', '-c', program, sid, action,
    ], text=True, timeout=10))


def _wait(read, predicate, description: str, timeout: float = 30):
    deadline = h.deadline_after(timeout)
    value = None
    while time.monotonic() < deadline:
        value = read()
        if predicate(value):
            return value
        time.sleep(0.3)
    raise AssertionError(f'{description}: {value!r}')


def test_native_child_identity_survives_compaction_and_answered_reader_takeover(e2e_client: httpx.Client) -> None:
    """Actual native work crosses Result, compaction, a saved answer and attach."""
    c = e2e_client
    assert h.engine_kind() == 'claude_code'
    sid = h.create_session(c, permission_mode='bypassPermissions')['session_id']
    qsid = h.sql_literal(sid)
    marker = 'CHILD_ATTACH_' + uuid.uuid4().hex[:10]
    started, release = (h.workspace_path(c, sid, marker + suffix) for suffix in ('.started', '.release'))
    receipt = 'CHILD_RECEIPT_' + uuid.uuid4().hex
    followup = 'QUEUED_REPLY_' + uuid.uuid4().hex
    try:
        h.poll_until_agent_ready(c, sid)
        command = f"printf {marker} > {started}; while [ ! -f {release} ]; do sleep 0.2; done; cat {release}"
        launch_prompt = (
            'Call Agent exactly once with subagent_type="general-purpose", run_in_background=true, '
            f'description="{marker}". Its prompt: Run exactly one Bash command with '
            f'run_in_background=false, timeout=120000: {command!r}. '
            'Then include the actual stdout receipt in your final answer. Do not create the release file. '
            'Immediately acknowledge launch and end your parent reply; do not wait or collect the result.'
        )
        launch = h.send_and_follow(c, sid, launch_prompt, until='end', timeout=60)
        assert launch.error is None and launch.ended, launch.error
        assert h.recorded_response(sid, launch.response_id)['outcome'] == 'completed'
        assert h.wait_for_file(c, sid, started, timeout=15) == marker.encode()
        native = h.transcript_entries(sid)
        receipts = [r['entry'] for r in native if not r['subpath']
                    and r['entry'].get('toolUseResult', {}).get('status') == 'async_launched']
        assert len(receipts) == 1, receipts
        agent_id = receipts[0]['toolUseResult']['agentId']
        tool_id = next(b['tool_use_id'] for b in receipts[0]['message']['content'] if b.get('type') == 'tool_result')
        children = h.data(c.get(f'/api/v1/sessions/{sid}/child-runs'))['child_runs']
        assert len(children) == 1, children
        child_id = children[0]['child_run_id']

        def compacted():
            stdout, stderr, code, _ = h.run_terminal(c, sid, 'cat /tmp/astrabox_runner.log')
            assert code == 0, stderr
            return re.findall(r'result_seq=(\d+) store_seq=\d+ removed=(\d+) first=(\d+)', stdout)

        cut = _wait(compacted, lambda rows: any(int(r) > 0 and result == first for result, r, first in rows),
                    'the real Result must compact its launch prefix', 20)
        after_parent = h.output_position(c, sid)
        h.data(c.post(f'/api/v1/sessions/{sid}/permission-mode', json={'permission_mode': 'default'}))
        question_prompt = (
            f'Call AskUserQuestion exactly once with one single-select question "{marker} proceed?", '
            'offering YES and NO. After the answer, acknowledge YES and end. Use no other tools.'
        )
        question = h.send_and_follow(c, sid, question_prompt, timeout=30)
        pending = h.get_session(c, sid)['pending_interaction']
        assert pending['tool_name'] == 'AskUserQuestion' and question.paused, pending
        iid, rid = pending['interaction_id'], question.response_id
        before = {(r['subpath'], r['seq']): r['entry'] for r in h.transcript_entries(sid)}
        _boundary(sid, 'arm')
        h.data(c.post(f'/api/v1/admin/sessions/{sid}/evict-runtime'))
        _wait(lambda: _boundary(sid, 'read'), lambda r: len(r.get('consumed', [])) == 1,
              'replacement reader reaches its pre-replay boundary')
        answer = {'answers': [{'question_id': item['id'], 'option_label': 'YES'} for item in pending['questions']]}
        h.answer_interaction(c, sid, iid, answer)
        queued_prompt = f'Answer only {followup}. Do not call any tool.'
        queued = h.send_input(c, sid, queued_prompt)
        assert h.recorded_input(sid, queued['input_id'])['state'] == 'pending'
        stdout, stderr, code, _ = h.run_terminal(c, sid, f'printf {receipt} > {release}')
        assert code == 0, (stdout, stderr)
        _wait(lambda: h.transcript_entries(sid),
              lambda rows: any(r['subpath'] == 'subagents/agent-' + agent_id
                               and receipt in json.dumps(r['entry']) for r in rows),
              'native child stores the actual tool output while the reader is held', 30)
        _boundary(sid, 'release')
        h.follow_reply(c, question, until='end', timeout=30)
        delivered = _wait(lambda: h.recorded_input(sid, queued['input_id']),
                          lambda row: row['state'] == 'admitted' and bool(row.get('response_id')),
                          'native queue admits the original input')
        _wait(lambda: h.recorded_response(sid, delivered['response_id']),
              lambda row: row['status'] == 'ended' and row['outcome'] == 'completed',
              'the native response answering the queued input completes')
        assert question.error is None and question.response_id == rid and question.ended
        assert h.recorded_response(sid, rid)['outcome'] == 'completed'
        messages = h.history(c, sid)
        answers = [m for m in messages if m.get('role') == 'assistant'
                   and queued['input_id'] in m.get('metadata', {}).get('input_ids', [])]
        assert len(answers) == 1, messages
        assert followup in h.message_text(answers[0]), answers[0]
        final = h.wait_until_settled(c, sid, timeout=30)
        assert not final.get('pending_interaction') and not final.get('held_inputs'), final
        interactions = h.psql_rows(f'SELECT interaction_id,status,answer,closed_at FROM session_interactions WHERE session_id={qsid}')
        assert len(interactions) == 1 and interactions[0]['interaction_id'] == iid
        assert interactions[0]['status'] == 'answered' and interactions[0]['answer'] == answer
        assert interactions[0]['closed_at']
        inputs = h.psql_rows(f'SELECT input_id,state FROM session_inputs WHERE session_id={qsid} ORDER BY seq')
        assert {r['input_id'] for r in inputs} == {launch.input_id, question.input_id, queued['input_id']}
        assert all(r['state'] == 'admitted' for r in inputs), inputs
        after = {(r['subpath'], r['seq']): r['entry'] for r in h.transcript_entries(sid)}
        assert all(after.get(key) == entry for key, entry in before.items()), 'native history changed'
        native_users = [entry['message']['content'] for (scope, _), entry in after.items()
                        if not scope and entry.get('type') == 'user']
        native_enqueues = [entry.get('content') for (scope, _), entry in after.items()
                           if not scope and entry.get('type') == 'queue-operation'
                           and entry.get('operation') == 'enqueue']
        for prompt in (launch_prompt, question_prompt, queued_prompt):
            assert native_enqueues.count(prompt) == 1
        tool_results = [block for content in native_users if isinstance(content, list)
                        for block in content if block.get('type') == 'tool_result'
                        and block.get('tool_use_id') == pending['tool_call_id']]
        assert len(tool_results) == 1 and 'YES' in json.dumps(tool_results[0]), tool_results
        child = h.data(c.get(f'/api/v1/sessions/{sid}/child-runs/{child_id}/messages'))
        assert receipt in json.dumps(child), child
        parts = h.psql_rows(f'SELECT seq,part FROM session_output WHERE session_id={qsid} AND seq>{after_parent}')
        assert any(row['part'].get('type') == 'data-subagent'
                   and row['part'].get('data', {}).get('kind') == 'message'
                   and row['part']['data'].get('engineRef') == agent_id for row in parts), (
            'Scene observation incomplete: the native child must actually emit a cross-Result SDK message; '
            'stored child history alone does not exercise the failed translation path')
        catalog = h.data(c.get(f'/api/v1/sessions/{sid}/child-runs'))['child_runs']
        assert [r['child_run_id'] for r in catalog] == [child_id] and not catalog[0]['active'], catalog
        print(json.dumps(dict(session_id=sid, native_agent_id=agent_id, native_tool_id=tool_id,
                             compacted=cut, original_input_ids=[r['input_id'] for r in inputs],
                             interaction_id=iid, response_id=rid, native_prefix_records=len(before), passed=True)))
    finally:
        _boundary(sid, 'clear')
        h.release_session(sid)
