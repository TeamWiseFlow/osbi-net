"""Two real HTTPS client outboxes share one graph with an isolated model provider."""
import threading
import time
import uvicorn
from fastapi.testclient import TestClient
from conftest import ExampleSettings as Settings
from osbi_server.service import Service
from osbi_server.app import create_app as server_app
from osbi_client.app import create_app as client_app
from osbi_client.daemon import Daemon
from conftest import Model
from test_client import Browser
from demo_harness import certificate, free_port


def test_two_daemons_over_verified_tls(tmp_path):
    cert,key=certificate(tmp_path)
    model=Model()
    shared=Service(Settings(tmp_path/'server'),model)
    token=shared.repo.issue_key('demo',time.time()+1000)['key']
    port=free_port()
    server=uvicorn.Server(uvicorn.Config(server_app(service=shared),host='127.0.0.1',port=port,
                                       ssl_certfile=str(cert),ssl_keyfile=str(key),log_level='error'))
    thread=threading.Thread(target=server.run,daemon=True);thread.start()
    try:
        deadline=time.monotonic()+10
        while not server.started and time.monotonic()<deadline:time.sleep(.03)
        assert server.started
        clients=[]
        for name in ['a','b']:
            daemon=Daemon(tmp_path/name,browser=Browser())
            daemon.state.save_config({'serverUrl':f'https://127.0.0.1:{port}','serverKey':token,'serverCA':str(cert),
                                      'defaultChannel':'economy',
                                      'sources':[{'id':'source','url':'https://example.org/news'}]})
            client=TestClient(client_app(daemon=daemon),base_url='http://127.0.0.1')
            client.__enter__();client.get('/settings')
            clients.append((client,daemon))
        jobs=[]
        for client,_ in clients:
            response=client.post('/api/runs',headers={'X-OSBI-Local':'1'},json={'sourceId':'source'})
            assert response.status_code==200,response.text
            jobs.append(response.json()['id'])
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            states=[c.get('/api/runs/'+job).json()['state'] for (c,_),job in zip(clients,jobs)]
            if states==['succeeded','succeeded']:break
            time.sleep(.05)
        assert states==['succeeded','succeeded']
        assert model.calls.count('ontology_entities')==1
        assert shared.repo.one('SELECT COUNT(*) n FROM requests')['n']==2
        for client,daemon in clients:
            graph=client.get('/api/server/channels/economy/graph').json()
            assert len(graph['nodes'])==2 and len(graph['edges'])==1
            # Both clients observed the same page, so its source is stored once.
            assert len(next(iter(graph['blocks'].values()))['sources'])==1
            assert daemon.state.one("SELECT COUNT(*) n FROM outbox WHERE state='done'")['n']==1
    finally:
        for client,_ in locals().get('clients',[]):client.__exit__(None,None,None)
        server.should_exit=True;thread.join(timeout=10)
