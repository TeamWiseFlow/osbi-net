"""Offline restore preserves shared graph, key authorization and schema bindings."""
import time
import pytest
from fastapi.testclient import TestClient
from osbi_server.config import Settings
from osbi_server.service import Service
from osbi_server.app import create_app
from osbi_server.backup import backup,restore
from conftest import Model,material,wait_job


def test_consistent_offline_restore_and_tamper_detection(tmp_path):
    data=tmp_path/'original';snapshot=tmp_path/'snapshot'
    original=Service(Settings(data),Model())
    key=original.repo.issue_key('demo',time.time()+1000)['key']
    headers={'Authorization':'Bearer '+key}
    with TestClient(create_app(service=original),base_url='https://testserver') as client:
        job=client.post('/v1/materials',headers=headers,json=material()).json()
        assert wait_job(client,headers,job['jobId'])['state']=='succeeded'
        with pytest.raises(RuntimeError):backup(data,snapshot)
    backup(data,snapshot)
    restore(snapshot,tmp_path/'restored')
    restored=Service(Settings(tmp_path/'restored'),Model())
    with TestClient(create_app(service=restored),base_url='https://testserver') as client:
        assert client.get('/v1/jobs/'+job['jobId'],headers=headers).json()['state']=='succeeded'
        graph=client.get('/v1/channels/economy/graph',headers=headers).json()
        assert len(graph['nodes'])==2 and len(graph['edges'])==1
    with (snapshot/'metadata.db').open('ab') as file:file.write(b'tampered')
    with pytest.raises(ValueError):restore(snapshot,tmp_path/'invalid-restore')
    assert not (tmp_path/'invalid-restore').exists()
