"""
agente_zammad.py
================
Agente conversacional experto en tu instancia de Zammad.

Combina un contexto de conocimiento del sistema (system prompt cargado desde
archivo) con consultas en tiempo real a la API REST de Zammad vía tool use.

Puede recibir situaciones como:
  "llegamos y el server estaba apagado, lo prendimos y está ok, igual analiza"
  "¿cuántos tickets abiertos tiene el grupo de infraestructura?"
  "¿qué sucursal tiene más tickets esta semana?"

Uso:
    pip install -e .
    export ANTHROPIC_API_KEY="tu_api_key"
    export ZAMMAD_URL="https://tu-servidor-zammad"
    export ZAMMAD_TOKEN="tu_token_api"
    zammad-agent

El contexto del sistema se carga desde system_prompt.md (tu documentación
interna — no se versiona). Si no existe, usa system_prompt.example.md como
plantilla de partida.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime

import anthropic
import requests

log = logging.getLogger("zammad_agent")

DEFAULT_MODEL = "claude-opus-4-8"
MAX_TOKENS = 8192
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# ──────────────────────────────────────────────
# CONFIGURACIÓN
# ──────────────────────────────────────────────

@dataclass
class Config:
    url: str
    token: str
    model: str = DEFAULT_MODEL
    verify_ssl: bool = True

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Token token={self.token}"}


def load_config() -> Config:
    """Lee configuración de variables de entorno. Falla con mensaje claro."""
    url = os.environ.get("ZAMMAD_URL")
    token = os.environ.get("ZAMMAD_TOKEN")
    if not url:
        sys.exit("ERROR: variable de entorno ZAMMAD_URL no definida.")
    if not token:
        sys.exit("ERROR: variable de entorno ZAMMAD_TOKEN no definida.")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ERROR: variable de entorno ANTHROPIC_API_KEY no definida.")

    verify_ssl = os.environ.get("ZAMMAD_VERIFY_SSL", "true").lower() != "false"
    if not verify_ssl:
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        log.warning("Verificación TLS desactivada (ZAMMAD_VERIFY_SSL=false).")

    return Config(
        url=url.rstrip("/"),
        token=token,
        model=os.environ.get("ZAMMAD_AGENT_MODEL", DEFAULT_MODEL),
        verify_ssl=verify_ssl,
    )


def cargar_system_prompt() -> str:
    """Carga la base de conocimiento: system_prompt.md real, o el ejemplo."""
    for nombre in ("system_prompt.md", "system_prompt.example.md"):
        ruta = os.path.join(_BASE_DIR, nombre)
        if os.path.exists(ruta):
            with open(ruta, encoding="utf-8") as f:
                contenido = f.read().strip()
            if nombre.endswith("example.md"):
                log.warning(
                    "Usando system_prompt.example.md — crea tu system_prompt.md "
                    "con el contexto real de tu instancia."
                )
            return contenido
    sys.exit("ERROR: no se encontró system_prompt.md ni system_prompt.example.md")


# ──────────────────────────────────────────────
# CLIENTE + IMPLEMENTACIÓN DE HERRAMIENTAS
# ──────────────────────────────────────────────

def parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


class ZammadClient:
    """Acceso de solo lectura a la API de Zammad. Cada método es una tool."""

    def __init__(self, config: Config):
        self.config = config

    def _get(self, endpoint: str, params: dict | None = None):
        url = f"{self.config.url}/api/v1/{endpoint}"
        try:
            r = requests.get(
                url, headers=self.config.headers, params=params,
                verify=self.config.verify_ssl, timeout=15,
            )
            r.raise_for_status()
            return r.json(), None
        except requests.exceptions.ConnectionError:
            return None, f"No se pudo conectar al servidor Zammad ({self.config.url}). ¿Está encendido?"
        except requests.exceptions.Timeout:
            return None, "El servidor tardó demasiado en responder (timeout 15s)."
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 401:
                return None, "Token de API inválido o expirado. Regenerar en Perfil → Token de acceso."
            return None, f"Error HTTP: {e}"
        except requests.exceptions.RequestException as e:
            return None, f"Error inesperado: {e}"

    def verificar_servidor(self) -> dict:
        inicio = datetime.now()
        data, error = self._get("users/me")
        latencia = round((datetime.now() - inicio).total_seconds() * 1000)
        if error:
            return {
                "estado": "INACCESIBLE",
                "mensaje": error,
                "acciones": [
                    "1. Verificar que el servidor físico esté encendido",
                    "2. Verificar que la VM de Zammad esté corriendo en el hipervisor",
                    "3. Verificar conectividad de red (ping al servidor)",
                    "4. Si la VM corre, revisar servicios: sudo systemctl status zammad",
                ],
            }
        return {
            "estado": "OK",
            "mensaje": "Servidor Zammad accesible y respondiendo correctamente",
            "latencia_ms": latencia,
            "usuario_autenticado": data.get("name", "desconocido"),
            "timestamp": datetime.now().isoformat(),
        }

    def obtener_tickets(self, grupo_id=None, limite=100, estados_excluir=None) -> dict:
        if estados_excluir is None:
            estados_excluir = [5, 7]

        tickets: list[dict] = []
        page = 1
        while len(tickets) < limite:
            data, error = self._get("tickets", params={"page": page, "per_page": 50, "expand": "true"})
            if error:
                return {"error": error}
            if not data:
                break
            for t in data:
                if t.get("state_id") in estados_excluir:
                    continue
                if grupo_id is not None and t.get("group_id") != grupo_id:
                    continue
                tickets.append(t)
                if len(tickets) >= limite:
                    break
            if len(data) < 50:
                break
            page += 1

        return {
            "total_encontrados": len(tickets),
            "filtro_grupo_id": grupo_id,
            "tickets": [self._resumen_ticket(t) for t in tickets],
        }

    @staticmethod
    def _resumen_ticket(t: dict) -> dict:
        return {
            "id": t.get("id"),
            "numero": t.get("number"),
            "titulo": (t.get("title") or "")[:80],
            "estado": t.get("state", ""),
            "estado_id": t.get("state_id"),
            "prioridad": t.get("priority", ""),
            "grupo": t.get("group", ""),
            "grupo_id": t.get("group_id"),
            "agente": t.get("owner", ""),
            "cliente": t.get("customer", ""),
            "creado_en": t.get("created_at", ""),
            "actualizado_en": t.get("updated_at", ""),
            "cerrado_en": t.get("close_at") or t.get("closed_at") or "",
        }

    def obtener_ticket_detalle(self, ticket_id: int) -> dict:
        ticket, error = self._get(f"tickets/{ticket_id}", params={"expand": "true"})
        if error:
            return {"error": error}
        if not ticket:
            return {"error": f"Ticket {ticket_id} no encontrado"}

        articulos, _ = self._get(f"ticket_articles/by_ticket/{ticket_id}")
        articulos = articulos or []

        creado = parse_dt(ticket.get("created_at"))
        cerrado = parse_dt(ticket.get("close_at") or ticket.get("closed_at"))
        primera_resp = None
        for art in sorted(articulos, key=lambda a: a.get("created_at", "")):
            if art.get("sender", "").lower() == "agent" and primera_resp is None:
                primera_resp = parse_dt(art.get("created_at"))

        t_primera = round((primera_resp - creado).total_seconds() / 60, 1) if creado and primera_resp else None
        t_resolucion = round((cerrado - creado).total_seconds() / 60, 1) if creado and cerrado else None

        return {
            "id": ticket.get("id"),
            "numero": ticket.get("number"),
            "titulo": ticket.get("title"),
            "estado": ticket.get("state"),
            "prioridad": ticket.get("priority"),
            "grupo": ticket.get("group"),
            "agente": ticket.get("owner"),
            "cliente": ticket.get("customer"),
            "creado_en": ticket.get("created_at"),
            "cerrado_en": ticket.get("close_at") or ticket.get("closed_at"),
            "tiempo_primera_respuesta_min": t_primera,
            "tiempo_resolucion_min": t_resolucion,
            "total_articulos": len(articulos),
            "articulos": [
                {
                    "tipo": a.get("type", ""),
                    "remitente": a.get("sender", ""),
                    "creado_en": a.get("created_at", ""),
                    "asunto": (a.get("subject") or "")[:60],
                    "preview": (a.get("body", "") or "")[:120].replace("\n", " "),
                }
                for a in articulos
            ],
        }

    def obtener_resumen(self, grupo_id=None) -> dict:
        result = self.obtener_tickets(grupo_id=grupo_id, limite=500)
        if "error" in result:
            return result
        tickets = result["tickets"]
        if not tickets:
            return {"mensaje": "No se encontraron tickets con los filtros especificados", "grupo_id": grupo_id}

        estados: dict[str, int] = {}
        grupos: dict[str, int] = {}
        for t in tickets:
            estados[t["estado"] or "desconocido"] = estados.get(t["estado"] or "desconocido", 0) + 1
            grupos[t["grupo"] or "sin grupo"] = grupos.get(t["grupo"] or "sin grupo", 0) + 1

        sin_agente = sum(1 for t in tickets if not t.get("agente") or t["agente"] in ("", "-"))
        pendientes = [t for t in tickets if (t.get("estado", "") or "").lower() not in ("closed", "completado")]
        completados = len(tickets) - len(pendientes)

        return {
            "total_tickets": len(tickets),
            "completados": completados,
            "pendientes": len(pendientes),
            "tasa_resolucion_pct": round(completados / len(tickets) * 100),
            "sin_agente_asignado": sin_agente,
            "por_estado": estados,
            "por_grupo": grupos,
            "tickets_pendientes_recientes": [
                {"id": t["id"], "numero": t["numero"], "titulo": t["titulo"][:50],
                 "estado": t["estado"], "grupo": t["grupo"]}
                for t in pendientes[:10]
            ],
        }


# ──────────────────────────────────────────────
# DEFINICIÓN DE HERRAMIENTAS
# ──────────────────────────────────────────────

TOOLS = [
    {
        "name": "verificar_servidor",
        "description": (
            "Verifica si el servidor Zammad está accesible y respondiendo. "
            "Útil cuando el usuario menciona que el servidor estuvo apagado o "
            "quiere saber el estado general del sistema."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "obtener_tickets",
        "description": (
            "Obtiene tickets de Zammad con filtros opcionales. Filtra por grupo_id "
            "según los grupos descritos en el contexto. Excluye merged(5) y spam(7) por defecto."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "grupo_id": {"type": "integer", "description": "ID del grupo (omitir para todos)"},
                "limite": {"type": "integer", "description": "Máximo de tickets (default 100)"},
                "estados_excluir": {
                    "type": "array", "items": {"type": "integer"},
                    "description": "IDs de estado a excluir. Default [5,7]",
                },
            },
            "required": [],
        },
    },
    {
        "name": "obtener_ticket_detalle",
        "description": "Detalle completo de un ticket, con artículos y métricas de tiempo.",
        "input_schema": {
            "type": "object",
            "properties": {"ticket_id": {"type": "integer", "description": "ID numérico del ticket"}},
            "required": ["ticket_id"],
        },
    },
    {
        "name": "obtener_resumen",
        "description": (
            "Resumen estadístico de tickets: total, por estado, sin respuesta, pendientes. "
            "Ideal para '¿cómo está el sistema ahora?'"
        ),
        "input_schema": {
            "type": "object",
            "properties": {"grupo_id": {"type": "integer", "description": "ID del grupo (omitir para todos)"}},
            "required": [],
        },
    },
]


def ejecutar_herramienta(client: ZammadClient, nombre: str, parametros: dict) -> dict:
    """Despacha una tool_use al método correspondiente del cliente."""
    try:
        if nombre == "verificar_servidor":
            return client.verificar_servidor()
        if nombre == "obtener_tickets":
            return client.obtener_tickets(
                grupo_id=parametros.get("grupo_id"),
                limite=parametros.get("limite", 100),
                estados_excluir=parametros.get("estados_excluir", [5, 7]),
            )
        if nombre == "obtener_ticket_detalle":
            return client.obtener_ticket_detalle(parametros["ticket_id"])
        if nombre == "obtener_resumen":
            return client.obtener_resumen(grupo_id=parametros.get("grupo_id"))
        return {"error": f"Herramienta desconocida: {nombre}"}
    except Exception as e:  # noqa: BLE001 — la tool nunca debe tumbar el loop del agente
        return {"error": f"Error ejecutando {nombre}: {e}"}


# ──────────────────────────────────────────────
# INTERFAZ DE CHAT
# ──────────────────────────────────────────────

def imprimir_bienvenida(config: Config) -> None:
    linea = "─" * 56
    print(f"\n{linea}")
    print("  AGENTE ZAMMAD — asistente operacional")
    print(linea)
    print(f"  Servidor: {config.url}")
    print(f"  Modelo:   {config.model}")
    print(linea)
    print("  Escribe tu consulta o describe una situación.")
    print("  Escribe 'salir' o presiona Ctrl+C para terminar.")
    print(f"{linea}\n")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    config = load_config()
    system_prompt = cargar_system_prompt()
    client = ZammadClient(config)
    api = anthropic.Anthropic()
    messages: list[dict] = []

    imprimir_bienvenida(config)

    while True:
        try:
            user_input = input("Tú: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n\nHasta luego.")
            break
        if not user_input:
            continue
        if user_input.lower() in ("salir", "exit", "quit", "q"):
            print("Hasta luego.")
            break

        messages.append({"role": "user", "content": user_input})

        for _ in range(10):  # límite de iteraciones del loop agéntico
            try:
                with api.messages.stream(
                    model=config.model,
                    max_tokens=MAX_TOKENS,
                    system=[{"type": "text", "text": system_prompt,
                             "cache_control": {"type": "ephemeral"}}],
                    tools=TOOLS,
                    messages=messages,
                ) as stream:
                    print("\nAgente: ", end="", flush=True)
                    for text in stream.text_stream:
                        print(text, end="", flush=True)
                    response = stream.get_final_message()
            except anthropic.AuthenticationError:
                print("\n\nERROR: API key inválida. Verifica ANTHROPIC_API_KEY.")
                return
            except anthropic.RateLimitError:
                print("\n\nERROR: límite de requests alcanzado. Espera un momento.")
                break
            except anthropic.APIError as e:
                print(f"\n\nERROR de API: {e}")
                break

            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "end_turn":
                print("\n")
                break

            if response.stop_reason == "tool_use":
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        log.info("tool_use: %s %s", block.name, block.input)
                        print(f"\n  [→ consultando {block.name}...]", flush=True)
                        resultado = ejecutar_herramienta(client, block.name, block.input)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(resultado, ensure_ascii=False, indent=2),
                        })
                messages.append({"role": "user", "content": tool_results})
                print()
            else:
                print(f"\n  [stop_reason: {response.stop_reason}]\n")
                break


if __name__ == "__main__":
    main()
