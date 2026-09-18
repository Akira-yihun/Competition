"""Textual v1 rules; unresolved physics belongs to simulator profiles."""
DAY_ROUNDS = 70
NIGHT_ROUNDS = 60
ROUNDS_PER_DAY = DAY_ROUNDS + NIGHT_ROUNDS

WEAPON_BUILD_COST = 25
WALL_MATERIAL = "stone"
LAND = "land"
STATION = "station"
WALL = "wall"
WORKER = "worker"
PIONEER = "pioneer"
TOWER_TYPES = ("gatling", "railgun", "rocket")
CONTROLLABLE_TYPES = (WORKER, PIONEER)
TOWER_RANGE_BY_LEVEL = {
    "gatling": (3, 5, 7),
    "railgun": (6, 8, 10),
    "rocket": (10, 15, 10**9),
}



SHOP_PRICES = {
    'WeaponUpgradeVoucher1':100, 'WeaponUpgradeVoucher2':150,
    'StationUpgradeVoucher1':100, 'StationUpgradeVoucher2':150,
    'WallUpgradeVoucher1':20, 'WallUpgradeVoucher2':30,
    'Medicine':10, 'WallFixer':10,
}
