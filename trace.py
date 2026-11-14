#!/usr/bin/env python3
"""血缘溯源:读取 jobs.json 与 outputs/ 产物摘要,对查询日产出 lineage.txt。"""
import json
import os
import re
import sys
from datetime import date, timedelta

QUERY_DAY = date(2026, 10, 3)
BASE = os.path.dirname(os.path.abspath(__file__))


def parse_incl(incl):
    """按含法写法原文解析端点含法,如 [..] 双闭、[..) 左闭右开。"""
    m = re.fullmatch(r"([\[(])\.\.([\])])", incl.strip())
    if not m:
        raise ValueError(f"未知含法写法: {incl!r}")
    return m.group(1) == "[", m.group(2) == "]"


def covers(job, day):
    """该版本窗口按自己的含法是否覆盖指定日期。"""
    lo = date.fromisoformat(job["win_from"])
    hi = date.fromisoformat(job["win_to"])
    inc_lo, inc_hi = parse_incl(job["incl"])
    if day < lo or day > hi:
        return False
    if day == lo and not inc_lo:
        return False
    if day == hi and not inc_hi:
        return False
    return True


def window_days(job):
    """窗口按自己的含法展开为日期集合。"""
    lo = date.fromisoformat(job["win_from"])
    hi = date.fromisoformat(job["win_to"])
    inc_lo, inc_hi = parse_incl(job["incl"])
    days = set()
    d = lo
    while d <= hi:
        days.add(d)
        d += timedelta(days=1)
    if not inc_lo:
        days.discard(lo)
    if not inc_hi:
        days.discard(hi)
    return days


def load_outputs():
    """outputs 目录:文件名 job_id_vN.json -> {(job_id, ver): {type: count}}"""
    out = {}
    outdir = os.path.join(BASE, "outputs")
    for fn in os.listdir(outdir):
        m = re.fullmatch(r"(.+)_v(\d+)\.json", fn)
        if not m:
            continue
        with open(os.path.join(outdir, fn), encoding="utf-8") as f:
            out[(m.group(1), int(m.group(2)))] = json.load(f)
    return out


def main():
    jobs = []
    with open(os.path.join(BASE, "jobs.json"), encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                jobs.append(json.loads(line))

    outputs = load_outputs()

    # 按任务分组,版本升序
    by_job = {}
    for j in jobs:
        by_job.setdefault(j["job_id"], []).append(j)
    for v in by_job.values():
        v.sort(key=lambda j: j["ver"])

    def done_versions(job_id):
        return [j for j in by_job[job_id] if j["state"] == "DONE"]

    def latest_done(job_id):
        ds = done_versions(job_id)
        return ds[-1] if ds else None

    lines = []
    lines.append(f"查询日 {QUERY_DAY.isoformat()}")
    lines.append(f"任务数 {len(by_job)} 登记行数 {len(jobs)}")

    # ---- 各表段(表名升序)----
    dst_tbls = sorted({j["dst_tbl"] for j in jobs})
    for tbl in dst_tbls:
        lines.append(f"表 {tbl}")
        contrib = []
        for job_id in sorted(by_job):
            lat = latest_done(job_id)
            if lat and lat["dst_tbl"] == tbl and covers(lat, QUERY_DAY):
                contrib.append(lat)
        if not contrib:
            lines.append("当日无有效血缘")
            continue
        for j in contrib:  # 已按 job_id 升序
            counts = outputs.get((j["job_id"], j["ver"]), {})
            pairs = " ".join(f"{t} {counts[t]}" for t in sorted(counts))
            line = (f"{j['job_id']} v{j['ver']} src {j['src_tbl']} "
                    f"win {j['win_from']} {j['win_to']} incl {j['incl']}")
            lines.append(f"{line} {pairs}" if pairs else line)

    # ---- 血缘空洞段:作废版本窗口覆盖而接替版本(最高DONE)未覆盖的日期 ----
    lines.append("血缘空洞")
    holes = []  # (tbl, date)
    for job_id in sorted(by_job):
        lat = latest_done(job_id)
        succ_days = window_days(lat) if lat else set()
        for j in by_job[job_id]:
            if j["state"] != "ROLLED_BACK":
                continue
            for d in sorted(window_days(j) - succ_days):
                holes.append((j["dst_tbl"], d))
    for tbl, d in sorted(holes, key=lambda x: (x[0], x[1])):
        lines.append(f"{tbl} {d.isoformat()}")

    # ---- 补采刷新段:同任务相邻 DONE 版本,新减旧 ----
    lines.append("补采刷新")
    for job_id in sorted(by_job):
        ds = done_versions(job_id)
        for old, new in zip(ds, ds[1:]):
            c_old = outputs.get((job_id, old["ver"]), {})
            c_new = outputs.get((job_id, new["ver"]), {})
            diffs = []
            for t in sorted(set(c_old) | set(c_new)):
                delta = c_new.get(t, 0) - c_old.get(t, 0)
                if delta:
                    diffs.append(f"{t} {delta:+d}")
            lines.append(f"{job_id} v{old['ver']} v{new['ver']} REFRESH "
                         + " ".join(diffs))

    # ---- 任务处置段 ----
    lines.append("任务处置")
    for job_id in sorted(by_job):
        vers = by_job[job_id]
        ds = done_versions(job_id)
        highest_done = ds[-1] if ds else None
        has_rolled = any(j["state"] == "ROLLED_BACK" for j in vers)
        for j in vers:
            tags = []
            if j["state"] == "ROLLED_BACK":
                tags.append("作废")
            if j["state"] in ("RUNNING", "ABORT"):
                tags.append("无产物")
            if j["state"] == "DONE":
                if highest_done and j["ver"] < highest_done["ver"]:
                    tags.append("被刷新")
                elif highest_done and j["ver"] == highest_done["ver"]:
                    earlier_done = any(d["ver"] < j["ver"] for d in ds)
                    if has_rolled and any(
                            x["state"] == "ROLLED_BACK" and x["ver"] < j["ver"]
                            for x in vers):
                        tags.append("顶替")
                    if earlier_done:
                        tags.append("刷新")
            line = f"{job_id} v{j['ver']} {j['state']}"
            if tags:
                line += " " + " ".join(tags)
            lines.append(line)

    with open(os.path.join(BASE, "lineage.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    sys.exit(main())
