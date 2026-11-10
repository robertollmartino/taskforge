from typing import Any
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from core.security import check_permission, verify_token
from workflows.task_workflow import create_task_workflow, update_task_workflow
from workflows.webhook_workflow import dispatch_webhook_event

webhooks_router = APIRouter(prefix="/webhooks", tags=["Inbound Webhooks"])


class InboundWebhookPayload(BaseModel):
    action: str = Field(default="create_task", description="create_task ou update_task")
    task_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


async def _dispatch_events_bg(events: list[str], task_data: dict[str, Any]) -> None:
    for ev in events:
        await dispatch_webhook_event(ev, task_data, project_id=task_data.get("project_id"))


@webhooks_router.post("/inbound")
async def inbound_webhook_trigger(
    payload: InboundWebhookPayload,
    background_tasks: BackgroundTasks,
    user: dict[str, Any] = Depends(verify_token),
) -> dict[str, Any]:
    """Protected inbound webhook endpoint for external automation tools (n8n, Make, AI agents)."""
    if payload.action == "create_task":
        check_permission(user, "create", payload.data.get("project_id"))
        task, events = create_task_workflow(payload.data, user)
        background_tasks.add_task(_dispatch_events_bg, events, task)
        return {"status": "ok", "action": "create_task", "task": task}

    elif payload.action == "update_task":
        if not payload.task_id:
            raise HTTPException(status_code=422, detail="task_id é obrigatório para update_task.")
        check_permission(user, "edit", payload.data.get("project_id"))
        task, events = update_task_workflow(payload.task_id, payload.data, user)
        background_tasks.add_task(_dispatch_events_bg, events, task)
        return {"status": "ok", "action": "update_task", "task": task}

    raise HTTPException(status_code=400, detail=f"Ação de webhook desconhecida: {payload.action}")
