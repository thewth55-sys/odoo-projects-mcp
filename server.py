"""
Odoo Projects MCP Server
========================

Servidor MCP remoto que da acceso al módulo de Proyectos de Odoo (Odoo Online / SaaS Custom).

Características:
- Transporte HTTP "streamable" (apto para desplegar en un VPS con Easypanel).
- Autenticación POR PERSONA mediante TOKEN EN LA URL: cada miembro del equipo usa una
  URL propia con un token opaco, p.ej. https://.../mcp/ab12cd34. El servidor mapea cada
  token a un (login + API key) de Odoo, guardados en la variable de entorno ODOO_USERS.
  Así cada acción queda registrada bajo el usuario real y hereda SUS permisos de Odoo,
  sin que la API key viaje nunca dentro de la URL.
  (Se eligió este método porque el conector de Claude no reenvía cabeceras HTTP
  personalizadas al servidor; la URL sí llega siempre intacta.)
- CRUD completo sobre Proyectos: proyectos, tareas, etapas, partes de horas (timesheets)
  e hitos, incluido el borrado.

La URL y la base de datos de Odoo son a nivel de servidor (misma instancia para todos);
solo el token (y por tanto la identidad) cambia por persona.

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

import contextvars
import html
import json
import logging
import os
import re
import secrets
import threading
import time
import xmlrpc.client
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any

import anyio
from cryptography.fernet import Fernet, InvalidToken
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("odoo-mcp")

# --------------------------------------------------------------------------- #
# Configuración a nivel de servidor (compartida por todo el equipo)
# --------------------------------------------------------------------------- #

ODOO_URL = os.environ.get("ODOO_URL", "").rstrip("/")   # p.ej. https://miempresa.odoo.com
ODOO_DB = os.environ.get("ODOO_DB", "")                 # nombre de la base de datos

# El endpoint MCP se monta en este path. La URL de cada usuario será:
#   https://<dominio><MCP_PATH>/<su-token>
MCP_PATH = os.environ.get("MCP_PATH", "/mcp")

# Borrado deshabilitado por defecto (mitiga borrados accidentales y prompt-injection).
# Actívalo con ALLOW_DELETE=true solo si de verdad necesitas borrar vía MCP.
ALLOW_DELETE = os.environ.get("ALLOW_DELETE", "false").strip().lower() in ("1", "true", "yes")

if not ODOO_URL or not ODOO_DB:
    raise RuntimeError(
        "Faltan variables de entorno obligatorias: ODOO_URL y ODOO_DB. "
        "Configúralas en Easypanel antes de arrancar el servicio."
    )


# --------------------------------------------------------------------------- #
# Mapa de usuarios: token -> (login de Odoo, API key personal)
# --------------------------------------------------------------------------- #
#
# Se define en la variable de entorno ODOO_USERS. Dos formatos aceptados:
#
#   1) Líneas 'token|login|api_key' (recomendado, fácil de editar en Easypanel):
#        ab12cd34|oswaldo@zuhma.online|0123456789abcdef...
#        ef56gh78|maria@zuhma.online|fedcba9876543210...
#      (separadas por saltos de línea o por ';')
#
#   2) JSON:
#        {"ab12cd34": {"login": "oswaldo@zuhma.online", "api_key": "..."}, ...}
#
# Los tokens deben ser aleatorios e impredecibles. Genera uno con:
#        python3 -c "import secrets; print(secrets.token_urlsafe(12))"


def _load_users() -> dict[str, dict[str, str]]:
    raw = os.environ.get("ODOO_USERS", "").strip()
    if not raw:
        return {}
    if raw.lstrip().startswith("{"):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"ODOO_USERS no es un JSON válido: {exc}") from exc
        return {
            str(token): {"login": v["login"], "api_key": v["api_key"]}
            for token, v in data.items()
        }
    users: dict[str, dict[str, str]] = {}
    for line in re.split(r"[;\n]+", raw):
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) != 3 or not all(parts):
            raise RuntimeError(
                f"Entrada inválida en ODOO_USERS: {line!r}. "
                "Formato esperado: token|login|api_key"
            )
        token, login, api_key = parts
        users[token] = {"login": login, "api_key": api_key}
    return users


# --------------------------------------------------------------------------- #
# Persistencia de usuarios auto-registrados (Opción 1: /enroll)
# --------------------------------------------------------------------------- #
#
# Los usuarios que se dan de alta solos se guardan en un archivo JSON dentro de un
# VOLUMEN PERSISTENTE, para sobrevivir a reinicios y redeploys sin tocar Easypanel.
#
#   ODOO_USERS_FILE : ruta del archivo (por defecto /data/users.json; monta /data como volumen)
#   ENROLL_SECRET   : clave de invitación para poder auto-registrarse (si está vacía, /enroll se deshabilita)
#   PUBLIC_BASE_URL : URL pública base para construir el enlace personal (opcional; si falta se deduce de la petición)

USERS_FILE = os.environ.get("ODOO_USERS_FILE", "/data/users.json")
ENROLL_SECRET = os.environ.get("ENROLL_SECRET", "")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

# --- Cifrado en reposo -----------------------------------------------------
# MASTER_KEY es una clave Fernet (urlsafe base64, 32 bytes). Genera una con:
#   python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Si se define, el archivo de usuarios se guarda CIFRADO. Si no, se guarda en
# texto plano (solo recomendable para pruebas) y se emite una advertencia.
MASTER_KEY = os.environ.get("MASTER_KEY", "").strip()
_fernet: Fernet | None = None
if MASTER_KEY:
    try:
        _fernet = Fernet(MASTER_KEY.encode())
    except (ValueError, TypeError) as exc:
        raise RuntimeError(
            "MASTER_KEY inválida. Debe ser una clave Fernet generada con "
            "Fernet.generate_key(). Detalle: " + str(exc)
        ) from exc
else:
    logger.warning(
        "MASTER_KEY no definida: el archivo de usuarios se guardará SIN CIFRAR. "
        "Define MASTER_KEY para cifrar los secretos en reposo."
    )

# --- Caducidad de tokens ---------------------------------------------------
# Si TOKEN_TTL_DAYS > 0, los tokens auto-registrados caducan pasado ese plazo.
try:
    TOKEN_TTL_DAYS = int(os.environ.get("TOKEN_TTL_DAYS", "0"))
except ValueError:
    TOKEN_TTL_DAYS = 0

_users_lock = threading.Lock()


def _load_users_from_file() -> dict[str, dict[str, Any]]:
    try:
        with open(USERS_FILE, "rb") as fh:
            raw = fh.read()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        logger.warning("No se pudo leer %s: %s", USERS_FILE, exc)
        return {}
    if not raw.strip():
        return {}
    # Si hay clave, el archivo está cifrado; si no, es JSON plano.
    if _fernet is not None:
        try:
            raw = _fernet.decrypt(raw)
        except InvalidToken:
            logger.error(
                "No se pudo descifrar %s (¿MASTER_KEY equivocada o archivo en texto "
                "plano?). Se ignora el archivo por seguridad.", USERS_FILE
            )
            return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.warning("Contenido inválido en %s: %s", USERS_FILE, exc)
        return {}
    return {str(token): dict(v) for token, v in data.items()}


def _save_users_to_file(users: dict[str, dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(USERS_FILE) or ".", exist_ok=True)
    payload = json.dumps(users, ensure_ascii=False, indent=2).encode("utf-8")
    if _fernet is not None:
        payload = _fernet.encrypt(payload)
    tmp = USERS_FILE + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(payload)
    os.chmod(tmp, 0o600)  # solo el dueño
    os.replace(tmp, USERS_FILE)  # escritura atómica


# Usuarios en memoria = definidos por variable de entorno (semilla) + auto-registrados (archivo).
# Los del archivo tienen prioridad si hubiera colisión de token.
_env_users = _load_users()
_file_users = _load_users_from_file()
USERS: dict[str, dict[str, str]] = {**_env_users, **_file_users}

if not USERS:
    logger.warning(
        "No hay usuarios cargados todavía. Da de alta con ODOO_USERS o mediante /enroll."
    )
else:
    logger.info(
        "Usuarios cargados: %d (env: %d, archivo: %d).",
        len(USERS), len(_env_users), len(_file_users),
    )


def _register_user(login: str, api_key: str) -> str:
    """Añade un usuario y devuelve su token nuevo. Persiste en archivo y memoria.

    Rotación: si el mismo login ya tenía tokens auto-registrados, se ELIMINAN, de modo
    que cada persona tiene un único token activo (re-registrarse invalida el anterior).
    """
    token = secrets.token_urlsafe(24)
    entry = {
        "login": login,
        "api_key": api_key,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    login_low = login.strip().lower()
    with _users_lock:
        stored = _load_users_from_file()
        # rotación: quita tokens previos del mismo login
        for old in [t for t, v in stored.items() if v.get("login", "").strip().lower() == login_low]:
            stored.pop(old, None)
            USERS.pop(old, None)
        stored[token] = entry
        _save_users_to_file(stored)
        USERS[token] = entry
    return token


def _token_expired(entry: dict[str, Any]) -> bool:
    """True si el token superó TOKEN_TTL_DAYS (si está activado y hay created_at)."""
    if TOKEN_TTL_DAYS <= 0:
        return False
    created = entry.get("created_at")
    if not created:
        return False  # usuarios sembrados por env no caducan
    try:
        created_dt = datetime.fromisoformat(created)
    except ValueError:
        return False
    age_days = (datetime.now(timezone.utc) - created_dt).total_seconds() / 86400
    return age_days > TOKEN_TTL_DAYS


# Token del usuario de la petición en curso (lo fija el middleware por cada request).
_current_token: contextvars.ContextVar[str] = contextvars.ContextVar("odoo_token", default="")


# --------------------------------------------------------------------------- #
# Rate limiting (ventana deslizante por IP, en memoria)
# --------------------------------------------------------------------------- #


class _RateLimiter:
    def __init__(self, max_calls: int, window_s: int):
        self.max = max_calls
        self.window = window_s
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.time()
        cutoff = now - self.window
        with self._lock:
            q = self._hits.setdefault(key, [])
            while q and q[0] < cutoff:
                q.pop(0)
            if len(q) >= self.max:
                return False
            q.append(now)
            return True


def _rl_from_env(name: str, default: str) -> _RateLimiter:
    """Lee 'N/segundos' de una variable de entorno (p.ej. '5/60')."""
    raw = os.environ.get(name, default)
    try:
        calls, window = raw.split("/")
        return _RateLimiter(int(calls), int(window))
    except (ValueError, AttributeError):
        calls, window = default.split("/")
        return _RateLimiter(int(calls), int(window))


# Límite estricto en /enroll (anti fuerza bruta / credential stuffing).
_enroll_rl = _rl_from_env("ENROLL_RATELIMIT", "5/60")
# Límite más holgado en el endpoint MCP.
_mcp_rl = _rl_from_env("MCP_RATELIMIT", "240/60")


def _client_ip(headers: dict[str, str], fallback: str = "unknown") -> str:
    xff = headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return headers.get("x-real-ip", "").strip() or fallback


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
    """Construye un OdooClient con las credenciales del usuario que hace la petición,
    resueltas a partir del token presente en su URL (p.ej. .../mcp/<token>)."""
    token = _current_token.get()
    if not token:
        raise OdooError(
            "Falta el token en la URL. Tu conector debe apuntar a "
            f"'{MCP_PATH}/<tu-token>' y no solo a '{MCP_PATH}'. "
            "Pide tu URL personal al administrador."
        )
    entry = USERS.get(token)
    if not entry:
        raise OdooError(
            "Token no reconocido. Verifica que tu URL personal sea correcta o vuelve a "
            "registrarte en /enroll."
        )
    if _token_expired(entry):
        raise OdooError(
            f"Tu enlace ha caducado (política de {TOKEN_TTL_DAYS} días). "
            "Vuelve a registrarte en /enroll para generar uno nuevo."
        )
    return OdooClient(ODOO_URL, ODOO_DB, OdooCredentials(entry["login"], entry["api_key"]))


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
        domain.append(("stage_id.fold", "=", False))
    fields = ["id", "name", "project_id", "stage_id", "user_ids", "date_deadline",
              "priority", "state", "allocated_hours", "effective_hours"]
    return odoo.search_read("project.task", domain, fields, limit=limit, order="priority desc, date_deadline asc")


@mcp.tool
def get_task(task_id: int) -> dict:
    """Devuelve el detalle completo de una tarea por su id."""
    odoo = _client_from_request()
    fields = ["id", "name", "project_id", "stage_id", "user_ids", "partner_id",
              "date_deadline", "priority", "state", "description",
              "allocated_hours", "effective_hours", "tag_ids", "child_ids"]
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
        domain.append(("stage_id.fold", "=", False))
    fields = ["id", "name", "project_id", "stage_id", "date_deadline", "priority", "state"]
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
        values["allocated_hours"] = planned_hours
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
    state: str | None = None,
) -> dict:
    """Actualiza campos de una tarea existente. Solo se cambian los campos que envíes.

    Args:
        task_id: id de la tarea.
        state: estado de la tarea en Odoo 17+: '01_in_progress' (en curso),
            '02_changes_requested' (cambios), '03_approved' (aprobada),
            '04_waiting_normal' (en espera), '1_done' (hecha), '1_canceled' (cancelada).
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
        values["allocated_hours"] = planned_hours
    if state is not None:
        values["state"] = state
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


def _guard_delete(confirm: bool) -> None:
    """Barrera común para operaciones destructivas."""
    if not ALLOW_DELETE:
        raise OdooError(
            "El borrado está deshabilitado en este servidor (ALLOW_DELETE=false). "
            "Para 'quitar del medio' un elemento, archívalo con la herramienta de actualización."
        )
    if not confirm:
        raise OdooError(
            "Operación destructiva y permanente. Vuelve a llamar con confirm=true "
            "para confirmar explícitamente el borrado."
        )


@mcp.tool
def delete_task(task_id: int, confirm: bool = False) -> dict:
    """Elimina una tarea de forma PERMANENTE. Requiere ALLOW_DELETE=true y confirm=true.

    Sugerencia: si solo quieres 'quitarla del medio', archívala con update_task."""
    _guard_delete(confirm)
    odoo = _client_from_request()
    odoo.unlink("project.task", [task_id])
    return {"deleted_id": task_id, "message": f"Tarea {task_id} eliminada permanentemente."}


@mcp.tool
def delete_project(project_id: int, confirm: bool = False) -> dict:
    """Elimina un proyecto de forma PERMANENTE (incluidas sus tareas).

    Operación peligrosa. Requiere ALLOW_DELETE=true y confirm=true."""
    _guard_delete(confirm)
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


@mcp.tool
def find_partners(query: str, limit: int = 10) -> list[dict]:
    """Busca contactos/clientes (res.partner) por nombre, email o teléfono. Devuelve su id."""
    odoo = _client_from_request()
    domain = ["|", "|", ("name", "ilike", query), ("email", "ilike", query), ("phone", "ilike", query)]
    return odoo.search_read(
        "res.partner", domain, ["id", "name", "email", "phone", "is_company"], limit=limit
    )


# =========================================================================== #
#  CRM  (lectura + escritura)                                    modelo crm.*  #
# =========================================================================== #


@mcp.tool
def list_leads(
    stage_id: int | None = None,
    team_id: int | None = None,
    user_id: int | None = None,
    only_open: bool = True,
    limit: int = 100,
) -> list[dict]:
    """Lista oportunidades/leads del CRM, con filtros opcionales por etapa, equipo o comercial."""
    odoo = _client_from_request()
    domain: list = []
    if stage_id is not None:
        domain.append(("stage_id", "=", stage_id))
    if team_id is not None:
        domain.append(("team_id", "=", team_id))
    if user_id is not None:
        domain.append(("user_id", "=", user_id))
    if only_open:
        domain.append(("active", "=", True))
    fields = ["id", "name", "contact_name", "email_from", "phone", "partner_id",
              "stage_id", "team_id", "user_id", "expected_revenue", "probability", "priority"]
    return odoo.search_read("crm.lead", domain, fields, limit=limit, order="probability desc")


@mcp.tool
def get_lead(lead_id: int) -> dict:
    """Detalle de una oportunidad/lead del CRM."""
    odoo = _client_from_request()
    fields = ["id", "name", "contact_name", "email_from", "phone", "partner_id", "stage_id",
              "team_id", "user_id", "expected_revenue", "probability", "priority",
              "date_deadline", "description", "type"]
    rows = odoo.search_read("crm.lead", [("id", "=", lead_id)], fields)
    if not rows:
        raise OdooError(f"No existe una oportunidad con id {lead_id}.")
    return rows[0]


@mcp.tool
def create_lead(
    name: str,
    contact_name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    partner_id: int | None = None,
    expected_revenue: float | None = None,
    team_id: int | None = None,
    user_id: int | None = None,
    description: str | None = None,
) -> dict:
    """Crea una oportunidad en el CRM. 'name' es el título de la oportunidad."""
    odoo = _client_from_request()
    values: dict[str, Any] = {"name": name, "type": "opportunity"}
    for key, val in (
        ("contact_name", contact_name), ("email_from", email), ("phone", phone),
        ("partner_id", partner_id), ("expected_revenue", expected_revenue),
        ("team_id", team_id), ("user_id", user_id), ("description", description),
    ):
        if val is not None:
            values[key] = val
    new_id = odoo.create("crm.lead", values)
    return {"created_id": new_id, "message": f"Oportunidad creada con id {new_id}."}


@mcp.tool
def update_lead(
    lead_id: int,
    name: str | None = None,
    expected_revenue: float | None = None,
    probability: float | None = None,
    user_id: int | None = None,
    description: str | None = None,
) -> dict:
    """Actualiza campos de una oportunidad. Solo cambia lo que envíes."""
    odoo = _client_from_request()
    values: dict[str, Any] = {}
    for key, val in (
        ("name", name), ("expected_revenue", expected_revenue),
        ("probability", probability), ("user_id", user_id), ("description", description),
    ):
        if val is not None:
            values[key] = val
    if not values:
        raise OdooError("No enviaste ningún campo para actualizar.")
    odoo.write("crm.lead", [lead_id], values)
    return {"updated_id": lead_id, "message": "Oportunidad actualizada."}


@mcp.tool
def move_lead_stage(lead_id: int, stage_id: int) -> dict:
    """Mueve una oportunidad a otra etapa del pipeline de CRM."""
    odoo = _client_from_request()
    odoo.write("crm.lead", [lead_id], {"stage_id": stage_id})
    return {"updated_id": lead_id, "message": f"Oportunidad movida a la etapa {stage_id}."}


@mcp.tool
def list_crm_stages() -> list[dict]:
    """Lista las etapas del pipeline de CRM."""
    odoo = _client_from_request()
    return odoo.search_read("crm.stage", [], ["id", "name", "sequence"], order="sequence asc")


@mcp.tool
def list_crm_teams() -> list[dict]:
    """Lista los equipos de ventas (CRM)."""
    odoo = _client_from_request()
    return odoo.search_read("crm.team", [], ["id", "name"], order="name asc")


# =========================================================================== #
#  SOPORTE AL CLIENTE / HELPDESK  (lectura + escritura)      modelo helpdesk.* #
#  (Requiere el módulo Helpdesk, de Odoo Enterprise.)                          #
# =========================================================================== #


@mcp.tool
def list_tickets(
    team_id: int | None = None,
    stage_id: int | None = None,
    user_id: int | None = None,
    only_open: bool = True,
    limit: int = 100,
) -> list[dict]:
    """Lista tickets de soporte (Helpdesk), con filtros opcionales."""
    odoo = _client_from_request()
    domain: list = []
    if team_id is not None:
        domain.append(("team_id", "=", team_id))
    if stage_id is not None:
        domain.append(("stage_id", "=", stage_id))
    if user_id is not None:
        domain.append(("user_id", "=", user_id))
    if only_open:
        domain.append(("stage_id.fold", "=", False))
    fields = ["id", "name", "team_id", "stage_id", "user_id", "partner_id",
              "priority", "kanban_state", "create_date"]
    return odoo.search_read("helpdesk.ticket", domain, fields, limit=limit, order="priority desc, create_date desc")


@mcp.tool
def get_ticket(ticket_id: int) -> dict:
    """Detalle de un ticket de soporte (Helpdesk)."""
    odoo = _client_from_request()
    fields = ["id", "name", "description", "team_id", "stage_id", "user_id", "partner_id",
              "partner_email", "priority", "kanban_state", "ticket_type_id", "create_date"]
    rows = odoo.search_read("helpdesk.ticket", [("id", "=", ticket_id)], fields)
    if not rows:
        raise OdooError(f"No existe un ticket con id {ticket_id}.")
    return rows[0]


@mcp.tool
def create_ticket(
    name: str,
    description: str | None = None,
    team_id: int | None = None,
    partner_id: int | None = None,
    user_id: int | None = None,
    priority: str | None = None,
) -> dict:
    """Crea un ticket de soporte. 'name' es el asunto; priority '0'..'3'."""
    odoo = _client_from_request()
    values: dict[str, Any] = {"name": name}
    for key, val in (
        ("description", description), ("team_id", team_id), ("partner_id", partner_id),
        ("user_id", user_id), ("priority", priority),
    ):
        if val is not None:
            values[key] = val
    new_id = odoo.create("helpdesk.ticket", values)
    return {"created_id": new_id, "message": f"Ticket creado con id {new_id}."}


@mcp.tool
def update_ticket(
    ticket_id: int,
    name: str | None = None,
    description: str | None = None,
    user_id: int | None = None,
    priority: str | None = None,
    kanban_state: str | None = None,
) -> dict:
    """Actualiza campos de un ticket. Solo cambia lo que envíes."""
    odoo = _client_from_request()
    values: dict[str, Any] = {}
    for key, val in (
        ("name", name), ("description", description), ("user_id", user_id),
        ("priority", priority), ("kanban_state", kanban_state),
    ):
        if val is not None:
            values[key] = val
    if not values:
        raise OdooError("No enviaste ningún campo para actualizar.")
    odoo.write("helpdesk.ticket", [ticket_id], values)
    return {"updated_id": ticket_id, "message": "Ticket actualizado."}


@mcp.tool
def move_ticket_stage(ticket_id: int, stage_id: int) -> dict:
    """Mueve un ticket a otra etapa del Helpdesk."""
    odoo = _client_from_request()
    odoo.write("helpdesk.ticket", [ticket_id], {"stage_id": stage_id})
    return {"updated_id": ticket_id, "message": f"Ticket movido a la etapa {stage_id}."}


@mcp.tool
def list_helpdesk_teams() -> list[dict]:
    """Lista los equipos de Helpdesk."""
    odoo = _client_from_request()
    return odoo.search_read("helpdesk.team", [], ["id", "name"], order="name asc")


@mcp.tool
def list_helpdesk_stages() -> list[dict]:
    """Lista las etapas del Helpdesk."""
    odoo = _client_from_request()
    return odoo.search_read("helpdesk.stage", [], ["id", "name", "sequence"], order="sequence asc")


# =========================================================================== #
#  EMAIL MARKETING  (lectura + escritura)                       modelo mailing.* #
#  Nota: crear envíos queda en BORRADOR; el envío se confirma en Odoo.          #
# =========================================================================== #


@mcp.tool
def list_mailings(limit: int = 50) -> list[dict]:
    """Lista campañas de email marketing (envíos)."""
    odoo = _client_from_request()
    fields = ["id", "subject", "state", "sent_date", "sent", "delivered", "opened"]
    return odoo.search_read("mailing.mailing", [], fields, limit=limit, order="create_date desc")


@mcp.tool
def get_mailing(mailing_id: int) -> dict:
    """Detalle de un envío de email marketing (métricas y estado)."""
    odoo = _client_from_request()
    fields = ["id", "subject", "state", "sent_date", "sent", "delivered", "opened",
              "clicked", "bounced", "contact_list_ids"]
    rows = odoo.search_read("mailing.mailing", [("id", "=", mailing_id)], fields)
    if not rows:
        raise OdooError(f"No existe un envío con id {mailing_id}.")
    return rows[0]


@mcp.tool
def create_mailing_draft(subject: str, body_html: str, contact_list_ids: list[int]) -> dict:
    """Crea un envío de email marketing EN BORRADOR dirigido a una o más listas de contactos.

    No se envía: queda en estado borrador para revisar y confirmar el envío desde Odoo.
    """
    odoo = _client_from_request()
    values = {
        "subject": subject,
        "body_arch": body_html,
        "body_html": body_html,
        "mailing_model_real": "mailing.list",
        "contact_list_ids": [(6, 0, contact_list_ids)],
    }
    new_id = odoo.create("mailing.mailing", values)
    return {"created_id": new_id, "message": f"Envío creado EN BORRADOR con id {new_id}. Revísalo y envíalo desde Odoo."}


@mcp.tool
def list_mailing_lists() -> list[dict]:
    """Lista las listas de contactos de email marketing."""
    odoo = _client_from_request()
    return odoo.search_read("mailing.list", [], ["id", "name", "contact_count"], order="name asc")


@mcp.tool
def list_mailing_contacts(list_id: int | None = None, limit: int = 100) -> list[dict]:
    """Lista contactos de email marketing, opcionalmente de una lista concreta."""
    odoo = _client_from_request()
    domain = [] if list_id is None else [("list_ids", "in", [list_id])]
    return odoo.search_read("mailing.contact", domain, ["id", "name", "email"], limit=limit)


@mcp.tool
def add_mailing_contact(name: str, email: str, list_ids: list[int]) -> dict:
    """Añade un contacto a una o más listas de email marketing."""
    odoo = _client_from_request()
    values = {"name": name, "email": email, "list_ids": [(6, 0, list_ids)]}
    new_id = odoo.create("mailing.contact", values)
    return {"created_id": new_id, "message": f"Contacto creado con id {new_id}."}


# =========================================================================== #
#  AUTOMATIZACIÓN DE MARKETING  (lectura + escritura)         modelo marketing.* #
#  (Requiere el módulo Marketing Automation, de Odoo Enterprise.)              #
# =========================================================================== #


@mcp.tool
def list_marketing_campaigns(limit: int = 50) -> list[dict]:
    """Lista campañas de automatización de marketing."""
    odoo = _client_from_request()
    fields = ["id", "name", "state", "model_id", "running_participant_count", "total_participant_count"]
    return odoo.search_read("marketing.campaign", [], fields, limit=limit, order="create_date desc")


@mcp.tool
def get_marketing_campaign(campaign_id: int) -> dict:
    """Detalle de una campaña de automatización de marketing."""
    odoo = _client_from_request()
    fields = ["id", "name", "state", "model_id", "running_participant_count",
              "total_participant_count", "completed_participant_count"]
    rows = odoo.search_read("marketing.campaign", [("id", "=", campaign_id)], fields)
    if not rows:
        raise OdooError(f"No existe una campaña con id {campaign_id}.")
    return rows[0]


@mcp.tool
def list_marketing_activities(campaign_id: int) -> list[dict]:
    """Lista las actividades (pasos) de una campaña de automatización de marketing."""
    odoo = _client_from_request()
    fields = ["id", "name", "activity_type", "trigger_type", "interval_number", "interval_type"]
    return odoo.search_read("marketing.activity", [("campaign_id", "=", campaign_id)], fields)


@mcp.tool
def set_marketing_campaign_state(campaign_id: int, state: str) -> dict:
    """Cambia el estado de una campaña de automatización: 'draft', 'running' o 'stopped'."""
    odoo = _client_from_request()
    if state not in ("draft", "running", "stopped"):
        raise OdooError("Estado inválido. Usa 'draft', 'running' o 'stopped'.")
    odoo.write("marketing.campaign", [campaign_id], {"state": state})
    return {"updated_id": campaign_id, "message": f"Campaña en estado '{state}'."}


# =========================================================================== #
#  TABLEROS / DASHBOARDS  (lectura)                        modelo spreadsheet.* #
# =========================================================================== #


@mcp.tool
def list_dashboard_groups() -> list[dict]:
    """Lista los grupos de tableros (Dashboards)."""
    odoo = _client_from_request()
    return odoo.search_read("spreadsheet.dashboard.group", [], ["id", "name"], order="sequence asc")


@mcp.tool
def list_dashboards(group_id: int | None = None) -> list[dict]:
    """Lista los tableros (Dashboards), opcionalmente de un grupo concreto."""
    odoo = _client_from_request()
    domain = [] if group_id is None else [("dashboard_group_id", "=", group_id)]
    return odoo.search_read(
        "spreadsheet.dashboard", domain, ["id", "name", "dashboard_group_id"], order="sequence asc"
    )


# =========================================================================== #
#  VENTAS  (SOLO LECTURA)                                       modelo sale.*  #
# =========================================================================== #


@mcp.tool
def list_sales_orders(
    partner_id: int | None = None, state: str | None = None, limit: int = 100
) -> list[dict]:
    """Lista pedidos de venta (solo lectura). state: 'draft','sent','sale','done','cancel'."""
    odoo = _client_from_request()
    domain: list = []
    if partner_id is not None:
        domain.append(("partner_id", "=", partner_id))
    if state is not None:
        domain.append(("state", "=", state))
    fields = ["id", "name", "partner_id", "date_order", "amount_total", "state", "user_id"]
    return odoo.search_read("sale.order", domain, fields, limit=limit, order="date_order desc")


@mcp.tool
def get_sales_order(order_id: int) -> dict:
    """Detalle de un pedido de venta con sus líneas (solo lectura)."""
    odoo = _client_from_request()
    fields = ["id", "name", "partner_id", "date_order", "amount_untaxed", "amount_tax",
              "amount_total", "state", "user_id", "order_line"]
    rows = odoo.search_read("sale.order", [("id", "=", order_id)], fields)
    if not rows:
        raise OdooError(f"No existe un pedido de venta con id {order_id}.")
    return rows[0]


# =========================================================================== #
#  CONTABILIDAD  (SOLO LECTURA)                              modelo account.*  #
# =========================================================================== #


@mcp.tool
def list_invoices(
    move_type: str = "out_invoice",
    state: str | None = None,
    partner_id: int | None = None,
    limit: int = 100,
) -> list[dict]:
    """Lista facturas/documentos contables (solo lectura).

    move_type: 'out_invoice' (factura cliente), 'in_invoice' (factura proveedor),
    'out_refund', 'in_refund', 'entry' (asiento).
    """
    odoo = _client_from_request()
    domain: list = [("move_type", "=", move_type)]
    if state is not None:
        domain.append(("state", "=", state))
    if partner_id is not None:
        domain.append(("partner_id", "=", partner_id))
    fields = ["id", "name", "partner_id", "invoice_date", "amount_total",
              "state", "payment_state"]
    return odoo.search_read("account.move", domain, fields, limit=limit, order="invoice_date desc")


@mcp.tool
def get_invoice(move_id: int) -> dict:
    """Detalle de una factura/documento contable (solo lectura)."""
    odoo = _client_from_request()
    fields = ["id", "name", "partner_id", "invoice_date", "invoice_date_due", "amount_untaxed",
              "amount_tax", "amount_total", "amount_residual", "state", "payment_state", "move_type"]
    rows = odoo.search_read("account.move", [("id", "=", move_id)], fields)
    if not rows:
        raise OdooError(f"No existe un documento contable con id {move_id}.")
    return rows[0]


# =========================================================================== #
#  COMPRAS  (SOLO LECTURA)                                  modelo purchase.*  #
# =========================================================================== #


@mcp.tool
def list_purchase_orders(
    partner_id: int | None = None, state: str | None = None, limit: int = 100
) -> list[dict]:
    """Lista órdenes de compra (solo lectura). state: 'draft','sent','purchase','done','cancel'."""
    odoo = _client_from_request()
    domain: list = []
    if partner_id is not None:
        domain.append(("partner_id", "=", partner_id))
    if state is not None:
        domain.append(("state", "=", state))
    fields = ["id", "name", "partner_id", "date_order", "amount_total", "state"]
    return odoo.search_read("purchase.order", domain, fields, limit=limit, order="date_order desc")


@mcp.tool
def get_purchase_order(order_id: int) -> dict:
    """Detalle de una orden de compra (solo lectura)."""
    odoo = _client_from_request()
    fields = ["id", "name", "partner_id", "date_order", "amount_untaxed", "amount_tax",
              "amount_total", "state", "order_line"]
    rows = odoo.search_read("purchase.order", [("id", "=", order_id)], fields)
    if not rows:
        raise OdooError(f"No existe una orden de compra con id {order_id}.")
    return rows[0]


# =========================================================================== #
#  GASTOS  (SOLO LECTURA)                                    modelo hr.expense #
# =========================================================================== #


@mcp.tool
def list_expenses(
    employee_id: int | None = None, state: str | None = None, limit: int = 100
) -> list[dict]:
    """Lista gastos (solo lectura). state: 'draft','reported','approved','done','refused'."""
    odoo = _client_from_request()
    domain: list = []
    if employee_id is not None:
        domain.append(("employee_id", "=", employee_id))
    if state is not None:
        domain.append(("state", "=", state))
    fields = ["id", "name", "employee_id", "total_amount", "state", "date", "product_id"]
    return odoo.search_read("hr.expense", domain, fields, limit=limit, order="date desc")


@mcp.tool
def get_expense(expense_id: int) -> dict:
    """Detalle de un gasto (solo lectura)."""
    odoo = _client_from_request()
    fields = ["id", "name", "employee_id", "total_amount", "untaxed_amount", "state",
              "date", "product_id", "description"]
    rows = odoo.search_read("hr.expense", [("id", "=", expense_id)], fields)
    if not rows:
        raise OdooError(f"No existe un gasto con id {expense_id}.")
    return rows[0]


# --------------------------------------------------------------------------- #
# Arranque
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Auto-registro de usuarios: página web /enroll
# --------------------------------------------------------------------------- #

_PAGE = """<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Alta - Odoo Proyectos MCP</title>
<style>
  body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#f4f5f7;
       margin:0;padding:40px 16px;color:#1f2733}
  .card{max-width:460px;margin:0 auto;background:#fff;border-radius:14px;
        box-shadow:0 6px 24px rgba(0,0,0,.08);padding:28px 26px}
  h1{font-size:20px;margin:0 0 6px}
  p.sub{color:#5b6472;margin:0 0 20px;font-size:14px;line-height:1.5}
  label{display:block;font-size:13px;font-weight:600;margin:14px 0 6px}
  input{width:100%;box-sizing:border-box;padding:11px 12px;border:1px solid #d3d8e0;
        border-radius:9px;font-size:14px}
  button{margin-top:22px;width:100%;padding:12px;border:0;border-radius:9px;
         background:#7b3fe4;color:#fff;font-size:15px;font-weight:600;cursor:pointer}
  button:hover{background:#6a30cf}
  .msg{padding:11px 13px;border-radius:9px;font-size:13.5px;margin-bottom:16px;line-height:1.45}
  .err{background:#fdecec;color:#a12622;border:1px solid #f5c2c0}
  .ok{background:#e8f7ee;color:#1a7a42;border:1px solid #bde5cc}
  code{background:#f0eefb;color:#5a29b8;padding:2px 6px;border-radius:6px;
       word-break:break-all;font-size:13px}
  .hint{font-size:12px;color:#7a828e;margin-top:6px}
</style></head>
<body><div class="card">__BODY__</div></body></html>"""

_FORM_BODY = """<h1>Conectar Odoo Proyectos</h1>
<p class="sub">Regístrate para obtener tu enlace personal de conexión con Claude.
Tus credenciales se validan contra Odoo y se guardan de forma segura en el servidor.</p>
__MSG__
<form method="post" action="enroll" autocomplete="off">
  <label>Clave de invitación</label>
  <input name="invite" type="password" required placeholder="La que te dio tu administrador">
  <label>Tu correo / login de Odoo</label>
  <input name="login" type="email" required placeholder="tucorreo@empresa.com">
  <label>Tu API key de Odoo</label>
  <input name="api_key" type="password" required placeholder="Perfil - Seguridad de la cuenta - Nueva clave de API">
  <div class="hint">En Odoo: tu avatar - Mi perfil - Seguridad de la cuenta - Nueva clave de API.</div>
  <button type="submit">Generar mi enlace</button>
</form>"""


def _render_form(message_html: str = "") -> HTMLResponse:
    body = _FORM_BODY.replace("__MSG__", message_html)
    return HTMLResponse(_PAGE.replace("__BODY__", body))


def _validate_odoo(login: str, api_key: str) -> bool:
    """Comprueba que (login, api_key) autentican correctamente en Odoo."""
    try:
        _ = OdooClient(ODOO_URL, ODOO_DB, OdooCredentials(login, api_key)).uid
        return True
    except OdooError:
        return False


def _base_url(request: Request) -> str:
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    return f"{proto}://{host}"


@mcp.custom_route("/enroll", methods=["GET"])
async def enroll_form(request: Request) -> HTMLResponse:
    if not ENROLL_SECRET:
        return HTMLResponse(
            _PAGE.replace("__BODY__", "<h1>Registro deshabilitado</h1>"
                          "<p class='sub'>El administrador no ha activado el auto-registro.</p>"),
            status_code=403,
        )
    return _render_form()


@mcp.custom_route("/enroll", methods=["POST"])
async def enroll_submit(request: Request) -> HTMLResponse:
    if not ENROLL_SECRET:
        return HTMLResponse("Registro deshabilitado.", status_code=403)

    ip = _client_ip(dict(request.headers), request.client.host if request.client else "unknown")
    if not _enroll_rl.allow(ip):
        logger.warning("Rate limit de /enroll excedido por %s", ip)
        return _render_form(
            "<div class='msg err'>Demasiados intentos. Espera un minuto e inténtalo de nuevo.</div>"
        )

    form = await request.form()
    invite = str(form.get("invite", "")).strip()
    login = str(form.get("login", "")).strip()
    api_key = str(form.get("api_key", "")).strip()

    if not secrets.compare_digest(invite, ENROLL_SECRET):
        return _render_form("<div class='msg err'>Clave de invitación incorrecta.</div>")
    if not login or not api_key:
        return _render_form("<div class='msg err'>Completa tu correo y tu API key.</div>")

    valid = await anyio.to_thread.run_sync(_validate_odoo, login, api_key)
    if not valid:
        return _render_form(
            "<div class='msg err'>Odoo rechazó esas credenciales. Revisa tu correo y "
            "tu API key (y que tu usuario tenga contraseña establecida en Odoo Online).</div>"
        )

    token = await anyio.to_thread.run_sync(_register_user, login, api_key)
    personal_url = f"{_base_url(request)}{MCP_PATH}/{token}"
    logger.info("Nuevo usuario auto-registrado: %s", login)

    body = (
        "<h1>¡Listo!</h1>"
        f"<div class='msg ok'>Registrado como <b>{html.escape(login)}</b>.</div>"
        "<p class='sub'>Copia tu enlace personal y pégalo en Claude "
        "(Ajustes → Conectores → Añadir conector personalizado, como URL):</p>"
        f"<p><code>{html.escape(personal_url)}</code></p>"
        "<p class='hint'>Trátalo como un secreto: quien tenga este enlace actúa como tú "
        "en Odoo. Si lo pierdes, vuelve a registrarte para generar uno nuevo.</p>"
    )
    return HTMLResponse(_PAGE.replace("__BODY__", body))


class TokenPathMiddleware:
    """Middleware ASGI puro.

    Convierte la URL personal de cada usuario, '<MCP_PATH>/<token>', en la ruta que
    FastMCP espera ('<MCP_PATH>'), y deja el token disponible para las herramientas
    a través de un ContextVar. Cualquier scope que no sea HTTP (p.ej. 'lifespan') se
    reenvía tal cual para no romper el arranque del gestor de sesiones.
    """

    def __init__(self, app, mount_path: str):
        self.app = app
        self.mount_path = mount_path.rstrip("/")
        self.prefix = self.mount_path + "/"

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        token = ""
        if path.startswith(self.prefix):
            # Rate limit por IP en el endpoint MCP.
            hdrs = {k.decode("latin-1").lower(): v.decode("latin-1")
                    for k, v in scope.get("headers", [])}
            client = scope.get("client")
            ip = _client_ip(hdrs, client[0] if client else "unknown")
            if not _mcp_rl.allow(ip):
                logger.warning("Rate limit de MCP excedido por %s", ip)
                await PlainTextResponse("Too Many Requests", status_code=429)(scope, receive, send)
                return
            token = path[len(self.prefix):].strip("/")
            scope = dict(scope)
            scope["path"] = self.mount_path
            scope["raw_path"] = self.mount_path.encode("utf-8")

        reset = _current_token.set(token)
        try:
            await self.app(scope, receive, send)
        finally:
            _current_token.reset(reset)


if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))

    # App ASGI de FastMCP (transporte HTTP streamable) montada en MCP_PATH,
    # envuelta por el middleware que resuelve el token de la URL.
    inner = mcp.http_app(path=MCP_PATH)
    app = TokenPathMiddleware(inner, mount_path=MCP_PATH)

    logger.info("Escuchando en http://%s:%s%s/<token>", host, port, MCP_PATH)
    # access_log=False: evita que los tokens (en la URL) queden en los logs del servidor.
    # Se pasa el lifespan del app interno para que arranque el session manager de MCP.
    uvicorn.run(app, host=host, port=port, lifespan="on", access_log=False)
