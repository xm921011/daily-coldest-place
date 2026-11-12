# -*- coding: utf-8 -*-
"""每日查询「世界上最冷的地方」。

数据源：Open-Meteo /v1/forecast（免费、无需 API Key），current=temperature_2m
        = 近地面 2 米气温的模式分析/预报值，单位 °C。
口径：在「具名极寒站点 + 南极高原 / 格陵兰内陆格点」这批候选点里取最低值，
      是全球候选点采样的最小值，不是全球连续温度场的数学最小值，也不是气象站实测记录。

本脚本只负责查询与生成文件，提交由工作流里的 git 步骤完成：
  data/latest.json      结构化结果
  history/YYYY-MM.md    当月每日一行，同日重复运行覆盖当天行（幂等）
  README.md             替换 <!-- LATEST_START --> ... <!-- LATEST_END --> 区块
  $GITHUB_STEP_SUMMARY  Actions 运行页摘要（本地无此变量时跳过）

铁律：接口失败一律重试，仍失败就 exit 1；绝不把「没读到数据」当成「今天不冷」。
"""
import json
import math
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

BJ = timezone(timedelta(hours=8))
UTC = timezone.utc
API = "https://api.open-meteo.com/v1/forecast"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BATCH_SIZE = 20
MIN_VALID_RATIO = 0.60

# 具名站点：公认的极寒地区 / 有历史观测记录的站点
NAMED = [
    ("南极·沃斯托克站 Vostok", -78.46, 106.84, "南极洲"),
    ("南极·冰穹A / 昆仑站 Dome A", -80.42, 77.12, "南极洲"),
    ("南极·冰穹C / 康科迪亚站 Dome C", -75.10, 123.35, "南极洲"),
    ("南极·阿蒙森-斯科特南极点站", -89.90, 0.00, "南极洲"),
    ("南极·富士冰穹 Dome Fuji", -77.32, 39.70, "南极洲"),
    ("南极·高原站 Plateau", -79.25, 40.50, "南极洲"),
    ("南极·伯德站 Byrd", -80.02, -119.52, "南极洲"),
    ("南极·里奇B Ridge B", -76.50, 95.00, "南极洲"),
    ("南极·麦克默多站 McMurdo", -77.85, 166.67, "南极洲"),
    ("南极·中山站 Zhongshan", -69.37, 76.37, "南极洲"),
    ("格陵兰·Summit 冰盖最高点", 72.58, -38.46, "格陵兰"),
    ("格陵兰·北部内陆", 78.00, -40.00, "格陵兰"),
    ("俄罗斯·奥伊米亚康 Oymyakon", 63.46, 142.79, "西伯利亚"),
    ("俄罗斯·上扬斯克 Verkhoyansk", 67.55, 133.38, "西伯利亚"),
    ("俄罗斯·雅库茨克 Yakutsk", 62.03, 129.73, "西伯利亚"),
    ("俄罗斯·诺里尔斯克 Norilsk", 69.35, 88.20, "西伯利亚"),
    ("加拿大·尤里卡 Eureka", 79.98, -85.93, "北美北极"),
    ("加拿大·阿勒特 Alert", 82.50, -62.35, "北美北极"),
    ("加拿大·斯纳格 Snag", 62.40, -140.37, "北美"),
    ("加拿大·道森 Dawson", 64.06, -139.43, "北美"),
    ("美国·费尔班克斯 Fairbanks", 64.84, -147.72, "阿拉斯加"),
    ("美国·乌特恰维克 Utqiagvik", 71.29, -156.79, "阿拉斯加"),
    ("挪威·卡拉绍克 Karasjok", 69.47, 25.51, "北欧"),
    ("芬兰·基蒂莱 Kittila", 67.65, 24.90, "北欧"),
    ("中国·漠河", 52.97, 122.54, "中国"),
    ("中国·青藏高原可可西里", 35.60, 90.00, "中国"),
    ("北冰洋·近北极点", 89.90, 0.00, "北冰洋"),
]

# 格点扫描：南极高原是地球上最可能出现极端低温的区域，补一层粗网格避免漏掉无名最冷点
GRID = []
for lat in (-79.0, -82.0, -85.0, -88.0):
    for lon in (0, 30, 60, 90, 120, 150, 180, -150, -120, -90, -60, -30):
        GRID.append(("南极高原格点", float(lat), float(lon), "南极洲"))
for lat, lon in ((72.0, -42.0), (75.0, -40.0), (78.0, -38.0), (81.0, -40.0)):
    GRID.append(("格陵兰内陆格点", lat, lon, "格陵兰"))

CANDIDATES = [(n, la, lo, r, "named") for n, la, lo, r in NAMED] + \
             [(n, la, lo, r, "grid") for n, la, lo, r in GRID]


def fmt_coord(lat, lon):
    return "%.1f°%s / %.1f°%s" % (
        abs(lat), "N" if lat >= 0 else "S", abs(lon), "E" if lon >= 0 else "W")


def fmt_label(row, named_rows):
    """格点若贴近某个具名站（<1.5°）就用站点名，否则显示坐标。"""
    if row["kind"] == "named":
        return row["name"]
    best, dist = None, 1e9
    for n in named_rows:
        d = math.hypot(row["lat"] - n["lat"], (row["lon"] - n["lon"]) * math.cos(math.radians(row["lat"])))
        if d < dist:
            best, dist = n, d
    if best is not None and dist < 1.5:
        return "%s 附近格点" % best["name"]
    return "%s（%s）" % (row["name"], fmt_coord(row["lat"], row["lon"]))


def fetch_batch(batch, tries=3):
    lat = ",".join("%.4f" % p[1] for p in batch)
    lon = ",".join("%.4f" % p[2] for p in batch)
    url = "%s?latitude=%s&longitude=%s&current=temperature_2m&timezone=UTC&forecast_days=1" % (API, lat, lon)
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "daily-coldest-place/1.0", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=45) as r:
                data = json.loads(r.read().decode("utf-8"))
            if isinstance(data, dict):
                data = [data]
            if len(data) != len(batch):
                raise ValueError("返回 %d 个点，期望 %d 个" % (len(data), len(batch)))
            out, obs = [], None
            for (name, la, lo, region, kind), item in zip(batch, data):
                cur = item.get("current") or {}
                if obs is None:
                    obs = cur.get("time")
                temp = cur.get("temperature_2m")
                if temp is None:
                    continue  # 个别格点无数据：跳过并计数，绝不当成 0 度
                out.append({
                    "name": name, "lat": float(item.get("latitude", la)),
                    "lon": float(item.get("longitude", lo)),
                    "region": region, "kind": kind, "temp": float(temp),
                })
            return out, len(batch) - len(out), obs
        except Exception as e:  # 网络 / 5xx / JSON 异常一律重试
            last = e
            print("[warn] 第 %d/%d 次请求失败：%s" % (i + 1, tries, e), file=sys.stderr)
            if i + 1 < tries:
                time.sleep(3 * (i + 1))
    raise RuntimeError("Open-Meteo 请求最终失败：%s" % last)


def query_all():
    rows, missing, obs = [], 0, None
    batches = [CANDIDATES[i:i + BATCH_SIZE] for i in range(0, len(CANDIDATES), BATCH_SIZE)]
    for bi, b in enumerate(batches, 1):
        got, miss, o = fetch_batch(b)
        rows.extend(got)
        missing += miss
        obs = obs or o
        print("[ok] 批次 %d/%d：有效 %d / %d" % (bi, len(batches), len(got), len(b)))
    if not rows:
        raise RuntimeError("一个有效气温都没读到，拒绝生成报告")
    if len(rows) < len(CANDIDATES) * MIN_VALID_RATIO:
        raise RuntimeError("有效气温仅 %d/%d（<%.0f%%），拒绝生成报告"
                           % (len(rows), len(CANDIDATES), MIN_VALID_RATIO * 100))
    if not obs:
        raise RuntimeError("接口未返回数据时刻，拒绝生成报告")
    rows.sort(key=lambda x: x["temp"])
    return rows, missing, obs


def parse_time(iso):
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(BJ)


def build_block(res):
    lines = [
        "### 今日结论 · %s（北京时间）" % res["date_bj"],
        "",
        "**今日最冷：%s，%.1f °C**" % (res["winner_label"], res["winner_temp"]),
        "",
        "数据时刻：%s（北京时间）｜候选点 %d 个、有效 %d 个、缺失 %d 个｜数据源：Open-Meteo 模式近地面 2m 气温"
        % (res["observed_at_bj"], res["candidate_count"], res["valid_count"], res["missing_count"]),
        "",
        "| 排名 | 地点 | 气温 | 类型 |",
        "| --- | --- | --- | --- |",
    ]
    for i, r in enumerate(res["top10"], 1):
        lines.append("| %d | %s | %.1f °C | %s |" % (i, r["label"], r["temp"], "站点" if r["kind"] == "named" else "格点"))
    lines += [
        "",
        "具名站点最冷前三：" + "；".join("%s %.1f °C" % (r["label"], r["temp"]) for r in res["named_top3"]) + "。",
        "",
        "> 口径说明：在固定候选站点 + 极区网格点中取最小值，是模式数据而非气象站实测；"
        "地球有观测记录以来的最低气温为南极沃斯托克站 −89.2 °C（1983-07-21）。",
    ]
    return "\n".join(lines)


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")


def update_readme(block):
    p = os.path.join(ROOT, "README.md")
    text = open(p, encoding="utf-8").read()
    a, b = "<!-- LATEST_START -->", "<!-- LATEST_END -->"
    if a not in text or b not in text:
        raise RuntimeError("README.md 缺少 LATEST 标记，拒绝覆盖")
    new = text.split(a)[0] + a + "\n\n" + block + "\n\n" + b + text.split(b)[1]
    if new != text:
        open(p, "w", encoding="utf-8", newline="\n").write(new)
        print("[ok] 更新 README.md")


def update_history(res):
    day = res["date_bj"]
    p = os.path.join(ROOT, "history", day[:7] + ".md")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    lines = {}
    if os.path.isfile(p):
        for ln in open(p, encoding="utf-8"):
            ln = ln.strip()
            if ln.startswith("| 20") and ln.count("|") >= 5:
                key = ln.split("|")[1].strip()
                if len(key) == 10 and key[4] == "-":
                    lines[key] = ln
    row = "| %s | %s | %s | %.1f °C |" % (
        day, res["observed_at_bj"].split(" ")[-1] if " " in res["observed_at_bj"] else res["observed_at_bj"],
        res["winner_label"], res["winner_temp"])
    lines[day] = row
    head = "# %s 每日最冷记录\n\n| 日期(北京) | 数据时刻(北京) | 当日最冷地点 | 气温 |\n| --- | --- | --- | --- |\n" % day[:7]
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(head)
        for k in sorted(lines):
            f.write(lines[k] + "\n")
    print("[ok] 更新 %s" % os.path.relpath(p, ROOT))


def write_summary(block):
    p = os.environ.get("GITHUB_STEP_SUMMARY")
    if not p:
        return
    with open(p, "a", encoding="utf-8") as f:
        f.write("\n" + block + "\n")
    print("[ok] 写入 Actions 运行摘要")


def main():
    rows, missing, obs = query_all()
    obs_bj = parse_time(obs)
    now_bj = datetime.now(BJ)
    named_rows = [r for r in rows if r["kind"] == "named"]
    top10 = []
    for r in rows[:10]:
        d = dict(r)
        d["label"] = fmt_label(r, named_rows)
        top10.append(d)
    named_top3 = []
    for r in named_rows[:3]:
        d = dict(r)
        d["label"] = r["name"]
        named_top3.append(d)
    winner = rows[0]
    res = {
        "date_bj": now_bj.strftime("%Y-%m-%d"),
        "generated_at_bj": now_bj.strftime("%Y-%m-%d %H:%M:%S"),
        "observed_at_utc": obs,
        "observed_at_bj": obs_bj.strftime("%Y-%m-%d %H:%M"),
        "candidate_count": len(CANDIDATES),
        "valid_count": len(rows),
        "missing_count": missing,
        "winner_label": fmt_label(winner, named_rows),
        "winner_temp": winner["temp"],
        "winner_coord": fmt_coord(winner["lat"], winner["lon"]),
        "winner_region": winner["region"],
        "winner_kind": winner["kind"],
        "top10": top10,
        "named_top3": named_top3,
        "source": "Open-Meteo /v1/forecast current.temperature_2m (ECMWF/GFS model, UTC)",
        "unit": "°C",
    }
    block = build_block(res)
    write_json(os.path.join(ROOT, "data", "latest.json"), res)
    update_readme(block)
    update_history(res)
    write_summary(block)
    print("\n" + block + "\n")
    print("[done] 今日最冷：%s %.1f °C" % (res["winner_label"], res["winner_temp"]))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        print("[fatal] %s" % e, file=sys.stderr)
        raise SystemExit(1)
