#!/usr/bin/env python3
"""Contract check: every API path the frontend calls must exist in the backend.

Static parse of the FastAPI route decorators, correctly resolving multi-line
decorators and router prefixes (including server.py's router mounted at both
``/api/v1`` and the root). Run:  python scripts/check_frontend_endpoints.py
"""
from __future__ import annotations
import re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VIEWS = ROOT / "frontend" / "src" / "views"
API = ROOT / "doctoragent" / "api"
ROOT_PREFIX = "/api/v1"

def frontend_calls():
    calls = []
    for vue in VIEWS.glob("*.vue"):
        src = vue.read_text(encoding="utf-8")
        for m in re.finditer(r'api\.(get|post|put|delete)\("([^"]+)"', src):
            calls.append((vue.name, m.group(1).upper(), m.group(2)))
        for m in re.finditer(r'fetch\("([^"]+)"', src):
            calls.append((vue.name, "GET", m.group(1)))
    return calls

def backend_routes():
    routes = {}  # path -> set(methods)
    def add(prefix, method, path):
        p = prefix + path
        p = re.sub(r'/\{[^}]+\}', '/*', p)
        routes.setdefault(p, set()).add(method)
    # server.py: main `router` mounted at both /api/v1 and root
    for py in API.rglob("*.py"):
        src = py.read_text(encoding="utf-8", errors="ignore")
        # multi-line decorators on `router` or `app`
        for m in re.finditer(r'@(?:router|app)\.(get|post|put|delete)\(\s*"([^"]+)"', src):
            add("", m.group(1).upper(), m.group(2))
        # router prefix within this file
        pm = re.search(r'APIRouter\([^)]*prefix="([^"]+)"', src)
        file_prefix = pm.group(1) if pm else ""
        if file_prefix:
            for m in re.finditer(r'@router\.(get|post|put|delete)\(\s*"([^"]+)"', src):
                add(file_prefix, m.group(1).upper(), m.group(2))
        else:
            # routers w/o explicit prefix but included with prefix=... ; the
            # server.py `router` is mounted at /api/v1 too
            for m in re.finditer(r'@router\.(get|post|put|delete)\(\s*"([^"]+)"', src):
                if py.name == "server.py":
                    add(ROOT_PREFIX, m.group(1).upper(), m.group(2))
    return routes

def norm(p): return re.sub(r'/\{[^}]+\}', '/*', p.split("?")[0])
def match(route, path): return route == path or (route.endswith("/*") and path.startswith(route[:-2]))

def main():
    routes = backend_routes()
    calls = frontend_calls()
    problems = []
    for src_name, method, raw in calls:
        p = norm(raw)
        found = any(match(r, p) for r in routes)
        if not found:
            problems.append(f"{src_name}: {method} {raw}  ->  NOT FOUND")
        else:
            methods = {mt for r, ms in routes.items() if match(r, p) for mt in ms}
            if methods and method not in methods and "GET" not in methods:
                problems.append(f"{src_name}: {method} {raw}  ->  backend allows {sorted(methods)}")
    if problems:
        print(f"❌ {len(problems)} mismatch(es):")
        for x in problems: print("  ", x)
        return 1
    print(f"✅ {len(calls)} frontend API calls match backend routes ({len(routes)} routes)")
    return 0

if __name__ == "__main__":
    sys.exit(main())
