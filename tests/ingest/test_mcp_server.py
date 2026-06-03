"""tlde-kb MCP server tool tests (in-process, no subprocess).

Builds a generic model, persists it, points the server module at that cache,
and exercises each tool function directly. The full stdio protocol handshake is
covered manually; these keep the tool logic + citation shape regression-safe.
"""

from __future__ import annotations

import importlib
import textwrap

import pytest

from tlde import settings as S
from tlde.ingest import build_datasheet_model
from tlde.ingest.cache import write_current
from tlde.rag import KnowledgeBase

SVD = textwrap.dedent("""\
    <?xml version="1.0" encoding="utf-8"?>
    <device schemaVersion="1.3"><name>ACME_MCU</name><width>32</width><size>32</size>
      <peripherals>
        <peripheral><name>WIDGET0</name><baseAddress>0x40001000</baseAddress>
          <addressBlock><offset>0</offset><size>0x1000</size><usage>registers</usage></addressBlock>
          <interrupt><name>WIDGET0</name><value>5</value></interrupt>
          <registers><register><name>CTRL</name><addressOffset>0x0</addressOffset><size>32</size>
            <access>read-write</access><resetValue>0x0</resetValue></register></registers>
        </peripheral>
      </peripherals></device>
""")


@pytest.fixture
def srv(tmp_path, monkeypatch):
    svd = tmp_path / "g.svd"; svd.write_text(SVD)
    cache = tmp_path / "cache"
    s = S.Settings(); s.sources.svd = str(svd); s.ingest.cache_dir = str(cache)
    kb = KnowledgeBase(use_rerank=False)
    b = build_datasheet_model(s, kb=kb)
    write_current(str(cache), b.model, kb.export_corpus())

    monkeypatch.setenv("TLDE_KB_DIR", str(cache))
    module = importlib.import_module("tlde.ingest.mcp_server")
    module._CACHE_DIR = str(cache)
    module._MODEL = None  # force reload from this cache
    return module


def test_list_and_get_peripheral(srv):
    names = [p["name"] for p in srv.list_peripherals()["peripherals"]]
    assert "WIDGET0" in names
    p = srv.get_peripheral("WIDGET0")
    assert p["base_address"] == "0x40001000" and p["irqs"] == [5]
    assert p["citations"][0]["source_type"] == "svd"
    assert any(r["name"] == "CTRL" for r in p["registers"])


def test_get_register_and_unknown(srv):
    r = srv.get_register("WIDGET0", "CTRL")
    assert r["offset"] == "0x0" and r["access"] == "rw"
    assert "error" in srv.get_peripheral("NOPE")
    assert "error" in srv.get_register("WIDGET0", "NOPE")


def test_search_docs_exact_token(srv):
    res = srv.search_docs("0x40001000")["results"]
    assert res and res[0]["citation"].get("peripheral") == "WIDGET0"
