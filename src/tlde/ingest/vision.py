"""Vision extraction — figures -> structured connectivity / bit-fields.

Renders figure-bearing PDF pages (pinouts, schematics, bit-field/timing
diagrams) to images and asks a vision-capable model to return structured JSON,
which is merged into the DatasheetModel as tier-3 (vision) facts with page +
bbox citations. Vision-derived facts are validated against DTS/SVD where they
overlap, and they NEVER override higher-trust structured sources.

The model call runs through the same Copilot SDK runtime as every other role,
using image BlobAttachments — so it is provider-agnostic (Copilot/Anthropic/
OpenAI/Azure/OpenRouter/Ollama). With ``ingest.vision = "auto"`` the step
degrades gracefully (warns + skips) when no vision-capable model is available.

Target-agnostic: the model is asked to read whatever the figures show; nothing
about any vendor/board is hard-coded.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path

from tlde.ingest.datasheet_model import (
    BitField,
    Citation,
    DatasheetModel,
    PinConnection,
)

VISION_TRUST_TIER = 3
DEFAULT_DPI = 144
DEFAULT_MAX_PAGES = 12  # bound cost; figures beyond this are skipped

EXTRACTION_PROMPT = """\
You are extracting hardware facts from datasheet/schematic figures for an MCU
emulation tool. Look ONLY at the provided images. Return STRICT JSON (no prose,
no markdown) with this shape:

{
  "pins": [
    {"net": "<board net/label, e.g. LED_ROW1>", "soc_pin": "<e.g. P0.21 or PA5>",
     "peripheral": "<owning peripheral or null>", "function": "<e.g. gpio-out, i2c-sda>",
     "active_low": <true|false|null>}
  ],
  "bitfields": [
    {"peripheral": "<NAME>", "register": "<NAME>", "field": "<NAME>",
     "bit_offset": <int>, "bit_width": <int>, "access": "<rw|ro|wo|null>"}
  ]
}

Rules:
- Only report what is legibly visible in the images. Do NOT guess or use prior
  knowledge. If a figure is unreadable, omit it.
- Prefer exact tokens as printed (pin names, net names, register/field names).
- If you see no pins or no bit-fields, return empty arrays.
First character must be '{', last must be '}'.
"""


# ---------------------------------------------------------------------------
# Page selection + rendering (deterministic, testable)
# ---------------------------------------------------------------------------

def select_figure_pages(path: str, max_pages: int = DEFAULT_MAX_PAGES) -> list[int]:
    """1-based page numbers likely to contain pinouts/schematics/bit-field figures.

    Heuristic: pages with embedded images or substantial vector drawings.
    """
    import fitz

    pages: list[int] = []
    with fitz.open(path) as doc:
        for i in range(doc.page_count):
            pg = doc[i]
            has_images = bool(pg.get_images())
            try:
                drawings = len(pg.get_drawings())
            except Exception:
                drawings = 0
            if has_images or drawings >= 40:  # schematic pages are drawing-heavy
                pages.append(i + 1)
            if len(pages) >= max_pages:
                break
    return pages


def render_page_png(path: str, page_no: int, dpi: int = DEFAULT_DPI) -> bytes:
    """Render a 1-based page to PNG bytes."""
    import fitz

    with fitz.open(path) as doc:
        return doc[page_no - 1].get_pixmap(dpi=dpi).tobytes("png")


# ---------------------------------------------------------------------------
# Response parsing (deterministic, testable)
# ---------------------------------------------------------------------------

def _extract_json(text: str) -> dict | None:
    text = (text or "").strip()
    if text.startswith("{"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    m = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    start = text.find("{")
    if start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break
    return None


def parse_vision_json(text: str, source: str, page: int | None = None) -> DatasheetModel:
    """Parse a vision model's JSON into a DatasheetModel fragment (pins + bitfields)."""
    data = _extract_json(text) or {}
    cite = Citation(source=source, source_type="vision", trust_tier=VISION_TRUST_TIER,
                    page=page, note="vision extraction")

    pins: list[PinConnection] = []
    for p in data.get("pins", []) or []:
        net = (p.get("net") or "").strip()
        soc_pin = (p.get("soc_pin") or "").strip()
        if not net or not soc_pin:
            continue
        pins.append(PinConnection(
            net=net, soc_pin=soc_pin,
            peripheral=(p.get("peripheral") or None),
            function=(p.get("function") or None),
            active_low=p.get("active_low") if isinstance(p.get("active_low"), bool) else None,
            citations=[cite],
        ))

    # Bit-fields are returned grouped under (peripheral, register); the caller
    # merges them only into registers that currently lack fields.
    fields: list[dict] = []
    for b in data.get("bitfields", []) or []:
        try:
            fields.append({
                "peripheral": (b["peripheral"]).strip(),
                "register": (b["register"]).strip(),
                "field": BitField(
                    name=(b["field"]).strip(),
                    bit_offset=int(b["bit_offset"]),
                    bit_width=int(b["bit_width"]),
                    access=(b.get("access") or None),
                    citations=[cite],
                ),
            })
        except (KeyError, TypeError, ValueError):
            continue

    frag = DatasheetModel(connectivity=pins)
    frag.__dict__["_vision_bitfields"] = fields  # carried out-of-band for merge
    return frag


# ---------------------------------------------------------------------------
# Validation + merge (deterministic, testable)
# ---------------------------------------------------------------------------

def validate(fragment: DatasheetModel, structured: DatasheetModel) -> list[str]:
    """Cross-check vision pins against DTS/SVD; return warnings for conflicts."""
    warnings: list[str] = []
    known_pins = {c.net.lower(): c.soc_pin for c in structured.connectivity}
    for pin in fragment.connectivity:
        kp = known_pins.get(pin.net.lower())
        if kp and kp.lower() != pin.soc_pin.lower():
            warnings.append(
                f"vision pin {pin.net}->{pin.soc_pin} conflicts with grounded "
                f"{pin.net}->{kp}; keeping the grounded (higher-tier) value."
            )
    return warnings


def merge_vision(model: DatasheetModel, fragment: DatasheetModel) -> int:
    """Merge vision facts in-place: add new pins; fill bit-fields on bare registers.

    Vision never overrides higher-trust facts: existing nets win, and bit-fields
    are only attached to registers that currently have none.
    """
    added = 0
    have = {c.net.lower() for c in model.connectivity}
    for pin in fragment.connectivity:
        if pin.net.lower() not in have:
            model.connectivity.append(pin)
            have.add(pin.net.lower())
            added += 1

    for entry in fragment.__dict__.get("_vision_bitfields", []):
        reg = model.register(entry["peripheral"], entry["register"])
        if reg is not None and not reg.bit_fields:
            reg.bit_fields.append(entry["field"])
            added += 1
    model.connectivity.sort(key=lambda c: c.net)
    model.recompute_coverage()
    return added


# ---------------------------------------------------------------------------
# Live model call + orchestration
# ---------------------------------------------------------------------------

async def _default_call(model: str, provider: dict | None, images: list[bytes],
                        prompt: str = EXTRACTION_PROMPT, timeout: float = 180.0) -> str:
    """Call a vision-capable model via the Copilot SDK with image attachments."""
    from copilot import CopilotClient
    from copilot.client import ModelCapabilitiesOverride, ModelSupportsOverride
    from copilot.generated.session_events import AssistantMessageData

    from tlde.agent import _approve_all_handler

    attachments = [
        {"type": "blob", "data": base64.b64encode(png).decode(), "mimeType": "image/png",
         "displayName": f"figure-{i + 1}.png"}
        for i, png in enumerate(images)
    ]
    async with CopilotClient() as client:
        async with await client.create_session(
            on_permission_request=_approve_all_handler,
            model=model,
            provider=provider,
            # Assert vision so BYOK models the runtime can't introspect are allowed
            # to receive images; truly non-vision models will error -> graceful skip.
            model_capabilities=ModelCapabilitiesOverride(
                supports=ModelSupportsOverride(vision=True)
            ),
        ) as session:
            ev = await session.send_and_wait(prompt, attachments=attachments, timeout=timeout)
            if ev and isinstance(ev.data, AssistantMessageData):
                return ev.data.content
    return ""


async def augment(cfg, structured: DatasheetModel, pdf_sources: list[str],
                  call=None) -> tuple[DatasheetModel, list[str]]:
    """Render figure pages, extract structured facts, validate, and return a fragment.

    ``cfg`` is a :class:`tlde.settings.Settings`. Returns (vision_fragment,
    warnings); the caller merges with :func:`merge_vision`. ``call`` is
    injectable for testing.
    """
    from tlde import settings as _settings_mod

    mode = cfg.ingest.vision
    warnings: list[str] = []
    fragment = DatasheetModel()
    if mode == "off" or not pdf_sources:
        return fragment, warnings

    role = _settings_mod.for_role("vision", cfg)
    model = role.get("model")
    if not model:
        return fragment, warnings
    provider = None
    # Reuse AgentConfig's provider resolution for the vision provider name.
    if role.get("provider") and role["provider"] != "github":
        from tlde.providers import get_provider, provider_to_dict
        provider = provider_to_dict(get_provider(role["provider"]))

    caller = call or _default_call
    all_bitfields: list[dict] = []
    for src in pdf_sources:
        try:
            pages = select_figure_pages(src)
        except Exception as e:
            warnings.append(f"vision: could not open {src}: {e}")
            continue
        from tlde.progress import track
        for pno in track(pages, total=len(pages),
                         desc=f"[vision] {Path(src).name}", unit="pg"):
            try:
                png = render_page_png(src, pno)
                text = await caller(model, provider, [png])
                pfrag = parse_vision_json(text, source=src, page=pno)
            except Exception as e:
                msg = f"vision: extraction failed on {src} p.{pno}: {e}"
                if mode == "on":
                    warnings.append(msg)
                else:  # auto — degrade quietly after the first notice
                    warnings.append(msg)
                    return fragment, warnings  # likely no vision model; stop early
            else:
                fragment.connectivity.extend(pfrag.connectivity)
                all_bitfields.extend(pfrag.__dict__.get("_vision_bitfields", []))

    fragment.__dict__["_vision_bitfields"] = all_bitfields
    warnings.extend(validate(fragment, structured))
    return fragment, warnings
