import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..game.database import Database
from ..game.engine import GameEngine
from ..game.hex_names import hex_name
from ..radio.adapter import RadioAdapter
from .auth import AuthMiddleware
from .routes import router
from .multiplayer_routes import router as multiplayer_router

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"


def create_app(engine: GameEngine, db: Database, config: dict, radio: RadioAdapter | None = None, multiplayer_manager=None) -> FastAPI:
    app = FastAPI(title="LoRa the Explorer", docs_url=None, redoc_url=None)

    app.state.engine = engine
    app.state.db = db
    app.state.config = config
    app.state.radio = radio
    app.state.multiplayer_manager = multiplayer_manager
    app.state.templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    app.state.templates.env.globals["hex_name"] = hex_name

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        return FileResponse(STATIC_DIR / "icon-192.png", media_type="image/png")

    @app.get("/apple-touch-icon.png", include_in_schema=False)
    async def apple_touch_icon():
        return FileResponse(STATIC_DIR / "icon-180.png", media_type="image/png")

    @app.get("/apple-touch-icon-precomposed.png", include_in_schema=False)
    async def apple_touch_icon_precomposed():
        return FileResponse(STATIC_DIR / "icon-180.png", media_type="image/png")

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.add_middleware(AuthMiddleware)
    app.include_router(router)
    app.include_router(multiplayer_router)

    @app.exception_handler(404)
    async def not_found(request: Request, exc):
        return HTMLResponse(
            app.state.templates.get_template("404.html").render(
                {"request": request, "nav_active": ""}
            ),
            status_code=404,
        )

    return app
