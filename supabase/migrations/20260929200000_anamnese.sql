-- =====================================================================
-- Anamnese do paciente (bot/anamnese.py): o que o bot nunca pode sugerir.
--
--   pacientes.anamnese  {"alergias": [...], "intolerancias": [...], "nao_gosta": [...],
--                        "restricoes": [...], "observacoes": "..."}
-- Dado de saúde: mesma proteção da tabela (RLS por nutri). A nutri preenche.
-- =====================================================================

alter table public.pacientes
  add column if not exists anamnese jsonb not null default '{}'::jsonb;

alter table public.pacientes
  drop constraint if exists pacientes_anamnese_objeto;
alter table public.pacientes
  add constraint pacientes_anamnese_objeto check (
    jsonb_typeof(anamnese) = 'object'
    and (not anamnese ? 'alergias'      or jsonb_typeof(anamnese -> 'alergias')      = 'array')
    and (not anamnese ? 'intolerancias' or jsonb_typeof(anamnese -> 'intolerancias') = 'array')
    and (not anamnese ? 'nao_gosta'     or jsonb_typeof(anamnese -> 'nao_gosta')     = 'array')
    and (not anamnese ? 'restricoes'    or jsonb_typeof(anamnese -> 'restricoes')    = 'array')
  );
