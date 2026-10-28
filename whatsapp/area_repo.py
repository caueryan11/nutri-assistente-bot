"""Banco da área da nutri (cadastros). TODA consulta filtra pela nutri do link: uma nutri nunca
lê nem altera paciente de outra, mesmo trocando o id na URL."""

from __future__ import annotations

import json
from typing import Protocol


class TelefoneDuplicado(Exception):
    pass


class RepositorioArea(Protocol):
    def pacientes(self, nutri_id: str) -> list[dict]: ...
    def paciente(self, nutri_id: str, paciente_id: str) -> dict | None: ...
    def criar_paciente(self, nutri_id: str, nome: str, telefone: str) -> str: ...
    def atualizar_paciente(self, nutri_id: str, paciente_id: str, nome: str, telefone: str, ativo: bool) -> bool: ...
    def salvar_anamnese(self, nutri_id: str, paciente_id: str, anamnese: dict) -> bool: ...
    def salvar_plano(self, nutri_id: str, paciente_id: str, conteudo: dict) -> bool: ...
    def salvar_rascunho(self, nutri_id: str, paciente_id: str, conteudo: dict, origem: str, avisos: list[str]) -> bool: ...
    def descartar_rascunho(self, nutri_id: str, paciente_id: str) -> bool: ...
    def paciente_por_telefone(self, nutri_id: str, telefone: str) -> dict | None: ...
    def definir_acompanhamento(self, nutri_id: str, paciente_id: str, modo, dias: int | None = None) -> bool: ...
    def evolucao(self, nutri_id: str, paciente_id: str, limite: int = 12) -> list[dict]: ...
    def excluir_paciente(self, nutri_id: str, paciente_id: str) -> bool: ...
    def consultorio(self, nutri_id: str) -> dict | None: ...
    def salvar_consultorio(self, nutri_id: str, dados: dict) -> None: ...


CAMPOS_CONSULTORIO = ("telefone_aviso", "atende_convenio", "convenios_aceitos", "valor_consulta",
                      "politica_reembolso", "apresentacao_servico", "agenda_config", "checkin_config",
                      "modelo_acompanhamento", "acompanhamento_dias")
CAMPOS_JSON = ("agenda_config", "checkin_config")


class RepositorioAreaPostgres:
    def __init__(self, db):
        self._db = db

    def _um(self, sql, params):
        r = self._db.executar(sql, params, retorno="um", como_dict=True)
        return dict(r) if r else None

    def _todos(self, sql, params):
        return [dict(r) for r in self._db.executar(sql, params, retorno="todos", como_dict=True)]

    def pacientes(self, nutri_id: str) -> list[dict]:
        return self._todos(
            "select p.id::text as id, p.nome, p.telefone, p.ativo, p.status_consentimento, "
            "  exists (select 1 from public.planos_alimentares pl where pl.paciente_id = p.id and pl.ativo) as tem_plano, "
            "  exists (select 1 from public.planos_alimentares pl where pl.paciente_id = p.id and pl.rascunho) as tem_rascunho, "
            "  p.anamnese <> '{}'::jsonb as tem_anamnese, "
            "  p.acompanhamento_desde, p.acompanhamento_ate, "
            "  (select c.adesao from public.checkins c where c.paciente_id = p.id and c.status = 'respondido' "
            "     order by c.semana desc limit 1) as ultima_adesao, "
            "  (select min(a.inicio) from public.agendamentos a where a.paciente_id = p.id "
            "     and a.status = 'confirmado' and a.inicio > now()) as proxima_consulta "
            "from public.pacientes p where p.nutricionista_id = %s order by p.ativo desc, p.nome",
            (nutri_id,))

    def paciente(self, nutri_id: str, paciente_id: str) -> dict | None:
        return self._um(
            "select p.id::text as id, p.nome, p.telefone, p.ativo, p.status_consentimento, p.consentimento_em, "
            "  p.anamnese, p.acompanhamento_desde, p.acompanhamento_ate, pl.conteudo as plano, pl.vigente_desde, "
            "  r.conteudo as rascunho, r.avisos as rascunho_avisos, r.origem as rascunho_origem, r.criado_em as rascunho_em "
            "from public.pacientes p "
            "left join public.planos_alimentares pl on pl.paciente_id = p.id and pl.ativo "
            "left join public.planos_alimentares r on r.paciente_id = p.id and r.rascunho "
            "where p.nutricionista_id = %s and p.id = %s",
            (nutri_id, paciente_id))

    def criar_paciente(self, nutri_id: str, nome: str, telefone: str) -> str:
        import psycopg2.errors

        try:
            r = self._um("insert into public.pacientes (nutricionista_id, nome, telefone) values (%s, %s, %s) "
                         "returning id::text as id", (nutri_id, nome, telefone))
        except psycopg2.errors.UniqueViolation as e:
            raise TelefoneDuplicado() from e
        return r["id"]

    def atualizar_paciente(self, nutri_id: str, paciente_id: str, nome: str, telefone: str, ativo: bool) -> bool:
        import psycopg2.errors

        try:
            r = self._um("update public.pacientes set nome = %s, telefone = %s, ativo = %s "
                         "where nutricionista_id = %s and id = %s returning id",
                         (nome, telefone, ativo, nutri_id, paciente_id))
        except psycopg2.errors.UniqueViolation as e:
            raise TelefoneDuplicado() from e
        return r is not None

    def salvar_anamnese(self, nutri_id: str, paciente_id: str, anamnese: dict) -> bool:
        r = self._um("update public.pacientes set anamnese = %s::jsonb where nutricionista_id = %s and id = %s "
                     "returning id", (json.dumps(anamnese, ensure_ascii=False), nutri_id, paciente_id))
        return r is not None

    def salvar_plano(self, nutri_id: str, paciente_id: str, conteudo: dict) -> bool:
        """Desativa o plano anterior (fica no histórico) e grava o novo como ativo, numa transação (função do banco)."""
        r = self._db.executar("select public.salvar_plano_ativo(%s, %s, %s::jsonb)",
                              (nutri_id, paciente_id, json.dumps(conteudo, ensure_ascii=False)), retorno="um")
        return bool(r and r[0])

    def salvar_rascunho(self, nutri_id: str, paciente_id: str, conteudo: dict, origem: str, avisos: list[str]) -> bool:
        r = self._db.executar("select public.salvar_rascunho_plano(%s, %s, %s::jsonb, %s, %s::jsonb)",
                              (nutri_id, paciente_id, json.dumps(conteudo, ensure_ascii=False), origem,
                               json.dumps(avisos, ensure_ascii=False)), retorno="um")
        return bool(r and r[0])

    def descartar_rascunho(self, nutri_id: str, paciente_id: str) -> bool:
        r = self._um("delete from public.planos_alimentares where nutricionista_id = %s and paciente_id = %s "
                     "and rascunho returning id", (nutri_id, paciente_id))
        return r is not None

    def paciente_por_telefone(self, nutri_id: str, telefone: str) -> dict | None:
        """Inclusive inativo (o telefone é único por nutri)."""
        from whatsapp.repositorio import variantes_telefone

        return self._um("select id::text as id, nome, ativo from public.pacientes "
                        "where nutricionista_id = %s and telefone = any(%s)", (nutri_id, variantes_telefone(telefone)))

    def definir_acompanhamento(self, nutri_id: str, paciente_id: str, modo, dias: int | None = None) -> bool:
        """modo: 'validade' (hoje + dias; renova o ciclo), 'sem_data' (assinatura) ou 'desligar'.
        Aceita True/False (compatível: True = sem data, False = desligar)."""
        modo = {True: "sem_data", False: "desligar"}.get(modo, modo)
        if modo == "desligar":
            sets, params = "acompanhamento_desde = null, acompanhamento_ate = null", ()
        elif modo == "sem_data":
            sets, params = "acompanhamento_desde = coalesce(acompanhamento_desde, current_date), acompanhamento_ate = null", ()
        elif modo == "validade" and dias:
            sets = ("acompanhamento_desde = case when acompanhamento_desde is null or acompanhamento_ate < current_date "
                    "then current_date else acompanhamento_desde end, acompanhamento_ate = current_date + %s, "
                    "aviso_fim_acompanhamento_em = null")
            params = (int(dias),)
        else:
            raise ValueError("modo de acompanhamento inválido")
        r = self._um(f"update public.pacientes set {sets} where nutricionista_id = %s and id = %s returning id",
                     (*params, nutri_id, paciente_id))
        return r is not None

    def evolucao(self, nutri_id: str, paciente_id: str, limite: int = 12) -> list[dict]:
        """Últimos check-ins (mais antigo primeiro), inclusive os sem resposta."""
        linhas = self._todos(
            "select semana, status, adesao, fome, intestino, peso_kg, comentario, alerta, alerta_motivo "
            "from public.checkins where nutricionista_id = %s and paciente_id = %s order by semana desc limit %s",
            (nutri_id, paciente_id, limite))
        return list(reversed(linhas))

    def excluir_paciente(self, nutri_id: str, paciente_id: str) -> bool:
        """Exclusão a pedido do titular (LGPD): apaga o paciente e, em cascata, planos, consultas e mensagens."""
        r = self._um("delete from public.pacientes where nutricionista_id = %s and id = %s returning id",
                     (nutri_id, paciente_id))
        return r is not None

    def consultorio(self, nutri_id: str) -> dict | None:
        return self._um(
            "select nome, telefone_aviso, atende_convenio, convenios_aceitos, valor_consulta, politica_reembolso, "
            "apresentacao_servico, agenda_config, checkin_config, modelo_acompanhamento, acompanhamento_dias, "
            "google_calendar_id "
            "from public.nutricionistas where id = %s",
            (nutri_id,))

    def salvar_consultorio(self, nutri_id: str, dados: dict) -> None:
        campos = {k: v for k, v in dados.items() if k in CAMPOS_CONSULTORIO}
        for k in CAMPOS_JSON:
            if k in campos:
                campos[k] = json.dumps(campos[k])
        sets = ", ".join(f"{k} = %s" + ("::jsonb" if k in CAMPOS_JSON else "") for k in campos)  # nomes fixos
        self._db.executar(f"update public.nutricionistas set {sets} where id = %s", (*campos.values(), nutri_id))
