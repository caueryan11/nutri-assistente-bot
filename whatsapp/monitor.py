"""Monitor da conexão do WhatsApp (uazapi): avisa quando cai e quando volta.

A instância gratuita da uazapi expira sem aviso e o bot fica mudo. O monitor confere o status
a cada 10 minutos e manda um alerta para ALERTA_URL (não dá para avisar pelo próprio WhatsApp,
que é justamente o que caiu). Formatos aceitos:
  - ntfy (https://ntfy.sh/<tópico>): texto puro, notificação no celular pelo app ntfy
  - Discord (https://discord.com/api/webhooks/...) ou Slack (https://hooks.slack.com/...)
O alerta não leva dado de paciente. Enquanto continuar fora, repete a cada 6 horas.
O estado também aparece no /saude.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Callable

import httpx

log = logging.getLogger("monitor")

REPETIR_ALERTA = timedelta(hours=6)


def enviar_alerta(url: str, texto: str, http: httpx.Client | None = None) -> None:
    if not url:
        return
    http = http or httpx.Client(timeout=15)
    if "discord.com/api/webhooks" in url:
        http.post(url, json={"content": texto}).raise_for_status()
    elif "hooks.slack.com" in url:
        http.post(url, json={"text": texto}).raise_for_status()
    else:  # ntfy e afins: corpo em texto puro
        http.post(url, content=texto.encode("utf-8"),
                  headers={"Title": "Nutri Assistente", "Tags": "warning"}).raise_for_status()


class MonitorWhatsApp:
    def __init__(self, consultar: Callable[[], dict], alertar: Callable[[str], None],
                 agora: Callable[[], datetime] | None = None):
        """consultar() -> {"conectado", "estado"} (ClienteUazapi.status); alertar(texto)."""
        self._consultar = consultar
        self._alertar = alertar
        self._agora = agora or (lambda: datetime.now(timezone.utc))
        self.conectado: bool | None = None  # None = ainda não conferiu
        self.estado = "não conferido"
        self.desde: datetime | None = None
        self._ultimo_alerta: datetime | None = None

    def resumo(self) -> dict:
        return {"conectado": self.conectado, "estado": self.estado,
                "desde": self.desde.isoformat() if self.desde else None}

    def _avisar(self, texto: str) -> None:
        try:
            self._alertar(texto)
            self._ultimo_alerta = self._agora()
        except Exception as e:  # alerta fora do ar não derruba nada
            log.warning("alerta não enviado: %s", type(e).__name__)

    def conferir(self) -> dict:
        s = self._consultar()
        agora = self._agora()
        mudou = s["conectado"] != self.conectado
        if mudou:
            self.desde = agora
        antes, self.conectado, self.estado = self.conectado, s["conectado"], s["estado"]
        if not self.conectado:
            log.warning("WhatsApp fora do ar: %s", self.estado)
            if mudou or self._ultimo_alerta is None or agora - self._ultimo_alerta >= REPETIR_ALERTA:
                self._avisar(f"⚠️ WhatsApp do Nutri Assistente fora do ar: {self.estado}. O bot não recebe nem "
                             f"envia mensagens até reconectar a instância na uazapi.")
        elif mudou and antes is False:
            log.info("WhatsApp reconectado")
            self._avisar("✅ WhatsApp do Nutri Assistente reconectado. O bot voltou a funcionar.")
        return self.resumo()
