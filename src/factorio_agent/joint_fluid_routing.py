"""Same-consumer prospective fluid crossings; foreign/native hard cells stay hard."""
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json

from .transport import VECTORS,xy


def revision(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


@dataclass(frozen=True)
class OwnedFluidPrior:
    world_id:str
    core_revision:str
    entities_revision:str
    rows:tuple


@dataclass(frozen=True)
class ProspectiveWaterSource:
    world_id:str
    catalog_fingerprint:str
    old_source:dict
    port:dict
    producer_revision:str


def trusted_prior(world_id,core,fluid_plans):
    """Called by the owning planner after the prior typed routes have succeeded."""
    rows=[];entities=[]
    for fluid,plan in fluid_plans.items():
        route=plan['route'];entities+=route['entities']
        if not route.get('ok')or not isinstance(route.get('independent_clear_cells'),list):
            raise ValueError('prior route requires its original independent native clearance')
        rows.append((fluid,deepcopy(route['entities']),set(map(tuple,route['independent_clear_cells'])),revision(route['entities'])))
    return OwnedFluidPrior(world_id,revision(core),revision(entities),tuple(rows))


def crossing_permission(context,world_id,core,prior,base_hard,actual):
    """Only independently clear halos of normal unbuilt pipes may be buried."""
    if context is None:return set(),{}
    if(not isinstance(context,OwnedFluidPrior)or context.world_id!=world_id or context.core_revision!=revision(core)
            or context.entities_revision!=revision(prior)):
        raise ValueError('same-consumer prior identity changed')
    cells=set();owners={};native_positions={(q[1],q[2])for q in actual}
    for fluid,entities,clear,owner_revision in context.rows:
        if fluid not in('water','petroleum-gas'):continue
        for spec in entities:
            p=xy(spec)
            if spec['name']!='pipe'or ('pipe',*p)in actual:continue
            halo={p}|{(p[0]+dx,p[1]+dy)for dx,dy in VECTORS.values()}
            if halo&(base_hard|native_positions)or not halo<=clear:continue
            owners[p]=dict(spec,fluid=fluid,owner_revision=owner_revision)
            cells.update(halo)
    return cells,owners


def validate_underpasses(compiled,permitted,owners):
    """A cleared halo is usable only inside one straight, bounded paid tunnel."""
    path={(p['x'],p['y'])for p in compiled.get('full_path',[])};used=path&permitted
    if not used:return {'ok':True,'crossings':[]}
    buried=set();crossings=[]
    for pair in compiled.get('underground_pairs',[]):
        dx,dy=VECTORS[pair['direction']];a=pair['first']
        cells={(a[0]+dx*i,a[1]+dy*i)for i in range(1,pair['span'])}
        touched=cells&used
        if not touched:continue
        selected=[(p,q)for p,q in owners.items()if p in cells]
        if len(selected)!=1:return {'ok':False,'reason':'owned_fluid_crossing_requires_one_declared_pipe'}
        p,spec=selected[0];buried.update(cells)
        crossings.append({'schema':'same-consumer-fluid-v1','pipe':deepcopy(spec),'pair':deepcopy(pair)})
    if not used<=buried or len(crossings)>1:
        return {'ok':False,'reason':'owned_fluid_halo_requires_one_buried_crossing'}
    return {'ok':True,'crossings':crossings}
