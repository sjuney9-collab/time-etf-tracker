"""
TIME ETF 트래커 - 수집 + '실제 매매' 분석

평일마다 GitHub Actions가 실행합니다.
  python tracker.py                         # 오늘 구성종목 수집 + 분석
  python tracker.py --backfill 2026-03-23   # 해당 날짜부터 빠진 영업일 전부 채우기

핵심 아이디어
  ETF에 자금이 들어오거나(설정) 빠지면(환매) 운용역 판단과 무관하게 모든 종목 수량이
  같은 비율로 늘거나 줄어듭니다. 종목별 수량 변화율의 중앙값으로 이 비율(자금 유출입)을
  추정하고, 그 비율을 넘어서는 부분만 '실제 매매'로 봅니다.
"""
import argparse
import io
import json
import re
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup

BASE = "https://www.timeetf.co.kr"
LIST_URL = f"{BASE}/m11.php"
VIEW_URL = f"{BASE}/m11_view.php?idx={{idx}}&pdfDate={{d}}"

ROOT = Path(__file__).parent
DATA = ROOT / "data"
SNAP_DIR = DATA / "snapshots"
VIEW_DIR = DATA / "views"
KST = timezone(timedelta(hours=9))

WINDOWS = {"1d": 1, "1w": 5, "1m": 20}   # 비교 기간(영업일)
MIN_ACTIVE_PCT = 0.02      # 자금흐름 대비 2% 이상 수량 차이
MIN_NAV_PCT = 0.0005       # 펀드 순자산 대비 0.05% 이상 규모

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9",
}
session = requests.Session()
session.headers.update(HEADERS)


# ================================================================ 수집
def get(url):
    for attempt in range(4):
        try:
            r = session.get(url, timeout=30)
            r.raise_for_status()
            r.encoding = "utf-8"
            return r.text
        except Exception as e:
            if attempt == 3:
                raise
            print(f"  재시도 {url} ({e})")
            time.sleep(3 * (attempt + 1))


def list_funds():
    soup = BeautifulSoup(get(LIST_URL), "lxml")
    funds = {}
    for a in soup.find_all("a", href=True):
        m = re.search(r"m11_view\.php\?idx=(\d+)", a["href"])
        if not m:
            continue
        idx = int(m.group(1))
        text = " ".join(a.get_text(" ", strip=True).split())
        nm = re.search(r"TIME\s*\S.*?액티브", text)
        name = nm.group(0) if nm else text
        if idx not in funds or ("TIME" in name and ("TIME" not in funds[idx] or len(name) < len(funds[idx]))):
            funds[idx] = name or f"idx {idx}"
    return dict(sorted(funds.items()))


def _num(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = re.sub(r"[^\d.\-]", "", str(v))
    if s in ("", "-", ".", "-."):
        return None
    try:
        f = float(s)
        return int(f) if f.is_integer() else f
    except ValueError:
        return None


def _col(cols, *keys):
    for c in cols:
        if any(k in c for k in keys):
            return c
    return None


def parse_holdings(html):
    try:
        tables = pd.read_html(io.StringIO(html), converters={0: str})
    except ValueError:
        return []
    for t in tables:
        t.columns = [str(c).strip() for c in t.columns]
        if _col(t.columns, "종목명") and _col(t.columns, "수량"):
            break
    else:
        return []
    c_code, c_name = _col(t.columns, "종목코드", "코드"), _col(t.columns, "종목명")
    c_qty, c_amt, c_wt = _col(t.columns, "수량"), _col(t.columns, "평가금액", "금액"), _col(t.columns, "비중")
    rows = []
    for _, r in t.iterrows():
        name = str(r[c_name]).strip()
        code = str(r[c_code]).strip() if c_code else ""
        if code.lower() in ("nan", "none", "-"):
            code = ""
        if code.isdigit() and len(code) < 6:
            code = code.zfill(6)
        if not name or name.lower() == "nan" or name in ("합계", "계") or "데이터가 없" in name:
            continue
        rows.append({"code": code, "name": name, "qty": _num(r[c_qty]),
                     "amount": _num(r[c_amt]) if c_amt else None,
                     "weight": _num(r[c_wt]) if c_wt else None})
    return rows


def fetch_day(funds, d):
    """하루치 전체 펀드 구성종목. 휴장일이면 None."""
    ds = d.isoformat()

    def one(idx):
        time.sleep(0.3)
        return idx, parse_holdings(get(VIEW_URL.format(idx=idx, d=ds)))

    with ThreadPoolExecutor(max_workers=4) as ex:
        res = dict(ex.map(one, funds))
    if not any(res.values()):
        return None
    return {"date": d.strftime("%Y%m%d"),
            "collected_at": datetime.now(KST).isoformat(timespec="seconds"),
            "funds": {str(i): {"idx": i, "name": funds[i], "holdings": res[i]} for i in funds if res[i]}}


def save_snapshot(snap):
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    (SNAP_DIR / f"{snap['date']}.json").write_text(
        json.dumps(snap, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


# ================================================================ 분석
def is_cash(h):
    return not h["code"] and any(k in h["name"] for k in ("현금", "예금", "원화", "USD", "달러", "증거금"))


def key(h):
    return h["code"] or h["name"]


def estimate_flow(ratios):
    """자금 유출입 비율 추정: 종목별 수량 변화율의 중앙값 → 그 근처(±1.5%) 종목들의 평균.
    대부분 종목은 설정/환매 비율대로만 움직이므로, 실제 매매 종목은 이상치로 빠집니다."""
    ratios = sorted(r for r in ratios if r > 0)
    if not ratios:
        return 1.0
    med = ratios[len(ratios) // 2] if len(ratios) % 2 else (ratios[len(ratios) // 2 - 1] + ratios[len(ratios) // 2]) / 2
    near = [r for r in ratios if abs(r / med - 1) <= 0.015]
    return sum(near) / len(near) if near else med


def analyze_fund(cur, prev):
    """두 시점 사이 실제 매매 추정."""
    c = {key(h): h for h in cur["holdings"] if not is_cash(h)}
    p = {key(h): h for h in prev["holdings"] if not is_cash(h)}
    nav1 = sum(h["amount"] or 0 for h in cur["holdings"]) or 1
    nav0 = sum(h["amount"] or 0 for h in prev["holdings"]) or 1

    both = [k for k in c if k in p and c[k]["qty"] and p[k]["qty"]]
    flow = estimate_flow([c[k]["qty"] / p[k]["qty"] for k in both])

    trades = []

    def add(kind, h, qty0, qty1, active_qty, price, w0, w1):
        amt = active_qty * price if price else None
        trades.append({"kind": kind, "code": h["code"], "name": h["name"],
                       "qty0": qty0, "qty1": qty1, "active_qty": round(active_qty),
                       "amount": round(amt) if amt is not None else None,
                       "nav_pct": round(amt / nav1 * 100, 3) if amt is not None else None,
                       "w0": w0, "w1": w1})

    for k, h in c.items():
        price = (h["amount"] / h["qty"]) if h["amount"] and h["qty"] else None
        if k not in p:
            add("new", h, 0, h["qty"], h["qty"] or 0, price, 0, h["weight"])
            continue
        q0, q1 = p[k]["qty"], h["qty"]
        if not q0 or q1 is None:
            continue
        expected = q0 * flow
        active = q1 - expected
        if expected <= 0 or abs(active) < 1 or abs(active / expected) < MIN_ACTIVE_PCT:
            continue
        if price and abs(active * price) / nav1 < MIN_NAV_PCT:
            continue
        add("add" if active > 0 else "trim", h, q0, q1, active, price, p[k]["weight"], h["weight"])

    for k, h in p.items():
        if k not in c:
            price = (h["amount"] / h["qty"]) if h["amount"] and h["qty"] else None
            add("exit", h, h["qty"], 0, -(h["qty"] or 0) * flow, price, h["weight"], 0)

    trades.sort(key=lambda t: abs(t["nav_pct"] or 0), reverse=True)
    return {"idx": cur["idx"], "name": cur["name"], "nav": round(nav1), "nav0": round(nav0),
            "flow_pct": round((flow - 1) * 100, 2), "n_holdings": len(c),
            "n_proportional": len(both) - sum(1 for t in trades if t["kind"] in ("add", "trim")),
            "trades": trades}


def load(d):
    return json.loads((SNAP_DIR / f"{d}.json").read_text(encoding="utf-8"))


def build_views(only_recent=None):
    dates = sorted(p.stem for p in SNAP_DIR.glob("*.json"))
    VIEW_DIR.mkdir(parents=True, exist_ok=True)
    targets = dates if only_recent is None else dates[-only_recent:]
    cache = {}

    def snap(d):
        if d not in cache:
            cache[d] = load(d)
        return cache[d]

    for d in targets:
        i = dates.index(d)
        view = {"date": d, "windows": {}}
        for wname, n in WINDOWS.items():
            if i - n < 0:
                continue
            d0 = dates[i - n]
            cur, prev = snap(d), snap(d0)
            funds = [analyze_fund(f, prev["funds"][k]) for k, f in cur["funds"].items() if k in prev["funds"]]
            new_funds = [f["name"] for k, f in cur["funds"].items() if k not in prev["funds"]]
            view["windows"][wname] = {"from": d0, "funds": funds, "new_funds": new_funds}
        (VIEW_DIR / f"{d}.json").write_text(json.dumps(view, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        if len(cache) > 30:
            cache.pop(min(cache))

    (DATA / "dates.json").write_text(json.dumps(sorted((p.stem for p in VIEW_DIR.glob("*.json")), reverse=True)),
                                     encoding="utf-8")
    if dates:
        (DATA / "latest.json").write_text((VIEW_DIR / f"{dates[-1]}.json").read_text(encoding="utf-8"),
                                          encoding="utf-8")


# ================================================================ 실행
def business_days(start, end):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", help="YYYY-MM-DD 부터 빠진 영업일 채우기")
    args = ap.parse_args()

    shutil.rmtree(DATA / "diffs", ignore_errors=True)  # 이전 버전 형식 정리
    funds = list_funds()
    if not funds:
        sys.exit("상품 목록을 찾지 못했습니다. 사이트 구조 확인이 필요합니다.")
    print(f"{len(funds)}개 ETF: " + ", ".join(funds.values()))
    today = datetime.now(KST).date()

    if args.backfill:
        have = {p.stem for p in SNAP_DIR.glob("*.json")}
        days = [d for d in business_days(date.fromisoformat(args.backfill), today) if d.strftime("%Y%m%d") not in have]
        print(f"채울 영업일 {len(days)}일")
        for d in days:
            snap = fetch_day(funds, d)
            print(f"  {d} {'휴장' if snap is None else str(len(snap['funds'])) + '개 펀드'}")
            if snap:
                save_snapshot(snap)
        build_views()
    else:
        snap = fetch_day(funds, today)
        if snap is None:
            print(f"{today} 데이터 없음(휴장일) — 종료")
            return
        save_snapshot(snap)
        build_views(only_recent=1)

    latest = json.loads((DATA / "latest.json").read_text(encoding="utf-8"))
    w = latest["windows"].get("1d")
    if w:
        n = sum(len(f["trades"]) for f in w["funds"])
        print(f"완료: {latest['date']} (전일 {w['from']}) · 실제 매매 {n}건")


if __name__ == "__main__":
    main()
