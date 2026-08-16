#!/usr/bin/env python3
"""
콘텐츠 서버
───────────
사이니지가 화면 파일과 사진을 가져가는 웹서버입니다.

파이썬 기본 서버(python -m http.server)는 캐시 헤더를 주지 않아
사진을 매번 다시 받아옵니다. 그러면 화면을 펼칠 때 사진만 늦게 나타납니다.
여기서는 사진에 캐시를 허용해 한 번 받은 것은 다시 받지 않게 합니다.

실행
  ./.venv/bin/python 서버.py            (기본 8899 포트)
  ./.venv/bin/python 서버.py 9000       (포트 지정)

중지: Ctrl+C
"""

import functools
import http.server
import socket
import socketserver
import sys
import time
import urllib.parse
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).parent / "콘텐츠"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8899


# 화면에서 "지금부터 자주 갱신해줘" 라고 알릴 때 쓰는 표시 파일.
# 수집기가 이 파일의 시각을 보고 버스 갱신 주기를 바꿉니다.
BOOST = Path(__file__).parent / "콘텐츠" / ".버스집중"
ONCE = Path(__file__).parent / "콘텐츠" / ".버스지금"      # "지금 한 번만 받아와" 신호


class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        # 리모컨 확인 버튼이 눌리면 화면이 이 주소를 부릅니다.
        if self.path.split("?")[0] == "/bus-boost":
            # 무엇을 집중해서 볼지 화면이 함께 알려줍니다.
            #   all            정류소 전체를 다시 조회
            #   ars:<ARS번호>   그 정류소 하나만
            #   route:<노선>@<ARS>  그 정류소의 그 노선 하나만
            #   off            집중조회 그만두기
            #   once           지금 한 번만 정류소 전체 갱신
            q = urllib.parse.parse_qs(urlparse(self.path).query)
            target = (q.get("target") or ["all"])[0][:40]
            try:
                if target == "once":                # 지금 한 번만 전체 갱신
                    ONCE.write_text(str(int(time.time())), encoding="utf-8")
                elif target == "off":               # 집중조회 그만두기
                    BOOST.unlink(missing_ok=True)
                else:
                    BOOST.write_text(f"{int(time.time())}\n{target}", encoding="utf-8")
                body = ('{"ok":true,"target":"%s"}' % target).encode()
            except Exception:
                body = b'{"ok":false}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def end_headers(self):
        path = self.path.split("?")[0].lower()

        if path.startswith("/img/") or path.endswith((".jpg", ".jpeg", ".png")):
            # 사진은 내용이 바뀌면 파일명(해시)도 바뀌므로 오래 캐시해도 안전합니다
            self.send_header("Cache-Control", "public, max-age=86400")
        elif path.endswith(".js"):
            # 데이터는 10분마다 갱신되므로 짧게만 캐시합니다
            self.send_header("Cache-Control", "public, max-age=60")
        else:
            # 화면 파일은 항상 최신을 받아야 합니다
            self.send_header("Cache-Control", "no-cache")

        super().end_headers()

    def log_message(self, fmt, *args):
        # 접속 로그는 남기지 않습니다 (사이니지가 계속 요청해 시끄럽습니다)
        pass


def local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def main():
    if not ROOT.exists():
        sys.exit(f"콘텐츠 폴더가 없습니다: {ROOT}")

    handler = functools.partial(Handler, directory=str(ROOT))
    socketserver.TCPServer.allow_reuse_address = True

    with socketserver.ThreadingTCPServer(("0.0.0.0", PORT), handler) as httpd:
        ip = local_ip()
        print(f"\n  화면 주소   http://{ip}:{PORT}/signage.html")
        print(f"  진단 주소   http://{ip}:{PORT}/diag.html")
        print(f"  리모컨 테스트 http://{ip}:{PORT}/remote-test.html")
        print(f"\n  사진은 하루, 데이터는 1분간 캐시합니다.")
        print(f"  버스 집중 갱신 신호  http://{ip}:{PORT}/bus-boost")
        print(f"  중지하려면 Ctrl+C\n")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n  서버를 멈춥니다.\n")


if __name__ == "__main__":
    main()
