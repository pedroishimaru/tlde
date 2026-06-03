"""Binary-in-the-loop: prebuilt firmware as empirical ground truth.

Loads already-built Zephyr artifacts from ``target_binaries/`` (no toolchain
required), runs them headless under Renode bounded by virtual time, classifies
failures into a model-defect taxonomy, and routes re-grounded fixes back to the
engineer — complementing the doc-grounded Verifier and the samples-build Tester.
"""

from tlde.binloop.classify import Classification, classify
from tlde.binloop.loop import run_binary_loop
from tlde.binloop.metadata import BinaryMeta, discover
from tlde.binloop.report import BinaryReport, failure_report, summarize

__all__ = [
    "BinaryMeta", "discover", "Classification", "classify",
    "run_binary_loop", "BinaryReport", "failure_report", "summarize",
]
