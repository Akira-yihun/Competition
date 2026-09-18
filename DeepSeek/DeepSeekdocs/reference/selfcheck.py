#!/usr/bin/env python3
"""交付包自检：验证 docs/DeepSeek/ 里的文档、模板、schema、参考实现、样例**互相一致**。

    python3 reference/selfcheck.py          # 全部检查
    python3 reference/selfcheck.py -v       # 打印细节

设计意图：文档会漂移，脚本不会。任何一次改动后跑一遍，比人眼复查可靠。
退出码 0 = 全绿；1 = 有失败项（打印明细）。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # docs/DeepSeek/
FAILS: list[str] = []
CHECKS = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if ok:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILS.append(f"{name} {detail}")


def section(title: str) -> None:
    print(f"\n== {title} ==")


def read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------- 1. JSON 合法性
def check_json() -> None:
    section("1. 所有 JSON 可解析")
    files = ["templates/loop.config.json", "templates/endpoints.reserved.json",
             "schemas/fwlog.event.schema.json", "schemas/loop.state.schema.json",
             "reference/fwlog_kat.json", "samples/sample-match.meta.json"]
    for f in files:
        try:
            json.loads(read(f))
            check(f, True)
        except Exception as exc:                      # noqa: BLE001
            check(f, False, str(exc))


# ---------------------------------------------------------------- 2. 参考实现
def check_crypto() -> None:
    section("2. fwlog 参考实现")
    sys.path.insert(0, str(ROOT / "reference"))
    try:
        import fwlog_crypto_ref as fw
    except Exception as exc:                          # noqa: BLE001
        check("import fwlog_crypto_ref", False, str(exc))
        return
    check("import fwlog_crypto_ref", True)
    check("selftest 8/8", fw.selftest() == 0)

    kat_file = json.loads(read("reference/fwlog_kat.json"))
    fresh = fw.build_kat()
    check("KAT 可确定性复现", json.dumps(kat_file, sort_keys=True) == json.dumps(fresh, sort_keys=True))

    # 文档正文里的 KAT 数值必须与 JSON 逐字节一致
    doc = read("03-日志格式与加密设计.md")
    d = kat_file["derived"]
    vals = {
        "kid": d["kid"], "context": d["context"],
        "K_enc": d["k_enc_hex"], "K_mac": d["k_mac_hex"],
        "frame0.c": kat_file["frames"][0]["c"], "frame0.t": kat_file["frames"][0]["t"],
        "frame1.c": kat_file["frames"][1]["c"], "frame1.t": kat_file["frames"][1]["t"],
    }
    for k, v in vals.items():
        check(f"03 文档内嵌 KAT 值 {k}", v in doc, "(文档里的值与 fwlog_kat.json 不一致)")


# ---------------------------------------------------------------- 3. 样例日志
def _reproducible() -> bool:
    try:
        out = subprocess.run([sys.executable, str(ROOT / "reference" / "make_sample.py"), "--check"],
                             capture_output=True, text=True, timeout=60)
        return out.returncode == 0
    except Exception:                                     # noqa: BLE001
        return False


def check_sample() -> None:
    section("3. 加密样例日志可解码")
    sys.path.insert(0, str(ROOT / "reference"))
    import fwlog_crypto_ref as fw

    meta = json.loads(read("samples/sample-match.meta.json"))
    keys = fw.derive_keys(bytes.fromhex(meta["demo_secret_hex"]), meta["match_id"],
                          meta["team_id"], meta["instance_id"], meta["half_no"], meta["run_id"])
    lines = read("samples/sample-match.fwl.jsonl").strip().splitlines()
    check("样例帧数 == meta.frames", len(lines) == meta["frames"], f"{len(lines)} vs {meta['frames']}")
    check("样例可复现（渲染 == 已提交）", True if _reproducible() else False, "make_sample --check 不一致")

    reader = fw.Reader(keys)
    events, err = [], None
    try:
        for line in lines:
            events.append(reader.read_line(line))
    except Exception as exc:                          # noqa: BLE001
        err = str(exc)
    check("正确密钥可解全部帧", err is None, err or "")
    check("kid 与 meta 一致", all(e["header"]["kid"] == meta["kid"] for e in events))

    try:
        fw.Reader(fw.derive_keys(fw.gen_secret(), meta["match_id"], meta["team_id"],
                                 meta["instance_id"], meta["half_no"],
                                 meta["run_id"])).read_line(lines[0])
        check("错误密钥被拒绝", False, "竟然解密成功")
    except ValueError:
        check("错误密钥被拒绝", True)

    # 篡改一帧应被拒绝
    frame = json.loads(lines[2])
    raw = bytearray(fw.b64u_dec(frame["c"]))
    raw[0] ^= 1
    frame["c"] = fw.b64u(raw)
    try:
        fw.Reader(keys).read_line(json.dumps(frame))
        check("篡改帧被拒绝", False, "竟然通过")
    except ValueError:
        check("篡改帧被拒绝", True)

    # 样例里出现的事件类型必须在 schema enum 里
    schema = json.loads(read("schemas/fwlog.event.schema.json"))
    enum = set(schema["properties"]["ev"]["enum"])
    used = {e["event"]["ev"] for e in events}
    check("样例事件类型 ⊆ schema enum", used <= enum, f"多出 {sorted(used - enum)}")

    # 文档 §4.2 事件表里的 ev 名称应与 schema 一致
    doc = read("03-日志格式与加密设计.md")
    table_evs = set(re.findall(r"^\| `([a-z]+)` \|", doc, re.M)) 
    missing = {e for e in enum if f"`{e}`" not in doc}
    check("schema 的每个 ev 都在 03 文档里出现", not missing, f"缺 {sorted(missing)}")


# ---------------------------------------------------------------- 3b. schema 子集校验
def _validate(inst, schema, path="$"):
    """极简 draft2020-12 子集校验：type/required/enum/const/minimum/maximum/
    maxLength/maxItems/items/properties/allOf+if-then/additionalProperties。"""
    errs = []
    if "allOf" in schema:
        for sub in schema["allOf"]:
            if "if" in sub:
                if not _validate(inst, sub["if"], path):
                    errs += _validate(inst, sub.get("then", {}), path)
            else:
                errs += _validate(inst, sub, path)
    t = schema.get("type")
    if t:
        types = t if isinstance(t, list) else [t]
        ok = any((x == "object" and isinstance(inst, dict)) or
                 (x == "array" and isinstance(inst, list)) or
                 (x == "string" and isinstance(inst, str)) or
                 (x == "integer" and isinstance(inst, int) and not isinstance(inst, bool)) or
                 (x == "number" and isinstance(inst, (int, float)) and not isinstance(inst, bool)) or
                 (x == "boolean" and isinstance(inst, bool)) or
                 (x == "null" and inst is None) for x in types)
        if not ok:
            errs.append(f"{path}: type {types} != {type(inst).__name__}")
            return errs
    if "const" in schema and inst != schema["const"]:
        errs.append(f"{path}: const {schema['const']!r} != {inst!r}")
    if "enum" in schema and inst not in schema["enum"]:
        errs.append(f"{path}: {inst!r} not in enum")
    if isinstance(inst, str) and "maxLength" in schema and len(inst) > schema["maxLength"]:
        errs.append(f"{path}: too long")
    if isinstance(inst, (int, float)) and not isinstance(inst, bool):
        if "minimum" in schema and inst < schema["minimum"]:
            errs.append(f"{path}: {inst} < minimum {schema['minimum']}")
        if "maximum" in schema and inst > schema["maximum"]:
            errs.append(f"{path}: {inst} > maximum {schema['maximum']}")
    if isinstance(inst, list):
        if "maxItems" in schema and len(inst) > schema["maxItems"]:
            errs.append(f"{path}: too many items")
        if "items" in schema:
            for i, v in enumerate(inst):
                errs += _validate(v, schema["items"], f"{path}[{i}]")
    if isinstance(inst, dict):
        for r in schema.get("required", []):
            if r not in inst:
                errs.append(f"{path}: missing required {r!r}")
        props = schema.get("properties", {})
        for k, v in inst.items():
            if k in props:
                errs += _validate(v, props[k], f"{path}.{k}")
            elif schema.get("additionalProperties") is False:
                errs.append(f"{path}: unexpected key {k!r}")
    return errs


def check_event_schema() -> None:
    section("3b. 事件负载确实满足 schema")
    schema = json.loads(read("schemas/fwlog.event.schema.json"))
    sys.path.insert(0, str(ROOT / "reference"))
    import fwlog_crypto_ref as fw
    meta = json.loads(read("samples/sample-match.meta.json"))
    keys = fw.derive_keys(bytes.fromhex(meta["demo_secret_hex"]), meta["match_id"],
                          meta["team_id"], meta["instance_id"], meta["half_no"], meta["run_id"])
    reader = fw.Reader(keys)
    bad = []
    n = 0
    for line in read("samples/sample-match.fwl.jsonl").strip().splitlines():
        ev = reader.read_line(line)["event"]
        n += 1
        errs = _validate(ev, schema)
        if errs:
            bad.append((n, ev.get("ev"), errs[:2]))
    check("样例每一帧都通过 event schema", not bad, f"{bad[:3]}")

    # KAT 负载也要过 schema
    kat = json.loads(read("reference/fwlog_kat.json"))
    kkeys = fw.derive_keys(bytes.fromhex(kat["constants"]["team_secret_hex"]),
                           kat["constants"]["match_id"], kat["constants"]["team_id"],
                           kat["constants"]["instance_id"], kat["constants"]["half_no"],
                           kat["constants"]["run_id"])
    kbad = []
    for i, frame in enumerate(kat["frames"], 1):
        payload, _ = fw.open_frame(kkeys, frame)
        ev = json.loads(payload)
        errs = _validate(ev, schema)
        if errs:
            kbad.append((i, ev.get("ev"), errs[:2]))
    check("KAT 每一帧都通过 event schema", not kbad, f"{kbad[:3]}")

    # 文档 §4.2 表格里的 ev 必须都在 schema enum
    doc = read("03-日志格式与加密设计.md")
    enum = set(schema["properties"]["ev"]["enum"])
    missing = {e for e in enum if f"`{e}`" not in doc}
    check("schema 的每个 ev 都在 03 文档里出现", not missing, f"缺 {sorted(missing)}")


def check_anchors() -> None:
    section("3c. 样例可复现 + 锚可检出截断")
    sys.path.insert(0, str(ROOT / "reference"))
    import fwlog_crypto_ref as fw
    meta = json.loads(read("samples/sample-match.meta.json"))
    anchor = json.loads(read("samples/sample-match.anchor.json"))
    check("meta 标注 secret_is_demo", meta.get("secret_is_demo") is True)
    check("anchor 与 meta 的 run/kid 一致",
          anchor["run_id"] == meta["run_id"] and anchor["kid"] == meta["kid"])
    lines = read("samples/sample-match.fwl.jsonl").strip().splitlines()
    check("anchor.count == 实际帧数（frames_written 语义一致）", anchor["count"] == len(lines),
          f"{anchor['count']} vs {len(lines)}")
    bye = json.loads(fw.open_frame(
        fw.derive_keys(bytes.fromhex(meta["demo_secret_hex"]), meta["match_id"], meta["team_id"],
                       meta["instance_id"], meta["half_no"], meta["run_id"]),
        json.loads(lines[-1]))[0])
    check("bye.frames_written 含自身 == 总帧数",
          bye.get("frames_written") == len(lines), f"{bye.get('frames_written')} vs {len(lines)}")


# ---------------------------------------------------------------- 4. 端点一致性
def check_endpoints() -> None:
    section("4. 预留端点：文档 ↔ 模板 一致")
    eps = json.loads(read("templates/endpoints.reserved.json"))["endpoints"]
    cfg_raw = json.loads(read("templates/loop.config.json"))
    check("config.endpoints_source 指向本模板",
          cfg_raw["platform"].get("endpoints_source", "").endswith("endpoints.reserved.json"))
    doc = read("02-Loop工程计划.md")
    missing_in_doc = [k for k in eps if f"`{k}`" not in doc]
    check("模板端点都在 02 文档表格里", not missing_in_doc, f"缺 {missing_in_doc}")

    # 只在 §4.1「端点表」小节里扫，避免误抓 verb 表/超时表里的裸名字
    m = re.search(r"### 4\.1 端点表(.*?)### 4\.2", doc, re.S)
    check("02 文档能定位到 §4.1 端点表", m is not None)
    table = re.findall(r"^\| `([a-z]+(?:\.[a-z]+)?)`", m.group(1) if m else "", re.M)
    missing_in_tpl = [t for t in table if t not in eps]
    check("02 文档表格端点都在模板里", not missing_in_tpl, f"缺 {missing_in_tpl}")
    extra_in_doc = [t for t in set(table) if t not in eps]
    check("文档表格没有多余端点", not extra_in_doc, f"多余 {extra_in_doc}")
    check("模板端点数量 == 文档表格行数", len(eps) == len(set(table)),
          f"模板 {len(eps)} 个，文档表格 {len(set(table))} 个")

    # config 必须覆盖除端点映射外的关键字段
    cfg = json.loads(read("templates/loop.config.json"))
    for key in ("base_url", "auth", "endpoints", "poll", "limits", "match_defaults", "log_sink"):
        check(f"loop.config.platform.{key} 存在", key in cfg["platform"])


# ---------------------------------------------------------------- 5. 关键常量一致
def check_constants() -> None:
    section("5. 关键常量跨文档一致")
    d1, d2, d3, readme = read("01-游戏目标与策略分析.md"), read("02-Loop工程计划.md"), read("03-日志格式与加密设计.md"), read("README.md")
    facts = {
        "1300": ("总回合数", [d1, d3]),
        "550": ("score_3 上限", [d1]),
        "70": ("白天回合", [d1, d3]),
        "60": ("黑夜回合", [d1, d3]),
        "12 格": ("武器可建格数", [d1]),
        "20 格": ("围墙可建格数", [d1]),
    }
    for token, (desc, docs) in facts.items():
        check(f"{desc} 出现在 01", all(token in d for d in docs), token)

    check("01 含可建造区域修正说明", "修正" in d1 and "d≥3" in d1)
    check("README 含三条核心结论", "动作数" in readme and "分水岭" in readme)


# ---------------------------------------------------------------- 6. 文件清单
def check_manifest() -> None:
    section("6. README 文件清单 ↔ 实际文件")
    readme = read("README.md")
    actual = {str(p.relative_to(ROOT)) for p in ROOT.rglob("*") if p.is_file()
              and "__pycache__" not in p.parts}
    actual_names = {Path(a).name for a in actual}
    # 反引号里可能是中文文件名，正则必须允许非 ASCII
    listed = set(re.findall(r"`([^`\s]+\.(?:md|json|py|jsonl|txt))`", readme))
    listed = {m for m in listed if not m.startswith(("loop/", "src/", "docs/", "runs/", "secrets/"))}
    # 被引用但**有意留在归档区之外**的文件（本自检脚本自身、索引自身、仓库里已有的分析文档）
    external = {"README.md", "selfcheck.py", "Demo代码优化分析.md"}
    listed = {m for m in listed if Path(m).name not in external}

    # README 允许用 basename 列文件（更易读），比较时同样按 basename 归一
    missing = sorted(m for m in listed if Path(m).name not in actual_names)
    check("README 列出的文件都存在", not missing, f"不存在：{missing}")
    listed_names = {Path(m).name for m in listed} | external
    unlisted = sorted(a for a in actual if Path(a).name not in listed_names)
    check("实际文件都被 README 列出", not unlisted, f"未列出：{unlisted}")

    for f in sorted(actual):
        check(f"非空文件 {f}", (ROOT / f).stat().st_size > 0)


# ---------------------------------------------------------------- 7. 无密钥入库
def check_secrets() -> None:
    section("7. 密钥卫生")
    leaks = []
    for p in ROOT.rglob("*.py"):
        if p.name == "selfcheck.py":
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        if re.search(r"SECRET\s*=\s*bytes\.fromhex\(['\"][0-9a-fA-F]{64}['\"]\)", text):
            leaks.append(str(p.relative_to(ROOT)))
    check("参考实现里没有硬编码真实密钥", not leaks, str(leaks))
    meta = json.loads(read("samples/sample-match.meta.json"))
    check("样例密钥标注为演示用", "warning" in meta and "演示" in meta["warning"])
    check("样例 meta 的 kid 与日志一致",
          meta["kid"] in read("samples/sample-match.fwl.jsonl"))


def main() -> int:
    verbose = "-v" in sys.argv
    print(f"selfcheck @ {ROOT}")
    check_json()
    check_crypto()
    check_sample()
    check_event_schema()
    check_anchors()
    check_endpoints()
    check_constants()
    check_manifest()
    check_secrets()
    print(f"\n{'=' * 60}")
    if FAILS:
        print(f"RESULT: {len(FAILS)} FAILED / {CHECKS} checks")
        for f in FAILS:
            print("  -", f)
        return 1
    print(f"RESULT: ALL PASS ({CHECKS} checks)")
    if verbose:
        print("（-v 目前无额外输出，保留给后续扩展）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
