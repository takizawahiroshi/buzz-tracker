#!/usr/bin/env python3
"""
バズトレンド探知機 - プロキシサーバー（スレッド対応版）
依存: Python 3.7+ 標準ライブラリのみ

起動:
  python server.py sk-ant-api03-XXXXXXXX
  または
  ANTHROPIC_API_KEY=sk-ant-... python server.py
"""

import os, sys, json, socket, threading
import urllib.request, urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from pathlib import Path

PORT   = 8000
API_URL = "https://api.anthropic.com/v1/messages"
HTML_FILE = Path(__file__).parent / "buzz_tracker.html"
TIMEOUT   = 600   # 10分

# ── スレッド対応HTTPサーバー ──────────────────────────────────
class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """各リクエストを別スレッドで処理する。長時間リクエストが他の接続をブロックしない。"""
    allow_reuse_address = True
    daemon_threads = True   # メインスレッド終了時に子スレッドも終了


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        status = args[1] if len(args) > 1 else "?"
        method = getattr(self, "command", "?")
        print(f"  [{status}] {method} {self.path}  (thread: {threading.current_thread().name})")

    # ── CORS headers ──────────────────────────────────────────
    def cors(self):
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, x-api-key, anthropic-version, anthropic-beta")

    # ── OPTIONS preflight ─────────────────────────────────────
    def do_OPTIONS(self):
        self.send_response(204)
        self.cors()
        self.end_headers()

    # ── GET: HTMLファイルを配信 ───────────────────────────────
    def do_GET(self):
        if self.path.split("?")[0] in ("/", "/buzz_tracker.html"):
            try:
                data = HTML_FILE.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type",   "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.cors()
                self.end_headers()
                self.wfile.write(data)
            except FileNotFoundError:
                self._err(404, "buzz_tracker.html が見つかりません。server.py と同じフォルダに置いてください。")
        else:
            self._err(404, "Not found")

    # ── POST /proxy: Anthropic API に転送 ─────────────────────
    def do_POST(self):
        if self.path != "/proxy":
            self._err(404, "Not found")
            return

        # APIキー取得
        key = _API_KEY or os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            self._json_err(401, {
                "error": {
                    "type": "auth_error",
                    "message": "APIキーが設定されていません。"
                                "起動時の引数で渡してください: python server.py sk-ant-YOUR_KEY"
                }
            })
            return

        # リクエストボディ読み込み
        try:
            length = int(self.headers.get("Content-Length", 0))
            body   = self.rfile.read(length)
        except Exception as e:
            self._json_err(400, {"error": {"type": "bad_request", "message": f"リクエスト読み込みエラー: {e}"}})
            return

        # リクエストの model / max_tokens をログに出す
        try:
            req_json = json.loads(body)
            model    = req_json.get("model", "?")
            tools    = [t.get("type","?") for t in req_json.get("tools", [])]
            print(f"  → model={model}  tools={tools}  timeout={TIMEOUT}s")
        except Exception:
            pass

        # Anthropic API へ転送
        api_req = urllib.request.Request(
            API_URL,
            data    = body,
            headers = {
                "Content-Type":      "application/json",
                "x-api-key":         key,
                "anthropic-version": "2023-06-01",
                "anthropic-beta":    "web-search-2025-03-05",
            },
            method = "POST",
        )

        try:
            with urllib.request.urlopen(api_req, timeout=TIMEOUT) as resp:
                resp_body = resp.read()

            print(f"  ← OK ({len(resp_body):,} bytes)")
            # JSON の中身をプレビュー表示（デバッグ用）
            try:
                resp_json = json.loads(resp_body)
                for block in resp_json.get("content", []):
                    if block.get("type") == "text":
                        preview = block["text"][:200].replace("\n", " ")
                        print(f"  [text preview] {preview}")
                        break
            except Exception:
                pass
            self.send_response(200)
            self.send_header("Content-Type",   "application/json")
            self.send_header("Content-Length", str(len(resp_body)))
            self.cors()
            self.end_headers()
            self.wfile.write(resp_body)

        except urllib.error.HTTPError as e:
            body_err = e.read()
            print(f"  ✗ HTTP {e.code}: {body_err[:200]}")
            self.send_response(e.code)
            self.send_header("Content-Type",   "application/json")
            self.send_header("Content-Length", str(len(body_err)))
            self.cors()
            self.end_headers()
            self.wfile.write(body_err)

        except (socket.timeout, TimeoutError):
            msg = (f"タイムアウト ({TIMEOUT}秒)。"
                   "クエリ数を3〜4件以下に減らして再試行してください。")
            print(f"  ✗ Timeout after {TIMEOUT}s")
            self._json_err(504, {"error": {"type": "timeout", "message": msg}})

        except ConnectionResetError:
            print("  ✗ 接続がリセットされました（クライアントが切断）")

        except BrokenPipeError:
            print("  ✗ 接続が切断されました（BrokenPipe）")

        except Exception as e:
            print(f"  ✗ プロキシエラー: {type(e).__name__}: {e}")
            self._json_err(500, {"error": {"type": "server_error", "message": str(e)}})

    # ── エラー送信ヘルパー ────────────────────────────────────
    def _err(self, code: int, message: str):
        body = message.encode()
        self.send_response(code)
        self.send_header("Content-Type",   "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.cors()
        self.end_headers()
        self.wfile.write(body)

    def _json_err(self, code: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type",   "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.cors()
        self.end_headers()
        self.wfile.write(body)


# ── エントリーポイント ────────────────────────────────────────
_API_KEY = ""

if __name__ == "__main__":
    # コマンドライン引数からAPIキーを取得
    for arg in sys.argv[1:]:
        if arg.startswith("sk-"):
            _API_KEY = arg
            break

    if not _API_KEY:
        _API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

    print()
    print("  ╔═══════════════════════════════════════════╗")
    print("  ║     バズトレンド探知機  プロキシサーバー   ║")
    print("  ╚═══════════════════════════════════════════╝")
    print()

    if _API_KEY:
        masked = _API_KEY[:16] + "..." + _API_KEY[-4:]
        print(f"  APIキー    : {masked}  ✓")
    else:
        print("  ⚠  APIキーが設定されていません。")
        print("  起動例: python server.py sk-ant-api03-XXXXXXXX")
        print()

    print(f"  スレッド   : マルチスレッド対応（並列リクエスト可）")
    print(f"  タイムアウト: {TIMEOUT}秒")
    print(f"  URL        : http://localhost:{PORT}")
    print(f"  停止       : Ctrl+C")
    print()

    # HTML ファイル確認
    if not HTML_FILE.exists():
        print(f"  ⚠  buzz_tracker.html が見つかりません。")
        print(f"     このスクリプトと同じフォルダに置いてください: {HTML_FILE.parent}")
        print()

    server = ThreadedHTTPServer(("", PORT), Handler)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  サーバーを停止しました。")
    finally:
        server.server_close()
