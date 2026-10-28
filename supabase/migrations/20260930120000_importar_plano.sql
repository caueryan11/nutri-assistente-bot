-- =====================================================================
-- Importação do plano (PDF/foto pelo painel ou pelo WhatsApp da nutri).
--   planos_alimentares.rascunho   plano lido pela IA que a nutri ainda não conferiu (nunca ativo)
--   planos_alimentares.origem     painel | pdf | whatsapp
--   planos_alimentares.avisos     o que a leitura não entendeu, para a nutri conferir
--   salvar_rascunho_plano         troca o rascunho do paciente (um por paciente)
--   salvar_plano_ativo            agora também apaga o rascunho (foi conferido)
-- =====================================================================

alter table public.planos_alimentares
  add column if not exists rascunho boolean not null default false,
  add column if not exists origem   text not null default 'painel',
  add column if not exists avisos   jsonb not null default '[]'::jsonb;

alter table public.planos_alimentares drop constraint if exists planos_rascunho_nunca_ativo;
alter table public.planos_alimentares add constraint planos_rascunho_nunca_ativo check (not (rascunho and ativo));
alter table public.planos_alimentares drop constraint if exists planos_origem_valida;
alter table public.planos_alimentares add constraint planos_origem_valida check (origem in ('painel', 'pdf', 'whatsapp'));

create or replace function public.salvar_rascunho_plano(p_nutri uuid, p_paciente uuid, p_conteudo jsonb,
                                                         p_origem text, p_avisos jsonb)
returns uuid
language plpgsql
security invoker
set search_path = ''
as $$
declare
  novo uuid;
begin
  perform 1 from public.pacientes where id = p_paciente and nutricionista_id = p_nutri for update;
  if not found then
    return null;
  end if;
  delete from public.planos_alimentares where paciente_id = p_paciente and rascunho;
  insert into public.planos_alimentares (paciente_id, nutricionista_id, conteudo, ativo, rascunho, origem, avisos)
  values (p_paciente, p_nutri, p_conteudo, false, true, p_origem, coalesce(p_avisos, '[]'::jsonb))
  returning id into novo;
  return novo;
end;
$$;
revoke execute on function public.salvar_rascunho_plano(uuid, uuid, jsonb, text, jsonb) from public, anon, authenticated;
grant  execute on function public.salvar_rascunho_plano(uuid, uuid, jsonb, text, jsonb) to service_role;

create or replace function public.salvar_plano_ativo(p_nutri uuid, p_paciente uuid, p_conteudo jsonb)
returns uuid
language plpgsql
security invoker
set search_path = ''
as $$
declare
  novo uuid;
  v_origem text;
begin
  if jsonb_typeof(p_conteudo) <> 'object' then
    raise exception 'conteudo precisa ser um objeto';
  end if;
  perform 1 from public.pacientes where id = p_paciente and nutricionista_id = p_nutri for update;
  if not found then
    return null;
  end if;
  -- o plano ativado herda a origem do rascunho conferido (se havia um)
  select origem into v_origem from public.planos_alimentares where paciente_id = p_paciente and rascunho;
  delete from public.planos_alimentares where paciente_id = p_paciente and rascunho;
  update public.planos_alimentares set ativo = false where paciente_id = p_paciente and ativo;
  insert into public.planos_alimentares (paciente_id, nutricionista_id, conteudo, ativo, origem)
  values (p_paciente, p_nutri, p_conteudo, true, coalesce(v_origem, 'painel'))
  returning id into novo;
  return novo;
end;
$$;
