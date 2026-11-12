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
"""

from .geometry import CHUNK_TILES, Rect, plan_rect
from .patches import Patch, Shore
from .survey import SurveyError, SurveyResult, survey_area, update
from .worldmap import Feature, RectReport, UnsurveyedError, Well, WorldMap

__all__ = [
    "CHUNK_TILES",
    "Feature",
    "Patch",
    "Rect",
    "RectReport",
    "Shore",
    "SurveyError",
    "SurveyResult",
    "UnsurveyedError",
    "Well",
    "WorldMap",
    "plan_rect",
    "survey_area",
    "update",
]
