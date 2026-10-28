"""Cliente mínimo da Google Calendar API v3 com conta de serviço.

A nutri compartilha a agenda dela com o e-mail da conta de serviço
(seu-servico@seu-projeto.iam.gserviceaccount.com), com a permissão
"Fazer alterações nos eventos". Sem tela de login, sem aprovação de app.

Credencial: GOOGLE_SERVICE_ACCOUNT_B64 = JSON da chave em base64 (nunca no git).
Conta de serviço não convida participantes (precisaria de delegação de domínio):
o evento vai só para a agenda da nutri, e o paciente é avisado pelo WhatsApp.
"""

from __future__ import annotations

import base64
import json
import threading
from datetime import datetime

import httpx

API = "https://www.googleapis.com/calendar/v3"
ESCOPO = "https://www.googleapis.com/auth/calendar"


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("datetime precisa ter fuso horário")
    return dt.isoformat()


def _ler_data(valor: dict) -> datetime:
    # evento de dia inteiro vem como {"date": "2026-10-05"}
    texto = valor.get("dateTime") or (valor["date"] + "T00:00:00+00:00")
    return datetime.fromisoformat(texto.replace("Z", "+00:00"))


class GoogleCalendar:
    def __init__(self, credenciais_b64: str, http: httpx.Client | None = None):
        from google.oauth2 import service_account

        info = json.loads(base64.b64decode(credenciais_b64))
        self.email = info["client_email"]
        self._cred = service_account.Credentials.from_service_account_info(info, scopes=[ESCOPO])
        self._http = http or httpx.Client(timeout=20)
        self._lock = threading.Lock()

    def _token(self) -> str:
        from google.auth.transport.requests import Request

        with self._lock:
            if not self._cred.valid:
                self._cred.refresh(Request())
            return self._cred.token

    def _pedir(self, metodo: str, caminho: str, **kwargs) -> dict:
        r = self._http.request(metodo, API + caminho, headers={"Authorization": f"Bearer {self._token()}"}, **kwargs)
        r.raise_for_status()
        return r.json() if r.content else {}

    # --- agendas -----------------------------------------------------------
    def criar_agenda(self, nome: str, fuso: str = "America/Sao_Paulo") -> str:
        return self._pedir("POST", "/calendars", json={"summary": nome, "timeZone": fuso})["id"]

    def compartilhar(self, calendar_id: str, email: str, papel: str = "owner") -> None:
        """papel: reader | writer | owner."""
        self._pedir("POST", f"/calendars/{calendar_id}/acl",
                    json={"role": papel, "scope": {"type": "user", "value": email}})

    # --- horários ----------------------------------------------------------
    def ocupados(self, calendar_id: str, inicio: datetime, fim: datetime) -> list[tuple[datetime, datetime]]:
        """Intervalos ocupados (inclui eventos que a nutri marcou por fora do bot)."""
        r = self._pedir("POST", "/freeBusy", json={
            "timeMin": _iso(inicio), "timeMax": _iso(fim), "items": [{"id": calendar_id}]})
        agenda = r["calendars"][calendar_id]
        if agenda.get("errors"):
            raise PermissionError(f"sem acesso à agenda: {agenda['errors']}")
        return [(_ler_data({"dateTime": b["start"]}), _ler_data({"dateTime": b["end"]})) for b in agenda.get("busy", [])]

    # --- eventos -----------------------------------------------------------
    def listar_eventos(self, calendar_id: str, inicio: datetime, fim: datetime) -> list[dict]:
        """Eventos com hora marcada (ignora dia inteiro e cancelados), com paginação."""
        eventos, pagina = [], None
        while True:
            params = {"timeMin": _iso(inicio), "timeMax": _iso(fim), "singleEvents": "true",
                      "orderBy": "startTime", "maxResults": 250}
            if pagina:
                params["pageToken"] = pagina
            r = self._pedir("GET", f"/calendars/{calendar_id}/events", params=params)
            for e in r.get("items", []):
                if e.get("status") == "cancelled" or "dateTime" not in e.get("start", {}):
                    continue
                eventos.append({"id": e["id"], "titulo": e.get("summary", ""), "descricao": e.get("description", ""),
                                "inicio": _ler_data(e["start"]), "fim": _ler_data(e["end"])})
            pagina = r.get("nextPageToken")
            if not pagina:
                return eventos

    def criar_evento(self, calendar_id: str, inicio: datetime, fim: datetime, titulo: str,
                     descricao: str = "", fuso: str = "America/Sao_Paulo") -> str:
        r = self._pedir("POST", f"/calendars/{calendar_id}/events", json={
            "summary": titulo,
            "description": descricao,
            "start": {"dateTime": _iso(inicio), "timeZone": fuso},
            "end": {"dateTime": _iso(fim), "timeZone": fuso},
            "reminders": {"useDefault": True},
        })
        return r["id"]

    def cancelar_evento(self, calendar_id: str, evento_id: str) -> None:
        self._pedir("DELETE", f"/calendars/{calendar_id}/events/{evento_id}")
