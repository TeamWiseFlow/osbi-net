function readDom(maxSegments = 1200) {
    const visible = el => !!el && !['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE'].includes(el.tagName) &&
        getComputedStyle(el).display !== 'none' && getComputedStyle(el).visibility !== 'hidden' &&
        (el.getClientRects().length > 0 || getComputedStyle(el).display === 'contents');
    const value = selector => document.querySelector(selector)?.innerText?.trim() || '';
    const metaValue = names => {
        for (const el of document.querySelectorAll('meta[name],meta[property]'))
            if (names.includes((el.getAttribute('name') || el.getAttribute('property')).toLowerCase()) && el.content)
                return el.content;
        return '';
    };
    const pathOf = el => {
        const parts = [];
        for (let p = el; p && p !== document.documentElement; p = p.parentElement) {
            const same = p.parentElement ? [...p.parentElement.children].filter(x => x.tagName === p.tagName) : [p];
            parts.unshift(`${p.tagName.toLowerCase()}:nth-of-type(${same.indexOf(p) + 1})`);
        }
        return `html > ${parts.join(' > ')}`;
    };
    const meta = {
        title: value('#activity-name') || metaValue(['og:title']) || value('article h1') || document.title,
        publisher: value('#js_name') || metaValue(['publisher', 'og:site_name', 'sitename']),
        author: value('#js_author_name') || metaValue(['author', 'article:author', 'dc.creator']),
        publishedAt: value('#publish_time') || metaValue(['pubdate', 'publishdate', 'publish_date', 'article:published_time', 'datepublished']),
        modifiedAt: metaValue(['article:modified_time', 'datemodified', 'last-modified']),
    };
    const metadataNodes = new Set();
    const metadataEvidence = [];
    const metadataLinks = el => [...(el.matches('a[href]') ? [el] : []), ...el.querySelectorAll('a[href]')]
        .map(anchor => ({ text: anchor.innerText, href: anchor.href }));
    const selectors = ['#js_name', '#publish_time', '#js_author_name', '.rich_media_meta_text', '[itemprop="author"]',
        '[itemprop="publisher"]', '[itemprop="datePublished"]', '[itemprop="dateModified"]', '.Article_author', '.Article_time',
        '.Article_description', '.pubtime', '.publish_time', '#pubtime'];
    for (const selector of selectors)
        for (const el of document.querySelectorAll(selector)) {
            metadataNodes.add(el);
            metadataEvidence.push({ path: pathOf(el), selector, text: el.innerText?.trim() || el.getAttribute('content') || '', links: metadataLinks(el) });
        }
    const labelledMetadata = /^(作者|来源|发布机构|发布者|编辑|责编|校审|审核|发布日期|发布时间|印发日期|成文日期|更新时间|修改时间)\s*[：:]/;
    const metadataCandidates = 'p,small,time,em,span,label,div';
    for (const el of document.querySelectorAll(metadataCandidates)) {
        if (!visible(el))
            continue;
        const text = el.innerText?.trim() || '';
        if (text.length <= 140 && labelledMetadata.test(text) &&
            ![...el.querySelectorAll(metadataCandidates)].some(child => visible(child) && labelledMetadata.test(child.innerText?.trim() || ''))) {
            metadataNodes.add(el);
            metadataEvidence.push({ path: pathOf(el), text, selector: 'labelled-metadata', links: metadataLinks(el) });
            if (!meta.author && /^作者\s*[：:]/.test(text))
                meta.author = text.replace(/^作者\s*[：:]\s*/, '');
            if (!meta.publishedAt && /^发布(日期|时间)\s*[：:]/.test(text))
                meta.publishedAt = text.replace(/^发布(日期|时间)\s*[：:]\s*/, '');
            if (/^(印发|成文)日期\s*[：:]/.test(text))
                meta[/^印发/.test(text) ? 'issuedAt' : 'writtenAt'] = text.replace(/^(印发|成文)日期\s*[：:]\s*/, '');
        }
        if (/^修改于\d{4}年\d{1,2}月\d{1,2}日$/.test(text)) {
            metadataNodes.add(el);
            meta.modifiedAt = text.slice(3);
            metadataEvidence.push({ path: pathOf(el), text, selector: 'modification-label' });
        }
        if (['TIME', 'EM', 'SPAN'].includes(el.tagName) &&
            (el.closest('header') || !el.closest('#js_content,.Article_content,article,main')) &&
            [meta.author, meta.publisher, meta.publishedAt, meta.modifiedAt].filter(Boolean).includes(text)) {
            metadataNodes.add(el);
            metadataEvidence.push({ path: pathOf(el), text, selector: 'header-metadata-value' });
        }
    }
    for (const el of document.querySelectorAll('#ivs_content p,.Article_content p,#UCAP-CONTENT p,article p')) {
        const date = el.innerText?.trim() || '';
        const previous = el.previousElementSibling;
        const issuer = previous?.innerText?.trim() || '';
        if (!visible(el) || !visible(previous) || previous.tagName !== 'P' ||
            getComputedStyle(el).textAlign !== 'right' || getComputedStyle(previous).textAlign !== 'right' ||
            !/^\d{4}年\d{1,2}月\d{1,2}日$/.test(date) || issuer.length < 4 || issuer.length > 80 ||
            !meta.title.startsWith(`${issuer}关于`))
            continue;
        meta.issuedBy = issuer;
        meta.signedAt = date;
        for (const node of [previous, el]) {
            metadataNodes.add(node);
            metadataEvidence.push({ path: pathOf(node), text: node.innerText.trim(), selector: 'document-signature', links: metadataLinks(node) });
        }
    }
    const tables = new Map();
    let complexTables = 0, metadataUnresolved = false, infoTables = 0;
    const metaColumn = /^(发布(日期|时间|者|机构|人)|作者|来源|生成日期|更新时间|修改时间)$/;
    const infoLabel = /^(索\s*取\s*号|文件编号|文号|发文字号|发文机关|载体类型|记录形式|公开类别|关键词|内容描述|主题分类|体裁分类|服务对象|信息名称|发布机构|发布日期|成文日期|印发日期)$/;
    const distinctiveInfoLabel = /^(索取号|文件编号|文号|发文字号|发布机构|公开类别|载体类型|记录形式|内容描述)$/;
    const identifierInfoLabel = /^(索\s*取\s*号|文件编号|文号|发文字号)$/;
    const hasColumnHeader = table => !!table.tHead ||
        [...table.rows].some(row => {
            const cells = [...row.cells], headers = cells.filter(cell => cell.tagName === 'TH').length;
            return headers >= 2 && headers > cells.length / 2;
        });
    const keyValueInfoLabels = table => {
        if (hasColumnHeader(table))
            return null;
        const pairs = [];
        for (const row of table.rows) {
            const cells = [...row.cells];
            if (cells.length >= 2 && cells[0].tagName === 'TH' && cells.slice(1).every(cell => cell.tagName === 'TD')) {
                pairs.push([cells[0].innerText.trim(), cells[1].innerText.trim()]);
                continue;
            }
            for (let index = 0; index + 1 < cells.length; index += 2) {
                const label = cells[index].innerText.trim(), value = cells[index + 1].innerText.trim();
                if (infoLabel.test(label) && !infoLabel.test(value))
                    pairs.push([label, value]);
            }
        }
        const labels = pairs.map(([label]) => label);
        return pairs.length >= 2 && labels.filter(label => distinctiveInfoLabel.test(label)).length >= 2 &&
            labels.some(label => identifierInfoLabel.test(label)) ? labels : null;
    };
    for (const table of document.querySelectorAll('table')) {
        const rows = [...table.rows];
        let header = table.tHead?.rows[0] || rows[0];
        if (!header)
            continue;
        // Government tables often put a full-width title before the column
        // labels. It is a caption row, not a one-column header. Only accept
        // leading titles spanning the exact width of a regular header row.
        const titleRows = new Set();
        for (const row of rows) {
            const cells = [...row.cells];
            if (cells.length !== 1 || cells[0].colSpan <= 1 || cells[0].rowSpan !== 1)
                break;
            titleRows.add(row);
        }
        if (titleRows.size) {
            const candidate = rows[titleRows.size];
            const width = candidate?.cells.length || 0;
            if (width > 1 && [...candidate.cells].every(cell => cell.colSpan === 1 && cell.rowSpan === 1) &&
                [...titleRows].every(row => row.cells[0].colSpan === width))
                header = candidate;
            else
                titleRows.clear();
        }
        const labels = [...header.cells].map(el => el.innerText.trim());
        const infoLabels = keyValueInfoLabels(table);
        if (infoLabels) {
            infoTables++;
            metadataNodes.add(table);
            metadataEvidence.push({ path: pathOf(table), text: infoLabels.join(' / '), selector: 'key-value-info-table' });
            tables.set(table, { labels, header, metadataColumns: [], simple: false });
            continue;
        }
        const simple = rows.every(row => titleRows.has(row) || (row.cells.length === labels.length &&
            [...row.cells].every(cell => cell.colSpan === 1 && cell.rowSpan === 1)));
        const metadataColumns = simple ? labels.flatMap((label, index) => metaColumn.test(label) ? [index] : []) : [];
        if (!simple) {
            complexTables++;
            if ([...table.querySelectorAll('th'), ...header.cells].some(cell => metaColumn.test(cell.innerText.trim())))
                metadataUnresolved = true;
        }
        tables.set(table, { labels, header, titleRows, metadataColumns, simple });
        for (const row of rows.filter(row => !titleRows.has(row)))
            for (const index of metadataColumns)
                if (row.cells[index])
                    metadataNodes.add(row.cells[index]);
    }
    const excluded = el => {
        for (let p = el; p && p !== document.body; p = p.parentElement)
            if (metadataNodes.has(p))
                return true;
        return false;
    };
    const contentSelectors = '#js_content,#ivs_content,#UCAP-CONTENT,.Article_content,#zoom,article,main';
    const target = [...document.querySelectorAll(contentSelectors)].filter(visible)
        .sort((a, b) => b.innerText.length - a.innerText.length)[0] || document.body;
    const chromeOf = el => {
        for (let node = el; node && node !== document.body; node = node.parentElement) {
            if (['NAV', 'FOOTER'].includes(node.tagName))
                return true;
            if (node.tagName === 'HEADER' && !node.closest('article,main'))
                return true;
            if (/(?:^|[\s_-])(footer|breadcrumb|share|sharing|social-share|zihao|font-size|cookie-banner)(?:$|[\s_-])/i.test(`${node.id} ${node.className}`))
                return true;
        }
        return false;
    };
    function rendered(node) {
        if (node.nodeType === Node.TEXT_NODE)
            return node.nodeValue.replace(/\s+/gu, ' ');
        if (node.nodeType !== Node.ELEMENT_NODE || !visible(node) || excluded(node) ||
            ['SVG', 'CANVAS', 'IFRAME', 'BUTTON', 'INPUT', 'SELECT', 'TEXTAREA'].includes(node.tagName) ||
            node.matches('[role="button"],.zihao,.font-size'))
            return '';
        if (node.tagName === 'BR')
            return '\n';
        if (node.tagName === 'PRE')
            return node.innerText;
        const text = [...node.childNodes].map(rendered).join('');
        return /^(block|list-item|table-row)$/.test(getComputedStyle(node).display) ? `\n${text}\n` : text;
    }
    const clean = text => text.replace(/[ \t]+\n/g, '\n').replace(/\n[ \t]+/g, '\n').replace(/\n{3,}/g, '\n\n').trim();
    const groups = [{ id: 'G0001', kind: 'page', parentId: null }];
    const groupIds = new Map([[document.body, 'G0001']]);
    const groupTags = new Set(['MAIN', 'ARTICLE', 'SECTION', 'NAV', 'ASIDE', 'HEADER', 'FOOTER', 'UL', 'OL', 'TABLE']);
    function groupFor(el) {
        let group = el;
        while (group && group !== document.body && !groupTags.has(group.tagName))
            group = group.parentElement;
        if (!group || group === document.body)
            return 'G0001';
        if (groupIds.has(group))
            return groupIds.get(group);
        const parentId = groupFor(group.parentElement);
        const id = `G${String(groups.length + 1).padStart(4, '0')}`;
        groups.push({ id, kind: group.tagName.toLowerCase(), parentId, sourcePath: pathOf(group) });
        groupIds.set(group, id);
        return id;
    }
    const segments = [];
    let truncated = false;
    const allWithin = (nodes, selector) => [...new Set(nodes.flatMap(node => node.nodeType === Node.ELEMENT_NODE ?
            [...(node.matches(selector) ? [node] : []), ...node.querySelectorAll(selector)] : []))];
    function emit(el, nodes = [el], partial = false) {
        const text = clean(partial ? nodes.map(rendered).join('') : rendered(el));
        const imageOffset = image => {
            let found = false;
            const before = node => {
                if (found)
                    return '';
                if (node === image) {
                    found = true;
                    return '';
                }
                if (node.nodeType !== Node.ELEMENT_NODE || !node.contains(image))
                    return rendered(node);
                return [...node.childNodes].map(before).join('');
            };
            return clean(nodes.map(before).join('')).length;
        };
        const images = allWithin(nodes, 'img').filter(x => visible(x) && !excluded(x)).map(x => ({
            path: pathOf(x), alt: x.alt, title: x.title, src: x.getAttribute('src'), dataSrc: x.getAttribute('data-src'),
            offset: imageOffset(x),
            complete: x.complete, width: x.naturalWidth, height: x.naturalHeight,
        }));
        const links = allWithin(nodes, 'a[href]').filter(x => visible(x) && !excluded(x)).map(x => ({
            path: pathOf(x), rawHref: x.getAttribute('href'), url: x.href,
            text: clean(rendered(x)), rawText: x.innerText, title: x.title, ariaLabel: x.getAttribute('aria-label') || '',
            images: [...x.querySelectorAll('img')].map(y => ({ alt: y.alt, src: y.getAttribute('src') || y.getAttribute('data-src') })),
        }));
        if (!text && !images.length && !links.length)
            return;
        if (segments.length >= maxSegments) {
            truncated = true;
            return;
        }
        let kind = /^H[1-6]$/.test(el.tagName) ? 'heading' : el.tagName === 'LI' ? 'listitem' : el.tagName === 'TR' ? 'table-row' : el.tagName === 'PRE' ? 'code' : 'paragraph';
        const segment = { path: pathOf(el), kind, groupId: groupFor(el.parentElement), text,
            headingLevel: /^H[1-6]$/.test(el.tagName) ? Number(el.tagName.slice(1)) : null,
            inMain: target !== document.body && target.contains(el), chrome: chromeOf(el),
            regionPath: pathOf(el.closest('article') || target),
            partial, rawText: partial ? text : el.innerText, containsMeta: !partial && [...metadataNodes].some(x => el.contains(x)), links, images };
        if (el.tagName === 'TR') {
            const spec = tables.get(el.closest('table'));
            const title = spec?.titleRows?.has(el);
            segment.kind = title ? 'table-title' : spec?.header === el ? 'table-header' : 'table-row';
            segment.cells = [...el.cells].flatMap((cell, index) => title || !spec?.metadataColumns.includes(index) ? [{ label: !title && spec?.simple ? spec.labels[index] : '', text: clean(rendered(cell)), colSpan: cell.colSpan, rowSpan: cell.rowSpan }] : []);
            segment.itemMeta = title ? {} : Object.fromEntries((spec?.metadataColumns || []).map(index => [spec.labels[index], el.cells[index]?.innerText.trim() || '']));
            if (!title && spec?.metadataColumns.length)
                segment.containsMeta = true;
        }
        segments.push(segment);
    }
    const atomic = new Set(['P', 'LI', 'TR', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'PRE', 'DT', 'DD', 'BLOCKQUOTE']);
    function walk(el) {
        if (!visible(el) || excluded(el) || ['SVG', 'CANVAS', 'IFRAME', 'BUTTON', 'INPUT', 'SELECT', 'TEXTAREA'].includes(el.tagName))
            return;
        if (atomic.has(el.tagName)) {
            emit(el);
            return;
        }
        if (['A', 'IMG'].includes(el.tagName)) {
            emit(el);
            return;
        }
        const children = [...el.childNodes];
        const structural = node => node.nodeType === Node.ELEMENT_NODE &&
            (atomic.has(node.tagName) || /^(block|list-item|table|table-row-group|table-header-group|table-footer-group|table-row|flex|grid)$/.test(getComputedStyle(node).display));
        if (!children.some(structural)) {
            emit(el);
            return;
        }
        let pending = [];
        const flush = () => { if (pending.length)
            emit(el, pending, true); pending = []; };
        for (const child of children) {
            if (structural(child)) {
                flush();
                walk(child);
            }
            else
                pending.push(child);
        }
        flush();
    }
    walk(document.body);
    const controls = [...document.querySelectorAll('button,[role="tab"],[role="button"],div[data-page],div[name]')]
        .filter(x => visible(x) && !excluded(x) && x.innerText.trim()).map(x => ({
        path: pathOf(x), role: x.getAttribute('role') || x.tagName.toLowerCase(), text: clean(rendered(x)),
    }));
    const frames = [...document.querySelectorAll('iframe')].filter(visible).map(x => ({ title: x.title, src: x.getAttribute('src') }));
    return { url: location.href, readyState: document.readyState, bodyText: document.body.innerText,
        targetText: target.innerText, targetPath: pathOf(target), articlePresent: !!document.querySelector('#js_content'),
        passwordForm: [...document.querySelectorAll('input[type="password"]')].some(visible),
        meta, metadataEvidence, groups, segments, controls, frames, truncated, complexTables, infoTables, metadataUnresolved,
        observedBrowser: { userAgent: navigator.userAgent, platform: navigator.platform, languages: [...navigator.languages],
            cores: navigator.hardwareConcurrency, screen: [screen.width, screen.height], timezone: Intl.DateTimeFormat().resolvedOptions().timeZone },
    };
}
