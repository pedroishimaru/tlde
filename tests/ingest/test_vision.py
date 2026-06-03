"""Vision extraction tests — deterministic parts (no live model call).

The model call is stubbed so parsing, validation, and merge are covered without
network/keys. Page selection runs against the real bundled schematic PDF.
"""

from __future__ import annotations

import asyncio
import textwrap

import pytest

from tlde import settings as S
from tlde.ingest import dts as dts_loader
from tlde.ingest import svd as svd_loader
from tlde.ingest import vision
from tlde.ingest.merge import merge

SVD = textwrap.dedent("""\
    <?xml version="1.0"?>
    <device schemaVersion="1.3"><name>ACME_MCU</name><width>32</width><size>32</size>
      <peripherals><peripheral><name>WIDGET0</name><baseAddress>0x40001000</baseAddress>
        <addressBlock><offset>0</offset><size>0x1000</size><usage>registers</usage></addressBlock>
        <registers><register><name>CTRL</name><addressOffset>0x0</addressOffset><size>32</size>
          <access>read-write</access></register></registers>
      </peripheral></peripherals></device>
""")

DTS = textwrap.dedent("""\
    /dts-v1/;
    / { #address-cells=<1>; #size-cells=<1>;
        soc { #address-cells=<1>; #size-cells=<1>;
            gpio0: gpio@50000000 { compatible="acme,gpio"; reg=<0x50000000 0x1000>; phandle=<1>; }; };
        leds { compatible="gpio-leds"; led0: led_0 { gpios=<&gpio0 21 1>; label="LED_ROW1"; }; };
    };
""")

VISION_JSON = """{
  "pins": [
    {"net": "BTN_A", "soc_pin": "P0.14", "peripheral": "gpio0", "function": "gpio-in", "active_low": true},
    {"net": "LED_ROW1", "soc_pin": "P9.99", "function": "gpio-out", "active_low": false}
  ],
  "bitfields": [
    {"peripheral": "WIDGET0", "register": "CTRL", "field": "ENABLE", "bit_offset": 0, "bit_width": 1, "access": "rw"}
  ]
}"""


def test_select_figure_pages_on_real_schematic():
    pages = vision.select_figure_pages("docs/microbit_v2_schematic.pdf")
    assert pages and pages[0] == 1  # schematic pages are figure/drawing-heavy


def test_parse_vision_json():
    frag = vision.parse_vision_json(VISION_JSON, source="schematic.pdf", page=1)
    nets = {p.net: p.soc_pin for p in frag.connectivity}
    assert nets == {"BTN_A": "P0.14", "LED_ROW1": "P9.99"}
    assert frag.connectivity[0].citations[0].source_type == "vision"
    bfs = frag.__dict__["_vision_bitfields"]
    assert bfs[0]["peripheral"] == "WIDGET0" and bfs[0]["field"].name == "ENABLE"


def test_validate_flags_conflict_with_grounded(tmp_path):
    svd = tmp_path / "g.svd"; svd.write_text(SVD)
    dts = tmp_path / "g.dts"; dts.write_text(DTS)
    structured = merge([svd_loader.load(str(svd)), dts_loader.load(str(dts))])
    frag = vision.parse_vision_json(VISION_JSON, source="schematic.pdf", page=1)
    warnings = vision.validate(frag, structured)
    # DTS grounds LED_ROW1 -> gpio0.21; vision says P9.99 -> must warn (grounded wins)
    assert any("LED_ROW1" in w for w in warnings)


def test_merge_vision_adds_pins_and_fills_bitfields(tmp_path):
    svd = tmp_path / "g.svd"; svd.write_text(SVD)
    dts = tmp_path / "g.dts"; dts.write_text(DTS)
    model = merge([svd_loader.load(str(svd)), dts_loader.load(str(dts))])
    frag = vision.parse_vision_json(VISION_JSON, source="schematic.pdf", page=1)
    added = vision.merge_vision(model, frag)
    nets = {c.net: c.soc_pin for c in model.connectivity}
    assert nets["BTN_A"] == "P0.14"            # new pin added
    assert nets["LED_ROW1"] == "gpio0.21"       # grounded DTS value preserved (not overridden)
    ctrl = model.register("WIDGET0", "CTRL")
    assert any(f.name == "ENABLE" for f in ctrl.bit_fields)  # bit-field gap-filled
    assert added >= 2


def test_augment_with_stubbed_model(tmp_path, monkeypatch):
    svd = tmp_path / "g.svd"; svd.write_text(SVD)
    structured = svd_loader.load(str(svd))
    s = S.Settings()
    s.ingest.vision = "on"

    async def fake_call(model, provider, images, **kw):
        return VISION_JSON

    # avoid touching a real PDF: stub page selection + rendering
    monkeypatch.setattr(vision, "select_figure_pages", lambda *a, **k: [1])
    monkeypatch.setattr(vision, "render_page_png", lambda *a, **k: b"\x89PNG")

    frag, warnings = asyncio.run(
        vision.augment(s, structured, ["schematic.pdf"], call=fake_call)
    )
    assert {p.net for p in frag.connectivity} == {"BTN_A", "LED_ROW1"}
    assert frag.__dict__["_vision_bitfields"]


def test_augment_off_is_noop():
    from tlde.ingest.datasheet_model import DatasheetModel
    s = S.Settings(); s.ingest.vision = "off"
    frag, warnings = asyncio.run(vision.augment(s, DatasheetModel(), ["x.pdf"]))
    assert not frag.connectivity and not warnings
