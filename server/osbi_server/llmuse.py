"""Select original body blocks and follow-up links against a supplied focus."""
import json

from .llm_client import ModelError, generate, parse_json
from osbi_contracts.models import Selection

from .prompts import RELEVANCE_PROMPT


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
