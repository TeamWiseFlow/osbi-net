"""Inspect/start the independent ontology workflow through its single graph owner."""
import argparse
import asyncio
import json
import os

import httpx


def print_stats(data):
    """Print unused-first tables so schema pruning candidates are obvious."""
    stats = data['stats']
    print(f"{data['title']} ({data['libraryId']})  schema={data['schemaId'][:12]}")
    print(f"实体 {stats['entities']}  关系 {stats['edges']}  原文块 {stats['blocks']}  "
          f"三元组 {stats['triples']}  孤立实体 {stats['orphans']}")
    print('\n类（实体数，0 = 未使用）:')
    for row in data['classes']:
        print(f"  {row['entities']:>5}  {row['label']}  ({row['name']})")
    print('\n谓词（边数，0 = 未使用）:')
    for row in data['predicates']:
        print(f"  {row['edges']:>5}  {row['label']}  ({row['name']})")
    if data['orphans']:
        print('\n孤立实体（无任何关系边）:')
        for row in data['orphans']:
            print(f"  {row['label']}  {row['name']}  {row['uri']}")


def main():
    parser = argparse.ArgumentParser(description='Inspect or run the selected-original-block ontology backlog')
    commands = parser.add_subparsers(dest='command', required=True)
    status = commands.add_parser('status')
    status.add_argument('--library', default=None, help='Only inspect this ontology library')
    run = commands.add_parser('run')
    run.add_argument('--max-blocks', type=int)
    run.add_argument('--wait', action='store_true', help='Wait for this batch; disconnecting does not cancel it')
    run.add_argument('--reprocess', action='store_true', help='Also rebuild completed blocks with an outdated extraction identity')
    run.add_argument('--library', default=None, help='Only process this library; omitted processes all libraries')
    run.add_argument('--created-from', default=None, help='Only jobs created at/after this date (YYYY-MM-DD, Asia/Shanghai) or ISO timestamp')
    run.add_argument('--created-to', default=None, help='Only jobs created at/before this date (YYYY-MM-DD, Asia/Shanghai) or ISO timestamp')
    viewer = commands.add_parser('viewer', help='Regenerate the static graph viewer HTML (no batch)')
    viewer.add_argument('--out', default=None, help='Output directory (default: WISEFLOW_WORK_DIR/viewer)')
    viewer.add_argument('--library', default=None, help='Export this library viewer')
    stats = commands.add_parser('stats', help='Show class/predicate usage and orphan entities for schema pruning')
    stats.add_argument('--library', default=None, help='Inspect this library; omitted uses the default library')
    stats.add_argument('--json', action='store_true', help='Print the raw JSON instead of tables')
    seed = commands.add_parser('seed', help='Load the deterministic Shanghai admin skeleton into one library')
    seed.add_argument('--library', default=None, help='Target library; omitted loads the default library')
    args = parser.parse_args()
    if args.command == 'run' and args.max_blocks is not None and args.max_blocks < 1:
        parser.error('--max-blocks must be positive')
    async def execute():
        base = os.environ.get('WISEFLOW_BACKEND_URL', 'http://127.0.0.1:8077').rstrip('/')
        async with httpx.AsyncClient(base_url=base, timeout=30) as client:
            if args.command == 'viewer':
                from .ontology_viewer import default_out_dir
                from pathlib import Path
                out = Path(args.out) if args.out else default_out_dir() / (args.library or 'policy')
                prefix = f'/v1/ontology/libraries/{args.library}/viewer/' if args.library else '/v1/ontology/viewer/'
                response = await client.get(prefix)
                response.raise_for_status()
                script = await client.get(prefix + 'echarts.min.js')
                script.raise_for_status()
                out.mkdir(parents=True, exist_ok=True)
                (out / 'index.html').write_text(response.text, encoding='utf-8')
                (out / 'echarts.min.js').write_bytes(script.content)
                print(out / 'index.html')
                return 0
            if args.command == 'stats':
                response = await client.get('/v1/ontology/stats',
                                            params={'libraryId': args.library} if args.library else None)
                response.raise_for_status()
                data = response.json()['data']
                if args.json:
                    print(json.dumps(data, ensure_ascii=False, indent=2))
                else:
                    print_stats(data)
                return 0
            if args.command == 'seed':
                response = await client.post('/v1/ontology/seeds',
                                             params={'libraryId': args.library} if args.library else None)
                response.raise_for_status()
                print(json.dumps(response.json().get('data', response.json()), ensure_ascii=False))
                return 0
            if args.command == 'run':
                response = await client.post('/v1/ontology/batches', json={
                    'maxBlocks': args.max_blocks, 'reprocess': args.reprocess,
                    'libraryId': args.library, 'createdFrom': args.created_from,
                    'createdTo': args.created_to})
                response.raise_for_status()
            while True:
                response = await client.get('/v1/ontology/batches/status', params={'libraryId': args.library} if args.library else None)
                response.raise_for_status()
                status = response.json()['data']
                if args.command != 'run' or not args.wait or not status['running']:
                    print(json.dumps(status, ensure_ascii=False, indent=2))
                    batch = status['lastBatch'] or {}
                    return int(args.command == 'run' and args.wait and
                               bool(batch.get('failed') or batch.get('windowExpired')))
                await asyncio.sleep(2)

    raise SystemExit(asyncio.run(execute()))


if __name__ == '__main__':
    main()
