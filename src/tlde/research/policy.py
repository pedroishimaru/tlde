"""Trust policy for autonomous web research.

Decides which domains are allowed and what trust tier a fetched artifact gets,
from the project's ``[research]`` config. Target-agnostic: the allowlist and
tier map are data (config), not branchy per-vendor code. The actual vendor for a
target is curated by the user via the allowlist, not hard-coded here.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

# Map well-known ecosystem domains to a trust *category*; anything else that is
# explicitly allowlisted is treated as a "vendor" source (the user curated it).
_CATEGORY_DOMAINS = {
    "arm_cmsis": ("arm.com", "developer.arm.com", "github.com/arm-software", "github.com/cmsis"),
    "zephyr": ("github.com/zephyrproject-rtos", "zephyrproject.org"),
    "renode": ("github.com/renode", "renode.io", "antmicro.com"),
    "standards": ("ieee.org", "jedec.org", "usb.org"),
}


@dataclass
class ResearchPolicy:
    mode: str                       # autonomous | approval | off
    allowlist: list[str]
    trust_tiers: dict[str, int]
    max_sources: int

    @classmethod
    def from_settings(cls, cfg) -> "ResearchPolicy":
        r = cfg.research
        return cls(mode=r.mode, allowlist=list(r.allowlist),
                   trust_tiers=dict(r.trust_tiers), max_sources=r.max_sources)

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    @staticmethod
    def _host_path(url: str) -> str:
        p = urlparse(url if "://" in url else "https://" + url)
        return (p.netloc + p.path).lower().lstrip("/").replace("www.", "")

    def is_allowed(self, url: str) -> bool:
        hp = self._host_path(url)
        host = hp.split("/", 1)[0]
        for entry in self.allowlist:
            e = entry.lower().replace("www.", "")
            # entry may be a bare host ("nordicsemi.com") or host+path prefix
            if "/" in e:
                if hp.startswith(e):
                    return True
            elif host == e or host.endswith("." + e):
                return True
        return False

    def category_of(self, url: str) -> str:
        hp = self._host_path(url)
        for cat, domains in _CATEGORY_DOMAINS.items():
            for d in domains:
                d = d.lower()
                if (("/" in d and hp.startswith(d))
                        or ("/" not in d and hp.split("/", 1)[0].endswith(d))):
                    return cat
        return "vendor"  # allowlisted but unrecognised ⇒ treat as vendor-authoritative

    def trust_tier(self, url: str) -> int:
        cat = self.category_of(url)
        # default to the worst (community) tier if a category isn't configured
        return self.trust_tiers.get(cat, self.trust_tiers.get("community", 5))
