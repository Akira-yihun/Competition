"""Tunable strategy parameters.

Everything a reviewer might want to ablate lives here, so that a run can be
described by (config digest, code digest).  ``freeze()`` produces a stable
digest for build/report manifests.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Config:
    # --- 时间预算 (秒) -----------------------------------------------------
    request_seconds: float = 4.0      # 官方硬边界 5s；留 1s 余量给网络与序列化
    compute_seconds: float = 2.5      # 策略计算预算
    max_body_bytes: int = 8 * 1024 * 1024

    # --- 会话 -------------------------------------------------------------
    cached_rounds: int = 12
    max_sessions: int = 4

    # --- 寻路 -------------------------------------------------------------
    path_node_limit: int = 3000       # 单次 A* 扩展上限 (41x32=1312 格)
    path_soft_step_limit: int = 2     # 允许穿过机器人格的"软步"次数上限

    # --- 建造 -------------------------------------------------------------
    build_fail_limit: int = 2         # 同一格连续几次未生效后判非法
    wall_ring_enabled: bool = True
    wall_ring_limit: int = 24         # 围墙计划的最大格数
    stone_target: int = 8             # 计划囤积的石头数
    #: 塔的建造顺序 (v2)。三座火箭：射程 10/15/全图 + 八格溅射；
    #: 一级加特林/电磁射程只有 3/6，首夜火力差距明显。
    tower_loadout: tuple = ("rocket", "rocket", "rocket")
    towers_before_stone: bool = True  # 先建满三塔，再考虑围墙/囤石

    # --- 经济 -------------------------------------------------------------
    sell_batch_min: int = 5           # 触发批量售卖的最小同类矿石数
    gold_reserve: int = 0
    mineral_priority: tuple = ("copper", "iron", "stone")
    mine_lock: bool = True            # 跨回合锁定矿点，采完再换 (R4)
    mine_yield: int = 10              # 任务书 §4.1: 每矿采集 10 次后消失
    mine_sell_weight: float = 1.0     # 评分里"矿→小贩"距离的权重
    sell_trip_weight: float = 1.5     # 一批矿石至少值"每走一格 N 金币"才去卖

    # --- 采购 (R5) --------------------------------------------------------
    station_voucher_reserve: bool = True   # 基地券买来先存着，不急用
    station_emergency_hp: int = 100        # 基地血量低于此值必须用券
    station_standby_ratio: float = 0.6     # 低于该血量比时防守工人回基地待命
    station_predict_rounds: int = 2        # 预测未来几回合的攻击伤害

    # --- 防守 -------------------------------------------------------------
    return_margin: int = 2            # 返防提前量（回合）
    idle_defend_radius: int = 1       # 角色距塔多少格内视为可开火站位

    # --- 夜间避险 (R3) ----------------------------------------------------
    night_margin: int = 8             # 天黑前多少回合开始谨慎
    night_side_divisor: int = 3       # 安全区宽度 = width // 该值
    night_side_min: int = 2           # 安全区最小格数（贴边）
    night_robot_radius: int = 4       # 机器人周围多少格视为危险
    night_hold_tasks: bool = False    # 任务态开拓者夜间是否留在任务点

    # --- 战斗 -------------------------------------------------------------
    aim_k: int = 6                    # 选靶时纳入枚举的高价值目标数
    bomb_min_cluster: int = 3         # 3x3 机器人数达到该值才考虑 Bomb
    boss_dizzy_hp_ratio: float = 0.6  # BOSS/大型低于该血量比时考虑眩晕
    rocket_rear_bonus: float = 12.0   # 火箭落点向内侧后移一格的加分 (R2)
    rocket_rear_min_cluster: int = 3  # 后排成群的门槛
    rocket_urgent_radius: int = 3     # 最近敌人进入基地该距离时改为直击

    # --- 任务 -------------------------------------------------------------
    task_llm_max_per_instance: int = 4
    skill_min_success: int = 1

    # --- LLM --------------------------------------------------------------
    llm_calls_per_day: int = 3        # 接口文档 1.7
    llm_out_of_task_enabled: bool = True

    # --- 杂项 -------------------------------------------------------------
    max_commands: int = 16            # 角色数上限，防止异常报文导致响应膨胀
    config_note: str = "demo_ds-v2"

    # -- 派生量 ------------------------------------------------------------
    def night_side_limit(self, width: int) -> int:
        """Width of the safe band along each map edge (R3, 地图两侧)."""
        return max(self.night_side_min, width // max(1, self.night_side_divisor))


DEFAULT = Config()


def digest(cfg: Config = DEFAULT) -> str:
    blob = json.dumps(asdict(cfg), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
