"""Lê o plano alimentar de um PDF ou foto (o mesmo arquivo que a nutri já manda ao paciente).

O Claude transcreve para o formato do sistema; o resultado vira RASCUNHO e só vale depois que a nutri
confere e ativa (painel). Nada é inventado: o que não der para ler vai em `avisos`.
Modelo: MODELO_IMPORTACAO (padrão Sonnet 5.5: acontece uma vez por plano e precisa ler bem tabelas e fotos).
"""

from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass, field

import anthropic

from bot.anamnese import CATEGORIAS, limpar
from bot.plano import gramas_do_item
from bot.plano_texto import de_texto, para_texto

log = logging.getLogger(__name__)

MODELO_IMPORTACAO_PADRAO = "claude-sonnet-5-5"
LIMITE_BYTES = 10 * 1024 * 1024
TIPOS = {"application/pdf": "document", "image/jpeg": "image", "image/png": "image", "image/webp": "image",
         "image/gif": "image"}


class ErroImportacao(Exception):
    """Mensagem pronta para mostrar à nutri."""


@dataclass
class Importacao:
    plano: dict
    paciente_nome: str | None = None
    anamnese: dict = field(default_factory=dict)
    avisos: list[str] = field(default_factory=list)
    tokens_entrada: int = 0
    tokens_saida: int = 0

    @property
    def total_itens(self) -> int:
        return sum(len(r["itens"]) for r in self.plano.get("refeicoes", []))

    @property
    def itens_sem_gramas(self) -> list[str]:
        return [i["alimento"] for r in self.plano.get("refeicoes", []) for i in r["itens"]
                if gramas_do_item(i.get("quantidade") or "") is None]


def tipo_do_arquivo(dados: bytes, mimetype: str | None = None) -> str | None:
    """Confere pelos primeiros bytes (não confia só na extensão/mimetype)."""
    if dados[:5] == b"%PDF-":
        return "application/pdf"
    if dados[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if dados[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if dados[:4] == b"RIFF" and dados[8:12] == b"WEBP":
        return "image/webp"
    if dados[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None


INSTRUCOES = """Você recebe o plano alimentar que uma nutricionista entregou a um paciente (PDF ou foto).
Transcreva-o com fidelidade chamando a ferramenta `registrar_plano`.

Regras:
- Não invente nada: nenhuma quantidade, horário, substituição ou alimento que não esteja no documento.
- Copie as quantidades como estão escritas, incluindo medidas caseiras e gramas/ml (ex.: "4 colheres de sopa (100 g)").
  Se o documento trouxer só a medida caseira, copie só ela.
- Cada refeição com nome e horário (HH:MM, vazio se não houver).
- "Opção 1 / Opção 2", "ou", listas de equivalentes e trocas do mesmo item viram `substituicoes` do item principal.
- Orientações gerais (água, temperos, o que evitar) vão em `observacoes_gerais`.
- Se o documento citar alergias, intolerâncias, restrições (ex.: vegetariano) ou alimentos que o paciente não gosta,
  preencha `anamnese`. Não deduza: só o que estiver escrito.
- Tudo que estiver ilegível, cortado, ambíguo ou que você não soube encaixar vai em `avisos`, em português, curto.
- Se o arquivo não for um plano alimentar, chame a ferramenta com `eh_plano_alimentar` = false e refeições vazias.
"""

FERRAMENTA = {
    "name": "registrar_plano",
    "description": "Registra o plano alimentar transcrito do documento.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "eh_plano_alimentar": {"type": "boolean"},
            "paciente_nome": {"type": "string", "description": "Nome do paciente como aparece no documento (vazio se não houver)."},
            "refeicoes": {"type": "array", "items": {"type": "object", "properties": {
                "nome": {"type": "string"},
                "horario": {"type": "string", "description": "HH:MM ou vazio"},
                "itens": {"type": "array", "items": {"type": "object", "properties": {
                    "alimento": {"type": "string"},
                    "quantidade": {"type": "string"},
                    "substituicoes": {"type": "array", "items": {"type": "string"}},
                }, "required": ["alimento", "quantidade", "substituicoes"], "additionalProperties": False}},
            }, "required": ["nome", "horario", "itens"], "additionalProperties": False}},
            "observacoes_gerais": {"type": "array", "items": {"type": "string"}},
            "anamnese": {"type": "object", "properties": {k: {"type": "array", "items": {"type": "string"}}
                                                          for k in CATEGORIAS},
                         "required": list(CATEGORIAS), "additionalProperties": False},
            "avisos": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["eh_plano_alimentar", "paciente_nome", "refeicoes", "observacoes_gerais", "anamnese", "avisos"],
        "additionalProperties": False,
    },
}


def _normalizar(bruto: dict) -> tuple[dict, list[str]]:
    """Passa pelo formato de texto: valida horários/itens e devolve o plano no formato do sistema."""
    refeicoes = []
    for r in bruto.get("refeicoes") or []:
        itens = [{"alimento": str(i.get("alimento") or "").replace("|", "/").strip(),
                  "quantidade": str(i.get("quantidade") or "").replace("|", "/").strip(),
                  "substituicoes": [str(s).replace("|", "/").replace(";", ",").strip()
                                    for s in i.get("substituicoes") or [] if str(s).strip()]}
                 for i in r.get("itens") or [] if str(i.get("alimento") or "").strip()]
        horario = str(r.get("horario") or "").strip()
        refeicoes.append({"nome": str(r.get("nome") or "Refeição").replace("#", "").strip(),
                          **({"horario": horario} if horario else {}), "itens": itens})
    rascunho = {"refeicoes": refeicoes}
    obs = [str(o).strip() for o in bruto.get("observacoes_gerais") or [] if str(o).strip()]
    if obs:
        rascunho["observacoes_gerais"] = obs
    return de_texto(para_texto(rascunho))


def importar(dados: bytes, mimetype: str | None = None, *, cliente: anthropic.Anthropic | None = None,
             modelo: str | None = None) -> Importacao:
    if not dados:
        raise ErroImportacao("o arquivo veio vazio")
    if len(dados) > LIMITE_BYTES:
        raise ErroImportacao("o arquivo passa de 10 MB; mande um PDF menor ou uma foto")
    tipo = tipo_do_arquivo(dados, mimetype)
    if tipo is None:
        raise ErroImportacao("formato não suportado: mande o plano em PDF ou foto (JPG/PNG). Arquivo do Word: salve como PDF")
    bloco = {"type": TIPOS[tipo], "source": {"type": "base64", "media_type": tipo,
                                              "data": base64.standard_b64encode(dados).decode("ascii")}}
    cliente = cliente or anthropic.Anthropic()
    modelo = modelo or os.environ.get("MODELO_IMPORTACAO") or MODELO_IMPORTACAO_PADRAO
    # Sonnet 5.5 não aceita forçar a ferramenta (tool_choice any/tool): pede no texto e, se não vier, pede de novo.
    mensagens = [{"role": "user", "content": [bloco, {"type": "text", "text":
                  "Transcreva este plano alimentar chamando a ferramenta registrar_plano."}]}]
    chamada, resposta = None, None
    for tentativa in (1, 2):
        try:
            resposta = cliente.messages.create(model=modelo, max_tokens=8000, system=INSTRUCOES, tools=[FERRAMENTA],
                                               messages=mensagens)
        except anthropic.APIError as e:
            log.warning("importação: erro na API (%s)", type(e).__name__)
            raise ErroImportacao("não consegui ler o arquivo agora (serviço de IA indisponível); "
                                 "tente de novo em alguns minutos") from e
        chamada = next((b for b in resposta.content if b.type == "tool_use"), None)
        if chamada is not None or resposta.stop_reason == "max_tokens":
            break
        mensagens += [{"role": "assistant", "content": resposta.content},
                      {"role": "user", "content": "Responda chamando a ferramenta registrar_plano."}]
    if chamada is None or resposta.stop_reason == "max_tokens":
        raise ErroImportacao("o plano é longo demais para ler de uma vez; divida em partes ou fale com o suporte")
    bruto = chamada.input if isinstance(chamada.input, dict) else {}
    if not bruto.get("eh_plano_alimentar", True):
        raise ErroImportacao("o arquivo não parece ser um plano alimentar")
    plano, erros = _normalizar(bruto)
    if not plano.get("refeicoes"):
        raise ErroImportacao("não encontrei refeições no arquivo; confira se é o plano alimentar e se está legível")
    imp = Importacao(
        plano=plano, paciente_nome=(str(bruto.get("paciente_nome") or "").strip() or None),
        anamnese={k: v for k, v in limpar(bruto.get("anamnese")).items() if k in CATEGORIAS and v},
        avisos=[str(a).strip() for a in bruto.get("avisos") or [] if str(a).strip()] + erros,
        tokens_entrada=resposta.usage.input_tokens, tokens_saida=resposta.usage.output_tokens,
    )
    log.info("importação: %d refeições, %d itens, %d aviso(s)", len(plano["refeicoes"]), imp.total_itens, len(imp.avisos))
    return imp


def mesclar_anamnese(atual: dict | None, nova: dict) -> dict:
    """União sem repetir (restringir a mais nunca faz mal); observações ficam como estão."""
    a = limpar(atual)
    for k in CATEGORIAS:
        vistos = {x.lower() for x in a[k]}
        a[k] += [x for x in nova.get(k, []) if x.lower() not in vistos]
    return a
