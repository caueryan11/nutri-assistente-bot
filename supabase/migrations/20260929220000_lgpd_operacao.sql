-- =====================================================================
-- LGPD na operação:
--   pacientes.consentimento_revogado_em  quando o paciente respondeu PARAR (prova da revogação;
--                                         status_consentimento = 'revogado', consentimento_em = null)
-- A retenção (limpar_mensagens_antigas / limpar_lead_mensagens_antigas) passa a rodar todo dia
-- pelo agendador do app (bot/rotinas.py), com prazos em RETENCAO_DIAS / RETENCAO_LEADS_DIAS.
-- =====================================================================

alter table public.pacientes
  add column if not exists consentimento_revogado_em timestamptz;
