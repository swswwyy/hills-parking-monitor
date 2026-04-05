#!/usr/bin/env python3
"""
Hills Showground Park&Ride 空余车位推送脚本（云端版）
────────────────────────────────────────────────────
推送逻辑：
  · 每天悉尼时间 6:00 ~ 7:30 运行（共 90 分钟）
  · 空余 > 40%：每 5 分钟推送一次
  · 空余 <= 40%：每 1 分钟推送一次
              + 根据历史数据预测车位耗尽时间

【云端部署 - GitHub Actions】
  1. 仓库结构：
       parking-monitor/
       ├── hills_parking_push.py
       └── .github/workflows/parking.yml

  2. Settings → Secrets → Actions 添加：
       NTFY_TOPIC = 你的频道名

【手机 ntfy 设置】
  iOS/Android 安装 ntfy，订阅与 NTFY_TOPIC 相同的频道名
  iOS:     https://apps.apple.com/app/ntfy/id1625396347
  Android: https://play.google.com/store/apps/details?id=io.heckel.ntfy
"""

import urllib.request
import json
import time
import sys
import os
from datetime import datetime, timezone, timedelta
from collections import deque

# ============================================================
# 配置区
# ============================================================

# 从环境变量读取（GitHub Actions 推荐），没有则用默认值
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "hills-parking-my-channel-2024")

# 推送时间窗口（悉尼本地时间）
WINDOW_START = (20, 0)    # 06:00
WINDOW_END   = (20, 30)   # 07:30

# 空余率阈值：高于此值用"宽松间隔"，低于用"紧密间隔"
THRESHOLD_PCT = 40.0

INTERVAL_HIGH = 5    # 空余 > 40%：每 5 分钟一次（分钟）
INTERVAL_LOW  = 1    # 空余 <= 40%：每 1 分钟一次（分钟）

# 预测耗尽时间：用最近 N 条记录的变化速率做线性回归
HISTORY_SIZE = 10

# ============================================================

API_URL = "https://transportnsw.info/api/graphql"
GRAPHQL_QUERY = """query getLocations($id: ID) {
  result: widgets {
    pnrLocations(id: $id) {
      id name spots occupancy
    }
  }
}"""


def sydney_now():
    try:
        import zoneinfo
        return datetime.now(zoneinfo.ZoneInfo("Australia/Sydney"))
    except Exception:
        return datetime.now(timezone(timedelta(hours=11)))


def in_window(now):
    cur = now.hour * 60 + now.minute
    s   = WINDOW_START[0] * 60 + WINDOW_START[1]
    e   = WINDOW_END[0]   * 60 + WINDOW_END[1]
    return s <= cur <= e


def get_hills_parking():
    payload = json.dumps({
        "operationName": "getLocations",
        "query": GRAPHQL_QUERY,
        "variables": {}
    }).encode()

    req = urllib.request.Request(
        API_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; ParkingMonitor/1.0)"
        },
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode())

    for loc in data["data"]["result"]["pnrLocations"]:
        if "Hills Showground" in loc["name"]:
            spots     = loc["spots"]
            occupancy = loc["occupancy"]
            available = spots - occupancy
            return {
                "total"    : spots,
                "occupied" : occupancy,
                "available": available,
                "pct_free" : round(available / spots * 100, 1) if spots else 0
            }
    return None


def send_push(title, message, priority="default", tags="parking,car"):
    req = urllib.request.Request(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers={
            "Title"       : title.encode("utf-8"),
            "Priority"    : priority,
            "Tags"        : tags,
            "Content-Type": "text/plain; charset=utf-8"
        },
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"  推送失败: {e}")
        return False


def predict_depletion(history):
    if len(history) < 3:
        return None, None

    pts = list(history)
    n   = len(pts)
    t0  = pts[0][0]

    xs = [p[0] - t0 for p in pts]
    ys = [p[1] for p in pts]

    sum_x  = sum(xs)
    sum_y  = sum(ys)
    sum_xx = sum(x * x for x in xs)
    sum_xy = sum(x * y for x, y in zip(xs, ys))

    denom = n * sum_xx - sum_x ** 2
    if abs(denom) < 1e-9:
        return None, None

    a = (n * sum_xy - sum_x * sum_y) / denom
    b = (sum_y - a * sum_x) / n

    if a >= 0:
        return None, a

    secs_to_zero = -b / a
    now_epoch    = pts[-1][0]
    elapsed      = now_epoch - t0
    remaining    = secs_to_zero - elapsed

    if remaining <= 0:
        return None, a

    eta = sydney_now() + timedelta(seconds=remaining)
    return eta, a


def main():
    history      = deque(maxlen=HISTORY_SIZE)
    push_count   = 0
    last_push_at = None

    print("=" * 54)
    print("  Hills Showground Park&Ride 停车位监控（云端版）")
    print("=" * 54)
    print(f"  悉尼时间 : {sydney_now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  推送频道 : ntfy.sh/{NTFY_TOPIC}")
    print(f"  推送时段 : {WINDOW_START[0]:02d}:{WINDOW_START[1]:02d} ~ "
          f"{WINDOW_END[0]:02d}:{WINDOW_END[1]:02d} 悉尼时间")
    print(f"  空余 > {THRESHOLD_PCT:.0f}%  -> 每 {INTERVAL_HIGH} 分钟推送")
    print(f"  空余 <= {THRESHOLD_PCT:.0f}% -> 每 {INTERVAL_LOW} 分钟推送 + 预测耗尽")
    print("=" * 54)

    # 发送启动通知
    send_push(
        "Hills Showground 停车监控已启动",
        f"监控开始\n时段: {WINDOW_START[0]:02d}:{WINDOW_START[1]:02d}~{WINDOW_END[0]:02d}:{WINDOW_END[1]:02d} 悉尼时间",
        priority="min",
        tags="parking,rocket"
    )

    while True:
        now = sydney_now()

        if not in_window(now):
            print(f"\n[{now.strftime('%H:%M:%S')}] 超出推送时段，共推送 {push_count} 次，脚本退出。")
            break

        # 获取数据
        try:
            data = get_hills_parking()
        except Exception as e:
            print(f"[{now.strftime('%H:%M:%S')}] 获取失败: {e}")
            time.sleep(30)
            continue

        if not data:
            print(f"[{now.strftime('%H:%M:%S')}] 未找到数据")
            time.sleep(30)
            continue

        available = data["available"]
        total     = data["total"]
        pct       = data["pct_free"]
        epoch     = now.timestamp()

        history.append((epoch, available))

        # 判断推送间隔
        interval_min = INTERVAL_HIGH if pct > THRESHOLD_PCT else INTERVAL_LOW
        interval_sec = interval_min * 60

        should_push = (
            last_push_at is None or
            (epoch - last_push_at) >= interval_sec
        )

        if should_push:
            now_str = now.strftime("%H:%M")

            if pct > THRESHOLD_PCT:
                # 宽松模式：简洁格式
                title    = "Hills Showground [充裕]"
                priority = "default"
                tags     = "parking,white_check_mark"
                message  = (
                    f"空余车位：{available} / {total}\n"
                    f"悉尼时间：{now_str}"
                )

            else:
                # 紧密模式：带耗尽预测
                eta, rate = predict_depletion(history)

                if pct > 20:
                    title    = "Hills Showground [偏少]"
                    priority = "default"
                    tags     = "parking,warning"
                else:
                    title    = "Hills Showground [紧张!]"
                    priority = "high"
                    tags     = "parking,rotating_light"

                if eta:
                    mins_left    = int((eta.timestamp() - epoch) / 60)
                    eta_str      = eta.strftime("%H:%M")
                    rate_per_min = abs(rate * 60) if rate else 0
                    eta_line = (
                        f"\n预计耗尽：{eta_str}（约 {mins_left} 分钟后）\n"
                        f"消耗速率：{rate_per_min:.1f} 个/分钟"
                    )
                elif len(history) < 3:
                    eta_line = "\n预计耗尽：数据积累中..."
                else:
                    eta_line = "\n预计耗尽：车位稳定，暂无风险"

                message = (
                    f"空余车位：{available} / {total}"
                    f"{eta_line}\n"
                    f"悉尼时间：{now_str}"
                )

            ok = send_push(title, message, priority, tags)
            push_count   += 1
            last_push_at  = epoch

            status = "推送成功" if ok else "推送失败"
            print(
                f"[{now.strftime('%H:%M:%S')}] 空余 {available:>4}/{total}"
                f"  {pct:>5}%  间隔={interval_min}min  {status} (#{push_count})"
            )

        else:
            secs_since = epoch - last_push_at if last_push_at else 0
            print(
                f"[{now.strftime('%H:%M:%S')}] 采样 空余 {available:>4}/{total}"
                f"  {pct}%  (距上次推送 {int(secs_since)}s)"
            )

        time.sleep(30)


if __name__ == "__main__":
    main()
