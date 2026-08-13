"""Start the telemetry API and the operator console.

    python run_api.py                 # http://127.0.0.1:8000
    python run_api.py --port 8080 --reload

That is the whole stack. The console in web/ is plain HTML/CSS/ES modules served
by this same app, so there is no second server, no npm install and no build step
— edit a file in web/ and reload the page.

Equivalent to `uvicorn api.main:app`; this wrapper exists so the service starts
with the repo root on `sys.path` regardless of where it is launched from.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--reload", action="store_true", help="restart on source changes")
    args = ap.parse_args()

    try:
        import uvicorn
    except ImportError:
        print('uvicorn is not installed.  pip install fastapi "uvicorn[standard]"',
              file=sys.stderr)
        return 1

    print(f"X-NioS digital twin  ->  http://{args.host}:{args.port}")
    print(f"  console   http://{args.host}:{args.port}/")
    print(f"  api docs  http://{args.host}:{args.port}/docs")
    uvicorn.run("api.main:app", host=args.host, port=args.port, reload=args.reload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
