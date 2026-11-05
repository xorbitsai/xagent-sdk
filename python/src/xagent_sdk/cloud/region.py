"""Hosted regions for the workspace surface.

The hosted service runs as independent per-region deployments, each with
its own database. A workspace key is only valid against the region that
issued it, so the client must target that region's host. Pass the
``Region`` shown in the deploy snippet to ``WorkspaceClient`` instead of
hardcoding a URL; for a self-hosted or not-yet-listed region, pass an
explicit ``base_url`` instead.
"""

from enum import StrEnum


class Region(StrEnum):
    """A hosted region. Its value is the short region code (``"au"`` /
    ``"sg"``); ``base_url`` gives the region's API host -- reuse it for the
    ``AgentClient`` that runs an agent minted in this region.
    """

    AU = "au"
    SG = "sg"

    @property
    def base_url(self) -> str:
        return _REGION_BASE_URL[self]


# The ``-origin`` hosts serve the API directly. The plain region hosts
# (``au.cloud.xagent.co``) answer every request, API paths included, with a
# 301 to the browser entry point, and this client does not follow redirects.
_REGION_BASE_URL = {
    Region.AU: "https://au-origin.cloud.xagent.co",
    Region.SG: "https://sg-origin.cloud.xagent.co",
}
