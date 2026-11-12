"""Bounded building stock preparation while preserving the active raw budget."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import math
from pathlib import Path

from .construction_supply import OUTPUT_NAMES,CHEST_NAMES


ITEMS=frozenset(('burner-mining-drill','electric-mining-drill','stone-furnace','steel-furnace','electric-furnace',
    'assembling-machine-1','assembling-machine-2','assembling-machine-3','inserter','burner-inserter','fast-inserter',
    'long-handed-inserter','bulk-inserter','stack-inserter','small-electric-pole','medium-electric-pole','big-electric-pole',
    'substation','transport-belt','fast-transport-belt','express-transport-belt','underground-belt','fast-underground-belt',
    'express-underground-belt','splitter','fast-splitter','express-splitter','pipe','pipe-to-ground','boiler','steam-engine',
    'offshore-pump','pump','pumpjack','oil-refinery','chemical-plant','storage-tank','lab','wooden-chest','iron-chest',
    'steel-chest','radar','stone-wall','gun-turret','solar-panel','accumulator','small-lamp'))
RAW=frozenset(('iron-plate','copper-plate','stone-brick','stone','coal','wood'))


def validate_parameters(parameters):
    if not isinstance(parameters,dict)or set(parameters)!={'items'}or not isinstance(parameters['items'],dict):
        raise ValueError('prepare_components requires items with absolute stock targets')
    items=parameters['items']
    if not 1<=len(items)<=8 or any(name not in ITEMS or type(count)is not int or not 1<=count<=50 for name,count in items.items()):
        raise ValueError('prepare_components supports 1..8 building kinds, each absolute target1..50')


def _count(value):
    if type(value)not in(int,float)or not math.isfinite(value)or value<0 or value%1:
        raise ValueError('finite nonnegative item counts required')
    return int(value)


class ComponentReserve:
    def __init__(self,game,bootstrap,catalog):
        self.game,self.bootstrap,self.catalog=game,bootstrap,catalog;self.last_evidence={}

    def _recipe(self,item,obs):
        recipe=self.catalog.recipe_for_product(item)
        if not recipe or not obs.get('enabled_recipes',{}).get(recipe.get('name')):
            raise ValueError('native_recipe_locked_or_unknown:'+item)
        products=recipe.get('products',[])
        if(len(products)!=1 or products[0].get('name')!=item or products[0].get('type','item')!='item'
                or products[0].get('probability',1)!=1 or _count(products[0].get('amount'))<1):
            raise ValueError('deterministic_single_item_recipe_required:'+item)
        return recipe

    def _recipe_cost(self,recipe,runs,obs,recipes=None):
        pool=Counter({name:_count(n)for name,n in obs.get('inventory',{}).items()});cost=Counter()
        def expand(item,count,stack):
            if item in RAW:cost[item]+=count;return
            use=min(pool[item],count);pool[item]-=use;count-=use
            if not count:return
            if item in stack or len(stack)>24:raise ValueError('component_recipe_dependency_cycle:'+item)
            dependency=self._recipe(item,obs);cycles=math.ceil(count/_count(dependency['products'][0]['amount']))
            ingredients(dependency,cycles,(*stack,item))
        def ingredients(current,cycles,stack):
            if recipes is not None:recipes.add(current['name'])
            rows=current.get('ingredients')
            if not isinstance(rows,list)or not rows:raise ValueError('component_recipe_cost_unknown')
            for row in rows:
                if row.get('type','item')!='item':raise ValueError('component_recipe_requires_factory_fluid')
                amount=_count(row.get('amount'))
                if amount<1:raise ValueError('component_recipe_cost_unknown')
                expand(row['name'],amount*cycles,stack)
        ingredients(recipe,runs,());return dict(cost)

    def _public_stock(self,obs,item):
        ownership=getattr(self.bootstrap,'_unreserved_stock_observation',None)
        if not callable(ownership):raise ValueError('component_public_stock_ownership_unavailable')
        filtered=ownership(obs,item)
        total=0;seen=set()
        for row in filtered.get('entities',[]):
            unit=row.get('unit_number');position=row.get('position',{})
            if type(unit)is not int or unit<1 or unit in seen or set(position)!={'x','y'}:continue
            if any(type(v)not in(int,float)or not math.isfinite(v)for v in position.values()):continue
            if row.get('name')in OUTPUT_NAMES:stock=row.get('output_inventory',{})
            elif row.get('name')in CHEST_NAMES:stock=row.get('inventory',{})
            elif row.get('name')=='transport-belt':stock=row.get('belt_inventory',{})
            else:continue
            seen.add(unit);total+=_count(stock.get(item,0))
        return total

    def _budget(self,obs):
        budget=obs.get('known_project_construction_budget',obs.get('construction_budget'))
        if budget is None:
            root=getattr(getattr(self.game,'cfg',None),'runtime_dir',None)
            if not isinstance(root,(str,Path)):raise ValueError('active_construction_budget_required')
            from .diagnostics import construction_budget
            budget=construction_budget(Path(root),obs,self.catalog)
        if(not isinstance(budget,dict)or not isinstance(budget.get('base_material_upper_bound'),dict)
                or budget.get('unresolved_materials')):raise ValueError('active_construction_material_cost_unknown')
        reserved={name:_count(n)for name,n in budget['base_material_upper_bound'].items()}
        if any(name not in RAW for name in reserved):raise ValueError('active_construction_raw_material_unknown')
        return reserved

    def proof(self,obs,parameters):
        validate_parameters(parameters)
        held={name:_count(obs.get('inventory',{}).get(name,0))for name in parameters['items']}
        if any(held[name]<target for name,target in parameters['items'].items()):return None
        return dict(tick=obs.get('tick'),world_id=obs.get('world_id'),targets=deepcopy(parameters['items']),held=held,
            scope='actual actor building inventory at absolute targets; no future output credit')

    def step(self,obs,parameters):
        validate_parameters(parameters);proof=self.proof(obs,parameters)
        if proof:return dict(status='succeeded',reason='component_absolute_stock_targets_available',evidence=proof)
        try:
            reserved=self._budget(obs)
            held={name:_count(obs.get('inventory',{}).get(name,0))for name in RAW}
            public={name:self._public_stock(obs,name)for name in RAW}
        except (ValueError,KeyError,TypeError,OSError)as exc:
            self.last_evidence={'detail':str(exc)}
            return dict(status='blocked',reason='component_reserve_budget_unavailable',evidence=self.last_evidence)
        waits=[];blocks=[]
        for item,target in parameters['items'].items():
            missing=target-_count(obs.get('inventory',{}).get(item,0))
            if missing<=0:continue
            try:
                recipe=self._recipe(item,obs);runs=math.ceil(missing/_count(recipe['products'][0]['amount']))
                allowed_recipes=set();target_cost=self._recipe_cost(recipe,runs,obs,allowed_recipes)
                evidence=dict(item=item,target=target,missing=missing,raw_bom=target_cost,
                    held_raw=held,public_raw=public,protected_raw=reserved)
                result=self.bootstrap.ensure_item(obs,item,target)
                if result.get('type')=='craft':
                    action_recipe=self.catalog.recipes.get(result.get('recipe'))
                    if not action_recipe or action_recipe.get('name')not in allowed_recipes or not obs.get('enabled_recipes',{}).get(action_recipe['name']):
                        raise ValueError('native_component_action_recipe_locked_or_unrelated')
                    count=result.get('count')
                    if type(count)is not int or count<1:raise ValueError('component_action_cost_unknown')
                    spend=self._recipe_cost(action_recipe,count,obs);evidence['action_raw_cost']=spend
                    short={name:amount+reserved.get(name,0)-held.get(name,0)-public.get(name,0)for name,amount in spend.items()
                        if amount+reserved.get(name,0)>held.get(name,0)+public.get(name,0)}
                    if short:
                        evidence['shortfall']=short;blocks.append(dict(reason='component_preparation_would_spend_primary_raw_reserve',evidence=evidence));continue
                    # Public machines can consume their own output between
                    # observations. Bring the protected balance into the actor
                    # before issuing a craft; public stock is not a reservation.
                    prefetch=next((name for name,amount in spend.items()if held.get(name,0)<amount+reserved.get(name,0)),None)
                    if prefetch:
                        evidence['reserve_prefetch_item']=prefetch
                        pickup=self.bootstrap.ensure_item(obs,prefetch,spend[prefetch]+reserved.get(prefetch,0))
                        if pickup.get('type')in('take','take_many','move','mine'):
                            evidence['action_raw_cost']={};self.last_evidence=evidence;return pickup
                        blocks.append(dict(reason='component_reserve_prefetch_required',evidence=evidence));continue
                if result.get('type')=='wait':
                    waits.append(dict(reason=result.get('reason','component_preparation_wait'),evidence=evidence));continue
                if result.get('type'):
                    if result['type']not in('craft','take','take_many','take_mall_stock','mine','move','wait'):
                        raise ValueError('unsupported_component_preparation_action')
                    self.last_evidence=evidence;return result
                row=dict(reason=result.get('reason','component_preparation_wait'),evidence=evidence)
                (waits if result.get('status')=='waiting'else blocks).append(row)
            except (ValueError,KeyError,TypeError)as exc:blocks.append(dict(reason=str(exc),item=item))
        self.last_evidence=dict(blocked=blocks,waiting=waits)
        return dict(status='waiting'if waits else'blocked',reason='component_preparation_waiting'if waits else'component_preparation_blocked',evidence=self.last_evidence)
