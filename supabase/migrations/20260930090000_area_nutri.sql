-- =====================================================================
-- Área da nutri (whatsapp/area.py): troca do plano ativo numa transação.
--   salvar_plano_ativo: desativa o plano atual (fica no histórico) e grava o novo como ativo.
--   Retorna o id do novo plano, ou null se o paciente não é desta nutri.
-- =====================================================================

create or replace function public.salvar_plano_ativo(p_nutri uuid, p_paciente uuid, p_conteudo jsonb)
returns uuid
language plpgsql
security invoker
set search_path = ''
as $$
declare
  novo uuid;
begin
  if jsonb_typeof(p_conteudo) <> 'object' then
    raise exception 'conteudo precisa ser um objeto';
  end if;
  perform 1 from public.pacientes where id = p_paciente and nutricionista_id = p_nutri for update;
  if not found then
    return null;
  end if;
  update public.planos_alimentares set ativo = false where paciente_id = p_paciente and ativo;
  insert into public.planos_alimentares (paciente_id, nutricionista_id, conteudo, ativo)
  values (p_paciente, p_nutri, p_conteudo, true)
  returning id into novo;
  return novo;
end;
$$;
revoke execute on function public.salvar_plano_ativo(uuid, uuid, jsonb) from public, anon, authenticated;
grant  execute on function public.salvar_plano_ativo(uuid, uuid, jsonb) to service_role;
