"""Painel da nutri: /painel/<token> (números) e a área de cadastros (pacientes, anamnese, plano, consultório).

Sem login: o link vem no relatório semanal (ou a nutri manda "painel" do telefone de aviso) e é um
token assinado que expira (bot/metricas.py). Sem cookies, então não há CSRF: quem não tem o link não
posta nada. Toda consulta ao banco filtra pela nutri do token (whatsapp/area_repo.py).
Páginas sem cache, sem indexação e sem referrer, para o link não vazar.
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from agenda.horarios import CONFIG_PADRAO, DIAS
from bot.anamnese import CATEGORIAS, conflitos_no_plano, limpar
from bot.metricas import FUSO, RepositorioMetricas, reais, validar_token
from bot.plano import gramas_do_item
from bot.plano_texto import de_texto, mesclar_extras, para_texto

_env = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"), autoescape=select_autoescape(["html"]))

CABECALHOS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": ("default-src 'none'; script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com "
                                "https://cdn.jsdelivr.net; style-src 'unsafe-inline'; img-src data:; "
                                "connect-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"),
}

LINK_INVALIDO = """<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Link expirado</title></head>
<body style="font-family:system-ui,sans-serif;background:#f8fafc;color:#1e293b;padding:48px 16px;text-align:center">
<h1 style="font-size:20px">Este link expirou ou não é válido</h1>
<p>O próximo relatório semanal traz um link novo. Se precisar agora, mande <b>painel</b> para o assistente
pelo seu número de avisos.</p></body></html>"""

AVISOS = {
    "criado": "Paciente cadastrado. Agora preencha a anamnese e o plano alimentar.",
    "dados": "Dados salvos.",
    "anamnese": "Anamnese salva. O assistente já usa na próxima mensagem.",
    "plano": "Plano salvo e ativo. O anterior ficou no histórico.",
    "excluido": "Paciente e todos os dados dele foram excluídos.",
    "consultorio": "Consultório salvo.",
    "importado": "Plano lido do arquivo. Confira com o documento original e clique em Salvar plano para ativar.",
    "descartado": "Rascunho descartado.",
    "acompanhamento_on": "Acompanhamento ligado: o paciente recebe o check-in no próximo dia configurado.",
    "acompanhamento_renovado": "Acompanhamento renovado.",
    "importados": "Lista importada.",
    "campanha": "Campanha criada: as mensagens saem aos poucos, de segunda a sábado, das 9h às 20h.",
    "campanha_pausada": "Campanha pausada.",
    "campanha_retomada": "Campanha retomada.",
    "acompanhamento_off": "Acompanhamento desligado: o paciente não recebe mais o check-in semanal.",
}
DIAS_SEMANA = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]
STATUS_TERMO = {"aceito": "Aceito", "pendente": "Aguardando aceite", "revogado": "Revogado"}
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_FAIXA = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)\s*-\s*([01]?\d|2[0-3]):([0-5]\d)$")
LIMITE_FORM = 200_000  # bytes


# ---------------------------------------------------------------------
# Validação (funções puras)
# ---------------------------------------------------------------------
def normalizar_telefone(texto: str) -> str | None:
    """'(51) 99999-0000' -> '5551999990000'. None se não parece um celular/telefone com DDD."""
    digitos = re.sub(r"\D", "", texto or "")
    if len(digitos) in (10, 11):
        digitos = "55" + digitos
    return digitos if 12 <= len(digitos) <= 15 else None


def lista(texto: str, maximo: int = 30) -> list[str]:
    itens, vistos = [], set()
    for parte in re.split(r"[\n,;]", texto or ""):
        item = parte.strip()[:60]
        if item and item.lower() not in vistos:
            vistos.add(item.lower())
            itens.append(item)
    return itens[:maximo]


def anamnese_do_form(form: dict) -> dict:
    a = {k: lista(form.get(k, "")) for k in CATEGORIAS}
    obs = (form.get("observacoes") or "").strip()[:1000]
    return {**a, "observacoes": obs} if obs else a


def valor_reais(texto: str) -> tuple[Decimal | None, str | None]:
    t = (texto or "").strip().replace("R$", "").replace(" ", "")
    if not t:
        return None, None
    if "," in t:
        t = t.replace(".", "").replace(",", ".")
    try:
        v = Decimal(t).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None, "valor da consulta inválido (use, por exemplo, 250,00)"
    if v <= 0 or v > 100000:
        return None, "valor da consulta fora do esperado"
    return v, None


def _inteiro(form, campo, minimo, maximo, rotulo, erros):
    try:
        v = int((form.get(campo) or "").strip())
        if minimo <= v <= maximo:
            return v
    except ValueError:
        pass
    erros.append(f"{rotulo} precisa ser um número entre {minimo} e {maximo}")
    return None


def consultorio_do_form(form: dict) -> tuple[dict, list[str]]:
    erros: list[str] = []
    valor, erro = valor_reais(form.get("valor_consulta", ""))
    if erro:
        erros.append(erro)
    aviso_txt = (form.get("telefone_aviso") or "").strip()
    aviso = normalizar_telefone(aviso_txt) if aviso_txt else None
    if aviso_txt and not aviso:
        erros.append("WhatsApp para avisos inválido (coloque com DDD)")
    atendimento = {}
    for d in range(1, 8):
        faixas = [f.strip() for f in (form.get(f"dia_{d}") or "").split(",") if f.strip()]
        boas = []
        for f in faixas:
            m = _FAIXA.match(f)
            if not m or (int(m.group(1)), int(m.group(2))) >= (int(m.group(3)), int(m.group(4))):
                erros.append(f"{DIAS[d - 1]}: \"{f}\" não é um horário válido (ex.: 08:00-12:00)")
                continue
            boas.append(f"{int(m.group(1)):02d}:{m.group(2)}-{int(m.group(3)):02d}:{m.group(4)}")
        if boas:
            atendimento[str(d)] = boas
    agenda = {
        "duracao_min": _inteiro(form, "duracao_min", 15, 240, "Duração", erros),
        "antecedencia_horas": _inteiro(form, "antecedencia_horas", 0, 168, "Antecedência", erros),
        "janela_dias": _inteiro(form, "janela_dias", 1, 60, "Até (dias)", erros),
        "atendimento": atendimento,
    }
    from bot.checkin import validar_config

    checkin, erro = validar_config(form.get("checkin_dia", "7"), form.get("checkin_hora", "18:00"))
    if erro:
        erros.append(erro)
    modelo = form.get("modelo_acompanhamento") or "todos"
    if modelo not in ("todos", "por_paciente"):
        erros.append("modelo de acompanhamento inválido")
    dias_acomp = _inteiro(form, "acompanhamento_dias", 7, 120, "Validade do acompanhamento (dias)", erros) \
        if form.get("acompanhamento_dias") else 45
    dados = {
        "modelo_acompanhamento": modelo,
        "acompanhamento_dias": dias_acomp,
        "checkin_config": checkin,
        "valor_consulta": valor,
        "atende_convenio": form.get("atende_convenio") == "1",
        "convenios_aceitos": lista(form.get("convenios_aceitos", "")),
        "politica_reembolso": (form.get("politica_reembolso") or "").strip()[:600] or None,
        "apresentacao_servico": (form.get("apresentacao_servico") or "").strip()[:1000] or None,
        "telefone_aviso": aviso,
        "agenda_config": agenda,
    }
    return dados, erros


def form_do_consultorio(c: dict) -> dict:
    agenda = {**CONFIG_PADRAO, **(c.get("agenda_config") or {})}
    f = {
        "valor_consulta": reais(float(c["valor_consulta"])) if c.get("valor_consulta") is not None else "",
        "atende_convenio": bool(c.get("atende_convenio")),
        "convenios_aceitos": ", ".join(c.get("convenios_aceitos") or []),
        "politica_reembolso": c.get("politica_reembolso") or "",
        "apresentacao_servico": c.get("apresentacao_servico") or "",
        "telefone_aviso": c.get("telefone_aviso") or "",
        "duracao_min": agenda.get("duracao_min"), "antecedencia_horas": agenda.get("antecedencia_horas"),
        "janela_dias": agenda.get("janela_dias"),
        "checkin_dia": int((c.get("checkin_config") or {}).get("dia", 7)),
        "checkin_hora": (c.get("checkin_config") or {}).get("hora", "18:00"),
        "modelo_acompanhamento": c.get("modelo_acompanhamento") or "todos",
        "acompanhamento_dias": c.get("acompanhamento_dias") or 45,
    }
    for d in range(1, 8):
        f[f"dia_{d}"] = ", ".join(agenda.get("atendimento", {}).get(str(d), []))
    return f


# ---------------------------------------------------------------------
# Páginas
# ---------------------------------------------------------------------
def _html(template: str, status: int = 200, **ctx) -> HTMLResponse:
    return HTMLResponse(_env.get_template(template).render(**ctx), status_code=status, headers=CABECALHOS)


def renderizar(nutri: dict, m, agora: datetime, base: str = "") -> str:
    fuso = ZoneInfo(nutri.get("fuso_horario") or FUSO)
    return _env.get_template("painel.html").render(
        nutri=nutri["nome"], m=m, agora=agora.astimezone(fuso).strftime("%d/%m às %H:%M"), reais=reais,
        data=lambda d: d.astimezone(fuso).strftime("%d/%m %H:%M"), base=base, aba="geral",
        grafico={"duvidas": [m.duvidas_respondidas, m.duvidas_encaminhadas],
                 "consultas": [m.primeiras_consultas, m.retornos_agendados, m.retornos_recuperados]},
    )


async def ler_form(request: Request) -> dict:
    corpo = await request.body()
    if len(corpo) > LIMITE_FORM:
        raise HTTPException(status_code=413, detail="formulário grande demais")
    return {k: v[0] for k, v in parse_qs(corpo.decode("utf-8", "replace"), keep_blank_values=True).items()}


def criar_rotas(repo: RepositorioMetricas, segredo: str, area=None, importar_fn=None, reativacao=None) -> APIRouter:
    """area: whatsapp.area_repo.RepositorioArea (None = só o painel de números).
    importar_fn(bytes, mimetype) -> bot.importar_plano.Importacao (padrão: bot.importar_plano.importar)."""
    if importar_fn is None:
        from bot.importar_plano import importar as importar_fn
    rotas = APIRouter()

    def nutri_do_token(token: str) -> dict | None:
        nutri_id = validar_token(token, segredo)
        return repo.nutri(nutri_id) if nutri_id else None

    def invalido():
        return HTMLResponse(LINK_INVALIDO, status_code=404, headers=CABECALHOS)

    @rotas.get("/painel/{token}", response_class=HTMLResponse, include_in_schema=False)
    def painel(token: str, dias: int = 7):
        nutri = nutri_do_token(token)
        if nutri is None:
            return invalido()
        if dias not in (7, 30):
            raise HTTPException(status_code=400, detail="dias precisa ser 7 ou 30")
        fuso = nutri.get("fuso_horario") or FUSO
        m = repo.metricas(nutri["id"], dias, fuso, detalhes=True)
        base = f"/painel/{token}" if area is not None else ""
        return HTMLResponse(renderizar(nutri, m, datetime.now(ZoneInfo(fuso)), base=base), headers=CABECALHOS)

    if area is None:
        return rotas

    def contexto(token: str, nutri: dict, aba: str, aviso: str | None = None, **extra) -> dict:
        fuso = ZoneInfo(nutri.get("fuso_horario") or FUSO)
        return {"nutri": nutri["nome"], "base": f"/painel/{token}", "aba": aba, "aviso": AVISOS.get(aviso or ""),
                "status_termo": STATUS_TERMO, "data": lambda d: d.astimezone(fuso).strftime("%d/%m %H:%M"), **extra}

    def ir(token: str, caminho: str, aviso: str) -> RedirectResponse:
        return RedirectResponse(f"/painel/{token}{caminho}?aviso={aviso}", status_code=303, headers=CABECALHOS)

    def pagina_paciente(token, nutri, p, *, aviso=None, erros=None, plano_texto=None, previa=None, status=200):
        a = limpar(p.get("anamnese"))
        mostrando_rascunho = plano_texto is None and bool(p.get("rascunho"))
        if mostrando_rascunho:
            plano_texto, previa = para_texto(p["rascunho"]), p["rascunho"]
        if previa is None and p.get("plano"):
            previa = p["plano"]
        evolucao = area.evolucao(nutri["id"], p["id"]) if hasattr(area, "evolucao") else []
        respondidos = [c for c in evolucao if c.get("status") == "respondido"]
        grafico = {"semanas": [c["semana"].strftime("%d/%m") for c in respondidos],
                   "adesao": [c.get("adesao") for c in respondidos],
                   "peso": [float(c["peso_kg"]) if c.get("peso_kg") is not None else None for c in respondidos]}
        dias_acomp = int((area.consultorio(nutri["id"]) or {}).get("acompanhamento_dias") or 45)
        return _html("paciente.html", status, **contexto(
            token, nutri, "pacientes", aviso, p=p, a=a, erros=erros, evolucao=evolucao, grafico=grafico,
            dias_acomp=dias_acomp, hoje=datetime.now(ZoneInfo(nutri.get("fuso_horario") or FUSO)).date(),
            plano_texto=plano_texto if plano_texto is not None else para_texto(p.get("plano")),
            previa=previa, conflitos=conflitos_no_plano(previa or {}, a), mostrando_rascunho=mostrando_rascunho,
            tem_gramas=lambda q: gramas_do_item(q or "") is not None))

    def achar(token: str, paciente_id: str):
        nutri = nutri_do_token(token)
        if nutri is None:
            return None, None
        p = area.paciente(nutri["id"], paciente_id) if _UUID.match(paciente_id or "") else None
        return nutri, p

    # --- pacientes -------------------------------------------------------
    @rotas.get("/painel/{token}/pacientes", response_class=HTMLResponse, include_in_schema=False)
    def pacientes(token: str, aviso: str | None = None):
        nutri = nutri_do_token(token)
        if nutri is None:
            return invalido()
        return _html("pacientes.html", **contexto(token, nutri, "pacientes", aviso, pacientes=area.pacientes(nutri["id"])))

    @rotas.get("/painel/{token}/pacientes/novo", response_class=HTMLResponse, include_in_schema=False)
    def novo(token: str):
        nutri = nutri_do_token(token)
        if nutri is None:
            return invalido()
        return _html("paciente_novo.html", **contexto(token, nutri, "pacientes", form={}))

    @rotas.post("/painel/{token}/pacientes/novo", include_in_schema=False)
    def criar(token: str, form: dict = Depends(ler_form)):
        from whatsapp.area_repo import TelefoneDuplicado

        nutri = nutri_do_token(token)
        if nutri is None:
            return invalido()
        nome = (form.get("nome") or "").strip()[:120]
        telefone = normalizar_telefone(form.get("telefone", ""))
        erros = [e for ok, e in [(len(nome.split()) >= 2, "informe o nome completo"),
                                 (telefone is not None, "WhatsApp inválido (coloque com DDD)")] if not ok]
        if not erros:
            try:
                pid = area.criar_paciente(nutri["id"], nome, telefone)
                return ir(token, f"/pacientes/{pid}", "criado")
            except TelefoneDuplicado:
                erros.append("já existe um paciente com este WhatsApp")
        return _html("paciente_novo.html", 400, **contexto(token, nutri, "pacientes", form=form, erros=erros))

    @rotas.get("/painel/{token}/pacientes/{paciente_id}", response_class=HTMLResponse, include_in_schema=False)
    def ver_paciente(token: str, paciente_id: str, aviso: str | None = None):
        nutri, p = achar(token, paciente_id)
        if nutri is None:
            return invalido()
        if p is None:
            raise HTTPException(status_code=404, detail="paciente não encontrado")
        return pagina_paciente(token, nutri, p, aviso=aviso)

    @rotas.post("/painel/{token}/pacientes/{paciente_id}/dados", include_in_schema=False)
    def salvar_dados(token: str, paciente_id: str, form: dict = Depends(ler_form)):
        from whatsapp.area_repo import TelefoneDuplicado

        nutri, p = achar(token, paciente_id)
        if nutri is None:
            return invalido()
        if p is None:
            raise HTTPException(status_code=404, detail="paciente não encontrado")
        nome = (form.get("nome") or "").strip()[:120]
        telefone = normalizar_telefone(form.get("telefone", ""))
        erros = [e for ok, e in [(len(nome.split()) >= 2, "informe o nome completo"),
                                 (telefone is not None, "WhatsApp inválido (coloque com DDD)")] if not ok]
        if not erros:
            try:
                area.atualizar_paciente(nutri["id"], paciente_id, nome, telefone, form.get("ativo") == "1")
                return ir(token, f"/pacientes/{paciente_id}", "dados")
            except TelefoneDuplicado:
                erros.append("já existe um paciente com este WhatsApp")
        return pagina_paciente(token, nutri, {**p, "nome": nome or p["nome"]}, erros=erros, status=400)

    @rotas.post("/painel/{token}/pacientes/{paciente_id}/acompanhamento", include_in_schema=False)
    def acompanhamento(token: str, paciente_id: str, form: dict = Depends(ler_form)):
        nutri, p = achar(token, paciente_id)
        if nutri is None:
            return invalido()
        if p is None:
            raise HTTPException(status_code=404, detail="paciente não encontrado")
        modo = form.get("modo") or {"1": "sem_data", "0": "desligar"}.get(form.get("ligar", ""), "")
        if modo not in ("validade", "sem_data", "desligar"):
            raise HTTPException(status_code=400, detail="modo inválido")
        dias = int((area.consultorio(nutri["id"]) or {}).get("acompanhamento_dias") or 45)
        area.definir_acompanhamento(nutri["id"], paciente_id, modo, dias if modo == "validade" else None)
        aviso = {"desligar": "acompanhamento_off", "sem_data": "acompanhamento_on"}.get(
            modo, "acompanhamento_renovado" if p.get("acompanhamento_desde") else "acompanhamento_on")
        return ir(token, f"/pacientes/{paciente_id}", aviso)

    @rotas.post("/painel/{token}/pacientes/{paciente_id}/anamnese", include_in_schema=False)
    def salvar_anamnese(token: str, paciente_id: str, form: dict = Depends(ler_form)):
        nutri, p = achar(token, paciente_id)
        if nutri is None:
            return invalido()
        if p is None:
            raise HTTPException(status_code=404, detail="paciente não encontrado")
        area.salvar_anamnese(nutri["id"], paciente_id, anamnese_do_form(form))
        return ir(token, f"/pacientes/{paciente_id}", "anamnese")

    @rotas.post("/painel/{token}/pacientes/{paciente_id}/plano", include_in_schema=False)
    def salvar_plano(token: str, paciente_id: str, form: dict = Depends(ler_form)):
        nutri, p = achar(token, paciente_id)
        if nutri is None:
            return invalido()
        if p is None:
            raise HTTPException(status_code=404, detail="paciente não encontrado")
        texto = (form.get("plano") or "")[:50000]
        plano, erros = de_texto(texto)
        if erros or form.get("acao") != "salvar":
            return pagina_paciente(token, nutri, p, erros=erros or None, plano_texto=texto,
                                   previa=None if erros else plano, status=400 if erros else 200)
        antigo = p.get("plano") or {}
        plano = mesclar_extras(plano, antigo)
        plano = {"paciente": p["nome"], "nutricionista": nutri["nome"],
                 "vigente_desde": datetime.now(ZoneInfo(nutri.get("fuso_horario") or FUSO)).date().isoformat(),
                 **plano}
        area.salvar_plano(nutri["id"], paciente_id, plano)
        return ir(token, f"/pacientes/{paciente_id}", "plano")

    @rotas.post("/painel/{token}/pacientes/{paciente_id}/plano/importar", include_in_schema=False)
    async def importar_arquivo(token: str, paciente_id: str, request: Request):
        from starlette.concurrency import run_in_threadpool

        from bot.importar_plano import LIMITE_BYTES, ErroImportacao, mesclar_anamnese

        nutri, p = await run_in_threadpool(achar, token, paciente_id)
        if nutri is None:
            return invalido()
        if p is None:
            raise HTTPException(status_code=404, detail="paciente não encontrado")
        if int(request.headers.get("content-length") or 0) > LIMITE_BYTES + 100_000:
            return pagina_paciente(token, nutri, p, erros=["o arquivo passa de 10 MB"], status=413)
        form = await request.form(max_files=1, max_fields=5)
        arquivo = form.get("arquivo")
        dados = await arquivo.read(LIMITE_BYTES + 1) if hasattr(arquivo, "read") else b""
        try:
            imp = await run_in_threadpool(importar_fn, dados, getattr(arquivo, "content_type", None))
        except ErroImportacao as e:
            return pagina_paciente(token, nutri, p, erros=[str(e)], status=400)
        avisos = imp.avisos + ([f"{len(imp.itens_sem_gramas)} item(ns) sem gramas: o assistente não calcula calorias deles"]
                               if imp.itens_sem_gramas else [])
        if imp.anamnese:
            await run_in_threadpool(area.salvar_anamnese, nutri["id"], paciente_id, mesclar_anamnese(p.get("anamnese"), imp.anamnese))
            avisos.append("o documento cita restrições do paciente: foram somadas à anamnese; confira")
        await run_in_threadpool(area.salvar_rascunho, nutri["id"], paciente_id, imp.plano, "pdf", avisos)
        return ir(token, f"/pacientes/{paciente_id}", "importado")

    @rotas.post("/painel/{token}/pacientes/{paciente_id}/plano/descartar", include_in_schema=False)
    def descartar(token: str, paciente_id: str, form: dict = Depends(ler_form)):
        nutri, p = achar(token, paciente_id)
        if nutri is None:
            return invalido()
        if p is None:
            raise HTTPException(status_code=404, detail="paciente não encontrado")
        area.descartar_rascunho(nutri["id"], paciente_id)
        return ir(token, f"/pacientes/{paciente_id}", "descartado")

    @rotas.post("/painel/{token}/pacientes/{paciente_id}/excluir", include_in_schema=False)
    def excluir(token: str, paciente_id: str, form: dict = Depends(ler_form)):
        nutri, p = achar(token, paciente_id)
        if nutri is None:
            return invalido()
        if p is None:
            raise HTTPException(status_code=404, detail="paciente não encontrado")
        if " ".join((form.get("confirmacao") or "").split()).lower() != " ".join(p["nome"].split()).lower():
            return pagina_paciente(token, nutri, p, status=400,
                                   erros=["para excluir, digite o nome completo do paciente exatamente como está no cadastro"])
        area.excluir_paciente(nutri["id"], paciente_id)
        return ir(token, "/pacientes", "excluido")

    # --- reativação ------------------------------------------------------
    def pagina_reativacao(token, nutri, *, aviso=None, erros=None, lista=None, conferencia=None, form=None,
                          publico=None, status=200):
        from bot.reativacao import TEXTO_PADRAO, montar_mensagem

        form = form or {"dias": 90, "sem_data": False, "mensagem": TEXTO_PADRAO}
        c = area.consultorio(nutri["id"]) or {}
        previa = montar_mensagem(form["mensagem"], "Maria", nutri["nome"]) if publico is not None else None
        return _html("reativacao.html", status, **contexto(
            token, nutri, "reativacao", aviso, erros=erros, lista=lista, conferencia=conferencia, form=form,
            publico=publico, previa=previa, campanhas=reativacao.campanhas(nutri["id"]), reais=reais,
            valor_consulta=float(c["valor_consulta"]) if c.get("valor_consulta") is not None else None))

    def form_campanha(form: dict) -> tuple[dict, list[str]]:
        from bot.reativacao import TEXTO_PADRAO

        erros = []
        try:
            dias = int(form.get("dias") or 90)
        except ValueError:
            dias = 0
        if dias not in (45, 60, 90, 180, 365):
            erros.append("escolha o tempo sem consulta")
            dias = 90
        mensagem = (form.get("mensagem") or TEXTO_PADRAO).strip()[:700]
        if "{nome}" not in mensagem:
            erros.append("a mensagem precisa ter {nome}, para ser pessoal")
        return {"dias": dias, "sem_data": form.get("sem_data") == "1", "mensagem": mensagem}, erros

    if reativacao is not None:
        @rotas.get("/painel/{token}/reativacao", response_class=HTMLResponse, include_in_schema=False)
        def reativacao_pagina(token: str, aviso: str | None = None):
            nutri = nutri_do_token(token)
            if nutri is None:
                return invalido()
            return pagina_reativacao(token, nutri, aviso=aviso)

        @rotas.post("/painel/{token}/reativacao/importar", include_in_schema=False)
        def reativacao_importar(token: str, form: dict = Depends(ler_form)):
            from bot.reativacao import hoje_local, ler_lista

            nutri = nutri_do_token(token)
            if nutri is None:
                return invalido()
            lista = (form.get("lista") or "")[:200000]
            validos, erros = ler_lista(lista, hoje_local(nutri.get("fuso_horario") or FUSO))
            if form.get("acao") == "importar" and validos:
                r = reativacao.importar(nutri["id"], validos)
                resumo = f"{r['criados']} paciente(s) novo(s) e {r['atualizados']} já cadastrado(s)"
                return pagina_reativacao(token, nutri, aviso="importados",
                                         erros=[f"Importados: {resumo}."] + erros if erros else None,
                                         conferencia={"validos": validos, "erros": erros})
            return pagina_reativacao(token, nutri, lista=lista, conferencia={"validos": validos, "erros": erros},
                                     status=200 if validos else 400,
                                     erros=None if validos else ["nenhuma linha válida na lista"])

        @rotas.post("/painel/{token}/reativacao/campanha", include_in_schema=False)
        def reativacao_campanha(token: str, form: dict = Depends(ler_form)):
            nutri = nutri_do_token(token)
            if nutri is None:
                return invalido()
            dados, erros = form_campanha(form)
            publico = reativacao.publico(nutri["id"], dados["dias"], dados["sem_data"])
            if form.get("acao") == "enviar" and not erros:
                if form.get("confirmo") != "1":
                    erros.append("confirme que são seus pacientes e que você pode contatá-los")
                elif publico["total"] == 0:
                    erros.append("nenhum paciente para receber com esses filtros")
                else:
                    reativacao.criar_campanha(nutri["id"], dados["mensagem"], dados["dias"], dados["sem_data"])
                    return ir(token, "/reativacao", "campanha")
            return pagina_reativacao(token, nutri, form=dados, publico=publico, erros=erros or None,
                                     status=400 if erros else 200)

        @rotas.post("/painel/{token}/reativacao/campanha/{campanha_id}/{acao}", include_in_schema=False)
        def reativacao_pausar(token: str, campanha_id: str, acao: str, form: dict = Depends(ler_form)):
            nutri = nutri_do_token(token)
            if nutri is None:
                return invalido()
            if acao not in ("pausar", "retomar") or not _UUID.match(campanha_id):
                raise HTTPException(status_code=404)
            reativacao.pausar(nutri["id"], campanha_id, acao == "pausar")
            return ir(token, "/reativacao", "campanha_pausada" if acao == "pausar" else "campanha_retomada")

    # --- consultório -----------------------------------------------------
    @rotas.get("/painel/{token}/consultorio", response_class=HTMLResponse, include_in_schema=False)
    def consultorio(token: str, aviso: str | None = None):
        nutri = nutri_do_token(token)
        if nutri is None:
            return invalido()
        c = area.consultorio(nutri["id"]) or {}
        return _html("consultorio.html", **contexto(token, nutri, "consultorio", aviso, f=form_do_consultorio(c),
                                                     dias_semana=list(enumerate(DIAS_SEMANA, start=1)),
                                                     dias=list(enumerate(DIAS, start=1)),
                                                     google_conectado=bool(c.get("google_calendar_id"))))

    @rotas.post("/painel/{token}/consultorio", include_in_schema=False)
    def salvar_consultorio(token: str, form: dict = Depends(ler_form)):
        nutri = nutri_do_token(token)
        if nutri is None:
            return invalido()
        dados, erros = consultorio_do_form(form)
        if erros:
            c = area.consultorio(nutri["id"]) or {}
            f = {**form_do_consultorio(c), **form, "atende_convenio": form.get("atende_convenio") == "1"}
            return _html("consultorio.html", 400, **contexto(token, nutri, "consultorio", f=f, erros=erros,
                                                              dias_semana=list(enumerate(DIAS_SEMANA, start=1)),
                                                              dias=list(enumerate(DIAS, start=1)),
                                                              google_conectado=bool(c.get("google_calendar_id"))))
        area.salvar_consultorio(nutri["id"], dados)
        return ir(token, "/consultorio", "consultorio")

    return rotas
