"""Opt-in current-runtime browser acceptance with fresh profiles and local pages."""
import asyncio
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import os
from pathlib import Path
import threading
import pytest
from osbi_client.browser import Browser


@pytest.mark.skipif(os.environ.get('OSBI_BROWSER_E2E')!='1',reason='Requires explicitly installed pinned browser assets')
def test_interactive_current_tab_projection(tmp_path):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            data='''<html><body><button id="go" onclick="document.body.innerHTML='<article><h1>产品发布</h1><p>甲科技公司参与产品发布会，展示新产品。</p></article>'">进入结果</button></body></html>'''.encode()
            self.send_response(200);self.send_header('Content-Type','text/html;charset=utf-8');self.end_headers();self.wfile.write(data)
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    async def run():
        browser=Browser(tmp_path/'browser')
        try:
            await browser.command('fixture','start')
            lease=(await browser.command('fixture','lease.acquire',{'ttlMs':300000}))['leaseToken']
            url=f'http://127.0.0.1:{server.server_port}/'
            await browser.command('fixture','open',{'url':url},lease)
            await browser.command('fixture','click',{'selector':'#go'},lease)
            material=await browser.capture('fixture',url,lease)
            assert '甲科技公司' in material['blocks'][0]['text']
            assert material['meta']['finalUrl']==url
            await browser.command('fixture','lease.release',{},lease)
        finally:
            await browser.command('fixture','stop')
    try:asyncio.run(run())
    finally:server.shutdown();server.server_close();thread.join()
