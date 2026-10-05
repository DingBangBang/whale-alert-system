"""Daily run report for the incremental whale pipeline.

Runs ONE incremental scan (``whale_alert.py --once``), summarises the outcome from
SQLite, writes an HTML report to ``reports/daily_report_YYYYMMDD.html``, raises a
macOS notification and (on success) opens the report in the browser.

The whole flow is wrapped in ``try/except`` so a failed run (API quota exhausted,
network down, …) still produces a failure report and a failure notification —
it never fails silently.

Usage
-----
python scripts/daily_report.py
"""
from __future__ import annotations

import datetime as dt
import html
import os
import sqlite3
import subprocess
import sys
import time
import traceback
import urllib.request
import webbrowser
from pathlib import Path

# Make the project root importable when run as `python scripts/daily_report.py`.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402

GRAFANA_URL = os.getenv("GRAFANA_URL", "http://localhost:3001")
REPORTS_DIR = ROOT / "reports"

TITLE_OK = "Whale Alert 每日运行报告"
TITLE_WARN = "Whale Alert 每日运行报告 ⚠️"


# --- helpers -----------------------------------------------------------------
def _applescript_string(text: str) -> str:
    """Escape a Python string so it can be embedded in an AppleScript literal."""
    return '"' + str(text).replace("\\", "\\\\").replace('"', '\\"') + '"'


def notify(message: str, title: str = TITLE_OK) -> None:
    """Best-effort macOS notification via ``osascript`` (no-op on other OSes)."""
    if sys.platform != "darwin":
        return
    try:
        script = (
            f"display notification {_applescript_string(message)} "
            f"with title {_applescript_string(title)}"
        )
        subprocess.run(["osascript", "-e", script], check=False, timeout=10)
    except Exception:  # pragma: no cover - notification must never break the run
        pass


def _db_path() -> str:
    return os.getenv("WHALE_DB_PATH", config.DB_PATH)


def _query(sql: str, default=None):
    """Run a scalar SQL query against the whale DB (returns ``default`` on error)."""
    try:
        conn = sqlite3.connect(_db_path())
        try:
            row = conn.execute(sql).fetchone()
            return row[0] if row else default
        finally:
            conn.close()
    except Exception:
        return default


def _grafana_ok() -> bool:
    """True if the Grafana HTTP endpoint responds (dashboard can render new data)."""
    try:
        url = GRAFANA_URL.rstrip("/") + "/api/health"
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


def _write_html(report_path: Path, ctx: dict) -> None:
    rows = "\n".join(
        f"    <tr><th>{html.escape(k)}</th><td>{html.escape(str(v))}</td></tr>"
        for k, v in ctx["fields"].items()
    )
    ok = ctx["ok"]
    badge = "✅ 成功" if ok else "❌ 失败"
    color = "#16a34a" if ok else "#dc2626"
    error_block = ""
    if not ok and ctx.get("error"):
        error_block = "<h3>错误详情</h3>\n<pre>" + html.escape(ctx["error"]) + "</pre>"
    report_path.write_text(
        f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>Whale Alert 每日运行报告 · {ctx['date']}</title>
<style>
 body{{font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;max-width:820px;margin:40px auto;padding:0 20px;color:#0f172a}}
 h1{{font-size:22px}} .badge{{color:{color};font-weight:700}}
 table{{border-collapse:collapse;width:100%;margin:18px 0}}
 th,td{{text-align:left;padding:9px 12px;border-bottom:1px solid #e2e8f0;font-size:14px}}
 th{{width:240px;color:#475569;background:#f8fafc}}
 pre{{background:#0f172a;color:#e2e8f0;padding:14px;border-radius:8px;overflow:auto;font-size:12px}}
 a{{color:#2563eb}} .muted{{color:#64748b;font-size:12px}}
</style></head><body>
<h1>🐋 Whale Alert 每日运行报告 <span class="badge">{badge}</span></h1>
<p class="muted">日期：{ctx['date']} · 生成时间：{ctx['generated_at']}</p>
<table>
{rows}
</table>
{error_block}
<p>📊 Grafana 看板：<a href="{GRAFANA_URL}">{GRAFANA_URL}</a></p>
</body></html>
""",
        encoding="utf-8",
    )


def main() -> int:
    started = time.time()
    date_str = dt.datetime.now().strftime("%Y%m%d")
    generated_at = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORTS_DIR / f"daily_report_{date_str}.html"

    before = _query("SELECT COUNT(*) FROM whale_transfers", default=0) or 0
    ctx: dict = {"date": date_str, "generated_at": generated_at, "ok": False, "error": ""}
    fetch_ok = append_ok = False

    try:
        # 1) Run one incremental scan.
        proc = subprocess.run(
            [sys.executable, str(ROOT / "whale_alert.py"), "--once"],
            cwd=str(ROOT), capture_output=True, text=True,
        )
        combined = (proc.stdout or "") + (proc.stderr or "")
        scan_failed = proc.returncode != 0
        fetch_ok = (not scan_failed) and ("Scanning blocks" in combined)
        if scan_failed:
            last_line = combined.strip().splitlines()[-1] if combined.strip() else \
                f"whale_alert.py exited with code {proc.returncode}"
            raise RuntimeError(last_line)

        # 2) Collect post-run state from SQLite.
        after = _query("SELECT COUNT(*) FROM whale_transfers", default=before) or before
        added = max(after - before, 0)
        append_ok = after >= before  # table still readable & count did not shrink
        last_block = _query("SELECT last_scanned_block FROM scan_state WHERE id=1", default="?")
        latest_ts = _query("SELECT MAX(timestamp) FROM whale_transfers")
        latest_str = (
            dt.datetime.fromtimestamp(int(latest_ts)).strftime("%Y-%m-%d %H:%M:%S")
            if latest_ts else "N/A"
        )
        grafana_ok = _grafana_ok()

        ctx["ok"] = True
        ctx["fields"] = {
            "运行状态": "✅ 成功",
            "链上拉取（Etherscan）": "✅ 成功" if fetch_ok else "❌ 失败",
            "累积表写入（whale_transfers）": "✅ 成功" if append_ok else "❌ 失败",
            "Grafana 刷新（3001）": "✅ 可访问" if grafana_ok else "⚠️ 不可访问",
            "运行时长": f"{round(time.time() - started, 1)} 秒",
            "本次新增条数": added,
            "最新记录时间戳": latest_str,
            "总记录数": after,
            "last_scanned_block": last_block,
        }
        _write_html(report_path, ctx)

        notify(f"新增{added}条，总记录{after}条", title=TITLE_OK)
        try:
            webbrowser.open(f"file://{report_path}")
        except Exception:
            pass
        print(f"[daily_report] OK -> {report_path} (added={added}, total={after})")
        return 0

    except Exception as exc:  # noqa: BLE001 - failures must be reported, not raised
        reason = str(exc).strip() or exc.__class__.__name__
        after = _query("SELECT COUNT(*) FROM whale_transfers", default=before) or before
        ctx["ok"] = False
        ctx["error"] = traceback.format_exc()
        ctx["fields"] = {
            "运行状态": "❌ 失败",
            "链上拉取（Etherscan）": "✅ 成功" if fetch_ok else "❌ 失败",
            "累积表写入（whale_transfers）": "✅ 成功" if append_ok else "❌ 失败",
            "Grafana 刷新（3001）": "✅ 可访问" if _grafana_ok() else "⚠️ 不可访问",
            "运行时长": f"{round(time.time() - started, 1)} 秒",
            "本次新增条数": max(after - before, 0),
            "最新记录时间戳": "N/A",
            "总记录数": after,
            "last_scanned_block": "N/A",
            "失败原因": reason,
        }
        try:
            _write_html(report_path, ctx)
        except Exception:
            pass
        notify(f"今日运行失败：{reason}", title=TITLE_WARN)
        print(f"[daily_report] FAILED: {reason}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
