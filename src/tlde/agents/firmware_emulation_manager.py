from tlde.config import AgentConfig
from tlde.ingest.mcp_config import kb_mcp_servers


class FirmwareEmulationManager(AgentConfig):
    def __init__(self, **overrides):
        defaults = dict(
            name="firmware_emulation_manager",
            agent_type="firmware_emulation_manager",
            model="claude-opus-4.6",
            description=(
                "Reads the grounded datasheet model (via the tlde-kb MCP) and "
                "decomposes the Renode emulation work into self-contained units "
                "for FirmwareEmulationEngineer agents."
            ),
            # Grounded retrieval over the cached, page-anchored DatasheetModel.
            mcp_servers=kb_mcp_servers(),
            skills=["renode-peripheral-catalogue"],
        )
        defaults.update(overrides)
        super().__init__(**defaults)
