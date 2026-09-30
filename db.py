#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
db.py — 统一数据库连接管理（架构优化 P0，2026-08-26）
=====================================================================
背景：存储碎片化——signals.db / decision_log.db / data_health.db /
slippage_calib.db 分散在多个文件，无法统一查询/对账/迁移。
本模块提供统一连接入口（单一 quant.db + 表名空间），消除碎片。

表名空间（单一文件 data/quant.db）：
  signals     信号库（原 signals.db，被 stock_quant/signal_tracker 等大量引用，
               2026-08-26 保留原文件避免破坏 22 依赖方，标注后续迁移）
  decisions   决策日志（原 decision_log.db，已迁移）
  probes      数据源健康（原 data_health.db，已迁移）
  fills       滑点校准（原 slippage_calib.db，已迁移）

用法：
  from db import get_conn, migrate_legacy
  conn = get_conn()          # 单一 quant.db 连接（自动建表）
  migrate_legacy()           # 迁移旧独立 db 到 quant.db（幂等）
"""
import os
import shutil
import sqlite3

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
QUANT_DB = os.path.join(DATA_DIR, 'quant.db')

# 旧独立 db → 迁移目标表（表名空间）
LEGACY_DBS = {
    'decision_log.db': 'decisions',
    'data_health.db': 'probes',
    'slippage_calib.db': 'fills',
}

# 各表建表 SQL（与各模块原建表一致，保证兼容）
SCHEMAS = {
    'decisions': '''CREATE TABLE IF NOT EXISTS decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT, code TEXT, name TEXT,
        advice TEXT, advice_line TEXT,
        action TEXT, return_pct REAL, follow TEXT, note TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime'))
    )''',
    'probes': '''CREATE TABLE IF NOT EXISTS probes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT, source TEXT, ok INTEGER, latency REAL, empty INTEGER, note TEXT
    )''',
    'fills': '''CREATE TABLE IF NOT EXISTS fills (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT, code TEXT, trigger_price REAL, exec_price REAL,
        side TEXT, note TEXT, created_at TEXT DEFAULT (datetime('now','localtime'))
    )''',
}


def get_conn():
    """返回统一 quant.db 连接（自动建表，幂等）"""
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(QUANT_DB)
    for sql in SCHEMAS.values():
        conn.execute(sql)
    conn.commit()
    return conn


def _copy_table(src_db, src_table, dst_table):
    """把旧 db 的整表数据复制到 quant.db（幂等：按 id 去重）"""
    if not os.path.exists(src_db):
        return 0
    try:
        sconn = sqlite3.connect(src_db)
        srows = sconn.execute(f"SELECT * FROM {src_table}").fetchall()
        scol = [r[1] for r in sconn.execute(f"PRAGMA table_info({src_table})").fetchall()]
        sconn.close()
    except Exception:
        return 0
    if not srows:
        return 0
    dconn = get_conn()
    dcol = [r[1] for r in dconn.execute(f"PRAGMA table_info({dst_table})").fetchall()]
    # 只复制目标表已有的列
    common = [c for c in scol if c in dcol]
    if not common:
        dconn.close()
        return 0
    placeholders = ','.join('?' * len(common))
    colnames = ','.join(common)
    n = 0
    for row in srows:
        vals = [row[scol.index(c)] for c in common]
        try:
            dconn.execute(
                f"INSERT OR IGNORE INTO {dst_table} ({colnames}) VALUES ({placeholders})",
                vals)
            n += 1
        except Exception:
            pass
    dconn.commit()
    dconn.close()
    return n


def migrate_legacy():
    """迁移旧独立 db 到 quant.db（幂等，可重复执行）"""
    report = {}
    for legacy_file, table in LEGACY_DBS.items():
        src = os.path.join(DATA_DIR, legacy_file)
        n = _copy_table(src, table, table)
        report[legacy_file] = n
    return report


def legacy_remaining():
    """返回仍存在的旧独立 db（供健康监控提示迁移进度）"""
    return [f for f in LEGACY_DBS if os.path.exists(os.path.join(DATA_DIR, f))]


if __name__ == '__main__':
    r = migrate_legacy()
    print('✅ 迁移完成:', r)
    print('剩余旧 db:', legacy_remaining() or '无（全部已迁移）')
    conn = get_conn()
    for t in ['decisions', 'probes', 'fills']:
        cnt = conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
        print(f'  {t}: {cnt} 行')
    conn.close()
