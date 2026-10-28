"""Métricas do consultório: o que o assistente fez pela nutri (ROI).

  - agregação por nutri e período (7 ou 30 dias) direto no Postgres
  - relatório semanal no WhatsApp (segunda 08:30, America/Sao_Paulo)
  - link do painel web assinado (HMAC, expira): /painel/<token>

Definições (o painel explica cada uma):
  dúvidas respondidas   mensagens do bot com acao='responder' (não conta confirmação do termo nem lembretes)
  fora do horário       dessas, as de seg-sex antes das 8h / depois das 18h, sábado e domingo (fuso da nutri)
  encaminhadas          mensagens com acao='encaminhar'
  novos interessados    leads criados no período
  primeiras consultas   agendamentos tipo='primeira' marcados pelo bot no período (não cancelados)
  retornos agendados    agendamentos tipo='retorno' marcados pelo bot no período (não cancelados)
  retornos resgatados   retornos marcados até 30 dias depois de uma cobrança de retorno (paciente 35+ dias sem consulta)
  receita estimada      (primeiras + retornos agendados) x valor_consulta
Textos provisórios: a Ray revisa.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Protocol
from zoneinfo import ZoneInfo

log = logging.getLogger("metricas")

FUSO = "America/Sao_Paulo"
VALIDADE_LINK_DIAS = 8  # o link do relatório vale até o próximo relatório chegar
HORARIO_COMERCIAL = (8, 18)  # seg-sex; fora disso a dúvida conta como "fora do horário"

Enviar = Callable[[str, str], "str | None"]


# ---------------------------------------------------------------------
# Números
# ---------------------------------------------------------------------
@dataclass
class Metricas:
    dias: int
    duvidas_respondidas: int = 0
    duvidas_fora_horario: int = 0
    duvidas_encaminhadas: int = 0
    novos_leads: int = 0
    leads_convertidos: int = 0
    primeiras_consultas: int = 0
    retornos_agendados: int = 0
    retornos_recuperados: int = 0
    valor_consulta: float | None = None
    ultimas_encaminhadas: list[dict] = field(default_factory=list)  # {quando, paciente, motivo}
    ultimas_consultas: list[dict] = field(default_factory=list)     # {quando, paciente, tipo, marcada_em}

    @property
    def consultas_agendadas(self) -> int:
        return self.primeiras_consultas + self.retornos_agendados

    @property
    def receita_estimada(self) -> float | None:
        return None if self.valor_consulta is None else self.consultas_agendadas * float(self.valor_consulta)

    @property
    def taxa_conversao(self) -> float | None:
        """% dos novos interessados do período que já marcaram a primeira consulta."""
        return None if not self.novos_leads else round(100 * self.leads_convertidos / self.novos_leads, 1)


def reais(valor: float) -> str:
    """1234.5 -> '1.234,50'."""
    return f"{valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


SQL_METRICAS = """
with msg as (
  select acao, motivo, criado_em at time zone %(fuso)s as local
    from public.mensagens
   where nutricionista_id = %(nutri)s and direcao = 'saida' and acao is not null
     and coalesce(motivo, '') not like 'check-in%%'
     and criado_em >= now() - make_interval(days => %(dias)s)
), ag as (
  select a.tipo,
         exists (select 1 from public.mensagens c
                  where c.paciente_id = a.paciente_id and c.direcao = 'saida' and c.motivo = 'cobrança de retorno'
                    and c.criado_em <= a.criado_em and c.criado_em > a.criado_em - interval '30 days') as resgatado
    from public.agendamentos a
   where a.nutricionista_id = %(nutri)s and a.origem = 'bot' and a.status in ('confirmado', 'realizado')
     and a.criado_em >= now() - make_interval(days => %(dias)s)
), ld as (
  select status from public.leads
   where nutricionista_id = %(nutri)s and criado_em >= now() - make_interval(days => %(dias)s)
)
select
  (select count(*) from msg where acao = 'responder' and coalesce(motivo, '') not like 'consentimento%%')
    as duvidas_respondidas,
  (select count(*) from msg where acao = 'responder' and coalesce(motivo, '') not like 'consentimento%%'
      and (extract(isodow from local) in (6, 7) or extract(hour from local) < %(abre)s
           or extract(hour from local) >= %(fecha)s)) as duvidas_fora_horario,
  (select count(*) from msg where acao = 'encaminhar') as duvidas_encaminhadas,
  (select count(*) from ld) as novos_leads,
  (select count(*) from ld where status = 'convertido') as leads_convertidos,
  (select count(*) from ag where tipo = 'primeira') as primeiras_consultas,
  (select count(*) from ag where tipo = 'retorno') as retornos_agendados,
  (select count(*) from ag where tipo = 'retorno' and resgatado) as retornos_recuperados,
  (select valor_consulta from public.nutricionistas where id = %(nutri)s) as valor_consulta
"""

SQL_ULTIMAS_ENCAMINHADAS = """
select m.criado_em as quando, p.primeiro_nome as paciente, m.motivo
  from public.mensagens m join public.pacientes p on p.id = m.paciente_id
 where m.nutricionista_id = %(nutri)s and m.direcao = 'saida' and m.acao = 'encaminhar'
   and m.criado_em >= now() - make_interval(days => %(dias)s)
 order by m.criado_em desc limit 10
"""

SQL_ULTIMAS_CONSULTAS = """
select a.inicio as quando, p.primeiro_nome as paciente, a.tipo, a.criado_em as marcada_em
  from public.agendamentos a join public.pacientes p on p.id = a.paciente_id
 where a.nutricionista_id = %(nutri)s and a.origem = 'bot' and a.status in ('confirmado', 'realizado')
   and a.criado_em >= now() - make_interval(days => %(dias)s)
 order by a.criado_em desc limit 10
"""

SQL_SEMANAIS = """
with alvo as (
  select n.id, n.relatorio_semanal_enviado_em as anterior
    from public.nutricionistas n
   where n.ativo and n.telefone_aviso is not null
     and (n.relatorio_semanal_enviado_em is null
          or n.relatorio_semanal_enviado_em < (now() at time zone n.fuso_horario)::date)
   for update skip locked
)
update public.nutricionistas n
   set relatorio_semanal_enviado_em = (now() at time zone n.fuso_horario)::date
  from alvo
 where n.id = alvo.id
returning n.id::text as nutri_id, n.nome, n.telefone_aviso, n.fuso_horario, alvo.anterior
"""


class RepositorioMetricas(Protocol):
    def metricas(self, nutri_id: str, dias: int, fuso: str = FUSO, detalhes: bool = False) -> Metricas: ...
    def nutri(self, nutri_id: str) -> dict | None: ...
    def reservar_semanais(self) -> list[dict]: ...
    def desfazer_semanal(self, nutri_id: str, anterior) -> None: ...


class RepositorioMetricasPostgres:
    def __init__(self, db):
        """db: bot.banco.Conexao (ou qualquer objeto com o mesmo executar())."""
        self._db = db

    def _todos(self, sql: str, params) -> list[dict]:
        return [dict(r) for r in self._db.executar(sql, params, retorno="todos", como_dict=True)]

    def metricas(self, nutri_id: str, dias: int, fuso: str = FUSO, detalhes: bool = False) -> Metricas:
        p = {"nutri": nutri_id, "dias": int(dias), "fuso": fuso, "abre": HORARIO_COMERCIAL[0], "fecha": HORARIO_COMERCIAL[1]}
        r = dict(self._db.executar(SQL_METRICAS, p, retorno="um", como_dict=True))
        valor = r.pop("valor_consulta")
        m = Metricas(dias=int(dias), valor_consulta=float(valor) if valor is not None else None, **{k: int(v) for k, v in r.items()})
        if detalhes:
            m.ultimas_encaminhadas = self._todos(SQL_ULTIMAS_ENCAMINHADAS, p)
            m.ultimas_consultas = self._todos(SQL_ULTIMAS_CONSULTAS, p)
        return m

    def nutri(self, nutri_id: str) -> dict | None:
        r = self._db.executar("select id::text as id, nome, fuso_horario from public.nutricionistas where id = %s and ativo",
                              (nutri_id,), retorno="um", como_dict=True)
        return dict(r) if r else None

    def reservar_semanais(self) -> list[dict]:
        return self._todos(SQL_SEMANAIS, ())

    def desfazer_semanal(self, nutri_id: str, anterior) -> None:
        self._db.executar("update public.nutricionistas set relatorio_semanal_enviado_em = %s where id = %s",
                          (anterior, nutri_id))


# ---------------------------------------------------------------------
# Link do painel (sem login: token assinado que expira)
# ---------------------------------------------------------------------
def _assinar(msg: str, segredo: str) -> str:
    sig = hmac.new(segredo.encode(), msg.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(sig).decode().rstrip("=")


def gerar_token(nutri_id: str, segredo: str, validade_dias: int = VALIDADE_LINK_DIAS, agora: float | None = None) -> str:
    expira = int((agora or time.time()) + validade_dias * 86400)
    corpo = f"{nutri_id}.{expira}"
    return f"{corpo}.{_assinar(corpo, segredo)}"


def validar_token(token: str, segredo: str, agora: float | None = None) -> str | None:
    """nutri_id se o token é autêntico e não expirou; senão None."""
    try:
        nutri_id, expira, sig = token.rsplit(".", 2)
        valido = hmac.compare_digest(sig, _assinar(f"{nutri_id}.{expira}", segredo))
        return nutri_id if valido and int(expira) > (agora or time.time()) else None
    except (ValueError, AttributeError):
        return None


def link_painel(url_base: str, nutri_id: str, segredo: str) -> str | None:
    if not url_base or not segredo:
        return None
    return f"{url_base.rstrip('/')}/painel/{gerar_token(nutri_id, segredo)}"


# ---------------------------------------------------------------------
# Relatório semanal
# ---------------------------------------------------------------------
def montar_relatorio_semanal(m: Metricas, link: str | None, inicio: datetime | None = None, fim: datetime | None = None) -> str:
    periodo = f" ({inicio:%d/%m} a {fim:%d/%m})" if inicio and fim else ""
    fora = f" ({m.duvidas_fora_horario} fora do horário comercial)" if m.duvidas_fora_horario else ""
    linhas = [
        f"📊 *Seu Consultório na Semana - Nutri Assistente*{periodo}",
        "",
        "💬 *Atendimento no WhatsApp:*",
        f"• {m.duvidas_respondidas} dúvidas de dieta resolvidas pelo assistente{fora}",
        f"• {m.duvidas_encaminhadas} casos clínicos encaminhados",
        "",
        "📅 *Agenda e Novos Pacientes:*",
        f"• {m.novos_leads} novos interessados atendidos",
        f"• {m.primeiras_consultas} primeiras consultas agendadas",
        f"• {m.retornos_recuperados} retornos resgatados após 35+ dias",
        "",
        "💰 *Impacto Estimado na sua Agenda:*",
    ]
    if m.receita_estimada is None:
        linhas.append("• Cadastre o valor da consulta para ver a estimativa em reais.")
    elif m.consultas_agendadas == 0:
        linhas.append("• Nenhuma consulta marcada pelo assistente nesta semana.")
    else:
        linhas.append(f"• Cerca de R$ {reais(m.receita_estimada)} em consultas viabilizadas ou mantidas!")
    if link:
        linhas += ["", f"👉 Acesse seu painel completo aqui: {link}"]
    return "\n".join(linhas)


def rodar_relatorios_semanais(repo: RepositorioMetricas, enviar: Enviar, link: Callable[[str], "str | None"] | None = None,
                              agora: datetime | None = None) -> int:
    """Uma vez por dia no máximo (reserva no banco); o cron chama só na segunda.
    link(nutri_id): link do painel (None = relatório sem link)."""
    enviados = 0
    for n in repo.reservar_semanais():
        fuso = n.get("fuso_horario") or FUSO
        fim = (agora or datetime.now(ZoneInfo(fuso))).astimezone(ZoneInfo(fuso))
        try:
            m = repo.metricas(n["nutri_id"], 7, fuso)
            texto = montar_relatorio_semanal(m, link(n["nutri_id"]) if link else None,
                                             fim - timedelta(days=7), fim - timedelta(days=1))
            enviar(n["telefone_aviso"], texto)
        except Exception as e:
            repo.desfazer_semanal(n["nutri_id"], n.get("anterior"))
            log.warning("relatório semanal não enviado (nutri=%s): %s", n["nutri_id"], type(e).__name__)
            continue
        enviados += 1
    log.info("relatórios semanais enviados: %d", enviados)
    return enviados
