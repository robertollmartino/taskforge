import hmac
import hashlib
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch
import pytest

# Configure isolated test data directory before importing app modules
TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="taskforge_tests_"))
os.environ["DATA_DIR"] = str(TEST_DATA_DIR)
os.environ["DB_PATH"] = str(TEST_DATA_DIR / "test_tasks.db")
os.environ["EXECUTIONS_DB_PATH"] = str(TEST_DATA_DIR / "test_executions.db")
os.environ["ATTACHMENTS_DIR"] = str(TEST_DATA_DIR / "attachments")
os.environ["BACKUPS_DIR"] = str(TEST_DATA_DIR / "backups")
os.environ["VAPID_FILE"] = str(TEST_DATA_DIR / "vapid.json")
os.environ["DEFAULT_ADMIN_USER"] = "admin"
os.environ["DEFAULT_ADMIN_PASSWORD"] = "Admin@123456"

from fastapi.testclient import TestClient
from main import app
from core.database import init_db
from core.audit import init_executions_db
from services.webhook_client import WebhookClient


@pytest.fixture(scope="module")
def client():
    init_db()
    init_executions_db()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin_headers(client: TestClient) -> dict[str, str]:
    res = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "Admin@123456"},
    )
    assert res.status_code == 200, res.text
    token = res.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_health_and_pwa_endpoints(client: TestClient):
    r_health = client.get("/health")
    assert r_health.status_code == 200
    assert r_health.json()["status"] == "healthy"
    assert r_health.json()["timezone"] == "America/Sao_Paulo"

    r_manifest = client.get("/manifest.json")
    assert r_manifest.status_code == 200
    assert r_manifest.json()["short_name"] == "TaskForge"

    r_sw = client.get("/sw.js")
    assert r_sw.status_code == 200
    assert "addEventListener('push'" in r_sw.text


def test_custom_statuses_and_custom_fields(client: TestClient, admin_headers: dict[str, str]):
    # 1. Create custom status
    st_res = client.post(
        "/api/v1/statuses",
        headers=admin_headers,
        json={"name": "Em Revisão Jurídica", "color": "#a855f7", "position": 25},
    )
    assert st_res.status_code == 201
    assert st_res.json()["status"]["slug"] == "em_revisao_juridica"

    # 2. Create custom fields of multiple types (currency, select, checkbox, url)
    cf1 = client.post(
        "/api/v1/custom-fields",
        headers=admin_headers,
        json={
            "key": "valor_contrato",
            "name": "Valor do Contrato",
            "field_type": "currency",
            "required": True,
            "default_value": 1500.0,
        },
    )
    assert cf1.status_code == 201

    cf2 = client.post(
        "/api/v1/custom-fields",
        headers=admin_headers,
        json={
            "key": "canal_origem",
            "name": "Canal de Origem",
            "field_type": "select",
            "options": ["Indicação", "Inbound", "Parceiro"],
            "required": False,
        },
    )
    assert cf2.status_code == 201


def test_tasks_lifecycle_subtasks_comments_attachments_and_recurrence(
    client: TestClient, admin_headers: dict[str, str]
):
    projects = client.get("/api/v1/projects", headers=admin_headers).json()["items"]
    proj_a = projects[0]["id"]
    proj_b = projects[1]["id"]

    # Create task without due date (allowed by requirement 1)
    t1_res = client.post(
        "/api/v1/tasks",
        headers=admin_headers,
        json={
            "title": "Tarefa Sem Data no Projeto A",
            "priority": "P1",
            "project_id": proj_a,
            "due_date": None,
            "tags": ["urgente", "juridico"],
            "custom_values": {"valor_contrato": 5000, "canal_origem": "Indicação"},
            "subtasks": [{"title": "Revisar cláusula 1"}, {"title": "Assinar documento"}],
        },
    )
    assert t1_res.status_code == 201, t1_res.text
    t1 = t1_res.json()["task"]
    assert t1["due_date"] is None
    assert t1["subtasks_total"] == 2
    assert t1["custom_values"]["valor_contrato"] == 5000.0

    # Create task with recurrence in Project B
    t2_res = client.post(
        "/api/v1/tasks",
        headers=admin_headers,
        json={
            "title": "Fechamento Mensal Recorrente",
            "priority": "P2",
            "project_id": proj_b,
            "due_date": "2026-11-10",
            "due_time": "18:00",
            "recurrence": {"enabled": True, "frequency": "daily", "interval": 1},
            "custom_values": {"valor_contrato": 1200, "canal_origem": "Inbound"},
        },
    )
    assert t2_res.status_code == 201
    t2 = t2_res.json()["task"]

    # Complete recurring task -> should automatically spawn next occurrence
    comp_res = client.patch(
        f"/api/v1/tasks/{t2['id']}",
        headers=admin_headers,
        json={"status_slug": "completed"},
    )
    assert comp_res.status_code == 200
    assert comp_res.json()["task"]["status_slug"] == "completed"

    all_tasks = client.get("/api/v1/tasks", headers=admin_headers).json()["items"]
    spawned = [
        x
        for x in all_tasks
        if x["title"] == "Fechamento Mensal Recorrente" and x["id"] != t2["id"]
    ]
    assert len(spawned) == 1
    assert spawned[0]["due_date"] == "2026-11-11"
    assert spawned[0]["status_slug"] == "pending"

    # Add comment and verify history
    c_res = client.post(
        f"/api/v1/tasks/{t1['id']}/comments",
        headers=admin_headers,
        json={"content": "Comentário de validação automatizada"},
    )
    assert c_res.status_code == 201
    assert len(c_res.json()["task"]["comments"]) == 1
    assert len(c_res.json()["task"]["history"]) >= 2

    # Upload attachment
    att_res = client.post(
        f"/api/v1/tasks/{t1['id']}/attachments",
        headers=admin_headers,
        files={"file": ("contrato.txt", b"Conteudo do anexo de teste", "text/plain")},
    )
    assert att_res.status_code == 201
    att_list = att_res.json()["task"]["attachments"]
    assert len(att_list) == 1

    # Download attachment
    dl_res = client.get(
        f"/api/v1/attachments/{att_list[0]['id']}/download",
        headers=admin_headers,
    )
    assert dl_res.status_code == 200
    assert dl_res.content == b"Conteudo do anexo de teste"


def test_advanced_filters_and_multi_project_queries(
    client: TestClient, admin_headers: dict[str, str]
):
    projects = client.get("/api/v1/projects", headers=admin_headers).json()["items"]
    proj_a = projects[0]["id"]
    proj_b = projects[1]["id"]

    # Query across multiple projects using nested AND + OR + custom fields
    query_payload = {
        "project_ids": [proj_a, proj_b],
        "filter_tree": {
            "logic": "AND",
            "conditions": [
                {"field": "custom.valor_contrato", "op": "gte", "value": 1000}
            ],
            "groups": [
                {
                    "logic": "OR",
                    "conditions": [
                        {"field": "priority", "op": "eq", "value": "P1"},
                        {"field": "custom.canal_origem", "op": "eq", "value": "Inbound"},
                    ],
                    "groups": [],
                }
            ],
        },
        "sorts": [{"field": "custom.valor_contrato", "dir": "desc"}],
    }
    q_res = client.post("/api/v1/tasks/query", headers=admin_headers, json=query_payload)
    assert q_res.status_code == 200
    items = q_res.json()["items"]
    assert len(items) >= 2
    assert items[0]["custom_values"]["valor_contrato"] >= items[1]["custom_values"]["valor_contrato"]


def test_saved_views_crud_and_duplication(client: TestClient, admin_headers: dict[str, str]):
    v_res = client.post(
        "/api/v1/views",
        headers=admin_headers,
        json={
            "name": "Contratos VIP P1",
            "view_type": "kanban",
            "filters": {
                "logic": "AND",
                "conditions": [{"field": "priority", "op": "eq", "value": "P1"}],
                "groups": [],
            },
            "is_default": False,
            "is_shared": True,
        },
    )
    assert v_res.status_code == 201
    vid = v_res.json()["view"]["id"]

    dup_res = client.post(f"/api/v1/views/{vid}/duplicate", headers=admin_headers)
    assert dup_res.status_code == 201
    assert "(Cópia)" in dup_res.json()["view"]["name"]


def test_multiuser_rbac_and_api_tokens(client: TestClient, admin_headers: dict[str, str]):
    # Create an employee (member: only sees assigned/created tasks, cannot delete)
    emp_res = client.post(
        "/api/v1/users",
        headers=admin_headers,
        json={
            "username": "funcionario1",
            "full_name": "Funcionário Um",
            "email": "func1@taskforge.local",
            "password": "Func@123456",
            "role": "member",
        },
    )
    assert emp_res.status_code == 201
    emp_id = emp_res.json()["user"]["id"]

    # Create a supervisor (sees all team tasks)
    sup_res = client.post(
        "/api/v1/users",
        headers=admin_headers,
        json={
            "username": "supervisor1",
            "full_name": "Supervisor Equipe",
            "email": "sup1@taskforge.local",
            "password": "Sup@123456",
            "role": "supervisor",
        },
    )
    assert sup_res.status_code == 201

    # Login as employee
    emp_login = client.post(
        "/api/v1/auth/login",
        json={"username": "funcionario1", "password": "Func@123456"},
    )
    assert emp_login.status_code == 200
    emp_headers = {"Authorization": f"Bearer {emp_login.json()['access_token']}"}

    # Employee should NOT see unassigned admin tasks
    emp_tasks_before = client.get("/api/v1/tasks", headers=emp_headers).json()["items"]
    assert len(emp_tasks_before) == 0

    # Admin assigns a task to employee
    t_assigned = client.post(
        "/api/v1/tasks",
        headers=admin_headers,
        json={
            "title": "Tarefa Exclusiva do Funcionário 1",
            "assignee_id": emp_id,
            "custom_values": {"valor_contrato": 500},
        },
    ).json()["task"]

    # Now employee sees exactly 1 task
    emp_tasks_after = client.get("/api/v1/tasks", headers=emp_headers).json()["items"]
    assert len(emp_tasks_after) == 1
    assert emp_tasks_after[0]["id"] == t_assigned["id"]

    # Employee cannot delete task (403 Forbidden)
    del_attempt = client.delete(f"/api/v1/tasks/{t_assigned['id']}", headers=emp_headers)
    assert del_attempt.status_code == 403

    # Employee cannot create new users (403 Forbidden)
    usr_attempt = client.post(
        "/api/v1/users",
        headers=emp_headers,
        json={
            "username": "hacker",
            "full_name": "Hacker",
            "email": "h@h.com",
            "password": "123",
            "role": "admin",
        },
    )
    assert usr_attempt.status_code == 403

    # Supervisor login -> sees all tasks across the team
    sup_login = client.post(
        "/api/v1/auth/login",
        json={"username": "supervisor1", "password": "Sup@123456"},
    )
    sup_headers = {"Authorization": f"Bearer {sup_login.json()['access_token']}"}
    sup_tasks = client.get("/api/v1/tasks", headers=sup_headers).json()["items"]
    assert len(sup_tasks) >= 3

    # Generate API token for external AI Agent / n8n and authenticate with X-API-Key
    tok_res = client.post(
        "/api/v1/tokens",
        headers=admin_headers,
        json={"name": "n8n Integration Token", "scopes": ["*"]},
    )
    assert tok_res.status_code == 201
    raw_api_token = tok_res.json()["token"]

    api_key_res = client.get("/api/v1/tasks", headers={"X-API-Key": raw_api_token})
    assert api_key_res.status_code == 200
    assert api_key_res.json()["total"] >= 3


def test_configurable_webhooks_and_hmac_signature(
    client: TestClient, admin_headers: dict[str, str]
):
    wh_res = client.post(
        "/api/v1/webhooks",
        headers=admin_headers,
        json={
            "name": "Webhook Teste n8n",
            "target_url": "https://example.org/webhook-test",
            "events": ["task.created", "task.completed"],
            "secret": "meu_segredo_hmac_123",
        },
    )
    assert wh_res.status_code == 201
    wh_id = wh_res.json()["webhook"]["id"]

    # Verify HMAC signature utility
    sig = WebhookClient.sign_payload("meu_segredo_hmac_123", b'{"hello":"world"}')
    expected = "sha256=" + hmac.new(b"meu_segredo_hmac_123", b'{"hello":"world"}', hashlib.sha256).hexdigest()
    assert sig == expected

    # Mock httpx post to verify webhook delivery recording
    with patch("services.webhook_client.WebhookClient.send", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = {
            "success": True,
            "status_code": 200,
            "response_body": '{"received":true}',
            "error": "",
            "duration_ms": 14.5,
        }
        client.post(
            "/api/v1/tasks",
            headers=admin_headers,
            json={
                "title": "Tarefa que Dispara Webhook",
                "custom_values": {"valor_contrato": 300},
            },
        )

    deliv_res = client.get(
        f"/api/v1/webhooks/deliveries?webhook_id={wh_id}", headers=admin_headers
    )
    assert deliv_res.status_code == 200
    assert deliv_res.json()["count"] >= 1
    assert deliv_res.json()["items"][0]["status"] == "success"


def test_notifications_deduplication_and_digests(
    client: TestClient, admin_headers: dict[str, str]
):
    # Vapid public key
    vk = client.get("/api/v1/push/vapid-public-key")
    assert vk.status_code == 200
    assert len(vk.json()["public_key"]) > 20

    # Subscribe push device
    sub_res = client.post(
        "/api/v1/push/subscribe",
        headers=admin_headers,
        json={
            "endpoint": "https://fcm.googleapis.com/fcm/send/fake-device-1",
            "keys": {"p256dh": "BM123456", "auth": "AU123456"},
        },
    )
    assert sub_res.status_code == 201

    # Trigger digest summary (08h/13h/18h engine) and verify deduplication
    d1 = client.post("/api/v1/notifications/trigger-digest", headers=admin_headers)
    assert d1.status_code == 200
    assert d1.json()["digests_sent"] >= 1

    # Trigger again in the same minute slot -> must deduplicate (0 sent)
    d2 = client.post("/api/v1/notifications/trigger-digest", headers=admin_headers)
    assert d2.status_code == 200
    assert d2.json()["digests_sent"] == 0


def test_online_sqlite_backup_and_restore(
    client: TestClient, admin_headers: dict[str, str]
):
    # Create online backup
    bkp_res = client.post(
        "/api/v1/backups",
        headers=admin_headers,
        json={"notes": "Snapshot antes de alteração", "include_attachments": True},
    )
    assert bkp_res.status_code == 201
    bkp_id = bkp_res.json()["backup"]["id"]

    # Create a temporary task after backup
    temp_task = client.post(
        "/api/v1/tasks",
        headers=admin_headers,
        json={
            "title": "Tarefa Temporária Pós-Backup",
            "custom_values": {"valor_contrato": 999},
        },
    ).json()["task"]

    # Restore backup online -> temporary task should no longer exist
    rest_res = client.post(f"/api/v1/backups/{bkp_id}/restore", headers=admin_headers)
    assert rest_res.status_code == 200
    assert rest_res.json()["restored"] is True

    check_gone = client.get(f"/api/v1/tasks/{temp_task['id']}", headers=admin_headers)
    assert check_gone.status_code == 404


def test_executions_observability_endpoint(
    client: TestClient, admin_headers: dict[str, str]
):
    exec_res = client.get("/executions", headers=admin_headers)
    assert exec_res.status_code == 200
    data = exec_res.json()
    assert data["count"] > 0
    assert "duration_ms" in data["items"][0]
