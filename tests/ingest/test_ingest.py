"""Target-agnostic ingestion tests.

All fixtures are synthetic / generic (made-up peripherals, neutral names) so the
suite proves the parsers and gates work for *any* MCU, never fitting a specific
vendor. The headline regression — a corrupt/zero-yield source must fail loud,
not silently produce an empty knowledge base — is covered by the PDF and gate
tests.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from tlde import settings as S
from tlde.ingest import build_datasheet_model
from tlde.ingest import dts as dts_loader
from tlde.ingest import headers as header_loader
from tlde.ingest import pdf as pdf_loader
from tlde.ingest import svd as svd_loader
from tlde.ingest.coverage import decide
from tlde.ingest.errors import IngestError
from tlde.ingest.merge import merge

SVD = textwrap.dedent("""\
    <?xml version="1.0" encoding="utf-8"?>
    <device schemaVersion="1.3">
      <name>ACME_MCU</name><width>32</width><size>32</size>
      <cpu><name>CM4</name><revision>r0p1</revision><endian>little</endian>
        <nvicPrioBits>3</nvicPrioBits><vendorSystickConfig>false</vendorSystickConfig></cpu>
      <peripherals>
        <peripheral>
          <name>WIDGET0</name><groupName>WIDGET</groupName>
          <baseAddress>0x40001000</baseAddress>
          <addressBlock><offset>0</offset><size>0x1000</size><usage>registers</usage></addressBlock>
          <interrupt><name>WIDGET0</name><value>5</value></interrupt>
          <registers>
            <register><name>CTRL</name><addressOffset>0x0</addressOffset><size>32</size>
              <access>read-write</access><resetValue>0x0</resetValue>
              <fields><field><name>ENABLE</name><bitOffset>0</bitOffset><bitWidth>1</bitWidth>
                <access>read-write</access></field></fields></register>
            <register><name>STATUS</name><addressOffset>0x4</addressOffset><size>32</size>
              <access>read-only</access><resetValue>0x1</resetValue></register>
          </registers>
        </peripheral>
        <peripheral derivedFrom="WIDGET0"><name>WIDGET1</name>
          <baseAddress>0x40002000</baseAddress>
          <interrupt><name>WIDGET1</name><value>6</value></interrupt></peripheral>
      </peripherals>
    </device>
""")

DTS = textwrap.dedent("""\
    /dts-v1/;
    / {
        #address-cells = <1>; #size-cells = <1>;
        soc {
            #address-cells = <1>; #size-cells = <1>;
            gpio0: gpio@50000000 { compatible = "acme,gpio"; reg = <0x50000000 0x1000>; phandle = <1>; };
            widget0: widget@40001000 { compatible = "acme,widget"; reg = <0x40001000 0x1000>; interrupts = <5 2>; status = "okay"; };
            widgetx: widget@40009000 { compatible = "acme,widget"; reg = <0x40009000 0x1000>; status = "disabled"; };
            sram0: memory@20000000 { device_type = "memory"; reg = <0x20000000 0x20000>; };
            flash0: flash@0 { compatible = "soc-nv-flash"; reg = <0x0 0x80000>; };
        };
        leds { compatible = "gpio-leds"; led0: led_0 { gpios = <&gpio0 21 1>; label = "LED_ROW1"; }; };
        keys { compatible = "gpio-keys"; btn0: button_0 { gpios = <&gpio0 14 0x11>; label = "BTN_A"; }; };
    };
""")

HEADER = textwrap.dedent("""\
    #define WIDGET0_BASE 0x40001000UL
    #define WIDGET1_BASE (0x40002000U)
    typedef enum IRQn { Reset_IRQn = -15, WIDGET0_IRQn = 5, WIDGET1_IRQn = 6 } IRQn_Type;
""")


@pytest.fixture
def svd_file(tmp_path: Path) -> str:
    p = tmp_path / "g.svd"; p.write_text(SVD); return str(p)


@pytest.fixture
def dts_file(tmp_path: Path) -> str:
    p = tmp_path / "g.dts"; p.write_text(DTS); return str(p)


@pytest.fixture
def header_file(tmp_path: Path) -> str:
    p = tmp_path / "g.h"; p.write_text(HEADER); return str(p)


def test_svd_registers_fields_irqs_and_derived(svd_file):
    m = svd_loader.load(svd_file)
    by = {p.name: p for p in m.peripherals}
    assert set(by) == {"WIDGET0", "WIDGET1"}
    w0 = by["WIDGET0"]
    assert w0.base_address == 0x40001000 and w0.size == 0x1000 and w0.irqs == [5]
    assert {r.name for r in w0.registers} == {"CTRL", "STATUS"}
    ctrl = next(r for r in w0.registers if r.name == "CTRL")
    assert ctrl.access == "rw" and ctrl.reset_value == 0
    assert ctrl.bit_fields[0].name == "ENABLE"
    # derivedFrom must inherit the register layout
    assert {r.name for r in by["WIDGET1"].registers} == {"CTRL", "STATUS"}
    assert all(r.grounded for p in m.peripherals for r in p.registers)


def test_dts_memory_peripherals_connectivity_and_status(dts_file):
    m = dts_loader.load(dts_file)
    names = {p.name for p in m.peripherals}
    assert "widget0" in names and "gpio0" in names
    assert "widgetx" not in names  # disabled node excluded
    kinds = {mr.name: mr.kind for mr in m.memory_map}
    assert kinds == {"sram0": "ram", "flash0": "flash"}
    nets = {c.net: c.soc_pin for c in m.connectivity}
    assert nets["LED_ROW1"] == "gpio0.21" and nets["BTN_A"] == "gpio0.14"


def test_headers_base_and_irqn(header_file):
    m = header_loader.load(header_file)
    by = {p.name: p for p in m.peripherals}
    assert by["WIDGET0"].base_address == 0x40001000 and by["WIDGET0"].irqs == [5]
    assert "Reset" not in by  # negative core-exception IRQns ignored


def test_pdf_corrupt_fails_loud():
    # The bundled placeholder PDF is 0-page/corrupt — must raise, not fail open.
    with pytest.raises(IngestError):
        pdf_loader.extract_pages("docs/nrf52833_rm.pdf")


def test_merge_precedence_svd_over_dts(svd_file, dts_file):
    # SVD (highest) keeps register maps; DTS confirms + adds gpio0 + memory + pins.
    svd_frag = svd_loader.load(svd_file)
    dts_frag = dts_loader.load(dts_file)
    merged = merge([svd_frag, dts_frag])  # highest precedence first
    by = {p.name.lower(): p for p in merged.peripherals}
    assert by["widget0"].registers  # registers came from SVD, survived merge
    assert any(p.name == "gpio0" for p in merged.peripherals)  # DTS-only peripheral added
    assert {m.kind for m in merged.memory_map} == {"ram", "flash"}
    assert len(merged.connectivity) == 2


def _settings(tmp_path, **ingest):
    s = S.Settings()
    s.ingest.cache_dir = str(tmp_path / "cache")
    for k, v in ingest.items():
        setattr(s.ingest, k, v)
    return s


def test_build_fail_closed_on_corrupt(tmp_path):
    s = _settings(tmp_path)
    s.sources.reference_manual = "docs/nrf52833_rm.pdf"
    with pytest.raises(IngestError):
        build_datasheet_model(s, kb=None)


def test_build_fail_closed_on_empty(tmp_path):
    s = _settings(tmp_path)  # no sources
    res = build_datasheet_model(s, kb=None)
    assert res.gate.ok is False and "no grounded material" in res.gate.reason


def test_build_success_and_cache(tmp_path, svd_file, dts_file):
    s = _settings(tmp_path)
    s.sources.svd = svd_file
    s.sources.dts = dts_file
    res = build_datasheet_model(s, kb=None)
    assert res.gate.ok is True
    assert res.model.coverage.overall_coverage == 1.0
    assert res.model.peripheral("WIDGET1") is not None
    # second build is a cache hit (same content hash)
    res2 = build_datasheet_model(s, kb=None)
    assert res2.model.content_hash == res.model.content_hash


def test_gate_warn_proceeds_when_empty():
    from tlde.ingest.datasheet_model import DatasheetModel
    g = decide(DatasheetModel(), retrieval_chunks=0, strictness="warn")
    assert g.ok is True and g.warnings
