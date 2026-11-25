"""Prompt overrides replace defaults; default prompts and output copy are configurable."""
import asyncio

from osbi_client.models import ClientConfig, NodeReportRules
from osbi_client.publisher_penalty import PublisherPenaltyLedger
from osbi_client.shadow_topnews import _select_board
from osbi_client.shadow_weekly import _section, push_text


class CapturingLlm:
    model = 'capture'

    def __init__(self, reply):
        self.reply = reply
        self.messages = None

    async def chat(self, messages, **kwargs):
        self.messages = messages
        return self.reply


def row():
    return dict(articleId='a1', title='社区治理案例', url='https://example.org/a',
                publisher='示例发布', publishedDate='2026-11-24',
                relationCount=2, entityCount=3, keyEntities=[], relations=[],
                section='社区治理', boards=['社区治理'])


def test_topnews_board_prompt_is_a_full_override():
    llm = CapturingLlm('<links>[1]</links>')
    asyncio.run(_select_board(llm, board='社区治理', rows=[row()], top_n=3,
                              prompt='CUSTOM TOPNEWS PROMPT'))
    assert llm.messages[0]['content'] == 'CUSTOM TOPNEWS PROMPT'
    assert 'You are an editor preparing a daily brief' not in llm.messages[0]['content']


def test_weekly_board_prompt_is_a_full_override():
    llm = CapturingLlm('### 热点\n正文 [1]')
    result = asyncio.run(_section(llm, '社区治理', [{
        **row(), 'excerpt': '正文', 'blockIds': ['b1']}], prompt='CUSTOM WEEKLY PROMPT'))
    assert llm.messages[0]['content'] == 'CUSTOM WEEKLY PROMPT'
    assert result['markdown']


def test_weekly_intro_template_supports_date_and_digest():
    assert push_text('本周摘要。', '2026-11-25',
                     intro='Weekly {date}\n\n{digest}') == (
        'Weekly 2026-11-25\n\n本周摘要。')
    assert push_text('本周摘要。', '2026-11-25', intro='Prefix {date}') == (
        'Prefix 2026-11-25\n\n本周摘要。')


def test_config_carries_output_copy_and_task_prompts():
    config = ClientConfig(nodeRules=NodeReportRules(
        topnewsShout='[sun] Daily brief', topnewsSuffix='Footer',
        weeklyIntro='Weekly {date}\n\n{digest}'))
    profile = config.reportProfiles[0]
    assert profile.topnews.prompt == ''
    assert profile.weeklyreport.prompt == ''
    assert config.nodeRules.topnewsSuffix == 'Footer'


def test_publisher_penalty_reset_clears_scores_without_reseeding(tmp_path):
    ledger = PublisherPenaltyLedger(tmp_path / 'publisher_penalty.json')
    ledger.record('2026-11-24', {'示例发布'})
    assert ledger.scores
    ledger.reset('2026-11-25')
    ledger = PublisherPenaltyLedger(tmp_path / 'publisher_penalty.json').load()
    assert ledger.scores == {}
    assert ledger.updated_date == '2026-11-25'
