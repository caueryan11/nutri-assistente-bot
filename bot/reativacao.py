"""Campanha de reativação da base: a implantação que "se paga na primeira semana".

  1. A nutri cola a lista de pacientes (nome; WhatsApp; última consulta) no painel; o sistema confere e importa.
  2. Escolhe o público: sem consulta há X dias e sem retorno marcado (quem pediu PARAR fica fora).
  3. Os envios saem aos poucos (poucos por minuto, seg-sáb 09h-20h, fuso da nutri) para não queimar o número.
  4. Quem responde recebe o pedido do termo (LGPD) e depois o assistente mostra os horários.
  5. O painel mostra enviados, responderam, agendaram e a receita estimada (agendaram × valor da consulta).
A mensagem sempre termina com a opção de PARAR. Texto provisório: a Ray revisa.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from typing import Callable

log = logging.getLogger("reativacao")

POR_MINUTO = 3            # ~180 por hora: ritmo de gente, não de disparo em massa
MAX_TENTATIVAS = 3
RECENCIA_DIAS = 90        # quem recebeu campanha nos últimos 90 dias não entra de novo
JANELA_RESPOSTA_DIAS = 30

TEXTO_PADRAO = ("Oi, {nome}! Aqui é o assistente da nutricionista {nutri}. Faz um tempinho desde a sua última "
                "consulta e ela lembrou de você. Que tal marcar um retorno para ver como você está? Responda SIM "
                "que eu te mostro os horários livres.")
RODAPE_PARAR = "(Se não quiser mais receber mensagens, responda PARAR.)"


def montar_mensagem(modelo: str, nome: str, nutri: str) -> str:
    texto = (modelo or TEXTO_PADRAO).replace("{nome}", nome).replace("{nutri}", nutri).strip()
    return texto if "PARAR" in texto.upper() else f"{texto}\n\n{RODAPE_PARAR}"


# ---------------------------------------------------------------------
# Lista colada pela nutri
# ---------------------------------------------------------------------
_DATA_BR = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})$")
_MES_ANO = re.compile(r"^(\d{1,2})/(\d{4})$")
_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


def ler_data(texto: str, hoje: date) -> date | None:
    t = (texto or "").strip()
    try:
        if m := _DATA_BR.match(t):
            ano = int(m.group(3)) + (2000 if len(m.group(3)) == 2 else 0)
            d = date(ano, int(m.group(2)), int(m.group(1)))
        elif m := _MES_ANO.match(t):
            d = date(int(m.group(2)), int(m.group(1)), 1)
        elif m := _ISO.match(t):
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        else:
            return None
    except ValueError:
        return None
    return d if d <= hoje else None


def ler_lista(texto: str, hoje: date) -> tuple[list[dict], list[str]]:
    """Uma pessoa por linha: nome; WhatsApp; última consulta (opcional). Aceita ; tab ou vírgula (copiar do Excel)."""
    from whatsapp.painel import normalizar_telefone

    validos, erros, vistos = [], [], set()
    for n, linha in enumerate((texto or "").splitlines(), start=1):
        if not linha.strip():
            continue
        sep = ";" if ";" in linha else "\t" if "\t" in linha else ","
        partes = [p.strip() for p in linha.split(sep)]
        if n == 1 and "nome" in partes[0].lower():
            continue  # cabeçalho
        if len(partes) < 2:
            erros.append(f"linha {n}: use nome; WhatsApp; última consulta")
            continue
        nome, tel = partes[0][:120], normalizar_telefone(partes[1])
        if len(nome.split()) < 1 or not re.search(r"[A-Za-zÀ-ÿ]", nome):
            erros.append(f"linha {n}: nome vazio")
            continue
        if not tel:
            erros.append(f"linha {n} ({nome}): WhatsApp inválido")
            continue
        if tel in vistos:
            erros.append(f"linha {n} ({nome}): WhatsApp repetido na lista")
            continue
        ultima = None
        if len(partes) > 2 and partes[2]:
            ultima = ler_data(partes[2], hoje)
            if ultima is None:
                erros.append(f"linha {n} ({nome}): data \"{partes[2]}\" não entendida (use dd/mm/aaaa); importado sem data")
        vistos.add(tel)
        validos.append({"nome": nome, "telefone": tel, "ultima_consulta": ultima})
    return validos, erros


# ---------------------------------------------------------------------
# Banco
# ---------------------------------------------------------------------
_PUBLICO = """
  from public.pacientes p
 where p.nutricionista_id = %(nutri)s and p.ativo and p.status_consentimento <> 'revogado'
   and not (p.acompanhamento_desde is not null and (p.acompanhamento_ate is null or p.acompanhamento_ate >= current_date))
   and not exists (select 1 from public.agendamentos f where f.paciente_id = p.id
                    and f.status in ('proposto', 'confirmado') and f.inicio >= now())
   and not exists (select 1 from public.campanha_envios e where e.paciente_id = p.id
                    and e.criado_em > now() - make_interval(days => %(recencia)s))
   and (
     greatest(p.ultima_consulta_em,
              (select max(a.inicio) from public.agendamentos a where a.paciente_id = p.id
                  and a.status in ('confirmado', 'realizado') and a.inicio < now()))
       < now() - make_interval(days => %(dias)s)
     or (%(sem_data)s and p.ultima_consulta_em is null
         and not exists (select 1 from public.agendamentos a where a.paciente_id = p.id and a.inicio < now()))
   )
"""

SQL_PULAR = """
update public.campanha_envios e set status = 'pulado'
  from public.pacientes p
 where p.id = e.paciente_id and e.status = 'pendente'
   and (not p.ativo or p.status_consentimento = 'revogado'
        or exists (select 1 from public.agendamentos f where f.paciente_id = p.id
                    and f.status in ('proposto', 'confirmado') and f.inicio >= now()))
"""

SQL_RESERVAR = """
with alvo as (
  select e.id
    from public.campanha_envios e
    join public.campanhas c on c.id = e.campanha_id
    join public.nutricionistas n on n.id = c.nutricionista_id
   where c.status = 'enviando' and e.status = 'pendente' and n.ativo
     and extract(isodow from now() at time zone n.fuso_horario) between 1 and 6
     and (now() at time zone n.fuso_horario)::time between time '09:00' and time '19:59'
   order by e.criado_em, e.id
   limit %s
   for update of e skip locked
)
update public.campanha_envios e
   set status = 'enviado', enviado_em = now(), tentativas = e.tentativas + 1
  from alvo, public.campanhas c, public.pacientes p, public.nutricionistas n
 where e.id = alvo.id and c.id = e.campanha_id and p.id = e.paciente_id and n.id = c.nutricionista_id
returning e.id::text as envio_id, e.tentativas, p.id::text as paciente_id, n.id::text as nutri_id,
          p.primeiro_nome, p.telefone, n.nome as nutri_nome, c.mensagem
"""


class RepositorioReativacaoPostgres:
    def __init__(self, db):
        self._db = db

    def _todos(self, sql, params=()):
        return [dict(r) for r in self._db.executar(sql, params, retorno="todos", como_dict=True)]

    # --- painel ---------------------------------------------------------
    def importar(self, nutri_id: str, pessoas: list[dict]) -> dict:
        criados = atualizados = 0
        for p in pessoas:
            r = self._db.executar(
                "insert into public.pacientes (nutricionista_id, nome, telefone, ultima_consulta_em) "
                "values (%s, %s, %s, %s) on conflict (nutricionista_id, telefone) do update set "
                "ultima_consulta_em = greatest(public.pacientes.ultima_consulta_em, excluded.ultima_consulta_em) "
                "returning (xmax = 0) as criado",
                (nutri_id, p["nome"], p["telefone"], p.get("ultima_consulta")), retorno="um")
            if r and r[0]:
                criados += 1
            else:
                atualizados += 1
        return {"criados": criados, "atualizados": atualizados}

    def publico(self, nutri_id: str, dias: int, sem_data: bool) -> dict:
        p = {"nutri": nutri_id, "dias": int(dias), "sem_data": bool(sem_data), "recencia": RECENCIA_DIAS}
        total = self._db.executar("select count(*) " + _PUBLICO, p, retorno="um")[0]
        exemplos = [r["nome"] for r in self._todos("select p.nome " + _PUBLICO + " order by p.nome limit 5", p)]
        return {"total": int(total), "exemplos": exemplos}

    def criar_campanha(self, nutri_id: str, mensagem: str, dias: int, sem_data: bool) -> tuple[str, int]:
        c = self._db.executar("insert into public.campanhas (nutricionista_id, mensagem, dias_sem_consulta, status) "
                              "values (%s, %s, %s, 'enviando') returning id::text", (nutri_id, mensagem, int(dias)),
                              retorno="um")[0]
        p = {"nutri": nutri_id, "dias": int(dias), "sem_data": bool(sem_data), "recencia": RECENCIA_DIAS, "camp": c}
        self._db.executar("insert into public.campanha_envios (campanha_id, nutricionista_id, paciente_id) "
                          "select %(camp)s, %(nutri)s, p.id " + _PUBLICO, p)
        total = self._db.executar("update public.campanhas set total = (select count(*) from public.campanha_envios "
                                  "where campanha_id = %s) where id = %s returning total", (c, c), retorno="um")[0]
        return c, int(total)

    def campanhas(self, nutri_id: str) -> list[dict]:
        return self._todos(
            "select c.id::text as id, c.criada_em, c.status, c.total, c.dias_sem_consulta, "
            "  count(*) filter (where e.status = 'enviado') as enviados, "
            "  count(*) filter (where e.status = 'pendente') as pendentes, "
            "  count(*) filter (where e.status in ('pulado', 'falhou')) as nao_enviados, "
            "  count(*) filter (where e.respondeu_em is not null) as responderam, "
            "  count(*) filter (where e.status = 'enviado' and exists (select 1 from public.agendamentos a "
            "     where a.paciente_id = e.paciente_id and a.status in ('confirmado', 'realizado') "
            "       and a.criado_em > e.enviado_em and a.criado_em < e.enviado_em + interval '30 days')) as agendaram "
            "from public.campanhas c left join public.campanha_envios e on e.campanha_id = c.id "
            "where c.nutricionista_id = %s group by c.id order by c.criada_em desc limit 10", (nutri_id,))

    def pausar(self, nutri_id: str, campanha_id: str, pausar: bool) -> None:
        self._db.executar("update public.campanhas set status = %s where id = %s and nutricionista_id = %s "
                          "and status in ('enviando', 'pausada')", ("pausada" if pausar else "enviando", campanha_id, nutri_id))

    # --- envio ------------------------------------------------------------
    def reservar_envios(self, limite: int) -> list[dict]:
        self._db.executar(SQL_PULAR)
        return self._todos(SQL_RESERVAR, (limite,))

    def devolver_envio(self, envio_id: str, tentativas: int) -> None:
        self._db.executar("update public.campanha_envios set status = %s, enviado_em = null where id = %s",
                          ("falhou" if tentativas >= MAX_TENTATIVAS else "pendente", envio_id))

    def concluir_campanhas(self) -> None:
        self._db.executar("update public.campanhas c set status = 'concluida' where c.status = 'enviando' "
                          "and not exists (select 1 from public.campanha_envios e "
                          "where e.campanha_id = c.id and e.status = 'pendente')")

    def registrar_proativa(self, nutri_id: str, paciente_id: str, texto: str, motivo: str, wa_id: str | None) -> None:
        self._db.executar(
            "insert into public.mensagens (nutricionista_id, paciente_id, direcao, wa_message_id, tipo, conteudo, motivo) "
            "values (%s, %s, 'saida', %s, 'texto', %s, %s)", (nutri_id, paciente_id, wa_id, texto, motivo))

    # --- webhook ----------------------------------------------------------
    def campanha_recente(self, paciente_id: str) -> bool:
        r = self._db.executar("select 1 from public.campanha_envios where paciente_id = %s and status = 'enviado' "
                              "and enviado_em > now() - make_interval(days => %s) limit 1",
                              (paciente_id, JANELA_RESPOSTA_DIAS), retorno="um")
        return r is not None

    def marcar_resposta(self, paciente_id: str) -> None:
        """Só o horário da resposta (nada do conteúdo): a métrica "responderam" funciona antes do aceite do termo."""
        self._db.executar("update public.campanha_envios set respondeu_em = now() where paciente_id = %s "
                          "and status = 'enviado' and respondeu_em is null "
                          "and enviado_em > now() - make_interval(days => %s)", (paciente_id, JANELA_RESPOSTA_DIAS))


def rodar_campanhas(repo, enviar: Callable[[str, str], "str | None"], por_minuto: int = POR_MINUTO) -> int:
    """Roda a cada minuto. Falha de envio devolve para a fila (até 3 tentativas) e para a rodada."""
    enviados = 0
    for e in repo.reservar_envios(por_minuto):
        texto = montar_mensagem(e["mensagem"], e["primeiro_nome"], e["nutri_nome"])
        try:
            wa_id = enviar(e["telefone"], texto)
        except Exception as ex:
            repo.devolver_envio(e["envio_id"], e["tentativas"])
            log.warning("reativação: envio falhou (%s); tenta de novo depois", type(ex).__name__)
            continue
        repo.registrar_proativa(e["nutri_id"], e["paciente_id"], texto, "campanha de reativação", wa_id)
        enviados += 1
    repo.concluir_campanhas()
    if enviados:
        log.info("reativação: %d mensagem(ns) enviada(s)", enviados)
    return enviados


def hoje_local(fuso: str) -> date:
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo(fuso)).date()
