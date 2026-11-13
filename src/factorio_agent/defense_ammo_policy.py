"""Persistent factory-funded piercing upgrades of registered defensive posts."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from factorio_agent.core.deterministic_state import _atomic_json
from .post_ammo_transfer import (
    ACTION,
    AMMUNITION_LUA,
    POLICY_FILE,
    RED,
    YELLOW,
    active_reservations,
    revision,
    signature,
    validate_action,
    valid_receipt,
)


INSPECT_POST_AMMUNITION_LUA = (
    AMMUNITION_LUA
    + r"""
local main=a.get_main_inventory();if not main or not main.valid then return reject("post_ammunition_actor_inventory_missing")end
local actor=totals(main);if not actor then return reject("post_ammunition_actor_metadata_unsupported")end
local rows={}
for _,q in ipairs(x.posts)do
 local e=target(q.position,"gun-turret");local row={name=q.name,position=q.position}
 if not e or not e.valid then row.missing=true
 elseif not owned(q)then row.changed=true
 else
  row.unit_number=e.unit_number;row.health=e.health;row.max_health=e.max_health
  local inv=e.get_inventory(defines.inventory.turret_ammo);row.ammo=inv and totals(inv)
  if not row.ammo or #inv~=1 then return reject("post_ammunition_registered_turret_metadata_unsupported")end
  local st=inv[1];if st.valid_for_read then
   if not plain(st)then return reject("post_ammunition_registered_turret_item_unsupported")end
   row.stock={name=st.name,count=st.count,ammo=st.ammo}
  end
  row.nearby_enemies=s.count_entities_filtered{position=e.position,radius=36,force="enemy",type={"unit","turret","unit-spawner"}}
 end
 rows[#rows+1]=row
end
local markers={}
for _,id in ipairs(x.pending)do local m=d.defense_ammo_upgrades and d.defense_ammo_upgrades[id]
 if m then markers[id]={phase=m.phase,world_id=m.world_id,actor_unit=m.actor_unit,unit_number=m.unit_number,receipt=m.receipt}end
end
return {ok=true,tick=game.tick,actor=actor,posts=rows,markers=markers,range=prototypes.entity["gun-turret"].attack_parameters.range-2}
"""
)


class DefenseAmmoPolicy:
    """Call from the sole writer before routine top-offs; None preserves upkeep."""

    def __init__(self, game, catalog, perimeter, piercing):
        self.game, self.catalog, self.perimeter, self.piercing = game, catalog, perimeter, piercing
        self.path = Path(game.cfg.runtime_dir) / POLICY_FILE
        self.last_diagnostic = None

    def preferred_item(self, post_name):
        """Desired tier only; callers must still inspect actual native ammunition."""
        if not self.path.exists():
            return YELLOW
        state = json.loads(self.path.read_text(encoding="utf-8"))
        if state.get("catalog_fingerprint") != self.catalog.fingerprint or state.get("world_id") != getattr(
            self.game, "expected_world", None
        ):
            raise ValueError("ammunition preference world or catalog changed")
        item = state.get("preferences", {}).get(post_name, {}).get("item", YELLOW)
        if item not in (YELLOW, RED):
            raise ValueError("unsupported ammunition preference")
        return item

    def _frame(self, obs):
        if not obs.get("ok", True) or not obs.get("world_id") or type(obs.get("actor_unit_number")) is not int:
            raise ValueError("successful live actor observation required for ammunition policy")
        root = self.path.parent
        perimeter = json.loads((root / "defense-perimeter.json").read_text(encoding="utf-8"))
        if (
            perimeter.get("world_id") != obs["world_id"]
            or perimeter.get("catalog_fingerprint") != self.catalog.fingerprint
        ):
            raise ValueError("ammunition policy perimeter world or catalog changed")
        posts = perimeter.get("posts", [])
        if not isinstance(posts, list) or len(posts) > 128 or len({p.get("name") for p in posts}) != len(posts):
            raise ValueError("bounded unique registered defensive posts required")
        state = (
            json.loads(self.path.read_text(encoding="utf-8"))
            if self.path.exists()
            else dict(
                world_id=obs["world_id"],
                catalog_fingerprint=self.catalog.fingerprint,
                preferences={},
                transactions={},
                conversions={},
            )
        )
        if state.get("world_id") != obs["world_id"] or state.get("catalog_fingerprint") != self.catalog.fingerprint:
            raise ValueError("ammunition policy checkpoint world or catalog changed")
        reservations = active_reservations(root, obs["world_id"], self.catalog.fingerprint)
        pending = [key for key, value in state["transactions"].items() if value["phase"] == "pending"]
        if len(pending) > 1:
            raise ValueError("only one pending post ammunition transaction is allowed")
        x = dict(
            expected_entity_world_id=obs["world_id"],
            expected_actor_unit=obs["actor_unit_number"],
            posts=posts,
            pending=pending,
        )
        report = self.game.query(
            "local x=helpers.json_to_table("
            + json.dumps(json.dumps(x, separators=(",", ":")))
            + ");"
            + INSPECT_POST_AMMUNITION_LUA
        )
        if not report.get("ok"):
            raise ValueError(report.get("reason", "native post ammunition inspection failed"))
        if any(p.get("changed") for p in report["posts"]):
            raise ValueError("registered defensive post changed identity")
        return state, perimeter, reservations, report

    def _front_posts(self, world):
        path = self.path.parent / "defense-hardening.json"
        if not path.exists():
            return set()
        state = json.loads(path.read_text(encoding="utf-8"))
        if state.get("world_id") != world or state.get("catalog_fingerprint") != self.catalog.fingerprint:
            raise ValueError("front defensive post authority changed")
        return {p["name"] for p in state.get("posts", [])} if state.get("phase") == "verified" else set()

    @staticmethod
    def _supports(post, rows, posts, radius):
        result = []
        for other in posts:
            row = rows[other["name"]]
            if other["name"] == post["name"] or row.get("missing") or row.get("changed"):
                continue
            if (
                row.get("unit_number") != other.get("unit_number")
                or row.get("health", 0) < row.get("max_health", 1) * 0.5
            ):
                continue
            if sum(row.get("ammo", {}).get(item, {}).get("full", 0) for item in (YELLOW, RED)) < 1:
                continue
            if sum((row["position"][axis] - post["position"][axis]) ** 2 for axis in ("x", "y")) > radius**2:
                continue
            result.append(signature(other))
        return sorted(result, key=lambda p: p["name"])[:2]

    def prepare(self, obs):
        """Read-only current eligibility and priorities; no coverage claims invented."""
        try:
            state, perimeter, reservation, report = self._frame(obs)
            front = self._front_posts(obs["world_id"])
            rows = {p["name"]: p for p in report["posts"]}
            candidates = []
            uncovered = []
            for post in perimeter["posts"]:
                row = rows[post["name"]]
                if row.get("missing") or not post.get("unit_number"):
                    continue
                red = row["ammo"][RED]["full"]
                if red >= 5:
                    continue
                supports = self._supports(post, rows, perimeter["posts"], report["range"])
                if len(supports) < 2:
                    uncovered.append(post["name"])
                    continue
                candidates.append(
                    dict(
                        post=signature(post),
                        stock=deepcopy(row.get("stock")),
                        supports=supports,
                        count=10 - red if row.get("stock", {}).get("name") == RED else 10,
                        priority=(
                            0 if post["name"] in front else 1,
                            -row.get("nearby_enemies", 0),
                            row.get("health", 1) / max(row.get("max_health", 1), 1),
                            post["name"],
                        ),
                    )
                )
            candidates.sort(key=lambda p: p["priority"])
            return dict(
                ok=True,
                state=state,
                perimeter=perimeter,
                reservation=reservation,
                report=report,
                candidates=candidates,
                unsupported_posts=uncovered,
            )
        except (ValueError, KeyError, TypeError, OSError) as exc:
            return dict(ok=False, reason=str(exc))

    def step(self, obs):
        """Emit paid collection/conversion only; do not stall ordinary fallback upkeep."""
        preview = self.prepare(obs)
        if not preview.get("ok"):
            self.last_diagnostic = preview
            return {"status": "blocked", "reason": "defense_ammo_policy_guard_failed", "evidence": preview}
        state, report = preview["state"], preview["report"]
        for key, txn in state["transactions"].items():
            if txn["phase"] != "pending":
                continue
            marker = report.get("markers", {}).get(key)
            if marker:
                receipt = marker.get("receipt")
                action = txn["payload"]["action"]
                if marker.get("phase") != "complete" or not valid_receipt(receipt, dict(action, transaction_id=key)):
                    return {
                        "status": "blocked",
                        "reason": "post_ammunition_uncertain_or_changed_native_receipt",
                        "transaction_id": key,
                    }
                txn.update(phase="complete", receipt=deepcopy(receipt))
                state["conversions"].setdefault(action["post_name"], []).append(key)
                _atomic_json(self.path, state)
                return None
            # Combat can change a partial stack between observations. An unexecuted
            # intent may be cancelled only after a fresh native absence of its marker.
            # The next step creates another immutable intent without erasing this one.
            rows = {p["name"]: p for p in report["posts"]}
            row = rows.get(txn["payload"]["action"]["post_name"], {})
            a = txn["payload"]["action"]
            current_posts = {p["name"]: p for p in preview["perimeter"]["posts"]}
            supports_stale = any(
                p["name"] not in current_posts
                or signature(current_posts[p["name"]]) != p
                or rows[p["name"]].get("missing")
                or rows[p["name"]].get("health", 0) < rows[p["name"]].get("max_health", 1) * 0.5
                or sum(rows[p["name"]].get("ammo", {}).get(item, {}).get("full", 0) for item in (YELLOW, RED)) < 1
                for p in txn["payload"]["supports"]
            )
            stale = (
                row.get("missing")
                or row.get("unit_number") != a["expected_entity_unit"]
                or row.get("stock") != txn["payload"]["stock"]
                or a["expected_actor_unit"] != obs["actor_unit_number"]
                or txn["payload"]["reservation"] != preview["reservation"]
                or supports_stale
            )
            if stale:
                txn.update(phase="cancelled_unexecuted", cancelled_tick=report["tick"])
                _atomic_json(self.path, state)
                return None
            target = preview["reservation"]["count"] + a["count"]
            if report["actor"][RED]["full"] < target:
                supplied = (
                    self.piercing.ensure_full_magazines(obs, target)
                    if self.piercing and self.piercing.commissioned
                    else None
                )
                return supplied if supplied and supplied.get("type") else None
            return dict(
                a,
                transaction_id=key,
                reason="normally upgrade or refill one registered defensive post with factory piercing ammunition",
            )
        if not preview["candidates"]:
            self.last_diagnostic = dict(
                reason="no_supported_post_needs_piercing_conversion", unsupported_posts=preview["unsupported_posts"]
            )
            return None
        if not self.piercing or not self.piercing.commissioned:
            self.last_diagnostic = dict(reason="commissioned_piercing_factory_required_for_global_upgrade")
            return None
        candidate = preview["candidates"][0]
        count = candidate["count"]
        held = report["actor"][RED]["full"]
        target = preview["reservation"]["count"] + count
        if held < target:
            action = self.piercing.ensure_full_magazines(obs, target)
            self.last_diagnostic = dict(
                reason="waiting_for_factory_red_stock",
                required=target,
                actual_full=held,
                reservation=preview["reservation"],
            )
            return action if action and action.get("type") else None
        # The factory's existing proof includes its actual unit/recipe/output witness.
        # This proves the provider, not continuous spare steel or a sustained rate.
        if not self.piercing.proof(obs):
            self.last_diagnostic = dict(reason="piercing_factory_fresh_proof_required")
            return None
        post = candidate["post"]
        action = dict(
            type=ACTION,
            name="gun-turret",
            post_name=post["name"],
            position=deepcopy(post["position"]),
            item=RED,
            count=count,
            expected_entity_unit=post["unit_number"],
            expected_entity_world_id=obs["world_id"],
            expected_actor_unit=obs["actor_unit_number"],
            expected_catalog_fingerprint=self.catalog.fingerprint,
        )
        payload = dict(
            action=action,
            post=post,
            supports=candidate["supports"],
            stock=candidate["stock"],
            reservation=preview["reservation"],
            created_tick=report["tick"],
        )
        key = revision(payload)
        validate_action(dict(action, transaction_id=key), obs["world_id"])
        state["preferences"][post["name"]] = dict(item=RED, selected_tick=report["tick"])
        state["transactions"][key] = dict(phase="pending", payload=payload)
        _atomic_json(self.path, state)
        return dict(
            action,
            transaction_id=key,
            reason="normally upgrade or refill one registered defensive post with factory piercing ammunition",
        )
