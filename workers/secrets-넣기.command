#!/bin/bash
# 지역·정류소 설정을 Cloudflare 에 넣습니다 — 이 파일을 더블클릭하세요.
#
# 저장소가 공개라 근무지가 드러나지 않도록, 이런 값들은 코드에 두지 않고
# Cloudflare 쪽에만 보관합니다. 값은 옆의 '설정.로컬.json' 에서 읽습니다.

cd "$(dirname "$0")" || exit 1

VALUES=$(../.venv/bin/python - << 'PYEOF'
import json, pathlib
c = json.loads(pathlib.Path("../설정.로컬.json").read_text(encoding="utf-8"))["공공데이터포털"]
w, b = c.get("날씨", {}), c.get("버스", {})
stops = b.get("정류장", [])
out = {
    "LOCATION":    w.get("지역명", ""),
    "GRID_X":      str(w.get("격자X", "")),
    "GRID_Y":      str(w.get("격자Y", "")),
    "LAT":         str(w.get("위도", 37.5665)),
    "LON":         str(w.get("경도", 126.9780)),
    "MID_LAND":    w.get("중기육상코드", "11B00000"),
    "MID_TA":      w.get("중기기온코드", "11B10101"),
    "AIR_STATION": c.get("대기질", {}).get("측정소", ""),
    "UV_AREA":     c.get("자외선", {}).get("지역코드", ""),
    "BUS_ARS":     ",".join(str(s["ARS"]) for s in stops),
    "BUS_STID":    json.dumps({str(s["ARS"]): s.get("stId", "") for s in stops if s.get("stId")}, ensure_ascii=False),
    "BUS_PIN":     json.dumps({str(s["ARS"]): s.get("우선노선", []) for s in stops if s.get("우선노선")}, ensure_ascii=False),
}
for k, v in out.items():
    print(f"{k}\t{v}")
PYEOF
)

echo ""
echo "  ─────────────────────────────────────────"
echo "   지역 설정을 Cloudflare 에 넣습니다"
echo "  ─────────────────────────────────────────"

while IFS=$'\t' read -r NAME VALUE; do
  [ -z "$NAME" ] && continue
  printf "   %-12s " "$NAME"
  if printf '%s' "$VALUE" | npx wrangler secret put "$NAME" > /dev/null 2>&1; then
    echo "넣었습니다"
  else
    echo "실패"
  fi
done <<< "$VALUES"

echo ""
echo "   끝났습니다. 창을 닫으려면 아무 키나 누르세요."
read -r -n 1 -s
