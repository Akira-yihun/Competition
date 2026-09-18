#!/usr/bin/env python3
"""fwlog v1.1 — reference implementation (SPEC, 不参与正式提交的 src/ 代码树)

用途
----
1. 作为 `03-日志格式与加密设计.md` 的**可执行规范**：字节级行为以本文件为准。
2. 提供可直接使用的子命令（不再只是"纸面契约"）：
       --kat        生成已知答案向量（KAT）
       --selftest   14 项自测（含对抗用例）
       --bench      性能基准
       --verify     校验日志文件（MAC + 内容链 + 序号连续性 + 终结锚）
       --dump       解密并筛选输出（--ev / --round / --raw）
3. 供实现者照抄进 `src/agent/fwlog.py`。

设计约束
--------
- **仅标准库**（本机实测 cryptography / pycryptodome 都不存在，这不是假设）。
- 缺省算法 `hmac-aead-v1`：HKDF-SHA256 派生密钥 + HMAC-SHA256 计数器密钥流 + Encrypt-then-MAC。
- 若环境有 AES-GCM 可切 `aes-256-gcm`：帧格式一致，**但密钥材料按 alg 分离**（避免跨算法复用）。
- 双方队伍共用同一接口/同一份代码，各持不同 `team_secret`，因此互不可解。

v1.1 相对 v1.0 的关键修正（来自对抗式评审的实测复现）
----------------------------------------------------
C1 链改为**内容绑定**：chain_i 依赖 (chain_{i-1}, seq, len(aad), aad, ct)，因此跨运行拼接必然断裂。
C2 引入 **run_id**：参与 HKDF 上下文、chain_0、文件名与幂等键 ⇒ 崩溃重跑 / 平台重判不会互相污染。
C3 **终结锚（anchor）**：文件末尾帧承诺 (run_id, count, first_chain, last_chain)，
   并可导出到平台回执 ⇒ 尾部截断可被发现（无锚则不可，文档已如实说明）。
C4 去掉 nonce 前缀的默认值（全零前缀会造成 two-time pad）；前缀必填或自动随机。
C5 alg 参与 HKDF info ⇒ 算法与密钥绑定。
C6 base64 强制规范编码（否则同一帧有多种字节表示，内容寻址失效）。
C7 `kid` 语义修正为**上下文指纹**（不是 build 指纹）；另加 `bid`（build 指纹）供打包注入排障。
C8 显式校验 nonce/tag/chain 的长度；未知算法抛干净的 ValueError（不再漏 ModuleNotFoundError）。
C9 HKDF 长度上界修正为 RFC 5869 的 255×HashLen。
C10 payload 大对象（blob）也走同一加密帧，**不再有明文快照**。
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import hmac
import json
import os
import struct
import sys
import time

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------
FRAME_MAGIC = "FWL1"
FRAME_SCHEMA = "fwlog.frame/1"
EVENT_SCHEMA = "fwlog.event/1"
ANCHOR_SCHEMA = "fwlog.anchor/1"
ALG_HMAC = "hmac-aead-v1"
ALG_AESGCM = "aes-256-gcm"
KNOWN_ALGS = (ALG_HMAC, ALG_AESGCM)

_KID_LABEL = b"KID"
_BID_LABEL = b"BUILD"
_KS_LABEL = b"KS"
_MAC_LABEL = b"MAC"
_CHAIN_LABEL = b"CHAIN"
_CHAIN0_LABEL = b"CHAIN0"
_KDF_PREFIX = b"fwlog/v1 aead|"

TAG_LEN = 16          # 帧尾 MAC 截断长度
CHAIN_LEN = 16        # 链值长度
NONCE_LEN = 12
SECRET_LEN = 32


# --------------------------------------------------------------------------
# 基础原语
# --------------------------------------------------------------------------
def b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def b64u_dec(text: str, *, strict: bool = True) -> bytes:
    """严格 base64url 解码：拒绝标准字母表、拒绝非规范补位、拒绝空白。"""
    if not isinstance(text, str):
        raise ValueError("base64 field must be a string")
    if strict:
        if any(c.isspace() for c in text):
            raise ValueError("base64 field contains whitespace")
        if "+" in text or "/" in text:
            raise ValueError("base64 field is not urlsafe-alphabet")
        if "=" in text:
            raise ValueError("base64 field must be unpadded")
    raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    if strict and b64u(raw) != text:          # 规范编码检查（防同一内容多种表示）
        raise ValueError("base64 field is not canonical")
    return raw


def hkdf_sha256(ikm: bytes, salt: bytes, info: bytes, length: int) -> bytes:
    """RFC 5869 HKDF-SHA256（Extract + Expand）。长度上界 = 255 × HashLen。"""
    hash_len = hashlib.sha256().digest_size
    if length > 255 * hash_len:
        raise ValueError("hkdf: requested length too large")
    if not salt:
        salt = b"\x00" * hash_len
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    okm = bytearray()
    block = b""
    counter = 1
    while len(okm) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        okm += block
        counter += 1
    return bytes(okm[:length])


def canonical(obj: object) -> bytes:
    """帧头的规范化序列化：键排序、无空格、ASCII。**AAD 就是这段字节。**"""
    text = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return text.encode("ascii")


def gen_secret() -> bytes:
    return os.urandom(SECRET_LEN)


def new_run_id() -> str:
    """运行实例 id：时间戳 + 随机。参与密钥派生、链起点、文件名与幂等键。"""
    return f"r{int(time.time()):x}-{os.urandom(4).hex()}"


# --------------------------------------------------------------------------
# 密钥派生（C2 上下文含 run_id；C5 alg 参与 info；C7 kid/bid 语义分离）
# --------------------------------------------------------------------------
def derive_context(match_id: str, team_id: str, instance_id: str, half_no: int,
                   run_id: str) -> str:
    return f"fwlog/v1|{match_id}|{team_id}|{instance_id}|{half_no}|{run_id}"


def derive_keys(team_secret: bytes, match_id: str, team_id: str, instance_id: str,
                half_no: int, run_id: str, *, alg: str = ALG_HMAC,
                build_id: str = "") -> dict:
    if len(team_secret) != SECRET_LEN:
        raise ValueError(f"team_secret must be {SECRET_LEN} bytes")
    if alg not in KNOWN_ALGS:
        raise ValueError(f"unknown alg: {alg!r}")
    if not run_id:
        raise ValueError("run_id is required (prevents cross-run key/chain reuse)")
    ctx = derive_context(match_id, team_id, instance_id, half_no, run_id)
    salt = hashlib.sha256(ctx.encode("utf-8")).digest()
    okm = hkdf_sha256(team_secret, salt, _KDF_PREFIX + alg.encode("ascii"), 64)
    k_enc, k_mac = okm[:32], okm[32:64]
    return {
        "k_enc": k_enc, "k_mac": k_mac,
        "kid": hmac.new(k_mac, _KID_LABEL, hashlib.sha256).digest()[:8].hex(),
        "bid": (hmac.new(team_secret, _BID_LABEL + build_id.encode("utf-8"),
                         hashlib.sha256).digest()[:8].hex() if build_id else ""),
        "context": ctx, "alg": alg, "run_id": run_id,
    }


# --------------------------------------------------------------------------
# nonce（C4：无默认前缀；前缀必填或自动随机）
# --------------------------------------------------------------------------
def nonce_for(seq: int, prefix: bytes) -> bytes:
    """12 字节 nonce = 4 字节运行前缀 || 8 字节大端计数器（从 1 开始）。**prefix 必填。**"""
    if len(prefix) != 4:
        raise ValueError("nonce prefix must be exactly 4 bytes")
    if not 1 <= seq < 2 ** 64:
        raise ValueError("seq out of range")
    return prefix + struct.pack(">Q", seq)


# --------------------------------------------------------------------------
# 帧封装（C1 内容绑定链 / C3 终结锚 / C8 长度校验）
# --------------------------------------------------------------------------
def chain_start(k_mac: bytes, run_id: str) -> bytes:
    """链起点绑定 run_id ⇒ 不同运行的链序列必然不同。"""
    return hmac.new(k_mac, _CHAIN0_LABEL + run_id.encode("utf-8"),
                    hashlib.sha256).digest()[:CHAIN_LEN]


def chain_next(k_mac: bytes, prev: bytes, seq: int, aad: bytes, ct: bytes) -> bytes:
    """**内容绑定**：链值依赖序号、头部字节与密文 ⇒ 跨运行/跨段拼接必然断裂。"""
    return hmac.new(
        k_mac,
        _CHAIN_LABEL + prev + struct.pack(">Q", seq) + struct.pack(">Q", len(aad)) + aad + ct,
        hashlib.sha256,
    ).digest()[:CHAIN_LEN]


def _keystream(k_enc: bytes, nonce: bytes, length: int) -> bytes:
    """HMAC-SHA256 作 PRF 的计数器密钥流。单 nonce 上限 2^32 块 = 128 GiB。"""
    blocks = (length + 31) // 32
    if blocks > 2 ** 32:
        raise ValueError("payload too large for a single nonce")
    out = bytearray()
    for i in range(blocks):
        out += hmac.new(k_enc, _KS_LABEL + nonce + struct.pack(">I", i), hashlib.sha256).digest()
    return bytes(out[:length])


def make_header(keys: dict, seq: int, prev_chain: bytes, *, match_id: str, team_id: str,
                instance_id: str, run_id: str, half_no: int, round_no: int, event: str,
                level: str = "info", nonce: bytes, ts_ms: int | None = None) -> dict:
    """`prev_chain` = 上一帧的链值（第 1 帧用 chain_start）。帧头 `chain` 字段存的就是它；
    本帧产生的新链值放在帧尾字段 `x`，由 seal() 计算（内容绑定，避免循环依赖）。"""
    return {
        "magic": FRAME_MAGIC, "v": 1, "alg": keys["alg"], "kid": keys["kid"],
        "mid": match_id, "tid": team_id, "inst": instance_id, "run": run_id,
        "half": half_no, "seq": seq, "round": round_no,
        "ts": ts_ms if ts_ms is not None else int(time.time() * 1000),
        "ev": event, "lvl": level,
        "n": b64u(nonce), "chain": b64u(prev_chain),
    }


def seal(keys: dict, header: dict, payload: bytes) -> dict:
    """Encrypt-then-MAC。返回可直接 json.dumps 的帧对象。"""
    aad = canonical(header)
    nonce = b64u_dec(header["n"])
    if len(nonce) != NONCE_LEN:
        raise ValueError("nonce must be 12 bytes")
    if header.get("alg") != keys["alg"]:
        raise ValueError("header alg does not match key material (cross-algorithm reuse)")
    if header["alg"] == ALG_AESGCM:
        ct, tag = _aesgcm_encrypt(keys["k_enc"], nonce, aad, payload)
    else:
        ks = _keystream(keys["k_enc"], nonce, len(payload))
        ct = bytes(a ^ b for a, b in zip(payload, ks))
        tag = hmac.new(keys["k_mac"],
                       _MAC_LABEL + struct.pack(">I", len(aad)) + aad + nonce + ct,
                       hashlib.sha256).digest()[:TAG_LEN]
    next_chain = chain_next(keys["k_mac"], b64u_dec(header["chain"]),
                            int(header["seq"]), aad, ct)
    return {"h": header, "c": b64u(ct), "t": b64u(tag), "x": b64u(next_chain)}


def open_frame(keys: dict, frame: dict, *, prev_chain: bytes | None = None) -> tuple[bytes, bytes]:
    """校验并解密一帧。返回 (payload, 本帧链值)。任何失败都抛 ValueError。

    校验顺序：格式 → 算法/kid → 内容链 → MAC → 解密。
    kid 先于 chain，是为了在"用错密钥"时给出正确的诊断（而不是误报成被篡改）。
    """
    if not isinstance(frame, dict) or not {"h", "c", "t", "x"} <= set(frame):
        raise ValueError("malformed frame")
    header = frame["h"]
    if header.get("magic") != FRAME_MAGIC:
        raise ValueError("bad magic")
    alg = header.get("alg")
    if alg not in KNOWN_ALGS:
        raise ValueError(f"unknown alg in frame: {alg!r}")
    if alg != keys["alg"]:
        raise ValueError("frame alg does not match key material")
    if alg == ALG_AESGCM and not _aesgcm_available():
        raise ValueError("aes-256-gcm frame but no AES-GCM provider is available")
    if header.get("kid") != keys["kid"]:
        raise ValueError("kid mismatch (wrong team/build secret, or wrong run/context)")

    aad = canonical(header)
    nonce = b64u_dec(header["n"])
    ct = b64u_dec(frame["c"])
    tag = b64u_dec(frame["t"])
    prev_link = b64u_dec(header["chain"])
    next_link = b64u_dec(frame["x"])
    if len(nonce) != NONCE_LEN:
        raise ValueError("nonce must be 12 bytes")
    if len(tag) != TAG_LEN:
        raise ValueError("tag must be 16 bytes")
    if len(prev_link) != CHAIN_LEN:
        raise ValueError("chain must be 16 bytes")
    if len(next_link) != CHAIN_LEN:
        raise ValueError("chain-link must be 16 bytes")

    if prev_chain is not None:
        if not hmac.compare_digest(prev_link, prev_chain):
            raise ValueError("chain-link mismatch (frame does not follow the previous one)")
        expected_next = chain_next(keys["k_mac"], prev_chain, int(header["seq"]), aad, ct)
        if not hmac.compare_digest(next_link, expected_next):
            raise ValueError("chain mismatch (reordered/spliced/tampered frame)")

    if alg == ALG_AESGCM:
        payload = _aesgcm_decrypt(keys["k_enc"], nonce, aad, ct, tag)
    else:
        expected = hmac.new(keys["k_mac"],
                            _MAC_LABEL + struct.pack(">I", len(aad)) + aad + nonce + ct,
                            hashlib.sha256).digest()[:TAG_LEN]
        if not hmac.compare_digest(expected, tag):
            raise ValueError("MAC verification failed")
        ks = _keystream(keys["k_enc"], nonce, len(ct))
        payload = bytes(a ^ b for a, b in zip(ct, ks))
    return payload, next_link


def _aesgcm_available() -> bool:
    try:
        import cryptography.hazmat.primitives.ciphers.aead  # noqa: F401
        return True
    except ImportError:
        pass
    try:
        import Crypto.Cipher.AES  # noqa: F401
        return True
    except ImportError:
        return False


def _aesgcm_encrypt(key, nonce, aad, payload):        # pragma: no cover - 本环境无库
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        blob = AESGCM(key).encrypt(nonce, payload, aad)
        return blob[:-16], blob[-16:]
    except ImportError:
        from Crypto.Cipher import AES
        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
        cipher.update(aad)
        return cipher.encrypt_and_digest(payload)


def _aesgcm_decrypt(key, nonce, aad, ct, tag):        # pragma: no cover - 本环境无库
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        return AESGCM(key).decrypt(nonce, ct + tag, aad)
    except ImportError:
        from Crypto.Cipher import AES
        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
        cipher.update(aad)
        return cipher.decrypt_and_verify(ct, tag)


# --------------------------------------------------------------------------
# 事件负载
# --------------------------------------------------------------------------
def make_event(event: str, round_no: int, **fields) -> bytes:
    body = {"schema": EVENT_SCHEMA, "ev": event, "round": round_no}
    body.update(fields)
    return canonical(body)


# --------------------------------------------------------------------------
# Writer / Reader
# --------------------------------------------------------------------------
class Writer:
    """顺序封装帧。**线程不安全**（调用方加锁或用单写线程）。"""

    def __init__(self, keys: dict, *, match_id: str, team_id: str, instance_id: str,
                 run_id: str, half_no: int, nonce_prefix: bytes | None = None,
                 now_ms: int | None = None, start_seq: int = 1, start_chain: bytes | None = None):
        self.keys = keys
        self.match_id, self.team_id = match_id, team_id
        self.instance_id, self.run_id, self.half_no = instance_id, run_id, half_no
        self.prefix = nonce_prefix if nonce_prefix is not None else os.urandom(4)
        if len(self.prefix) != 4:
            raise ValueError("nonce_prefix must be 4 bytes")
        self.seq = start_seq - 1
        self.chain = start_chain if start_chain is not None else chain_start(keys["k_mac"], run_id)
        self.fixed_ts = now_ms
        self.first_chain: bytes | None = None

    def write(self, event: str, round_no: int, *, level: str = "info", **fields) -> str:
        self.seq += 1
        header = make_header(
            self.keys, self.seq, self.chain, match_id=self.match_id, team_id=self.team_id,
            instance_id=self.instance_id, run_id=self.run_id, half_no=self.half_no,
            round_no=round_no, event=event, level=level,
            nonce=nonce_for(self.seq, self.prefix), ts_ms=self.fixed_ts,
        )
        frame = seal(self.keys, header, make_event(event, round_no, **fields))
        self.chain = b64u_dec(frame["x"])
        if self.first_chain is None:
            self.first_chain = self.chain
        return json.dumps(frame, sort_keys=True, separators=(",", ":"), ensure_ascii=True)

    # --- 崩溃续写：把 (seq, chain, prefix) 落盘，重启后从断点继续（不换链、不复用 nonce）
    def checkpoint(self) -> dict:
        return {"schema": "fwlog.checkpoint/1", "run_id": self.run_id,
                "seq": self.seq, "chain": b64u(self.chain), "prefix": self.prefix.hex(),
                "first_chain": b64u(self.first_chain) if self.first_chain else ""}

    @classmethod
    def resume(cls, keys: dict, checkpoint: dict, **kwargs) -> "Writer":
        w = cls(keys, run_id=checkpoint["run_id"],
                nonce_prefix=bytes.fromhex(checkpoint["prefix"]),
                start_seq=int(checkpoint["seq"]) + 1,
                start_chain=b64u_dec(checkpoint["chain"]), **kwargs)
        if checkpoint.get("first_chain"):
            w.first_chain = b64u_dec(checkpoint["first_chain"])
        return w

    def anchor(self, *, frames_written: int | None = None) -> dict:
        """终结锚：导出到平台回执即可让"尾部截断"可被发现（无需平台持有密钥）。"""
        return {"schema": ANCHOR_SCHEMA, "run_id": self.run_id, "kid": self.keys["kid"],
                "alg": self.keys["alg"], "count": self.seq,
                "first_chain": b64u(self.first_chain) if self.first_chain else "",
                "last_chain": b64u(self.chain)}


class Reader:
    """顺序读取帧。异常帧**不会**推进链状态（保证后续帧仍可正确诊断）。"""

    def __init__(self, keys: dict, *, strict_seq: bool = True):
        self.keys = keys
        self.chain = chain_start(keys["k_mac"], keys["run_id"])
        self.strict_seq = strict_seq
        self.count = 0
        self.first_seq: int | None = None
        self.last_seq: int | None = None
        self.first_chain: bytes | None = None
        self.last_chain: bytes = self.chain

    def read_line(self, line: str) -> dict:
        frame = json.loads(line)
        payload, chain = open_frame(self.keys, frame, prev_chain=self.chain)
        seq = int(frame["h"]["seq"])
        if self.strict_seq and self.last_seq is not None and seq != self.last_seq + 1:
            raise ValueError(f"non-sequential frame: expected {self.last_seq + 1}, got {seq}")
        if self.first_seq is None:
            self.first_seq = seq
            self.first_chain = chain
        self.last_seq = seq
        self.chain = chain
        self.last_chain = chain
        self.count += 1
        return {"header": frame["h"], "event": json.loads(payload.decode("utf-8"))}

    def observed_anchor(self) -> dict:
        return {"schema": ANCHOR_SCHEMA, "run_id": self.keys["run_id"], "kid": self.keys["kid"],
                "alg": self.keys["alg"], "count": self.count,
                "first_chain": b64u(self.first_chain) if self.first_chain else "",
                "last_chain": b64u(self.last_chain)}


# --------------------------------------------------------------------------
# 大对象（C10：blob 也走加密帧，**不存在明文快照**）
# --------------------------------------------------------------------------
def seal_blob(keys: dict, data: bytes, *, match_id: str, team_id: str, instance_id: str,
              run_id: str, half_no: int, round_no: int, seq: int,
              nonce_prefix: bytes) -> tuple[str, dict]:
    """把一个原始 payload 存成**单帧加密文件**。返回 (plain_sha256, frame_json)。"""
    digest = hashlib.sha256(data).hexdigest()
    # blob 自成一条单帧链：起点同样是 chain_start(run_id)，第 1 帧的 prev 就是它
    header = make_header(keys, seq, chain_start(keys["k_mac"], run_id),
                         match_id=match_id, team_id=team_id, instance_id=instance_id,
                         run_id=run_id, half_no=half_no, round_no=round_no,
                         event="payload", level="info",
                         nonce=nonce_for(seq, nonce_prefix))
    frame = seal(keys, header, gzip.compress(data, 6))
    return digest, json.dumps(frame, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


# --------------------------------------------------------------------------
# KAT / 自测 / 基准
# --------------------------------------------------------------------------
KAT_SECRET_HEX = "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"
KAT_MATCH, KAT_TEAM, KAT_INSTANCE, KAT_HALF = "M-2026-0001", "6324", "challenger-node-a", 1
KAT_RUN, KAT_PREFIX, KAT_TS = "r18f2c0a1-9d3b7e42", bytes.fromhex("deadbeef"), 1767225600000
KAT_BUILD = "coregeek-0.1.0+abc1234"
KAT_EVENTS = [
    ("boot", 1, {"build": KAT_BUILD, "side": "challenger", "cfg_hash": "sha256:9f2c…",
                 "py": "3.11.9"}),
    ("round", 85, {"phase": "night", "gold": 120, "score_total": 280, "station_hp": 1500,
                   "robots_n": 37, "decide_ms": 3.4, "cmds_n": 3, "errors_n": 0}),
    ("decision", 85, {"dkind": "attack_target", "module": "combat.gatling",
                      "chosen": {"weapon": 10020, "aim": [[9, 28]], "ev": 30.0},
                      "cands": [{"aim": [[9, 28]], "ev": 30.0}, {"aim": [[8, 27]], "ev": 20.0}],
                      "why": "cone_ok=3,max_ev"}),
    ("outcome", 86, {"dkind": "attack_target", "module": "combat.gatling",
                     "dmg": 30, "kills": 3, "miss": 0, "matched": True}),
    ("guard", 86, {"rejected": [{"unit": 10011, "action": "build",
                                 "reason": "day_only_violation"}]}),
]


def _kat_keys() -> dict:
    return derive_keys(bytes.fromhex(KAT_SECRET_HEX), KAT_MATCH, KAT_TEAM, KAT_INSTANCE,
                       KAT_HALF, KAT_RUN, build_id=KAT_BUILD)


def build_kat() -> dict:
    keys = _kat_keys()
    writer = Writer(keys, match_id=KAT_MATCH, team_id=KAT_TEAM, instance_id=KAT_INSTANCE,
                    run_id=KAT_RUN, half_no=KAT_HALF, nonce_prefix=KAT_PREFIX, now_ms=KAT_TS)
    frames = [json.loads(writer.write(ev, rnd, **f)) for ev, rnd, f in KAT_EVENTS]
    return {
        "spec": FRAME_SCHEMA, "alg": ALG_HMAC,
        "note": "KAT 固定输入见 constants。任何实现必须逐字节复现 frames 的 h/c/t/x。"
                "v1.1 起链为内容绑定，且绑定 run_id。",
        "constants": {"team_secret_hex": KAT_SECRET_HEX, "match_id": KAT_MATCH,
                      "team_id": KAT_TEAM, "instance_id": KAT_INSTANCE, "half_no": KAT_HALF,
                      "run_id": KAT_RUN, "build_id": KAT_BUILD,
                      "nonce_prefix_hex": KAT_PREFIX.hex(), "ts_ms": KAT_TS},
        "derived": {"context": keys["context"], "kid": keys["kid"], "bid": keys["bid"],
                    "k_enc_hex": keys["k_enc"].hex(), "k_mac_hex": keys["k_mac"].hex()},
        "frames": frames,
        "anchor": writer.anchor(),
    }


def selftest() -> int:
    fails: list[str] = []
    secret = gen_secret()
    base = dict(match_id="M1", team_id="T1", instance_id="I1", half_no=1)
    k1 = derive_keys(secret, **base, run_id="run-A")
    k2 = derive_keys(secret, **base, run_id="run-B")
    pf = b"\x01\x02\x03\x04"

    def mk(keys, run, prefix=pf):
        return Writer(keys, run_id=run, nonce_prefix=prefix, now_ms=1700000000000, **base)

    def rd(keys):
        return Reader(keys)

    # 1 往返
    w = mk(k1, "run-A")
    lines = [w.write("round", r, r=r) for r in range(1, 6)]
    r = rd(k1)
    got = [r.read_line(x)["event"]["r"] for x in lines]
    if got != [1, 2, 3, 4, 5]:
        fails.append(f"roundtrip mismatch: {got}")

    # 2 错误密钥
    try:
        rd(derive_keys(gen_secret(), **base, run_id="run-A")).read_line(lines[0])
        fails.append("wrong secret accepted")
    except ValueError:
        pass

    # 3 错误 run_id（同 secret/match）必须失败
    try:
        rd(k2).read_line(lines[0])
        fails.append("wrong run_id accepted")
    except ValueError:
        pass

    # 4 篡改密文
    f = json.loads(lines[1]); raw = bytearray(b64u_dec(f["c"])); raw[0] ^= 1; f["c"] = b64u(raw)
    try:
        rd(k1).read_line(json.dumps(f)); fails.append("tampered ct accepted")
    except ValueError:
        pass

    # 5 篡改帧头
    f = json.loads(lines[2]); f["h"]["round"] = 999
    try:
        rd(k1).read_line(json.dumps(f)); fails.append("tampered header accepted")
    except ValueError:
        pass

    # 6 删帧 / 换序
    r = rd(k1); r.read_line(lines[0])
    try:
        r.read_line(lines[2]); fails.append("deleted frame accepted")
    except ValueError:
        pass

    # 7 **跨运行拼接**（v1.0 的漏洞）：run-A 前 2 帧 + run-B 后 2 帧
    wa, wb = mk(k1, "run-A"), mk(k2, "run-B")
    la = [wa.write("round", i, i=i) for i in range(1, 4)]
    lb = [wb.write("round", i, i=i) for i in range(4, 7)]
    r = rd(k1)
    try:
        for x in la[:2]:
            r.read_line(x)
        r.read_line(lb[0])
        fails.append("cross-run splice accepted (chain not content-bound)")
    except ValueError:
        pass

    # 8 尾部截断必须能被"锚"发现
    w = mk(k1, "run-A")
    ls = [w.write("round", i, i=i) for i in range(1, 6)]
    anchor = w.anchor()
    r = rd(k1)
    for x in ls[:-2]:
        r.read_line(x)

    if r.observed_anchor() == anchor:
        fails.append("truncation not detected by anchor")
    r2 = rd(k1)
    for x in ls:
        r2.read_line(x)
    if r2.observed_anchor()["last_chain"] != anchor["last_chain"] or r2.count != anchor["count"]:
        fails.append("anchor mismatch on intact file")

    # 9 nonce 不复用 + 前缀必填
    seen = {nonce_for(i, pf) for i in range(1, 500)}
    if len(seen) != 499:
        fails.append("nonce collision")
    for bad in (b"", b"\x00\x00\x00", b"\x00" * 5):
        try:
            nonce_for(1, bad); fails.append(f"nonce accepted bad prefix {bad!r}")
        except ValueError:
            pass

    # 10 上下文隔离（match/team/instance/half/run 任一变化都换密钥）
    ctxs = [derive_keys(secret, "M1", "T1", "I1", 1, "run-A")["k_enc"],
            derive_keys(secret, "M1", "T1", "I1", 2, "run-A")["k_enc"],
            derive_keys(secret, "M2", "T1", "I1", 1, "run-A")["k_enc"],
            derive_keys(secret, "M1", "T2", "I1", 1, "run-A")["k_enc"],
            derive_keys(secret, "M1", "T1", "I2", 1, "run-A")["k_enc"],
            derive_keys(secret, "M1", "T1", "I1", 1, "run-B")["k_enc"]]
    if len(set(ctxs)) != 6:
        fails.append("context binding broken")

    # 11 算法与密钥绑定
    ka = derive_keys(secret, **base, run_id="run-A", alg=ALG_HMAC)
    kb = derive_keys(secret, **base, run_id="run-A", alg=ALG_AESGCM)
    if ka["k_enc"] == kb["k_enc"]:
        fails.append("alg not bound to key material")
    try:
        derive_keys(secret, **base, run_id="run-A", alg="rot13")
        fails.append("unknown alg accepted")
    except ValueError:
        pass

    # 12 base64 必须规范
    f = json.loads(lines[0])
    std = base64.b64encode(b64u_dec(f["c"])).decode()          # 标准字母表
    f2 = dict(f); f2["c"] = std
    try:
        rd(k1).read_line(json.dumps(f2)); fails.append("non-canonical base64 accepted")
    except ValueError:
        pass

    # 13 HKDF 上界（RFC 5869: ≤ 255×32=8160）
    try:
        hkdf_sha256(b"k", b"s", b"i", 8160)
    except ValueError:
        fails.append("hkdf rejected a legal length (8160)")
    try:
        hkdf_sha256(b"k", b"s", b"i", 8161); fails.append("hkdf accepted L>8160")
    except ValueError:
        pass

    # 14 崩溃续写：checkpoint 后继续，链不断
    w = mk(k1, "run-A")
    ls = [w.write("round", i, i=i) for i in range(1, 4)]
    ck = w.checkpoint()
    w2 = Writer.resume(k1, ck, match_id="M1", team_id="T1", instance_id="I1", half_no=1)
    ls += [w2.write("round", i, i=i) for i in range(4, 7)]
    r = rd(k1)
    seqs = [r.read_line(x)["header"]["seq"] for x in ls]
    if seqs != [1, 2, 3, 4, 5, 6]:
        fails.append(f"resume broke chain/seq: {seqs}")

    # 15 KAT 确定性
    if json.dumps(build_kat(), sort_keys=True) != json.dumps(build_kat(), sort_keys=True):
        fails.append("KAT not deterministic")

    if fails:
        print("SELFTEST FAILED")
        for x in fails:
            print("  -", x)
        return 1
    print("SELFTEST OK (15/15)")
    return 0


def bench(rounds: int = 1300, payload_bytes: int = 900) -> None:
    keys = derive_keys(gen_secret(), "M", "T", "I", 1, "run-bench")
    w = Writer(keys, match_id="M", team_id="T", instance_id="I", run_id="run-bench",
               half_no=1, nonce_prefix=b"\x00\x00\x00\x01", now_ms=1700000000000)
    body = make_event("round", 1, pad="x" * payload_bytes)
    t0 = time.perf_counter()
    lines = [w.write("round", i, pad="x" * payload_bytes) for i in range(1, rounds + 1)]
    t1 = time.perf_counter()
    total = sum(len(x) for x in lines)
    print(f"seal: {rounds} frames, plaintext payload {len(body)}B/frame, "
          f"encoded {total/1e6:.2f} MB, {t1-t0:.3f}s ({rounds/(t1-t0):.0f} frames/s, "
          f"{total/1e6/(t1-t0):.1f} MB/s encoded)")
    r = Reader(keys)
    t2 = time.perf_counter()
    for line in lines:
        r.read_line(line)
    t3 = time.perf_counter()
    print(f"open: {rounds} frames in {t3-t2:.3f}s ({rounds/(t3-t2):.0f} frames/s)")


# --------------------------------------------------------------------------
# CLI：verify / dump（v1.1 起真实可用）
# --------------------------------------------------------------------------
def _resolve_context(args) -> None:
    """未显式给出的上下文参数，从 --secret-file 的 meta（含 match_id/team_id/...）里补全。"""
    meta = {}
    if args.secret_file:
        meta = json.loads(open(args.secret_file, encoding="utf-8").read())
    args.match = args.match or meta.get("match_id") or KAT_MATCH
    args.team = args.team or meta.get("team_id") or KAT_TEAM
    args.instance = args.instance or meta.get("instance_id") or KAT_INSTANCE
    if args.half is None:
        args.half = int(meta.get("half_no", KAT_HALF))
    args.run = args.run or meta.get("run_id") or KAT_RUN


def _load_secret(args) -> bytes:
    """接受三种来源：--secret-hex；--secret-file（{"secret_hex": ...}）；
    以及样例用的 meta.json（{"demo_secret_hex": ...}，必须带 secret_is_demo=true）。"""
    if args.secret_file:
        blob = json.loads(open(args.secret_file, encoding="utf-8").read())
        if "secret_hex" in blob:
            return bytes.fromhex(blob["secret_hex"])
        if blob.get("secret_is_demo") and "demo_secret_hex" in blob:
            return bytes.fromhex(blob["demo_secret_hex"])
        raise SystemExit(f"{args.secret_file}: unrecognised secret file layout")
    if args.secret_hex:
        return bytes.fromhex(args.secret_hex)
    raise SystemExit("need --secret-file or --secret-hex")


def cmd_verify(args) -> int:
    _resolve_context(args)
    secret = _load_secret(args)
    keys = derive_keys(secret, args.match, args.team, args.instance, args.half, args.run)
    reader = Reader(keys)
    report = {"schema": "fwlog.verify/1", "file": args.verify, "ok": True,
              "frames": 0, "gap": None, "error": None, "kid": keys["kid"]}
    try:
        with open(args.verify, encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    reader.read_line(line)
                except Exception as exc:                       # noqa: BLE001
                    report.update(ok=False, error=f"line {lineno}: {exc}", gap=lineno)
                    break
    except OSError as exc:
        report.update(ok=False, error=str(exc))
    report["frames"] = reader.count
    report["observed_anchor"] = reader.observed_anchor()
    if args.anchor and report["ok"]:
        anchor = json.loads(open(args.anchor, encoding="utf-8").read())
        if reader.count != anchor.get("count") or \
                reader.observed_anchor()["last_chain"] != anchor.get("last_chain"):
            report.update(ok=False, error="anchor mismatch (truncation or wrong run)")
    elif report["ok"] and not args.anchor:
        report["warning"] = ("no --anchor supplied: tail truncation is NOT detectable "
                            "(chain alone cannot detect it)")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


def cmd_dump(args) -> int:
    _resolve_context(args)
    secret = _load_secret(args)
    keys = derive_keys(secret, args.match, args.team, args.instance, args.half, args.run)
    reader = Reader(keys)
    lo, hi = (args.round if args.round else (0, 10 ** 9))
    with open(args.dump, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = reader.read_line(line)
            ev = rec["event"]
            if args.ev and ev["ev"] not in args.ev:
                continue
            if not lo <= int(ev.get("round", 0)) <= hi:
                continue
            print(json.dumps(ev, ensure_ascii=False) if args.raw else
                  f"[{rec['header']['seq']:>5}] r{rec['header']['round']:<5} {ev['ev']:<9} "
                  f"{json.dumps({k: v for k, v in ev.items() if k not in ('schema', 'ev', 'round')}, ensure_ascii=False)}")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="fwlog v1.1 reference implementation")
    ap.add_argument("--kat", action="store_true", help="输出 KAT 向量")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--bench", action="store_true")
    ap.add_argument("--verify", metavar="FILE")
    ap.add_argument("--dump", metavar="FILE")
    ap.add_argument("--anchor")
    ap.add_argument("--ev", action="append")
    ap.add_argument("--round", type=lambda s: tuple(int(x) for x in s.split("-")))
    ap.add_argument("--raw", action="store_true")
    ap.add_argument("--secret-file")
    ap.add_argument("--secret-hex")
    ap.add_argument("--match")
    ap.add_argument("--team")
    ap.add_argument("--instance")
    ap.add_argument("--half", type=int)
    ap.add_argument("--run")
    args = ap.parse_args(argv)

    if args.kat:
        json.dump(build_kat(), sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
        return 0
    if args.selftest:
        return selftest()
    if args.bench:
        bench()
        return 0
    if args.verify:
        return cmd_verify(args)
    if args.dump:
        return cmd_dump(args)
    print(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
