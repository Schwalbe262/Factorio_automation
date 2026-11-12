"""Current native recipe locks and queue cycles hidden behind those locks."""

from __future__ import annotations


def native_recipe_gates(catalog, observation, capabilities):
    enabled = observation.get("enabled_recipes", {})
    if not isinstance(enabled, dict):
        raise ValueError("native enabled recipe observation required")
    unlockers = {}
    for technology, spec in catalog.technologies.items():
        for recipe in spec.get("unlocks", []):
            unlockers.setdefault(recipe, []).append(technology)
    result = {}
    for capability, spec in capabilities.items():
        required = spec.get("native_recipes")
        if not required:
            continue
        missing = sorted(recipe for recipe in required if enabled.get(recipe) is not True)
        options = {recipe: sorted(unlockers.get(recipe, [])) for recipe in missing}
        result[capability] = dict(
            required_recipes=sorted(required),
            missing_recipes=missing,
            native_recipes_ready=not missing,
            recipe_unlockers=options,
            required_technologies=sorted({names[0] for names in options.values() if len(names) == 1}),
            unresolved_unlock_recipes=[r for r, names in options.items() if not names],
            scope="native recipe locks only; factory, power and capability prerequisites still apply",
        )
    return result


def reject_native_recipe_cycles(nodes, gates, incoming):
    """Reject proposed research depending on the factory that research unlocks."""
    if not incoming or not gates:
        return
    incoming = set(incoming)
    by_id = {node["id"]: node for node in nodes}

    def depends_on(start, target):
        changed = start in incoming or target in incoming
        pending = [(key, changed or key in incoming) for key in by_id[start]["depends_on"]]
        seen = set()
        while pending:
            key, touched = pending.pop()
            if key == target:
                if touched:
                    return True
                continue
            if (key, touched) in seen:
                continue
            seen.add((key, touched))
            pending.extend((dep, touched or dep in incoming) for dep in by_id[key]["depends_on"])
        return False

    research = [
        n
        for n in nodes
        if n["capability"] in ("research", "starter_research", "bootstrap_research") and n.get("status") != "completed"
    ]
    for producer in nodes:
        if producer.get("status") == "completed":
            continue
        options = gates.get(producer["capability"], {}).get("recipe_unlockers", {})
        for recipe, technologies in options.items():
            # Alternative unlocks need a separate choice; one cyclic option
            # does not prove that every legal research path is cyclic.
            if len(technologies) != 1:
                continue
            for study in research:
                if study["parameters"].get("technology") != technologies[0]:
                    continue
                if depends_on(study["id"], producer["id"]):
                    raise ValueError(
                        "native recipe dependency cycle: research "
                        + technologies[0]
                        + " must precede "
                        + producer["id"]
                        + " to unlock "
                        + recipe
                        + "; remove its dependency on that producer"
                    )
