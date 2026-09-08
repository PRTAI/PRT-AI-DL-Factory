"""平台文档：目录、正文、插图与相关项目链接。"""

from __future__ import annotations

import re

import pytest

from backend.app.config import settings


def test_doc_index_is_grouped_and_ordered(client, member_headers):
    docs = client.get("/api/handbook", headers=member_headers).json()
    assert docs, "文档目录不该为空"
    # order 决定侧栏顺序，必须单调
    assert [d["order"] for d in docs] == sorted(d["order"] for d in docs)
    for doc in docs:
        assert doc["title"], doc
        assert doc["category"], doc
    # 入门类必须排在最前，否则新人打开文档第一眼看到的是运维内容
    assert docs[0]["category"] == "入门"


def test_doc_body_and_internal_links_resolve(client, member_headers):
    docs = client.get("/api/handbook", headers=member_headers).json()
    slugs = {doc["slug"] for doc in docs}

    for doc in docs:
        body = client.get(f"/api/handbook/{doc['slug']}", headers=member_headers).json()["body"]
        assert body.strip(), doc["slug"]
        # 站内互链写成 /docs/<slug>，指向不存在的文档就是死链
        for target in re.findall(r"\]\(/docs/([a-z0-9\-_]+)\)", body):
            assert target in slugs, f"{doc['slug']} 里的 /docs/{target} 指向不存在的文档"
        # 插图必须真的存在
        for asset in re.findall(r"\]\(/api/handbook/assets/([^)]+)\)", body):
            response = client.get(f"/api/handbook/assets/{asset}", headers=member_headers)
            assert response.status_code == 200, f"{doc['slug']} 引用了不存在的插图 {asset}"


def test_asset_path_traversal_is_refused(client, member_headers):
    response = client.get("/api/handbook/assets/../../config.py", headers=member_headers)
    assert response.status_code in {400, 404}


def test_upstream_links_point_at_the_real_repos(client, member_headers):
    """URL 是从各检出自己的元信息里取的，版本跟着本机装的那份走。"""
    links = client.get("/api/handbook/links", headers=member_headers).json()
    by_key = {row["key"]: row for row in links}

    # 平台真正调用的四个框架都要有入口
    assert {"llamafactory", "verl", "trl", "harbor"} <= set(by_key)

    expected = {
        "llamafactory": "https://github.com/hiyouga/LLaMA-Factory",
        "verl": "https://github.com/verl-project/verl",
        "trl": "https://github.com/huggingface/trl",
        "harbor": "https://github.com/harbor-framework/harbor",
    }
    for key, url in expected.items():
        assert by_key[key]["url"] == url, key
        assert by_key[key]["note"], f"{key} 缺少一句说明"

    for row in links:
        assert row["url"].startswith("https://github.com/"), row


def test_link_versions_follow_the_local_checkouts(client, member_headers):
    if not (settings.llamafactory_root / "src" / "llamafactory").is_dir():
        pytest.skip("LLaMA-Factory 源码不可用")
    links = {row["key"]: row for row in client.get("/api/handbook/links", headers=member_headers).json()}
    # 版本不能是写死的字符串，得能从本机检出解析出来
    assert re.match(r"^\d+\.\d+", links["llamafactory"]["version"]), links["llamafactory"]
