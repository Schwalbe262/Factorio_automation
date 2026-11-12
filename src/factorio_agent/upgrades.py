"""Normal, prerequisite-ready upgrades alongside autonomous factory work."""
from __future__ import annotations


PRIORITIES=(
    'stone-wall','radar','military','military-2','repair-pack',
    'physical-projectile-damage-1','weapon-shooting-speed-1',
    'mining-productivity-1','research-speed-1',
    'physical-projectile-damage-2','weapon-shooting-speed-2',
    'mining-productivity-2','research-speed-2',
    'physical-projectile-damage-3','weapon-shooting-speed-3',
    'mining-productivity-3','research-speed-3',
)


def next_upgrade(obs,researcher):
    """Return only an ordinary research action; preserve active research/defense."""
    if obs.get('research')is not None:return None
    for name in PRIORITIES:
        technology=researcher.catalog.technologies.get(name)
        if(not technology or technology.get('enabled')is False or technology.get('research_trigger')
                or obs.get('technologies',{}).get(name)
                or any(not obs.get('technologies',{}).get(p)for p in technology.get('prerequisites',[]))):
            continue
        result=researcher.step(obs,{'technology':name})
        if result.get('type')=='research':return result
    return None
