from __future__ import annotations

from factorio_ai.models import PlannerDecision, nearest_resource, player_position, distance, nearest_entity, entities_named
from factorio_ai.planner import PlannerDecision as PlannerDecisionType


class ResearchAutomationSkill:
    """Research automation: find a researchable technology and attempt to research it.
    Handles build failures by scanning for clear positions or mining blockers.
    """

    def __init__(self, target_technology: str = "automation-science-pack"):
        self.target_technology = target_technology
        self.max_attempts = 10
        self.attempt_count = 0

    def next_action(self, observation):
        """Attempt to research the target technology.
        Returns done=True when research is complete or abandoned.
        """
        self.attempt_count += 1
        if self.attempt_count > self.max_attempts:
            return PlannerDecision(None, "max attempts reached; aborting research", done=True)

        # Check if research is already complete
        if self.target_technology in observation.get("researched", []):
            return PlannerDecision(None, f"{self.target_technology} already researched", done=True)

        # Check if we can research this technology
        if self.target_technology not in observation.get("researchable", []):
            return PlannerDecision(None, f"{self.target_technology} not researchable", done=True)

        # Find a research station
        research_stations = entities_named(observation, "lab")
        if not research_stations:
            return PlannerDecision({"type": "wait", "ticks": 60}, "no lab found; waiting")

        # Try to find a clear position near a lab
        lab = research_stations[0]
        lab_position = lab.get("position", {"x": 0, "y": 0})
        
        # Scan for a clear position around the lab
        clear_position = self._find_clear_position(observation, lab_position)
        if clear_position is None:
            return PlannerDecision({"type": "wait", "ticks": 60}, "no clear position found; waiting")

        # Move to the clear position
        player_pos = player_position(observation)
        if distance(player_pos, clear_position) > 3:
            return PlannerDecision({"type": "move_to", "position": clear_position}, "move to clear position near lab")

        # Attempt to research
        return PlannerDecision({"type": "research", "technology": self.target_technology}, f"research {self.target_technology}")

    def _find_clear_position(self, observation, center: dict) -> dict | None:
        """Find a clear position within 3 tiles of the center.
        Returns None if no clear position is found.
        """
        import math
        
        # Scan positions in a 3x3 area around the center
        for dx in range(-3, 4):
            for dy in range(-3, 4):
                pos = {"x": center["x"] + dx, "y": center["y"] + dy}
                
                # Check if this position is clear (no blockers)
                if self._is_position_clear(observation, pos):
                    return pos
        
        return None

    def _is_position_clear(self, observation, position: dict) -> bool:
        """Check if a position is clear of blockers (trees, rocks, cliffs, water).
        Returns True if clear, False otherwise.
        """
        # Check for trees
        trees = entities_named(observation, "tree")
        for tree in trees:
            tree_pos = tree.get("position", {})
            if tree_pos.get("x") == position["x"] and tree_pos.get("y") == position["y"]:
                return False
        
        # Check for simple entities (rocks, cliffs)
        simple_entities = entities_named(observation, "simple-entity")
        for entity in simple_entities:
            entity_pos = entity.get("position", {})
            if entity_pos.get("x") == position["x"] and entity_pos.get("y") == position["y"]:
                return False
        
        # Check for cliffs
        cliffs = entities_named(observation, "cliff")
        for cliff in cliffs:
            cliff_pos = cliff.get("position", {})
            if cliff_pos.get("x") == position["x"] and cliff_pos.get("y") == position["y"]:
                return False
        
        # Check for water
        water_tiles = entities_named(observation, "water")
        for water in water_tiles:
            water_pos = water.get("position", {})
            if water_pos.get("x") == position["x"] and water_pos.get("y") == position["y"]:
                return False
        
        return True
