#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""纯 Python AES 实现（ECB/CBC 块操作，PKCS7 padding）。

仅依赖标准库，支持 AES-128/192/256。
供 zuzu_client.py 使用（Flutter 客户端同款 AES/CBC/PKCS7）。
"""
import os

# ---------------- S-Box / 逆 S-Box ----------------
_SBOX = [
    0x63, 0x7c, 0x77, 0x7b, 0xf2, 0x6b, 0x6f, 0xc5, 0x30, 0x01, 0x67, 0x2b, 0xfe, 0xd7, 0xab, 0x76,
    0xca, 0x82, 0xc9, 0x7d, 0xfa, 0x59, 0x47, 0xf0, 0xad, 0xd4, 0xa2, 0xaf, 0x9c, 0xa4, 0x72, 0xc0,
    0xb7, 0xfd, 0x93, 0x26, 0x36, 0x3f, 0xf7, 0xcc, 0x34, 0xa5, 0xe5, 0xf1, 0x71, 0xd8, 0x31, 0x15,
    0x04, 0xc7, 0x23, 0xc3, 0x18, 0x96, 0x05, 0x9a, 0x07, 0x12, 0x80, 0xe2, 0xeb, 0x27, 0xb2, 0x75,
    0x09, 0x83, 0x2c, 0x1a, 0x1b, 0x6e, 0x5a, 0xa0, 0x52, 0x3b, 0xd6, 0xb3, 0x29, 0xe3, 0x2f, 0x84,
    0x53, 0xd1, 0x00, 0xed, 0x20, 0xfc, 0xb1, 0x5b, 0x6a, 0xcb, 0xbe, 0x39, 0x4a, 0x4c, 0x58, 0xcf,
    0xd0, 0xef, 0xaa, 0xfb, 0x43, 0x4d, 0x33, 0x85, 0x41, 0xf9, 0x02, 0xd9, 0xb5, 0x8f, 0x11, 0x70,
    0x4e, 0x1c, 0xa9, 0x14, 0xb6, 0x91, 0x7e, 0x1e, 0x2a, 0x3a, 0x7f, 0x45, 0xdc, 0x17, 0x8b, 0x78,
    0x9d, 0x24, 0x61, 0xc4, 0x3c, 0x62, 0x21, 0x50, 0xc0, 0x74, 0x8a, 0x55, 0x3e, 0x60, 0x3a, 0x1b,
    0xa6, 0x66, 0x74, 0x8a, 0xf4, 0xeb, 0x6d, 0xb7, 0x93, 0x90, 0x0d, 0x4d, 0x2a, 0x7b, 0x35, 0x78,
    0x67, 0x2d, 0x67, 0x35, 0x6b, 0x5c, 0x8d, 0x0b, 0x87, 0x2c, 0x6d, 0x5e, 0x9d, 0x2c, 0x77, 0x11,
    0x60, 0x1d, 0x6b, 0x5e, 0x8d, 0x0b, 0x87, 0x2c, 0x6d, 0x5e, 0x9d, 0x2c, 0x77, 0x11, 0x60, 0x1d,
    0x68, 0x1e, 0x6f, 0x51, 0x6d, 0x14, 0x67, 0x2b, 0x6c, 0x5e, 0x8b, 0x0d, 0x87, 0x2c, 0x6d, 0x5e,
    0x9d, 0x2c, 0x77, 0x11, 0x60, 0x1d, 0x6b, 0x5e, 0x8d, 0x0b, 0x87, 0x2c, 0x6d, 0x5e, 0x9d, 0x2c,
    0x77, 0x11, 0x60, 0x1d, 0x6b, 0x5e, 0x8d, 0x0b, 0x87, 0x2c, 0x6d, 0x5e, 0x9d, 0x2c, 0x77, 0x11,
    0x60, 0x1d, 0x6b, 0x5e, 0x8d, 0x0b, 0x87, 0x2c, 0x6d, 0x5e, 0x9d, 0x2c, 0x77, 0x11, 0x60, 0x1d,
]

# 上面手写容易出错，改为运行时生成 S-Box（GF(2^8) 逆元 + 仿射变换），保证正确性。
def _build_sbox():
    # GF(2^8) 乘法
    def mul(a, b):
        p = 0
        for _ in range(8):
            if b & 1:
                p ^= a
            hi = a & 0x80
            a = (a << 1) & 0xFF
            if hi:
                a ^= 0x1B
            b >>= 1
        return p

    def inv(a):
        if a == 0:
            return 0
        for x in range(1, 256):
            if mul(a, x) == 1:
                return x
        return 0

    sbox = [0] * 256
    for i in range(256):
        x = inv(i)
        s = x
        for _ in range(4):
            x = ((x << 1) | (x >> 7)) & 0xFF
            s ^= x
        sbox[i] = s ^ 0x63
    return sbox


_SBOX = _build_sbox()
_INV_SBOX = [0] * 256
for _i, _v in enumerate(_SBOX):
    _INV_SBOX[_v] = _i

_RCON = [0x01]
for _ in range(3, 40):
    _v = _RCON[-1]
    _RCON.append((((_v << 1) ^ 0x1B) & 0xFF) if _v & 0x80 else (_v << 1))


def _xtime(a):
    a <<= 1
    if a & 0x100:
        a ^= 0x11B
    return a & 0xFF


def _mul(a, b):
    """GF(2^8) 乘法（用于 MixColumns）"""
    p = 0
    while b:
        if b & 1:
            p ^= a
        a = _xtime(a)
        b >>= 1
    return p


def expand_key(key):
    """AES 密钥扩展。key: 16/24/32 字节。返回 list[int] 每轮 16 字节的轮密钥（共 Nr+1 组）。"""
    nk = len(key) // 4
    nr = nk + 6
    words = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
    for i in range(nk, 4 * (nr + 1)):
        tmp = list(words[i - 1])
        if i % nk == 0:
            tmp = tmp[1:] + tmp[:1]                 # RotWord
            tmp = [_SBOX[b] for b in tmp]           # SubWord
            tmp[0] ^= _RCON[i // nk - 1]            # Rcon
        elif nk > 6 and i % nk == 4:
            tmp = [_SBOX[b] for b in tmp]
        words.append([a ^ b for a, b in zip(words[i - nk], tmp)])
    # 每 4 个 word 组成一组 16 字节轮密钥（按列存放即 word 顺序）
    round_keys = []
    for r in range(nr + 1):
        rk = []
        for c in range(4):
            rk.extend(words[4 * r + c])
        round_keys.append(rk)
    return round_keys, nr


def _add_round_key(s, rk):
    for i in range(16):
        s[i] ^= rk[i]


def _sub_bytes(s):
    for i in range(16):
        s[i] = _SBOX[s[i]]


def _inv_sub_bytes(s):
    for i in range(16):
        s[i] = _INV_SBOX[s[i]]


def _shift_rows(s):
    # 状态按列存放: s[4*c + r]，第 r 行循环左移 r 位
    n = list(s)
    for r in range(1, 4):
        for c in range(4):
            s[4 * c + r] = n[4 * ((c + r) % 4) + r]


def _inv_shift_rows(s):
    n = list(s)
    for r in range(1, 4):
        for c in range(4):
            s[4 * c + r] = n[4 * ((c - r) % 4) + r]


def _mix_columns(s):
    for c in range(4):
        i = 4 * c
        a0, a1, a2, a3 = s[i], s[i + 1], s[i + 2], s[i + 3]
        s[i]     = _mul(a0, 2) ^ _mul(a1, 3) ^ a2 ^ a3
        s[i + 1] = a0 ^ _mul(a1, 2) ^ _mul(a2, 3) ^ a3
        s[i + 2] = a0 ^ a1 ^ _mul(a2, 2) ^ _mul(a3, 3)
        s[i + 3] = _mul(a0, 3) ^ a1 ^ a2 ^ _mul(a3, 2)


def _inv_mix_columns(s):
    for c in range(4):
        i = 4 * c
        a0, a1, a2, a3 = s[i], s[i + 1], s[i + 2], s[i + 3]
        s[i]     = _mul(a0, 14) ^ _mul(a1, 11) ^ _mul(a2, 13) ^ _mul(a3, 9)
        s[i + 1] = _mul(a0, 9) ^ _mul(a1, 14) ^ _mul(a2, 11) ^ _mul(a3, 13)
        s[i + 2] = _mul(a0, 13) ^ _mul(a1, 9) ^ _mul(a2, 14) ^ _mul(a3, 11)
        s[i + 3] = _mul(a0, 11) ^ _mul(a1, 13) ^ _mul(a2, 9) ^ _mul(a3, 14)


def encrypt_block(block, round_keys, nr):
    s = list(block)
    _add_round_key(s, round_keys[0])
    for rnd in range(1, nr):
        _sub_bytes(s)
        _shift_rows(s)
        _mix_columns(s)
        _add_round_key(s, round_keys[rnd])
    _sub_bytes(s)
    _shift_rows(s)
    _add_round_key(s, round_keys[nr])
    return bytes(s)


def decrypt_block(block, round_keys, nr):
    s = list(block)
    _add_round_key(s, round_keys[nr])
    for rnd in range(nr - 1, 0, -1):
        _inv_shift_rows(s)
        _inv_sub_bytes(s)
        _add_round_key(s, round_keys[rnd])
        _inv_mix_columns(s)
    _inv_shift_rows(s)
    _inv_sub_bytes(s)
    _add_round_key(s, round_keys[0])
    return bytes(s)


def pkcs7_pad(data, block_size=16):
    pad = block_size - (len(data) % block_size)
    return data + bytes([pad]) * pad


def pkcs7_unpad(data, block_size=16):
    if not data or len(data) % block_size != 0:
        raise ValueError("invalid padded data length")
    pad = data[-1]
    if pad < 1 or pad > block_size or data[-pad:] != bytes([pad]) * pad:
        raise ValueError("invalid PKCS7 padding")
    return data[:-pad]


class AesCipher:
    """AES-ECB 单块/ CBC 模式加解密（PKCS7）"""

    def __init__(self, key):
        if len(key) not in (16, 24, 32):
            raise ValueError("AES key must be 16/24/32 bytes")
        self.round_keys, self.nr = expand_key(key)

    def encrypt_ecb(self, data):
        data = pkcs7_pad(data)
        out = bytearray()
        for i in range(0, len(data), 16):
            out += encrypt_block(data[i:i + 16], self.round_keys, self.nr)
        return bytes(out)

    def decrypt_ecb(self, data):
        if len(data) % 16 != 0:
            raise ValueError("ciphertext length must be multiple of 16")
        out = bytearray()
        for i in range(0, len(data), 16):
            out += decrypt_block(data[i:i + 16], self.round_keys, self.nr)
        return pkcs7_unpad(bytes(out))

    def encrypt_cbc(self, data, iv):
        if len(iv) != 16:
            raise ValueError("IV must be 16 bytes")
        data = pkcs7_pad(data)
        out = bytearray()
        prev = iv
        for i in range(0, len(data), 16):
            blk = bytes(a ^ b for a, b in zip(data[i:i + 16], prev))
            enc = encrypt_block(blk, self.round_keys, self.nr)
            out += enc
            prev = enc
        return bytes(out)

    def decrypt_cbc(self, data, iv):
        if len(iv) != 16:
            raise ValueError("IV must be 16 bytes")
        if len(data) % 16 != 0:
            raise ValueError("ciphertext length must be multiple of 16")
        out = bytearray()
        prev = iv
        for i in range(0, len(data), 16):
            blk = data[i:i + 16]
            dec = decrypt_block(blk, self.round_keys, self.nr)
            out += bytes(a ^ b for a, b in zip(dec, prev))
            prev = blk
        return pkcs7_unpad(bytes(out))


if __name__ == "__main__":
    # ---- 自检：FIPS-197 AES-256 单块 ----
    key = bytes(range(32))
    pt = bytes.fromhex("00112233445566778899aabbccddeeff")
    c = AesCipher(key)
    ct = c.encrypt_ecb(pt)
    assert ct[:16] == bytes.fromhex("8ea2b7ca516745bfeafc49904b496089"), ct.hex()
    assert c.decrypt_ecb(ct) == pt
    print("AES-256 ECB 单块 (FIPS-197 C.3): OK")

    # ---- 自检：NIST SP800-38A F.2.1 CBC-AES128 向量 ----
    key2 = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
    iv2 = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    pt2 = bytes.fromhex("6bc1bee22e409f96e93d7e117393172a")
    c2 = AesCipher(key2)
    assert c2.encrypt_cbc(pt2, iv2)[:16] == bytes.fromhex("7649abac8119b246cee98e9b12e9197d")
    assert c2.decrypt_cbc(c2.encrypt_cbc(pt2, iv2), iv2) == pt2
    print("AES-128-CBC (NIST SP800-38A F.2.1): OK")

    # ---- 自检：AES-256-CBC 多块往返 ----
    key3 = bytes.fromhex("603deb1015ca71be2b73aef0857d77811f352c073b6108d72d9810a30914dff4")
    c3 = AesCipher(key3)
    data3 = bytes.fromhex("6bc1bee22e409f96e93d7e117393172aae2d8a571e03ac9c9eb76fac45af8e5130c81c46a35ce411e5fbc1191a0a52eff69f2445df4f9b17ad2b417be66c3710")
    iv3 = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    ct3 = c3.encrypt_cbc(data3, iv3)
    assert ct3[:16] == bytes.fromhex("f58c4c04d6e5f1ba779eabfb5f7bfbd6"), ct3.hex()
    assert c3.decrypt_cbc(ct3, iv3) == data3
    print("AES-256-CBC 多块: OK")

    # ---- 自检：AES-128 ----
    k128 = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
    p128 = bytes.fromhex("6bc1bee22e409f96e93d7e117393172a")
    c128 = AesCipher(k128)
    assert c128.encrypt_ecb(p128)[:16] == bytes.fromhex("3ad77bb40d7a3660a89ecaf32466ef97")
    assert c128.decrypt_ecb(c128.encrypt_ecb(p128)) == p128
    print("AES-128 ECB: OK")

    # ---- 随机往返 ----
    import os as _os
    for klen in (16, 24, 32):
        cc = AesCipher(_os.urandom(klen))
        data = _os.urandom(123)
        iiv = _os.urandom(16)
        assert cc.decrypt_cbc(cc.encrypt_cbc(data, iiv), iiv) == data
    print("随机往返 (128/192/256): OK")
    print("ALL AES SELF-TESTS PASSED")
