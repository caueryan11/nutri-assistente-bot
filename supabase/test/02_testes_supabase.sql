-- Os mesmos testes de 01_testes.sql, adaptados para rodar no SQL Editor do Supabase.
-- Tudo roda numa transação e termina em ROLLBACK: nenhum dado de teste fica no banco.
-- Se algum teste falhar, o editor mostra o erro "FALHOU: ...".
-- Se passar, aparece a linha "TODOS OS TESTES PASSARAM" (antes do rollback).
-- Adaptado de 01_testes.sql; se mudar um, mude o outro.

begin;

-- Os testes usam uma amostra própria da TACO; a TACO real volta no ROLLBACK.
delete from public.alimentos_taco;

-- ---------------------------------------------------------------------
-- Dados fictícios
-- ---------------------------------------------------------------------
insert into auth.users (id, email) values
  ('aaaaaaaa-0000-0000-0000-000000000001', 'nutri.a@teste.com'),
  ('bbbbbbbb-0000-0000-0000-000000000001', 'nutri.b@teste.com');

insert into public.nutricionistas (id, auth_user_id, nome, whatsapp_phone_number_id) values
  ('aaaaaaaa-1111-0000-0000-000000000000', 'aaaaaaaa-0000-0000-0000-000000000001', 'Nutri A', 'PHONE_A'),
  ('bbbbbbbb-1111-0000-0000-000000000000', 'bbbbbbbb-0000-0000-0000-000000000001', 'Nutri B', 'PHONE_B');

insert into public.pacientes (id, nutricionista_id, nome, telefone, consentimento_em) values
  ('aaaaaaaa-2222-0000-0000-000000000001', 'aaaaaaaa-1111-0000-0000-000000000000', 'Maria Silva', '5511999990001', now()),
  ('aaaaaaaa-2222-0000-0000-000000000002', 'aaaaaaaa-1111-0000-0000-000000000000', 'João Souza',  '5511999990002', null),
  ('bbbbbbbb-2222-0000-0000-000000000001', 'bbbbbbbb-1111-0000-0000-000000000000', 'Ana Lima',    '5521999990001', now());

insert into public.planos_alimentares (paciente_id, nutricionista_id, conteudo) values
  ('aaaaaaaa-2222-0000-0000-000000000001', 'aaaaaaaa-1111-0000-0000-000000000000', '{"refeicoes": []}'),
  ('bbbbbbbb-2222-0000-0000-000000000001', 'bbbbbbbb-1111-0000-0000-000000000000', '{"refeicoes": []}');

insert into public.mensagens (nutricionista_id, paciente_id, direcao, wa_message_id, conteudo, criado_em) values
  ('aaaaaaaa-1111-0000-0000-000000000000', 'aaaaaaaa-2222-0000-0000-000000000001', 'entrada', 'wamid.A1', 'Posso trocar o arroz?', now()),
  ('aaaaaaaa-1111-0000-0000-000000000000', 'aaaaaaaa-2222-0000-0000-000000000001', 'entrada', 'wamid.A0', 'mensagem antiga', now() - interval '200 days'),
  ('bbbbbbbb-1111-0000-0000-000000000000', 'bbbbbbbb-2222-0000-0000-000000000001', 'entrada', 'wamid.B1', 'Oi', now());

insert into public.regras_clinicas (nutricionista_id, versao, conteudo) values
  (null, 1, 'regra padrão'),
  ('bbbbbbbb-1111-0000-0000-000000000000', 1, 'regra da B');

-- Amostra TACO (valores ilustrativos só para teste de busca)
insert into public.alimentos_taco (id, nome, grupo, energia_kcal, proteina_g, fibra_g, marcacoes) values
  (1, 'Arroz, integral, cozido',          'Cereais e derivados', 124, 2.6, 2.7, '{}'),
  (3, 'Arroz, tipo 1, cozido',            'Cereais e derivados', 128, 2.5, 1.6, '{}'),
  (90,'Batata, doce, cozida',             'Verduras, hortaliças e derivados', 77, 0.6, 2.2, '{}'),
  (91,'Batata, inglesa, cozida',          'Verduras, hortaliças e derivados', 52, 1.2, 1.3, '{}'),
  (409,'Frango, peito, sem pele, grelhado','Carnes e derivados', 159, 32.0, null, '{"fibra_g": "NA"}'),
  (488,'Leite, de vaca, integral',        'Leite e derivados', 61, 3.0, null, '{"fibra_g": "NA"}'),
  (200,'Açúcar, cristal',                 'Produtos açucarados', 387, 0.3, null, '{"fibra_g": "NA"}');

-- Helpers de teste
create function pg_temp.como_nutri(auth_id uuid) returns void language plpgsql as $$
begin
  perform set_config('request.jwt.claim.sub', auth_id::text, false);
  execute 'set role authenticated';
end $$;

select pg_temp.como_nutri('aaaaaaaa-0000-0000-0000-000000000001');
do $$
begin
  if (select count(*) from public.nutricionistas) <> 1 then raise exception 'FALHOU: vê outras nutris'; end if;
  if (select count(*) from public.pacientes) <> 2 then raise exception 'FALHOU: pacientes visíveis = %', (select count(*) from public.pacientes); end if;
  if exists (select 1 from public.pacientes where nutricionista_id <> 'aaaaaaaa-1111-0000-0000-000000000000') then raise exception 'FALHOU: vê paciente da B'; end if;
  if (select count(*) from public.planos_alimentares) <> 1 then raise exception 'FALHOU: planos'; end if;
  if (select count(*) from public.mensagens) <> 2 then raise exception 'FALHOU: mensagens'; end if;
  if (select count(*) from public.regras_clinicas) <> 1 then raise exception 'FALHOU: A deveria ver só a regra padrão'; end if;
  if (select count(*) from public.alimentos_taco) <> 7 then raise exception 'FALHOU: TACO deveria ser legível'; end if;
end $$;

do $$
begin
  begin
    insert into public.pacientes (nutricionista_id, nome, telefone)
    values ('bbbbbbbb-1111-0000-0000-000000000000', 'Intruso', '5500000000000');
    raise exception 'FALHOU: inseriu paciente na B';
  exception when insufficient_privilege then null;  -- esperado (violação de RLS)
  end;

  update public.pacientes set nome = 'hack' where id = 'bbbbbbbb-2222-0000-0000-000000000001';
  -- RLS esconde a linha: update não afeta nada
  begin
    insert into public.mensagens (nutricionista_id, paciente_id, direcao, conteudo)
    values ('aaaaaaaa-1111-0000-0000-000000000000', 'aaaaaaaa-2222-0000-0000-000000000001', 'saida', 'x');
    raise exception 'FALHOU: nutri gravou mensagem (só o backend grava)';
  exception when insufficient_privilege then null;
  end;

  begin
    insert into public.alimentos_taco (id, nome) values (9999, 'falso');
    raise exception 'FALHOU: nutri editou a TACO';
  exception when insufficient_privilege then null;
  end;

  if public.excluir_paciente('bbbbbbbb-2222-0000-0000-000000000001') then
    raise exception 'FALHOU: A excluiu paciente da B';
  end if;

  begin
    perform public.limpar_mensagens_antigas(30);
    raise exception 'FALHOU: nutri rodou limpeza de mensagens';
  exception when insufficient_privilege then null;
  end;
end $$;
reset role;
do $$ begin
  if (select nome from public.pacientes where id = 'bbbbbbbb-2222-0000-0000-000000000001') <> 'Ana Lima' then
    raise exception 'FALHOU: A alterou paciente da B';
  end if;
end $$;

set role anon;
do $$ begin
  if (select count(*) from public.pacientes) + (select count(*) from public.mensagens)
     + (select count(*) from public.alimentos_taco) <> 0 then
    raise exception 'FALHOU: anon vê dados';
  end if;
end $$;
reset role;

set role service_role;
do $$ begin
  begin
    insert into public.planos_alimentares (paciente_id, nutricionista_id, conteudo, ativo)
    values ('bbbbbbbb-2222-0000-0000-000000000001', 'aaaaaaaa-1111-0000-0000-000000000000', '{}', false);
    raise exception 'FALHOU: plano cruzado aceito';
  exception when foreign_key_violation then null;
  end;

  begin
    insert into public.planos_alimentares (paciente_id, nutricionista_id, conteudo)
    values ('aaaaaaaa-2222-0000-0000-000000000001', 'aaaaaaaa-1111-0000-0000-000000000000', '{"v": 2}');
    raise exception 'FALHOU: dois planos ativos';
  exception when unique_violation then null;
  end;

  begin
    insert into public.mensagens (nutricionista_id, paciente_id, direcao, wa_message_id)
    values ('aaaaaaaa-1111-0000-0000-000000000000', 'aaaaaaaa-2222-0000-0000-000000000001', 'entrada', 'wamid.A1');
    raise exception 'FALHOU: wa_message_id duplicado aceito';
  exception when unique_violation then null;
  end;

  begin
    insert into public.planos_alimentares (paciente_id, nutricionista_id, conteudo, ativo)
    values ('aaaaaaaa-2222-0000-0000-000000000002', 'aaaaaaaa-1111-0000-0000-000000000000', '[1,2]', true);
    raise exception 'FALHOU: plano que não é objeto JSON aceito';
  exception when check_violation then null;
  end;
end $$;

do $$ begin
  insert into public.agendamentos (nutricionista_id, paciente_id, inicio, fim, status)
  values ('aaaaaaaa-1111-0000-0000-000000000000', 'aaaaaaaa-2222-0000-0000-000000000001',
          '2026-10-05 13:00-03', '2026-10-05 14:00-03', 'confirmado');
  begin
    insert into public.agendamentos (nutricionista_id, paciente_id, inicio, fim)
    values ('aaaaaaaa-1111-0000-0000-000000000000', 'aaaaaaaa-2222-0000-0000-000000000002',
            '2026-10-05 13:30-03', '2026-10-05 14:30-03');
    raise exception 'FALHOU: horário sobreposto aceito';
  exception when exclusion_violation then null;
  end;
  -- Horário encostado (14:00) pode; outra nutri no mesmo horário pode; cancelado não bloqueia.
  insert into public.agendamentos (nutricionista_id, paciente_id, inicio, fim)
  values ('aaaaaaaa-1111-0000-0000-000000000000', 'aaaaaaaa-2222-0000-0000-000000000002',
          '2026-10-05 14:00-03', '2026-10-05 15:00-03');
  insert into public.agendamentos (nutricionista_id, paciente_id, inicio, fim)
  values ('bbbbbbbb-1111-0000-0000-000000000000', 'bbbbbbbb-2222-0000-0000-000000000001',
          '2026-10-05 13:00-03', '2026-10-05 14:00-03');
  update public.agendamentos set status = 'cancelado'
   where paciente_id = 'aaaaaaaa-2222-0000-0000-000000000001';
  insert into public.agendamentos (nutricionista_id, paciente_id, inicio, fim)
  values ('aaaaaaaa-1111-0000-0000-000000000000', 'aaaaaaaa-2222-0000-0000-000000000002',
          '2026-10-05 13:00-03', '2026-10-05 13:45-03');
end $$;

do $$
declare r record;
begin
  select * into r from public.buscar_alimento('batata doce', 3) limit 1;
  if r.id <> 90 then raise exception 'FALHOU: "batata doce" -> %', r.nome; end if;

  select * into r from public.buscar_alimento('ACUCAR') limit 1;
  if r.id <> 200 then raise exception 'FALHOU: sem acento -> %', r.nome; end if;

  select * into r from public.buscar_alimento('frango') limit 1;
  if r.id <> 409 then raise exception 'FALHOU: frango -> %', r.nome; end if;
  if r.fibra_g is not null or r.marcacoes->>'fibra_g' <> 'NA' then raise exception 'FALHOU: NA virou número'; end if;

  select * into r from public.buscar_alimento('arros') limit 1;  -- erro de digitação
  if r.id not in (1, 3) then raise exception 'FALHOU: "arros" -> %', coalesce(r.nome, 'nada'); end if;

  if (select count(*) from public.buscar_alimento('arroz', 10)) <> 2 then raise exception 'FALHOU: arroz deveria dar 2'; end if;
  if (select count(*) from public.buscar_alimento('a')) <> 0 then raise exception 'FALHOU: termo de 1 letra'; end if;
  if (select count(*) from public.buscar_alimento('xyzqwk')) <> 0 then raise exception 'FALHOU: lixo retornou algo'; end if;
end $$;

do $$ begin
  if public.limpar_mensagens_antigas(180) <> 1 then raise exception 'FALHOU: limpeza deveria apagar 1'; end if;
  begin
    perform public.limpar_mensagens_antigas(0);
    raise exception 'FALHOU: aceitou 0 dias';
  exception when raise_exception then
    if sqlerrm like 'FALHOU%' then raise; end if;
  end;
end $$;
reset role;

select pg_temp.como_nutri('aaaaaaaa-0000-0000-0000-000000000001');
do $$ begin
  if not public.excluir_paciente('aaaaaaaa-2222-0000-0000-000000000001') then
    raise exception 'FALHOU: A não conseguiu excluir paciente dela';
  end if;
end $$;
reset role;
do $$ begin
  if exists (select 1 from public.mensagens    where paciente_id = 'aaaaaaaa-2222-0000-0000-000000000001')
  or exists (select 1 from public.planos_alimentares where paciente_id = 'aaaaaaaa-2222-0000-0000-000000000001')
  or exists (select 1 from public.agendamentos where paciente_id = 'aaaaaaaa-2222-0000-0000-000000000001') then
    raise exception 'FALHOU: exclusão não apagou em cascata';
  end if;
  if (select count(*) from public.pacientes where nutricionista_id in ('aaaaaaaa-1111-0000-0000-000000000000', 'bbbbbbbb-1111-0000-0000-000000000000')) <> 2 then raise exception 'FALHOU: apagou paciente a mais'; end if;
end $$;

do $$ begin
  if (select primeiro_nome from public.pacientes where id = 'aaaaaaaa-2222-0000-0000-000000000002') <> 'João' then
    raise exception 'FALHOU: primeiro_nome';
  end if;
  if (select consentimento_em from public.pacientes where id = 'aaaaaaaa-2222-0000-0000-000000000002') is not null then
    raise exception 'FALHOU: consentimento deveria estar vazio';
  end if;
end $$;

do $$ begin
  if (select status_consentimento from public.pacientes where id = 'aaaaaaaa-2222-0000-0000-000000000002') <> 'pendente' then
    raise exception 'FALHOU: sem consentimento deveria estar pendente';
  end if;
  if (select status_consentimento from public.pacientes where id = 'bbbbbbbb-2222-0000-0000-000000000001') <> 'aceito' then
    raise exception 'FALHOU: com consentimento_em deveria estar aceito';
  end if;
  update public.pacientes set consentimento_em = now(), versao_termo = 'v1.0'
   where id = 'aaaaaaaa-2222-0000-0000-000000000002';
  if (select status_consentimento from public.pacientes where id = 'aaaaaaaa-2222-0000-0000-000000000002') <> 'aceito' then
    raise exception 'FALHOU: aceite não mudou o status';
  end if;
  update public.pacientes set consentimento_em = null where id = 'aaaaaaaa-2222-0000-0000-000000000002';
  if (select status_consentimento from public.pacientes where id = 'aaaaaaaa-2222-0000-0000-000000000002') <> 'pendente' then
    raise exception 'FALHOU: sem data o status deveria voltar a pendente';
  end if;
  begin
    update public.pacientes set status_consentimento = 'qualquer' where id = 'aaaaaaaa-2222-0000-0000-000000000002';
    raise exception 'FALHOU: status inválido aceito';
  exception when check_violation then null;
  end;
end $$;

reset role;
select 'TODOS OS TESTES PASSARAM' as resultado;
rollback;
