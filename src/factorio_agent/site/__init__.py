"""World tile map for offline site selection and routing.

Modules:
    geometry  - Rect, tile/chunk conventions, entity footprints
    worldmap  - WorldMap (sparse tile grid) and its query API
    patches   - resource patches and shoreline search
    mapjson   - compact JSON cache format
    survey    - bulk read-only survey of the live game (one query per slice)
    fakeworld - deterministic synthetic maps for tests
    fakegame  - lupa-backed fake game that answers the survey Lua
    render    - standalone SVG rendering
    masterplan - one-per-map master plan: patches, bus, zones, corridors
                 (import it directly: it is also a ``python -m`` entry point)
    allocator - district blocks along the bus (used by MasterPlan.allocate)
"""

from .geometry import CHUNK_TILES, Rect, plan_rect
from .planmodel import SiteConfig, SiteError, Strip
from .planzones import Allocation, Corridor, Port, Segment, Zone
from .patches import Patch, Shore
from .survey import SurveyError, SurveyResult, survey_area, update
from .worldmap import Feature, RectReport, UnsurveyedError, Well, WorldMap

__all__ = [
    "Allocation",
    "CHUNK_TILES",
    "Corridor",
    "Feature",
    "Patch",
    "Port",
    "Rect",
    "RectReport",
    "Segment",
    "Shore",
    "SiteConfig",
    "SiteError",
    "Strip",
    "SurveyError",
    "SurveyResult",
    "UnsurveyedError",
    "Well",
    "WorldMap",
    "Zone",
    "plan_rect",
    "survey_area",
    "update",
]
