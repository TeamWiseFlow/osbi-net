"""Regressions for grassroots positions, concurrent posts and visit destinations."""
import json
import pytest
from conftest import material, wait_job
from osbi_server.leader_rules import is_target_jurisdiction, is_target_leader_sentence
from osbi_server.ontology import Entity
from osbi_server.ontology_rules import _drop_concurrent_post_segment, normalize_entities


@pytest.mark.parametrize("post", ["上海市图们路居民区前党总支书记", "居民区书记",
    "安泰华庭社区党委书记", "长白新村街道党工委副书记", "上海市委常委、浦东新区区委书记"])
def test_grassroots_and_other_district_posts_are_not_target_leaders(post):
    assert not is_target_jurisdiction(post)


@pytest.mark.parametrize("post", ["静安区委书记", "静安区委常委、宣传部部长", "上海市静安区区长",
                                  "上海市委书记", "上海市委常委、组织部部长"])
def test_genuine_district_and_city_posts_still_qualify(post):
    assert is_target_jurisdiction(post)


def test_concurrent_district_post_and_city_leader_visit_are_distinct():
    assert not is_target_leader_sentence("上海市委常委、浦东新区区委书记李政在会上强调")
    assert is_target_leader_sentence("上海市委书记陈吉宁赴浦东新区调研")
    assert is_target_leader_sentence("静安区委书记钟晓咏走访居民区")


@pytest.mark.parametrize("post,expected", [
    ("上海市委常委、浦东新区区委书记", "浦东新区区委书记"),
    ("静安区委常委、宣传部部长", "静安区委常委、宣传部部长"),
    ("市委常委、常务副市长", "市委常委、常务副市长"),
    ("上海市委常委、组织部部长", "上海市委常委、组织部部长"),
])
def test_concurrent_segment_only_drops_when_remainder_is_qualified(post, expected):
    assert _drop_concurrent_post_segment(post) == expected


@pytest.mark.parametrize("entities", [
    [Entity(text="张三", label="人物"), Entity(text="上海市委常委、浦东新区区委书记", label="职务")],
    [Entity(text="上海市委常委、浦东新区区委书记张三", label="人物")],
])
def test_normalization_uses_substantive_post_and_preserves_holds_edge(entities):
    kept, aliases, minted = normalize_entities(entities, {"人物", "领导干部", "职务"},
                                              "上海市委常委、浦东新区区委书记张三在会上强调。")
    assert [e.text for e in kept if e.label == "职务"] == ["浦东新区区委书记"]
    assert ("张三", "现任", "浦东新区区委书记") in [(r.subject, r.predicate, r.object) for r in minted]
    if len(entities) == 2:
        assert aliases["上海市委常委、浦东新区区委书记"] == "浦东新区区委书记"


def test_server_stores_substantive_post_and_remaps_model_relation(shared):
    client, service, model, headers = shared
    original_post = "上海市委常委、浦东新区区委书记"
    async def chat(messages, selected_model):
        body = json.loads(messages[1]["content"])
        if body.get("stage") == "ontology_entities":
            return json.dumps({"entities": [{"text": "张三", "label": "领导干部"},
                                            {"text": original_post, "label": "职务"}]}, ensure_ascii=False)
        if body.get("stage") == "ontology_relations":
            return json.dumps({"relations": [{"subject": "张三", "predicate": "现任", "object": original_post}]}, ensure_ascii=False)
        return await model(messages, selected_model)
    service.chat_provider = chat
    job = client.post("/v1/materials", headers=headers, json=material(text=original_post + "张三在会上强调。")).json()
    assert wait_job(client, headers, job["jobId"])["state"] == "succeeded"
    graph = client.get("/v1/channels/economy/graph", headers=headers).json()
    posts = [n for n in graph["nodes"] if n["label"] == "职务"]
    assert [n["name"] for n in posts] == ["浦东新区区委书记"]
    assert any(e["label"] == "现任" and e["target"] == posts[0]["id"] for e in graph["edges"])
