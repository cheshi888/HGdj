#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""面板配置统一读取。

端口只需改一处：项目根目录的 panel.conf（或临时用环境变量 PANEL_PORT 覆盖）。
优先级：环境变量 PANEL_PORT > panel.conf > 内置默认值。
player_server / daemon / watchdog / start 全部通过这里取端口，
避免出现「改了面板、忘了看门狗」这种端口不一致。
"""
import os

BASE = os.path.dirname(os.path.abspath(__file__))
CONF = os.path.join(BASE, "panel.conf")
DEFAULT_PORT = 8899


def _conf_get(key):
    """从 panel.conf 读取一个键（KEY=VALUE，# 开头为注释）"""
    try:
        with open(CONF, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or line.startswith(";") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip().upper() == key.upper():
                    return v.strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def _valid_port(v):
    try:
        p = int(str(v).strip())
        return p if 1 <= p <= 65535 else None
    except (TypeError, ValueError):
        return None


def panel_port():
    """返回面板端口：PANEL_PORT 环境变量 > panel.conf > 8899"""
    for src in (os.environ.get("PANEL_PORT"), _conf_get("PANEL_PORT")):
        p = _valid_port(src)
        if p:
            return p
    return DEFAULT_PORT


def panel_host():
    """监听地址，默认 0.0.0.0（局域网/同网段设备可直接访问）"""
    for src in (os.environ.get("PANEL_HOST"), _conf_get("PANEL_HOST")):
        if src:
            return src.strip()
    return "0.0.0.0"


if __name__ == "__main__":
    print("端口:", panel_port())
    print("监听:", panel_host())
    print("配置:", CONF, "(存在)" if os.path.exists(CONF) else "(不存在，用默认值)")
