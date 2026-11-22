"""Weekly Word export, ported from midplatform/workflows/weekly_report.py.

Same style as the traditional weekly (penholder): SimSun (宋体) 12pt body,
Cambria headings, clickable [text](url) / bare-URL hyperlinks, and the 工作提示
block (heading line + following numbered items) force-bolded without relying
on the model emitting ** markers.
"""
from __future__ import annotations

import re

from docx import Document
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
from docx.shared import Pt, RGBColor
from docx.enum.text import WD_PARAGRAPH_ALIGNMENT

# Markdown hyperlinks [text](url); bare URLs (http/https).
_MD_LINK_RE = re.compile(r'\[([^\]]+)\]\((https?://[^\s)]+)\)')
_BARE_URL_RE = re.compile(r'(?<![\w\)\]])(https?://[^\s\)]+)')


def add_hyperlink(paragraph, url: str, text: str = None, color="0563C1", underline=True):
    """Append an external hyperlink run to the paragraph (display text or URL)."""
    part = paragraph.part
    r_id = part.relate_to(url, "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink", is_external=True)
    hyperlink = OxmlElement('w:hyperlink')
    hyperlink.set(qn('r:id'), r_id)

    new_run = OxmlElement('w:r')
    rPr = OxmlElement('w:rPr')

    if color:
        c = OxmlElement('w:color')
        c.set(qn('w:val'), color)
        rPr.append(c)
    if underline:
        u = OxmlElement('w:u')
        u.set(qn('w:val'), 'single')
        rPr.append(u)

    new_run.append(rPr)
    t = OxmlElement('w:t')
    t.text = text if text is not None else url
    new_run.append(t)
    hyperlink.append(new_run)
    paragraph._p.append(hyperlink)


def _emit_segment_with_links(para, text: str, bold: bool = False):
    """Render [text](url) and bare URLs as hyperlinks; the rest as plain runs."""
    cursor = 0
    for m in _MD_LINK_RE.finditer(text):
        if m.start() > cursor:
            run = para.add_run(text[cursor:m.start()])
            run.bold = bold
        add_hyperlink(para, m.group(2), text=m.group(1))
        cursor = m.end()
    for m in _BARE_URL_RE.finditer(text, cursor):
        if m.start() > cursor:
            run = para.add_run(text[cursor:m.start()])
            run.bold = bold
        add_hyperlink(para, m.group(1))
        cursor = m.end()
    if cursor < len(text):
        run = para.add_run(text[cursor:])
        run.bold = bold


def md_to_docx(markdown: str, out_file: str):
    """markdown -> docx (penholder style: SimSun 12pt body, Cambria headings)."""
    new_doc = Document()
    new_doc.styles['Normal'].font.name = u'宋体'
    new_doc.styles['Normal']._element.rPr.rFonts.set(qn('w:eastAsia'), u'宋体')
    new_doc.styles['Normal'].font.size = Pt(12)
    new_doc.styles['Normal'].font.color.rgb = RGBColor(0, 0, 0)

    lines = [line.strip() for line in markdown.split('\n') if line.strip()]

    if lines and lines[0]:
        head = new_doc.add_heading(level=1)
        head.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
        run = head.add_run(lines[0].lstrip('#').strip())
        run.font.name = u'Cambria'
        run.font.color.rgb = RGBColor(0, 0, 0)
        run._element.rPr.rFonts.set(qn('w:eastAsia'), u'Cambria')

    # 工作提示 block state: heading line + following numbered items are all
    # bolded, independent of whether the LLM emitted ** markers.
    in_hint = False

    current_table = None
    for line in lines[1:]:
        if line.startswith('#'):
            in_hint = False   # new section leaves the 工作提示 block
            text = line.lstrip('#')
            level = min(len(line) - len(text), 9)
            head = new_doc.add_heading(level=level)
            run = head.add_run(text.strip())
            run.font.name = u'Cambria'
            run.font.color.rgb = RGBColor(0, 0, 0)
        elif '|' in line and line.count('|') >= 2:
            in_hint = False
            row_cells = [cell.strip() for cell in line.split('|')[1:-1]]
            if current_table is None:
                current_table = new_doc.add_table(rows=0, cols=len(row_cells))
            row = current_table.add_row().cells
            for i, cell_text in enumerate(row_cells):
                if i < len(row):
                    row[i].text = cell_text
        else:
            current_table = None
            plain = line.strip(' *')
            # Force-bold the whole 工作提示 block (heading + numbered items).
            in_hint = plain.startswith('工作提示') or (in_hint and bool(re.match(r'\d+\.', plain)))
            para = new_doc.add_paragraph()
            # Split on **: odd segments are bold runs; an unclosed trailing
            # segment stays plain so no text is swallowed. Segments containing
            # [text](url) or bare URLs render as clickable hyperlinks.
            parts = line.split('**')
            for i, part in enumerate(parts):
                if not part:
                    continue
                is_bold = (i % 2 == 1) or in_hint
                _emit_segment_with_links(para, part, bold=is_bold)

    new_doc.save(out_file)
