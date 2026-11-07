"""Local HTTP entry point for the payment-agent demo."""
from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
import logging
from pathlib import Path
import threading
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from .sessions import install_live_routes, runtime_identity

LOGGER = logging.getLogger(__name__)
WEB = Path(__file__).parent / "web"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
PUBLIC_ASSETS = frozenset({"/", "/live.html", "/live.js", "/live.css"})


def _default_engine_factory():
    # Import only when starting live inference; HTTP contract tests need no model.
    from sg_guard.runtime import Engine

    return Engine()


def build_app(*, engine_factory=None, data_dir=None):
    """Build a local app. Injecting an engine is reserved for integration tests."""
    state = {"status": "loading", "engine": None, "error": None}
    inference_lock = threading.Lock()
    factory = engine_factory or _default_engine_factory

    def load_model():
        try:
            state["engine"] = factory()
            state["status"] = "ready"
            LOGGER.info("Payment model ready")
        except Exception as exc:
            LOGGER.exception("Could not initialize payment model")
            state.update(status="error", error=f"{type(exc).__name__}: 模型加载失败，请查看本地服务日志。")

    @asynccontextmanager
    async def lifespan(app):
        threading.Thread(target=load_model, daemon=True).start()
        yield

    app = FastAPI(title="SilentGuard Payment Agent", lifespan=lifespan)
    app.state.runtime = state

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        host = request.headers.get("host", "")
        try:
            parsed_host = urlsplit("http://" + host)
            valid_host = (
                parsed_host.hostname in LOOPBACK_HOSTS
                and parsed_host.username is None
                and parsed_host.password is None
                and parsed_host.path == ""
                and not parsed_host.query
                and not parsed_host.fragment
            )
        except ValueError:
            valid_host = False
        if not valid_host:
            return JSONResponse({"detail": "Loopback Host required"}, status_code=400)
        # README links may navigate here from another site. Only immutable public
        # UI assets are exempt; runtime data, sessions and mutations stay local.
        public_asset = request.method == "GET" and request.url.path in PUBLIC_ASSETS
        if not public_asset:
            origin = request.headers.get("origin")
            if origin and origin not in (f"http://{host}", f"https://{host}"):
                return JSONResponse({"detail": "Same-origin requests only"}, status_code=403)
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "Cross-site requests rejected"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/api/health")
    def health():
        return {
            "status": state["status"],
            "error": state["error"],
            "runtime": runtime_identity(state["engine"]) if state["engine"] else None,
            "busy": inference_lock.locked(),
        }

    install_live_routes(app, state, inference_lock, directory=data_dir)

    # Serve only these assets, never a repository or data directory.
    @app.get("/", include_in_schema=False)
    @app.get("/live.html", include_in_schema=False)
    def index():
        return FileResponse(WEB / "live.html", media_type="text/html")

    @app.get("/live.js", include_in_schema=False)
    def javascript():
        return FileResponse(WEB / "live.js", media_type="application/javascript")

    @app.get("/live.css", include_in_schema=False)
    def stylesheet():
        return FileResponse(WEB / "live.css", media_type="text/css")

    return app


def main():
    import uvicorn

    parser = argparse.ArgumentParser(description="Run the local SilentGuard payment demo")
    parser.add_argument("--host", choices=sorted(LOOPBACK_HOSTS), default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    uvicorn.run(build_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
