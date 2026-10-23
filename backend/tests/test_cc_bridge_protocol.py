"""Exercise the pinned real adapter with in-memory Responses replies, no network."""
import asyncio
import json
from importlib.metadata import version

import httpx
import pytest

from backend.app.config import settings


@pytest.mark.parametrize('stream', [False, True])
def test_real_litellm_tool_use_roundtrip(monkeypatch, stream):
    pytest.importorskip('litellm')
    if version('litellm') != '1.105.1':
        pytest.skip('协议验收需要用户 bridge 固定的 litellm==1.105.1')
    monkeypatch.syspath_prepend(str(settings.resources_dir / 'cc-openai-bridge' / 'src'))
    monkeypatch.setenv('LITELLM_LOCAL_MODEL_COST_MAP', 'True')
    from cc_openai_bridge.app import create_app
    from cc_openai_bridge.settings import Settings
    calls = []
    original = httpx.AsyncClient.send

    async def send(client, request, **kwargs):
        if request.url.host != 'bridge-upstream.invalid':
            assert request.url.host == 'bridge'
            return await original(client, request, **kwargs)
        body = json.loads(request.content)
        calls.append(body)
        assert request.url.path == '/v1/responses'
        has_result = any(item.get('type') == 'function_call_output' for item in body.get('input', []) if isinstance(item, dict))
        item = (dict(type='message', id='msg-1', role='assistant', status='completed',
                     content=[dict(type='output_text', text='received tool-result-42', annotations=[])]) if has_result else
                dict(type='function_call', id='fc-1', call_id='call-1', name='Read', arguments='{"file_path":"demo.txt"}', status='completed'))
        response = dict(id='resp-1', object='response', created_at=1700000000, model=body['model'], status='completed',
                        output=[item], error=None, incomplete_details=None, parallel_tool_calls=True,
                        usage=dict(input_tokens=10, output_tokens=5, total_tokens=15,
                                   input_tokens_details={'cached_tokens': 0}, output_tokens_details={'reasoning_tokens': 0}))
        if not body.get('stream'):
            return httpx.Response(200, json=response, request=request)
        events = [dict(type='response.created', response={**response, 'status': 'in_progress', 'output': [], 'usage': None})]
        if not has_result:
            events.extend([dict(type='response.output_item.added', output_index=0, item={**item, 'arguments': ''}),
                           dict(type='response.function_call_arguments.delta', output_index=0, item_id='fc-1', delta=item['arguments']),
                           dict(type='response.function_call_arguments.done', output_index=0, item_id='fc-1', arguments=item['arguments'])])
        else:
            events.extend([dict(type='response.output_item.added', output_index=0, item={**item, 'content': []}),
                           dict(type='response.content_part.added', output_index=0, content_index=0, item_id='msg-1', part={'type': 'output_text', 'text': '', 'annotations': []}),
                           dict(type='response.output_text.delta', output_index=0, content_index=0, item_id='msg-1', delta='received tool-result-42')])
        events.extend([dict(type='response.output_item.done', output_index=0, item=item), dict(type='response.completed', response=response)])
        content = ''.join('event: '+event['type']+'\ndata: '+json.dumps(event)+'\n\n' for event in events)
        return httpx.Response(200, content=content, headers={'Content-Type': 'text/event-stream'}, request=request)

    monkeypatch.setattr(httpx.AsyncClient, 'send', send)

    async def scenario():
        app = create_app(Settings(upstream_base_url='https://bridge-upstream.invalid/v1', upstream_api_key='fake-key', bridge_api_key='fake-client'))
        messages = [{'role': 'user', 'content': 'Read demo.txt'}]
        body = {'model': 'gpt-6-astra', 'max_tokens': 100, 'messages': messages, 'stream': stream,
                'tools': [{'name': 'Read', 'description': 'Read a file', 'input_schema': {'type': 'object', 'properties': {'file_path': {'type': 'string'}}, 'required': ['file_path']}}]}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://bridge', headers={'x-api-key': 'fake-client'}) as client:
            first = await client.post('/v1/messages', json=body)
            assert first.status_code == 200, first.text
            if stream:
                events = [json.loads(line[6:]) for line in first.text.splitlines() if line.startswith('data: ')]
                assert not any(event['type'] == 'error' for event in events), first.text
                tool = next(event['content_block'] for event in events if event['type'] == 'content_block_start' and event['content_block'].get('type') == 'tool_use')
                args = ''.join(event.get('delta', {}).get('partial_json', '') for event in events if event['type'] == 'content_block_delta')
                tool['input'] = json.loads(args)
                assert any(event['type'] == 'message_stop' for event in events)
            else:
                result = first.json()
                assert result['stop_reason'] == 'tool_use'
                tool = next(block for block in result['content'] if block['type'] == 'tool_use')
            assert tool['name'] == 'Read' and tool['input'] == {'file_path': 'demo.txt'}
            messages.extend([{'role': 'assistant', 'content': [tool]}, {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': tool['id'], 'content': 'tool-result-42'}]}])
            final = await client.post('/v1/messages', json=body)
            assert final.status_code == 200, final.text
            assert 'received tool-result-42' in final.text
        assert len(calls) == 2
        assert calls[0]['tools'][0]['type'] == 'function'
        assert any(item.get('type') == 'function_call_output' and item.get('output') == 'tool-result-42' for item in calls[1]['input'])
    asyncio.run(scenario())


@pytest.mark.parametrize('variant', ['deltas', 'done_only', 'foreign_item_id'])
def test_parallel_tool_calls_keep_their_arguments(monkeypatch, variant):
    pytest.importorskip('litellm')
    if version('litellm') != '1.105.1':
        pytest.skip('协议验收需要用户 bridge 固定的 litellm==1.105.1')
    monkeypatch.syspath_prepend(str(settings.resources_dir / 'cc-openai-bridge' / 'src'))
    monkeypatch.setenv('LITELLM_LOCAL_MODEL_COST_MAP', 'True')
    from cc_openai_bridge.app import patch_tool_arguments
    patch_tool_arguments()
    from litellm.llms.anthropic.pass_through.responses_adapters.streaming_iterator import AnthropicResponsesStreamWrapper
    calls = [('fc-1', 'call-1', '{"pattern":"*.py"}'), ('fc-2', 'call-2', '{"pattern":"*.md"}')]
    events = [dict(type='response.created')]
    for index, (item_id, call_id, arguments) in enumerate(calls):
        item = dict(type='function_call', id=item_id, call_id=call_id, name='Glob', arguments='')
        events.append(dict(type='response.output_item.added', output_index=index, item=item))
    for index, (item_id, call_id, arguments) in enumerate(calls):
        if variant != 'done_only':
            delta_id = 'other-' + item_id if variant == 'foreign_item_id' else item_id
            events.extend(dict(type='response.function_call_arguments.delta', output_index=index, item_id=delta_id, delta=part)
                          for part in (arguments[:6], arguments[6:]))
    for index, (item_id, call_id, arguments) in enumerate(calls):
        item = dict(type='function_call', id=item_id, call_id=call_id, name='Glob',
                    arguments='' if variant == 'foreign_item_id' else arguments)
        events.append(dict(type='response.output_item.done', output_index=index, item=item))
    events.append(dict(type='response.completed', response=dict(status='completed', output=[
        dict(type='function_call', id=i, call_id=c, name='Glob', arguments=a) for i, c, a in calls])))

    async def collect():
        async def upstream():
            for event in events:
                yield event
        return [chunk async for chunk in AnthropicResponsesStreamWrapper(upstream(), 'openai/gpt-6-astra-codex')]

    chunks = asyncio.run(collect())
    tools = {chunk['index']: chunk['content_block'] for chunk in chunks
             if chunk['type'] == 'content_block_start' and chunk['content_block']['type'] == 'tool_use'}
    arguments = {index: '' for index in tools}
    for chunk in chunks:
        if chunk['type'] == 'content_block_delta' and chunk['delta']['type'] == 'input_json_delta':
            arguments[chunk['index']] += chunk['delta']['partial_json']
    assert {tools[i]['id']: json.loads(arguments[i]) for i in tools} == {c: json.loads(a) for _, c, a in calls}
    assert sum(chunk['type'] == 'content_block_stop' for chunk in chunks) == 2
    assert chunks[-1]['type'] == 'message_stop'
