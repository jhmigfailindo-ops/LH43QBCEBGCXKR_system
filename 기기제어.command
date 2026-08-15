#!/bin/bash
# 사이니지 기기 제어 — 이 파일을 더블클릭하면 메뉴가 뜹니다.
# 리모컨 없이 맥에서 전원·볼륨·입력을 바꿀 수 있습니다.

cd "$(dirname "$0")" || exit 1

MDC="./.venv/bin/samsung-mdc"
TARGET="1@192.168.0.15:1515"      # 기기 주소가 바뀌면 여기를 고치세요

run() { $MDC -t 8 "$TARGET" "$@" 2>&1 | sed "s|.*1515 ||"; }

while true; do
  echo ""
  echo "  ──────────────────────────────────────────"
  echo "   사이니지 기기 제어"
  echo "  ──────────────────────────────────────────"
  echo "   1) 지금 상태 보기"
  echo "   2) 볼륨 바꾸기"
  echo "   3) 음소거 켜기 / 끄기"
  echo "   4) 화면 끄기 (전원)"
  echo "   5) 화면 켜기"
  echo "   6) 다시 시작 (재부팅)"
  echo "   7) 입력을 사이니지 화면으로 되돌리기"
  echo "   0) 끝내기"
  echo ""
  read -r -p "   번호를 고르세요 > " sel

  case "$sel" in
    1) echo ""; echo "   전원/볼륨/음소거/입력:"; run status ;;
    2) read -r -p "   볼륨 (0~100) > " v; run volume "$v" ;;
    3) read -r -p "   음소거 (ON / OFF) > " m; run mute "$(echo "$m" | tr '[:lower:]' '[:upper:]')" ;;
    4) run power OFF ;;
    5) run power ON ;;
    6) read -r -p "   정말 재시작할까요? (y) > " y; [ "$y" = "y" ] && run power REBOOT ;;
    7) run input_source URL_LAUNCHER ;;
    0) echo ""; exit 0 ;;
    *) echo "   없는 번호입니다" ;;
  esac
done
