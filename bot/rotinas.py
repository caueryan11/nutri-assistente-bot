"""Rotinas automáticas do consultório, rodando junto com o webhook no Railway (APScheduler).

  1. Lembrete 24 h antes da consulta          a cada 15 min
  2. Cobrança de retorno (paciente sumido)    todo dia às 10:00
  3. Resumo diário para a nutri               seg a sex às 08:00
  4. Relatório semanal (bot/metricas.py)      segunda às 08:30
  5. Retenção (LGPD): apaga conversas antigas todo dia às 03:30
  6. Monitor do WhatsApp (whatsapp/monitor.py) a cada 10 min
  7. Check-in semanal do acompanhamento (bot/checkin.py): a cada 15 min, envia no dia/hora de cada nutri

Tudo em America/Sao_Paulo. Só manda mensagem para paciente que aceitou o termo (LGPD).
Cada rotina "reserva" as linhas no banco antes de enviar (UPDATE ... RETURNING): se houver
duas cópias do app rodando (ex.: durante um deploy), ninguém recebe mensagem duplicada.
Se o envio falhar, a reserva é desfeita e a próxima execução tenta de novo.
Textos provisórios: a Ray revisa.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Callable, Protocol
from zoneinfo import ZoneInfo

from bot.avisos import curto

log = logging.getLogger("rotinas")

FUSO = "America/Sao_Paulo"
DIAS = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]
DIAS_CURTOS = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]

TEXTO_LEMBRETE = ("Olá, {nome}! Passando para lembrar da sua consulta com a Dra. {nutri} {quando}, {data} às {hora}. "
                  "Você confirma a sua presença? (Responda 'Sim' para confirmar ou avise se precisar remarcar)")
TEXTO_RETORNO = ("Olá, {nome}! Já faz um tempinho desde a sua última consulta com a Dra. {nutri}. Como você está se "
                 "adaptando ao plano? Vamos agendar o seu retorno para avaliarmos a sua evolução? Me avise por aqui "
                 "se quiser ver os horários livres!")
MAX_ENCAMINHADAS_NO_RESUMO = 10

Enviar = Callable[[str, str], "str | None"]  # (numero, texto) -> wa_message_id


# ---------------------------------------------------------------------
# Textos
# ---------------------------------------------------------------------
def texto_lembrete(primeiro_nome: str, nutri: str, inicio: datetime, agora: datetime) -> str:
    fuso = agora.tzinfo
    ini = inicio.astimezone(fuso)
    dias = (ini.date() - agora.astimezone(fuso).date()).days
    quando = "amanhã" if dias == 1 else ("hoje" if dias == 0 else DIAS[ini.isoweekday() - 1])
    return TEXTO_LEMBRETE.format(nome=primeiro_nome, nutri=nutri, quando=quando, data=f"{ini:%d/%m}", hora=f"{ini:%H:%M}")


def texto_retorno(primeiro_nome: str, nutri: str) -> str:
    return TEXTO_RETORNO.format(nome=primeiro_nome, nutri=nutri)


def montar_resumo(nutri: str, dados: dict, hoje: date, eventos_extras: list[dict] | None = None) -> str:
    """dados: respondidas, encaminhadas[{hora, paciente, motivo, resumo}], consultas_hoje[{hora, paciente, tipo}],
    avisos_hoje, avisos_semana, leads_novos, leads_convertidos. eventos_extras: eventos do Google de hoje que não foram marcados pelo bot.
    Ordem de leitura: o que precisa dela (urgentes primeiro), a agenda do dia e, por último, numa linha,
    o que o assistente já resolveu sozinho."""
    primeiro = nutri.split()[0] if nutri else ""
    linhas = [f"*Bom dia, {primeiro}!* Resumo do assistente ({DIAS_CURTOS[hoje.isoweekday() - 1]} {hoje:%d/%m})", ""]

    enc = sorted(dados.get("encaminhadas", []), key=lambda e: not _eh_urgente(e))  # sorted é estável: mantém a hora
    if enc:
        icone = "🔴" if any(_eh_urgente(e) for e in enc) else "🟡"
        linhas.append(f"{icone} *Precisam de você ({len(enc)})*")
        for e in enc[:MAX_ENCAMINHADAS_NO_RESUMO]:
            detalhe = curto(e.get("resumo") or e.get("motivo") or "", 140)
            marca = "🔴 " if _eh_urgente(e) else ""
            linhas.append(f"• {marca}*{e['paciente']}* ({e['hora']}): {detalhe}")
        if len(enc) > MAX_ENCAMINHADAS_NO_RESUMO:
            linhas.append(f"• e mais {len(enc) - MAX_ENCAMINHADAS_NO_RESUMO} no painel.")
    else:
        linhas.append("✅ Nada pendente para você.")

    consultas = [(c["hora"], f"{c['paciente']} ({c.get('tipo') or 'consulta'})") for c in dados.get("consultas_hoje", [])]
    consultas += [(e["hora"], f"{e['titulo']} (sua agenda)") for e in (eventos_extras or [])]
    linhas += ["", f"📅 *Hoje: {len(consultas)} consulta(s)*" if consultas else "📅 Nenhuma consulta hoje."]
    linhas += [f"• {hora} {texto}" for hora, texto in sorted(consultas)]

    feitos = [f"{dados.get('respondidas', 0)} dúvida(s) respondida(s)"]
    if dados.get("avisos_hoje") or dados.get("avisos_semana"):
        feitos.append(f"{dados.get('avisos_hoje', 0)} convite(s) de retorno hoje ({dados.get('avisos_semana', 0)} na semana)")
    if dados.get("leads_novos") or dados.get("leads_convertidos"):
        feitos.append(f"{dados.get('leads_novos', 0)} novo(s) contato(s), {dados.get('leads_convertidos', 0)} marcou(aram) consulta")
    linhas += ["", "🤖 *O assistente cuidou (24 h):* " + " · ".join(feitos)]
    return "\n".join(linhas)


def _eh_urgente(e: dict) -> bool:
    """Encaminhamento de risco (urgente: ... ) ou check-in com alerta."""
    motivo = (e.get("motivo") or "").lower()
    return motivo.startswith("urgente") or "alerta" in motivo


# ---------------------------------------------------------------------
# Banco
# ---------------------------------------------------------------------
class RepositorioRotinas(Protocol):
    def reservar_lembretes(self) -> list[dict]: ...
    def desfazer_lembrete(self, agendamento_id: str) -> None: ...
    def reservar_retornos(self) -> list[dict]: ...
    def desfazer_retorno(self, paciente_id: str, anterior) -> None: ...
    def reservar_resumos(self) -> list[dict]: ...
    def desfazer_resumo(self, nutri_id: str, anterior) -> None: ...
    def dados_resumo(self, nutri_id: str, fuso: str) -> dict: ...
    def registrar_proativa(self, nutri_id: str, paciente_id: str, texto: str, motivo: str, wa_id: str | None) -> None: ...
    def limpar_antigos(self, dias_pacientes: int, dias_leads: int) -> dict: ...
    def reservar_checkins(self) -> list[dict]: ...
    def reservar_fim_acompanhamento(self) -> list[dict]: ...
    def desfazer_fim_acompanhamento(self, paciente_id: str, anterior) -> None: ...
    def desfazer_checkin(self, checkin_id: str) -> None: ...


SQL_LEMBRETES = """
update public.agendamentos a
   set lembrete_enviado_em = now()
  from public.pacientes p, public.nutricionistas n
 where p.id = a.paciente_id and n.id = a.nutricionista_id
   and a.status = 'confirmado' and a.lembrete_enviado_em is null
   and a.inicio >= now() + interval '23 hours 30 minutes'
   and a.inicio <  now() + interval '24 hours 30 minutes'
   and p.ativo and n.ativo and p.consentimento_em is not null
returning a.id::text as agendamento_id, a.inicio, a.paciente_id::text as paciente_id,
          a.nutricionista_id::text as nutri_id, p.primeiro_nome, p.telefone, n.nome as nutri_nome
"""

SQL_RETORNOS = """
with ultima as (
  select p.id,
         greatest(p.ultima_consulta_em,
                  (select max(a.inicio) from public.agendamentos a
                    where a.paciente_id = p.id and a.status in ('confirmado', 'realizado') and a.inicio < now())) as em
    from public.pacientes p
), alvo as (
  select p.id, p.ultimo_aviso_retorno_em as anterior
    from public.pacientes p
    join ultima u on u.id = p.id
    join public.nutricionistas n on n.id = p.nutricionista_id
   where p.ativo and n.ativo and p.consentimento_em is not null
     and u.em >= now() - interval '60 days' and u.em < now() - interval '35 days'
     and (p.ultimo_aviso_retorno_em is null or p.ultimo_aviso_retorno_em < now() - interval '30 days')
     and not exists (select 1 from public.agendamentos f
                      where f.paciente_id = p.id and f.status in ('proposto', 'confirmado') and f.inicio >= now())
   for update of p skip locked
)
update public.pacientes p
   set ultimo_aviso_retorno_em = now()
  from alvo, public.nutricionistas n
 where p.id = alvo.id and n.id = p.nutricionista_id
returning p.id::text as paciente_id, p.nutricionista_id::text as nutri_id, p.primeiro_nome, p.telefone,
          n.nome as nutri_nome, alvo.anterior
"""

SQL_RESUMOS = """
with alvo as (
  select n.id, n.resumo_enviado_em as anterior
    from public.nutricionistas n
   where n.ativo and n.telefone_aviso is not null
     and (n.resumo_enviado_em is null or n.resumo_enviado_em < (now() at time zone n.fuso_horario)::date)
   for update skip locked
)
update public.nutricionistas n
   set resumo_enviado_em = (now() at time zone n.fuso_horario)::date
  from alvo
 where n.id = alvo.id
returning n.id::text as nutri_id, n.nome, n.telefone_aviso, n.fuso_horario, n.google_calendar_id, alvo.anterior
"""


class RepositorioRotinasPostgres:
    def __init__(self, db):
        """db: bot.banco.Conexao (ou qualquer objeto com o mesmo executar())."""
        self._db = db

    @classmethod
    def de_url(cls, dsn: str) -> "RepositorioRotinasPostgres":
        from bot.banco import Conexao

        return cls(Conexao(dsn))

    def _todos(self, sql: str, params: tuple = ()) -> list[dict]:
        return [dict(r) for r in self._db.executar(sql, params, retorno="todos", como_dict=True)]

    def reservar_lembretes(self) -> list[dict]:
        return self._todos(SQL_LEMBRETES)

    def desfazer_lembrete(self, agendamento_id: str) -> None:
        self._db.executar("update public.agendamentos set lembrete_enviado_em = null where id = %s", (agendamento_id,))

    def reservar_retornos(self) -> list[dict]:
        return self._todos(SQL_RETORNOS)

    def desfazer_retorno(self, paciente_id: str, anterior) -> None:
        self._db.executar("update public.pacientes set ultimo_aviso_retorno_em = %s where id = %s", (anterior, paciente_id))

    def reservar_resumos(self) -> list[dict]:
        return self._todos(SQL_RESUMOS)

    def desfazer_resumo(self, nutri_id: str, anterior) -> None:
        self._db.executar("update public.nutricionistas set resumo_enviado_em = %s where id = %s", (anterior, nutri_id))

    def dados_resumo(self, nutri_id: str, fuso: str) -> dict:
        respondidas = self._db.executar(
            "select count(*) from public.mensagens where nutricionista_id = %s and direcao = 'saida' "
            "and acao = 'responder' and criado_em >= now() - interval '24 hours'", (nutri_id,), retorno="um")[0]
        encaminhadas = self._todos(
            "select to_char(m.criado_em at time zone %s, 'HH24:MI') as hora, p.nome as paciente, m.motivo, m.resumo_para_nutri as resumo "
            "from public.mensagens m join public.pacientes p on p.id = m.paciente_id "
            "where m.nutricionista_id = %s and m.direcao = 'saida' and m.acao = 'encaminhar' "
            "and m.criado_em >= now() - interval '24 hours' order by m.criado_em", (fuso, nutri_id))
        consultas = self._todos(
            "select to_char(a.inicio at time zone %s, 'HH24:MI') as hora, p.nome as paciente, a.tipo, a.google_event_id "
            "from public.agendamentos a join public.pacientes p on p.id = a.paciente_id "
            "where a.nutricionista_id = %s and a.status = 'confirmado' "
            "and (a.inicio at time zone %s)::date = (now() at time zone %s)::date order by a.inicio",
            (fuso, nutri_id, fuso, fuso))
        avisos = self._db.executar(
            "select count(*) filter (where (ultimo_aviso_retorno_em at time zone %s)::date = (now() at time zone %s)::date), "
            "count(*) filter (where ultimo_aviso_retorno_em >= now() - interval '7 days') "
            "from public.pacientes where nutricionista_id = %s", (fuso, fuso, nutri_id), retorno="um")
        leads = self._db.executar(
            "select count(*) filter (where criado_em >= now() - interval '24 hours'), "
            "count(*) filter (where status = 'convertido' and atualizado_em >= now() - interval '24 hours') "
            "from public.leads where nutricionista_id = %s", (nutri_id,), retorno="um")
        return {"respondidas": respondidas, "encaminhadas": encaminhadas, "consultas_hoje": consultas,
                "avisos_hoje": avisos[0], "avisos_semana": avisos[1],
                "leads_novos": leads[0], "leads_convertidos": leads[1]}

    def reservar_checkins(self) -> list[dict]:
        from bot.checkin import SQL_RESERVAR

        return self._todos(SQL_RESERVAR)

    def desfazer_checkin(self, checkin_id: str) -> None:
        self._db.executar("delete from public.checkins where id = %s and status = 'enviado'", (checkin_id,))

    def reservar_fim_acompanhamento(self) -> list[dict]:
        from bot.acompanhamento import SQL_FIM

        return self._todos(SQL_FIM)

    def desfazer_fim_acompanhamento(self, paciente_id: str, anterior) -> None:
        self._db.executar("update public.pacientes set aviso_fim_acompanhamento_em = %s where id = %s",
                          (anterior, paciente_id))

    def limpar_antigos(self, dias_pacientes: int, dias_leads: int) -> dict:
        """Retenção: mensagens de pacientes e de leads mais antigas que o prazo; leads que não viraram
        paciente e estão parados há mais que o prazo dos leads (some o telefone de quem não voltou)."""
        mensagens = self._db.executar("select public.limpar_mensagens_antigas(%s)", (dias_pacientes,), retorno="um")[0]
        lead_msgs = self._db.executar("select public.limpar_lead_mensagens_antigas(%s)", (dias_leads,), retorno="um")[0]
        leads = self._db.executar(
            "with apagados as (delete from public.leads where status <> 'convertido' "
            "and atualizado_em < now() - make_interval(days => %s) returning 1) select count(*) from apagados",
            (dias_leads,), retorno="um")[0]
        # rascunhos guardam a pergunta do paciente (dado de saúde): mesmo prazo das mensagens
        rascunhos = self._db.executar(
            "with apagados as (delete from public.rascunhos where criado_em < now() - make_interval(days => %s) "
            "returning 1) select count(*) from apagados", (dias_pacientes,), retorno="um")[0]
        return {"mensagens": mensagens, "lead_mensagens": lead_msgs, "leads": leads, "rascunhos": rascunhos}

    def registrar_proativa(self, nutri_id: str, paciente_id: str, texto: str, motivo: str, wa_id: str | None) -> None:
        """Guarda no histórico: quando o paciente responder "Sim", o bot entende a que ele respondeu."""
        self._db.executar(
            "insert into public.mensagens (nutricionista_id, paciente_id, direcao, wa_message_id, tipo, conteudo, motivo) "
            "values (%s, %s, 'saida', %s, 'texto', %s, %s)", (nutri_id, paciente_id, wa_id, texto, motivo))


# ---------------------------------------------------------------------
# Rotinas
# ---------------------------------------------------------------------
def rodar_lembretes(repo: RepositorioRotinas, enviar: Enviar, agora: datetime | None = None) -> int:
    agora = agora or datetime.now(ZoneInfo(FUSO))
    enviados = 0
    for c in repo.reservar_lembretes():
        texto = texto_lembrete(c["primeiro_nome"], c["nutri_nome"], c["inicio"], agora)
        try:
            wa_id = enviar(c["telefone"], texto)
        except Exception as e:
            repo.desfazer_lembrete(c["agendamento_id"])
            log.warning("lembrete não enviado (agendamento=%s): %s", c["agendamento_id"], type(e).__name__)
            continue
        repo.registrar_proativa(c["nutri_id"], c["paciente_id"], texto, "lembrete 24 h", wa_id)
        enviados += 1
    log.info("lembretes 24 h enviados: %d", enviados)
    return enviados


def rodar_retornos(repo: RepositorioRotinas, enviar: Enviar) -> int:
    enviados = 0
    for p in repo.reservar_retornos():
        texto = texto_retorno(p["primeiro_nome"], p["nutri_nome"])
        try:
            wa_id = enviar(p["telefone"], texto)
        except Exception as e:
            repo.desfazer_retorno(p["paciente_id"], p.get("anterior"))
            log.warning("cobrança de retorno não enviada (paciente=%s): %s", p["paciente_id"], type(e).__name__)
            continue
        repo.registrar_proativa(p["nutri_id"], p["paciente_id"], texto, "cobrança de retorno", wa_id)
        enviados += 1
    log.info("cobranças de retorno enviadas: %d", enviados)
    return enviados


def _eventos_extras(calendario, nutri: dict, consultas: list[dict], agora: datetime) -> list[dict]:
    """Eventos de hoje no Google que não foram marcados pelo bot (ex.: a nutri marcou à mão)."""
    if calendario is None or not nutri.get("google_calendar_id"):
        return []
    inicio = agora.replace(hour=0, minute=0, second=0, microsecond=0)
    do_bot = {c.get("google_event_id") for c in consultas}
    try:
        eventos = calendario.listar_eventos(nutri["google_calendar_id"], inicio, inicio + timedelta(days=1))
    except Exception as e:  # Google fora do ar não impede o resumo
        log.warning("resumo sem eventos do Google: %s", type(e).__name__)
        return []
    return [{"hora": f"{e['inicio'].astimezone(agora.tzinfo):%H:%M}", "titulo": e["titulo"] or "(sem título)"}
            for e in eventos if e["id"] not in do_bot]


def rodar_resumos(repo: RepositorioRotinas, enviar: Enviar, calendario=None, agora: datetime | None = None) -> int:
    enviados = 0
    for n in repo.reservar_resumos():
        fuso = ZoneInfo(n.get("fuso_horario") or FUSO)
        agora_n = (agora or datetime.now(fuso)).astimezone(fuso)
        try:
            dados = repo.dados_resumo(n["nutri_id"], str(fuso))
            texto = montar_resumo(n["nome"], dados, agora_n.date(),
                                  _eventos_extras(calendario, n, dados.get("consultas_hoje", []), agora_n))
            enviar(n["telefone_aviso"], texto)
        except Exception as e:
            repo.desfazer_resumo(n["nutri_id"], n.get("anterior"))
            log.warning("resumo não enviado (nutri=%s): %s", n["nutri_id"], type(e).__name__)
            continue
        enviados += 1
    log.info("resumos diários enviados: %d", enviados)
    return enviados


def rodar_retencao(repo: RepositorioRotinas, dias_pacientes: int, dias_leads: int) -> dict:
    apagados = repo.limpar_antigos(dias_pacientes, dias_leads)
    log.info("retenção: apagadas %d mensagem(ns) de pacientes, %d de leads, %d lead(s) parado(s) e %d rascunho(s)",
             apagados["mensagens"], apagados["lead_mensagens"], apagados["leads"], apagados.get("rascunhos", 0))
    return apagados


# ---------------------------------------------------------------------
# Agendador
# ---------------------------------------------------------------------
def criar_agendador(repo: RepositorioRotinas, enviar: Enviar, calendario=None, metricas=None,
                    link_painel: Callable[[str], "str | None"] | None = None,
                    retencao: tuple[int, int] | None = None, monitor=None, reativacao=None):
    """retencao: (dias das mensagens de pacientes, dias dos leads); None = não apaga.
    monitor: whatsapp.monitor.MonitorWhatsApp; None = sem monitor."""
    """AsyncIOScheduler em America/Sao_Paulo. As rotinas são síncronas (banco e HTTP):
    o APScheduler as roda numa thread, sem travar o webhook."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    fuso = ZoneInfo(FUSO)
    ag = AsyncIOScheduler(timezone=fuso, job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 600})
    ag.add_job(rodar_lembretes, IntervalTrigger(minutes=15, timezone=fuso), args=[repo, enviar],
               id="lembrete_24h", name="Lembrete 24 h antes da consulta")
    ag.add_job(rodar_retornos, CronTrigger(hour=10, minute=0, timezone=fuso), args=[repo, enviar],
               id="cobranca_retorno", name="Cobrança de retorno")
    ag.add_job(rodar_resumos, CronTrigger(day_of_week="mon-fri", hour=8, minute=0, timezone=fuso),
               args=[repo, enviar, calendario], id="resumo_8h", name="Resumo diário das 8h")
    if metricas is not None:
        from bot.metricas import rodar_relatorios_semanais

        ag.add_job(rodar_relatorios_semanais, CronTrigger(day_of_week="mon", hour=8, minute=30, timezone=fuso),
                   args=[metricas, enviar, link_painel], id="relatorio_semanal", name="Relatório semanal (segunda 08:30)")
    from bot.checkin import rodar_checkins

    ag.add_job(rodar_checkins, IntervalTrigger(minutes=15, timezone=fuso), args=[repo, enviar],
               id="checkin_semanal", name="Check-in semanal do acompanhamento")
    from bot.acompanhamento import rodar_fim_acompanhamento

    if reativacao is not None:
        from bot.reativacao import rodar_campanhas

        ag.add_job(rodar_campanhas, IntervalTrigger(minutes=1, timezone=fuso), args=[reativacao, enviar],
                   id="reativacao", name="Campanha de reativação (poucos envios por minuto)")

    ag.add_job(rodar_fim_acompanhamento, CronTrigger(hour=10, minute=30, timezone=fuso), args=[repo, enviar],
               id="fim_acompanhamento", name="Aviso de fim do acompanhamento (convite ao retorno)")
    if retencao is not None:
        ag.add_job(rodar_retencao, CronTrigger(hour=3, minute=30, timezone=fuso), args=[repo, *retencao],
                   id="retencao", name="Retenção: apaga conversas antigas (03:30)")
    if monitor is not None:
        from datetime import datetime as _dt

        ag.add_job(monitor.conferir, IntervalTrigger(minutes=10, timezone=fuso), next_run_time=_dt.now(fuso),
                   id="monitor_whatsapp", name="Monitor do WhatsApp (10 min)")
    return ag
