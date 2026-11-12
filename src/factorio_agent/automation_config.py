"""Validated editable automation controls; world settings affect future creation only."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "automation_config.json"


def _number(value, low, high, name):
    if type(value) not in (int, float) or not low <= value <= high or not math.isfinite(value):
        raise ValueError(name + " must be finite within " + str(low) + ".." + str(high))


def _keys(value, allowed, name):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ValueError(name + " must be an object with only " + ", ".join(allowed))


def _pairs(rows):
    out = {}
    for key, value in rows:
        if key in out:
            raise ValueError("duplicate automation config key: " + key)
        out[key] = value
    return out


def _constant(value):
    raise ValueError("non-finite automation config value: " + value)


@dataclass(frozen=True)
class WorldConfig:
    starting_area_percent: int | float = 200
    nauvis_cliffs: bool = False
    seed: int | None = None

    def validate(self):
        _number(self.starting_area_percent, 0, 600, "world.starting_area_percent")
        if type(self.nauvis_cliffs) is not bool:
            raise ValueError("world.nauvis_cliffs must be boolean")
        if self.seed is not None and (type(self.seed) is not int or not 0 <= self.seed <= 2**32 - 1):
            raise ValueError("world.seed must be null or an unsigned32-bit integer")


PLANNERS = ("luna", "none")


@dataclass(frozen=True)
class ControllerConfig:
    """Supervisor startup arguments.

    ``planner`` (default "luna"): "luna" consults the pinned model only on
    incidents; "none" runs on framework rules alone and never starts the model.
    ``decision_interval_seconds`` (60): minimum seconds between model calls.
    ``execution_slice_seconds`` (25): length of one ``run --all`` slice.
    """

    decision_interval_seconds: int | float = 60
    execution_slice_seconds: int | float = 25
    planner: str = "luna"

    def validate(self):
        if self.planner not in PLANNERS:
            raise ValueError("controller.planner must be one of " + ", ".join(PLANNERS))
        _number(self.decision_interval_seconds, 10, 3600, "controller.decision_interval_seconds")
        _number(self.execution_slice_seconds, 1, 50, "controller.execution_slice_seconds")


@dataclass(frozen=True)
class AutomationConfig:
    world: WorldConfig
    controller: ControllerConfig
    source_path: Path
    source_sha256: str | None = None

    def validate(self):
        if not isinstance(self.world, WorldConfig) or not isinstance(self.controller, ControllerConfig):
            raise ValueError("typed world/controller automation config required")
        self.world.validate()
        self.controller.validate()
        if not isinstance(self.source_path, Path) or not self.source_path.is_absolute():
            raise ValueError("automation config source must be absolute")
        return self

    def to_dict(self):
        return dict(world=asdict(self.world), controller=asdict(self.controller))

    @property
    def fingerprint(self):
        return hashlib.sha256(
            json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()

    def map_gen_settings(self, seed):
        self.validate()
        if type(seed) is not int or not 0 <= seed <= 2**32 - 1:
            raise ValueError("effective map seed must be uint32")
        result = dict(seed=seed, starting_area=self.world.starting_area_percent / 100)
        if not self.world.nauvis_cliffs:
            result["cliff_settings"] = dict(cliff_elevation_interval=0, richness=0)
        return result

    def generation_snapshot(self, seed):
        return dict(
            schema_version=1,
            config_source=str(self.source_path),
            source_sha256=self.source_sha256,
            requested_config=self.to_dict(),
            config_fingerprint=self.fingerprint,
            effective_world=dict(
                seed=seed,
                starting_area_percent=self.world.starting_area_percent,
                starting_area=self.world.starting_area_percent / 100,
                nauvis_cliffs=self.world.nauvis_cliffs,
            ),
            map_gen_settings=self.map_gen_settings(seed),
            scope="pinned future-world generation; explicit runtime.create seed is authoritative",
        )

    def controller_snapshot(self):
        self.validate()
        return dict(
            schema_version=1,
            config_source=str(self.source_path),
            source_sha256=self.source_sha256,
            config_fingerprint=self.fingerprint,
            controller=asdict(self.controller),
            scope="effective supervisor startup arguments; existing world generation unchanged",
        )


def load_automation_config(path=None):
    path = Path(path if path is not None else DEFAULT_PATH).resolve()
    raw_bytes = path.read_bytes() if path.exists() else None
    value = (
        json.loads(raw_bytes.decode("utf-8-sig"), object_pairs_hook=_pairs, parse_constant=_constant)
        if raw_bytes is not None
        else {}
    )
    _keys(value, ("world", "controller"), "automation config")
    world = value.get("world", {})
    controller = value.get("controller", {})
    _keys(world, ("starting_area_percent", "nauvis_cliffs", "seed"), "world")
    _keys(controller, ("decision_interval_seconds", "execution_slice_seconds", "planner"), "controller")
    return AutomationConfig(
        WorldConfig(**world),
        ControllerConfig(**controller),
        path,
        hashlib.sha256(raw_bytes).hexdigest() if raw_bytes is not None else None,
    ).validate()
