#!/usr/bin/env python3
"""
比特币私钥与地址生成脚本（仅用于学习/测试）
使用 Python 标准库，生成压缩 P2PKH 地址。
"""

import secrets
import hashlib
import base58  # 若未安装，需先 pip install base58；或使用下方纯标准库 Base58 实现


# ---------- 纯标准库 Base58Check 实现（避免额外依赖） ----------
B58_ALPHABET = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'


def b58encode_check(payload: bytes) -> str:
    """Base58Check 编码：payload + 4 字节双 SHA256 校验和"""
    checksum = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    data = payload + checksum
    n = int.from_bytes(data, 'big')
    res = []
    while n > 0:
        n, r = divmod(n, 58)
        res.append(B58_ALPHABET[r])
    # 处理前导零字节
    pad = 0
    for b in data:
        if b == 0:
            pad += 1
        else:
            break
    return B58_ALPHABET[0] * pad + ''.join(reversed(res))


# ---------- secp256k1 椭圆曲线点乘（纯 Python，仅用于学习） ----------
# 生产环境请使用经过审计的密码学库（如 coincurve、ecdsa）
P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
Gx = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
Gy = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8


def point_add(p1, p2):
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2 and y1 != y2:
        return None
    if x1 == x2:
        m = (3 * x1 * x1) * pow(2 * y1, P - 2, P) % P
    else:
        m = (y2 - y1) * pow(x2 - x1, P - 2, P) % P
    x3 = (m * m - x1 - x2) % P
    y3 = (m * (x1 - x3) - y1) % P
    return (x3, y3)


def point_mul(k, point):
    """椭圆曲线标量乘法：k * point"""
    result = None
    addend = point
    while k:
        if k & 1:
            result = point_add(result, addend)
        addend = point_add(addend, addend)
        k >>= 1
    return result


def private_key_to_public_key(private_key_bytes: bytes) -> bytes:
    """从 32 字节私钥推导压缩公钥（33 字节）"""
    k = int.from_bytes(private_key_bytes, 'big')
    if k == 0 or k >= N:
        raise ValueError("私钥超出有效范围")
    point = point_mul(k, (Gx, Gy))
    x, y = point
    prefix = b'\x02' if y % 2 == 0 else b'\x03'
    return prefix + x.to_bytes(32, 'big')


def public_key_to_address(pubkey: bytes) -> str:
    """压缩公钥 -> P2PKH 地址"""
    sha256_hash = hashlib.sha256(pubkey).digest()
    ripemd160 = hashlib.new('ripemd160')
    ripemd160.update(sha256_hash)
    pubkey_hash = ripemd160.digest()
    # 主网 P2PKH 版本字节 0x00
    payload = b'\x00' + pubkey_hash
    return b58encode_check(payload)


def generate_one():
    """生成一组私钥与对应地址"""
    private_key_bytes = secrets.token_bytes(32)
    # 确保私钥在有效范围内（概率极低需要重试）
    while int.from_bytes(private_key_bytes, 'big') == 0 or int.from_bytes(private_key_bytes, 'big') >= N:
        private_key_bytes = secrets.token_bytes(32)
    priv_hex = private_key_bytes.hex()
    pubkey = private_key_to_public_key(private_key_bytes)
    address = public_key_to_address(pubkey)
    return priv_hex, address


if __name__ == "__main__":
    print("=" * 60)
    print("比特币私钥与地址（仅用于学习/测试，请勿用于真实资金）")
    print("=" * 60)
    for i in range(1, 11):
        priv_hex, addr = generate_one()
        print(f"{i:2d}. 私钥 (hex): {priv_hex}")
        print(f"    地址      : {addr}")
        print("-" * 60)