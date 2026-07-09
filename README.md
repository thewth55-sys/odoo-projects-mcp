# Odoo Projects MCP

Servidor MCP remoto que da a Claude acceso al **módulo de Proyectos de Odoo** (Odoo Online / SaaS Custom), pensado para que **varios miembros de un equipo** lo usen, cada uno con **su propia identidad de Odoo**.

- **CRUD completo** sobre proyectos, tareas, etapas, partes de horas (timesheets) e hitos, incluido el borrado.
- **Autenticación por persona**: cada usuario envía su login y su API key personal en cabeceras HTTP, así cada acción queda registrada bajo el usuario real y respeta sus permisos de Odoo.
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
| `ODOO_AUTH_MODE` | `per_user` | Sí |
| `ODOO_FALLBACK_LOGIN` | vacío | No |
| `ODOO_FALLBACK_API_KEY` | vacío | No |
| `PORT` | `8000` | No |

Ver `.env.example` para el detalle.

### 3.2 Comprobar que arrancó

El endpoint MCP queda en:

```
https://odoo-mcp.tudominio.com/mcp
```

En los logs de Easypanel deberías ver que el servidor arranca en el puerto 8000. (El endpoint `/mcp` responde a clientes MCP, no a un navegador normal.)

---

## 4. Cómo conecta cada miembro del equipo (en Claude)

Cada persona añade el conector **una vez**, poniendo **sus propias** credenciales en las cabeceras:

1. En Claude → **Settings / Ajustes → Connectors** → **Add custom connector**.
2. **URL**: `https://odoo-mcp.tudominio.com/mcp`
3. En **Headers / Cabeceras** (o "configuración avanzada" del conector) añade:

   | Cabecera | Valor |
   |---|---|
   | `X-Odoo-Login` | su email/login de Odoo |
   | `X-Odoo-Api-Key` | su API key personal (paso 2.2) |

4. Guardar. Listo: a partir de ahí Claude actúa en Odoo **como esa persona**.

> Si la interfaz de conectores que uses no permitiera añadir cabeceras personalizadas, avísame y adaptamos el servidor para recibir la credencial de otra forma (por ejemplo un token por usuario en la ruta). También se puede evolucionar a **OAuth** para identidad completa sin manejar API keys.

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

- **Nunca** pongas las API keys en el código ni en el repositorio: van en cabeceras (por usuario) o en variables de entorno de Easypanel.
- Sirve el MCP **siempre por HTTPS** (Easypanel lo hace por defecto). Las cabeceras viajan cifradas.
- Los permisos efectivos son los del usuario de Odoo: para restringir a alguien, ajusta su rol en Odoo.
- Puedes revocar el acceso de una persona borrando su API key en Odoo, sin afectar al resto.
- Si `ODOO_AUTH_MODE=per_user`, una petición sin cabeceras válidas es rechazada (no hay acceso anónimo).
