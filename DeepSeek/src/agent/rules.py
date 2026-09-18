"""Rule constants derived from 任务书 / 接口文档 / docs/request.txt.

Everything here is either quoted from an official document or measured from the
sample request.  Where the two disagree the more conservative value wins and the
disagreement is recorded in ``RANGE_MISMATCH`` so it can be surfaced as telemetry
instead of silently changing behaviour.
"""

# ---------------------------------------------------------------------------
# 时间 (任务书 4.2)
# ---------------------------------------------------------------------------
DAY_ROUNDS = 70
NIGHT_ROUNDS = 60
ROUNDS_PER_DAY = DAY_ROUNDS + NIGHT_ROUNDS
MAX_ROUNDS = 1300

# ---------------------------------------------------------------------------
# 角色 (任务书 4.5.2 / 接口文档 1.3.1)
# ---------------------------------------------------------------------------
STATION = "station"
WALL = "wall"
WORKER = "worker"
PIONEER = "pioneer"
GATLING = "gatling"
RAILGUN = "railgun"
ROCKET = "rocket"

TOWER_TYPES = (GATLING, RAILGUN, ROCKET)
MOBILE_TYPES = (WORKER, PIONEER)
ALLY_TYPES = MOBILE_TYPES + TOWER_TYPES + (STATION, WALL)

#: 只有开拓者能领取/作答/召唤宝藏 (任务书 4.4)
PIONEER_ONLY_ACTIONS = ("acceptTask", "submitAnswer", "summonTreasure")

# ---------------------------------------------------------------------------
# 机器人 (任务书 4.7.2)
# ---------------------------------------------------------------------------
SMALL_ROBOT = "smallRobot"
MIDDLE_ROBOT = "middleRobot"
LARGE_ROBOT = "largeRobot"
BOSS_ROBOT = "bossRobot"

ROBOT_STATS = {
    SMALL_ROBOT: {"hp": 40, "attack": 5, "range": 3, "score": 1},
    MIDDLE_ROBOT: {"hp": 60, "attack": 10, "range": 3, "score": 2},
    LARGE_ROBOT: {"hp": 500, "attack": 20, "range": 3, "score": 4},
    BOSS_ROBOT: {"hp": 800, "attack": 40, "range": 3, "score": 10},
}
DEFAULT_ROBOT = SMALL_ROBOT

# ---------------------------------------------------------------------------
# 建筑 (任务书 4.5.1)
# ---------------------------------------------------------------------------
BUILDING_HP = {
    STATION: (1500, 3000, 4500),
    GATLING: (1000, 1500, 2000),
    RAILGUN: (1000, 1500, 2000),
    ROCKET: (1000, 1500, 2000),
    WALL: (1000, 1500, 2000),
}

#: 任务书 4.5.1 表格给出的攻击距离（等级 1..3）
DOCUMENTED_RANGE = {
    GATLING: (3, 5, 7),
    RAILGUN: (6, 8, 10),
    ROCKET: (10, 15, None),  # 全图
}

WEAPON_BUILD_COST = 25
MAX_TOWERS = 3              # 任务书 4.5.1 补充说明: 武器工事全局同时最多 3 座
ROCKET_COOLDOWN = 3         # 火箭发射台发射后 3 回合冷却空窗
WALL_MATERIAL = "stone"

#: 建造名称。接口文档未给出枚举，样例 response.txt 用英文 "wall"，
#: 现有实现 (Demo/CoreGeek) 全部使用英文名，因此默认英文。
BUILD_NAME = {
    GATLING: "gatling",
    RAILGUN: "railgun",
    ROCKET: "rocket",
    WALL: "wall",
}

# ---------------------------------------------------------------------------
# 中立单位 (接口文档 1.2.1)
# ---------------------------------------------------------------------------
ZONE_STONE = "stone"
ZONE_IRON = "iron"
ZONE_COPPER = "copper"
ZONE_VENDOR = "vendor"
ZONE_WEAPON_SHOP = "weaponShop"
ZONE_TERRAIN = (ZONE_STONE, ZONE_IRON, ZONE_COPPER)
MINE_KINDS = (ZONE_STONE, ZONE_IRON, ZONE_COPPER)

TASK_POINT_PREFIX = {"challenger": "challenger", "defender": "defender"}
TASK_ZONE_TYPES = (
    "challengerTaskPoint1", "challengerTaskPoint2",
    "defenderTaskPoint1", "defenderTaskPoint2",
)

#: 背景地形值；接口文档中 zones 只列出中立元素，"land" 是我们对空地的内部表示
LAND = "land"

# ---------------------------------------------------------------------------
# 商店 (任务书 4.6.3, 与 docs/request.txt 的 weaponShopList 逐项一致)
# ---------------------------------------------------------------------------
VOUCHER_WEAPON_1 = "WeaponUpgradeVoucher1"
VOUCHER_WEAPON_2 = "WeaponUpgradeVoucher2"
VOUCHER_WALL_1 = "WallUpgradeVoucher1"
VOUCHER_WALL_2 = "WallUpgradeVoucher2"
VOUCHER_STATION_1 = "StationUpgradeVoucher1"
VOUCHER_STATION_2 = "StationUpgradeVoucher2"
WALL_FIXER = "WallFixer"
MEDICINE = "Medicine"
DIZZY_WEAPON = "DizzyWeapon"
BOMB = "Bomb"
SUMMON_SMALL = "SmallRobotSummonOrder"
SUMMON_MIDDLE = "MiddleRobotSummonOrder"
SUMMON_LARGE = "LargeRobotSummonOrder"
SUMMON_BOSS = "BossRobotSummonOrder"

TASK_ITEMS = (
    "AcientTablet", "StarSand", "FlameBreath",
    "FrostPotion", "ThornAmulet", "IronWhistle",
)

SHOP_PRICE = {
    VOUCHER_WEAPON_1: 100, VOUCHER_WEAPON_2: 150,
    VOUCHER_WALL_1: 20, VOUCHER_WALL_2: 30,
    VOUCHER_STATION_1: 100, VOUCHER_STATION_2: 150,
    WALL_FIXER: 10, MEDICINE: 10,
    DIZZY_WEAPON: 100, BOMB: 100,
    SUMMON_SMALL: 20, SUMMON_MIDDLE: 30,
    SUMMON_LARGE: 100, SUMMON_BOSS: 200,
    **{item: 15 for item in TASK_ITEMS},
}

#: 需要 targetPos 的 use 目标 (接口文档 2.3 注解 + 任务书 4.6.3)
USE_NEEDS_TARGET = (
    VOUCHER_WEAPON_1, VOUCHER_WEAPON_2, VOUCHER_WALL_1, VOUCHER_WALL_2,
    VOUCHER_STATION_1, VOUCHER_STATION_2, WALL_FIXER, DIZZY_WEAPON, BOMB,
)

#: 基准矿价；真实价格以报文 vendorShopList 为准 (任务书 4.6.1)
BASE_MINERAL_PRICE = {ZONE_STONE: 1, ZONE_IRON: 3, ZONE_COPPER: 5}

# ---------------------------------------------------------------------------
# 动作码 (接口文档 2.3)
# ---------------------------------------------------------------------------
ACTIONS = (
    "move", "attack", "sell", "buy", "build", "remove", "acceptTask",
    "submitAnswer", "summonTreasure", "use", "drop", "collect",
)
ACTIONS_NEEDING_TARGET = (
    "move", "attack", "build", "remove", "collect", "summonTreasure",
)
ACTIONS_NEEDING_NAME = ("build", "sell", "buy", "use", "drop")

#: 仅夜晚可用 / 仅白天可用
NIGHT_ONLY_ACTIONS = ("attack",)
DAY_ONLY_ACTIONS = ("build",)
WORKER_ONLY_ACTIONS = ("build", "remove", "collect")

# ---------------------------------------------------------------------------
# 积分 (任务书 六)
# ---------------------------------------------------------------------------
SPEED_BONUS_FACTOR = 5      # score_1 速度项系数
SURVIVAL_PER_DAY = 10       # score_3 = Σ 10 * day * 存活系数

# ---------------------------------------------------------------------------
# 报文中的哨兵值
# ---------------------------------------------------------------------------
#: 样例里火箭的 attackRange 是 INT32_MAX，代表"全图"。绝不可当普通数值参与算术。
RANGE_SENTINEL = 2 ** 31 - 1
