# Odoo Projects MCP

Servidor MCP remoto que da a Claude acceso al **módulo de Proyectos de Odoo** (Odoo Online / SaaS Custom), pensado para que **varios miembros de un equipo** lo usen, cada uno con **su propia identidad de Odoo**.

- **CRUD completo** sobre proyectos, tareas, etapas, partes de horas (timesheets) e hitos, incluido el borrado.
- **Autenticación por persona mediante token en la URL**: cada usuario tiene una URL propia (`https://<dominio>/mcp/<token>`). El servidor mapea cada token a un login + API key de Odoo. Así cada acción queda registrada bajo el usuario real y respeta sus permisos de Odoo, **sin que la API key viaje nunca en la URL**. *(Se usa este método porque el conector de Claude no reenvía cabeceras HTTP personalizadas; la URL sí llega siempre intacta.)*
- **Auto-registro (self-service)**: los usuarios se dan de alta solos en la página `/enroll` con una clave de invitación. El servidor valida sus credenciales contra Odoo y guarda el mapa en un **volumen persistente**. El administrador no tiene que editar variables ni redesplegar por cada persona.
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
| `ENROLL_SECRET` | clave de invitación para el auto-registro en `/enroll` | Sí (para self-service) |
| `ODOO_USERS_FILE` | `/data/users.json` (debe estar en un volumen persistente) | Sí (para self-service) |
| `PUBLIC_BASE_URL` | `https://odoo-mcp.tudominio.com` (para armar el enlace personal) | Recomendada |
| `ODOO_USERS` | mapa `token\|login\|api_key` (opcional, para sembrar usuarios manualmente) | No |
| `MCP_PATH` | `/mcp` (por defecto; normalmente no cambiar) | No |
| `PORT` | `8000` | No |

Ver `.env.example` para el detalle.

### 3.2 Añadir el volumen persistente (una sola vez)

Para que los usuarios auto-registrados sobrevivan a reinicios y redeploys, monta un volumen:

1. En Easypanel → tu servicio → **Mounts / Volúmenes** → **Add Volume**.
2. Tipo **Volume**, con un nombre (p. ej. `odoo-mcp-data`) y **Mount Path** = `/data`.
3. Guarda y redepliega.

Así el archivo `/data/users.json` (donde se guardan los registros) persiste siempre.

### 3.3 Comprobar que arrancó

Abre en el navegador `https://odoo-mcp.tudominio.com/enroll`: debe aparecer el **formulario de alta**. En los logs de Easypanel verás `Application startup complete`.

---

## 4. Cómo se da de alta y conecta cada miembro del equipo

Con el auto-registro, tú (admin) solo compartes **dos cosas** con tu equipo: la URL `/enroll` y la **clave de invitación** (`ENROLL_SECRET`). Cada persona hace:

1. **Genera su API key en Odoo:** su avatar → *Mi perfil* → *Seguridad de la cuenta* → *Nueva clave de API* → copia la clave. *(En Odoo Online, antes debe tener una contraseña establecida en su usuario.)*
2. **Se registra:** abre `https://odoo-mcp.tudominio.com/enroll`, escribe la clave de invitación, su correo de Odoo y su API key, y pulsa **Generar mi enlace**.
3. El servidor valida contra Odoo y le muestra su **URL personal**, tipo `https://odoo-mcp.tudominio.com/mcp/<su-token>`.
4. **Conecta en Claude:** *Ajustes → Conectores → Añadir conector personalizado* → pega esa URL como **URL** del conector → Guardar.

Listo: a partir de ahí Claude actúa en Odoo **como esa persona**, sin cabeceras ni credenciales dentro de Claude.

> **Revocar acceso:** borra la entrada del usuario en `/data/users.json` (o su API key en Odoo). Cambiar `ENROLL_SECRET` impide nuevos registros pero no afecta a los ya dados de alta.

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
