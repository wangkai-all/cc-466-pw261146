"""数据契约 CI 服务 — FastAPI 入口（API + 页面路由）。"""
from __future__ import annotations

import os

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db, service
from .models import (ApprovalRequest, ContractContent, CreateChangeRequest,
                     CreateContractRequest, CreateRulesetRequest,
                     RollbackRequest, TaskUpdateRequest)
from .seed import seed_if_empty

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = FastAPI(title="数据契约 CI 服务", version="1.0.0")
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


@app.on_event("startup")
def startup() -> None:
    db.init_db()
    seed_if_empty()


@app.exception_handler(service.ServiceError)
def service_error_handler(_: Request, exc: service.ServiceError):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})


# ============================== API ==============================

@app.post("/api/contracts", status_code=201)
def api_create_contract(req: CreateContractRequest):
    return service.create_contract(req.name, req.owner, req.description,
                                   req.content.model_dump(), req.owner)


@app.get("/api/contracts")
def api_list_contracts():
    return service.list_contracts()


@app.get("/api/contracts/{name}")
def api_get_contract(name: str):
    return service.get_contract(name)


@app.get("/api/contracts/{name}/versions")
def api_contract_versions(name: str):
    contract = service.get_contract(name)
    rows = db.query(
        "SELECT id, version, status, created_by, change_request_id, created_at"
        " FROM contract_versions WHERE contract_id = ? ORDER BY version DESC",
        (contract["id"],))
    return rows


@app.post("/api/contracts/{name}/changes", status_code=201)
def api_submit_change(name: str, req: CreateChangeRequest):
    return service.submit_change(name, req.title, req.author, req.content.model_dump())


@app.get("/api/changes")
def api_list_changes(status: str | None = None):
    return service.list_changes(status)


@app.get("/api/changes/{cr_id}")
def api_get_change(cr_id: int):
    return service.get_change(cr_id)


@app.post("/api/changes/{cr_id}/approvals", status_code=201)
def api_add_approval(cr_id: int, req: ApprovalRequest):
    return service.add_approval(cr_id, req.approver, req.role, req.decision, req.comment)


@app.post("/api/changes/{cr_id}/merge")
def api_merge_change(cr_id: int):
    return service.merge_change(cr_id)


@app.post("/api/changes/{cr_id}/close")
def api_close_change(cr_id: int):
    return service.close_change(cr_id)


@app.patch("/api/tasks/{task_id}")
def api_update_task(task_id: int, req: TaskUpdateRequest):
    return service.update_task(task_id, req.status)


@app.get("/api/rulesets")
def api_list_rulesets():
    return service.list_rulesets()


@app.post("/api/rulesets", status_code=201)
def api_create_ruleset(req: CreateRulesetRequest):
    return service.create_ruleset(req.name, req.rules, req.created_by, req.note)


@app.post("/api/rulesets/{name}/rollback", status_code=201)
def api_rollback_ruleset(name: str, req: RollbackRequest):
    return service.rollback_ruleset(name, req.target_version_id, req.actor, req.note)


@app.get("/api/graph")
def api_graph():
    return service.dependency_graph()


# ============================== 页面 ==============================

@app.get("/", response_class=HTMLResponse)
def page_dashboard(request: Request):
    contracts = service.list_contracts()
    changes = service.list_changes()
    open_changes = [c for c in changes if c["status"] == "open"]
    stats = {
        "contracts": len(contracts),
        "open_changes": len(open_changes),
        "blocked": len([c for c in open_changes if c["gate_status"] == "BLOCKED"]),
        "auto_merged": len([c for c in changes if c["auto_merged"]]),
    }
    return templates.TemplateResponse(request, "dashboard.html", {"contracts": contracts, "changes": changes[:20],
        "stats": stats, "active": "dashboard",
    })


@app.get("/contracts/{name}", response_class=HTMLResponse)
def page_contract(request: Request, name: str):
    contract = service.get_contract(name)
    versions = db.query(
        "SELECT id, version, status, created_by, change_request_id, created_at"
        " FROM contract_versions WHERE contract_id = ? ORDER BY version DESC",
        (contract["id"],))
    changes = [c for c in service.list_changes() if c["contract_name"] == name]
    return templates.TemplateResponse(request, "contract.html", {"contract": contract, "versions": versions,
        "changes": changes, "active": "dashboard",
    })


@app.get("/changes", response_class=HTMLResponse)
def page_changes(request: Request):
    return templates.TemplateResponse(request, "changes.html", {"changes": service.list_changes(), "active": "changes",
    })


@app.get("/changes/{cr_id}", response_class=HTMLResponse)
def page_change_detail(request: Request, cr_id: int):
    cr = service.get_change(cr_id)
    return templates.TemplateResponse(request, "change_detail.html", {"cr": cr, "active": "changes",
    })


@app.get("/rulesets", response_class=HTMLResponse)
def page_rulesets(request: Request):
    return templates.TemplateResponse(request, "rulesets.html", {"rulesets": service.list_rulesets(), "active": "rulesets",
    })


@app.get("/graph", response_class=HTMLResponse)
def page_graph(request: Request):
    return templates.TemplateResponse(request, "graph.html", {"graph": service.dependency_graph(), "active": "graph",
    })

