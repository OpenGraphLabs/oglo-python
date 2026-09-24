"""Optional localhost HTTP adapter for :mod:`oglo.collection`."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import Request

from .studio import Collection, _episode_video_path


def create_app(root: Path | str, studio: Collection | None = None):
    """Create the optional FastAPI app without making web dependencies mandatory."""
    from contextlib import asynccontextmanager
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import FileResponse, Response
    from starlette.concurrency import run_in_threadpool
    from urllib.parse import urlparse

    service = studio or Collection(Path(root))

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            await run_in_threadpool(service.close)

    app = FastAPI(title="OGLO Studio", lifespan=lifespan)
    static = Path(__file__).with_name("studio_web")

    @app.middleware("http")
    async def same_origin(request: Request, next_handler):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc != request.headers.get("host"):
                return Response(status_code=403, content="cross-origin control is disabled")
        return await next_handler(request)

    def call(action, *args):
        try:
            return action(*args)
        except (ValueError, RuntimeError, OSError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    def source_of(body: dict) -> str:
        source = body.get("source", "onscreen")
        if type(source) is not str or source not in {"onscreen", "keyboard", "focus_loss", "external"}:
            raise HTTPException(status_code=422, detail="invalid input source")
        return source

    def input_event_of(body: dict) -> dict | None:
        event = body.get("input_event")
        if event is None:
            return None
        if (type(event) is not dict or set(event) - {"control_id", "host_received_ns"}
            or type(event.get("control_id")) is not str
            or not 1 <= len(event["control_id"]) <= 64
            or (event.get("host_received_ns") is not None and
                (type(event["host_received_ns"]) is not int or event["host_received_ns"] < 0))):
            raise HTTPException(status_code=422, detail="invalid button input event")
        return event

    async def json_object(request: Request) -> dict:
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=422, detail="expected a JSON object") from exc
        if type(body) is not dict:
            raise HTTPException(status_code=422, detail="expected a JSON object")
        return body

    @app.get("/")
    def home():
        return FileResponse(static / "index.html")

    @app.get("/studio.js")
    def script():
        return FileResponse(static / "studio.js", media_type="text/javascript")

    @app.get("/studio.css")
    def style():
        return FileResponse(static / "studio.css", media_type="text/css")

    @app.get("/guide")
    def guide():
        return FileResponse(static / "guide.html", media_type="text/html")

    @app.get("/api/status")
    def status():
        return service.status()

    @app.get("/api/live")
    def live():
        return service.live()

    @app.get("/api/devices")
    async def devices():
        return await run_in_threadpool(call, service.devices)

    @app.post("/api/connect")
    async def connect(request: Request):
        body = await json_object(request)
        index = body.get("camera_index", 0)
        if type(index) is not int or not 0 <= index <= 32:
            raise HTTPException(status_code=422, detail="invalid camera selection")
        camera_mode = body.get("camera_mode", "default")
        camera_name = body.get("camera_name")
        if camera_mode not in {"default", "ovision_left", "ovision_right",
                               "ovision_native_left", "ovision_native_right"} or (
            camera_mode != "default" and (type(camera_name) is not str or not camera_name
                                          or len(camera_name) > 200)
        ):
            raise HTTPException(status_code=422, detail="invalid camera mode")
        left_port = body.get("left_port")
        right_port = body.get("right_port")
        if any(port is not None and (type(port) is not str or not port or len(port) > 256)
               for port in (left_port, right_port)):
            raise HTTPException(status_code=422, detail="invalid glove selection")
        if body.get("pair", True) is not True or body.get("serial") is not None:
            raise HTTPException(status_code=422, detail="Studio requires both left and right OGLO gloves")
        return await run_in_threadpool(call, service.connect, index, left_port, right_port,
                                       camera_mode, camera_name)

    @app.post("/api/calibrate")
    async def calibrate(request: Request):
        body = await json_object(request)
        threshold = body.get("threshold")
        if threshold is not None and (type(threshold) is not int or not 0 <= threshold <= 500):
            raise HTTPException(status_code=422, detail="calibration threshold must be 0..500")
        return await run_in_threadpool(call, service.calibrate, threshold)

    @app.post("/api/start")
    async def start(request: Request):
        body = await json_object(request)
        task = body.get("task")
        if type(task) is not str or not 1 <= len(task) <= 500:
            raise HTTPException(status_code=422, detail="enter a task description")
        return await run_in_threadpool(call, service.start, task, source_of(body),
                                       body.get("profile", "source_archive"), body.get("mapping"),
                                       input_event_of(body))

    @app.post("/api/stop")
    async def stop(request: Request):
        body = await json_object(request)
        return await run_in_threadpool(call, service.stop, source_of(body), input_event_of(body))

    @app.post("/api/episodes/{episode_id}/selection")
    async def select(episode_id: str, request: Request):
        body = await json_object(request)
        return await run_in_threadpool(call, service.select, episode_id,
                                       body.get("selection"), source_of(body), input_event_of(body))

    @app.get("/api/preview")
    def preview():
        image = service.camera.preview() if service.camera else None
        if image is None:
            return Response(status_code=204)
        return Response(content=image, media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})

    @app.get("/api/episodes/{episode_id}/video")
    async def video(episode_id: str):
        path = await run_in_threadpool(call, service.review_video, episode_id)
        return FileResponse(path, media_type="video/mp4")

    @app.get("/api/episodes/{episode_id}/poster")
    async def poster(episode_id: str):
        path = await run_in_threadpool(call, service.review_poster, episode_id)
        return FileResponse(path, media_type="image/jpeg")

    @app.get("/api/episodes/{episode_id}/source-video")
    def source_video(episode_id: str):
        path = call(_episode_video_path, service.root, episode_id)
        return FileResponse(path, media_type="video/mp4", filename=f"{episode_id}-source.mp4")

    @app.post("/api/export")
    async def export(request: Request):
        body = await json_object(request)
        path = await run_in_threadpool(call, service.export, body.get("profile", "source_archive"))
        return {"download": f"/api/download/{path.name}", "filename": path.name}

    @app.get("/api/download/{name}")
    def download(name: str):
        if not re.fullmatch(r"oglo-dataset-[0-9a-f]{32}\.zip", name):
            raise HTTPException(status_code=404, detail="dataset not found")
        path = service.root / "exports" / name
        if not path.is_file():
            raise HTTPException(status_code=404, detail="dataset not found")
        return FileResponse(path, media_type="application/zip", filename=name)

    return app
