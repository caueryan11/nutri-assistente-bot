"""Acesso ao banco para o webhook (service_role / DATABASE_URL do backend)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from bot.assistente import Resultado
from whatsapp.uazapi import MensagemRecebida


@dataclass
class Nutri:
    id: str
    nome: str
    telefone_aviso: str | None
    google_calendar_id: str | None = None
    agenda_config: dict | None = None
    fuso_horario: str = "America/Sao_Paulo"
    atende_convenio: bool = False
    convenios_aceitos: list[str] = field(default_factory=list)
    valor_consulta: object | None = None
    politica_reembolso: str | None = None
    apresentacao_servico: str | None = None
    modelo_acompanhamento: str = "todos"  # 'todos' | 'por_paciente'


@dataclass
class Paciente:
    id: str
    nome: str
    primeiro_nome: str
    telefone: str
    consentimento_em: object | None
    anamnese: dict = field(default_factory=dict)
    status_consentimento: str = "pendente"  # pendente | aceito | revogado
    acompanhamento_ativo: bool = False     # comprou a consulta com acompanhamento e ainda vale
    termo_pedido_em: object | None = None  # quando o pedido do termo foi mostrado (o aceite só vale depois)


def variantes_telefone(telefone: str) -> list[str]:
    """Celular brasileiro pode chegar com ou sem o 9 (ex.: 5511999990001 / 551199990001)."""
    v = {telefone}
    if telefone.startswith("55") and len(telefone) == 13 and telefone[4] == "9":
        v.add(telefone[:4] + telefone[5:])
    if telefone.startswith("55") and len(telefone) == 12:
        v.add(telefone[:4] + "9" + telefone[4:])
    return sorted(v | {"+" + t for t in v})


class Repositorio(Protocol):
    def nutri_por_instancia(self, instancia: str) -> Nutri | None: ...
    def paciente(self, nutri_id: str, telefone: str) -> Paciente | None: ...
    def registrar_entrada(self, nutri: Nutri, paciente: Paciente, msg: MensagemRecebida) -> bool: ...
    def plano_ativo(self, paciente_id: str) -> dict | None: ...
    def historico(self, paciente_id: str, antes_de_wa_id: str, limite: int = 6) -> list[tuple[str, str]]: ...
    def registrar_saida(self, nutri: Nutri, paciente: Paciente, r: Resultado, wa_id: str | None) -> None: ...
    def registrar_agendamento(self, nutri_id: str, paciente_id: str, inicio, fim, google_event_id: str) -> None: ...
    def registrar_consentimento(self, paciente_id: str, versao_termo: str) -> bool: ...
    def registrar_revogacao(self, paciente_id: str) -> bool: ...
    def marcar_pedido_termo(self, paciente_id: str) -> None: ...
    def checkin_pendente(self, paciente_id: str) -> dict | None: ...
    def salvar_checkin(self, checkin_id: str, campos: dict) -> None: ...
    def peso_anterior(self, paciente_id: str, antes_de) -> float | None: ...
    def consultas_futuras(self, paciente_id: str) -> list[dict]: ...
    def cancelar_agendamento(self, agendamento_id: str, paciente_id: str) -> None: ...
    # leads (quem ainda não é paciente)
    def lead(self, nutri_id: str, telefone: str) -> dict: ...
    def atualizar_lead(self, lead_id: str, campos: dict) -> None: ...
    def registrar_lead_mensagem(self, lead_id: str, direcao: str, wa_id: str | None, conteudo: str | None) -> bool: ...
    def historico_lead(self, lead_id: str, antes_de_wa_id: str, limite: int = 10) -> list[tuple[str, str]]: ...
    def converter_lead(self, lead_id: str, nutri_id: str, nome: str, telefone: str, inicio, fim,
                       google_event_id: str, convenio: str) -> str: ...
    # rascunhos de resposta (avisos que a nutri aprova com "sim" ou edita)
    def criar_rascunho(self, nutri_id: str, paciente_id: str, pergunta: str, rascunho, aviso_wa_id: str | None) -> None: ...
    def rascunho_por_aviso(self, nutri_id: str, aviso_wa_id: str) -> dict | None: ...
    def rascunhos_pendentes(self, nutri_id: str) -> list[dict]: ...
    def decidir_rascunho(self, rascunho_id: str, status: str, texto_enviado: str | None) -> bool: ...
    def guardar_versao_nutri(self, rascunho_id: str, texto: str) -> None: ...


HORAS_RASCUNHO = 48  # depois disso a sugestão não é mais enviada (a conversa já andou)
SQL_RASCUNHO = (
    "select r.id::text as id, r.paciente_id::text as paciente_id, p.nome, p.primeiro_nome, p.telefone, "
    "p.consentimento_em, r.resposta, r.versao_nutri, r.status, "
    "r.criado_em < now() - make_interval(hours => %s) as expirado "
    "from public.rascunhos r join public.pacientes p on p.id = r.paciente_id where r.nutricionista_id = %s ")


class RepositorioPostgres:
    def __init__(self, dsn: str):
        from bot.banco import Conexao

        self._db = Conexao(dsn)

    def _um(self, sql: str, params: tuple) -> dict | None:
        return self._db.executar(sql, params, retorno="um", como_dict=True)

    def nutri_por_instancia(self, instancia: str) -> Nutri | None:
        # whatsapp_phone_number_id guarda o id do canal: nome da instância na uazapi
        # (ou phone_number_id, se um dia for a Cloud API da Meta).
        r = self._um(
            "select id::text, nome, telefone_aviso, google_calendar_id, agenda_config, fuso_horario, "
            "atende_convenio, convenios_aceitos, valor_consulta, politica_reembolso, apresentacao_servico, "
            "modelo_acompanhamento "
            "from public.nutricionistas "
            "where whatsapp_phone_number_id = %s and ativo",
            (instancia,),
        )
        return Nutri(**r) if r else None

    def paciente(self, nutri_id: str, telefone: str) -> Paciente | None:
        r = self._um(
            "select p.id::text, p.nome, p.primeiro_nome, p.telefone, p.consentimento_em, p.anamnese, "
            "p.status_consentimento, p.termo_pedido_em, (p.acompanhamento_desde is not null and (p.acompanhamento_ate is null or p.acompanhamento_ate >= (now() at time zone n.fuso_horario)::date)) as acompanhamento_ativo "
            "from public.pacientes p join public.nutricionistas n on n.id = p.nutricionista_id "
            "where p.nutricionista_id = %s and p.telefone = any(%s) and p.ativo",
            (nutri_id, variantes_telefone(telefone)),
        )
        return Paciente(**r) if r else None

    def registrar_entrada(self, nutri: Nutri, paciente: Paciente, msg: MensagemRecebida) -> bool:
        """False se a mensagem já foi registrada (evento repetido)."""
        r = self._um(
            "insert into public.mensagens (nutricionista_id, paciente_id, direcao, wa_message_id, tipo, conteudo) "
            "values (%s, %s, 'entrada', %s, %s, %s) on conflict (wa_message_id) do nothing returning id",
            (nutri.id, paciente.id, msg.id, msg.tipo, msg.texto or None),
        )
        return r is not None

    def plano_ativo(self, paciente_id: str) -> dict | None:
        r = self._um("select conteudo from public.planos_alimentares where paciente_id = %s and ativo", (paciente_id,))
        return r["conteudo"] if r else None

    def historico(self, paciente_id: str, antes_de_wa_id: str, limite: int = 6) -> list[tuple[str, str]]:
        linhas = self._db.executar(
            "select direcao, conteudo from public.mensagens "
            "where paciente_id = %s and conteudo is not null and wa_message_id is distinct from %s "
            "order by criado_em desc limit %s",
            (paciente_id, antes_de_wa_id, limite),
            retorno="todos",
        )
        return list(reversed(linhas))

    def registrar_saida(self, nutri: Nutri, paciente: Paciente, r: Resultado, wa_id: str | None) -> None:
        self._db.executar(
            "insert into public.mensagens (nutricionista_id, paciente_id, direcao, wa_message_id, tipo, conteudo, "
            "acao, motivo, resumo_para_nutri, tokens_entrada, tokens_saida) "
            "values (%s, %s, 'saida', %s, 'texto', %s, %s, %s, %s, %s, %s)",
            (nutri.id, paciente.id, wa_id, r.mensagem_paciente, r.acao, r.motivo or None,
             r.resumo_para_nutri or None, r.tokens_entrada or None, r.tokens_saida or None),
        )

    def registrar_agendamento(self, nutri_id: str, paciente_id: str, inicio, fim, google_event_id: str) -> None:
        """Levanta HorarioOcupado se o banco recusar por sobreposição (índice de exclusão)."""
        import psycopg2.errors

        from agenda.servico import HorarioOcupado

        try:
            self._db.executar(
                "insert into public.agendamentos (nutricionista_id, paciente_id, inicio, fim, status, origem, google_event_id) "
                "values (%s, %s, %s, %s, 'confirmado', 'bot', %s)",
                (nutri_id, paciente_id, inicio, fim, google_event_id),
            )
        except psycopg2.errors.ExclusionViolation as e:
            raise HorarioOcupado() from e

    def registrar_consentimento(self, paciente_id: str, versao_termo: str) -> bool:
        """Grava o aceite do termo. False se o paciente já tinha aceitado (evento repetido)."""
        r = self._db.executar(
            "update public.pacientes set consentimento_em = now(), versao_termo = %s, status_consentimento = 'aceito' "
            "where id = %s and consentimento_em is null returning id",
            (versao_termo, paciente_id),
            retorno="um",
        )
        return r is not None

    def marcar_pedido_termo(self, paciente_id: str) -> None:
        self._db.executar("update public.pacientes set termo_pedido_em = now() where id = %s", (paciente_id,))

    def registrar_revogacao(self, paciente_id: str) -> bool:
        """Revoga o consentimento (paciente respondeu PARAR). False se já estava revogado."""
        r = self._db.executar(
            "update public.pacientes set consentimento_em = null, status_consentimento = 'revogado', "
            "consentimento_revogado_em = now() where id = %s and status_consentimento <> 'revogado' returning id",
            (paciente_id,), retorno="um")
        return r is not None

    # --- check-in semanal ---------------------------------------------------
    def checkin_pendente(self, paciente_id: str) -> dict | None:
        """Check-in enviado nas últimas 72 h que ainda espera resposta."""
        return self._um(
            "select id::text as id, semana, adesao, fome, intestino, peso_kg, comentario, alerta, lembrete_enviado "
            "from public.checkins where paciente_id = %s and status = 'enviado' "
            "and enviado_em > now() - interval '72 hours' order by enviado_em desc limit 1", (paciente_id,))

    def salvar_checkin(self, checkin_id: str, campos: dict) -> None:
        permitidos = ("adesao", "fome", "intestino", "peso_kg", "comentario", "alerta", "alerta_motivo", "status",
                      "lembrete_enviado")
        campos = {k: v for k, v in campos.items() if k in permitidos}
        sets = ", ".join(f"{k} = %s" for k in campos)  # nomes da lista fixa
        extra = ", respondido_em = now()" if campos.get("status") == "respondido" else ""
        self._db.executar(f"update public.checkins set {sets}{extra} where id = %s", (*campos.values(), checkin_id))

    def peso_anterior(self, paciente_id: str, antes_de) -> float | None:
        r = self._um("select peso_kg from public.checkins where paciente_id = %s and semana < %s "
                     "and peso_kg is not null order by semana desc limit 1", (paciente_id, antes_de))
        return float(r["peso_kg"]) if r else None

    def consultas_futuras(self, paciente_id: str) -> list[dict]:
        linhas = self._db.executar(
            "select id::text as id, inicio, fim, google_event_id from public.agendamentos "
            "where paciente_id = %s and status = 'confirmado' and inicio > now() order by inicio",
            (paciente_id,), retorno="todos", como_dict=True)
        return [dict(l) for l in linhas]

    def cancelar_agendamento(self, agendamento_id: str, paciente_id: str) -> None:
        # paciente_id no filtro: o bot nunca cancela consulta de outro paciente
        self._db.executar(
            "update public.agendamentos set status = 'cancelado' where id = %s and paciente_id = %s",
            (agendamento_id, paciente_id))

    # --- leads ----------------------------------------------------------------
    def lead(self, nutri_id: str, telefone: str) -> dict:
        """Lead deste telefone (cria se for o primeiro contato)."""
        r = self._um(
            "insert into public.leads (nutricionista_id, telefone) values (%s, %s) "
            "on conflict (nutricionista_id, telefone) do update set telefone = excluded.telefone "
            "returning id::text, nome, objetivo, convenio_informado, status",
            (nutri_id, telefone),
        )
        return dict(r)

    def atualizar_lead(self, lead_id: str, campos: dict) -> None:
        permitidos = {"nome", "objetivo", "convenio_informado", "status"}
        campos = {k: v for k, v in campos.items() if k in permitidos}
        if not campos:
            return
        sets = ", ".join(f"{k} = %s" for k in campos)  # nomes vêm da lista fixa acima
        # lead convertido não volta para triagem
        self._db.executar(f"update public.leads set {sets} where id = %s and status <> 'convertido'",
                          (*campos.values(), lead_id))

    def registrar_lead_mensagem(self, lead_id: str, direcao: str, wa_id: str | None, conteudo: str | None) -> bool:
        """False se a mensagem já foi registrada (evento repetido)."""
        r = self._um(
            "insert into public.lead_mensagens (lead_id, direcao, wa_message_id, conteudo) values (%s, %s, %s, %s) "
            "on conflict (wa_message_id) do nothing returning id",
            (lead_id, direcao, wa_id, conteudo),
        )
        return r is not None

    def historico_lead(self, lead_id: str, antes_de_wa_id: str, limite: int = 10) -> list[tuple[str, str]]:
        linhas = self._db.executar(
            "select direcao, conteudo from public.lead_mensagens "
            "where lead_id = %s and conteudo is not null and wa_message_id is distinct from %s "
            "order by criado_em desc limit %s",
            (lead_id, antes_de_wa_id, limite), retorno="todos")
        return list(reversed(linhas))

    def converter_lead(self, lead_id: str, nutri_id: str, nome: str, telefone: str, inicio, fim,
                       google_event_id: str, convenio: str) -> str:
        """Numa só instrução (atômica): cria/reativa o paciente (sem consentimento), grava a primeira
        consulta e marca o lead como convertido. Levanta HorarioOcupado se o horário foi ocupado."""
        import psycopg2.errors

        from agenda.servico import HorarioOcupado

        try:
            r = self._um(
                "with pac as ("
                "  insert into public.pacientes (nutricionista_id, nome, telefone) values (%(nutri)s, %(nome)s, %(tel)s) "
                "  on conflict (nutricionista_id, telefone) do update set nome = excluded.nome, ativo = true "
                "  returning id), "
                "ag as ("
                "  insert into public.agendamentos (nutricionista_id, paciente_id, inicio, fim, tipo, status, origem, "
                "                                   google_event_id, convenio) "
                "  select %(nutri)s, id, %(ini)s, %(fim)s, 'primeira', 'confirmado', 'bot', %(evento)s, %(conv)s from pac "
                "  returning paciente_id) "
                "update public.leads set status = 'convertido', nome = %(nome)s, "
                "  convenio_informado = coalesce(nullif(%(conv)s, ''), convenio_informado), "
                "  paciente_id = (select paciente_id from ag) "
                "where id = %(lead)s returning paciente_id::text",
                {"nutri": nutri_id, "nome": nome, "tel": telefone, "ini": inicio, "fim": fim,
                 "evento": google_event_id, "conv": convenio or None, "lead": lead_id},
            )
        except psycopg2.errors.ExclusionViolation as e:
            raise HorarioOcupado() from e
        return r["paciente_id"]

    # --- rascunhos de resposta ---------------------------------------------------
    def criar_rascunho(self, nutri_id: str, paciente_id: str, pergunta: str, rascunho, aviso_wa_id: str | None) -> None:
        """Uma linha por aviso de encaminhamento (com ou sem sugestão): citar o aviso liga a resposta ao paciente."""
        import json

        self._db.executar(
            "insert into public.rascunhos (nutricionista_id, paciente_id, pergunta, resposta, fontes, atencao, "
            "confianca, aviso_wa_id, buscas, tokens_entrada, tokens_saida) "
            "values (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s)",
            (nutri_id, paciente_id, pergunta, (rascunho.resposta or None) if rascunho else None,
             json.dumps(rascunho.fontes if rascunho else [], ensure_ascii=False),
             (rascunho.atencao or None) if rascunho else None, rascunho.confianca if rascunho else None,
             aviso_wa_id, rascunho.buscas if rascunho else None,
             rascunho.tokens_entrada if rascunho else None, rascunho.tokens_saida if rascunho else None))

    def rascunho_por_aviso(self, nutri_id: str, aviso_wa_id: str) -> dict | None:
        r = self._um(SQL_RASCUNHO + "and r.aviso_wa_id = %s", (HORAS_RASCUNHO, nutri_id, aviso_wa_id))
        return dict(r) if r else None

    def rascunhos_pendentes(self, nutri_id: str) -> list[dict]:
        """Avisos ainda sem decisão nas últimas 48 h, do mais novo para o mais antigo."""
        linhas = self._db.executar(
            SQL_RASCUNHO + "and r.status = 'pendente' and r.criado_em >= now() - make_interval(hours => %s) "
            "order by r.criado_em desc", (HORAS_RASCUNHO, nutri_id, HORAS_RASCUNHO), retorno="todos", como_dict=True)
        return [dict(l) for l in linhas]

    def guardar_versao_nutri(self, rascunho_id: str, texto: str) -> None:
        """Texto da nutri sem citar o aviso: vira a resposta pendente, à espera do "sim" dela."""
        self._db.executar("update public.rascunhos set resposta = %s, versao_nutri = true "
                          "where id = %s and status = 'pendente'", (texto, rascunho_id))

    def decidir_rascunho(self, rascunho_id: str, status: str, texto_enviado: str | None) -> bool:
        """Marca a decisão só se ainda estava pendente: evento repetido não envia duas vezes."""
        r = self._db.executar(
            "update public.rascunhos set status = %s, texto_enviado = %s, decidido_em = now() "
            "where id = %s and status = 'pendente' returning id", (status, texto_enviado, rascunho_id), retorno="um")
        return r is not None
