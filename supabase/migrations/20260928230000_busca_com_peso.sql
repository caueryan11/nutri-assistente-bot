-- =====================================================================
-- Busca na TACO com peso por palavra.
--
-- Com peso igual, "carne de patinho moída cozida" trazia "Carne, bovina,
-- paleta, sem gordura, cozida": "carne" e "cozida" aparecem em dezenas de
-- alimentos e venciam "patinho", que só aparece em 2.
--
-- Agora cada palavra pesa 1 + ln(N / (1 + df)), onde df = quantos alimentos
-- têm uma palavra começando com ela (palavra rara pesa mais). Palavra que
-- não aparece em nenhum alimento pesa 1 (não ajuda a distinguir).
-- cobertura = média ponderada das notas (>= 0,5 para aparecer)
-- relevancia = cobertura + 0,5 se o nome começa pela 1ª palavra do termo
--              - 0,2 se o termo diz preparo (cozido, grelhado...) e o alimento é cru
-- Também limpa o termo curto (sem palavra de 3+ letras) para não quebrar a regex.
-- Mesma assinatura e mesmas colunas de retorno.
-- =====================================================================
create or replace function public.buscar_alimento(termo text, limite integer default 5)
returns table (
  id            integer,
  nome          text,
  grupo         text,
  energia_kcal  numeric,
  proteina_g    numeric,
  lipideos_g    numeric,
  carboidrato_g numeric,
  fibra_g       numeric,
  marcacoes     jsonb,
  relevancia    real
)
language sql
stable
security invoker
set search_path = ''
as $$
  with frase as (
    select public.normalizar_texto(trim(termo)) as f
  ),
  palavras as (
    select p.w, p.ordem
    from frase, regexp_split_to_table(frase.f, '[^a-z0-9]+') with ordinality as p(w, ordem)
    where length(p.w) >= 3
    union all
    select regexp_replace(frase.f, '[^a-z0-9]+', '', 'g'), 1 from frase
    where length(regexp_replace(frase.f, '[^a-z0-9]+', '', 'g')) >= 2
      and not exists (select 1 from regexp_split_to_table(frase.f, '[^a-z0-9]+') w where length(w) >= 3)
  ),
  total as (
    select greatest(count(*), 1)::float8 as n from public.alimentos_taco
  ),
  pesos as (
    select p.w, p.ordem,
           case when d.df = 0 then 1.0 else 1.0 + ln(t.n / (1 + d.df)) end as peso
    from palavras p
    cross join total t
    cross join lateral (
      select count(*) as df from public.alimentos_taco a where a.nome_normalizado ~ ('\m' || p.w)
    ) d
  ),
  notas as (
    select a.id,
           sum(p.peso * case when a.nome_normalizado ~ ('\m' || p.w) then 1
                             else extensions.word_similarity(p.w, a.nome_normalizado) end) / sum(p.peso) as cobertura,
           bool_or(p.ordem = (select min(ordem) from palavras) and a.nome_normalizado like p.w || '%') as comeca
    from public.alimentos_taco a
    cross join pesos p
    group by a.id
  )
  select a.id, a.nome, a.grupo, a.energia_kcal, a.proteina_g, a.lipideos_g,
         a.carboidrato_g, a.fibra_g, a.marcacoes,
         (n.cobertura + case when n.comeca then 0.5 else 0 end
          - case when exists (select 1 from palavras p where p.w in
                   ('cozido','cozida','grelhado','grelhada','assado','assada','frito','frita',
                    'refogado','refogada','ensopado','ensopada','torrado','torrada'))
                  and a.nome_normalizado ~ '\mcrua?\M' then 0.2 else 0 end)::real as relevancia
  from notas n
  join public.alimentos_taco a on a.id = n.id
  where n.cobertura >= 0.5
  order by relevancia desc, length(a.nome), a.id
  limit least(greatest(coalesce(limite, 5), 1), 50)
$$;
