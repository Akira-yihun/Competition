"""Single owner of same-turn actor, position, tower and gold reservations."""
from dataclasses import dataclass, field
from typing import Any
from .model import Pos
from .rules import TOWER_TYPES, SHOP_PRICES

@dataclass(frozen=True)
class Intent:
    role_id: int
    command: dict[str, Any]
    reason: str = 'policy'
    priority: int = 0

@dataclass
class ReservationTable:
    gold: int
    towers: int
    actors: set = field(default_factory=set)
    cells: set = field(default_factory=set)

    def reserve(self, intent, turn):
        cmd = intent.command
        actor = int(cmd['controllerId']) if cmd['action']=='attack' else intent.role_id
        if actor in self.actors:
            return False
        action = cmd['action']
        cell = Pos.load(cmd['targetPos'][0]) if action in ('move','build') else None
        if cell is not None and cell in self.cells:
            return False
        cost = 0
        tower = action=='build' and cmd.get('name') in TOWER_TYPES
        if tower:
            cost = 25
            if self.towers >= 3:
                return False
        if action=='buy':
            prices={i['name']:i['price'] for i in turn.raw.get('weaponShopList',[]) if isinstance(i,dict) and 'name' in i and 'price' in i}
            price=prices.get(cmd['name'],SHOP_PRICES.get(cmd['name']))
            if price is None and 'UpgradeVoucher' in cmd['name']:
                price=100 if cmd['name'].endswith('1') else 150
            if type(price) is not int or price <= 0:
                return False
            cost=price*cmd.get('num',1)
        if cost>self.gold:
            return False
        self.gold-=cost
        self.towers+=int(tower)
        self.actors.add(actor)
        if cell is not None:
            self.cells.add(cell)
        return True

def arbitrate(intents, turn):
    table=ReservationTable(turn.gold,len(turn.weapons()))
    commands={}
    for intent in sorted(intents,key=lambda i:-i.priority):
        if str(intent.role_id) not in commands and table.reserve(intent,turn):
            commands[str(intent.role_id)]=intent.command
    return commands
