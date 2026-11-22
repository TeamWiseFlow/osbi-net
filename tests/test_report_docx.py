"""Weekly docx export style parity with production md_to_docx."""
from docx import Document
from docx.oxml.ns import qn
from docx.shared import Pt

from osbi_client.report_docx import md_to_docx

MARKDOWN = """# 基层工作动态回顾与热点展望（2026年11月16日—11月22日）

## 一、社区治理

### 邻里互助行动

正文第一段，含链接 [静安发布](https://example.org/a) 与裸链接 https://example.org/b 。

**加粗片段**普通片段。

## 二、综合研判

工作提示
1. 对照本周政策脉络梳理上位政策。
2. 优先研究其他街道落实动作。

| 板块 | 数量 |
| 社区治理 | 2 |
"""


def test_md_to_docx_fonts_headings_links_and_hint_bold(tmp_path):
    out = tmp_path / 'weekly.docx'
    md_to_docx(MARKDOWN, str(out))
    doc = Document(str(out))
    normal = doc.styles['Normal']
    assert normal.font.name == '宋体'
    assert normal.font.size == Pt(12)
    assert normal._element.rPr.rFonts.get(qn('w:eastAsia')) == '宋体'
    # First heading centered, Cambria.
    head = doc.paragraphs[0]
    assert head.text == '基层工作动态回顾与热点展望（2026年11月16日—11月22日）'
    assert head.runs[0].font.name == 'Cambria'
    # Hyperlinks: markdown link text + bare URL both become w:hyperlink;
    # targets live in the package relationships.
    xml = doc.element.xml
    assert xml.count('<w:hyperlink') >= 2
    targets = {rel.target_ref for rel in doc.part.rels.values()
               if rel.reltype.endswith('/hyperlink')}
    assert 'https://example.org/a' in targets and 'https://example.org/b' in targets
    assert '静安发布' in xml  # markdown link display text kept
    # 工作提示 heading + numbered items are bold without ** markers.
    hint_paras = [p for p in doc.paragraphs
                  if p.text.startswith('工作提示') or p.text.startswith('1. 对照')
                  or p.text.startswith('2. 优先')]
    assert len(hint_paras) == 3
    assert all(run.bold for p in hint_paras for run in p.runs if run.text.strip())
    # ** segment bold, plain segment not.
    mixed = next(p for p in doc.paragraphs if '加粗片段' in p.text)
    bold_runs = [r.text for r in mixed.runs if r.bold]
    assert any('加粗片段' in t for t in bold_runs)
    # Table rows rendered.
    assert doc.tables and doc.tables[0].rows[0].cells[0].text == '板块'


def test_md_to_docx_unclosed_bold_does_not_swallow_text(tmp_path):
    out = tmp_path / 'unclosed.docx'
    md_to_docx('# 标题\n\n正文**未闭合片段', str(out))
    doc = Document(str(out))
    body = doc.paragraphs[-1]
    # Production splits on ** and bolds odd segments; the trailing unclosed
    # segment must still appear (no text swallowed).
    assert '未闭合片段' in body.text
    assert '正文' in body.text
