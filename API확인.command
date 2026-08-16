#!/bin/bash
# 공공데이터 API 상태 확인 — 이 파일을 더블클릭하면 됩니다.
# 어떤 API 가 열려 있고 어떤 것이 아직인지 한눈에 보여줍니다.

cd "$(dirname "$0")" || exit 1

./.venv/bin/python - << 'PYEOF'
import json, pathlib, re, time, urllib.parse, urllib.request
from datetime import datetime, timedelta

def read_key():
    """인증키는 설정 파일이 아니라 .env / 환경변수에서 읽습니다."""
    import os
    v = os.environ.get("DATA_GO_KR_KEY", "").strip()
    if v:
        return v
    try:
        for line in pathlib.Path(".env").read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("DATA_GO_KR_KEY="):
                return line.split("=", 1)[1].strip()
    except FileNotFoundError:
        pass
    return ""


def conf():
    """지역 설정은 설정.로컬.json 에 있습니다 (깃에 올리지 않습니다)."""
    try:
        return json.loads(pathlib.Path("설정.로컬.json").read_text(encoding="utf-8"))["공공데이터포털"]
    except Exception:
        return {}


C = conf()
W = C.get("날씨", {})
AIR = C.get("대기질", {}).get("측정소", "중구")
UVA = C.get("자외선", {}).get("지역코드", "1100000000")
STOPS = C.get("버스", {}).get("정류장", [])
ARS = str(STOPS[0]["ARS"]) if STOPS else ""

key = read_key()
if not key:
    raise SystemExit("  인증키를 찾지 못했습니다. .env 파일에 DATA_GO_KR_KEY 를 넣어 주세요.")
now = datetime.now()


def call(url, params, timeout=15):
    """서버가 이따금 응답하지 않아 몇 번 다시 물어봅니다."""
    q = urllib.parse.urlencode({**params, "serviceKey": key}, safe="%")
    last = ""
    for _ in range(3):
        try:
            return urllib.request.urlopen(f"{url}?{q}", timeout=timeout).read().decode("utf-8", "ignore")
        except Exception as e:
            last = str(e)
            if "403" in last or "404" in last:
                break                      # 승인·주소 문제는 다시 물어봐도 같습니다
            time.sleep(2)
    return f"__ERR__{last}"


def verdict(raw):
    """응답을 보고 (열림?, 한 줄 설명) 을 돌려줍니다."""
    if raw.startswith("__ERR__"):
        msg = raw[7:]
        if "403" in msg:
            return False, "아직 신청·승인 전입니다 (403)"
        if "404" in msg:
            return False, "주소가 바뀌었거나 폐기됐습니다 (404)"
        if "504" in msg or "502" in msg or "timed out" in msg:
            return None, "상대 서버가 응답하지 않습니다 (잠시 뒤 다시 해보세요)"
        return False, msg[:52]
    if "NOT_REGISTERED" in raw or "등록되지" in raw:
        return False, "승인 반영 대기 중입니다 (키가 아직 등록 전)"
    if "NO_OPENAPI_SERVICE" in raw:
        return False, "그런 서비스가 없습니다 (주소 확인 필요)"
    if "LIMITED_NUMBER" in raw or "초과" in raw:
        return False, "오늘 사용량을 다 썼습니다"
    if re.search(r'"resultCode"\s*:\s*"0?0"', raw) or "<resultCode>00" in raw or "<headerCd>0<" in raw:
        return True, ""
    m = re.search(r"<headerMsg>(.*?)</headerMsg>|<resultMsg>(.*?)</resultMsg>", raw)
    if m:
        return False, (m.group(1) or m.group(2))[:52]
    return False, raw.strip()[:52]


bd = (now - timedelta(minutes=45))
vil_time = max([h for h in (23, 20, 17, 14, 11, 8, 5, 2) if bd.hour >= h], default=23)
mid_base = now.strftime("%Y%m%d") + "0600" if now.hour >= 7 else \
           (now - timedelta(days=1)).strftime("%Y%m%d") + "1800"
uv_time = now.strftime("%Y%m%d") + f"{(now.hour // 3) * 3:02d}"

CHECKS = [
    ("기상청 단기예보 (지금 기온·시간별)",
     "https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getVilageFcst",
     {"base_date": bd.strftime("%Y%m%d"), "base_time": f"{vil_time:02d}00",
      "nx": W.get("격자X", 60), "ny": W.get("격자Y", 127), "dataType": "JSON", "numOfRows": 1, "pageNo": 1}),

    ("기상청 중기예보 (주간 6일)",
     "https://apis.data.go.kr/1360000/MidFcstInfoService/getMidTa",
     {"regId": "11B10101", "tmFc": mid_base, "dataType": "JSON", "numOfRows": 1, "pageNo": 1}),

    ("기상청 생활기상지수 (자외선)",
     "https://apis.data.go.kr/1360000/LivingWthrIdxServiceV5/getUVIdxV5",
     {"areaNo": UVA, "time": uv_time, "dataType": "JSON", "numOfRows": 1, "pageNo": 1}),

    ("에어코리아 (미세·초미세)",
     "https://apis.data.go.kr/B552584/ArpltnInforInqireSvc/getMsrstnAcctoRltmMesureDnsty",
     {"stationName": AIR, "dataTerm": "DAILY", "returnType": "json",
      "numOfRows": 1, "pageNo": 1, "ver": "1.0"}),

    ("서울시 버스도착정보",
     "http://ws.bus.go.kr/api/rest/stationinfo/getStationByUid",
     {"arsId": ARS}),
]

print()
print("  ──────────────────────────────────────────────────────────")
print(f"   공공데이터 API 상태   {now:%Y-%m-%d %H:%M}")
print("  ──────────────────────────────────────────────────────────")

ok_all = True
for name, url, params in CHECKS:
    ok, why = verdict(call(url, params))
    ok_all &= bool(ok)
    mark = "✅" if ok else ("⚠️ " if ok is None else "⏳")
    print(f"   {mark}  {name}")
    if not ok:
        print(f"       └ {why}")

print()
if ok_all:
    print("   모두 열려 있습니다. 화면에 실제 값이 나옵니다.")
else:
    print("   ⏳ 표시된 것은 승인 반영을 기다리는 중입니다.")
    print("   그동안 화면에는 예시값이 '샘플' 표시와 함께 나오고,")
    print("   열리는 순간 수집기가 알아서 실제 값으로 바꿉니다. (손댈 것 없음)")
print()
PYEOF

echo "  창을 닫으려면 아무 키나 누르세요."
read -r -n 1 -s
