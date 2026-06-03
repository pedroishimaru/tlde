"""Firmware Verification Engineer agent.

Generates Renode .resc scripts and validates .repl platform descriptions
against vendor documentation. Uses Renode skills to test the .repl provided
by the firmware emulation engineer and provides structured feedback on
mismatches.
"""

from tlde.config import AgentConfig
from tlde.ingest.mcp_config import kb_mcp_servers


class FirmwareVerificationEngineer(AgentConfig):
    def __init__(self, **overrides):
        defaults = dict(
            name="firmware_verification_engineer",
            agent_type="fw_verif_eng",
            description=(
                "Generates Renode .resc execution scripts and validates "
                ".repl platform descriptions against the grounded datasheet "
                "model / SVD (via the tlde-kb MCP). Treats the .repl as untrusted "
                "input and cross-checks every peripheral's address/size/IRQ "
                "against cited source facts."
            ),
            # The verifier previously had NO grounding tool — give it tlde-kb so
            # it cross-checks against the source of truth, not its own priors.
            mcp_servers=kb_mcp_servers(),
            skills=[
                "renode-resc-generation",
                "renode-feedback-schema",
                "mcuboot-emulation",
                "renode-debugging",
                "zephyr-dts-analysis",
            ],
            # tools=None ⇒ all built-in tools (read/write/search) plus the MCP
            # tools are available, so the verifier can read artifacts, write its
            # report, and query grounded facts.
        )
        defaults.update(overrides)
        super().__init__(**defaults)
