"""Separate relevance and traditional information-summary model calls."""
import json

from .llm_client import ModelError, generate, parse_json
from osbi_contracts.models import Selection

from .prompts import INFO_PROMPT, RELEVANCE_PROMPT


async def select_relevance(chat, focus, payload, model, fallback_model, *, system_prompt=RELEVANCE_PROMPT):
    def validate(text):
        selection = Selection.model_validate(parse_json(text))
        if not set(selection.blockIds) <= {b['id'] for b in payload['blocks']} or not set(selection.linkIds) <= {l['id'] for l in payload['links']}:
            raise ModelError('Model selected an ID outside this relevance batch')
        return selection
    selection, _ = await generate(chat, [
        {'role': 'system', 'content': system_prompt},
        {'role': 'user', 'content': json.dumps({'focus': focus.model_input(), 'material': payload}, ensure_ascii=False)},
    ], model, fallback_model, validate)
    return selection


async def summarize_block(chat, focus, title, text, model, fallback_model, *, system_prompt=INFO_PROMPT):
    def validate(raw):
        value = parse_json(raw)
        if not isinstance(value, dict) or set(value) != {'info'} or not isinstance(value['info'], str):
            raise ModelError('Information summary must contain only the string field info')
        return value['info'].strip()
    return await generate(chat, [
        {'role': 'system', 'content': system_prompt},
        {'role': 'user', 'content': json.dumps({'focus': focus.model_input(), 'title': title, 'text': text}, ensure_ascii=False)},
    ], model, fallback_model, validate)
