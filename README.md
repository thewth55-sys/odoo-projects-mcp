# Odoo Projects MCP

Servidor MCP remoto que da a Claude acceso al **módulo de Proyectos de Odoo** (Odoo Online / SaaS Custom), pensado para que **varios miembros de un equipo** lo usen, cada uno con **su propia identidad de Odoo**.

- **CRUD completo** sobre proyectos, tareas, etapas, partes de horas (timesheets) e hitos, incluido el borrado.
- **Autenticación por persona mediante token en la URL**: cada usuario tiene una URL propia (`https://<dominio>/mcp/<token>`). El servidor mapea cada token a un login + API key de Odoo, guardados en la variable de entorno `ODOO_USERS`. Así cada acción queda registrada bajo el usuario real y respeta sus permisos de Odoo, **sin que la API key viaje nunca en la URL**. *(Se usa este método porque el conector de Claude no reenvía cabeceras HTTP personalizadas; la URL sí llega siempre intacta.)*
- Desplegable en un **VPS con Easypanel** (que además gestiona el dominio y el certificado HTTPS automáticamente).

---

## 1. Requisitos previos

- Odoo **Online con plan Custom** (los planes One App Free y Standard **no** permiten la API externa).
- Un VPS con **Easypanel** instalado y un dominio (o subdominio) apuntando a él.
- Cada miembro del equipo necesita una **cuenta de usuario en Odoo** con acceso al módulo de Proyectos.

> **Nota de compatibilidad:** el servidor usa la API externa XML-RPC (`/xmlrpc/2`). Odoo tiene previsto retirarla en favor de la nueva *External JSON-2 API* (Online 21.1, invierno 2027). Toda la comunicación con Odoo está aislada en la clase `OdooClient` de `server.py` para poder migrar sin tocar las herramientas.

---

## 2. Preparar Odoo (una vez, más una API key por persona)

### 2.1 Fijar contraseña a los usuarios (solo Odoo Online)

En Odoo Online los usuarios se crean **sin contraseña local**. Para poder generar una API key, cada usuario debe primero establecer una contraseña en su cuenta (Ajustes → Usuarios → seleccionar usuario → *Cambiar contraseña*).

### 2.2 Cada miembro genera su API key personal

Cada persona, con su propia sesión de Odoo:

1. Clic en su avatar (arriba a la derecha) → **Mi perfil** (o *Preferencias*).
2. Pestaña **Seguridad de la cuenta**.
3. **Nueva clave de API** → escribe una descripción (p. ej. "Claude MCP") → **Generar clave**.
4. Copia la clave **en ese momento** (no se vuelve a mostrar).

Cada quien guardará su par **login + API key** para el paso 4.

> Seguridad: los permisos reales los define el rol del usuario en Odoo. Si quieres limitar a alguien a solo lectura, ajústalo en su perfil de Odoo (grupos de acceso), no en el MCP.

---

## 3. Desplegar en Easypanel

Tienes dos formas; la de **App desde repositorio Git** es la más cómoda si subes esta carpeta a GitHub/GitLab.

### Opción A — desde repositorio Git (recomendada)

1. Sube esta carpeta (`odoo-projects-mcp/`) a un repositorio.
2. En Easypanel: **Create → App**.
3. **Source**: conecta tu repositorio y rama.
4. **Build**: selecciona **Dockerfile** (Easypanel lo detecta solo).
5. **Environment**: añade las variables (ver sección 3.1).
6. **Domains**: añade un dominio, p. ej. `odoo-mcp.tudominio.com`, con el **puerto 8000**. Easypanel emite el certificado HTTPS con Let's Encrypt automáticamente.
7. **Deploy**.

### Opción B — imagen Docker manual

Si prefieres construir la imagen tú mismo:

```bash
docker build -t odoo-projects-mcp .
docker run -p 8000:8000 --env-file .env odoo-projects-mcp
```

Luego expón el contenedor con un dominio en Easypanel apuntando al puerto 8000.

### 3.1 Variables de entorno (en Easypanel → Environment)

| Variable | Valor | Obligatoria |
|---|---|---|
| `ODOO_URL` | `https://tuempresa.odoo.com` (sin barra final) | Sí |
| `ODOO_DB` | nombre de la base de datos (suele ser el subdominio) | Sí |
| `ODOO_USERS` | mapa `token\|login\|api_key`, una línea por usuario (ver 3.2) | Sí |
| `MCP_PATH` | `/mcp` (por defecto; normalmente no cambiar) | No |
| `PORT` | `8000` | No |

Ver `.env.example` para el detalle.

### 3.2 Dar de alta a cada usuario en `ODOO_USERS`

Cada persona necesita un **token** (aleatorio) asociado a su **login + API key** de Odoo. El valor de `ODOO_USERS` es una línea por usuario con el formato `token|login|api_key`:

```
2NkgNSerM5dtSWId|oswaldo@zuhma.online|API_KEY_DE_OSWALDO
9qe2N4Ubcm24qEPH|maria@zuhma.online|API_KEY_DE_MARIA
```

Genera tokens seguros con:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(12))"
```

La **URL personal** de cada quien será entonces:

```
https://odoo-mcp.tudominio.com/mcp/<su-token>
```

> Para añadir o quitar usuarios: edita `ODOO_USERS` en Easypanel y vuelve a desplegar. Revocar a alguien = borrar su línea (o su API key en Odoo).

### 3.3 Comprobar que arrancó

Abre en el navegador la URL personal de un usuario, p. ej. `https://odoo-mcp.tudominio.com/mcp/<token>`. Si responde con un JSON tipo `"Not Acceptable: Client must accept text/event-stream"`, **está funcionando** (el endpoint MCP está vivo; un navegador no completa el handshake). En los logs de Easypanel verás `ODOO_USERS cargado con N usuario(s)` y `Application startup complete`.

---

## 4. Cómo conecta cada miembro del equipo (en Claude)

Cada persona añade el conector **una vez** con su **URL personal** (que ya lleva su token):

1. En Claude → **Settings / Ajustes → Connectors / Conectores** → **Add custom connector**.
2. **Name / Nombre**: `Odoo Proyectos`
3. **URL**: `https://odoo-mcp.tudominio.com/mcp/<su-token>`
4. Guardar. Listo: a partir de ahí Claude actúa en Odoo **como esa persona**. No hay que configurar cabeceras ni credenciales en Claude.

> El token va en la URL, pero la **API key no**: el servidor la resuelve internamente. Aun así, trata la URL personal como un secreto (quien la tenga actúa como ese usuario). Para revocar, borra la línea del usuario en `ODOO_USERS` y redepliega.

---

## 5. Qué puede hacer Claude una vez conectado

**Consultar:** `list_projects`, `get_project`, `list_tasks`, `get_task`, `list_stages`, `my_tasks`, `list_timesheets`, `list_milestones`, `find_users`.

**Crear / editar:** `create_task`, `update_task`, `move_task_stage`, `create_project`, `log_timesheet`, `create_milestone`.

**Borrar:** `delete_task`, `delete_project` *(permanente — úsalo con cuidado; para "quitar del medio" suele bastar con archivar vía `update_task`)*.

Ejemplos de cosas que le puedes pedir a Claude:
- "¿Qué tareas mías vencen esta semana?"
- "Crea una tarea 'Revisar contrato' en el proyecto Alfa, asignada a María, para el viernes."
- "Mueve la tarea 812 a la etapa 'En revisión'."
- "Imputa 3 horas de hoy a la tarea 812: 'Ajustes de diseño'."
- "Dame un resumen del avance del proyecto Alfa por etapa."

---

## 6. Notas de seguridad

- **Nunca** pongas las API keys en el código ni en el repositorio: viven solo en la variable `ODOO_USERS` de Easypanel (y el `.env` está en `.gitignore`).
- Sirve el MCP **siempre por HTTPS** (Easypanel lo hace por defecto): la URL con el token viaja cifrada.
- La **API key nunca va en la URL**; el servidor la resuelve desde el token. Aun así, la URL personal es un secreto: quien la tenga actúa como ese usuario.
- Los permisos efectivos son los del usuario de Odoo: para restringir a alguien (p. ej. solo lectura), ajusta su rol/grupos en Odoo.
- Para **revocar** a una persona: borra su línea en `ODOO_USERS` (y/o su API key en Odoo) y vuelve a desplegar. No afecta al resto.
- Una petición sin token o con un token desconocido es rechazada: **no hay acceso anónimo**.
