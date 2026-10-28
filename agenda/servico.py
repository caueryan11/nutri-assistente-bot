"""Agenda de uma nutri para o bot: ver horários livres, marcar, remarcar e cancelar consulta.

Google Calendar é a fonte da verdade do que está ocupado (inclui o que a nutri marca
por fora). public.agendamentos guarda o que o bot marcou; o índice de exclusão do
banco é a última barreira contra horário duplicado.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Callable, Protocol
from zoneinfo import ZoneInfo

from agenda.horarios import config_completa, descrever, sugerir, todos_os_horarios


class CalendarioProtocolo(Protocol):
    def ocupados(self, calendar_id: str, inicio: datetime, fim: datetime) -> list[tuple[datetime, datetime]]: ...
    def criar_evento(self, calendar_id: str, inicio: datetime, fim: datetime, titulo: str, descricao: str = "",
                     fuso: str = "America/Sao_Paulo") -> str: ...
    def cancelar_evento(self, calendar_id: str, evento_id: str) -> None: ...
    def listar_eventos(self, calendar_id: str, inicio: datetime, fim: datetime) -> list[dict]: ...


JANELA_EVENTOS_MANUAIS_DIAS = 60


def telefone_no_texto(texto: str, telefone: str) -> bool:
    """O evento é deste paciente se o texto tem o DDD + número dele, em qualquer formato
    ("(47) 90000-0024", "+55 47 900000024", com ou sem o 9). Exige DDD + número completos."""
    digitos = re.sub(r"\D", "", texto or "")
    tel = re.sub(r"\D", "", telefone or "")
    local = tel[2:] if tel.startswith("55") and len(tel) >= 12 else tel  # DDD + número
    candidatos = {local}
    if len(local) == 11 and local[2] == "9":
        candidatos.add(local[:2] + local[3:])        # sem o 9
    if len(local) == 10:
        candidatos.add(local[:2] + "9" + local[2:])  # com o 9
    return any(len(c) >= 10 and c in digitos for c in candidatos)


class HorarioOcupado(Exception):
    pass


class AgendaNutri:
    def __init__(self, calendario: CalendarioProtocolo, calendar_id: str, config: dict | None,
                 fuso: str, paciente_nome: str, paciente_telefone: str,
                 gravar: Callable[[datetime, datetime, str], None],
                 agora: Callable[[], datetime] | None = None,
                 listar: Callable[[], list[dict]] | None = None,
                 cancelar_registro: Callable[[str], None] | None = None,
                 titulo: str = "Consulta"):
        """gravar(inicio, fim, google_event_id): grava em public.agendamentos;
        levanta HorarioOcupado se o banco recusar por sobreposição.
        listar(): próximas consultas confirmadas DESTE paciente [{id, inicio, fim, google_event_id}].
        cancelar_registro(id): marca a consulta como cancelada no banco."""
        self._cal = calendario
        self._id = calendar_id
        self.config = config_completa(config)
        self.fuso = ZoneInfo(fuso)
        self._nome = paciente_nome
        self._tel = paciente_telefone
        self._gravar = gravar
        self._agora = agora or (lambda: datetime.now(self.fuso))
        self._listar = listar or (lambda: [])
        self._cancelar_registro = cancelar_registro or (lambda _id: None)
        self._titulo = titulo

    def _livres(self, a_partir_de: date | None = None, periodo: str = "qualquer") -> list[datetime]:
        agora = self._agora()
        fim = agora + timedelta(days=int(self.config["janela_dias"]) + 1)
        ocupados = self._cal.ocupados(self._id, agora, fim)
        return todos_os_horarios(self.config, ocupados, agora, self.fuso, a_partir_de, periodo)

    def ver_horarios_livres(self, a_partir_de: str | None = None, periodo: str = "qualquer") -> dict:
        try:
            dia = date.fromisoformat(a_partir_de) if a_partir_de else None
        except ValueError:
            return {"erro": "a_partir_de precisa ser uma data AAAA-MM-DD"}
        sugestoes = sugerir(self._livres(dia, periodo or "qualquer"), maximo=4, por_dia=2)
        if not sugestoes:
            return {"horarios": [], "aviso": "Nenhum horário livre nesse período; ofereça outro período ou encaminhe."}
        return {
            "duracao_min": self.config["duracao_min"],
            "horarios": [{"inicio": t.strftime("%Y-%m-%dT%H:%M"), "descricao": descrever(t)} for t in sugestoes],
        }

    def marcar_consulta(self, inicio: str) -> dict:
        try:
            ini = datetime.fromisoformat(inicio)
        except (TypeError, ValueError):
            return {"marcado": False, "erro": "inicio precisa ser AAAA-MM-DDTHH:MM, de um horário da lista"}
        ini = ini.replace(tzinfo=self.fuso) if ini.tzinfo is None else ini.astimezone(self.fuso)
        fim = ini + timedelta(minutes=int(self.config["duracao_min"]))
        # confere de novo, na hora de marcar (a agenda pode ter mudado)
        if ini not in self._livres(ini.date()):
            return {"marcado": False, "erro": "horário não está mais livre ou não é de atendimento; ofereça outros"}
        evento = self._cal.criar_evento(
            self._id, ini, fim, f"{self._titulo} – {self._nome}",
            f"Marcada pelo assistente no WhatsApp.\nPaciente: {self._nome}\nTelefone: {self._tel}",
            fuso=str(self.fuso))
        try:
            self._gravar(ini, fim, evento)
        except HorarioOcupado:
            self._cal.cancelar_evento(self._id, evento)
            return {"marcado": False, "erro": "horário acabou de ser ocupado; ofereça outros"}
        return {"marcado": True, "inicio": ini.strftime("%Y-%m-%dT%H:%M"), "descricao": descrever(ini),
                "duracao_min": self.config["duracao_min"]}

    # --- consultas já marcadas --------------------------------------------
    def _manuais(self, ja_conhecidos: set[str]) -> list[dict]:
        """Eventos que a nutri criou à mão no Google com o telefone do paciente no título ou na descrição."""
        agora = self._agora()
        eventos = self._cal.listar_eventos(self._id, agora, agora + timedelta(days=JANELA_EVENTOS_MANUAIS_DIAS))
        return [{"id": f"g:{e['id']}", "inicio": e["inicio"], "fim": e["fim"], "google_event_id": e["id"], "manual": True}
                for e in eventos
                if e["id"] not in ja_conhecidos and telefone_no_texto(f"{e['titulo']} {e['descricao']}", self._tel)]

    def _consultas(self) -> list[dict]:
        do_bot = self._listar()
        conhecidos = {c.get("google_event_id") for c in do_bot if c.get("google_event_id")}
        return sorted(do_bot + self._manuais(conhecidos), key=lambda c: c["inicio"])

    def minhas_consultas(self) -> dict:
        consultas = self._consultas()
        if not consultas:
            return {"consultas": [], "aviso": ("Nenhuma consulta futura encontrada para este paciente. Se ele diz que tem "
                                               "consulta, ela foi marcada pela nutricionista sem o telefone dele: encaminhe.")}
        return {"consultas": [{"agendamento_id": c["id"], "inicio": c["inicio"].astimezone(self.fuso).strftime("%Y-%m-%dT%H:%M"),
                               "descricao": descrever(c["inicio"].astimezone(self.fuso))} for c in consultas]}

    def _achar(self, agendamento_id: str) -> dict | None:
        return next((c for c in self._consultas() if str(c["id"]) == str(agendamento_id)), None)

    def _apagar_evento(self, evento_id: str | None) -> None:
        if not evento_id:
            return
        try:
            self._cal.cancelar_evento(self._id, evento_id)
        except Exception as e:  # já apagado pela nutri (404/410): segue e cancela no banco
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status not in (404, 410):
                raise

    def cancelar_consulta(self, agendamento_id: str) -> dict:
        consulta = self._achar(agendamento_id)
        if consulta is None:
            return {"cancelado": False, "erro": "consulta não encontrada entre as deste paciente; chame minhas_consultas"}
        self._apagar_evento(consulta.get("google_event_id"))
        if not consulta.get("manual"):
            self._cancelar_registro(str(consulta["id"]))
        return {"cancelado": True, "descricao": descrever(consulta["inicio"].astimezone(self.fuso)),
                "manual": bool(consulta.get("manual"))}

    def remarcar_consulta(self, agendamento_id: str, novo_inicio: str) -> dict:
        """Marca o novo horário primeiro; só depois cancela o antigo (nunca fica sem consulta)."""
        antiga = self._achar(agendamento_id)
        if antiga is None:
            return {"remarcado": False, "erro": "consulta não encontrada entre as deste paciente; chame minhas_consultas"}
        nova = self.marcar_consulta(novo_inicio)
        if not nova.get("marcado"):
            return {"remarcado": False, "erro": nova.get("erro", "não foi possível marcar o novo horário")}
        self._apagar_evento(antiga.get("google_event_id"))
        if not antiga.get("manual"):
            self._cancelar_registro(str(antiga["id"]))
        return {"remarcado": True, "de": descrever(antiga["inicio"].astimezone(self.fuso)),
                "para": nova["descricao"], "inicio": nova["inicio"], "manual": bool(antiga.get("manual"))}
