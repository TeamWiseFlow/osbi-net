"""Read Word heading text for local evidence matching and coverage history."""
from collections import defaultdict
from pathlib import Path
import re
import zipfile
from xml.etree import ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def parse_sections(path):
    path = Path(path)
    if path.stat().st_size > 20 * 1024 * 1024:
        raise ValueError("Report file is too large")
    with zipfile.ZipFile(path) as archive:
        for name in ("word/document.xml", "word/styles.xml"):
            if name in archive.namelist() and archive.getinfo(name).file_size > 20 * 1024 * 1024:
                raise ValueError("Report XML is too large")
        styles = {}
        if "word/styles.xml" in archive.namelist():
            for style in ET.fromstring(archive.read("word/styles.xml")).iter(W + "style"):
                name = style.find(W + "name")
                if name is not None:
                    styles[style.get(W + "styleId")] = name.get(W + "val", "")
        root = ET.fromstring(archive.read("word/document.xml"))
    result, board = defaultdict(list), ""
    for para in root.iter(W + "p"):
        text = "".join(t.text or "" for t in para.iter(W + "t")).strip()
        style = para.find(W + "pPr/" + W + "pStyle")
        if not text or style is None:
            continue
        raw = style.get(W + "val", "")
        kind = re.sub(r'\s+', '', styles.get(raw, raw)).casefold()
        if kind in {"heading2", "标题2"}:
            board = re.sub(r'^[一二三四五六七八九十]+、', '', text)
        elif kind in {"heading3", "标题3"} and board:
            result[board].append(text)
    if not result:
        raise ValueError("Report has no Heading 2 board / Heading 3 hotspot structure")
    return dict(result)
