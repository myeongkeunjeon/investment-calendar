"""국내 주식 투자 일정 (코스피·코스닥).

하는 일:
  1. 네이버 증권에서 코스피·코스닥 상장 보통주 목록(종목코드·이름·업종·시가총액)을 가져온다.
  2. 종목마다 거래소 공시 목록을 훑어, 투자 일정에 해당하는 공시만 골라 내용을 읽는다.
     - 실적: 결산실적공시 예고(공시 예정일), 영업(잠정)실적, 매출액·손익구조 30% 변동(연간 실적)
     - 기업설명회(IR) 개최 일시
     - 주주총회 소집 일시
     - 현금배당 결정 → 배당락일(배당기준일 직전 거래일)
     - 유상·무상증자, 전환사채·신주인수권부사채, 주식분할·병합, 감자, 자사주 취득·소각 → 신주 상장일,
       권리락일, 전환 가능 시작일 같은 '앞으로의 날짜'
  3. 시장 전체 일정(선물·옵션 동시만기일, 코스피200·코스닥150 정기변경, MSCI·FTSE 지수 정기변경)을 계산한다.
  4. 분기·반기·사업보고서 법정 제출 기한을 계산한다 (웹앱이 '실적 예상' 일정을 만들 때 씀).
  5. 결과를 docs/kr.json 으로 저장한다. 웹앱은 이 파일을 읽어 국내 일정을 보여 준다.

한 번에 모든 공시를 다 읽지 않는다. 이미 읽은 공시 번호는 data/kr_cache.json 에 기억해 두고,
새로 올라온 것만 읽는다. 어디서 실패해도 지난번 데이터를 그대로 써서 일정이 비지 않는다.
공개 정보만 다루며 투자 권유가 아니다.
"""

from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

import holidays
import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DOCS_DIR = ROOT / "docs"
CACHE_PATH = DATA_DIR / "kr_cache.json"
STOCKS_PATH = DATA_DIR / "kr_stocks.json"
OUT_PATH = DOCS_DIR / "kr.json"

NAVER = "https://m.stock.naver.com/api"
LIST_URL = NAVER + "/stocks/marketValue/{market}?page={page}&pageSize=100"
INDUSTRY_GROUPS_URL = NAVER + "/stocks/industry?page={page}&pageSize=100"
INDUSTRY_URL = NAVER + "/stocks/industry/{no}?page={page}&pageSize=100"
DISC_LIST_URL = NAVER + "/stock/{code}/disclosure?page=1&pageSize={size}"
DISC_URL = NAVER + "/stock/{code}/disclosure/{id}"
STOCK_PAGE = "https://m.stock.naver.com/domestic/stock/{code}/total"
DISC_PAGE = "https://m.stock.naver.com/domestic/stock/{code}/disclosure"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
                   "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://m.stock.naver.com/",
}

KEEP_PAST_DAYS = 120      # 지난 일정을 며칠까지 남길지
LOOKBACK_DAYS = 150       # 공시를 며칠 전 것까지 읽을지 (첫 실행 기준)
FIRST_PAGE_SIZE = 100     # 처음 보는 종목은 공시 목록을 넉넉히
NEXT_PAGE_SIZE = 20       # 이미 본 종목은 최근 것만
DETAIL_LIMIT = 4000       # 한 번 실행에 새로 읽을 공시 내용 최대 개수 (나머지는 다음 실행에)
WORKERS = 10
TOP_N = 100               # 처음 켜져 있는 종목 수 (시가총액 상위)

WEEKDAY_KO = "월화수목금토일"
DATE_RE = re.compile(r"(20\d\d)\s*[-.년/]\s*(\d{1,2})\s*[-.월/]\s*(\d{1,2})")
TIME_RE = re.compile(r"(\d{1,2})\s*:\s*(\d{2})")
TIME_KO_RE = re.compile(r"(오전|오후)\s*(\d{1,2})\s*시\s*(?:(\d{1,2})\s*분)?")
NOTE = "※ 거래소 공시를 자동으로 읽어 정리한 것이며 투자 권유가 아닙니다. 정확한 내용은 원문 공시를 확인하세요."


# ---------------------------------------------------------------- 공통 도구

def warn(msg: str) -> None:
    print(f"::warning::{msg}")


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, data, compact: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = (json.dumps(data, ensure_ascii=False, separators=(",", ":")) if compact
            else json.dumps(data, ensure_ascii=False, indent=1))
    path.write_text(text + "\n", encoding="utf-8")


_session = requests.Session()
_session.headers.update(HEADERS)


def get_json(url: str, tries: int = 3, timeout: int = 20):
    last = None
    for attempt in range(tries):
        try:
            r = _session.get(url, timeout=timeout)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as err:
            last = err
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"{url} 가져오기 실패: {last}")


def num(text) -> float | None:
    try:
        return float(str(text).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def to_date(text: str) -> date | None:
    m = DATE_RE.search(text or "")
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def ko_day(d: date) -> str:
    return f"{d.month}월 {d.day}일({WEEKDAY_KO[d.weekday()]})"


# ---------------------------------------------------------------- 거래일 계산

_hol_cache: dict[int, set] = {}


def kr_holidays(year: int) -> set:
    if year not in _hol_cache:
        days = set(holidays.KR(years=year).keys())
        days.add(date(year, 12, 31))   # 연말 휴장
        days.add(date(year, 5, 1))     # 근로자의 날(노동절) 휴장
        _hol_cache[year] = days
    return _hol_cache[year]


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in kr_holidays(d.year)


def prev_trading_day(d: date) -> date:
    """d 바로 전 거래일."""
    d -= timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def on_or_before(d: date) -> date:
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def on_or_after(d: date) -> date:
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def ex_date(record: date) -> date:
    """배당락일·권리락일: 기준일(휴장일이면 그 전 마지막 거래일)의 바로 전 거래일.
    결제가 2거래일 걸리므로, 기준일에 주주가 되려면 그 전전 거래일까지 사야 한다."""
    return prev_trading_day(on_or_before(record))


def nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    while d.weekday() != weekday:
        d += timedelta(days=1)
    return d + timedelta(days=7 * (n - 1))


def last_trading_day_of_month(year: int, month: int) -> date:
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    return on_or_before(nxt - timedelta(days=1))


# ---------------------------------------------------------------- 1. 종목 목록

def fetch_stocks() -> list[dict]:
    """코스피·코스닥 보통주 [{code, name, mkt, ind, cap(억원)}]. 시가총액 큰 순."""
    stocks: dict[str, dict] = {}
    for market, mkt in (("KOSPI", "KS"), ("KOSDAQ", "KQ")):
        page = 1
        while page < 40:
            data = get_json(LIST_URL.format(market=market, page=page)) or {}
            rows = data.get("stocks") or []
            for s in rows:
                code, name = s.get("itemCode", ""), s.get("stockName", "")
                if s.get("stockEndType") != "stock" or not re.fullmatch(r"\d{5}0", code):
                    continue
                if re.search(r"스팩|기업인수목적", name):
                    continue
                stocks[code] = {"code": code, "name": name, "mkt": mkt, "ind": "",
                                "cap": int(num(s.get("marketValue")) or 0)}
            total = data.get("totalCount") or 0
            if not rows or page * 100 >= total:
                break
            page += 1
    # 업종
    groups = []
    for page in (1, 2):
        data = get_json(INDUSTRY_GROUPS_URL.format(page=page)) or {}
        groups += data.get("groups") or []
        if len(groups) >= (data.get("totalCount") or 0):
            break
    for g in groups:
        page = 1
        while page < 10:
            data = get_json(INDUSTRY_URL.format(no=g["no"], page=page)) or {}
            rows = data.get("stocks") or []
            for s in rows:
                if s.get("itemCode") in stocks:
                    stocks[s["itemCode"]]["ind"] = g.get("name", "")
            if not rows or page * 100 >= (data.get("totalCount") or 0):
                break
            page += 1
    out = sorted(stocks.values(), key=lambda s: -s["cap"])
    for i, s in enumerate(out):
        s["top"] = i < TOP_N
        s["ind"] = s["ind"] or "기타"
    return out


# ---------------------------------------------------------------- 2. 공시 분류와 읽기

SUBTYPES = [  # (종류, 제목에서 찾을 말) — 위에서부터 먼저 맞는 것
    ("preann", r"결산실적공시\s*예고"),
    ("result", r"\(잠정\)\s*실적|잠정\)실적"),
    ("annual", r"매출액\s*또는\s*손익구조"),
    ("ir", r"기업설명회\s*\(IR\)\s*개최"),
    ("agm", r"주주총회\s*소집\s*결의|주주총회소집결의"),
    ("div", r"현금\s*ㆍ?\s*현물\s*배당\s*결정|현금배당\s*결정"),
    ("rights", r"유무상증자\s*결정|유상증자\s*결정"),
    ("bonus", r"무상증자\s*결정"),
    ("bond", r"(전환사채권|신주인수권부사채권|교환사채권)\s*발행\s*결정"),
    ("split", r"주식\s*(분할|병합)\s*결정"),
    ("reduce", r"감자\s*결정"),
    ("buyback", r"자기주식\s*취득\s*(신탁계약\s*체결\s*)?결정"),
    ("cancel", r"주식\s*소각\s*결정"),
]
SKIP_TITLE = re.compile(r"결과|철회|취소|기재정정\s*\(공정위|종속회사|자회사의|해지")


def classify(title: str) -> str | None:
    if SKIP_TITLE.search(title or ""):
        return None
    for kind, pat in SUBTYPES:
        if re.search(pat, title or ""):
            return kind
    return None


def table_rows(html: str) -> list[list[str]]:
    soup = BeautifulSoup(html or "", "html.parser")
    rows = []
    for tr in soup.find_all("tr"):
        cells = [re.sub(r"\s+", " ", c.get_text(" ", strip=True)) for c in tr.find_all(["td", "th"])]
        cells = [c for c in cells if c]
        if cells:
            rows.append(cells)
    return rows


def body_rows(rows: list[list[str]]) -> list[list[str]]:
    """정정 공시는 맨 앞에 '정정 전/후' 표가 있다. 그 뒤의 본문만 쓴다."""
    for i, r in enumerate(rows):
        if any(re.search(r"정\s*정\s*후", c) for c in r):
            for j in range(i + 1, len(rows)):
                if any(re.match(r"^(1\.|Ⅰ\.)", c) for c in rows[j]) and not any("정정" in c for c in rows[j]):
                    return rows[j:]
    return rows


def find_row(rows, *labels, last=False):
    hit = None
    for r in rows:
        if any(any(lb in c for lb in labels) for c in r):
            hit = r
            if not last:
                return r
    return hit


def find_date(rows, *labels, last=False) -> date | None:
    """라벨이 있는 줄에서 라벨 뒤에 나오는 첫 날짜."""
    cand = [r for r in rows if any(any(lb in c for lb in labels) for c in r)]
    if last:
        cand = cand[::-1]
    for r in cand:
        idx = next(i for i, c in enumerate(r) if any(lb in c for lb in labels))
        for c in r[idx:]:
            d = to_date(c)
            if d:
                return d
    return None


def find_value(rows, *labels) -> str:
    r = find_row(rows, *labels)
    if not r:
        return ""
    idx = next(i for i, c in enumerate(r) if any(lb in c for lb in labels))
    rest = [c for c in r[idx + 1:] if c not in ("-",)]
    return rest[-1] if rest else ""


def find_time(rows, *labels) -> str:
    for r in rows:
        if any(any(lb in c for lb in labels) for c in r):
            text = " ".join(r)
            m = TIME_RE.search(text)
            if m and int(m.group(1)) < 24:
                return f"{int(m.group(1)):02d}:{m.group(2)}"
            m = TIME_KO_RE.search(text)
            if m:
                h = int(m.group(2)) % 12 + (12 if m.group(1) == "오후" else 0)
                return f"{h:02d}:{int(m.group(3) or 0):02d}"
    return ""


def original_date(rows) -> date | None:
    """정정 공시가 고치는 원래 공시의 최초 제출일."""
    return find_date(rows, "최초제출일", "정정관련 공시서류제출일")


def period_label(end: date | None, start: date | None = None) -> str:
    if not end:
        return ""
    if start and (end - start).days > 300:
        return f"{end.year}년 연간"
    if start and (end - start).days > 150 and end.month == 6:
        return f"{end.year}년 상반기"
    q = {3: 1, 6: 2, 9: 3, 12: 4}.get(end.month)
    return f"{end.year}년 {q}분기" if q else f"{end.year}년 {end.month}월"


def eok(v: float) -> str:
    """억원 단위 숫자 → '1,234억원' 또는 '12.34조원'."""
    return f"{v / 1e4:,.2f}조원" if abs(v) >= 1e4 else f"{v:,.0f}억원"


def guess_period(day: str) -> str:
    """잠정 실적 공시 날짜로 대상 분기를 짐작 (공시 표에서 기간을 못 읽었을 때)."""
    d = date.fromisoformat(day)
    if d.month <= 3:
        return f"{d.year - 1}년 4분기"
    q = {4: 1, 5: 1, 6: 1, 7: 2, 8: 2, 9: 2, 10: 3, 11: 3, 12: 3}[d.month]
    return f"{d.year}년 {q}분기"


def fmt_amount(value: float | None, unit: str) -> str:
    if value is None:
        return "-"
    if unit == "억원":
        return eok(value)
    if unit == "원":
        v = value / 1e8
        return f"{v / 1e4:,.2f}조원" if abs(v) >= 1e4 else f"{v:,.0f}억원"
    if unit == "백만원":
        v = value / 100
        return f"{v / 1e4:,.2f}조원" if abs(v) >= 1e4 else f"{v:,.0f}억원"
    if unit == "천원":
        v = value / 1e5
        return f"{v / 1e4:,.2f}조원" if abs(v) >= 1e4 else f"{v:,.0f}억원"
    return f"{value:,.2f}{unit}" if unit == "조원" else f"{value:,.0f}{unit}"


def unit_of(rows) -> str:
    for r in rows:
        for c in r:
            m = re.search(r"단위\s*[:：]?\s*(조원|억원|백만원|천원|원)", c)
            if m:
                return m.group(1)
    return "원"


def pct_txt(v: float | None) -> str:
    return "" if v is None else f"{v:+.1f}%"


# 각 공시 종류별로 일정(이벤트)을 만든다. 반환: [dict(kind, day, time, title, note, lines, short, key)]

def parse_result(rows, disc) -> list[dict]:
    unit = unit_of(rows)
    start = find_date(rows, "당기실적")
    r = find_row(rows, "당기실적")
    end = None
    if r:
        ds = [to_date(c) for c in r if to_date(c)]
        start, end = (ds[0], ds[-1]) if len(ds) >= 2 else (start, None)
    vals = {}
    for r in rows:
        if len(r) >= 3 and r[1] == "당해실적" and r[0] in ("매출액", "영업이익", "당기순이익"):
            cur = num(r[2])
            yoy = num(r[7]) if len(r) > 7 else None
            turn = r[8] if len(r) > 8 and r[8] not in ("-", "") else ""
            vals[r[0]] = (cur, yoy, turn)
    if not vals:
        return []
    per = period_label(end, start) or guess_period(disc["day"])
    lines, note = [], []
    for k in ("매출액", "영업이익", "당기순이익"):
        if k in vals and vals[k][0] is not None:
            cur, yoy, turn = vals[k]
            extra = f" (전년 동기 대비 {pct_txt(yoy)})" if yoy is not None else (f" ({turn})" if turn else "")
            lines.append(f"{k}: {fmt_amount(cur, unit)}{extra}")
    op = vals.get("영업이익")
    if op and op[0] is not None:
        note.append(f"영업이익 {fmt_amount(op[0], unit)}" + (f" ({pct_txt(op[1])})" if op[1] is not None else (f" ({op[2]})" if op[2] else "")))
    basis = "연결" if "연결" in disc["title"] else "별도"
    return [dict(kind="kr_result", day=disc["day"], time="",
                 title=f"{per} 잠정 실적 발표", note=" · ".join(note),
                 lines=[f"✅ {per} 잠정 실적 ({basis} 기준)"] + lines + ["", "잠정치라서 나중에 확정치와 조금 다를 수 있어요."],
                 short="실적", key=f"result-{per}", period=per, yoy=op[1] if op else None)]


def parse_annual(rows, disc) -> list[dict]:
    end = find_date(rows, "종료일")
    start = find_date(rows, "시작일")
    vals = {}
    for r in rows:
        name = r[0].lstrip("- ").strip() if r else ""
        if name in ("매출액", "영업이익", "당기순이익") and len(r) >= 5:
            vals[name] = (num(r[1]), num(r[4]), r[5] if len(r) > 5 and r[5] not in ("-", "") else "")
    if not vals:
        return []
    unit = unit_of(rows)
    per = period_label(end, start) or "연간"
    lines = []
    for k in ("매출액", "영업이익", "당기순이익"):
        if k in vals and vals[k][0] is not None:
            cur, yoy, turn = vals[k]
            extra = f" (전년 대비 {pct_txt(yoy)})" if yoy is not None else (f" ({turn})" if turn else "")
            lines.append(f"{k}: {fmt_amount(cur, unit)}{extra}")
    op = vals.get("영업이익")
    note = (f"영업이익 {fmt_amount(op[0], unit)}" + (f" ({pct_txt(op[1])})" if op[1] is not None else "")) if op else ""
    return [dict(kind="kr_result", day=disc["day"], time="", title=f"{per} 실적 발표 (감사 전)",
                 note=note, lines=[f"✅ {per} 실적 (외부감사 전 수치)"] + lines,
                 short="실적", key=f"result-{per}", period=per, yoy=op[1] if op else None)]


def parse_preann(rows, disc) -> list[dict]:
    d = find_date(rows, "공시예정일")
    if not d:
        return []
    end, start = find_date(rows, "종료일"), find_date(rows, "시작일")
    per = period_label(end, start)
    return [dict(kind="kr_earn", day=d.isoformat(), time="", title=f"{per} 실적 공시 예정".strip(),
                 note="회사가 미리 알린 실적 공시 날짜", short="실적",
                 lines=[f"■ {per} 실적 공시 예정일: {ko_day(d)}",
                        "회사가 '결산실적공시 예고'로 미리 알린 날짜예요. 보통 장 마감 후나 장 시작 전에 잠정 실적을 공시해요.",
                        "회사 사정으로 날짜가 바뀔 수 있어요."],
                 key=f"earn-{per}", period=per)]


def parse_ir(rows, disc) -> list[dict]:
    d = find_date(rows, "일시")
    if not d:
        return []
    t = find_time(rows, "일시")
    purpose = find_value(rows, "개최목적")
    method = find_value(rows, "개최방법")
    earnings = bool(re.search(r"실적|결산|분기|반기", purpose))
    lines = [f"■ 기업설명회(IR): {ko_day(d)}{(' ' + t) if t else ''}"]
    if purpose:
        lines.append(f"목적: {purpose}")
    if method:
        lines.append(f"방법: {method}")
    if earnings:
        lines.append("실적 발표 설명회예요. 잠정 실적은 보통 이날 또는 직전에 공시돼요.")
    return [dict(kind="kr_earn" if earnings else "kr_ir", day=d.isoformat(), time=t,
                 title="실적 발표 설명회(IR)" if earnings else "기업설명회(IR)",
                 note=purpose[:40], short="IR", lines=lines, key=f"ir-{d.isoformat()}")]


def parse_agm(rows, disc) -> list[dict]:
    d = find_date(rows, "일시", "날짜")
    if not d:
        return []
    t = find_time(rows, "시간", "일시")
    kind = find_value(rows, "주주총회 구분") or "주주총회"
    agenda = [r[1] for r in rows if len(r) >= 2 and re.fullmatch(r"\d+(-\d+)?", r[0]) and "건" in r[1]][:8]
    dividend = next((r[2] for r in rows if len(r) >= 3 and "배당" in r[2]), "")
    lines = [f"■ {kind}: {ko_day(d)}{(' ' + t) if t else ''}"]
    place = find_value(rows, "장소")
    if place:
        lines.append(f"장소: {place}")
    rec = find_date(rows, "의결권행사기준일")
    if rec:
        lines.append(f"의결권 기준일: {ko_day(rec)}")
    if agenda:
        lines += ["안건:"] + [f"· {a}" for a in agenda]
    if dividend:
        lines.append(f"배당 안건: {dividend}")
    return [dict(kind="kr_agm", day=d.isoformat(), time=t, title=kind, note=", ".join(a[:14] for a in agenda[:2]),
                 short="주총", lines=lines, key=f"agm-{d.isoformat()}")]


def parse_div(rows, disc) -> list[dict]:
    rec = find_date(rows, "배당기준일")
    if not rec:
        return []
    r = find_row(rows, "1주당 배당금")
    dps = num(r[-1]) if r and "보통" in " ".join(r) else None
    if dps is None and r:
        nums = [num(c) for c in r if num(c) is not None]
        dps = nums[0] if nums else None
    yr = find_row(rows, "시가배당")
    yld = next((num(c) for c in (yr or [])[1:] if num(c) is not None), None)
    pay = find_date(rows, "지급")
    gubun = find_value(rows, "배당구분") or "배당"
    agm = find_date(rows, "주주총회 예정일")
    ex = ex_date(rec)
    buy = prev_trading_day(ex)
    lines = [f"■ {gubun} 배당락일: {ko_day(ex)}",
             f"1주당 배당금(보통주): {dps:,.0f}원" if dps else "1주당 배당금: 공시 확인",
             f"배당기준일: {ko_day(rec)}"]
    if yld is not None:
        lines.append(f"시가배당률: {yld}%")
    if pay:
        lines.append(f"지급 예정일: {ko_day(pay)}")
    if agm:
        lines.append(f"주주총회에서 확정 예정: {ko_day(agm)}")
    lines += ["", f"이번 배당을 받으려면 {ko_day(buy)}까지 사 두어야 해요. 배당락일({ko_day(ex)})에 사면 이번 배당은 받지 못해요.",
              "배당락일에는 보통 배당금만큼 주가가 낮아진 채로 거래가 시작돼요.",
              "※ 배당락일은 거래소 휴장일을 반영해 계산한 날짜예요."]
    return [dict(kind="kr_div", day=ex.isoformat(), time="", title=f"배당락일 ({gubun})",
                 note=(f"1주당 {dps:,.0f}원" if dps else "") + (f" · 기준일 {rec.month}/{rec.day}" if rec else ""),
                 short="💵", lines=lines, key=f"div-{rec.isoformat()}")]


def share_count(rows, *labels) -> float | None:
    r = find_row(rows, *labels)
    if not r:
        return None
    for i, c in enumerate(r):
        if "보통주" in c:
            for x in r[i + 1:]:
                if num(x) is not None:
                    return num(x)
    nums = [num(c) for c in r if num(c) is not None]
    return nums[0] if nums else None


def parse_rights(rows, disc) -> list[dict]:
    new = share_count(rows, "신주의 종류와 수")
    pre = share_count(rows, "증자전 발행주식총수", "증자전발행주식총수")
    ratio = f"{new / pre * 100:.1f}%" if new and pre else ""
    method = find_value(rows, "증자방식")
    price = find_row(rows, "확정발행가", "신주 발행가액", last=True)
    listing = []
    for r in rows:
        if any("상장" in c and "예정" in c for c in r):
            d = next((to_date(c) for c in r if to_date(c)), None)
            if d and d not in listing:
                listing.append(d)
    base = find_date(rows, "신주배정기준일", "배정기준일")
    sub = find_date(rows, "청약예정일", "청약일")
    info = [f"증자 방식: {method}" if method else "", f"새로 발행하는 주식: {new:,.0f}주 (기존 주식의 {ratio})" if new and ratio else ""]
    if price:
        p = next((num(c) for c in price[::-1] if num(c) and num(c) > 0), None)
        if p:
            info.append(f"발행가: {p:,.0f}원")
    info = [x for x in info if x]
    warn_line = "유상증자는 주식 수가 늘어 기존 주주의 지분이 희석돼요. 상장일 전후로 매물이 나올 수 있어요."
    out = []
    for i, d in enumerate(listing[-2:]):
        label = "신주 상장 (유상증자)" if i == 0 or len(listing) == 1 else "신주 상장 (무상증자)"
        out.append(dict(kind="kr_issue", day=d.isoformat(), time="", title=label, note=(ratio and f"주식 수 +{ratio}") or method,
                        short="상장", lines=[f"■ {label}: {ko_day(d)}"] + info + ["", warn_line], key=f"rights-list-{i}"))
    if sub:
        out.append(dict(kind="kr_issue", day=sub.isoformat(), time="", title="유상증자 청약", note=method,
                        short="청약", lines=[f"■ 유상증자 청약: {ko_day(sub)}"] + info, key="rights-sub"))
    if base and "제3자" not in method:
        rx = ex_date(base)
        out.append(dict(kind="kr_issue", day=rx.isoformat(), time="", title="권리락일 (유상증자)", note=f"배정기준일 {base.month}/{base.day}",
                        short="권리락", lines=[f"■ 권리락일: {ko_day(rx)}", f"신주배정기준일: {ko_day(base)}",
                                             "이날부터 사면 이번 신주 배정을 받지 못해요. 보통 주가가 그만큼 조정되어 시작해요."] + info,
                        key="rights-ex"))
    if not out:
        out.append(dict(kind="kr_issue", day=disc["day"], time="", title="유상증자 결정", note=method,
                        short="증자", lines=["■ 유상증자 결정"] + info + ["", warn_line], key="rights"))
    return out


def parse_bonus(rows, disc) -> list[dict]:
    base = find_date(rows, "신주배정기준일", "배정기준일")
    listing = find_date(rows, "상장 예정일", "상장예정일")
    per = find_row(rows, "1주당 신주배정")
    ratio = next((num(c) for c in (per or [])[1:] if num(c) is not None), None)
    info = [f"1주당 {ratio:g}주 배정" if ratio else ""]
    info = [x for x in info if x]
    out = []
    if base:
        rx = ex_date(base)
        out.append(dict(kind="kr_issue", day=rx.isoformat(), time="", title="권리락일 (무상증자)", note=info[0] if info else "",
                        short="권리락", lines=[f"■ 무상증자 권리락일: {ko_day(rx)}", f"신주배정기준일: {ko_day(base)}"] + info +
                        ["이날부터 사면 무상 신주를 받지 못하고, 주가는 늘어난 주식 수만큼 낮아져 시작해요."], key="bonus-ex"))
    if listing:
        out.append(dict(kind="kr_issue", day=listing.isoformat(), time="", title="무상증자 신주 상장", note=info[0] if info else "",
                        short="상장", lines=[f"■ 무상증자 신주 상장: {ko_day(listing)}"] + info, key="bonus-list"))
    return out


def parse_bond(rows, disc) -> list[dict]:
    kind = "전환사채(CB)" if "전환" in disc["title"] else "신주인수권부사채(BW)" if "신주인수권" in disc["title"] else "교환사채(EB)"
    start = find_date(rows, "전환청구기간", "권리행사기간", "교환청구기간")
    amount = num(find_value(rows, "권면(전자등록)총액", "권면총액"))
    price_row = find_row(rows, "전환가액 (원/주)", "전환가액(원/주)", "행사가액 (원/주)", "행사가액(원/주)", "교환가액")
    price = next((num(c) for c in (price_row or [])[1:] if num(c)), None)
    ratio_row = find_row(rows, "주식총수 대비 비율")
    ratio = next((num(c) for c in (ratio_row or [])[1:] if num(c) is not None), None)
    info = []
    if amount:
        info.append(f"발행 금액: {eok(amount / 1e8)}")
    if price:
        info.append(f"전환(행사) 가격: {price:,.0f}원")
    if ratio is not None:
        info.append(f"주식으로 바뀌면 늘어나는 주식: 전체의 {ratio}%")
    lines_tail = ["", "이날부터 채권을 주식으로 바꿀 수 있어요. 주가가 전환 가격보다 높으면 주식으로 바꿔 파는 물량이 나올 수 있어요."]
    if start:
        return [dict(kind="kr_issue", day=start.isoformat(), time="", title=f"{kind} 전환 가능 시작",
                     note=(f"전환가 {price:,.0f}원" if price else "") + (f" · 주식 {ratio}%" if ratio is not None else ""),
                     short="CB", lines=[f"■ {kind} 주식 전환 가능 시작일: {ko_day(start)}", f"발행 결정: {disc['day']}"] + info + lines_tail,
                     key="bond")]
    return [dict(kind="kr_issue", day=disc["day"], time="", title=f"{kind} 발행 결정", note="", short="CB",
                 lines=[f"■ {kind} 발행 결정"] + info, key="bond")]


def parse_split(rows, disc) -> list[dict]:
    what = "주식병합" if "병합" in disc["title"] else "주식분할"
    listing = find_date(rows, "신주권상장예정일", "상장예정일")
    halt = find_date(rows, "매매거래정지예정기간", "매매거래 정지예정기간")
    r = find_row(rows, "1주당 가액")
    face = " → ".join(c for c in (r or [])[1:] if num(c)) if r else ""
    out = []
    if halt:
        out.append(dict(kind="kr_issue", day=halt.isoformat(), time="", title=f"{what} 매매거래 정지 시작", note=f"액면 {face}" if face else "",
                        short="정지", lines=[f"■ {what}로 매매거래 정지 시작: {ko_day(halt)}"] +
                        ([f"재상장 예정: {ko_day(listing)}"] if listing else []) + ["정지 기간에는 사고팔 수 없어요."], key="split-halt"))
    if listing:
        out.append(dict(kind="kr_issue", day=listing.isoformat(), time="", title=f"{what} 신주 상장", note=f"액면 {face}" if face else "",
                        short="재상장", lines=[f"■ {what} 후 거래 재개(신주 상장): {ko_day(listing)}"] + ([f"1주당 액면가: {face}원"] if face else []),
                        key="split-list"))
    return out


def parse_reduce(rows, disc) -> list[dict]:
    method = find_value(rows, "감자방법")
    listing = find_date(rows, "신주상장예정일", "상장예정일")
    rr = find_row(rows, "감자비율")
    ratio = next((num(c) for c in (rr or [])[1:] if num(c) is not None), None)
    info = [f"감자 방법: {method}" if method else "", f"감자 비율: {ratio}%" if ratio is not None else ""]
    info = [x for x in info if x]
    benign = "자기주식" in method or "소각" in method
    tail = ["자사주를 없애는 감자라 기존 주주에게는 보통 긍정적이에요."] if benign else ["무상감자는 보통 재무 상태가 나쁠 때 해요. 공시를 꼭 확인하세요."]
    d = listing or to_date(disc["day"])
    return [dict(kind="kr_issue", day=d.isoformat(), time="",
                 title="감자 후 신주 상장" if listing else "감자 결정", note=method[:20], short="감자",
                 lines=[f"■ {'감자 후 신주 상장: ' + ko_day(listing) if listing else '감자 결정'}"] + info + tail, key="reduce")]


def parse_buyback(rows, disc) -> list[dict]:
    start = find_date(rows, "취득예상기간", "계약기간")
    r = find_row(rows, "취득예상기간", "계약기간")
    end = None
    if r:
        ds = [to_date(c) for c in r if to_date(c)]
        end = ds[1] if len(ds) > 1 else None
    if not end:
        end = find_date(rows, "종료일")
    amount = num(find_value(rows, "취득예정금액", "계약금액"))
    info = []
    if amount:
        info.append(f"매입 예정 금액: {eok(amount / 1e8)}")
    if start:
        info.append(f"매입 기간: {ko_day(start)}" + (f" ~ {ko_day(end)}" if end else ""))
    return [dict(kind="kr_issue", day=disc["day"], time="", title="자사주 매입 결정",
                 note=(eok(amount / 1e8) if amount else "") + (f" · ~{end.month}/{end.day}" if end else ""),
                 short="자사주", lines=["■ 자기주식(자사주) 매입 결정"] + info +
                 ["회사가 시장에서 자기 주식을 사는 거라 보통 주가에 우호적이에요."], key=f"buyback-{disc['day']}")]


def parse_cancel(rows, disc) -> list[dict]:
    d = find_date(rows, "소각 예정일", "소각예정일")
    cnt = share_count(rows, "소각할 주식의 종류와 수", "소각할 주식")
    info = [f"소각 주식 수: {cnt:,.0f}주" if cnt else ""]
    info = [x for x in info if x]
    day = d or to_date(disc["day"])
    return [dict(kind="kr_issue", day=day.isoformat(), time="", title="자사주 소각" if d else "자사주 소각 결정", note=info[0] if info else "",
                 short="소각", lines=[f"■ 자기주식 소각{' 예정일: ' + ko_day(d) if d else ' 결정'}"] + info +
                 ["주식 수가 줄어 주당 가치가 높아지는 주주환원이에요."], key="cancel")]


PARSERS = {"result": parse_result, "annual": parse_annual, "preann": parse_preann, "ir": parse_ir, "agm": parse_agm,
           "div": parse_div, "rights": parse_rights, "bonus": parse_bonus, "bond": parse_bond, "split": parse_split,
           "reduce": parse_reduce, "buyback": parse_buyback, "cancel": parse_cancel}


def parse_disclosure(subtype: str, html: str, disc: dict) -> list[dict]:
    """공시 하나 → 일정 목록. disc = {id, code, title, day, time}"""
    rows = table_rows(html)
    if not rows:
        return []
    body = body_rows(rows)
    try:
        events = PARSERS[subtype](body, disc)
    except Exception as err:  # noqa: BLE001 — 공시 양식이 조금 달라도 전체가 멈추지 않게
        print(f"  {disc['code']} {disc['title'][:30]} 읽기 실패: {err}")
        return []
    orig = original_date(rows) if "정정" in disc["title"] else None
    group = f"{subtype}-{(orig.isoformat() if orig else disc['day'])}"
    for e in events:
        e["group"] = group
    return events


# ---------------------------------------------------------------- 3. 공시 모으기

def disc_meta(code: str, d: dict) -> dict | None:
    dt = d.get("datetime") or ""
    try:
        when = datetime.fromisoformat(dt[:19])
    except ValueError:
        return None
    return {"id": int(d.get("disclosureId") or 0), "code": code, "title": d.get("title") or "",
            # 네이버가 알려 주는 시각은 공시 접수 시각과 다를 때가 많아(아침 일괄 전송 등) 날짜만 쓴다
            "day": when.date().isoformat(), "time": ""}


def collect(stocks: list[dict], cache: dict, today: date) -> dict:
    seen: dict = cache.setdefault("seen", {})          # 종목 → 마지막으로 본 공시 번호
    parsed: dict = cache.setdefault("parsed", {})      # 공시 번호 → [일정...]  (읽은 것)
    since = (today - timedelta(days=LOOKBACK_DAYS)).isoformat()
    codes = [s["code"] for s in stocks]

    def list_one(code):
        size = NEXT_PAGE_SIZE if code in seen else FIRST_PAGE_SIZE
        try:
            rows = get_json(DISC_LIST_URL.format(code=code, size=size), tries=2) or []
            if code in seen and rows and all(int(r.get("disclosureId") or 0) > seen[code] for r in rows):
                rows = get_json(DISC_LIST_URL.format(code=code, size=FIRST_PAGE_SIZE), tries=2) or rows
            return code, rows
        except Exception as err:  # noqa: BLE001
            return code, err

    todo, fails = [], 0
    with ThreadPoolExecutor(WORKERS) as ex:
        for code, rows in ex.map(list_one, codes):
            if isinstance(rows, Exception):
                fails += 1
                continue
            for r in rows:
                m = disc_meta(code, r)
                if not m or m["day"] < since:
                    continue
                sub = classify(m["title"])
                if sub and str(m["id"]) not in parsed:
                    todo.append((sub, m))
            if rows:
                seen[code] = max([seen.get(code, 0)] + [int(r.get("disclosureId") or 0) for r in rows])
    print(f"국내 공시 목록 {len(codes) - fails}/{len(codes)}종목 조회, 새로 읽을 공시 {len(todo)}건")
    if fails > len(codes) * 0.5:
        warn(f"국내 공시 목록 조회 실패가 많아요 ({fails}종목)")
    todo.sort(key=lambda x: (x[1]["day"], x[1]["id"]), reverse=True)

    def read_one(item):
        sub, m = item
        try:
            data = get_json(DISC_URL.format(code=m["code"], id=m["id"]), tries=2) or {}
            html = (data.get("disclosure") or {}).get("contents") or ""
            return m, parse_disclosure(sub, html, m)
        except Exception as err:  # noqa: BLE001
            return m, err

    done = 0
    with ThreadPoolExecutor(WORKERS) as ex:
        for m, evs in ex.map(read_one, todo[:DETAIL_LIMIT]):
            if isinstance(evs, Exception):
                continue
            parsed[str(m["id"])] = {"m": m, "e": evs}
            done += 1
    if len(todo) > DETAIL_LIMIT:
        print(f"  공시 내용 {done}건 읽음, 나머지 {len(todo) - DETAIL_LIMIT}건은 다음 실행에서")
    # 오래된 것 정리
    keep_from = (today - timedelta(days=LOOKBACK_DAYS + 30)).isoformat()
    for k in [k for k, v in parsed.items() if v["m"]["day"] < keep_from and
              all(e["day"] < keep_from for e in v["e"])]:
        del parsed[k]
    return cache


def build_events(cache: dict, names: dict, today: date) -> list[dict]:
    """읽어 둔 공시들 → 웹앱 일정. 정정 공시가 있으면 최신 공시 내용만 남긴다."""
    keep_from = (today - timedelta(days=KEEP_PAST_DAYS)).isoformat()
    latest: dict[tuple, tuple[int, dict, list]] = {}
    for sid, v in cache.get("parsed", {}).items():
        m = v["m"]
        for e in v["e"]:
            g = (m["code"], e.get("group", ""))
            if g not in latest or m["id"] > latest[g][0]:
                latest[g] = (m["id"], m, [x for x in v["e"] if x.get("group") == e.get("group")])
    out, uids, result_days = [], set(), {}
    for (code, _), (_, m, evs) in sorted(latest.items(), key=lambda kv: -kv[1][0]):
        if code not in names:
            continue
        for e in evs:
            if e["day"] < keep_from:
                continue
            uid = f"kr-{code}-{e['key']}-{e['day']}"
            if uid in uids:
                continue
            if e["kind"] == "kr_result":  # 같은 날 정정·연결/별도 공시가 겹치면 최신 한 건만
                k = (code, e["day"])
                if k in result_days:
                    continue
                result_days[k] = True
            uids.add(uid)
            # 공통 꼬리(출처·안내·링크)는 웹앱이 붙인다 (파일 크기 줄이기)
            ev = {"uid": uid, "kind": e["kind"], "day": e["day"], "code": code, "title": e["title"],
                  "desc": "\n".join(e["lines"]), "src": f"{m['title']} ({m['day']})"}
            for f in ("time", "note", "short", "period"):
                if e.get(f):
                    ev[f] = e[f]
            if e.get("yoy") is not None:
                ev["yoy"] = e["yoy"]
            out.append(ev)
    # 같은 날 같은 종목의 실적 설명회(IR)와 실적 공시 예고가 겹치면 하나만
    out.sort(key=lambda x: (x["day"], x["code"], x["kind"]))
    return out


# ---------------------------------------------------------------- 4. 시장 전체 일정 & 실적 공시 기한

def market_events(today: date) -> list[dict]:
    out = []
    start, end = today - timedelta(days=KEEP_PAST_DAYS), today + timedelta(days=400)
    for y in range(start.year, end.year + 1):
        for mo in (3, 6, 9, 12):
            d = on_or_before(nth_weekday(y, mo, 3, 2))     # 둘째 목요일
            rebal = mo in (6, 12)
            lines = [f"■ 주가지수 선물·옵션, 개별주식 선물·옵션 동시 만기일: {ko_day(d)}",
                     "만기일 장 마감 무렵(오후 3시 20분~3시 30분 단일가)에 차익거래 물량이 한꺼번에 나와 지수와 대형주가 크게 움직일 수 있어요."]
            if rebal:
                lines += ["", "코스피200·코스닥150 정기변경: 이날 종가에 편입·편출 종목이 반영되고 다음 거래일부터 새 구성으로 바뀌어요.",
                          "편입 종목은 지수 펀드 매수, 편출 종목은 매도 물량이 이날 몰리기 쉬워요. 대상 종목은 보통 약 2주 전에 발표돼요."]
            out.append({"uid": f"krmkt-expiry-{d.isoformat()}", "kind": "krmkt", "day": d.isoformat(), "time": "15:20",
                        "title": "선물·옵션 동시만기일" + (" · 코스피200 정기변경" if rebal else ""),
                        "note": "장 마감 무렵 변동성 주의" + (" · 지수 구성 변경 반영" if rebal else ""),
                        "short": "만기", "desc": "\n".join(lines + ["", "※ 거래소 규정으로 계산한 날짜예요. 투자 권유가 아닙니다.",
                                                                 "", "한국거래소:", "https://www.krx.co.kr"])})
            f = on_or_before(nth_weekday(y, mo, 4, 3))     # 셋째 금요일
            out.append({"uid": f"krmkt-ftse-{f.isoformat()}", "kind": "krmkt", "day": f.isoformat(), "time": "15:30",
                        "title": "FTSE 지수 정기변경 반영", "note": "외국인 패시브 자금 리밸런싱", "short": "FTSE",
                        "desc": "\n".join([f"■ FTSE 지수 정기변경이 이날 장 마감 가격으로 반영돼요: {ko_day(f)}",
                                           "FTSE 지수를 따라가는 해외 펀드가 편입 종목은 사고 편출 종목은 팔아, 장 마감 동시호가에 거래가 크게 늘 수 있어요.",
                                           "※ 보통 매년 3·6·9·12월 셋째 금요일 장 마감 기준이에요. 정확한 날짜와 대상은 FTSE Russell 발표를 확인하세요.",
                                           "", "FTSE Russell:", "https://www.lseg.com/en/ftse-russell"])})
        for mo in (2, 5, 8, 11):
            d = last_trading_day_of_month(y, mo)
            out.append({"uid": f"krmkt-msci-{d.isoformat()}", "kind": "krmkt", "day": d.isoformat(), "time": "15:30",
                        "title": "MSCI 지수 정기변경 반영", "note": "외국인 패시브 자금 리밸런싱", "short": "MSCI",
                        "desc": "\n".join([f"■ MSCI 지수 정기변경이 이날 장 마감 가격으로 반영돼요: {ko_day(d)}",
                                           "MSCI 지수를 따라가는 글로벌 펀드가 편입 종목은 사고 편출 종목은 팔아, 장 마감 무렵 거래가 크게 늘 수 있어요.",
                                           "편입·편출 종목은 보통 약 2주 전(그달 중순)에 발표돼요.",
                                           "※ 매년 2·5·8·11월 마지막 거래일 기준으로 계산한 날짜예요. 정확한 날짜는 MSCI 발표를 확인하세요.",
                                           "", "MSCI 지수 정기변경 일정:", "https://www.msci.com/index-review"])})
    return [e for e in out if start.isoformat() <= e["day"] <= end.isoformat()]


def report_deadlines(today: date) -> list[dict]:
    """분기·반기 보고서(분기 끝나고 45일), 사업보고서(연말 끝나고 90일) 제출 기한. 휴일이면 다음 거래일."""
    out = []
    for y in range(today.year - 1, today.year + 2):
        for end, due, label in ((date(y, 3, 31), date(y, 5, 15), f"{y}년 1분기"),
                                (date(y, 6, 30), date(y, 8, 14), f"{y}년 상반기"),
                                (date(y, 9, 30), date(y, 11, 14), f"{y}년 3분기"),
                                (date(y, 12, 31), date(y + 1, 3, 31), f"{y}년 연간")):
            d = on_or_after(due)
            out.append({"period": label, "end": end.isoformat(), "day": d.isoformat()})
    lo, hi = (today - timedelta(days=20)).isoformat(), (today + timedelta(days=200)).isoformat()
    return [x for x in sorted(out, key=lambda x: x["day"]) if lo <= x["day"] <= hi]


# ---------------------------------------------------------------- 5. 저장

def update(today: date) -> dict | None:
    """국내 일정 전체를 만들어 docs/kr.json 에 저장한다."""
    stocks = load_json(STOCKS_PATH, [])
    try:
        fresh = fetch_stocks()
        if len(fresh) > 1500:
            stocks = fresh
            save_json(STOCKS_PATH, stocks, compact=True)
            print(f"국내 상장 보통주 {len(stocks)}개 (코스피 {sum(s['mkt'] == 'KS' for s in stocks)}, "
                  f"코스닥 {sum(s['mkt'] == 'KQ' for s in stocks)})")
        else:
            warn(f"국내 종목 목록이 너무 적어요({len(fresh)}개) → 저장된 목록 사용")
    except Exception as err:  # noqa: BLE001
        warn(f"국내 종목 목록 조회 실패 → 저장된 목록 사용 ({err})")
    if not stocks:
        warn("국내 종목 목록이 없어 국내 일정을 건너뜁니다.")
        return None
    cache = load_json(CACHE_PATH, {})
    try:
        cache = collect(stocks, cache, today)
    except Exception as err:  # noqa: BLE001
        warn(f"국내 공시 조회 실패 → 저장된 일정 사용 ({err})")
    save_json(CACHE_PATH, cache, compact=True)
    names = {s["code"]: s["name"] for s in stocks}
    events = build_events(cache, names, today)
    doc = {
        "updated": datetime.now().astimezone().isoformat(timespec="minutes"),
        "stocks": [[s["code"], s["name"], s["mkt"], s["ind"], s["cap"], 1 if s.get("top") else 0] for s in stocks],
        "events": events,
        "market": market_events(today),
        "deadlines": report_deadlines(today),
    }
    save_json(OUT_PATH, doc, compact=True)
    kinds = {}
    for e in events:
        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
    print(f"국내 일정 {len(events)}건 {kinds}, 시장 일정 {len(doc['market'])}건")
    return doc


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="국내 주식 투자 일정 만들기")
    p.add_argument("--today", help="기준 날짜(YYYY-MM-DD)")
    a = p.parse_args()
    update(date.fromisoformat(a.today) if a.today else datetime.now().date())
