"""국내 일정(kr_calendar.py) 검사. 인터넷에 접속하지 않고, 저장해 둔 공시 원문으로 확인한다."""

import json
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import kr_calendar as kr  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "kr"
META = json.loads((FIX / "meta.json").read_text(encoding="utf-8"))


def parse(name, subtype=None):
    m = META[name]
    disc = {"id": m["id"], "code": m["code"], "title": m["title"], "day": m["datetime"][:10], "time": ""}
    sub = subtype or kr.classify(m["title"])
    return kr.parse_disclosure(sub, (FIX / f"{name}.html").read_text(encoding="utf-8"), disc)


class TradingDays(unittest.TestCase):
    def test_ex_date_normal(self):
        # 기준일 6/30(화) → 배당락일 6/29(월)
        self.assertEqual(kr.ex_date(date(2026, 6, 30)), date(2026, 6, 29))

    def test_ex_date_year_end(self):
        # 12/31은 휴장 → 마지막 거래일 12/30의 전 거래일 12/29
        self.assertEqual(kr.ex_date(date(2026, 12, 31)), date(2026, 12, 29))

    def test_holiday_skip(self):
        # 2026-10-05(월)은 개천절 대체 휴일
        self.assertFalse(kr.is_trading_day(date(2026, 10, 5)))
        self.assertEqual(kr.prev_trading_day(date(2026, 10, 6)), date(2026, 10, 2))

    def test_market_events(self):
        evs = kr.market_events(date(2026, 10, 23))
        days = {e["uid"]: e["day"] for e in evs}
        self.assertEqual(days.get("krmkt-expiry-2026-12-10"), "2026-12-10")   # 12월 둘째 목요일
        self.assertIn("krmkt-msci-2026-11-30", days)                          # 11월 마지막 거래일
        self.assertIn("krmkt-ftse-2026-12-18", days)                          # 12월 셋째 금요일
        dec = next(e for e in evs if e["day"] == "2026-12-10")
        self.assertIn("정기변경", dec["title"])

    def test_deadlines(self):
        dl = kr.report_deadlines(date(2026, 10, 23))
        q3 = next(d for d in dl if d["period"] == "2026년 3분기")
        self.assertEqual(q3["day"], "2026-11-16")  # 11/14(토) → 다음 거래일


class Classify(unittest.TestCase):
    def test_titles(self):
        self.assertEqual(kr.classify("삼성전자(주) 연결재무제표기준영업(잠정)실적(공정공시)"), "result")
        self.assertEqual(kr.classify("주식회사 실리콘투 결산실적공시 예고"), "preann")
        self.assertEqual(kr.classify("(주)디지털대성 현금ㆍ현물배당 결정"), "div")
        self.assertEqual(kr.classify("삼성전자(주) 현금ㆍ현물 배당 결정"), "div")
        self.assertEqual(kr.classify("(주)엘앤씨바이오 (정정)유무상증자결정(주주배정후 실권주 일반공모)"), "rights")
        self.assertEqual(kr.classify("(주)알테오젠 (정정)무상증자결정"), "bonus")
        self.assertEqual(kr.classify("에이치엘비(주) 전환사채권발행결정(제44회차)"), "bond")
        self.assertIsNone(kr.classify("삼기 정기주주총회결과"))
        self.assertIsNone(kr.classify("디지털대성 자기주식취득 신탁계약 해지 결정"))
        self.assertIsNone(kr.classify("삼성전자(주) 주식선물ㆍ주식옵션 2단계 가격제한폭 확대요건 도달(하락)"))


class Parse(unittest.TestCase):
    def test_result(self):
        (e,) = parse("result_samsung")
        self.assertEqual(e["kind"], "kr_result")
        self.assertEqual(e["period"], "2026년 3분기")
        self.assertAlmostEqual(e["yoy"], 782.5)
        self.assertIn("107.40조원", e["note"])

    def test_annual(self):
        (e,) = parse("annual_samkee")
        self.assertEqual(e["period"], "2025년 연간")
        self.assertAlmostEqual(e["yoy"], 60.9)
        self.assertIn("83억원", e["note"])

    def test_preann(self):
        (e,) = parse("preann_silicon2")
        self.assertEqual((e["kind"], e["day"], e["period"]), ("kr_earn", "2026-08-12", "2026년 상반기"))

    def test_ir(self):
        (e,) = parse("ir_samsung")
        self.assertEqual((e["kind"], e["day"], e["time"]), ("kr_earn", "2026-10-29", "10:00"))

    def test_agm(self):
        (e,) = parse("agm_digital")
        self.assertEqual((e["kind"], e["day"], e["time"]), ("kr_agm", "2026-03-26", "09:30"))
        self.assertTrue(any("배당" in line for line in e["lines"]))

    def test_dividend(self):
        (e,) = parse("div_samsung")
        self.assertEqual((e["kind"], e["day"]), ("kr_div", "2026-06-29"))
        self.assertIn("374원", e["note"])
        (e2,) = parse("div_digital")
        self.assertEqual(e2["day"], "2026-03-30")  # 기준일 3/31 → 배당락 3/30
        self.assertIn("520원", e2["note"])

    def test_rights_correction(self):
        evs = parse("rights_lnc_corr")
        days = {e["title"]: e["day"] for e in evs}
        self.assertEqual(days.get("신주 상장 (유상증자)"), "2026-11-04")  # 정정 후 날짜
        self.assertEqual(days.get("신주 상장 (무상증자)"), "2026-11-12")
        self.assertTrue(all(e["group"] == "rights-2026-07-31" for e in evs))  # 원래 공시 날짜로 묶음

    def test_bonus_correction(self):
        evs = parse("bonus_alteogen_corr")
        got = {e["title"]: e["day"] for e in evs}
        self.assertEqual(got.get("무상증자 신주 상장"), "2026-08-26")
        self.assertEqual(got.get("권리락일 (무상증자)"), "2026-08-05")  # 기준일 8/6 → 8/5

    def test_bond(self):
        (e,) = parse("bond_hlb")
        self.assertEqual(e["day"], "2027-08-19")
        self.assertIn("33,361", e["note"])

    def test_split_correction(self):
        evs = parse("split_komico_corr")
        got = {e["title"]: e["day"] for e in evs}
        self.assertEqual(got.get("주식분할 신주 상장"), "2026-08-21")
        self.assertEqual(got.get("주식분할 매매거래 정지 시작"), "2026-07-29")


class Build(unittest.TestCase):
    def test_latest_correction_wins(self):
        old = {"m": {"id": 1, "code": "000001", "title": "A 유상증자결정", "day": "2026-07-31"},
               "e": [{"kind": "kr_issue", "day": "2026-11-03", "title": "신주 상장 (유상증자)", "lines": ["x"],
                      "key": "rights-list-0", "group": "rights-2026-07-31"}]}
        new = {"m": {"id": 2, "code": "000001", "title": "A (정정)유상증자결정", "day": "2026-10-12"},
               "e": [{"kind": "kr_issue", "day": "2026-11-04", "title": "신주 상장 (유상증자)", "lines": ["y"],
                      "key": "rights-list-0", "group": "rights-2026-07-31"}]}
        out = kr.build_events({"parsed": {"1": old, "2": new}}, {"000001": "A"}, date(2026, 10, 23))
        self.assertEqual([e["day"] for e in out], ["2026-11-04"])


if __name__ == "__main__":
    unittest.main()
