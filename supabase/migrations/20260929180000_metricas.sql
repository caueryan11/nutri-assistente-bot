-- =====================================================================
-- Métricas do consultório (bot/metricas.py).
--
--   nutricionistas.relatorio_semanal_enviado_em  dia do último relatório semanal
--                                                 (não manda dois no mesmo dia)
--   índices para as agregações por nutri e período
-- =====================================================================

alter table public.nutricionistas
  add column if not exists relatorio_semanal_enviado_em date;

create index if not exists agendamentos_nutri_criado_idx
  on public.agendamentos (nutricionista_id, criado_em) where origem = 'bot';

create index if not exists mensagens_cobranca_retorno_idx
  on public.mensagens (paciente_id, criado_em) where motivo = 'cobrança de retorno';
