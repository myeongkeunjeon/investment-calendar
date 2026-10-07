# 투자 일정 캘린더 (investment-calendar)

S&P 100 기업의 **실적 발표일**과 미국 연준의 **FOMC 금리 결정일**을 모아, 매일 자동으로 갱신되는 구독용 캘린더(.ics)로 공개합니다.

- 구독 주소: `https://myeongkeunjeon.github.io/investment-calendar/calendar.ics`
- 안내 페이지: `https://myeongkeunjeon.github.io/investment-calendar/`

각 일정에는 이런 내용이 들어갑니다.
- 한국어 설명: 섹터, 테마, **한국시간 기준 발표 시점**, 대상 분기, 시장 예상 EPS
- 영어 원문 링크: 나스닥 실적 페이지, 연준 FOMC 일정 페이지

공개 정보만 씁니다. 개인 보유 종목 같은 비공개 정보는 넣지 않습니다. 투자 권유가 아닙니다.

## 어떻게 돌아가나요?

```
매일 한국시간 오전 7:17
  └─ GitHub Actions(깃허브가 빌려주는 컴퓨터)가 자동 실행
       1. 위키백과    → S&P 100 종목 목록(티커·회사명·섹터)
       2. 나스닥 API  → 지난 100일~앞으로 90일의 확정 실적 발표일 (S&P 100 종목만 남김)
          + 아직 확정 안 된 분기는 '예상일'(🗓️)을 약 6개월 앞까지 계산
       3. 연준 홈페이지 → FOMC 회의 일정
       4. 한국어 설명을 붙여 docs/calendar.ics 생성
  └─ GitHub Pages(깃허브의 무료 웹 호스팅)에 올림
  └─ 구독한 캘린더 앱이 새 파일을 읽어 감
```

한 곳에서 가져오기에 실패해도 지난번 데이터를 그대로 써서 캘린더가 비지 않습니다. 지난 일정은 120일 동안 남겨 둡니다.

## 파일 안내

| 파일 | 하는 일 |
|---|---|
| `build_calendar.py` | 캘린더를 만드는 프로그램 |
| `data/themes.json` | 종목별 한국어 테마. **직접 고쳐도 되는 파일**이에요 |
| `data/sp100.json` | 마지막으로 가져온 S&P 100 목록 (자동 저장) |
| `docs/calendar.ics` | 구독용 캘린더 파일 (자동 생성) |
| `docs/events.json` | 일정 기록 (자동 생성) |
| `docs/index.html` | 구독 방법 안내 페이지 |
| `.github/workflows/update-calendar.yml` | 매일 자동 실행 설정 |
| `tests/` | 프로그램이 제대로 동작하는지 확인하는 검사 |

## 처음 한 번만 하는 설정

1. 저장소 **Settings → Pages**로 이동해요.
2. **Build and deployment → Source**를 **GitHub Actions**로 바꿔요.
3. **Actions** 탭 → 왼쪽 **투자 캘린더 갱신** → **Run workflow**를 눌러 첫 실행을 해요.
4. 초록색 체크가 뜨면 위의 구독 주소가 열려요.

## 자주 바꿀 만한 것

- **테마 문구**: `data/themes.json`에서 `"티커": "설명"` 형식으로 고치면 다음 실행부터 반영돼요.
- **조회 기간·발표 시각 추정값**: `build_calendar.py` 맨 위의 `DAYS_AHEAD`, `ESTIMATE_DAYS`(예상일 기간), `PRE_MARKET_ET`, `AFTER_HOURS_ET`를 바꾸면 돼요.
- **실행 시각**: `.github/workflows/update-calendar.yml`의 `cron` 값을 바꿔요. 시간은 UTC 기준이라 한국시간에서 9시간을 빼서 적어요.

## 알아 두면 좋은 점

- 실적 발표 **시각**은 나스닥이 '장 전/장 후'로만 알려 주기 때문에 추정값이에요. 장 전은 미 동부 07:00경, 장 후는 16:05경으로 계산하고, 미국 서머타임도 자동으로 반영해요.
- 시각을 알리지 않은 회사는 미국 날짜에 종일 일정으로 표시돼요.
- 회사들은 보통 발표 2~5주 전에야 날짜를 확정해요. 그래서 그 뒤 일정은 **🗓️ 예상일**로 보여 줘요.
  직전 발표일에 13주(같은 요일)를 더한 날짜이고, 1~2주 정도 차이 날 수 있어요.
  회사가 날짜를 확정하면 자동으로 **📊 확정 일정**으로 바뀌어요.
- 구글 캘린더는 구독 캘린더를 보통 12~24시간마다 새로 읽어요. 그래서 바로 반영되지 않을 수 있어요.

## 직접 실행해 보기 (선택)

```bash
pip install -r requirements.txt
python -m unittest discover tests   # 검사
python build_calendar.py            # 캘린더 만들기
```
