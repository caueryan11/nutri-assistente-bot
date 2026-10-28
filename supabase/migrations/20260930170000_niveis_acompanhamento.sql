-- =====================================================================
-- Dois níveis de consulta: simples e com acompanhamento (validade + gancho do retorno).
--
--   nutricionistas.modelo_acompanhamento  'todos' (padrão: todo paciente tem dúvidas 24h, como antes)
--                                         'por_paciente' (só quem comprou a consulta com acompanhamento)
--   nutricionistas.acompanhamento_dias    validade do acompanhamento comprado na consulta (padrão 45 dias)
--   pacientes.acompanhamento_ate          até quando vale (null = sem data, ex.: assinatura mensal)
--   pacientes.aviso_fim_acompanhamento_em quando o bot avisou que está acabando (um aviso por ciclo)
-- =====================================================================

alter table public.nutricionistas
  add column if not exists modelo_acompanhamento text not null default 'todos',
  add column if not exists acompanhamento_dias integer not null default 45;
alter table public.nutricionistas drop constraint if exists nutricionistas_modelo_acompanhamento_valido;
alter table public.nutricionistas add constraint nutricionistas_modelo_acompanhamento_valido
  check (modelo_acompanhamento in ('todos', 'por_paciente') and acompanhamento_dias between 7 and 120);

alter table public.pacientes
  add column if not exists acompanhamento_ate date,
  add column if not exists aviso_fim_acompanhamento_em timestamptz;
