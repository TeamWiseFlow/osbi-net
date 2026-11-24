"""node_source_metrics accumulation (observability only, never a gate)."""
import asyncio
import logging

from core.async_database import AsyncDatabaseManager
from wiseflow_server.metrics import NodeSourceMetrics, source_key

META = {'requestedUrl': 'https://news.example.org/a', 'publisher': '示例发布号'}


def test_source_key_prefers_publisher_and_normalizes_host():
    assert source_key(META) == '示例发布号'
    assert source_key({'requestedUrl': 'https://www.example.org/a'}) == 'example.org'
    assert source_key({}) == 'unknown'


def test_metrics_accumulate_per_node_and_source(tmp_path):
    async def run():
        db = AsyncDatabaseManager(db_path=tmp_path / 'data.db', logger=logging.getLogger('test'))
        await db.initialize()
        try:
            metrics = NodeSourceMetrics(db)
            await metrics.record_submission('linfen-shanghai', META, 'community')
            await metrics.record_submission('linfen-shanghai', META, 'economy')
            await metrics.record_selection('linfen-shanghai', META,
                                           selected_blocks=2, selected_links=1)
            await metrics.record_extraction('linfen-shanghai', META, blocks=2, entities=5,
                                            relations=3)
            await metrics.record_extraction('linfen-shanghai', META, blocks=1, entities=1,
                                            relations=0, zero_relation_blocks=1)
            await metrics.record_capture_failure('linfen-shanghai', META)
            rows = await metrics.query('linfen-shanghai')
            assert len(rows) == 1
            row = rows[0]
            assert row['source_key'] == '示例发布号'
            assert row['submissions'] == 2
            assert row['materials'] == 2
            assert row['selected_blocks'] == 2
            assert row['selected_links'] == 1
            assert row['extracted_blocks'] == 3
            assert row['entities'] == 6
            assert row['relations'] == 3
            assert row['zero_relation_blocks'] == 1
            assert row['capture_failures'] == 1
            assert row['first_seen'] <= row['last_seen']
        finally:
            await db.cleanup()

    asyncio.run(run())


def test_node_metrics_ignore_missing_node_identity(tmp_path):
    async def run():
        db = AsyncDatabaseManager(db_path=tmp_path / 'data.db', logger=logging.getLogger('test'))
        await db.initialize()
        try:
            metrics = NodeSourceMetrics(db)
            await metrics.record_submission(None, META, 'community')
            await metrics.ensure_schema()
            assert await metrics.query() == []
        finally:
            await db.cleanup()

    asyncio.run(run())


def test_link_quality_schema_upgrade_preserves_existing_metrics(tmp_path):
    async def run():
        db = AsyncDatabaseManager(db_path=tmp_path / 'data.db', logger=logging.getLogger('test'))
        await db.initialize()
        try:
            async def legacy(db):
                await db.execute('''CREATE TABLE node_source_metrics (
                    node_id TEXT NOT NULL, source_key TEXT NOT NULL,
                    source_url TEXT DEFAULT '', publisher TEXT DEFAULT '',
                    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
                    submissions INTEGER NOT NULL DEFAULT 0,
                    materials INTEGER NOT NULL DEFAULT 0,
                    selected_blocks INTEGER NOT NULL DEFAULT 0,
                    selected_links INTEGER NOT NULL DEFAULT 0,
                    extracted_blocks INTEGER NOT NULL DEFAULT 0,
                    entities INTEGER NOT NULL DEFAULT 0,
                    relations INTEGER NOT NULL DEFAULT 0,
                    zero_relation_blocks INTEGER NOT NULL DEFAULT 0,
                    capture_failures INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (node_id, source_key))''')
                await db.execute('''INSERT INTO node_source_metrics
                    (node_id, source_key, first_seen, last_seen, submissions, selected_links)
                    VALUES ('linfen-shanghai', '示例发布号', '2026-11-24', '2026-11-24', 4, 7)''')
            await db.execute_with_retry(legacy)
            metrics = NodeSourceMetrics(db)
            await asyncio.gather(*(metrics.ensure_schema() for _ in range(8)))
            row, = await metrics.query()
            assert row['submissions'] == 4 and row['selected_links'] == 7
            assert row['selected_content_links'] == row['selected_unknown_links'] == 0
            assert row['selected_navigation_links'] == row['navigation_nav_cap_dropped'] == 0
            assert row['navigation_only_fallback'] == 0
            await metrics.record_selection('linfen-shanghai', META, selected_links=3,
                selected_navigation_links=3, navigation_nav_cap_dropped=1,
                navigation_only_fallback=1)
            row, = await NodeSourceMetrics(db).query()
            assert row['submissions'] == 4 and row['selected_links'] == 10
            assert row['first_seen'] == '2026-11-24'
            assert row['selected_navigation_links'] == 3
            assert row['navigation_nav_cap_dropped'] == row['navigation_only_fallback'] == 1
        finally:
            await db.cleanup()
    asyncio.run(run())
