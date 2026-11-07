"""Qwen native tool-message encoding and token-to-character boundaries."""
import copy
import hashlib
import json


def digest(data):
    return hashlib.sha256(data).hexdigest()


def qwen_call(text):
    start = text.find('<tool_call>')
    if start < 0:
        return None, False
    end = text.find('</tool_call>', start)
    if end < 0:
        return None, True
    try:
        value = json.loads(text[start + len('<tool_call>'):end].strip())
    except ValueError:
        return None, True
    if not isinstance(value, dict) or not isinstance(value.get('name'), str) or not value['name'].strip():
        return None, True
    if not isinstance(value.get('arguments'), dict):
        return None, True
    return dict(function=value['name'], args=value['arguments']), False


def encode_request(request, tokenizer):
    assert request['condition'] == 'native' and request['policy'] is None
    assert request['tool_protocol'] == 'qwen_native'
    messages = copy.deepcopy(request['messages'])
    kwargs = dict(tools=request['tools'], tokenize=False,
                  add_generation_prompt=True, enable_thinking=False)
    rendered = tokenizer.apply_chat_template(messages, **kwargs)
    enc = tokenizer(rendered, add_special_tokens=False, return_offsets_mapping=True)
    nonce = '__BOUNDARY_' + digest(rendered.encode()) + '__'
    assert nonce not in rendered
    marked = copy.deepcopy(messages)
    markers = {}
    for mi, message in enumerate(marked):
        if message['role'] not in ('system', 'user', 'tool'):
            continue
        opening, closing = nonce + str(mi) + '_OPEN', nonce + str(mi) + '_CLOSE'
        message['content'] = opening + message['content'] + closing
        markers[opening] = (mi, True)
        markers[closing] = (mi, False)
    tagged = tokenizer.apply_chat_template(marked, **kwargs)
    found = sorted((tagged.index(mark), mark, value) for mark, value in markers.items())
    parts, cursor, length, starts, regions = [], 0, 0, {}, []
    for at, mark, (mi, opening) in found:
        part = tagged[cursor:at]
        parts.append(part)
        length += len(part)
        if opening:
            starts[mi] = length
        else:
            regions.append(dict(message_index=mi, role=messages[mi]['role'],
                                char_start=starts.pop(mi), char_end=length))
        cursor = at + len(mark)
    parts.append(tagged[cursor:])
    assert ''.join(parts) == rendered and not starts
    for region in regions:
        region['token_positions'] = [i for i, (a, b) in enumerate(enc['offset_mapping'])
                                     if b > a and b > region['char_start'] and a < region['char_end']]
    all_positions = {p for region in regions for p in region['token_positions']}
    return dict(rendered=rendered, rendered_sha256=digest(rendered.encode()),
                input_ids=enc['input_ids'], offsets=enc['offset_mapping'],
                regions=regions,
                template_and_assistant_positions=[i for i in range(len(enc['input_ids'])) if i not in all_positions],
                final_prefill_position=len(enc['input_ids']) - 1)
