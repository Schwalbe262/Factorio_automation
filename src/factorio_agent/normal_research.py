"""Model-selected technology, researched only by ordinary belt-fed science."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
import re
from factorio_agent.core.deterministic_state import _atomic_json


def validate_parameters(parameters):
    if not isinstance(parameters, dict) or set(parameters) != {"technology"}:
        raise ValueError("research requires technology")
    if not isinstance(parameters["technology"], str) or not re.fullmatch(
        r"[a-z0-9][a-z0-9-]{0,79}", parameters["technology"]
    ):
        raise ValueError("invalid technology name")


def ready_options(catalog, obs):
    rows = []
    triggers = []
    for name, tech in catalog.technologies.items():
        if obs.get("technologies", {}).get(name) or tech.get("enabled") is False:
            continue
        if any(not obs.get("technologies", {}).get(p) for p in tech["prerequisites"]):
            continue
        if tech.get("research_trigger"):
            triggers.append(
                {
                    "technology": name,
                    "trigger": deepcopy(tech["research_trigger"]),
                    "unlocks": list(tech.get("unlocks", [])),
                    "completion": "perform the native gameplay trigger; do not submit ordinary science research",
                }
            )
            continue
        rows.append(
            {
                "technology": name,
                "unit_count": tech.get("unit_count"),
                "unit_energy_ticks": tech.get("unit_energy"),
                "science_ingredients": tech["ingredients"],
            }
        )
    return {
        "technologies": sorted(rows, key=lambda r: r["technology"]),
        "natural_triggers": sorted(triggers, key=lambda r: r["technology"]),
        "scope": "native prerequisite-ready research costs; pack availability and continuous supply are checked by research job",
    }


class NormalResearch:
    def __init__(self, game, catalog, science, green=None, *, blue=None):
        self.game, self.catalog, self.science, self.green, self.blue = game, catalog, science, green, blue
        self.path = Path(game.cfg.runtime_dir) / "normal-research.json"
        self.state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    def step(self, obs, parameters, *, plan=None):
        """Wait while the technology sits in the native queue; otherwise queue it (``plan`` = policy order)."""
        validate_parameters(parameters)
        name = parameters["technology"]
        tech = self.catalog.technologies.get(name)
        if tech is None:
            return {"status": "blocked", "reason": "unknown_technology"}
        if self.state and (
            self.state["world_id"] != obs["world_id"] or self.state["catalog_fingerprint"] != self.catalog.fingerprint
        ):
            return {"status": "blocked", "reason": "normal_research_world_or_catalog_changed"}
        if obs.get("technologies", {}).get(name):
            proof = self.proof(obs, parameters)
            return {"status": "succeeded", "reason": "technology researched by the native engine", "evidence": proof}
        missing = [p for p in tech["prerequisites"] if not obs.get("technologies", {}).get(p)]
        if missing:
            return {"status": "blocked", "reason": "research_prerequisites_missing", "technologies": missing}
        if tech.get("research_trigger"):
            return {
                "status": "blocked",
                "reason": "natural_trigger_requires_its_gameplay_capability",
                "trigger": tech["research_trigger"],
            }
        packs = {p["name"] for p in tech["ingredients"]}
        supported = {"automation-science-pack"}
        green_supply = None
        if "logistic-science-pack" in packs and getattr(self, "green", None):
            green_supply = self.green.proof(obs)
            if green_supply:
                supported.add("logistic-science-pack")
            elif getattr(self.green, "state", {}).get("phase") == "verified":
                return {
                    "status": "waiting",
                    "reason": "waiting for commissioned green supply and powered labs to recover",
                }
        blue_supply = None
        if "chemical-science-pack" in packs and getattr(self, "blue", None):
            blue_supply = self.blue.proof(obs)
            if blue_supply:
                supported.add("chemical-science-pack")
            elif getattr(self.blue, "state", {}).get("phase") == "verified":
                return {
                    "status": "waiting",
                    "reason": "waiting for commissioned blue supply and powered labs to recover",
                }
        unsupported = packs - supported
        if unsupported:
            # The pack's permanent factory is a later roadmap step; wait for it
            # instead of blocking (seed 4176033748: four research steps blocked
            # before green science existed).
            return {
                "status": "waiting",
                "reason": "waiting_for_science_pack_supply",
                "packs": sorted(unsupported),
            }
        supply = self.science.proof(obs)
        if not supply:
            return {"status": "waiting", "reason": "waiting for commissioned machine science and automatic lab supply"}
        if not self.state:
            self.state = {"world_id": obs["world_id"], "catalog_fingerprint": self.catalog.fingerprint, "targets": {}}
        if name not in self.state["targets"]:
            self.state["targets"][name] = {
                "tick": obs["tick"],
                "was_active": obs.get("research") == name,
                "research_progress": obs.get("research_progress", 0),
                "supply_proof": supply,
                "green_supply_proof": green_supply,
                "blue_supply_proof": blue_supply,
                "consumed": {p["name"]: obs["production"][p["name"]]["consumed"] for p in tech["ingredients"]},
            }
            _atomic_json(self.path, self.state)
        from factorio_agent.research_queue import native_queue, queue_action

        queue = native_queue(obs)
        if name not in queue:
            # Not queued yet: enqueue it right after the running research,
            # followed by the rest of the policy plan (research_queue.py).
            technologies = list(plan or ())
            if name not in technologies:
                technologies = [name]
            action = queue_action(
                obs,
                technologies,
                reason="queue roadmap technology with automatically belt-fed machine packs",
                require_room=False,
            )
            if action is not None:
                return action
        return {
            "status": "waiting",
            "reason": "native labs consume normally produced science",
            "evidence": {
                "technology": name,
                "queue_position": queue.index(name) if name in queue else None,
                "research_queue": queue,
                "research_progress": obs.get("research_progress", 0) if obs.get("research") == name else 0,
                "autonomous_wait": True,
                "background_work_safe": True,
            },
        }

    def supplied_packs(self, obs):
        """Science packs with a proven automatic lab supply, by the same proofs ``step`` requires.

        Red needs the machine science proof; without it nothing is supplied.
        Green and blue each additionally need their own commissioned supply proof.
        """
        if not self.science.proof(obs):
            return set()
        packs = {"automation-science-pack"}
        if getattr(self, "green", None) and self.green.proof(obs):
            packs.add("logistic-science-pack")
        if getattr(self, "blue", None) and self.blue.proof(obs):
            packs.add("chemical-science-pack")
        return packs

    def proof(self, obs, parameters):
        name = parameters["technology"]
        if not obs.get("technologies", {}).get(name):
            return None
        target = self.state.get("targets", {}).get(name)
        if not target:
            return {"tick": obs["tick"], "technology": name, "already_researched": True}
        consumed = {item: obs["production"][item]["consumed"] - count for item, count in target["consumed"].items()}
        if any(count < 0 for count in consumed.values()):
            return None
        if not target["was_active"] and not all(count > 0 for count in consumed.values()):
            return None
        return {
            "tick": obs["tick"],
            "technology": name,
            "native_researched": True,
            "science_consumed_since_first_request": consumed,
            "started_tick": target["tick"],
            "machine_supply_units_at_start": target["supply_proof"]["science_units"],
            "green_supply_units_at_start": (target.get("green_supply_proof") or {}).get("science_units"),
            "blue_supply_units_at_start": (target.get("blue_supply_proof") or {}).get("science_units"),
            "scope": "native technology completion from normal machine packs and belt-fed labs; consumption counters are world totals over this request interval",
        }
