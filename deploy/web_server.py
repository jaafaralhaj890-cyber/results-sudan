# -*- coding: utf-8 -*-
"""
خادم ويب فائق السرعة ومحلي للاستعلام عن نتائج الشهادة السودانية.
يستخدم قاعدة بيانات SQLite مفهرسة للرد على أي استعلام في أقل من 2 ميلي ثانية.
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import os
import re
import sqlite3
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "results.db")
WEB_DIR = os.path.join(BASE_DIR, "web")


def ensure_db():
    if not os.path.exists(DB_PATH) or os.path.getsize(DB_PATH) == 0:
        gz_path = os.path.join(BASE_DIR, "results.db.gz")
        part1 = os.path.join(BASE_DIR, "results.db.gz.001")
        part2 = os.path.join(BASE_DIR, "results.db.gz.002")
        print("📦 جاري فك ضغط قاعدة البيانات لأول مرة...")
        if os.path.exists(part1) and os.path.exists(part2):
            buf = io.BytesIO()
            with open(part1, "rb") as f1:
                buf.write(f1.read())
            with open(part2, "rb") as f2:
                buf.write(f2.read())
            buf.seek(0)
            with gzip.GzipFile(fileobj=buf) as gz:
                with open(DB_PATH, "wb") as f_out:
                    f_out.write(gz.read())
        elif os.path.exists(gz_path):
            with gzip.open(gz_path, "rb") as f_in:
                with open(DB_PATH, "wb") as f_out:
                    f_out.write(f_in.read())
        print(f"✅ تم تجهيز قاعدة البيانات بنجاح: {DB_PATH}")


ensure_db()


def normalize_arabic(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"[\u064B-\u065F\u0640]", "", text)
    text = re.sub(r"[إأآا]", "ا", text)
    text = re.sub(r"[ة]", "ه", text)
    text = re.sub(r"[ى]", "ي", text)
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\bعبد\s+", "عبد", text)
    text = re.sub(r"\b(سيف|نور|عز|تاج|شمس|ضياء|محي|نجم)\s+الدين\b", r"\1الدين", text)
    return re.sub(r"\s+", " ", text).strip()


class ResultDB:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path

    def get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def search_by_seat(self, seat: int) -> dict | None:
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
            SELECT seat, name, pct, result, rank_national, percentile_all, percentile_pass
            FROM students
            WHERE seat = ?
            """, (seat,))
            row = cur.fetchone()
            if row:
                return {
                    "seat": row["seat"],
                    "name": row["name"],
                    "pct": row["pct"],
                    "result": row["result"],
                    "rank": row["rank_national"],
                    "percentile_all": row["percentile_all"],
                    "percentile_pass": row["percentile_pass"],
                }
        return None

    def search_by_name(self, name_query: str, limit: int = 50) -> list[dict]:
        norm = normalize_arabic(name_query)
        if not norm:
            return []

        tokens = [t for t in norm.split() if t]
        if not tokens:
            return []

        p_full = f"{norm}%"
        p_first = f"{tokens[0]} %"
        p_two = f"{tokens[0]} {tokens[1]}%" if len(tokens) >= 2 else p_full

        with self.get_connection() as conn:
            cur = conn.cursor()

            # 1. محاولة البحث السريع عبر FTS5 مع إعطاء الأولوية لمن يبدأ اسمه بالمدخلات
            fts_match = " AND ".join(f'"{t}"*' for t in tokens)
            try:
                cur.execute(f"""
                SELECT s.seat, s.name, s.pct, s.result, s.rank_national, s.percentile_all, s.percentile_pass,
                  CASE
                    WHEN s.name_normalized LIKE ? THEN 1
                    WHEN s.name_normalized LIKE ? THEN 2
                    WHEN s.name_normalized LIKE ? THEN 3
                    ELSE 4
                  END AS relevance
                FROM students s
                JOIN students_fts f ON s.seat = f.rowid
                WHERE students_fts MATCH ?
                ORDER BY relevance ASC, s.pct DESC
                LIMIT ?
                """, (p_full, p_two, p_first, fts_match, limit))
                rows = cur.fetchall()
                if rows:
                    return [
                        {
                            "seat": r["seat"],
                            "name": r["name"],
                            "pct": r["pct"],
                            "result": r["result"],
                            "rank": r["rank_national"],
                            "percentile_all": r["percentile_all"],
                            "percentile_pass": r["percentile_pass"],
                        }
                        for r in rows
                    ]
            except Exception:
                pass

            # 2. خطة بديلة (Fallback) باستخدام LIKE مع ترتيب الأولوية
            like_clauses = " AND ".join(["name_normalized LIKE ?"] * len(tokens))
            params = [p_full, p_two, p_first] + [f"%{t}%" for t in tokens] + [limit]
            cur.execute(f"""
            SELECT seat, name, pct, result, rank_national, percentile_all, percentile_pass,
              CASE
                WHEN name_normalized LIKE ? THEN 1
                WHEN name_normalized LIKE ? THEN 2
                WHEN name_normalized LIKE ? THEN 3
                ELSE 4
              END AS relevance
            FROM students
            WHERE {like_clauses}
            ORDER BY relevance ASC, pct DESC
            LIMIT ?
            """, params)
            rows = cur.fetchall()
            return [
                {
                    "seat": r["seat"],
                    "name": r["name"],
                    "pct": r["pct"],
                    "result": r["result"],
                    "rank": r["rank_national"],
                    "percentile_all": r["percentile_all"],
                    "percentile_pass": r["percentile_pass"],
                }
                for r in rows
            ]

    def get_stats(self) -> dict:
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*), AVG(pct) FROM students")
            total, avg_pct = cur.fetchone()
            cur.execute("SELECT COUNT(*) FROM students WHERE result = 'نجاح' OR pct >= 50.0")
            passed = cur.fetchone()[0]
            cur.execute("SELECT seat, name, pct FROM students ORDER BY pct DESC LIMIT 1")
            top = cur.fetchone()

            return {
                "total_students": total,
                "passed_students": passed,
                "passing_rate": round((passed / total * 100), 2) if total else 0,
                "avg_percentage": round(avg_pct, 2) if avg_pct else 0,
                "top_student": {
                    "seat": top["seat"],
                    "name": top["name"],
                    "pct": top["pct"],
                } if top else None,
            }


db = ResultDB()


class ExamRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        query_params = urllib.parse.parse_qs(parsed_url.query)

        if path == "/api/search":
            self.handle_api_search(query_params)
        elif path == "/api/stats":
            self.handle_api_stats()
        elif path == "/" or path == "/index.html":
            self.serve_file("index.html", "text/html; charset=utf-8")
        else:
            rel_path = os.path.normpath(path.lstrip("/")).replace("\\", "/")
            file_path = os.path.join(WEB_DIR, rel_path)
            if not os.path.abspath(file_path).startswith(os.path.abspath(WEB_DIR)):
                self.send_error(403, "Forbidden")
                return
            if os.path.exists(file_path) and os.path.isfile(file_path):
                ext = os.path.splitext(file_path)[1].lower()
                mime = {
                    ".html": "text/html; charset=utf-8",
                    ".css": "text/css; charset=utf-8",
                    ".js": "application/javascript; charset=utf-8",
                    ".png": "image/png",
                    ".jpg": "image/jpeg",
                    ".jpeg": "image/jpeg",
                    ".svg": "image/svg+xml",
                    ".ico": "image/x-icon",
                }.get(ext, "application/octet-stream")
                self.serve_file(rel_path, mime)
            else:
                self.send_error(404, "Page Not Found")

    def handle_api_search(self, params: dict) -> None:
        q = params.get("q", [""])[0].strip()
        limit = min(int(params.get("limit", [50])[0]), 200)

        if not q:
            self.send_json_response({"error": "يرجى إدخال اسم أو رقم جلوس", "results": []}, status=400)
            return

        if q.isdigit():
            seat = int(q)
            student = db.search_by_seat(seat)
            results = [student] if student else []
            search_type = "seat"
        else:
            results = db.search_by_name(q, limit=limit)
            search_type = "name"

        self.send_json_response({
            "query": q,
            "type": search_type,
            "count": len(results),
            "results": results,
        })

    def handle_api_stats(self) -> None:
        stats = db.get_stats()
        self.send_json_response(stats)

    def serve_file(self, filename: str, content_type: str) -> None:
        file_path = os.path.join(WEB_DIR, filename)
        if not os.path.exists(file_path) or not os.path.isfile(file_path):
            self.send_error(404, "File not found")
            return

        with open(file_path, "rb") as f:
            content = f.read()

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "public, max-age=3600")
        self.end_headers()
        self.wfile.write(content)

    def send_json_response(self, data: dict, status: int = 200) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args) -> None:
        # كتم تسجيل الطلبات الروتينية لعدم إغراق شاشة الطرفية
        pass


def run_server(port: int = 8080) -> None:
    server_address = ("0.0.0.0", port)
    httpd = ThreadingHTTPServer(server_address, ExamRequestHandler)
    print("=" * 65)
    print(f"🚀 تم تشغيل خادم نتائج الشهادة السودانية بنجاح!")
    print(f"🌐 افتح المتصفح على الرابط: http://localhost:{port}")
    print(f"📱 متاح عبر الشبكة المحلية على منفذ: {port}")
    print(f"⚡ قاعدة البيانات المحملة: {DB_PATH}")
    print("=" * 65)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n⏹️ تم إيقاف الخادم.")
    finally:
        httpd.server_close()


def main() -> int:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="خادم ويب محلي لنتائج الشهادة السودانية")
    parser.add_argument("--port", "-p", type=int, default=8080, help="منفذ الخادم (الافتراضي: 8080)")
    args = parser.parse_args()

    run_server(port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
