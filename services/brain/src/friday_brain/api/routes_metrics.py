from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from friday_brain.composition import CompositionRoot


router = APIRouter(tags=["Metrics"])


@router.get(
    "/metrics",
    include_in_schema=False,
)
async def metrics(
    request: Request,
) -> PlainTextResponse:
    root: CompositionRoot = request.app.state.composition_root

    content = root.get_metrics_registry().render_prometheus()

    return PlainTextResponse(
        content=content,
        media_type=("text/plain; version=0.0.4"),
    )
