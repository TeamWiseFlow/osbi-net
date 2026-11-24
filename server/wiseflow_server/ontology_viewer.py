"""Static ontology graph viewer for the extraction server's Oxigraph store.

Adapted from the onto_pilot prototype viewer to the block-graph layout:
- reuses the graph owner's handle for live APIs and batch exports; the
  standalone offline exporter can also open a read-only instance
- entities/predicates come from <block/o_{id}> graphs, provenance from each
  block's /source head, class ancestry from the schema fingerprint graphs
- renders one self-contained HTML (local echarts.min.js vendored in
  wiseflow_server/static/) into WISEFLOW_WORK_DIR/viewer/; the nightly
  batch refreshes it after completion, and it can be regenerated on demand:

    python -m wiseflow_server.ontology_viewer [--out DIR]
"""
import argparse
import json
import logging
import os
import shutil
from html import escape
from pathlib import Path

from pyoxigraph import Store

from .ontology_store import (BLOCK_BASE, INSTANCE_BASE, NS, RDF_TYPE, SCHEMA_BASE,
                             SEED_GRAPH, uri_for)

logger = logging.getLogger(__name__)

RDFS_SUBCLASS_OF = 'http://www.w3.org/2000/01/rdf-schema#subClassOf'
ACTIVE_GRAPH = f'FILTER NOT EXISTS {{ GRAPH ?g {{ ?oldHead <{NS}supersededBy> ?replacement }} }}'

# 行政层级配色（政策线红橙蓝绿青；行动紫；机构灰；人物粉）——沿用原型 v2.6 色板
CLASS_COLORS = {
    "NationalPolicy": "#c0392b", "ProvincePolicy": "#e67e22", "CityPolicy": "#2980b9",
    "DistrictPolicy": "#27ae60", "StreetPolicy": "#16a085", "PolicyDraft": "#d35400",
    "CityAction": "#8e44ad", "DistrictAction": "#9b59b6", "StreetAction": "#af7ac5",
    "CommunityAction": "#d2b4de", "Inspection": "#5b2c6f",
    "NationalGov": "#7f8c8d", "ProvinceGov": "#95a5a6", "CityGov": "#b0bec5",
    "DistrictGov": "#cfd8dc", "StreetGov": "#e0e0e0",
    "Policy": "#e74c3c", "Action": "#7d3c98", "Organization": "#5d6d7e",
    "GovBody": "#616a6b", "CommunityOrg": "#45b39d", "SocialOrg": "#48c9b0",
    "Enterprise": "#52be80", "OverseasOrg": "#34495e", "OverseasRegion": "#2c3e50",
    "OverseasGov": "#566573", "InternationalOrg": "#7b7d7d",
    "JobNotice": "#f4d03f", "RulingNotice": "#eb984e",
    "Event": "#85c1e9", "Meeting": "#5dade2", "Person": "#f5b7b1",
    "Official": "#ec7063", "Post": "#f1948a",
    "Practice": "#a3e4d7", "Platform": "#7fb3d5", "SocialGroup": "#f9e79f",
    "Concept": "#d7bde2", "Term": "#bb8fce",
    "ResidentActivity": "#af7ac5",
    "FinancialInstrument": "#d4ac0d", "FinancialInstitution": "#2e86c1",
    "MarketExchange": "#1b4f72", "EconomicIndicator": "#b7950b",
}
FALLBACK_COLOR = "#f39c12"

STATIC_DIR = Path(__file__).resolve().parent / 'static'


def _short(uri):
    return uri.rsplit('#', 1)[-1].rsplit('/', 1)[-1]


def collect(store):
    """Collect viewer data (nodes/edges/sources/class chains/stats) from the store."""
    def query(text, variables):
        return [{v: row[v].value for v in variables} for row in store.query(text)]

    # Seed classes are authoritative for known administrative regions (notably
    # 上海市 CityGov vs ProvinceGov drift), so they do not count as conflicts.
    seed_classes = {row['u']: _short(row['cls']) for row in query(f"""
        SELECT ?u ?cls WHERE {{ GRAPH <{SEED_GRAPH}> {{ ?u <{RDF_TYPE}> ?cls . }} }}""",
        ('u', 'cls'))}

    nodes, by_uri, type_conflicts = [], {}, 0
    for b in query(f"""
        SELECT ?u ?name ?cls (COUNT(DISTINCT ?g) AS ?nblocks) WHERE {{
          GRAPH ?g {{ ?u <{NS}name> ?name ; <{RDF_TYPE}> ?cls . }}
          {ACTIVE_GRAPH}
          FILTER(STRSTARTS(STR(?u), "{INSTANCE_BASE}") && STRSTARTS(STR(?g), "{BLOCK_BASE}"))
        }} GROUP BY ?u ?name ?cls""", ('u', 'name', 'cls', 'nblocks')):
        uri, cls = b['u'], _short(b['cls'])
        # 同实体跨块类型漂移：合并为一个节点（重复 id 会让 ECharts 渲染抛错）
        if uri in by_uri:
            if cls != by_uri[uri]['cls'] and cls not in by_uri[uri].get('alt_cls', []):
                by_uri[uri].setdefault('alt_cls', []).append(cls)
                if uri not in seed_classes:
                    type_conflicts += 1
            continue
        node = dict(id=uri, name=b['name'], cls=cls, nblocks=int(b['nblocks']),
                    up=[], sources=[], out_edges=0, in_edges=0)
        seed_cls = seed_classes.get(uri)
        if seed_cls:
            if seed_cls != cls:
                node['alt_cls'] = [cls]
            node['cls'] = seed_cls
        nodes.append(node)
        by_uri[uri] = node

    edges, dangling, seen_edges = [], 0, set()
    for b in query(f"""
        SELECT ?s ?pr ?o WHERE {{
          GRAPH ?g {{ ?s ?pr ?o . ?s <{NS}name> ?sn . ?o <{NS}name> ?on . }}
          {ACTIVE_GRAPH}
          FILTER(STRSTARTS(STR(?g), "{BLOCK_BASE}") && ?pr != <{NS}name>)
        }}""", ('s', 'pr', 'o')):
        s, o = b['s'], b['o']
        if s not in by_uri or o not in by_uri:
            dangling += 1
            continue
        if (s, b['pr'], o) in seen_edges:
            continue
        seen_edges.add((s, b['pr'], o))
        edges.append(dict(source=s, target=o, pred=_short(b['pr'])))
        by_uri[s]['out_edges'] += 1
        by_uri[o]['in_edges'] += 1

    for b in query(f"""
        SELECT ?u ?anc WHERE {{
          GRAPH ?g {{ ?u <{RDF_TYPE}> ?cls . }}
          {ACTIVE_GRAPH}
          GRAPH ?sg {{ ?cls <{RDFS_SUBCLASS_OF}>+ ?anc . }}
          FILTER(STRSTARTS(STR(?u), "{INSTANCE_BASE}") && STRSTARTS(STR(?g), "{BLOCK_BASE}")
                 && STRSTARTS(STR(?sg), "{SCHEMA_BASE}"))
        }}""", ('u', 'anc')):
        if b['u'] in by_uri:
            by_uri[b['u']]['up'].append(_short(b['anc']))
    for node in nodes:
        node['up'] = sorted(set(node['up']), key=node['up'].index)

    for b in query(f"""
        SELECT DISTINCT ?u ?title ?url WHERE {{
          GRAPH ?g {{ ?u <{NS}name> ?name . ?h <{NS}source_title> ?title ; <{NS}source_url> ?url . }}
          {ACTIVE_GRAPH}
          FILTER(STRSTARTS(STR(?u), "{INSTANCE_BASE}") && STRSTARTS(STR(?g), "{BLOCK_BASE}"))
        }}""", ('u', 'title', 'url')):
        source = dict(title=b['title'], url=b['url'])
        if b['u'] in by_uri and source not in by_uri[b['u']]['sources']:
            by_uri[b['u']]['sources'].append(source)

    for b in query(f'''SELECT DISTINCT ?u ?hints WHERE {{ GRAPH ?g {{
        ?u <{NS}identityHints> ?hints . }} {ACTIVE_GRAPH} FILTER(STRSTARTS(STR(?g), "{BLOCK_BASE}")) }}''', ('u', 'hints')):
        if b['u'] in by_uri:
            node = by_uri[b['u']]
            node['identityHints'] = sorted(set(node.get('identityHints', []) + json.loads(b['hints'])))
            node['identityStatus'] = 'name_merged'

    for b in query(f'''SELECT DISTINCT ?u ?tags WHERE {{ GRAPH ?g {{
        ?u <{NS}tags> ?tags . }} {ACTIVE_GRAPH} FILTER(STRSTARTS(STR(?g), "{BLOCK_BASE}")) }}''', ('u', 'tags')):
        if b['u'] in by_uri:
            node = by_uri[b['u']]
            merged_tags = {(tag['dimension'], tag['value']): tag for tag in node.get('tags', [])}
            merged_tags.update({(tag['dimension'], tag['value']): tag for tag in json.loads(b['tags'])})
            node['tags'] = [merged_tags[key] for key in sorted(merged_tags)]

    for b in query(f'''SELECT ?u ?value WHERE {{ GRAPH ?g {{
        ?u <{NS}canonicalName> ?value . }} {ACTIVE_GRAPH} FILTER(STRSTARTS(STR(?g), "{BLOCK_BASE}")) }}''',
                   ('u', 'value')):
        if b['u'] in by_uri:
            by_uri[b['u']]['canonicalName'] = b['value']
    for b in query(f'''SELECT ?u ?value WHERE {{ GRAPH ?g {{
        ?u <{NS}regionLevel> ?value . }} {ACTIVE_GRAPH} FILTER(STRSTARTS(STR(?g), "{BLOCK_BASE}")) }}''',
                   ('u', 'value')):
        if b['u'] in by_uri:
            try:
                by_uri[b['u']]['regionLevel'] = int(b['value'])
            except (TypeError, ValueError):
                pass

    # Normalize historical source-scoped people in the public view as well.
    # Original block graphs remain available unchanged for provenance/audit.
    merged, aliases = {}, {}
    for node in nodes:
        person = bool(node.get('identityStatus')) or bool({'Person', 'Official'} & {node['cls'], *node['up']})
        key = uri_for(node['name']) if person else node['id']
        aliases[node['id']] = key
        if key not in merged:
            merged[key] = dict(node, id=key, aliases=[node['id']], out_edges=0, in_edges=0)
        else:
            current = merged[key]
            current['nblocks'] += node['nblocks']
            current['aliases'].append(node['id'])
            for field in ('sources', 'identityHints', 'up', 'tags'):
                current[field] = current.get(field, []) + [v for v in node.get(field, []) if v not in current.get(field, [])]
        if person:
            merged[key]['identityStatus'] = 'name_merged'
    nodes = list(merged.values())
    canonical_edges = {}
    for edge in edges:
        edge = dict(edge, source=aliases[edge['source']], target=aliases[edge['target']])
        canonical_edges[(edge['source'], edge['target'], edge['pred'])] = edge
    edges = list(canonical_edges.values())
    for edge in edges:
        merged[edge['source']]['out_edges'] += 1
        merged[edge['target']]['in_edges'] += 1

    # Seed facts participate in nodes/edges but must not inflate extraction
    # block/triple quality metrics.
    seed_filter = f' && STR(?g) != "{SEED_GRAPH}"'
    blocks = int(query(f'SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE {{ GRAPH ?g {{ ?s ?p ?o }} {ACTIVE_GRAPH} FILTER(STRSTARTS(STR(?g), "{BLOCK_BASE}"){seed_filter}) }}', ('n',))[0]['n'])
    triples = int(query(f'SELECT (COUNT(*) AS ?n) WHERE {{ GRAPH ?g {{ ?s ?p ?o }} {ACTIVE_GRAPH} FILTER(STRSTARTS(STR(?g), "{BLOCK_BASE}"){seed_filter}) }}', ('n',))[0]['n'])
    return dict(nodes=nodes, edges=edges, dangling=dangling, type_conflicts=type_conflicts,
                stats=dict(blocks=blocks, triples=triples, entities=len(nodes)))


def render_html(data, title='本体图谱'):
    """Render the single-file HTML (local echarts + embedded data)."""
    payload = json.dumps(data, ensure_ascii=False).replace('<', '\\u003c')
    categories = [{'name': c} for c in sorted({n['cls'] for n in data['nodes']})]

    html = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>__TITLE__</title>
<style>
  html, body { margin: 0; height: 100%; font-family: "Microsoft YaHei", sans-serif; }
  #layout { display: flex; height: 100%; }
  #graph { flex: 1; height: 100%; }
  #panel { width: 320px; padding: 16px; box-shadow: -2px 0 8px rgba(0,0,0,.08); overflow-y: auto; }
  #panel h2 { font-size: 15px; margin: 4px 0; }
  .tag { display: inline-block; padding: 1px 8px; margin: 2px; border-radius: 10px; color: #fff; font-size: 12px; }
  .sec { font-size: 12px; color: #555; margin-top: 10px; line-height: 1.7; word-break: break-all; }
  #search { width: 100%; padding: 6px 10px; box-sizing: border-box; margin-bottom: 8px; }
  #pathq { width: 100%; padding: 6px 10px; box-sizing: border-box; }
  #pathbar { margin: 6px 0 2px; }
  #pathbar button { padding: 3px 12px; margin-right: 6px; cursor: pointer; }
  .pathline { padding: 4px 6px; margin: 4px 0; background: #fdf6ec; border-left: 3px solid #e74c3c; font-size: 12px; line-height: 1.8; }
  .arrow { color: #e74c3c; font-weight: bold; padding: 0 2px; }
  table { width: 100%; border-collapse: collapse; font-size: 12px; margin-top: 10px; }
  td, th { border-bottom: 1px solid #eee; padding: 3px 4px; text-align: left; }
  .num { text-align: right; }
  #tbl tbody tr { cursor: pointer; }
  #tbl tbody tr.selected { background: #fff0e8; outline: 1px solid #e74c3c; }
  #matches button { margin: 3px; cursor: pointer; text-align: left; }
</style>
</head>
<body>
<div id="layout">
  <div id="graph"></div>
  <div id="panel">
    <h2>__TITLE__ <span id="stat" style="font-weight:400;color:#888;font-size:12px"></span></h2>
    <input id="search" placeholder="搜索实体名…（回车定位）">
    <div id="matches" class="sec"></div>
    <div id="detail" class="sec">点击节点查看详情。</div>
    <h2 style="margin-top:18px">推理链路</h2>
    <input id="pathq" placeholder="一个实体看关联；「A B」查路径（≤4跳）">
    <div id="pathbar"><button id="pathbtn">查询</button><button id="pathclr">清除</button></div>
    <div id="pathout" class="sec">输入实体名（支持模糊匹配），展示问题到答案的关联线路。</div>
    <h2 style="margin-top:18px">实体清单</h2>
    <table id="tbl"><thead><tr><th>名称</th><th>类型</th><th class="num">块数</th></tr></thead><tbody></tbody></table>
  </div>
</div>
<script src="echarts.min.js"></script>
<script>
const DATA = __PAYLOAD__;
const COLORS = __COLORS__;
const CATS = __CATS__;
const TAG_COLORS = {entityType:'#8e44ad', industry:'#16a085', ownership:'#d35400',
                    scale:'#7f8c8d', qualification:'#2980b9', function:'#c0392b'};
const TAG_LABELS = {entityType:'类型', industry:'行业', ownership:'所有制',
                    scale:'规模', qualification:'资质', function:'职能'};
const tagText = t => `${TAG_LABELS[t.dimension] || t.dimension}：${t.value}`;
const tagChips = n => (n.tags || []).map(t =>
  `<span class="tag" style="background:${TAG_COLORS[t.dimension] || '#7f8c8d'}">${esc(tagText(t))}</span>`).join('');
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
document.getElementById('stat').textContent =
  `(${DATA.stats.blocks} 个原文块 / ${DATA.stats.triples} 三元组 / ${DATA.stats.entities} 实体)`;

// ---- 侧栏实体清单（按出现块数排序）----
const tbl = document.querySelector('#tbl tbody');
[...DATA.nodes].sort((a, b) => b.nblocks - a.nblocks || a.name.localeCompare(b.name)).forEach(n => {
  const tr = document.createElement('tr');
  tr.dataset.id = n.id;
  tr.innerHTML = `<td>${esc(n.name)}</td><td><span class="tag" style="background:${COLORS[n.cls] || '#f39c12'}">${esc(n.cls)}</span></td><td class="num">${n.nblocks}</td>`;
  tr.onclick = () => focusNode(n.id);
  tbl.appendChild(tr);
});

// ---- 图 ----
const chart = echarts.init(document.getElementById('graph'));
const colorOf = c => COLORS[c] || '#f39c12';
const nameOf = id => (DATA.nodes.find(x => x.id === id) || {}).name || id;
const baseNodes = DATA.nodes.map(n => ({ ...n, category: CATS.findIndex(c => c.name === n.cls), symbolSize: Math.min(46, 14 + n.nblocks * 8) }));
const baseLinks = DATA.edges.map(e => ({ source: e.source, target: e.target, value: e.pred,
  label: { show: true, fontSize: 9, color: '#999', formatter: () => e.pred } }));
const option = {
  // ECharts uses a scaled hover layer above this threshold (default 3000);
  // on the dense policy graph the emphasis stroke is then scaled by the symbol
  // (2px * 23) and becomes a huge red disc. Keep normal rendering instead.
  hoverLayerThreshold: Infinity,
  tooltip: { formatter: p => p.dataType === 'node' ? `${esc(p.data.name)}<br/>类型: ${esc(p.data.cls)}｜出现于 ${p.data.nblocks} 个原文块`
      + ((p.data.tags || []).length ? `<br/>${p.data.tags.map(t => esc(tagText(t))).join('｜')}` : '') : esc(p.data.value) },
  legend: { data: CATS.map(c => c.name), type: 'scroll', top: 0 },
  series: [{
    type: 'graph', layout: 'force', roam: true, draggable: false,
    // Thin outline instead of a large glow: graph roam scales the whole group,
    // so a fixed 12px shadow becomes a huge halo on the denser policy graph.
    emphasis: {scale: false, focus: 'none', itemStyle: {borderColor: '#e74c3c', borderWidth: 2,
      shadowBlur: 0}, lineStyle: {width: 2, color: '#e74c3c'}},
    force: { repulsion: 320, edgeLength: [60, 140], gravity: 0.08 },
    categories: CATS.map((c, i) => ({ name: c.name, itemStyle: { color: colorOf(c.name) } })),
    label: { show: true, position: 'right', fontSize: 11, formatter: p => p.data.name.length > 12 ? p.data.name.slice(0, 12) + '…' : p.data.name },
    edgeLabel: { show: false },
    data: baseNodes,
    links: baseLinks,
  }],
};
chart.setOption(option);

// ECharts only starts a roam drag when the pointer is inside the node cloud's
// bounding rect. That leaves dead margins after focus/zoom, so allow panning
// anywhere on the canvas. Nodes are not draggable (draggable:false), which
// means a drag that starts on the selected node also pans the view.
const seriesView = chart.getViewOfSeriesModel(chart.getModel().getSeriesByIndex(0));
seriesView?._controller?.setPointerChecker?.((ev, x, y) =>
  x >= 0 && y >= 0 && x <= chart.getWidth() && y <= chart.getHeight());

// ---- 节点详情侧栏 ----
function focusNode(id, select = true) {
  const n = DATA.nodes.find(x => x.id === id);
  if (!n) return;
  const src = (n.sources || []).map(s => {
    const title = typeof s === 'string' ? s : s.title;
    const url = typeof s === 'object' && /^https?:\/\//i.test(s.url) ? s.url : '';
    return `<div>· ${url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(title || url)}</a>` : esc(title)}</div>`;
  }).join('') || '<div>无</div>';
  document.getElementById('detail').innerHTML = `
    <h2>${esc(n.name)}</h2>
    <div><span class="tag" style="background:${colorOf(n.cls)}">${esc(n.cls)}</span>
         <span class="sec">出现于 ${n.nblocks} 个原文块</span></div>
    ${(n.tags || []).length ? `<div class="sec"><b>标签</b>：${tagChips(n)}</div>` : ''}
    ${(n.alt_cls || []).length ? `<div class="sec">⚠ 同实体其他类型（漂移）：${esc(n.alt_cls.join('、'))}</div>` : ''}
    <div class="sec"><b>类型层级</b>：${esc(n.up.join(' → ')) || '（根类）'}</div>
    <div class="sec"><b>关系边</b>：出 ${n.out_edges} / 入 ${n.in_edges}</div>
    <div class="sec"><b>出处</b>：${src}</div>`;
  if (select) highlight([id], []);
}
chart.on('click', p => p.dataType === 'node' && focusNode(p.data.id));

// ---- 推理链路查询 ----
// 无向邻接表（路径搜索不区分方向，展示时用箭头方向表达）
const adj = new Map();
function addAdj(a, b, e) { if (!adj.has(a)) adj.set(a, []); adj.get(a).push({ other: b, pred: e.pred, s: e.source, o: e.target }); }
DATA.edges.forEach(e => { addAdj(e.source, e.target, e); addAdj(e.target, e.source, e); });

const MAX_HOPS = 4, MAX_PATHS = 3;

// 迭代加深找 a→b 的路径（逐层加深保证先返回最短；图规模小，够用），每层取前 MAX_PATHS 条
function findPaths(a, b) {
  for (let depth = 1; depth <= MAX_HOPS; depth++) {
    const found = [];
    const stack = [{ node: a, visited: new Set([a]), edges: [] }];
    while (stack.length && found.length < MAX_PATHS) {
      const cur = stack.pop();
      for (const { other, pred, s, o } of (adj.get(cur.node) || [])) {
        if (cur.visited.has(other)) continue;
        const edges = [...cur.edges, { s, o, pred }];
        if (other === b) { found.push(edges); continue; }
        if (edges.length >= depth) continue;
        stack.push({ node: other, visited: new Set([...cur.visited, other]), edges });
      }
    }
    if (found.length) return found.slice(0, MAX_PATHS);
  }
  return [];
}

function pathText(edges) {
  return edges.map(e => ` ${esc(nameOf(e.s))} <span class="arrow">—${esc(e.pred)}→</span> ${esc(nameOf(e.o))}`).join('').trim();
}

// Native visual states preserve force coordinates, node scale and viewport.
function highlight(nodeIds, edgeKeys) {
  const nset = new Set(nodeIds), eset = new Set(edgeKeys);
  const selected = nset.size > 0;
  const legend = chart.getOption().legend?.[0]?.selected || {};
  if (selected) new Set(DATA.nodes.filter(n => nset.has(n.id)).map(n => n.cls))
    .forEach(name => {if (legend[name] === false) chart.dispatchAction({type: 'legendSelect', name});});
  const seriesData = chart.getModel().getSeriesByIndex(0).getData();
  chart.dispatchAction({type: 'downplay', seriesIndex: 0});
  chart.dispatchAction({type: 'downplay', seriesIndex: 0, dataType: 'edge'});
  const indices = [];
  seriesData.each(i => { if (nset.has(seriesData.getId(i))) indices.push(i); });
  if (indices.length) chart.dispatchAction({type: 'highlight', seriesIndex: 0, dataIndex: indices});
  const edges = chart.getModel().getSeriesByIndex(0).getEdgeData(), edgeIndices = [];
  edges.each(i => {
    const l = edges.getRawDataItem(i);
    if (eset.has(`${l.source}|${l.target}|${l.value}`)) edgeIndices.push(i);
  });
  if (edgeIndices.length) chart.dispatchAction({type: 'highlight', seriesIndex: 0, dataType: 'edge', dataIndex: edgeIndices});
  document.querySelectorAll('#tbl tbody tr').forEach(tr => tr.classList.toggle('selected', nset.has(tr.dataset.id)));
  if (selected) centerNodes(nodeIds);
}

function centerNodes(ids) {
  const seriesData = chart.getModel().getSeriesByIndex(0).getData(), points = [];
  seriesData.each(i => {
    const point = seriesData.getItemLayout(i);
    if (ids.includes(seriesData.getId(i)) && point?.every(Number.isFinite))
      points.push(chart.convertToPixel({seriesIndex: 0}, point));
  });
  if (!points.length) return;
  const xs = points.map(p => p[0]), ys = points.map(p => p[1]);
  const cx = (Math.min(...xs) + Math.max(...xs)) / 2, cy = (Math.min(...ys) + Math.max(...ys)) / 2;
  panGraph(chart.getWidth() / 2 - cx, chart.getHeight() / 2 - cy);
}

function panGraph(dx, dy) {
  const series = chart.getModel().getSeriesByIndex(0);
  const group = chart.getViewOfSeriesModel(series).group;
  // ECharts graphRoam updates the coordinate model with update:'none'. Native
  // dragging also translates this group; mirror that step for sidebar focus.
  group.attr({x: group.x + dx, y: group.y + dy});
  chart.dispatchAction({type: 'graphRoam', seriesIndex: 0, dx, dy});
}

function resolveEntity(kw) {
  const uri = DATA.nodes.find(n => n.id === kw);
  if (uri) return uri;
  const exact = DATA.nodes.filter(n => n.name === kw);
  if (exact.length === 1) return exact[0];
  if (exact.length > 1) { showMatches(exact); return null; }
  const hits = DATA.nodes.filter(n => n.name.includes(kw));
  if (hits.length > 1) showMatches(hits);
  return hits.length === 1 ? hits[0] : null;
}

function showMatches(hits) {
  const out = document.getElementById('matches');
  out.replaceChildren();
  out.append(document.createTextNode(hits.length ? `命中 ${hits.length} 个实体：` : '未找到实体。'));
  hits.forEach(n => {
    const button = document.createElement('button');
    button.textContent = n.name;
    button.onclick = () => { focusNode(n.id); document.getElementById('pathq').value = n.id; };
    out.append(button);
  });
  highlight(hits.map(n => n.id), []);
}

function runPathQuery() {
  const raw = document.getElementById('pathq').value.trim();
  const out = document.getElementById('pathout');
  if (!raw) { out.textContent = '请输入实体名。'; return; }
  const parts = raw.split(/\s+|(?:\s*(?:到|->|→)\s*)/).filter(Boolean);
  const a = resolveEntity(parts[0]);
  if (!a) { out.textContent = `未找到唯一实体「${parts[0]}」，请在搜索结果中选择具体实体。`; return; }

  // 单实体：展示一跳关联
  if (parts.length === 1) {
    const rels = adj.get(a.id) || [];
    if (!rels.length) { out.textContent = `「${a.name}」在图中无关联边。`; highlight([a.id], []); return; }
    const edges = rels.map(r => ({ s: r.s, o: r.o, pred: r.pred }));
    highlight([a.id, ...rels.map(r => r.other)], edges.map(e => edgeKeyOf(e)));
    out.innerHTML = `<b>「${esc(a.name)}」的关联线路</b>（${rels.length} 条）：<br>` +
      edges.map(e => `<div class="pathline">${pathText([e])}</div>`).join('');
    return;
  }

  // 双实体：找两者间路径
  const b = resolveEntity(parts.slice(1).join(' '));
  if (!b) { out.textContent = `未找到唯一实体「${parts.slice(1).join(' ')}」，请按具体实体 URI 查询。`; return; }
  if (a.id === b.id) { out.innerHTML = '起点终点相同。'; return; }
  const paths = findPaths(a.id, b.id);
  if (!paths.length) {
    highlight([a.id, b.id], []);
    out.textContent = `「${a.name}」与「${b.name}」之间 ≤${MAX_HOPS} 跳内无路径。`;
    return;
  }
  highlight([...new Set(paths.flat().flatMap(e => [e.s, e.o]))], paths.flat().map(edgeKeyOf));
  out.innerHTML = `<b>「${esc(a.name)}」到「${esc(b.name)}」的推理链路</b>（${paths.length} 条，最短 ${paths[0].length} 跳）：<br>` +
    paths.map(p => `<div class="pathline">${pathText(p)}</div>`).join('');
}

function edgeKeyOf(e) { return `${e.s}|${e.o}|${e.pred}`; }

document.getElementById('pathbtn').onclick = runPathQuery;
document.getElementById('pathq').addEventListener('keydown', ev => { if (ev.key === 'Enter') runPathQuery(); });
document.getElementById('pathclr').onclick = () => {
  highlight([], []);
  document.getElementById('pathout').textContent = '输入实体名（支持模糊匹配），展示问题到答案的关联线路。';
};

// ---- 搜索定位 ----
document.getElementById('search').addEventListener('keydown', ev => {
  if (ev.key !== 'Enter') return;
  const kw = ev.target.value.trim();
  const hit = DATA.nodes.filter(n => n.name.includes(kw));
  if (!kw) { showMatches([]); return; }
  showMatches(hit);
  if (hit.length === 1) focusNode(hit[0].id, false);
});
// 方向键平移视图（选中/高亮不影响）；输入框内不拦截。
window.addEventListener('keydown', ev => {
  const tag = ev.target?.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA' || ev.target?.isContentEditable) return;
  const step = ev.shiftKey ? 240 : 80;
  const delta = {ArrowLeft: [step, 0], ArrowRight: [-step, 0],
                 ArrowUp: [0, step], ArrowDown: [0, -step]}[ev.key];
  if (!delta) return;
  ev.preventDefault();
  panGraph(delta[0], delta[1]);
});
window.addEventListener('resize', () => chart.resize());
</script>
</body>
</html>
"""
    html = html.replace('__TITLE__', escape(title))
    html = html.replace('__PAYLOAD__', payload)
    html = html.replace('__COLORS__', json.dumps(CLASS_COLORS))
    html = html.replace('__CATS__', json.dumps(categories))
    return html


def default_out_dir():
    work_dir = Path(os.environ.get('WISEFLOW_WORK_DIR') or
                    Path(__file__).resolve().parent.parent / 'work_dir').expanduser().resolve()
    return work_dir / 'viewer'


def refresh_viewer(graph_path, out_dir=None, *, store=None, title='本体图谱'):
    """Generate/refresh the viewer HTML. Called after each batch, or via CLI."""
    out_dir = Path(out_dir) if out_dir else default_out_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    # Read-only coexists with the extraction server's writer handle.
    if store is None:
        store = Store.read_only(str(Path(graph_path)))
    data = collect(store)
    (out_dir / 'index.html').write_text(render_html(data, title), encoding='utf-8')
    shutil.copyfile(STATIC_DIR / 'echarts.min.js', out_dir / 'echarts.min.js')
    logger.info('Viewer refreshed: %s (%d entities / %d edges / %d dangling)',
                out_dir / 'index.html', len(data['nodes']), len(data['edges']), data['dangling'])
    return out_dir / 'index.html'


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    parser = argparse.ArgumentParser(description='Regenerate the ontology graph viewer HTML')
    parser.add_argument('--graph', default=None, help='Oxigraph store path (default: WISEFLOW_WORK_DIR/ontology)')
    parser.add_argument('--out', default=None, help='output directory (default: WISEFLOW_WORK_DIR/viewer)')
    args = parser.parse_args()
    graph = args.graph or (Path(os.environ.get('WISEFLOW_WORK_DIR') or
                                Path(__file__).resolve().parent.parent / 'work_dir').resolve() / 'ontology')
    print(refresh_viewer(graph, args.out))


if __name__ == '__main__':
    main()
