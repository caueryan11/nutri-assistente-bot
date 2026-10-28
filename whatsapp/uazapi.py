"""Adaptador da uazapi (uazapiGO v2, API não oficial do WhatsApp).

Formato conferido na especificação oficial (docs.uazapi.com/openapi-bundled.json, v2.4.2):
  - envio:   POST {URL}/send/text, header `token`, corpo {"number", "text", ...}
  - webhook: envelope {"EventType", "token", "instanceName", ...} com `message` e `chat` na raiz.
A uazapi não assina o webhook: conferimos o `token` da instância no corpo
(e o app usa um segredo no caminho da URL).

Para trocar pela Cloud API da Meta, crie um adaptador com a mesma interface
(`token_valido`, `extrair_mensagem`, `enviar_texto`).
"""

from __future__ import annotations

import hmac
import logging
import re
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

TIPOS = [  # trecho do messageType (minúsculo) -> tipo em public.mensagens
    ("conversation", "texto"),
    ("extendedtext", "texto"),
    ("audio", "audio"),
    ("ptt", "audio"),
    ("image", "imagem"),
    ("document", "documento"),
]


@dataclass(frozen=True)
class MensagemRecebida:
    id: str          # messageid do WhatsApp (idempotência)
    instancia: str   # instanceName: identifica o número da nutri
    telefone: str    # só dígitos, com DDI, ex.: 5511999990001
    tipo: str        # texto | audio | imagem | documento | outro
    texto: str
    citada: str = ""  # id da mensagem respondida com "citar" (a nutri responde o aviso de um rascunho)


def token_valido(payload: dict, token_esperado: str) -> bool:
    recebido = str(payload.get("token") or "")
    return bool(token_esperado) and hmac.compare_digest(recebido.encode(), token_esperado.encode())


def _telefone(msg: dict, chat: dict) -> str | None:
    """Número do remetente. `sender` pode vir como LID (@lid), que não é telefone."""
    for candidato in (msg.get("sender_pn"), chat.get("phone"), msg.get("chatid"), msg.get("sender")):
        if not candidato or "@lid" in candidato or "@g.us" in candidato:
            continue
        digitos = re.sub(r"\D", "", candidato.split("@")[0])
        if 10 <= len(digitos) <= 15:
            return digitos
    return None


def _tipo(message_type: str) -> str:
    mt = (message_type or "").lower()
    return next((tipo for trecho, tipo in TIPOS if trecho in mt), "outro")


def extrair_mensagem(payload: dict) -> MensagemRecebida | None:
    """Mensagem de um paciente, ou None se o evento deve ser ignorado."""
    if payload.get("EventType") != "messages":
        return None
    msg = payload.get("message") or {}
    chat = payload.get("chat") or {}
    if msg.get("fromMe") or msg.get("wasSentByApi") or msg.get("isGroup") or chat.get("wa_isGroup"):
        return None
    telefone = _telefone(msg, chat)
    if not msg.get("messageid") or not telefone:
        log.warning("uazapi: mensagem sem id ou sem telefone resolvido (tipo=%s)", msg.get("messageType"))
        return None
    tipo = _tipo(msg.get("messageType"))
    texto = (msg.get("text") or "").strip()
    conteudo = msg.get("content")
    if not texto and isinstance(conteudo, dict):  # legenda de documento/imagem
        texto = str(conteudo.get("caption") or "").strip()
    if tipo == "outro" and texto:
        tipo = "texto"
    return MensagemRecebida(
        id=str(msg["messageid"]),
        instancia=str(payload.get("instanceName") or ""),
        telefone=telefone,
        tipo=tipo,
        texto=texto,
        citada=str(msg.get("quoted") or ""),
    )


class ClienteUazapi:
    def __init__(self, url: str, token: str, http: httpx.Client | None = None):
        self._url = url.rstrip("/")
        self._token = token
        self._http = http or httpx.Client(timeout=20)

    def enviar_texto(self, numero: str, texto: str, digitando_ms: int = 1200) -> str | None:
        """Envia texto e devolve o messageid (ou None se a uazapi não informar)."""
        r = self._http.post(
            f"{self._url}/send/text",
            headers={"token": self._token},
            json={"number": numero, "text": texto, "delay": digitando_ms, "readchat": True},
        )
        r.raise_for_status()
        return (r.json() or {}).get("messageid")

    def status(self) -> dict:
        """Estado da instância (GET /instance/status): {"conectado": bool, "estado": str}.
        401 = token inválido: a instância expirou ou foi apagada."""
        try:
            r = self._http.get(f"{self._url}/instance/status", headers={"token": self._token})
        except httpx.HTTPError as e:
            return {"conectado": False, "estado": f"sem resposta da uazapi ({type(e).__name__})"}
        if r.status_code == 401:
            return {"conectado": False, "estado": "token inválido (instância expirada ou apagada)"}
        if r.status_code >= 400:
            return {"conectado": False, "estado": f"erro {r.status_code} da uazapi"}
        dados = r.json() or {}
        estado = str((dados.get("instance") or {}).get("status") or "desconhecido")
        conectado = bool((dados.get("status") or {}).get("connected")) or estado == "connected"
        return {"conectado": conectado, "estado": estado}

    def baixar_midia(self, message_id: str) -> tuple[bytes, str]:
        """Arquivo de uma mensagem recebida (POST /message/download): (conteúdo, mimetype)."""
        r = self._http.post(f"{self._url}/message/download", headers={"token": self._token},
                            json={"id": message_id, "return_base64": True, "generate_mp3": False}, timeout=60)
        r.raise_for_status()
        dados = r.json() or {}
        import base64

        return base64.b64decode(dados.get("base64Data") or ""), str(dados.get("mimetype") or "")
