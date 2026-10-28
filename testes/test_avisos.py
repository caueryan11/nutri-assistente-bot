"""Avisos para o WhatsApp da nutri: curtos, com cor de prioridade e link que abre a conversa."""

from bot import avisos


def test_duvida_comum_e_amarela_com_link():
    t = avisos.paciente_precisa("Maria Silva", "+55 (51) 99999-0000", "quer trocar o arroz do jantar por batata-doce")
    linhas = t.split("\n")
    assert linhas[0] == "🟡 *Responder · Maria Silva*"
    assert linhas[1] == "Quer trocar o arroz do jantar por batata-doce."
    assert linhas[2] == "👉 Responda quando puder: wa.me/5551999990000"
    assert len(linhas) == 3


def test_urgente_e_vermelho():
    t = avisos.paciente_precisa("Maria Silva", "5551999990000", "Tontura e enjoo desde ontem.", urgente=True)
    assert t.startswith("🔴 *Urgente · Maria Silva*") and "Fale com o paciente hoje" in t


def test_resumo_longo_e_cortado_em_uma_linha():
    t = avisos.curto("linha 1\n\nlinha 2 " + "x" * 400)
    assert "\n" not in t and len(t) <= avisos.LIMITE_RESUMO and t.endswith("…")


def test_checkin():
    t = avisos.checkin("Maria Silva", "5551999990000", ["relata tontura", "peso variou -3,0 kg desde o último check-in"],
                       "adesão 6/10 · fome muita · intestino solto")
    assert t.split("\n") == ["🔴 *Check-in pede atenção · Maria Silva*",
                             "Relata tontura; peso variou -3,0 kg desde o último check-in.",
                             "Semana: adesão 6/10 · fome muita · intestino solto",
                             "👉 Fale com o paciente hoje: wa.me/5551999990000"]


def test_agenda():
    assert avisos.agenda("Ana", {"tipo": "marcado", "descricao": "qua 30/09 às 10:00"}) == \
        "📅 *Consulta marcada · Ana*\nqua 30/09 às 10:00. Já está na sua agenda."
    assert "qua 30/09 às 10:00 → sex 02/10 às 15:00" in avisos.agenda(
        "Ana", {"tipo": "remarcado", "de": "qua 30/09 às 10:00", "para": "sex 02/10 às 15:00"})
    assert "liberado" in avisos.agenda("Ana", {"tipo": "cancelado", "descricao": "qua 30/09 às 10:00"})


def test_midia_e_falha_sem_jargao():
    assert avisos.resumo_midia("audio") == "Mandou um áudio; o assistente só lê texto, então você precisa ver"
    t = avisos.resumo_sem_resposta("posso comer pizza hoje?")
    assert t == "O assistente não conseguiu responder: \"Posso comer pizza hoje?\"" and "API" not in t


def test_novo_paciente_lead_e_revogacao():
    t = avisos.novo_paciente("Ana Lima", "5511988887777", "qua 30/09 às 10:00", None, "ganho de massa")
    assert t.startswith("🆕 *Novo paciente · Ana Lima*") and "Objetivo: ganho de massa" in t
    assert "(ainda não é paciente)" in avisos.lead_precisa("5511988887777", None, "Quer negociar o valor.")
    assert avisos.revogacao("Maria").startswith("⛔ *Saiu do assistente · Maria*")
