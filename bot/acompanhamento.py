"""Dois níveis de consulta: simples e com acompanhamento (vale X dias depois da consulta).

O acompanhamento acabando vira convite ao retorno: 3 a 5 dias antes do fim, quem ainda não tem consulta
marcada recebe um aviso e o bot oferece horários. Um aviso por ciclo (renovar zera o aviso).
Quem está sem acompanhamento (nutri com modelo 'por_paciente') continua com agenda e lembretes; dúvidas vão
para a nutri com o convite ao acompanhamento (bot/assistente.py, modo só agenda).
Textos provisórios: a Ray revisa.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Callable, Protocol

log = logging.getLogger("acompanhamento")

TEXTO_FIM = ("Oi, {nome}! Seu acompanhamento com a nutricionista {nutri} vai até {data}. Para continuar com o "
             "suporte entre as consultas, que tal já marcar seu retorno? Me diga o melhor dia ou período que eu "
             "vejo os horários para você.")

SQL_FIM = """
with alvo as (
  select p.id, p.aviso_fim_acompanhamento_em as anterior
    from public.pacientes p
    join public.nutricionistas n on n.id = p.nutricionista_id
   where p.ativo and n.ativo and p.consentimento_em is not null
     and p.acompanhamento_ate is not null
     and p.acompanhamento_ate between (now() at time zone n.fuso_horario)::date + 3
                                  and (now() at time zone n.fuso_horario)::date + 5
     and (p.aviso_fim_acompanhamento_em is null or p.aviso_fim_acompanhamento_em::date < p.acompanhamento_desde)
     and not exists (select 1 from public.agendamentos a
                      where a.paciente_id = p.id and a.status in ('proposto', 'confirmado') and a.inicio >= now())
   for update of p skip locked
)
update public.pacientes p
   set aviso_fim_acompanhamento_em = now()
  from alvo, public.nutricionistas n
 where p.id = alvo.id and n.id = p.nutricionista_id
returning p.id::text as paciente_id, p.nutricionista_id::text as nutri_id, p.primeiro_nome, p.telefone,
          p.acompanhamento_ate, n.nome as nutri_nome, alvo.anterior
"""


def texto_fim(nome: str, nutri: str, ate: date) -> str:
    return TEXTO_FIM.format(nome=nome, nutri=nutri, data=f"{ate:%d/%m}")


class RepositorioAcompanhamento(Protocol):
    def reservar_fim_acompanhamento(self) -> list[dict]: ...
    def desfazer_fim_acompanhamento(self, paciente_id: str, anterior) -> None: ...
    def registrar_proativa(self, nutri_id: str, paciente_id: str, texto: str, motivo: str, wa_id: str | None) -> None: ...


def rodar_fim_acompanhamento(repo: RepositorioAcompanhamento, enviar: Callable[[str, str], "str | None"]) -> int:
    enviados = 0
    for p in repo.reservar_fim_acompanhamento():
        texto = texto_fim(p["primeiro_nome"], p["nutri_nome"], p["acompanhamento_ate"])
        try:
            wa_id = enviar(p["telefone"], texto)
        except Exception as e:
            repo.desfazer_fim_acompanhamento(p["paciente_id"], p.get("anterior"))
            log.warning("aviso de fim do acompanhamento não enviado (paciente=%s): %s", p["paciente_id"], type(e).__name__)
            continue
        repo.registrar_proativa(p["nutri_id"], p["paciente_id"], texto, "fim do acompanhamento", wa_id)
        enviados += 1
    log.info("avisos de fim do acompanhamento: %d", enviados)
    return enviados
