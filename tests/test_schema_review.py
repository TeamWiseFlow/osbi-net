"""Explicit schema calibration preserves old graphs until re-extraction succeeds."""
import json
import time
import pytest
from osbi_server.admin import review,activate,reprocess
from conftest import material,wait_job


def test_manual_schema_revision_and_evidence_preservation(shared):
    client,service,model,headers=shared
    model.unknown=True
    job=client.post('/v1/materials',headers=headers,json=material()).json()
    assert wait_job(client,headers,job['jobId'])['state']=='succeeded'
    report=review(service.repo,'economy')
    assert report['classes']['企业']==1
    assert report['samples'][0]['originalBlock']['text']
    assert report['samples'][0]['statistics']['droppedSamples']
    old=report['activeSchemaId']
    text=service.repo.one('SELECT body FROM schema_revisions WHERE id=?',(old,))['body']
    revised=text+'\n# Explicit administrator revision for isolated acceptance.\n'
    new=activate(service.repo,'economy',revised,old)['activeSchemaId']
    with pytest.raises(ValueError):activate(service.repo,'economy',text,old)
    old_graph=client.get('/v1/channels/economy/graph',headers=headers).json()
    assert len(old_graph['nodes'])==2
    jobs=reprocess(service.repo,'economy')['workIds']
    assert jobs
    deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        row=service.repo.one('SELECT * FROM work WHERE id=?',(jobs[0],))
        if row['state'] in {'succeeded','failed'}:break
        time.sleep(.03)
    assert row['state']=='succeeded',row
    result=client.get('/v1/channels/economy/graph',headers=headers).json()
    assert len(result['nodes'])==2
    assert all(b['schemaId']==new for b in result['blocks'].values())
    assert service.repo.one('SELECT 1 FROM schema_revisions WHERE id=?',(old,))
    assert len(service.repo.rows('SELECT * FROM graph_intents'))==2
