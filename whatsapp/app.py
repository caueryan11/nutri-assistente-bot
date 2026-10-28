"""Webhook do WhatsApp (uazapi).

    MODO=eco  responde "Eco: <texto>" (testa a conexão; não usa banco nem IA)
    MODO=bot  paciente cadastrado + consentimento -> bot -> resposta; encaminhamento avisa a nutri
              paciente sem consentimento -> pede o aceite do termo (sem IA); "Aceito" grava e libera
              número desconhecido -> atendimento comercial (bot/comercial.py): triagem, convênio e primeira
              consulta; ao marcar, vira paciente e recebe o termo
              + rotinas (bot/rotinas.py) e relatório semanal (bot/metricas.py)
    Painel web da nutri: /painel/<token> (link no relatório semanal ou mandando "painel" do telefone de aviso): lembrete 24 h, cobrança de retorno, resumo das 8h (ROTINAS=off desliga)

Rodar:
    uvicorn whatsapp.app:criar_app_do_ambiente --factory --port 8000 --no-access-log

URL para cadastrar na uazapi:  https://<seu-endereço>/webhook/uazapi/<WEBHOOK_SEGREDO>

Responde 200 na hora e processa em segundo plano (recomendação da uazapi).
Logs sem conteúdo de mensagem (dado de saúde).
"""

from __future__ import annotations

import hmac
import logging
import os
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

from bot import avisos
from bot.assistente import MENSAGEM_ENCAMINHAMENTO_PADRAO, Resultado, responder
from bot.comercial import MENSAGEM_NAO_TEXTO, NutriComercial, ResultadoLead, atender_lead
from bot.taco import FonteTaco
from whatsapp import consentimento
from whatsapp.repositorio import Nutri, Paciente, Repositorio, variantes_telefone
from whatsapp.uazapi import ClienteUazapi, MensagemRecebida, extrair_mensagem, token_valido

log = logging.getLogger("whatsapp")


@dataclass
class Config:
    modo: str
    uazapi_url: str
    uazapi_token: str
    webhook_segredo: str
    database_url: str = ""
    google_sa_b64: str = ""
    termo_url: str = ""
    rotinas: bool = False  # liga o agendador (lembrete, retorno, resumo) no MODO=bot
    comercial: bool = True  # número desconhecido é atendido pelo bot comercial (COMERCIAL=off: fica sem resposta)
    painel_segredo: str = ""  # assina os links do painel; vazio = painel desligado
    url_publica: str = ""     # https://<domínio> do app, para montar o link do painel
    retencao_dias: int = 180        # mensagens de pacientes (LGPD; prazo a confirmar com a Ray e o advogado)
    retencao_leads_dias: int = 90   # mensagens de leads e leads que não viraram paciente
    alerta_url: str = ""      # ntfy/Discord/Slack: aviso quando o WhatsApp cai (whatsapp/monitor.py)
    rascunho: bool = False    # sugestão de resposta pesquisada no aviso de encaminhamento (bot/rascunho.py)

    @classmethod
    def do_ambiente(cls) -> "Config":
        from dotenv import load_dotenv

        # override: o .env vence variável antiga do sistema (ex.: ANTHROPIC_API_KEY de outro projeto)
        load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=True)
        return cls(
            modo=os.environ.get("MODO", "eco"),
            uazapi_url=os.environ.get("UAZAPI_URL", ""),
            uazapi_token=os.environ.get("UAZAPI_TOKEN", ""),
            webhook_segredo=os.environ.get("WEBHOOK_SEGREDO", ""),
            database_url=os.environ.get("DATABASE_URL", ""),
            google_sa_b64=os.environ.get("GOOGLE_SERVICE_ACCOUNT_B64", ""),
            termo_url=os.environ.get("TERMO_URL", ""),
            rotinas=os.environ.get("ROTINAS", "on").lower() not in ("off", "0", "false", "nao", "não"),
            comercial=os.environ.get("COMERCIAL", "on").lower() not in ("off", "0", "false", "nao", "não"),
            painel_segredo=os.environ.get("PAINEL_SEGREDO", ""),
            retencao_dias=int(os.environ.get("RETENCAO_DIAS") or 180),
            retencao_leads_dias=int(os.environ.get("RETENCAO_LEADS_DIAS") or 90),
            alerta_url=os.environ.get("ALERTA_URL", ""),
            rascunho=os.environ.get("RASCUNHO", "on").lower() not in ("off", "0", "false", "nao", "não"),
            # o Railway preenche RAILWAY_PUBLIC_DOMAIN com o domínio público do serviço
            url_publica=os.environ.get("URL_PUBLICA")
            or (f"https://{os.environ['RAILWAY_PUBLIC_DOMAIN']}" if os.environ.get("RAILWAY_PUBLIC_DOMAIN") else ""),
        )


class _Vistos:
    """Últimos ids recebidos, para descartar eventos repetidos antes de ir ao banco."""

    def __init__(self, maximo: int = 5000):
        self._ids: OrderedDict[str, None] = OrderedDict()
        self._maximo = maximo

    def novo(self, id_: str) -> bool:
        if id_ in self._ids:
            return False
        self._ids[id_] = None
        if len(self._ids) > self._maximo:
            self._ids.popitem(last=False)
        return True


def aviso_agenda(nome: str, telefone: str, ag: dict) -> str:
    return avisos.agenda(nome, ag)


APROVA = {"sim", "s", "pode", "pode sim", "ok", "okay", "enviar", "envia", "pode enviar", "manda", "pode mandar", "1", "👍"}
DESCARTA = {"nao", "não", "n", "descartar", "descarta", "ignorar", "ignora", "2"}
MIN_VERSAO = 12  # texto mais curto que isso (sem citar o aviso) não é tratado como resposta ao paciente


def _normalizar(texto: str) -> str:
    return " ".join((texto or "").lower().strip(" \t\n!.?,;:").split())


def _historico_para_api(linhas: list[tuple[str, str]]) -> list[dict]:
    """(direcao, texto) -> mensagens da API: começa em 'user' e alterna papéis."""
    msgs: list[dict] = []
    for direcao, texto in linhas:
        papel = "user" if direcao == "entrada" else "assistant"
        if not msgs and papel == "assistant":
            continue
        if msgs and msgs[-1]["role"] == papel:
            msgs[-1]["content"] += "\n" + texto
        else:
            msgs.append({"role": papel, "content": texto})
    if msgs and msgs[-1]["role"] == "user":  # ficou sem resposta; a pergunta atual entra como 'user' logo depois
        msgs.pop()
    return msgs


def criar_app(
    config: Config,
    *,
    cliente=None,
    repo: Repositorio | None = None,
    taco: FonteTaco | None = None,
    responder_fn: Callable[..., Resultado] = responder,
    atender_lead_fn: Callable[..., ResultadoLead] = atender_lead,
    calendario=None,
    metricas=None,
    area=None,
    importar_fn=None,
    checkin_fn=None,
    reativacao=None,
    rascunho_fn=None,
) -> FastAPI:
    if config.modo not in ("eco", "bot"):
        raise ValueError("MODO precisa ser 'eco' ou 'bot'")
    if len(config.webhook_segredo) < 16:
        raise ValueError("WEBHOOK_SEGREDO precisa ter 16+ caracteres (vai no caminho da URL)")
    if not config.uazapi_token:
        raise ValueError("UAZAPI_TOKEN não definido")

    cliente = cliente or ClienteUazapi(config.uazapi_url, config.uazapi_token)
    if config.modo == "bot" and repo is None:
        if not config.database_url:
            raise ValueError("MODO=bot precisa de DATABASE_URL")
        from bot.taco import TacoPostgres
        from whatsapp.repositorio import RepositorioPostgres

        repo = RepositorioPostgres(config.database_url)
        taco = taco or TacoPostgres(config.database_url)

    if calendario is None and config.google_sa_b64:
        from agenda.google_calendar import GoogleCalendar

        try:
            calendario = GoogleCalendar(config.google_sa_b64)
        except Exception as e:  # credencial colada errada não pode derrubar o bot
            log.error("GOOGLE_SERVICE_ACCOUNT_B64 inválida (%s); bot segue sem agenda", type(e).__name__)

    if config.painel_segredo and len(config.painel_segredo) < 32:
        raise ValueError("PAINEL_SEGREDO precisa ter 32+ caracteres (assina os links do painel)")
    db_rotinas = None
    if config.modo == "bot" and config.database_url:
        from bot.banco import Conexao

        db_rotinas = Conexao(config.database_url)  # rotinas + métricas (fora da conexão do webhook)
    if metricas is None and db_rotinas is not None:
        from bot.metricas import RepositorioMetricasPostgres

        metricas = RepositorioMetricasPostgres(db_rotinas)
    if reativacao is None and db_rotinas is not None:
        from bot.reativacao import RepositorioReativacaoPostgres

        reativacao = RepositorioReativacaoPostgres(db_rotinas)
    if area is None and db_rotinas is not None:
        from whatsapp.area_repo import RepositorioAreaPostgres

        area = RepositorioAreaPostgres(db_rotinas)

    def link_do_painel(nutri_id: str) -> str | None:
        from bot.metricas import link_painel

        return link_painel(config.url_publica, nutri_id, config.painel_segredo)

    # link do termo: TERMO_URL (página própria) ou a página /termo deste app
    url_termo = config.termo_url or (f"{config.url_publica.rstrip('/')}/termo" if config.url_publica
                                     else consentimento.TERMO_URL_PROVISORIO)

    if config.retencao_dias < 30 or config.retencao_leads_dias < 7:
        raise ValueError("RETENCAO_DIAS precisa ser >= 30 e RETENCAO_LEADS_DIAS >= 7 (evita apagar por engano)")
    monitor = None
    if config.modo == "bot" and hasattr(cliente, "status"):
        from whatsapp.monitor import MonitorWhatsApp, enviar_alerta

        monitor = MonitorWhatsApp(cliente.status, lambda texto: enviar_alerta(config.alerta_url, texto))

    from whatsapp.importacao import Pendentes, ler_legenda

    pendentes = Pendentes()
    if importar_fn is None:
        from bot.importar_plano import importar as importar_fn
    importar_plano = importar_fn

    if checkin_fn is None:
        from bot.checkin import interpretar as checkin_fn
    interpretar_checkin = checkin_fn

    if rascunho_fn is None and config.rascunho and config.modo == "bot":
        from bot.rascunho import gerar as rascunho_fn
    gerar_rascunho = rascunho_fn  # None = avisos sem sugestão e sem aprovação por "sim"

    vistos = _Vistos()
    agendador = None
    if config.rotinas and config.modo == "bot" and db_rotinas is not None:
        from bot.rotinas import RepositorioRotinasPostgres, criar_agendador

        agendador = criar_agendador(RepositorioRotinasPostgres(db_rotinas),
                                    cliente.enviar_texto, calendario,
                                    metricas=metricas, link_painel=link_do_painel,
                                    retencao=(config.retencao_dias, config.retencao_leads_dias), monitor=monitor,
                                    reativacao=reativacao)

    @asynccontextmanager
    async def ciclo_de_vida(_app):
        if agendador is not None:
            agendador.start()
            log.info("rotinas ligadas: %s", ", ".join(j.name for j in agendador.get_jobs()))
        yield
        if agendador is not None:
            agendador.shutdown(wait=False)

    app = FastAPI(title="nutri-assistente webhook", docs_url=None, redoc_url=None, lifespan=ciclo_de_vida)

    if metricas is not None and config.painel_segredo:
        from whatsapp.painel import criar_rotas

        app.include_router(criar_rotas(metricas, config.painel_segredo, area, importar_fn=importar_plano,
                                       reativacao=reativacao))

    @app.get("/termo", response_class=HTMLResponse, include_in_schema=False)
    def termo():
        from jinja2 import Environment, FileSystemLoader, select_autoescape

        env = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"), autoescape=select_autoescape(["html"]))
        html = env.get_template("termo.html").render(versao=consentimento.VERSAO_TERMO, retencao_dias=config.retencao_dias)
        return HTMLResponse(html, headers={"Cache-Control": "public, max-age=300", "X-Content-Type-Options": "nosniff"})

    @app.get("/saude")
    def saude():
        # RAILWAY_GIT_COMMIT_SHA: o Railway preenche com o commit publicado
        return {"ok": True, "modo": config.modo, "versao": os.environ.get("RAILWAY_GIT_COMMIT_SHA", "local")[:7],
                "agenda": calendario is not None,
                "painel": bool(metricas is not None and config.painel_segredo and config.url_publica),
                "whatsapp": monitor.resumo() if monitor is not None else None,
                "alerta": bool(config.alerta_url),
                "rotinas": [{"nome": j.name, "proxima": j.next_run_time.isoformat() if j.next_run_time else None}
                            for j in agendador.get_jobs()] if agendador is not None else []}

    @app.post("/webhook/uazapi/{segredo}")
    async def webhook(segredo: str, request: Request, tarefas: BackgroundTasks):
        if not hmac.compare_digest(segredo.encode(), config.webhook_segredo.encode()):
            raise HTTPException(status_code=404)
        try:
            payload = await request.json()
        except ValueError:
            raise HTTPException(status_code=400, detail="json inválido")
        if not isinstance(payload, dict) or not token_valido(payload, config.uazapi_token):
            log.warning("webhook com token inválido recusado")
            raise HTTPException(status_code=401)

        if payload.get("EventType") == "connection":
            # conexão mudou: confere o status na uazapi agora (o alerta sai em segundos, não em até 10 min)
            if monitor is not None:
                tarefas.add_task(monitor.conferir)
            return {"ok": True, "conexao": True}
        msg = extrair_mensagem(payload)
        if msg is None:
            return {"ok": True, "ignorado": True}
        if not vistos.novo(msg.id):
            return {"ok": True, "repetido": True}
        tarefas.add_task(processar, msg)
        return {"ok": True}

    def enviar(numero: str, texto: str) -> str | None:
        return cliente.enviar_texto(numero, texto)

    def processar(msg: MensagemRecebida) -> None:
        try:
            if config.modo == "eco":
                resposta = f"Eco: {msg.texto}" if msg.tipo == "texto" else f"Eco: recebi uma mensagem do tipo {msg.tipo}."
                enviar(msg.telefone, resposta)
                log.info("eco enviado (msg=%s)", msg.id)
            else:
                processar_bot(msg)
        except Exception:
            log.exception("falha ao processar mensagem %s", msg.id)

    def processar_bot(msg: MensagemRecebida) -> None:
        nutri = repo.nutri_por_instancia(msg.instancia)
        if nutri is None:
            log.warning("instância sem nutri cadastrada: %s", msg.instancia)
            return
        aviso = eh_telefone_aviso(nutri, msg.telefone)
        if aviso and msg.tipo == "texto" and (msg.texto or "").strip().lower().strip("!.? ") == "painel":
            link = link_do_painel(nutri.id) if metricas is not None else None
            enviar(msg.telefone, f"Seu painel dos últimos 7 e 30 dias (link pessoal, vale 8 dias): {link}" if link
                   else "O painel ainda não está ativado. Fale com o suporte do Nutri Assistente.")
            log.info("link do painel enviado para a nutri %s", nutri.id)
            return
        if aviso and msg.tipo == "texto" and gerar_rascunho is not None and responder_aviso(nutri, msg):
            return
        if aviso and area is not None and msg.tipo in ("documento", "imagem"):
            receber_plano_da_nutri(nutri, msg)
            return
        if aviso and area is not None and msg.tipo == "texto":
            nome, telefone = ler_legenda(msg.texto)
            if telefone and pendentes.tem_arquivo(nutri.id):
                arquivo = pendentes.pegar_arquivo(nutri.id)
                if arquivo:
                    importar_plano_para(nutri, msg.telefone, *arquivo, nome, telefone)
                    return
            elif telefone:
                pendentes.guardar_paciente(nutri.id, nome, telefone)  # o arquivo pode vir logo depois
        paciente = repo.paciente(nutri.id, msg.telefone)
        if paciente is None:
            if aviso:
                log.info("mensagem do próprio número de aviso da nutri (msg=%s); sem resposta", msg.id)
                return
            if not config.comercial:
                log.info("número não cadastrado como paciente e COMERCIAL=off (msg=%s); sem resposta", msg.id)
                return
            processar_lead(nutri, msg)
            return
        if reativacao is not None:
            reativacao.marcar_resposta(paciente.id)  # só o horário (métrica da campanha); nada do conteúdo
        if msg.tipo == "texto" and consentimento.eh_revogacao(msg.texto):
            tratar_revogacao(nutri, paciente, msg)
            return
        if not paciente.consentimento_em:
            tratar_sem_consentimento(nutri, paciente, msg)
            return
        if not repo.registrar_entrada(nutri, paciente, msg):
            return  # já processada

        texto = msg.texto
        pendente = getattr(repo, "checkin_pendente", lambda _id: None)(paciente.id) if msg.tipo == "texto" else None
        if pendente is not None:
            tratado, texto_para_bot = tratar_checkin(nutri, paciente, msg, pendente)
            if tratado and not texto_para_bot:
                return
            texto = texto_para_bot or texto

        plano, so_agenda = None, None
        if msg.tipo != "texto":
            r = encaminhamento(nutri, paciente, f"mensagem do tipo {msg.tipo}", avisos.resumo_midia(msg.tipo))
        else:
            plano = repo.plano_ativo(paciente.id)
            # só agenda: consulta simples (nutri com dois níveis) ou paciente ainda sem plano (ex.: reativação)
            so_agenda = ("sem_acompanhamento" if nutri.modelo_acompanhamento == "por_paciente"
                         and not paciente.acompanhamento_ativo else "sem_plano" if plano is None else None)
            agenda = montar_agenda(nutri, paciente, msg.telefone)
            if so_agenda and agenda is None:
                r = encaminhamento(nutri, paciente, "paciente sem plano ativo" if so_agenda == "sem_plano"
                                   else "dúvida de paciente sem acompanhamento",
                                   f"Mandou mensagem, mas {'ainda não tem plano ativo' if so_agenda == 'sem_plano' else 'não está no acompanhamento'}: \"{texto}\"")
            else:
                r = responder_fn(
                    texto,
                    {"primeiro_nome": paciente.primeiro_nome, "consentimento_em": paciente.consentimento_em,
                     "anamnese": paciente.anamnese},
                    plano or {"refeicoes": []},
                    taco=taco,
                    nome_nutri=nutri.nome,
                    historico=_historico_para_api(repo.historico(paciente.id, msg.id)),
                    agenda=agenda,
                    **({"somente_agenda": so_agenda} if so_agenda else {}),
                )

        urgente = r.acao == "encaminhar" and getattr(r, "urgente", False)
        if urgente and not r.motivo.startswith("urgente"):
            r.motivo = f"urgente: {r.motivo}"  # o resumo das 8h põe os urgentes primeiro
        wa_id = enviar(msg.telefone, r.mensagem_paciente)
        repo.registrar_saida(nutri, paciente, r, wa_id)
        log.info("bot: paciente=%s acao=%s passos=%d urgente=%s", paciente.id, r.acao, r.passos, urgente)

        if r.agendamento and nutri.telefone_aviso:
            enviar(nutri.telefone_aviso, aviso_agenda(paciente.nome, msg.telefone, r.agendamento))

        if r.acao == "encaminhar" and nutri.telefone_aviso:
            avisar_nutri(nutri, paciente, msg, texto, r, urgente, plano,
                         com_rascunho=msg.tipo == "texto" and bool(texto) and so_agenda != "sem_acompanhamento")

    def avisar_nutri(nutri: Nutri, paciente: Paciente, msg: MensagemRecebida, texto: str, r: Resultado,
                     urgente: bool, plano: dict | None, com_rascunho: bool) -> None:
        """Aviso de encaminhamento; com o rascunho ligado, pesquisa e manda uma sugestão de resposta junto
        (leva ~20 s; o paciente já recebeu o "passei para a nutricionista")."""
        resumo = r.resumo_para_nutri or r.motivo
        rascunho = None
        if gerar_rascunho is not None and com_rascunho:
            try:
                rascunho = gerar_rascunho(texto, resumo, paciente.primeiro_nome, plano, paciente.anamnese, nutri.nome,
                                          historico=_historico_para_api(repo.historico(paciente.id, msg.id)))
            except Exception as e:  # sem sugestão o aviso sai igual
                log.warning("rascunho falhou: %s", type(e).__name__)
        aviso_id = enviar(nutri.telefone_aviso, avisos.paciente_precisa(paciente.nome, msg.telefone, resumo, urgente,
                                                                         rascunho))
        if gerar_rascunho is not None and msg.tipo == "texto":
            try:  # uma linha por aviso, com ou sem sugestão: citar o aviso liga a resposta da nutri ao paciente
                repo.criar_rascunho(nutri.id, paciente.id, texto, rascunho, aviso_id)
            except Exception as e:
                log.warning("rascunho não gravado: %s", type(e).__name__)

    def responder_aviso(nutri: Nutri, msg: MensagemRecebida) -> bool:
        """A nutri respondeu um aviso: "sim" envia a sugestão, "não" descarta, outro texto vai como a versão dela.
        False = não era sobre um aviso (segue o fluxo normal do número de aviso)."""
        texto = (msg.texto or "").strip()
        n = _normalizar(texto)
        alvo = repo.rascunho_por_aviso(nutri.id, msg.citada) if msg.citada else None
        if alvo is None:
            pendentes = repo.rascunhos_pendentes(nutri.id)
            if not pendentes:
                return False
            # "sim"/"não" sem citar: primeiro a versão que ela acabou de escrever, depois a sugestão mais nova
            confirmando = next((p for p in pendentes if p.get("versao_nutri")), None)
            if n in APROVA:
                alvo = confirmando or next((p for p in pendentes if p["resposta"]), None)
                if alvo is None:
                    enviar(msg.telefone, avisos.rascunho_sem_texto(pendentes[0]["nome"], pendentes[0]["telefone"]))
                    return True
            elif n in DESCARTA:
                alvo = confirmando or pendentes[0]
            else:
                if ler_legenda(texto)[1]:
                    return False  # nome + telefone: é a legenda de um plano para importar
                if len(pendentes) > 1:
                    enviar(msg.telefone, avisos.rascunho_qual([p["nome"] for p in pendentes[:4]]))
                    return True
                alvo = pendentes[0]
                if len(texto) < MIN_VERSAO:  # "obrigada", "blz"...: curto demais para ser a resposta ao paciente
                    enviar(msg.telefone, f"Não entendi. Para {alvo['nome']}: responda *sim*, *não* ou escreva a "
                                         "resposta completa.")
                    return True
                if alvo["status"] == "pendente" and not alvo["expirado"]:
                    # sem citar o aviso: não envia direto; guarda a versão dela e pede o "sim"
                    repo.guardar_versao_nutri(alvo["id"], texto)
                    enviar(msg.telefone, avisos.confirmar_versao(alvo["nome"], texto))
                    return True
        if alvo["status"] != "pendente":
            enviar(msg.telefone, avisos.rascunho_ja_resolvido(alvo["nome"]))
            return True
        if alvo["expirado"]:
            enviar(msg.telefone, avisos.rascunho_expirado(alvo["nome"], alvo["telefone"]))
            return True
        if n in DESCARTA:
            if repo.decidir_rascunho(alvo["id"], "descartado", None):
                enviar(msg.telefone, avisos.rascunho_descartado(alvo["nome"], alvo["telefone"]))
            return True
        if n in APROVA:
            if not alvo["resposta"]:
                enviar(msg.telefone, avisos.rascunho_sem_texto(alvo["nome"], alvo["telefone"]))
                return True
            final, status = alvo["resposta"], "editado" if alvo.get("versao_nutri") else "enviado"
        else:
            final, status = texto, "editado"
        if not alvo["consentimento_em"]:  # LGPD: mandou PARAR depois do aviso
            enviar(msg.telefone, avisos.rascunho_sem_consentimento(alvo["nome"]))
            return True
        if not repo.decidir_rascunho(alvo["id"], status, final):
            return True  # evento repetido: já foi enviado
        mensagem = avisos.mensagem_da_nutri(nutri.nome, final)
        try:
            wa_id = enviar(alvo["telefone"], mensagem)
        except Exception as e:
            log.warning("resposta da nutri não enviada: %s", type(e).__name__)
            enviar(msg.telefone, f"⚠️ Não consegui enviar para {alvo['nome']} agora. Responda direto: "
                                 f"{avisos.link_whatsapp(alvo['telefone'])}")
            return True
        destino = Paciente(id=alvo["paciente_id"], nome=alvo["nome"], primeiro_nome=alvo["primeiro_nome"],
                           telefone=alvo["telefone"], consentimento_em=alvo["consentimento_em"])
        repo.registrar_saida(nutri, destino, Resultado(
            acao=None, mensagem_paciente=mensagem,
            motivo="resposta da nutri: " + ("sugestão aprovada" if status == "enviado" else "versão dela")), wa_id)
        enviar(msg.telefone, avisos.rascunho_enviado(alvo["nome"], editado=status == "editado"))
        log.info("rascunho %s: %s", alvo["id"], status)
        return True

    def tratar_checkin(nutri: Nutri, paciente: Paciente, msg: MensagemRecebida, pendente: dict) -> tuple[bool, str | None]:
        """(tratado, texto_para_bot). Não é resposta do check-in: (False, None) e o bot normal responde."""
        from bot.checkin import decidir

        try:
            leitura = interpretar_checkin(msg.texto)
        except Exception as e:  # IA fora do ar: deixa o bot normal tentar (ele encaminha se precisar)
            log.warning("check-in: leitura falhou (%s)", type(e).__name__)
            return False, None
        if not leitura.eh_resposta:
            return False, None
        d = decidir(leitura, pendente, repo.peso_anterior(paciente.id, pendente["semana"]), paciente.primeiro_nome,
                    nutri.nome, msg.telefone, paciente.nome)
        repo.salvar_checkin(pendente["id"], d.salvar)
        wa_id = enviar(msg.telefone, d.mensagem)
        repo.registrar_saida(nutri, paciente, Resultado(
            acao="encaminhar" if d.aviso_nutri else None, mensagem_paciente=d.mensagem,
            motivo="check-in semanal" + (": alerta" if d.aviso_nutri else ""),
            resumo_para_nutri=d.aviso_nutri or ""), wa_id)
        if d.aviso_nutri and nutri.telefone_aviso:
            enviar(nutri.telefone_aviso, d.aviso_nutri)
        log.info("check-in: paciente=%s status=%s alerta=%s", paciente.id, d.salvar.get("status"), bool(d.aviso_nutri))
        return True, d.seguir_para_bot

    def tratar_sem_consentimento(nutri: Nutri, paciente: Paciente, msg: MensagemRecebida) -> None:
        """LGPD: sem aceite, nada de IA e nada de gravar a mensagem (pode ter dado de saúde).
        O "Aceito" e a confirmação são gravados como prova do aceite."""
        veio_da_campanha = reativacao is not None and reativacao.campanha_recente(paciente.id)
        # o aceite só vale se o pedido do termo já foi mostrado ("SIM" à campanha não é aceite de termo não visto)
        # (quem revogou já conhecia o termo e foi convidado a responder "Aceito" para voltar)
        ja_viu_o_termo = bool(paciente.termo_pedido_em) or paciente.status_consentimento == "revogado"
        if msg.tipo == "texto" and consentimento.eh_aceite(msg.texto) and ja_viu_o_termo:
            if not repo.registrar_consentimento(paciente.id, consentimento.VERSAO_TERMO):
                return  # outro evento já registrou o aceite
            repo.registrar_entrada(nutri, paciente, msg)
            confirmacao = consentimento.CONFIRMACAO_REATIVACAO if veio_da_campanha else consentimento.CONFIRMACAO
            r = Resultado(acao="responder", mensagem_paciente=confirmacao,
                          motivo=f"consentimento aceito (termo {consentimento.VERSAO_TERMO})")
            wa_id = enviar(msg.telefone, r.mensagem_paciente)
            repo.registrar_saida(nutri, paciente, r, wa_id)
            log.info("paciente %s aceitou o termo %s", paciente.id, consentimento.VERSAO_TERMO)
        elif paciente.status_consentimento == "revogado":
            # revogou: não insistimos com o pedido (só o "Aceito" reativa); a mensagem fica só no WhatsApp da nutri
            log.info("paciente %s com consentimento revogado: sem resposta (msg=%s)", paciente.id, msg.id)
        else:
            enviar(msg.telefone, consentimento.pedido(url_termo, reativacao=veio_da_campanha))
            getattr(repo, "marcar_pedido_termo", lambda _id: None)(paciente.id)
            log.info("paciente %s sem consentimento: pedido de aceite enviado (msg=%s)", paciente.id, msg.id)

    def tratar_revogacao(nutri: Nutri, paciente: Paciente, msg: MensagemRecebida) -> None:
        """LGPD: PARAR revoga o consentimento. Nada da mensagem é gravado; a nutri é avisada."""
        if not repo.registrar_revogacao(paciente.id):
            return  # já revogado (evento repetido ou PARAR de novo)
        enviar(msg.telefone, consentimento.CONFIRMACAO_REVOGACAO.format(nutri=nutri.nome))
        if nutri.telefone_aviso:
            enviar(nutri.telefone_aviso, avisos.revogacao(paciente.nome))
        log.info("paciente %s revogou o consentimento", paciente.id)

    def receber_plano_da_nutri(nutri: Nutri, msg: MensagemRecebida) -> None:
        """Plano (PDF/foto) mandado do WhatsApp de avisos: importa como rascunho para o paciente da legenda."""
        try:
            dados, mimetype = cliente.baixar_midia(msg.id)
        except Exception as e:
            log.warning("não baixou o arquivo da nutri: %s", type(e).__name__)
            enviar(msg.telefone, "Não consegui baixar o arquivo. Pode mandar de novo?")
            return
        nome, telefone = ler_legenda(msg.texto)
        if not telefone:
            anterior = pendentes.pegar_paciente(nutri.id)
            if anterior:
                nome, telefone = anterior
        if not telefone:
            pendentes.guardar_arquivo(nutri.id, dados, mimetype)
            enviar(msg.telefone, "Recebi o arquivo do plano. De qual paciente é? Me mande o nome e o WhatsApp "
                                 "(ex.: Maria Silva 51 99999-0000).")
            return
        importar_plano_para(nutri, msg.telefone, dados, mimetype, nome, telefone)

    def importar_plano_para(nutri: Nutri, numero_nutri: str, dados: bytes, mimetype: str,
                            nome: str | None, telefone: str) -> None:
        from bot.importar_plano import ErroImportacao, mesclar_anamnese
        from whatsapp.importacao import resumo_para_nutri

        enviar(numero_nutri, "Lendo o plano, leva até 1 minuto...")
        try:
            imp = importar_plano(dados, mimetype)
        except ErroImportacao as e:
            enviar(numero_nutri, f"Não consegui importar o plano: {e}.")
            return
        avisos = list(imp.avisos)
        if imp.itens_sem_gramas:
            avisos.append(f"{len(imp.itens_sem_gramas)} item(ns) sem gramas: o assistente não calcula calorias deles")
        existente = area.paciente_por_telefone(nutri.id, telefone)
        if existente:
            pid, nome_final, criado = existente["id"], existente["nome"], False
            if imp.paciente_nome and imp.paciente_nome.split()[0].lower() != nome_final.split()[0].lower():
                avisos.insert(0, f"o nome no documento ({imp.paciente_nome}) é diferente do cadastro ({nome_final}): "
                                 "confira se é o paciente certo")
        else:
            nome_final = nome or imp.paciente_nome or f"Paciente final {telefone[-4:]}"
            if not (nome or imp.paciente_nome):
                avisos.append("não achei o nome do paciente: corrija no painel")
            pid, criado = area.criar_paciente(nutri.id, nome_final, telefone), True
        if imp.anamnese:
            atual = (area.paciente(nutri.id, pid) or {}).get("anamnese")
            area.salvar_anamnese(nutri.id, pid, mesclar_anamnese(atual, imp.anamnese))
            avisos.append("o documento cita restrições do paciente: foram somadas à anamnese")
        area.salvar_rascunho(nutri.id, pid, imp.plano, "whatsapp", avisos)
        link = link_do_painel(nutri.id)
        enviar(numero_nutri, resumo_para_nutri(nome_final, imp, avisos, f"{link}/pacientes/{pid}" if link else None, criado))
        log.info("plano importado pelo WhatsApp: nutri=%s paciente=%s criado=%s", nutri.id, pid, criado)

    def eh_telefone_aviso(nutri: Nutri, telefone: str) -> bool:
        return bool(nutri.telefone_aviso) and bool(set(variantes_telefone(telefone)) & set(variantes_telefone(nutri.telefone_aviso)))

    def processar_lead(nutri: Nutri, msg: MensagemRecebida) -> None:
        """Quem não é paciente: atendimento comercial. Guarda só o necessário para a conversa seguir
        (lead_mensagens, com retenção); o bot não pede dados de saúde."""
        lead = repo.lead(nutri.id, msg.telefone)
        if lead["status"] == "convertido":
            return  # já virou paciente (inativo ou em outro número): a nutri cuida
        if msg.tipo == "texto" and consentimento.eh_revogacao(msg.texto):
            if lead["status"] != "desistiu":
                repo.atualizar_lead(lead["id"], {"status": "desistiu"})
                enviar(msg.telefone, consentimento.CONFIRMACAO_SAIDA_LEAD)
            log.info("lead %s pediu para parar", lead["id"])
            return
        if lead["status"] == "desistiu":
            repo.atualizar_lead(lead["id"], {"status": "em_triagem"})  # voltou a escrever por conta própria
        if not repo.registrar_lead_mensagem(lead["id"], "entrada", msg.id, msg.texto or None):
            return  # já processada

        if msg.tipo != "texto":
            r = ResultadoLead(acao="responder", mensagem=MENSAGEM_NAO_TEXTO, fallback=True)
        else:
            dados = NutriComercial(nutri.nome, nutri.atende_convenio, list(nutri.convenios_aceitos or []),
                                   nutri.valor_consulta, nutri.politica_reembolso, nutri.apresentacao_servico)
            r = atender_lead_fn(
                msg.texto, lead, dados,
                historico=_historico_para_api(repo.historico_lead(lead["id"], msg.id)),
                agenda_para=agenda_do_lead(nutri, lead, msg.telefone),
                atualizar=lambda campos: repo.atualizar_lead(lead["id"], campos),
            )

        wa_id = enviar(msg.telefone, r.mensagem)
        repo.registrar_lead_mensagem(lead["id"], "saida", wa_id, r.mensagem)
        log.info("lead: %s acao=%s passos=%d convertido=%s", lead["id"], r.acao, r.passos, bool(r.convertido))

        if r.convertido:
            # agora é paciente sem consentimento: o termo vai já, e o fluxo clínico começa no "Aceito"
            enviar(msg.telefone, consentimento.pedido(url_termo))
            if nutri.telefone_aviso:
                enviar(nutri.telefone_aviso, avisos.novo_paciente(
                    r.convertido["nome"], msg.telefone, r.convertido["descricao"], r.convertido.get("convenio"),
                    lead.get("objetivo")))
        elif r.acao == "encaminhar" and nutri.telefone_aviso:
            enviar(nutri.telefone_aviso, avisos.lead_precisa(msg.telefone, lead.get("nome"), r.resumo_para_nutri or r.motivo))

    def agenda_do_lead(nutri: Nutri, lead: dict, telefone: str):
        """agenda_para(nome, convenio) -> AgendaNutri cujo gravar converte o lead em paciente."""
        if calendario is None or not nutri.google_calendar_id:
            return None
        from agenda.servico import AgendaNutri

        def agenda_para(nome: str, convenio: str):
            return AgendaNutri(
                calendario, nutri.google_calendar_id, nutri.agenda_config, nutri.fuso_horario, nome, telefone,
                gravar=lambda ini, fim, evento: repo.converter_lead(lead["id"], nutri.id, nome, telefone,
                                                                     ini, fim, evento, convenio),
                titulo="Primeira consulta",
            )
        return agenda_para

    def montar_agenda(nutri: Nutri, paciente: Paciente, telefone: str):
        """Agenda da nutri para o bot, ou None se ela não conectou o Google Agenda."""
        if calendario is None or not nutri.google_calendar_id:
            return None
        from agenda.servico import AgendaNutri

        return AgendaNutri(
            calendario, nutri.google_calendar_id, nutri.agenda_config, nutri.fuso_horario,
            paciente.nome, telefone,
            gravar=lambda ini, fim, evento: repo.registrar_agendamento(nutri.id, paciente.id, ini, fim, evento),
            listar=lambda: repo.consultas_futuras(paciente.id),
            cancelar_registro=lambda agendamento_id: repo.cancelar_agendamento(agendamento_id, paciente.id),
        )

    def encaminhamento(nutri: Nutri, paciente: Paciente, motivo: str, resumo: str) -> Resultado:
        return Resultado(
            acao="encaminhar",
            mensagem_paciente=MENSAGEM_ENCAMINHAMENTO_PADRAO.format(
                primeiro_nome=paciente.primeiro_nome, nome_nutri=nutri.nome),
            motivo=motivo,
            resumo_para_nutri=resumo,
            fallback=True,
        )

    return app


def criar_app_do_ambiente() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return criar_app(Config.do_ambiente())
