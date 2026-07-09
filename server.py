"""
Odoo Projects MCP Server
========================

Servidor MCP remoto que da acceso al módulo de Proyectos de Odoo (Odoo Online / SaaS Custom).

Características:
- Transporte HTTP "streamable" (apto para desplegar en un VPS con Easypanel).
- Autenticación POR PERSONA: cada miembro del equipo se identifica con su propio
  login de Odoo + su propia API key, enviados en cabeceras HTTP. Así cada acción
  queda registrada bajo el usuario real y hereda SUS permisos de Odoo.
- CRUD completo sobre Proyectos: proyectos, tareas, etapas, partes de horas (timesheets)
  e hitos, incluido el borrado.

La URL y la base de datos de Odoo son a nivel de servidor (misma instancia para todos);
solo las credenciales del usuario cambian por persona.

Modelos de Odoo usados:
  project.project          -> proyectos
  project.task             -> tareas
  project.task.type        -> etapas (columnas del kanban)
  account.analytic.line    -> partes de horas (timesheets)
  project.milestone        -> hitos
  res.users                -> usuarios (para resolver "mis tareas")

Compatibilidad: usa la API externa XML-RPC (/xmlrpc/2). Odoo planea sustituir XML-RPC
por la "External JSON-2 API" en versiones futuras; toda la E/S con Odoo está aislada en
la clase OdooClient para facilitar esa migración sin tocar las herramientas.
"""

from __future__ import annotations

import os
import xmlrpc.client
from dataclasses import dataclass
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers

# --------------------------------------------------------------------------- #
# Configuración a nivel de servidor (compartida por todo el equipo)
# --------------------------------------------------------------------------- #

ODOO_URL = os.environ.get("ODOO_URL", "").rstrip("/")   # p.ej. https://miempresa.odoo.com
ODOO_DB = os.environ.get("ODOO_DB", "")                 # nombre de la base de datos

# Credenciales de respaldo OPCIONALES. Se usan solo si una petición no trae cabeceras
# de usuario. En un despliegue de equipo se recomienda dejarlas vacías y forzar
# identidad por persona (ver AUTH_MODE).
FALLBACK_LOGIN = os.environ.get("ODOO_FALLBACK_LOGIN", "")
FALLBACK_API_KEY = os.environ.get("ODOO_FALLBACK_API_KEY", "")

# "per_user"  -> exige cabeceras X-Odoo-Login / X-Odoo-Api-Key en cada petición.
# "fallback"  -> si faltan cabeceras, usa las credenciales de respaldo de arriba.
AUTH_MODE = os.environ.get("ODOO_AUTH_MODE", "per_user").lower()

if not ODOO_URL or not ODOO_DB:
    raise RuntimeError(
        "Faltan variables de entorno obligatorias: ODOO_URL y ODOO_DB. "
        "Configúralas en Easypanel antes de arrancar el servicio."
    )


# --------------------------------------------------------------------------- #
# Cliente de Odoo (aislado para facilitar futura migración a JSON-2 API)
# --------------------------------------------------------------------------- #


class OdooError(RuntimeError):
    """Error legible para el usuario al hablar con Odoo."""


@dataclass
class OdooCredentials:
    login: str
    api_key: str


class OdooClient:
    """Cliente XML-RPC fino. Cachea el uid autenticado por (login) durante la vida
    del proceso para evitar re-autenticar en cada llamada."""

    _uid_cache: dict[str, int] = {}

    def __init__(self, url: str, db: str, creds: OdooCredentials):
        self.url = url
        self.db = db
        self.creds = creds
        self._common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common", allow_none=True)
        self._models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object", allow_none=True)

    @property
    def uid(self) -> int:
        cache_key = f"{self.db}:{self.creds.login}"
        if cache_key in self._uid_cache:
            return self._uid_cache[cache_key]
        try:
            uid = self._common.authenticate(
                self.db, self.creds.login, self.creds.api_key, {}
            )
        except Exception as exc:  # noqa: BLE001
            raise OdooError(f"No se pudo contactar con Odoo: {exc}") from exc
        if not uid:
            raise OdooError(
                "Autenticación de Odoo fallida. Revisa que el login y la API key "
                "sean correctos y que el usuario tenga acceso al módulo de Proyectos."
            )
        self._uid_cache[cache_key] = uid
        return uid

    def execute(self, model: str, method: str, args: list | None = None, kwargs: dict | None = None) -> Any:
        try:
            return self._models.execute_kw(
                self.db, self.uid, self.creds.api_key, model, method, args or [], kwargs or {}
            )
        except xmlrpc.client.Fault as fault:
            raise OdooError(f"Odoo rechazó la operación ({model}.{method}): {fault.faultString}") from fault
        except Exception as exc:  # noqa: BLE001
            raise OdooError(f"Error llamando a Odoo ({model}.{method}): {exc}") from exc

    # Azúcar sintáctico sobre los métodos ORM más usados -------------------- #

    def search_read(self, model, domain, fields=None, limit=None, order=None):
        kwargs: dict[str, Any] = {"fields": fields or []}
        if limit:
            kwargs["limit"] = limit
        if order:
            kwargs["order"] = order
        return self.execute(model, "search_read", [domain], kwargs)

    def create(self, model, values):
        return self.execute(model, "create", [values])

    def write(self, model, ids, values):
        return self.execute(model, "write", [ids, values])

    def unlink(self, model, ids):
        return self.execute(model, "unlink", [ids])


def _client_from_request() -> OdooClient:
    """Construye un OdooClient con las credenciales del usuario que hace la petición.

    Cada miembro del equipo configura estas dos cabeceras en su conector de Claude:
        X-Odoo-Login    -> su email / login de Odoo
        X-Odoo-Api-Key  -> su API key personal de Odoo
    """
    headers = get_http_headers()
    login = headers.get("x-odoo-login", "").strip()
    api_key = headers.get("x-odoo-api-key", "").strip()

    if not login or not api_key:
        if AUTH_MODE == "fallback" and FALLBACK_LOGIN and FALLBACK_API_KEY:
            login, api_key = FALLBACK_LOGIN, FALLBACK_API_KEY
        else:
            raise OdooError(
                "Faltan credenciales. Configura las cabeceras 'X-Odoo-Login' y "
                "'X-Odoo-Api-Key' con tu login y tu API key personal de Odoo en el "
                "conector de Claude."
            )

    return OdooClient(ODOO_URL, ODOO_DB, OdooCredentials(login, api_key))


# --------------------------------------------------------------------------- #
# Servidor MCP
# --------------------------------------------------------------------------- #

mcp = FastMCP(
    name="Odoo Projects",
    instructions=(
        "Acceso al módulo de Proyectos de Odoo. Puedes listar y consultar proyectos, "
        "tareas, etapas, partes de horas e hitos, y también crear, actualizar, mover "
        "de etapa y eliminar. Cada usuario actúa con su propia identidad de Odoo."
    ),
)


# ------------------------------ LECTURA ------------------------------------ #


@mcp.tool
def list_projects(active_only: bool = True, limit: int = 50) -> list[dict]:
    """Lista los proyectos. Devuelve id, nombre, cliente, responsable y nº de tareas.

    Args:
        active_only: si True, omite proyectos archivados.
        limit: número máximo de proyectos a devolver.
    """
    odoo = _client_from_request()
    domain = [] if not active_only else [("active", "=", True)]
    fields = ["id", "name", "partner_id", "user_id", "task_count", "date_start", "date"]
    return odoo.search_read("project.project", domain, fields, limit=limit, order="name asc")


@mcp.tool
def get_project(project_id: int) -> dict:
    """Devuelve el detalle de un proyecto por su id."""
    odoo = _client_from_request()
    fields = ["id", "name", "partner_id", "user_id", "task_count", "date_start",
              "date", "description", "active", "stage_id"]
    rows = odoo.search_read("project.project", [("id", "=", project_id)], fields)
    if not rows:
        raise OdooError(f"No existe un proyecto con id {project_id}.")
    return rows[0]


@mcp.tool
def list_tasks(
    project_id: int | None = None,
    stage_id: int | None = None,
    assignee_id: int | None = None,
    only_open: bool = True,
    limit: int = 100,
) -> list[dict]:
    """Lista tareas con filtros opcionales.

    Args:
        project_id: filtra por proyecto.
        stage_id: filtra por etapa (columna del kanban).
        assignee_id: filtra por usuario asignado (res.users id).
        only_open: si True, excluye tareas cerradas/hechas.
        limit: máximo de tareas a devolver.
    """
    odoo = _client_from_request()
    domain: list = []
    if project_id is not None:
        domain.append(("project_id", "=", project_id))
    if stage_id is not None:
        domain.append(("stage_id", "=", stage_id))
    if assignee_id is not None:
        domain.append(("user_ids", "in", [assignee_id]))
    if only_open:
        domain.append(("is_closed", "=", False))
    fields = ["id", "name", "project_id", "stage_id", "user_ids", "date_deadline",
              "priority", "kanban_state", "planned_hours", "effective_hours"]
    return odoo.search_read("project.task", domain, fields, limit=limit, order="priority desc, date_deadline asc")


@mcp.tool
def get_task(task_id: int) -> dict:
    """Devuelve el detalle completo de una tarea por su id."""
    odoo = _client_from_request()
    fields = ["id", "name", "project_id", "stage_id", "user_ids", "partner_id",
              "date_deadline", "priority", "kanban_state", "description",
              "planned_hours", "effective_hours", "tag_ids", "is_closed", "child_ids"]
    rows = odoo.search_read("project.task", [("id", "=", task_id)], fields)
    if not rows:
        raise OdooError(f"No existe una tarea con id {task_id}.")
    return rows[0]


@mcp.tool
def list_stages(project_id: int | None = None) -> list[dict]:
    """Lista las etapas/columnas del kanban. Opcionalmente filtradas por proyecto."""
    odoo = _client_from_request()
    domain = [] if project_id is None else [("project_ids", "in", [project_id])]
    return odoo.search_read(
        "project.task.type", domain, ["id", "name", "sequence", "fold"], order="sequence asc"
    )


@mcp.tool
def my_tasks(only_open: bool = True, limit: int = 100) -> list[dict]:
    """Devuelve las tareas asignadas al usuario actual (el dueño de la API key)."""
    odoo = _client_from_request()
    domain: list = [("user_ids", "in", [odoo.uid])]
    if only_open:
        domain.append(("is_closed", "=", False))
    fields = ["id", "name", "project_id", "stage_id", "date_deadline", "priority", "kanban_state"]
    return odoo.search_read("project.task", domain, fields, limit=limit, order="date_deadline asc")


@mcp.tool
def list_timesheets(task_id: int | None = None, project_id: int | None = None, limit: int = 100) -> list[dict]:
    """Lista partes de horas (timesheets). Filtra por tarea o por proyecto."""
    odoo = _client_from_request()
    domain: list = [("project_id", "!=", False)]  # solo líneas de proyecto
    if task_id is not None:
        domain.append(("task_id", "=", task_id))
    if project_id is not None:
        domain.append(("project_id", "=", project_id))
    fields = ["id", "date", "name", "unit_amount", "employee_id", "task_id", "project_id"]
    return odoo.search_read("account.analytic.line", domain, fields, limit=limit, order="date desc")


@mcp.tool
def list_milestones(project_id: int | None = None) -> list[dict]:
    """Lista hitos del proyecto. Opcionalmente filtrados por proyecto."""
    odoo = _client_from_request()
    domain = [] if project_id is None else [("project_id", "=", project_id)]
    return odoo.search_read(
        "project.milestone", domain,
        ["id", "name", "project_id", "deadline", "is_reached"], order="deadline asc"
    )


# ------------------------------ ESCRITURA ---------------------------------- #


@mcp.tool
def create_task(
    name: str,
    project_id: int,
    description: str | None = None,
    assignee_ids: list[int] | None = None,
    stage_id: int | None = None,
    deadline: str | None = None,
    priority: str | None = None,
    planned_hours: float | None = None,
) -> dict:
    """Crea una nueva tarea en un proyecto.

    Args:
        name: título de la tarea.
        project_id: proyecto donde se crea.
        description: descripción (HTML o texto plano).
        assignee_ids: lista de ids de usuarios asignados (res.users).
        stage_id: etapa inicial; si se omite, Odoo usa la primera.
        deadline: fecha límite en formato 'YYYY-MM-DD'.
        priority: '0' (normal) o '1' (importante/estrella).
        planned_hours: horas planificadas.
    """
    odoo = _client_from_request()
    values: dict[str, Any] = {"name": name, "project_id": project_id}
    if description is not None:
        values["description"] = description
    if assignee_ids:
        values["user_ids"] = [(6, 0, assignee_ids)]
    if stage_id is not None:
        values["stage_id"] = stage_id
    if deadline is not None:
        values["date_deadline"] = deadline
    if priority is not None:
        values["priority"] = priority
    if planned_hours is not None:
        values["planned_hours"] = planned_hours
    new_id = odoo.create("project.task", values)
    return {"created_id": new_id, "message": f"Tarea creada con id {new_id}."}


@mcp.tool
def update_task(
    task_id: int,
    name: str | None = None,
    description: str | None = None,
    assignee_ids: list[int] | None = None,
    deadline: str | None = None,
    priority: str | None = None,
    planned_hours: float | None = None,
    kanban_state: str | None = None,
) -> dict:
    """Actualiza campos de una tarea existente. Solo se cambian los campos que envíes.

    Args:
        task_id: id de la tarea.
        kanban_state: 'normal', 'done' (verde) o 'blocked' (rojo).
        (resto de argumentos: ver create_task)
    """
    odoo = _client_from_request()
    values: dict[str, Any] = {}
    if name is not None:
        values["name"] = name
    if description is not None:
        values["description"] = description
    if assignee_ids is not None:
        values["user_ids"] = [(6, 0, assignee_ids)]
    if deadline is not None:
        values["date_deadline"] = deadline
    if priority is not None:
        values["priority"] = priority
    if planned_hours is not None:
        values["planned_hours"] = planned_hours
    if kanban_state is not None:
        values["kanban_state"] = kanban_state
    if not values:
        raise OdooError("No enviaste ningún campo para actualizar.")
    odoo.write("project.task", [task_id], values)
    return {"updated_id": task_id, "message": "Tarea actualizada."}


@mcp.tool
def move_task_stage(task_id: int, stage_id: int) -> dict:
    """Mueve una tarea a otra etapa/columna del kanban."""
    odoo = _client_from_request()
    odoo.write("project.task", [task_id], {"stage_id": stage_id})
    return {"updated_id": task_id, "message": f"Tarea movida a la etapa {stage_id}."}


@mcp.tool
def create_project(
    name: str,
    partner_id: int | None = None,
    user_id: int | None = None,
    description: str | None = None,
) -> dict:
    """Crea un nuevo proyecto.

    Args:
        name: nombre del proyecto.
        partner_id: cliente asociado (res.partner id).
        user_id: responsable del proyecto (res.users id).
        description: descripción.
    """
    odoo = _client_from_request()
    values: dict[str, Any] = {"name": name}
    if partner_id is not None:
        values["partner_id"] = partner_id
    if user_id is not None:
        values["user_id"] = user_id
    if description is not None:
        values["description"] = description
    new_id = odoo.create("project.project", values)
    return {"created_id": new_id, "message": f"Proyecto creado con id {new_id}."}


@mcp.tool
def log_timesheet(task_id: int, hours: float, description: str, date: str | None = None) -> dict:
    """Registra un parte de horas (timesheet) en una tarea.

    Args:
        task_id: tarea a la que se imputan las horas.
        hours: número de horas (unit_amount).
        description: descripción del trabajo realizado.
        date: fecha 'YYYY-MM-DD'; si se omite, Odoo usa la de hoy.
    """
    odoo = _client_from_request()
    # Necesitamos el project_id de la tarea para la línea analítica.
    task = odoo.search_read("project.task", [("id", "=", task_id)], ["project_id"])
    if not task:
        raise OdooError(f"No existe una tarea con id {task_id}.")
    project_id = task[0]["project_id"][0] if task[0].get("project_id") else False
    if not project_id:
        raise OdooError("La tarea no pertenece a ningún proyecto; no se pueden imputar horas.")
    values: dict[str, Any] = {
        "name": description,
        "unit_amount": hours,
        "task_id": task_id,
        "project_id": project_id,
    }
    if date is not None:
        values["date"] = date
    new_id = odoo.create("account.analytic.line", values)
    return {"created_id": new_id, "message": f"Registradas {hours} h en la tarea {task_id}."}


@mcp.tool
def create_milestone(name: str, project_id: int, deadline: str | None = None) -> dict:
    """Crea un hito en un proyecto.

    Args:
        name: nombre del hito.
        project_id: proyecto al que pertenece.
        deadline: fecha objetivo 'YYYY-MM-DD'.
    """
    odoo = _client_from_request()
    values: dict[str, Any] = {"name": name, "project_id": project_id}
    if deadline is not None:
        values["deadline"] = deadline
    new_id = odoo.create("project.milestone", values)
    return {"created_id": new_id, "message": f"Hito creado con id {new_id}."}


# ------------------------------ BORRADO ------------------------------------ #


@mcp.tool
def delete_task(task_id: int) -> dict:
    """Elimina una tarea de forma permanente. Úsalo con cuidado.

    Sugerencia: si solo quieres 'quitarla del medio', considera archivarla con
    update_task en lugar de borrarla."""
    odoo = _client_from_request()
    odoo.unlink("project.task", [task_id])
    return {"deleted_id": task_id, "message": f"Tarea {task_id} eliminada permanentemente."}


@mcp.tool
def delete_project(project_id: int) -> dict:
    """Elimina un proyecto de forma permanente, incluidas sus tareas. Operación peligrosa."""
    odoo = _client_from_request()
    odoo.unlink("project.project", [project_id])
    return {"deleted_id": project_id, "message": f"Proyecto {project_id} eliminado permanentemente."}


# --------------------------------------------------------------------------- #
# Utilidad de apoyo: resolver usuarios por nombre/email
# --------------------------------------------------------------------------- #


@mcp.tool
def find_users(query: str, limit: int = 10) -> list[dict]:
    """Busca usuarios de Odoo por nombre o email (útil para obtener su id antes de asignar)."""
    odoo = _client_from_request()
    domain = ["|", ("name", "ilike", query), ("login", "ilike", query)]
    return odoo.search_read("res.users", domain, ["id", "name", "login"], limit=limit)


# --------------------------------------------------------------------------- #
# Arranque
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    # Transporte HTTP "streamable"; el endpoint MCP queda en http://<host>:<port>/mcp
    mcp.run(transport="http", host=host, port=port)
