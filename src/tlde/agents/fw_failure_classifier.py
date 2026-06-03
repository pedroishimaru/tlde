"""Firmware failure classifier agent.

Adjudicates ambiguous binary-in-the-loop runs: given a Renode log + console
output, decides whether the failure is a model defect (route back to the
engineer) or a firmware/harness issue, citing the log lines it relied on. Used
only when the deterministic heuristics in tlde.binloop.classify are inconclusive.
"""

from tlde.config import AgentConfig
from tlde.ingest.mcp_config import kb_mcp_servers


class FwFailureClassifier(AgentConfig):
    def __init__(self, **overrides):
        defaults = dict(
            name="fw_failure_classifier",
            agent_type="fw_failure_classifier",
            description=(
                "Classifies a headless Renode run (log + console output) into a "
                "model-defect taxonomy vs firmware/test-harness issues, citing "
                "the exact log lines, so only genuine model defects are routed "
                "back to the engineer."
            ),
            mcp_servers=kb_mcp_servers(),
            skills=["renode-debugging", "renode-feedback-schema"],
        )
        defaults.update(overrides)
        super().__init__(**defaults)
