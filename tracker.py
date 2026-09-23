"""
TIME ETF 트래커 - 수집 + 전일 대비 비교

매 영업일 GitHub Actions가 실행합니다.
1) timeetf.co.kr 상품 목록에서 액티브 ETF 전체(idx)를 찾고
2) 각 ETF 상세 페이지의 구성종목(PDF) 표를 읽어
3) data/snapshots/날짜.json 으로 저장한 뒤
4) 직전 스냅샷과 비교해 신규매수 / 전량매도 / 수량변동을 data/diffs/날짜.json, data/latest.json 으로 저장합니다.
"""
import io
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup

BASE = "https://www.timeetf.co.kr"
LIST_URL = f"{BASE}/m11.php"
VIEW_URL = f"{BASE}/m11_view.php?idx={{idx}}"

DATA = Path(__file__).parent / "data"
SNAP_DIR = DATA / "snapshots"
DIFF_DIR = DATA / "diffs"
KST = timezone(timedelta(hours=9))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9",
}


# ---------------------------------------------------------------- 수집
def get(url):
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            r.encoding = r.apparent_encoding or "utf-8"
            return r.text
        except Exception as e:  # 일시 오류는 재시도
            if attempt == 2:
                raise
            print(f"  재시도 ({e})")
            time.sleep(3)


def list_funds(html):
    """상품 목록 페이지에서 {idx: 이름}을 뽑습니다."""
    soup = BeautifulSoup(html, "lxml")
    funds = {}
    for a in soup.find_all("a", href=True):
        m = re.search(r"m11_view\.php\?idx=(\d+)", a["href"])
        if not m:
            continue
        idx = int(m.group(1))
        text = " ".join(a.get_text(" ", strip=True).split())
        # 같은 idx 링크가 여러 개면 'TIME'이 들어간 가장 그럴듯한 이름을 씁니다
        name_m = re.search(r"TIME\s*[^\s].*?액티브", text)
        name = name_m.group(0) if name_m else text
        if idx not in funds or ("TIME" in name and "TIME" not in funds[idx]) or (
            "TIME" in name and len(name) < len(funds[idx])
        ):
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
    """상세 페이지에서 구성종목 표와 기준일을 읽습니다."""
    try:
        tables = pd.read_html(io.StringIO(html), converters={0: str})
    except ValueError:
        tables = []
    target = None
    for t in tables:
        if isinstance(t.columns, pd.MultiIndex):
            t.columns = [" ".join(str(x) for x in c if "Unnamed" not in str(x)).strip() for c in t.columns]
        t.columns = [str(c).strip() for c in t.columns]
        if _col(t.columns, "종목명") and _col(t.columns, "수량"):
            target = t
            break
    if target is None:
        return None, []

    c_code = _col(target.columns, "종목코드", "코드")
    c_name = _col(target.columns, "종목명")
    c_qty = _col(target.columns, "수량")
    c_amt = _col(target.columns, "평가금액", "금액")
    c_wt = _col(target.columns, "비중")

    rows = []
    for _, r in target.iterrows():
        name = str(r[c_name]).strip() if c_name else ""
        code = str(r[c_code]).strip() if c_code else ""
        if code.lower() in ("nan", "none", "-"):
            code = ""
        if code.isdigit() and len(code) < 6:
            code = code.zfill(6)
        if not name or name.lower() == "nan" or name in ("합계", "계"):
            continue
        rows.append({
            "code": code,
            "name": name,
            "qty": _num(r[c_qty]) if c_qty else None,
            "amount": _num(r[c_amt]) if c_amt else None,
            "weight": _num(r[c_wt]) if c_wt else None,
        })

    # 기준일: 페이지에서 가장 최근 날짜 형식(YYYY-MM-DD / YYYY.MM.DD)을 찾습니다
    dates = re.findall(r"(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})", html)
    asof = None
    if dates:
        cands = sorted({f"{y}{int(m):02d}{int(d):02d}" for y, m, d in dates})
        today = datetime.now(KST).strftime("%Y%m%d")
        cands = [c for c in cands if c <= today]
        asof = cands[-1] if cands else None
    return asof, rows


def collect():
    print("상품 목록 수집…")
    funds = list_funds(get(LIST_URL))
    if not funds:
        sys.exit("상품 목록을 찾지 못했습니다. 사이트 구조가 바뀌었는지 확인이 필요합니다.")
    print(f"  {len(funds)}개 ETF")

    snapshot = {"collected_at": datetime.now(KST).isoformat(timespec="seconds"), "funds": {}}
    for idx, name in funds.items():
        asof, rows = parse_holdings(get(VIEW_URL.format(idx=idx)))
        print(f"  #{idx:<3} {name} · 기준일 {asof} · {len(rows)}종목")
        snapshot["funds"][str(idx)] = {"idx": idx, "name": name, "asof": asof, "holdings": rows}
        time.sleep(1)  # 사이트에 부담 주지 않도록

    got = [f for f in snapshot["funds"].values() if f["holdings"]]
    if not got:
        sys.exit("구성종목 표를 하나도 읽지 못했습니다. 사이트 구조 확인이 필요합니다.")
    asofs = sorted(f["asof"] for f in got if f["asof"])
    snapshot["date"] = asofs[-1] if asofs else datetime.now(KST).strftime("%Y%m%d")
    return snapshot


# ---------------------------------------------------------------- 비교
def is_cash(h):
    return not h["code"] and any(k in h["name"] for k in ("현금", "예금", "원화", "USD", "달러"))


def key(h):
    return h["code"] or h["name"]


def diff_fund(cur, prev):
    cur_map = {key(h): h for h in cur["holdings"]}
    prev_map = {key(h): h for h in (prev or {}).get("holdings", [])}
    out = {"idx": cur["idx"], "name": cur["name"], "asof": cur["asof"],
           "n_holdings": len(cur_map), "new_buys": [], "full_sells": [], "changed": []}
    if prev is None:
        return out
    for k, h in cur_map.items():
        if k not in prev_map:
            out["new_buys"].append({"code": h["code"], "name": h["name"], "qty": h["qty"], "weight": h["weight"]})
        else:
            q0, q1 = prev_map[k]["qty"], h["qty"]
            if is_cash(h):  # 현금·예금은 매일 바뀌어 수량변동에서 제외
                continue
            if q0 is not None and q1 is not None and q0 != q1:
                out["changed"].append({"code": h["code"], "name": h["name"], "prev_qty": q0, "qty": q1,
                                       "delta": q1 - q0, "weight": h["weight"]})
    for k, h in prev_map.items():
        if k not in cur_map:
            out["full_sells"].append({"code": h["code"], "name": h["name"], "prev_qty": h["qty"]})
    out["changed"].sort(key=lambda c: abs(c["delta"]), reverse=True)
    return out


def previous_snapshot(date):
    snaps = sorted(p for p in SNAP_DIR.glob("*.json") if p.stem < date)
    if not snaps:
        return None, None
    p = snaps[-1]
    return p.stem, json.loads(p.read_text(encoding="utf-8"))


def build_diff(snapshot):
    prev_date, prev = previous_snapshot(snapshot["date"])
    funds = []
    for k, f in snapshot["funds"].items():
        pf = prev["funds"].get(k) if prev else None
        if pf is not None and not pf.get("holdings"):
            pf = None
        d = diff_fund(f, pf)
        d["prev_date"] = prev_date if pf is not None else None
        funds.append(d)
    return {"date": snapshot["date"], "prev_date": prev_date,
            "collected_at": snapshot["collected_at"], "funds": funds}


def save(snapshot, diff):
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    DIFF_DIR.mkdir(parents=True, exist_ok=True)
    dump = lambda obj: json.dumps(obj, ensure_ascii=False, indent=1)
    (SNAP_DIR / f"{snapshot['date']}.json").write_text(dump(snapshot), encoding="utf-8")
    (DIFF_DIR / f"{diff['date']}.json").write_text(dump(diff), encoding="utf-8")
    (DATA / "latest.json").write_text(dump(diff), encoding="utf-8")
    dates = sorted((p.stem for p in DIFF_DIR.glob("*.json")), reverse=True)
    (DATA / "dates.json").write_text(json.dumps(dates), encoding="utf-8")


def main():
    snapshot = collect()
    diff = build_diff(snapshot)
    save(snapshot, diff)
    nb = sum(len(f["new_buys"]) for f in diff["funds"])
    ns = sum(len(f["full_sells"]) for f in diff["funds"])
    nc = sum(len(f["changed"]) for f in diff["funds"])
    print(f"완료: {diff['date']} (비교 기준 {diff['prev_date']}) · 신규매수 {nb} · 전량매도 {ns} · 수량변동 {nc}")


if __name__ == "__main__":
    main()
