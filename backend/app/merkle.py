"""Merkle tree for tamper-evident cohort roots (Merkle, 1979)."""
from .security import sha256


def leaf(claim_id: int, apaar_hash: str, seat_type: str, state: str) -> str:
    return sha256(f"leaf|{claim_id}|{apaar_hash}|{seat_type}|{state}")


def _pair(a: str, b: str) -> str:
    return sha256("node|" + a + b)


def build(leaves: list[str]) -> list[list[str]]:
    if not leaves:
        return [[sha256("empty")]]
    levels = [list(leaves)]
    while len(levels[-1]) > 1:
        cur = levels[-1]
        nxt = [_pair(cur[i], cur[i + 1] if i + 1 < len(cur) else cur[i]) for i in range(0, len(cur), 2)]
        levels.append(nxt)
    return levels


def root(leaves: list[str]) -> str:
    return build(leaves)[-1][0]


def proof(leaves: list[str], index: int) -> list[dict]:
    levels = build(leaves)
    out = []
    for lvl in levels[:-1]:
        sib = index ^ 1
        sibling = lvl[sib] if sib < len(lvl) else lvl[index]
        out.append({"hash": sibling, "side": "right" if index % 2 == 0 else "left"})
        index //= 2
    return out


def verify(leaf_hash: str, path: list[dict], expected_root: str) -> bool:
    h = leaf_hash
    for step in path:
        h = _pair(h, step["hash"]) if step["side"] == "right" else _pair(step["hash"], h)
    return h == expected_root
