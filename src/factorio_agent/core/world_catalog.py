"""Live, normal-quality Factorio data without the legacy curated recipe overlay.

The query callback executes a Lua *body* and returns its final table as a Python
dict. Exports inspect prototypes/force state only; they do not unlock research,
create entities, or change production statistics. Quantities use typed rows so
an item and a fluid with the same name never share a material balance.
"""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import hashlib
import json
import math
from typing import Any, Callable, Iterable, Mapping


class CatalogError(ValueError):
    """The live catalog cannot support the requested production plan."""


_TABLES = ("recipes", "technologies", "entities", "items", "fluids")
_SOURCES = {"recipes": "force.recipes", "technologies": "force.technologies",
            "entities": "prototypes.entity", "items": "prototypes.item", "fluids": "prototypes.fluid"}

# Optional properties can be absent or invalid for an entity subclass. Required
# recipe data is read directly so an incompatible API fails instead of exporting
# plausible but fabricated ingredient counts.
_LUA_HELPERS = r'''
local force = game.forces.player
local function optional(fn) local ok, value = pcall(fn); if ok then return value end end
local function keys(values)
  local out = {}; for name in pairs(values or {}) do out[#out+1] = name end
  table.sort(out); return out
end
local function rows(values)
  local out = {}; for _, value in pairs(values or {}) do out[#out+1] = value end
  return out
end
local function name_of(value) if value then return value.name end end
local function products(values)
  local out = {}
  for _, p in pairs(values or {}) do
    local row = {type=p.type or "item", name=p.name}
    for _, key in ipairs({"amount", "amount_min", "amount_max", "probability",
      "independent_probability", "shared_probability", "extra_count_fraction",
      "ignored_by_productivity", "ignored_by_stats", "temperature", "minimum_temperature",
      "maximum_temperature", "fluidbox_index", "fluidbox_multiplier", "quality_min", "quality_max"}) do
      row[key] = p[key]
    end
    out[#out+1] = row
  end
  return out
end
'''

_LUA_EXPORTERS = {
    "recipes": r'''
local function export(p)
  local proto = prototypes.recipe[p.name]
  local categories = optional(function() return rows(p.categories) end)
  if not categories then categories = {p.category} end
  return {name=p.name, enabled=p.enabled, hidden=p.hidden,
    categories=categories, category=categories[1], energy=p.energy,
    ingredients=products(p.ingredients), products=products(p.products),
    main_product=optional(function() return name_of(proto.main_product) end),
    surface_conditions=optional(function() return rows(proto.surface_conditions) end),
    maximum_productivity=optional(function() return proto.maximum_productivity end)}
end
''',
    "technologies": r'''
local function export(p)
  local proto = p.prototype
  local unlocked = {}
  for _, effect in pairs(proto.effects or {}) do
    if effect.type == "unlock-recipe" then unlocked[#unlocked+1] = effect.recipe end
  end
  table.sort(unlocked)
  local science = optional(function() return products(p.research_unit_ingredients) end) or {}
  return {name=p.name, enabled=p.enabled, researched=p.researched,
    prerequisites=keys(p.prerequisites), ingredients=science,
    unit_count=optional(function() return p.research_unit_count end),
    unit_energy=optional(function() return p.research_unit_energy end),
    count_formula=optional(function() return proto.research_unit_count_formula end),
    research_trigger=optional(function() return proto.research_trigger end),
    unlocks=unlocked}
end
''',
    "entities": r'''
local function export(p)
  local boxes = {}
  for _, b in pairs(p.fluidbox_prototypes or {}) do
    boxes[#boxes+1] = {index=b.index, production_type=b.production_type,
      volume=optional(function() return b.get_volume("normal") end),
      filter=optional(function() return name_of(b.filter) end),
      minimum_temperature=optional(function() return b.minimum_temperature end),
      maximum_temperature=optional(function() return b.maximum_temperature end),
      pipe_connections=rows(b.pipe_connections)}
  end
  local mining = p.mineable_properties
  return {name=p.name, type=p.type, collision_box=p.collision_box,
    selection_box=p.selection_box, crafting_categories=keys(optional(function() return p.crafting_categories end)),
    crafting_speed=optional(function() return p.get_crafting_speed("normal") end),
    fluidbox_prototypes=boxes, items_to_place_this=rows(p.items_to_place_this),
    mining_products=products(mining and mining.products),
    mining_time=mining and mining.mining_time, minable=mining and mining.minable,
    required_fluid=mining and mining.required_fluid, mining_fluid_amount=mining and mining.fluid_amount,
    resource_category=optional(function() return p.resource_category end),
    mining_speed=optional(function() return p.mining_speed end),
    rocket_parts_required=optional(function() return p.rocket_parts_required end),
    energy_usage=optional(function() return p.get_max_energy_usage("normal") end),
    energy_production=optional(function() return p.get_max_energy_production("normal") end),
    burner=optional(function() return p.burner_prototype ~= nil end),
    electric=optional(function() return p.electric_energy_source_prototype ~= nil end)}
end
''',
    "items": r'''
local function export(p)
  return {name=p.name, type=p.type, stack_size=p.stack_size, weight=p.weight,
    place_result=name_of(p.place_result), rocket_launch_products=products(p.rocket_launch_products),
    fuel_value=optional(function() return p.fuel_value end)}
end
''',
    "fluids": r'''
local function export(p)
  return {name=p.name, default_temperature=p.default_temperature,
    max_temperature=p.max_temperature, fuel_value=p.fuel_value}
end
''',
}


def _sequence(value: Any) -> list[Any]:
    """Factorio serializes empty Lua arrays as {}, which are still empty lists."""
    if value is None or value == {}:
        return []
    if not isinstance(value, (list, tuple)):
        raise CatalogError(f"expected an array, got {type(value).__name__}")
    return list(value)


def _material(row: Mapping[str, Any]) -> tuple[str, str]:
    kind, name = str(row.get("type", "item")), str(row["name"])
    if kind not in {"item", "fluid"} or not name:
        raise CatalogError(f"invalid material: {kind}:{name}")
    return kind, name


def _amount(value: Any) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise CatalogError(f"invalid material quantity: {value!r}")
    return number


def _typed_rows(values: Mapping[tuple[str, str], float]) -> list[dict[str, Any]]:
    return [{"type": k[0], "name": k[1], "amount": v} for k, v in sorted(values.items()) if v > 1e-9]


def _targets(values: Mapping[str, float] | Iterable[Mapping[str, Any]]) -> dict[tuple[str, str], float]:
    result: dict[tuple[str, str], float] = defaultdict(float)
    iterable = ({"type": "item", "name": name, "amount": count} for name, count in values.items()) if isinstance(values, Mapping) else values
    for row in iterable:
        result[_material(row)] += _amount(row.get("amount", 1))
    return dict(result)


def _yield(row: Mapping[str, Any]) -> float:
    probability = float(row.get("independent_probability", row.get("probability", 1)))
    shared = row.get("shared_probability")
    if shared:
        probability *= float(shared["max"]) - float(shared["min"])
    if probability != 1 or row.get("extra_count_fraction", 0):
        raise CatalogError(f"stochastic yield requires a probability-aware plan: {row['name']}")
    if "amount" in row:
        return _amount(row["amount"])
    if row.get("amount_min") == row.get("amount_max") and "amount_min" in row:
        return _amount(row["amount_min"])
    raise CatalogError(f"variable yield requires a probability-aware plan: {row['name']}")


class WorldCatalog:
    """A JSON-serializable snapshot of prototypes and the player force's state.

    ``fingerprint`` hashes structural data, excluding enabled/researched state.
    ``state_fingerprint`` additionally identifies that observed force state.
    Catalog snapshots must be refreshed when research changes recipe availability.
    """

    SCHEMA_VERSION = 1

    def __init__(self, payload: Mapping[str, Any]) -> None:
        self._data = deepcopy(dict(payload))
        if self._data.get("schema_version", self.SCHEMA_VERSION) != self.SCHEMA_VERSION:
            raise CatalogError("unsupported world catalog schema")
        self._data["schema_version"] = self.SCHEMA_VERSION
        self.version = str(self._data.get("version", "unknown"))
        self.active_mods = dict(self._data.get("active_mods") or {})
        for table in _TABLES:
            value = self._data.setdefault(table, {})
            if not isinstance(value, dict):
                raise CatalogError(f"{table} must be a name-keyed mapping")
            setattr(self, table, value)
            for name, row in value.items():
                if not isinstance(row, dict):
                    raise CatalogError(f"{table}.{name} must be an object")
                if row.setdefault("name", name) != name:
                    raise CatalogError(f"catalog name mismatch: {table}.{name}")
        for row in self.recipes.values():
            for field in ("ingredients", "products", "surface_conditions"):
                row[field] = _sequence(row.get(field))
            row["categories"] = _sequence(row.get("categories")) or ([row["category"]] if row.get("category") else [])
            row["category"] = row["categories"][0] if row["categories"] else ""
            for material in row["ingredients"] + row["products"]:
                _material(material)
        for row in self.technologies.values():
            for field in ("ingredients", "prerequisites", "unlocks"):
                row[field] = _sequence(row.get(field))
        for row in self.entities.values():
            for field in ("crafting_categories", "fluidbox_prototypes", "mining_products", "items_to_place_this"):
                row[field] = _sequence(row.get(field))
            for box in row["fluidbox_prototypes"]:
                box["pipe_connections"] = _sequence(box.get("pipe_connections"))
        self._products: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        self._unlocks: dict[str, list[str]] = defaultdict(list)
        for recipe in self.recipes.values():
            for product in recipe["products"]:
                self._products[_material(product)].append(recipe)
        for name, technology in self.technologies.items():
            for recipe in technology["unlocks"]:
                self._unlocks[recipe].append(name)
        self._raw = {_material(row) for row in _sequence(self._data.get("raw_sources"))}
        if not self._data.get("raw_sources_authoritative", False):
            for entity in self.entities.values():
                if entity.get("type") in {"resource", "tree"}:
                    self._raw.update(_material(p) for p in entity["mining_products"])

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "WorldCatalog":
        return cls(payload)

    @classmethod
    def from_game(cls, query: Callable[[str], dict[str, Any]], *, chunk_size: int = 48) -> "WorldCatalog":
        if not 1 <= chunk_size <= 256:
            raise CatalogError("chunk_size must be between 1 and 256")
        meta = query(_LUA_HELPERS + r'''
local surface = game.get_surface("nauvis") or game.surfaces[1]
local raw = {}; local seen = {}
local function add_source(kind, name)
  local key = kind .. ":" .. name
  if not seen[key] then raw[#raw+1] = {type=kind,name=name}; seen[key] = true end
end
local mapgen = surface.map_gen_settings
local placement = mapgen.autoplace_settings or {}
local controls = mapgen.autoplace_controls or {}
local function allowed(kind, prototype)
  local settings = placement[kind] and placement[kind].settings or {}
  local value = settings[prototype.name]
  if value then return value.size ~= 0 and value.frequency ~= 0 and value.richness ~= 0 end
  local ap = prototype.autoplace_specification
  local control = ap and ap.control and controls[ap.control]
  return control and control.size ~= 0 and control.frequency ~= 0 and control.richness ~= 0
end
for _, tile in pairs(prototypes.tile) do
  if tile.fluid and allowed("tile", tile) then add_source("fluid", tile.fluid.name) end
end
for _, entity in pairs(prototypes.entity) do
  if (entity.type == "resource" or entity.type == "tree" or entity.type == "simple-entity") and allowed("entity", entity) then
    local mining = entity.mineable_properties
    for _, product in pairs(mining and mining.products or {}) do add_source(product.type, product.name) end
  end
end
for _, entity in pairs(surface.find_entities_filtered{type={"resource","tree"}}) do
  local mining = entity.prototype.mineable_properties
  for _, product in pairs(mining and mining.products or {}) do add_source(product.type, product.name) end
end
table.sort(raw, function(a,b) return a.type .. ":" .. a.name < b.type .. ":" .. b.name end)
local properties = {}
for name in pairs(prototypes.surface_property) do properties[name] = surface.get_property(name) end
return {version=helpers.game_version, active_mods=script.active_mods,
  surface_name=surface.name, surface_properties=properties, raw_sources=raw, raw_sources_authoritative=true,
  names={recipes=keys(force.recipes), technologies=keys(force.technologies),
    entities=keys(prototypes.entity), items=keys(prototypes.item), fluids=keys(prototypes.fluid)}}
''')
        if not isinstance(meta, dict) or not isinstance(meta.get("names"), dict):
            raise CatalogError("game query returned no catalog metadata")
        payload = {key: value for key, value in meta.items() if key != "names"}
        for table in _TABLES:
            names = _sequence(meta["names"].get(table))
            output: dict[str, Any] = {}
            for start in range(0, len(names), chunk_size):
                batch = names[start:start + chunk_size]
                # JSON string literals for prototype identifiers have the same
                # escaping as Lua for these ASCII names; no shell interpolation.
                literal = "{" + ",".join(json.dumps(name, ensure_ascii=True) for name in batch) + "}"
                body = (_LUA_HELPERS + _LUA_EXPORTERS[table] + f"\nlocal out = {{}}\nfor _, name in ipairs({literal}) do\n"
                        f"  out[name] = export({_SOURCES[table]}[name])\nend\nreturn {{entries=out}}")
                response = query(body)
                entries = response.get("entries") if isinstance(response, dict) else None
                if not isinstance(entries, dict) or set(entries) != set(batch):
                    raise CatalogError(f"incomplete live export of {table}, chunk {start}")
                output.update(entries)
            payload[table] = output
        return cls(payload)

    def to_dict(self) -> dict[str, Any]:
        return deepcopy(self._data)

    @property
    def fingerprint(self) -> str:
        # Hashing only reads nested structures. Copy just the rows whose force
        # availability fields are omitted, keeping every call fresh without
        # deep-copying the complete prototype catalog.
        data = dict(self._data)
        for table, fields in (("recipes", ("enabled",)), ("technologies", ("enabled", "researched"))):
            data[table] = {name: {key: value for key, value in row.items() if key not in fields}
                           for name, row in data[table].items()}
        return self._hash(data)

    @property
    def state_fingerprint(self) -> str:
        return self._hash(self._data)

    @staticmethod
    def _hash(value: Any) -> str:
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()

    def technology_order(self, targets: str | Iterable[str], *, include_researched: bool = False) -> list[str]:
        """Prerequisites first, deterministic ties; unknown names/cycles fail."""
        result: list[str] = []
        visited: set[str] = set()
        visiting: set[str] = set()

        def visit(name: str) -> None:
            if name in visiting:
                raise CatalogError(f"technology prerequisite cycle: {name}")
            if name in visited:
                return
            if name not in self.technologies:
                raise CatalogError(f"unknown technology: {name}")
            row = self.technologies[name]
            if row.get("researched") and not include_researched:
                visited.add(name)
                return
            visiting.add(name)
            for parent in sorted(row["prerequisites"]):
                visit(parent)
            visiting.remove(name)
            visited.add(name)
            result.append(name)

        for target in sorted([targets] if isinstance(targets, str) else targets):
            visit(target)
        return result

    def _surface_allowed(self, recipe: Mapping[str, Any]) -> bool:
        properties = self._data.get("surface_properties", {})
        for condition in recipe.get("surface_conditions", []):
            value = properties.get(condition["property"])
            if value is None:
                return False
            if value < condition.get("min", -math.inf) or value > condition.get("max", math.inf):
                return False
        return True

    def _recipe_rank(self, recipe: Mapping[str, Any], product: str) -> tuple[Any, ...]:
        unlocks = self._unlocks.get(recipe["name"], [])
        depth = min((len(self.technology_order(t, include_researched=True)) for t in unlocks), default=0)
        return (recipe["name"] != product, not recipe.get("enabled", False), depth,
                recipe.get("main_product") != product, len(recipe["ingredients"]), recipe["name"])

    def _reverses_packaging(self, recipe: Mapping[str, Any], product: tuple[str, str]) -> bool:
        # Emptying a filled container is transportation, not an extraction path.
        # Detect the inverse transformation from recipes, without naming barrels
        # or maintaining special-case oil recipes. Ignore recycling alternatives.
        for ingredient in recipe["ingredients"]:
            key = _material(ingredient)
            if key in self._raw:
                continue
            producers = [r for r in self._products.get(key, [])
                         if not any("recycling" in c for c in r["categories"])]
            if producers and all(any(_material(i) == product for i in r["ingredients"]) for r in producers):
                return True
        return False

    def recipe_for_product(self, item: str, unlocked_only: bool = False, *, product_type: str = "item") -> dict[str, Any] | None:
        """Choose a standard local production recipe, never a recycling loop.

        Natural resource products are terminals even if Space Age provides an
        off-world synthesis recipe. Alternatives use live unlock depth, current
        availability and a stable name tie-break; no curated amounts or recipes.
        """
        key = (product_type, item)
        if key in self._raw:
            return None
        candidates = [r for r in self._products.get(key, []) if
                      (not unlocked_only or r.get("enabled", False)) and self._surface_allowed(r)
                      and not any("recycling" in category for category in r["categories"])
                      and not any(_material(i) == key for i in r["ingredients"])
                      and not self._reverses_packaging(r, key)]
        return min(candidates, key=lambda r: self._recipe_rank(r, item)) if candidates else None

    def _graph(self, targets: Mapping[tuple[str, str], float]) -> tuple[list[tuple[str, str]], dict[tuple[str, str], dict[str, Any]]]:
        order: list[tuple[str, str]] = []
        choices: dict[tuple[str, str], dict[str, Any]] = {}
        visited: set[tuple[str, str]] = set()
        visiting: set[tuple[str, str]] = set()

        def visit(key: tuple[str, str]) -> None:
            if key in visiting:
                raise CatalogError(f"recipe dependency cycle: {key[0]}:{key[1]}")
            if key in visited:
                return
            visiting.add(key)
            recipe = self.recipe_for_product(key[1], product_type=key[0])
            if recipe:
                choices[key] = recipe
                for ingredient in sorted(recipe["ingredients"], key=_material):
                    visit(_material(ingredient))
            elif key not in self._raw:
                raise CatalogError(f"no local recipe or extraction source: {key[0]}:{key[1]}")
            visiting.remove(key)
            visited.add(key)
            order.append(key)

        for key in sorted(targets):
            visit(key)
        return order, choices

    def _technology_for_recipes(self, recipes: Iterable[str]) -> list[str]:
        targets: set[str] = set()
        for recipe in recipes:
            if self.recipes[recipe].get("enabled", False):
                continue
            unlocks = self._unlocks.get(recipe, [])
            if not unlocks:
                raise CatalogError(f"disabled recipe has no technology unlock: {recipe}")
            targets.add(min(unlocks, key=lambda t: (len(self.technology_order(t)), t)))
        return self.technology_order(targets)

    def dependency_closure(self, products: Mapping[str, float] | Iterable[Mapping[str, Any]]) -> dict[str, Any]:
        requested = _targets(products)
        order, choices = self._graph(requested)
        recipes = list(dict.fromkeys(choices[key]["name"] for key in order if key in choices))
        return {"products": [{"type": key[0], "name": key[1]} for key in order],
                "recipes": recipes, "technologies": self._technology_for_recipes(recipes),
                "raw_materials": [{"type": key[0], "name": key[1]} for key in order if key not in choices]}

    def bill_of_materials(self, products: Mapping[str, float] | Iterable[Mapping[str, Any]]) -> dict[str, Any]:
        """Integer recipe batches with shared ingredients and coproduct credit.

        This is a constructive material budget, not an optimal refinery solver.
        It excludes power/fuel, factory construction and productivity bonuses.
        Stochastic outputs and cyclic recipes fail explicitly.
        """
        requested = _targets(products)
        order, choices = self._graph(requested)
        needed: dict[tuple[str, str], float] = defaultdict(float, requested)
        batches: dict[str, int] = defaultdict(int)
        for key in reversed(order):
            if needed[key] <= 1e-9 or key not in choices:
                continue
            recipe = choices[key]
            output = sum(_yield(p) for p in recipe["products"] if _material(p) == key)
            if output <= 0:
                raise CatalogError(f"zero recipe yield: {recipe['name']}")
            count = math.ceil(needed[key] / output - 1e-12)
            batches[recipe["name"]] += count
            for product in recipe["products"]:
                needed[_material(product)] -= _yield(product) * count
            for ingredient in recipe["ingredients"]:
                needed[_material(ingredient)] += _amount(ingredient["amount"]) * count
        return {"targets": _typed_rows(requested), "recipe_batches": dict(sorted(batches.items())),
                "raw_materials": _typed_rows({k: v for k, v in needed.items() if k in self._raw}),
                "surplus": _typed_rows({k: -v for k, v in needed.items() if v < -1e-9}),
                "technologies": self._technology_for_recipes(batches),
                "assumptions": ["normal quality", "integer batches", "no productivity bonus",
                                "factory construction and operating fuel excluded", "not a minimum-cost refinery solution"]}

    def first_rocket_bom(self, *, include_research: bool = True) -> dict[str, Any]:
        """One manufactured silo, its first rocket, and one platform starter pack.

        All quantities come from this catalog, including rocket part count and
        science costs. Trigger requirements are returned as milestones rather
        than fictional science packs or force-unlocked technologies.
        """
        silo = self.entities.get("rocket-silo")
        if not silo or not silo.get("rocket_parts_required"):
            raise CatalogError("live rocket silo part requirement is unavailable")
        targets: dict[str, float] = {"rocket-silo": 1, "rocket-part": _amount(silo["rocket_parts_required"]),
                                   "space-platform-starter-pack": 1}
        technology_names: set[str] = set()
        science: dict[str, float] = defaultdict(float)
        if include_research:
            while True:
                closure = self.dependency_closure(targets)
                added = set(closure["technologies"]) - technology_names
                if not added:
                    break
                technology_names.update(added)
                for name in added:
                    technology = self.technologies[name]
                    if technology.get("research_trigger"):
                        continue
                    if technology.get("unit_count") is None:
                        raise CatalogError(f"research unit count unavailable: {name}")
                    for ingredient in technology["ingredients"]:
                        count = _amount(ingredient["amount"]) * _amount(technology["unit_count"])
                        science[ingredient["name"]] += count
                        targets[ingredient["name"]] = targets.get(ingredient["name"], 0) + count
        result = self.bill_of_materials(targets)
        result["science_packs"] = dict(sorted(science.items()))
        result["technology_order"] = self.technology_order(technology_names or result["technologies"])
        result["trigger_requirements"] = [{"technology": name, "trigger": deepcopy(self.technologies[name]["research_trigger"])}
                                          for name in result["technology_order"] if self.technologies[name].get("research_trigger")]
        result["catalog_fingerprint"] = self.fingerprint
        return result
