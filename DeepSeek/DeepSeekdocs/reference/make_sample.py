#!/usr/bin/env python3
"""生成一份**真实加密**的样例对局日志（供 loop / 分析工具做端到端自测）。

    python3 reference/make_sample.py                 # 写到 ../samples/
    python3 reference/make_sample.py --check         # 只校验已提交样例仍可解（不覆盖）

产物：
    samples/sample-match.fwl.jsonl       # 13 帧密文（NDJSON）
    samples/sample-match.meta.json       # 解密上下文 + **演示密钥**（非真实密钥）
    samples/sample-match.anchor.json     # 终结锚（验证尾部截断用）
    samples/sample-match.readme.txt      # 如何解开它

设计要点（v1.1 修正）：
- **可复现**：演示密钥与前缀由固定公开种子派生，因此重复运行产生**逐字节相同**的样例，
  既可以当跨实现测试向量，也不会每次生成都产生全量 diff。
- 演示密钥**只用于样例**，与任何真实队伍密钥无关；`selfcheck.py` 会检查它被显式标注。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fwlog_crypto_ref import (  # noqa: E402
    ALG_HMAC, Writer, derive_keys,
)

MATCH_ID = "M-2026-DEMO-001"
TEAM_ID = "6324"
INSTANCE = "challenger-node-a"
HALF = 1
RUN_ID = "rdemo0001-00000000"          # 固定，保证可复现
DEMO_SEED = "fwlog-demo/1"             # 固定公开种子
BUILD_ID = "coregeek-demo+0000000"
TS = 1767225600000

# 事件序列：每个 ev 的字段都满足 schemas/fwlog.event.schema.json 的 required
EVENTS = [
    ("boot", 1, {"build": BUILD_ID, "side": "challenger",
                 "cfg_hash": "sha256:9f2c3a1b", "py": "3.11.9"}),
    ("cfg", 1, {"config": {"sell_batch": 60, "wall_gap_cells": 1, "return_margin": 12,
                           "decision_topk": 3, "tower_layout": [[9, 23], [10, 25], [11, 22]]}}),
    ("round", 1, {"phase": "day", "gold": 75, "score_total": 0, "station_hp": 1500,
                  "robots_n": 0, "decide_ms": 1.8, "cmds_n": 3, "errors_n": 0}),
    ("cmd", 1, {"cmds": [
        {"unit": 10010, "action": "move", "targetPos": [{"x": 9, "y": 22}]},
        {"unit": 10011, "action": "move", "targetPos": [{"x": 14, "y": 15}]},
        {"unit": 10012, "action": "collect", "targetPos": [{"x": 4, "y": 24}]}]}),
    ("ack", 2, {"results": {"10010": True, "10011": True, "10012": False},
                "treasure_result": 0, "new_errors": []}),
    ("guard", 2, {"rejected": [{"unit": 10012, "action": "collect",
                                "reason": "target_not_mine",
                                "detail": "mineral gone after respawn"}]}),
    ("decision", 71, {"dkind": "attack_target", "module": "combat.gatling",
                      "chosen": {"weapon": 10020, "controller": 10010,
                                 "aim": [[9, 28]], "ev": 30.0},
                      "cands": [{"aim": [[9, 28]], "ev": 30.0},
                                {"aim": [[8, 27]], "ev": 20.0},
                                {"aim": [[10, 27]], "ev": 10.0}],
                      "why": "cone_ok,first_hit_on_path,no_overkill"}),
    ("outcome", 72, {"dkind": "attack_target", "module": "combat.gatling",
                     "dmg": 30, "kills": 1, "miss": 0, "matched": True}),
    ("task", 20, {"task_point": 1, "task_type": "自进化类1", "phase": "accept",
                  "timeout": 30, "deadline": 50}),
    ("sandbox", 21, {"cmd_hash": "sha256:1a2b3c4d", "cmd_head": "python3 -c 'print(1)'",
                     "exit_code": 0, "out_head": "1", "truncated": False}),
    ("econ", 22, {"kind": "buy", "item": "AcientTablet", "num": 1,
                  "price": 15, "gold_before": 135, "gold_after": 120}),
    ("anomaly", 23, {"kind": "watchdog_timeout", "exc_type": "TimeoutError",
                     "tb_head": "decide() exceeded 3.0s", "fallback_used": True}),
    ("stat", 131, {"s1": 150, "s2": 42, "s3": 10, "total": 202, "kills": 17,
                   "tasks_done": 1, "tasks_failed": 0, "uptime_days": 1,
                   "base_alive": True}),
    ("bye", 131, {"reason": "half_finished", "rounds_seen": 130, "frames_written": 14}),
]
# 说明：bye 事件写在最后，其 frames_written 语义 = **包含 bye 自身在内的总帧数**（= len(EVENTS)）。


def demo_secret() -> bytes:
    """从固定公开种子派生演示密钥 ⇒ 样例可复现，且与真实密钥无关。"""
    return hashlib.sha256(DEMO_SEED.encode()).digest()


def demo_prefix() -> bytes:
    return hashlib.sha256((DEMO_SEED + "/nonce").encode()).digest()[:4]


def render() -> tuple[list[str], dict]:
    keys = derive_keys(demo_secret(), MATCH_ID, TEAM_ID, INSTANCE, HALF, RUN_ID,
                       alg=ALG_HMAC, build_id=BUILD_ID)
    writer = Writer(keys, match_id=MATCH_ID, team_id=TEAM_ID, instance_id=INSTANCE,
                    run_id=RUN_ID, half_no=HALF, nonce_prefix=demo_prefix(), now_ms=TS)
    lines = [writer.write(ev, rnd, **fields) for ev, rnd, fields in EVENTS]
    anchor = writer.anchor()
    meta = {
        "match_id": MATCH_ID, "team_id": TEAM_ID, "instance_id": INSTANCE,
        "half_no": HALF, "run_id": RUN_ID, "build_id": BUILD_ID, "alg": ALG_HMAC,
        "kid": keys["kid"], "bid": keys["bid"], "context": keys["context"],
        "frames": len(lines),
        "demo_secret_hex": demo_secret().hex(),
        "secret_is_demo": True,
        "warning": ("demo_secret_hex 由固定公开种子 sha256('fwlog-demo/1') 派生，"
                    "**仅用于演示与自测**；真实队伍密钥绝不可入库，也不得写进日志。"),
    }
    return lines, {"meta": meta, "anchor": anchor}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "samples"))
    ap.add_argument("--check", action="store_true",
                    help="只校验已提交样例，不重写文件")
    args = ap.parse_args()

    lines, extra = render()
    out = Path(args.out)
    log_path = out / "sample-match.fwl.jsonl"

    if args.check:
        existing = log_path.read_text(encoding="utf-8").strip().splitlines()
        same = existing == lines
        print(f"reproducible: {same}  (on disk {len(existing)} frames, rendered {len(lines)})")
        return 0 if same else 1

    out.mkdir(parents=True, exist_ok=True)
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out / "sample-match.meta.json").write_text(
        json.dumps(extra["meta"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "sample-match.anchor.json").write_text(
        json.dumps(extra["anchor"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "sample-match.readme.txt").write_text(
        "解开样例日志（相对 docs/DeepSeek/ 运行）：\n"
        "  python3 reference/fwlog_crypto_ref.py --verify samples/sample-match.fwl.jsonl \\\n"
        "      --secret-file samples/sample-match.meta.json \\\n"
        "      --anchor samples/sample-match.anchor.json\n"
        "  python3 reference/fwlog_crypto_ref.py --dump samples/sample-match.fwl.jsonl \\\n"
        "      --secret-file samples/sample-match.meta.json\n"
        "说明：--secret-file 接受两种格式——本样例的 meta.json（读 demo_secret_hex 字段）\n"
        "      或 secrets 目录下的 {\"secret_hex\": \"...\"} 文件。\n",
        encoding="utf-8")
    print(f"wrote {log_path} ({log_path.stat().st_size} bytes, {len(lines)} frames)")
    print(f"kid = {extra['meta']['kid']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
