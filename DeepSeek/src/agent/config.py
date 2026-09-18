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
    build_probe_per_turn: int = 1     # 每回合最多几个角色做建址探测
    wall_ring_enabled: bool = True
    wall_ring_limit: int = 24         # 围墙计划的最大格数
    wall_keep_entrance: bool = True   # 留一个气闸口供角色出入
    stone_target: int = 8             # 计划囤积的石头数

    # --- 经济 -------------------------------------------------------------
    sell_batch_min: int = 5           # 触发批量售卖的最小同类矿石数
    gold_reserve: int = 0
    upgrade_station_first: bool = False
    mineral_priority: tuple = ("copper", "iron", "stone")

    # --- 防守 -------------------------------------------------------------
    return_margin: int = 2            # 返防提前量（回合）
    emergency_hp_ratio: float = 0.35  # 基地血量低于该比例触发应急
    idle_defend_radius: int = 1       # 角色距塔多少格内视为可开火站位

    # --- 战斗 -------------------------------------------------------------
    aim_k: int = 6                    # 选靶时纳入枚举的高价值目标数
    bomb_min_cluster: int = 3         # 3x3 内机器人数达到该值才考虑 Bomb
    boss_dizzy_hp_ratio: float = 0.6  # BOSS/大型低于该血量比时考虑眩晕
    fire_empty_cells: bool = True     # 无可击杀目标时是否向合法空点开火
    rocket_focus_fire: bool = True    # 火箭多弹是否优先叠打

    # --- 任务 -------------------------------------------------------------
    task_leave_margin: int = 2        # 天黑前提前多少回合停接新任务
    task_llm_max_per_instance: int = 4
    task_exec_timeout_hint: float = 10.0
    skill_min_success: int = 1
    pioneer_help_defend: bool = False  # 任务态开拓者是否参与操炮（默认否）

    # --- LLM --------------------------------------------------------------
    llm_calls_per_day: int = 3        # 接口文档 1.7
    llm_out_of_task_enabled: bool = True

    # --- 杂项 -------------------------------------------------------------
    max_commands: int = 16            # 角色数上限，防止异常报文导致响应膨胀
    telemetry_ring: int = 256
    config_note: str = "demo_ds-v1"


DEFAULT = Config()


def digest(cfg: Config = DEFAULT) -> str:
    blob = json.dumps(asdict(cfg), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
