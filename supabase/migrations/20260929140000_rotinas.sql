-- =====================================================================
-- Rotinas automáticas (lembrete 24 h, cobrança de retorno, resumo das 8h).
--
--   agendamentos.lembrete_enviado_em   já existia (quando o lembrete saiu; índice agendamentos_lembrete_idx)
--   agendamentos.lembrete_24h_enviado  calculada a partir de lembrete_enviado_em (não dessincroniza)
--   pacientes.ultima_consulta_em       última consulta conhecida (a nutri pode preencher para quem veio
--                                       antes do sistema; as consultas do bot também contam)
--   pacientes.ultimo_aviso_retorno_em  quando o bot cobrou o retorno (no máximo 1 a cada 30 dias)
--   nutricionistas.resumo_enviado_em   dia do último resumo das 8h (não manda dois no mesmo dia)
-- =====================================================================

alter table public.agendamentos
  add column if not exists lembrete_24h_enviado boolean
    generated always as (lembrete_enviado_em is not null) stored;

alter table public.pacientes
  add column if not exists ultima_consulta_em timestamptz,
  add column if not exists ultimo_aviso_retorno_em timestamptz;

alter table public.nutricionistas
  add column if not exists resumo_enviado_em date;

create index if not exists mensagens_saida_recentes_idx
  on public.mensagens (nutricionista_id, criado_em) where direcao = 'saida';
