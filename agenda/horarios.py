"""Horários livres: horário de atendimento da nutri menos o que está ocupado.

Função pura (sem rede) para ser testada sem Google.
agenda_config da nutri (jsonb em public.nutricionistas):
  {"duracao_min": 60, "antecedencia_horas": 12, "janela_dias": 14,
   "atendimento": {"1": ["08:00-12:00", "14:00-18:00"], ..., "5": [...]}}
Chaves de "atendimento": dia da semana ISO (1 = segunda ... 7 = domingo).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

CONFIG_PADRAO = {
    "duracao_min": 60,
    "antecedencia_horas": 12,
    "janela_dias": 14,
    "atendimento": {str(d): ["08:00-12:00", "14:00-18:00"] for d in range(1, 6)},
}
DIAS = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]


def config_completa(config: dict | None) -> dict:
    return {**CONFIG_PADRAO, **(config or {})}


def descrever(dt: datetime) -> str:
    """ex.: 'qua 30/09 às 10:00'"""
    return f"{DIAS[dt.isoweekday() - 1]} {dt:%d/%m} às {dt:%H:%M}"


def _hora(texto: str) -> time:
    h, m = texto.split(":")
    return time(int(h), int(m))


def blocos_do_dia(config: dict, dia: date, fuso: ZoneInfo) -> list[tuple[datetime, datetime]]:
    blocos = []
    for faixa in config["atendimento"].get(str(dia.isoweekday()), []):
        ini, fim = faixa.split("-")
        blocos.append((datetime.combine(dia, _hora(ini), fuso), datetime.combine(dia, _hora(fim), fuso)))
    return blocos


def _periodo_ok(dt: datetime, periodo: str) -> bool:
    if periodo == "manha":
        return dt.hour < 12
    if periodo == "tarde":
        return 12 <= dt.hour < 18
    if periodo == "noite":
        return dt.hour >= 18
    return True


def _sobrepoe(ini: datetime, fim: datetime, ocupados: list[tuple[datetime, datetime]]) -> bool:
    return any(ini < o_fim and o_ini < fim for o_ini, o_fim in ocupados)


def todos_os_horarios(config: dict, ocupados, agora: datetime, fuso: ZoneInfo,
                      a_partir_de: date | None = None, periodo: str = "qualquer") -> list[datetime]:
    config = config_completa(config)
    duracao = timedelta(minutes=int(config["duracao_min"]))
    minimo = agora + timedelta(hours=float(config["antecedencia_horas"]))
    primeiro_dia = max(a_partir_de or agora.astimezone(fuso).date(), agora.astimezone(fuso).date())
    ultimo_dia = agora.astimezone(fuso).date() + timedelta(days=int(config["janela_dias"]))
    livres = []
    dia = primeiro_dia
    while dia <= ultimo_dia:
        for ini_bloco, fim_bloco in blocos_do_dia(config, dia, fuso):
            t = ini_bloco
            while t + duracao <= fim_bloco:
                if t >= minimo and _periodo_ok(t, periodo) and not _sobrepoe(t, t + duracao, ocupados):
                    livres.append(t)
                t += duracao
        dia += timedelta(days=1)
    return livres


def sugerir(livres: list[datetime], maximo: int = 6, por_dia: int = 2) -> list[datetime]:
    """Espalha as sugestões: no máximo `por_dia` horários por dia."""
    escolhidos, contagem = [], {}
    for t in livres:
        if contagem.get(t.date(), 0) < por_dia:
            escolhidos.append(t)
            contagem[t.date()] = contagem.get(t.date(), 0) + 1
        if len(escolhidos) >= maximo:
            break
    return escolhidos
