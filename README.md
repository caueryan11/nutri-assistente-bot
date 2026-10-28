# nutri-assistente

Assistente no WhatsApp para nutricionistas de consultório.

## Banco (Supabase)

```
supabase/migrations/20260928120000_schema.sql   schema, RLS, busca TACO, funções LGPD
supabase/migrations/20260928200000_busca_por_palavras.sql   busca na TACO palavra por palavra
supabase/test/00_stub_supabase.sql              imita auth/roles do Supabase (só para teste local)
supabase/test/01_testes.sql                     testes de RLS, integridade, agenda, busca e LGPD
supabase/test/02_testes_supabase.sql            mesmos testes, para o SQL Editor do Supabase (termina em ROLLBACK)
scripts/testar_banco_local.sh                   sobe Postgres temporário e roda tudo
```

### Tabelas

| Tabela | O que guarda |
| --- | --- |
| `nutricionistas` | clientes; `whatsapp_phone_number_id` roteia a mensagem para a nutri certa |
| `pacientes` | telefone E.164, `consentimento_em` (sem ele o bot não responde), `retorno_previsto_em` |
| `planos_alimentares` | plano em JSON, no máximo um ativo por paciente |
| `regras_clinicas` | texto do prompt; `nutricionista_id` nulo = regra padrão |
| `agendamentos` | consultas; o banco recusa horários sobrepostos da mesma nutri |
| `mensagens` | histórico do WhatsApp; `wa_message_id` único = idempotência do webhook |
| `alimentos_taco` | TACO por 100 g; "Tr", "NA" e "*" ficam NULL e anotados em `marcacoes` |

### Funções

- `buscar_alimento(termo, limite)`: busca tolerante a acento e erro de digitação.
- `minha_nutri_id()`: usada nas políticas de RLS.
- `limpar_mensagens_antigas(dias)`: retenção; só a service_role executa.
- `excluir_paciente(id)`: exclusão a pedido do titular, em cascata; a nutri só apaga paciente dela.

### Aplicar no Supabase

Opção 1: cole o conteúdo da migração no **SQL Editor** do projeto e rode.

Opção 2, com a CLI:

```bash
supabase link --project-ref <ref-do-projeto>
supabase db push
```

As duas migrações já foram aplicadas pelo SQL Editor no projeto `vector`
(`nqsczvwcjqhbdvnxjvjq`, sa-east-1) em 28/09/2026. Como foi pelo editor, o histórico de migrações
da CLI não sabe disso: antes de usar `supabase db push`, rode
`supabase migration repair --status applied 20260928120000 20260928200000`.

## TACO

Fonte: planilha oficial da TACO 4ª edição (NEPA/UNICAMP), salva em `dados/taco_4ed_nepa.xlsx`
(baixada de https://nepa.unicamp.br/publicacoes/tabela-taco-excel/).

```bash
python scripts/importar_taco.py dados/taco_4ed_nepa.xlsx --sql dados/taco.sql --json dados/taco.json
```

- `dados/taco.sql`: upsert dos 597 alimentos numa transação; cole no SQL Editor e rode (pode repetir).
- `dados/taco.json`: mesmos dados, para testar o bot sem banco.
- "Tr", "NA" e "*" viram NULL + `marcacoes`; célula em branco (análise não solicitada) vira NULL sem marcação.
- Valores sem arredondar (o livro arredonda): arredonde só ao exibir.
- Correções conferidas no PDF oficial: alimento 373, piridoxina `,0,02` → 0,02;
  carboidrato negativo por diferença (288, 322, 337, 400) → 0, como no PDF.

Importada no Supabase em 28/09/2026: 597 alimentos, 15 grupos.

### Testar no Supabase

Cole `supabase/test/02_testes_supabase.sql` no SQL Editor e rode. Deve aparecer
"TODOS OS TESTES PASSARAM"; o ROLLBACK no fim desfaz os dados de teste.

### Testar localmente

Precisa de Postgres 15 ou mais novo:

```bash
./scripts/testar_banco_local.sh
```

## Bot do paciente

```
bot/regras_clinicas.md     regras do bot (dona: Ray; entram inteiras no prompt) — RASCUNHO
bot/assistente.py          laço com Claude Haiku 4.5: consultar_taco -> enviar_resposta (responder | encaminhar)
bot/taco.py                busca na TACO (Supabase ou JSON) e cálculo por porção
dados/plano_exemplo.json   plano FICTÍCIO para testes
testes/perguntas.csv       perguntas-teste com o esperado (Ray amplia) — RASCUNHO
testes/rodar_testes.py     roda as perguntas na API real e gera testes/resultados/*.csv (abre no Excel)
testes/test_unidade.py     testes sem internet (API simulada)
```

Regras garantidas no código (não dependem do modelo obedecer ao prompt):
sem `consentimento_em` o bot não responde; até 6 passos, senão encaminha; erro da API encaminha;
resposta final validada; emojis só se o paciente usou (no máximo um); valor que arredonda para zero
aparece como "menos de 0,1 g"; "Tr"/"NA"/"*" nunca viram zero.

```bash
pip install -r requirements.txt
python scripts/importar_taco.py dados/taco_4ed_nepa.xlsx --json dados/taco.json
python -m pytest                       # sem internet
python testes/rodar_testes.py          # API real (~US$ 0,005 por pergunta)
```

## WhatsApp (uazapi)

```
whatsapp/app.py            webhook FastAPI: MODO=eco (teste) ou MODO=bot
whatsapp/uazapi.py         formato da uazapi (receber/enviar); trocar pela Meta = outro adaptador igual
whatsapp/repositorio.py    consultas ao banco do webhook
testes/test_whatsapp.py    testes sem internet
```

- A uazapi não assina o webhook: o app confere o `token` da instância no corpo e exige
  `WEBHOOK_SEGREDO` no caminho da URL (`/webhook/uazapi/<segredo>`).
- `nutricionistas.whatsapp_phone_number_id` guarda o **nome da instância** na uazapi.
- Ignora mensagens enviadas pela própria conta, pela API e de grupos; eventos repetidos são descartados.
- MODO=bot: só responde paciente cadastrado **com consentimento**; áudio/foto e paciente sem plano
  são encaminhados; todo encaminhamento avisa a nutri no `telefone_aviso`.
- Número que não é paciente vai para o atendimento comercial (ver abaixo); o próprio `telefone_aviso`
  da nutri é ignorado.

```bash
uvicorn whatsapp.app:criar_app_do_ambiente --factory --port 8000 --no-access-log
```

## Hospedagem (Railway)

`railway.json` define o start (`uvicorn ... --port $PORT --no-access-log`), healthcheck em `/saude`
e região US East (o Railway não tem região no Brasil; os dados ficam no Supabase em São Paulo).
**1 réplica só**: o descarte de eventos repetidos em memória e o processamento em segundo plano
assumem uma instância. Python 3.13 (`.python-version`).

Variáveis no Railway (Variables → Raw Editor): `ANTHROPIC_API_KEY`, `MODELO_BOT`, `DATABASE_URL`,
`UAZAPI_URL`, `UAZAPI_TOKEN`, `WEBHOOK_SEGREDO`, `MODO`.
Depois do deploy, aponte o webhook da uazapi para `https://<dominio-railway>/webhook/uazapi/<WEBHOOK_SEGREDO>`.

## Agenda (Google Calendar)

```
agenda/google_calendar.py   cliente da Calendar API v3 com conta de serviço (freeBusy, criar/cancelar evento)
agenda/horarios.py          horário de atendimento menos ocupados (função pura, testada sem Google)
agenda/servico.py           AgendaNutri: ver_horarios_livres / marcar_consulta para o bot
testes/test_agenda.py       testes sem internet
supabase/migrations/20260929100000_agenda_google.sql   google_calendar_id, agenda_config, google_event_id
```

- Projeto Google Cloud `nutri-assistente`, Calendar API ativada, conta de serviço
  `seu-servico@seu-projeto.iam.gserviceaccount.com` sem papéis no projeto.
- **Conectar a agenda de uma nutri:** ela abre o Google Agenda → configurações da agenda →
  "Compartilhar com pessoas específicas" → adiciona o e-mail da conta de serviço com
  "Fazer alterações nos eventos". Depois grave o ID da agenda em `nutricionistas.google_calendar_id`
  e ajuste `agenda_config` (horário de atendimento, duração, antecedência, janela).
- O Google é a fonte da verdade do que está ocupado (inclui o que a nutri marca por fora).
  `agendamentos` guarda o que o bot marcou; o índice de exclusão impede horário duplicado.
- O bot só marca depois que o paciente escolhe um horário; toda consulta marcada avisa a nutri.
  Remarcar e cancelar: o bot lista as consultas do paciente (`minhas_consultas`), remarca marcando o novo horário
  antes de cancelar o antigo, e cancela só com confirmação; a nutri é avisada.
- **Consulta marcada à mão pela nutri no Google:** o bot enxerga se o evento tiver o **celular do paciente** no título
  ou na descrição, em qualquer formato ("(47) 90000-0024", "+55 47 900000024", com ou sem o 9; precisa de DDD).
  Olha os próximos 60 dias. Sem telefone no evento, o bot não sabe de quem é e encaminha para a nutri.
- Conta de serviço não convida participantes: o paciente é avisado pelo WhatsApp.
- Railway precisa de `GOOGLE_SERVICE_ACCOUNT_B64`.

## Consentimento (LGPD)

```
whatsapp/consentimento.py                              textos, versão do termo, o que conta como aceite
supabase/migrations/20260929120000_consentimento.sql   versao_termo (era consentimento_versao), status_consentimento + gatilho
```

- Paciente cadastrado **sem** `consentimento_em`: qualquer mensagem recebe o pedido de aceite com o link
  `TERMO_URL`. Não chama a IA e **não grava** a mensagem (pode ter dado de saúde).
- Resposta de aceite (`Aceito`, `Sim`, `Concordo`, `Eu aceito`... ignorando maiúsculas, acento, pontuação e emoji;
  com `?` não conta): grava `consentimento_em = now()`, `versao_termo = 'v1.0'`, `status_consentimento = 'aceito'`,
  guarda o "Aceito" e a confirmação em `mensagens` como prova, e confirma ao paciente.
- `status_consentimento` (pendente | aceito | revogado) é mantido coerente com `consentimento_em` por gatilho.
- **Pendente:** o termo em si (texto revisado por advogado, publicado num link) e os textos das mensagens (Ray).

## Área da nutri (cadastros sem SQL)

```
whatsapp/painel.py + templates/            rotas, validação e telas (Tailwind por CDN)
whatsapp/area_repo.py                      banco; TODA consulta filtra pela nutri do link
bot/plano_texto.py                         plano em texto simples <-> JSON do bot
supabase/migrations/20260930090000_area_nutri.sql   salvar_plano_ativo (troca o plano numa transação)
testes/test_area_nutri.py, test_area_banco.py
```

Pelo mesmo link do painel (`/painel/<token>`), com abas:
- **Pacientes:** lista (termo, plano, anamnese, próxima consulta), cadastro (nome + WhatsApp com DDD),
  edição, desativar, e **excluir com todos os dados** (pedido do titular; exige digitar o nome completo).
- **Ficha do paciente:** anamnese (alergias, intolerâncias, restrições, não gosta, observações) e **plano
  alimentar em texto simples**: `# Almoço 12:30` abre a refeição; cada item `alimento | quantidade |
  substituições; separadas; por ponto e vírgula`; `Observações:` no fim. "Conferir" mostra como o assistente vai
  ler o plano, aponta itens sem gramas (sem cálculo pela TACO) e conflitos com a anamnese. "Salvar" troca o plano
  ativo e guarda o anterior no histórico.
- **Consultório:** valor, convênios, reembolso, como é a consulta, WhatsApp de avisos e horário de atendimento
  por dia (ex.: `08:00-12:00, 14:00-18:00`), duração, antecedência e janela da agenda.

Sem login e sem cookies: o link assinado expira em 8 dias (a nutri pede outro mandando "painel"). Paciente de
outra nutri dá 404 mesmo trocando o id na URL. Cadastrar paciente não manda mensagem: na primeira conversa ele
recebe o pedido de aceite do termo. Conectar o Google Agenda ainda é feito pelo suporte.

## Níveis de consulta e campanha de reativação

```
bot/acompanhamento.py        aviso de fim do acompanhamento (convite ao retorno), 10:30
bot/reativacao.py            lista colada, público, fila de envio gradual, métricas da campanha
supabase/migrations/20260930170000_niveis_acompanhamento.sql, 20260930180000_reativacao.sql
testes/test_niveis_reativacao.py, test_niveis_reativacao_banco.py
```

**Dois níveis** (aba Consultório → "Níveis de consulta"; padrão "todos", que mantém o comportamento antigo):
- **Consulta simples:** agenda, lembretes e confirmação. Dúvida de alimentação vai para a nutri com o convite ao
  acompanhamento. Nesse modo o modelo **não recebe o plano** nem as ferramentas de TACO, e sugerir alimento é
  barrado no código.
- **Consulta com acompanhamento:** dúvidas 24h, check-in semanal e alertas. Vale X dias depois da consulta
  (`acompanhamento_dias`, padrão 45). Na ficha: "Ligar/Renovar por X dias", "Sem data (assinatura)" ou "Desligar".
- 3 a 5 dias antes de vencer, quem não tem retorno marcado recebe o convite para marcar (um aviso por ciclo).
- Paciente sem plano (ex.: voltou pela reativação) também fica em modo agenda: consegue marcar consulta.

**Campanha de reativação** (aba Reativação):
1. A nutri cola a lista (nome; WhatsApp; última consulta), confere e importa.
2. Escolhe há quanto tempo sem consulta (45 a 365 dias); ficam fora quem tem consulta marcada, quem pediu PARAR,
   quem está em acompanhamento e quem recebeu campanha nos últimos 90 dias. Precisa confirmar que são pacientes dela.
3. Envio gradual: 3 por minuto (~180/h), seg a sáb das 9h às 20h no fuso dela; falha volta para a fila (3 tentativas).
   Pausar/retomar. A mensagem sempre termina com a opção de PARAR.
4. Quem responde recebe o pedido do termo contextual e, depois do "Aceito", o assistente oferece horários.
5. Resultados: enviadas, responderam (só o horário, sem conteúdo, antes do aceite), agendaram (até 30 dias) e
   receita estimada (agendaram × valor da consulta).

**LGPD, aceite:** "Aceito"/"Sim" só vale depois que o pedido do termo foi mostrado (`pacientes.termo_pedido_em`) ou
para quem revogou e quer voltar. Um "SIM" à campanha nunca vira aceite de um termo que a pessoa não viu.

## Acompanhamento mensal: check-in semanal

```
bot/checkin.py                                 pergunta, leitura da resposta (Claude Haiku), alertas, envio semanal
supabase/migrations/20260930150000_checkin.sql pacientes.acompanhamento_desde, nutricionistas.checkin_config, checkins
testes/test_checkin.py, test_checkin_banco.py
```

Produto que a nutri vende ao paciente (acompanhamento entre consultas). No painel ela liga o acompanhamento por
paciente e escolhe dia e hora (aba Consultório; padrão domingo 18:00).
- Toda semana o paciente recebe 4 perguntas: adesão (0 a 10), fome, intestino e peso (opcional). Responde em texto
  livre; o Haiku extrai os campos (ferramenta com schema) e o código confere os valores.
- Faltou algo essencial: pede uma vez. Depois agradece (sem orientar). Mensagem que não é resposta (dúvida, remarcar)
  segue para o bot normal; resposta + dúvida: grava a resposta e o bot responde a dúvida.
- **Alerta à nutri na hora:** sintoma/mal-estar citado ou variação de peso de 2 kg ou mais na semana. O paciente
  recebe a orientação de ligar 192 se for urgente.
- Painel: a ficha mostra a evolução (gráfico de adesão e peso, tabela semanal com alertas); a lista de pacientes
  mostra quem está no acompanhamento e a última adesão.
- Uma linha por paciente por semana (não duplica em deploy); envio a cada 15 min dentro de 3 h após o horário.
  Só para quem aceitou o termo. Os check-ins são evolução do paciente: não entram na retenção de mensagens e são
  apagados junto com o paciente.
- As mensagens do check-in não contam como "dúvidas respondidas" nas métricas.

## Importar o plano (PDF ou foto)

```
bot/importar_plano.py     Claude lê o PDF/foto e transcreve para o formato do sistema (MODELO_IMPORTACAO, padrão Sonnet 5.5)
whatsapp/importacao.py    legenda (nome + WhatsApp), espera entre arquivo e paciente, resumo para a nutri
supabase/migrations/20260930120000_importar_plano.sql   rascunho, origem e avisos em planos_alimentares
testes/test_importar_plano.py, test_importar_banco.py
```

A nutri não redigita o plano: usa o mesmo arquivo que já manda ao paciente.
- **Pelo painel:** ficha do paciente → "Importar do PDF ou foto do plano".
- **Pelo WhatsApp:** do número de avisos, ela manda o PDF/foto para o assistente com o nome e o WhatsApp do
  paciente na legenda (ex.: `Maria Silva 51 99999-0000`) ou numa mensagem logo antes/depois (espera 30 min).
  Se o paciente não existe, é cadastrado. A resposta traz o resumo, os pontos para conferir e o link da ficha.
- O resultado é sempre **rascunho**: o bot continua usando o plano ativo até a nutri conferir e clicar em
  "Salvar plano". Pontos para conferir: o que a IA não entendeu, itens sem gramas, nome do documento diferente do
  cadastro. Alergias/restrições citadas no documento são **somadas** à anamnese (restringir a mais é seguro).
- Aceita PDF, JPG, PNG e WEBP até 10 MB (conferido pelo conteúdo). Word: salvar como PDF.
- Custo medido: ~12 s e ~5 mil tokens por plano de 5 refeições. O arquivo é processado pela Anthropic (EUA),
  como as conversas: o termo e o advogado precisam cobrir isso (a importação pode acontecer antes do aceite do
  paciente; base legal provável: tutela da saúde pelo profissional, art. 11, II, "f" — confirmar).
- A uazapi guarda o arquivo recebido e expõe uma URL pública por 2 dias (comportamento dela): mais um ponto para o termo.

## LGPD na operação e monitor do WhatsApp

```
whatsapp/consentimento.py + templates/termo.html   textos, PARAR/Aceito, página /termo (rascunho para o advogado)
whatsapp/monitor.py                                 confere a uazapi a cada 10 min e alerta quando cai/volta
supabase/migrations/20260929220000_lgpd_operacao.sql  pacientes.consentimento_revogado_em
testes/test_lgpd_operacao.py, test_lgpd_banco.py
```

- **Revogação:** o paciente manda só "PARAR" (ou "sair", "stop", "revogar"...). O bot grava `status_consentimento =
  'revogado'`, `consentimento_em = null` e `consentimento_revogado_em`, confirma, avisa a nutri e para de responder
  e de mandar lembretes/cobranças. Nada da mensagem é gravado. "Aceito" reativa. Frases como "posso sair da dieta?"
  não contam. Lead que manda PARAR vira `desistiu` (volta para a triagem se escrever de novo por conta própria).
- **Página do termo:** `/termo`, servida pelo app (versão, prazo de retenção, direitos, como revogar). O pedido de
  aceite usa `TERMO_URL` se definida; senão `<URL pública>/termo`. Texto provisório: o advogado substitui
  `whatsapp/templates/termo.html` (campos `[a preencher]`: razão social, CNPJ, encarregado).
- **Retenção:** todo dia às 03:30 apaga mensagens de pacientes com mais de `RETENCAO_DIAS` (padrão 180) e de leads
  com mais de `RETENCAO_LEADS_DIAS` (padrão 90), além dos leads que não viraram paciente e estão parados há mais que
  isso. Prazos mínimos 30/7 dias (o app não sobe com menos). Prazos a confirmar com a Ray e o advogado.
- **Monitor do WhatsApp:** `/saude` mostra `whatsapp: {conectado, estado, desde}`. Com `ALERTA_URL` definida, avisa
  quando cai e quando volta (repete a cada 6 h enquanto fora). Mais simples: instale o app **ntfy** no celular,
  assine um tópico com nome difícil de adivinhar e use `ALERTA_URL=https://ntfy.sh/<tópico>`. Discord e Slack
  (webhook) também funcionam. O alerta não leva dado de paciente.

## Anamnese (o que o bot nunca sugere)

```
bot/anamnese.py                                  conferência, termos implícitos (lactose -> leite, queijo...), texto do prompt
supabase/migrations/20260929200000_anamnese.sql  pacientes.anamnese (jsonb, formato conferido pelo banco)
testes/test_anamnese.py                          conferência, laço do bot e webhook
```

- `pacientes.anamnese`: `{"alergias": [], "intolerancias": [], "nao_gosta": [], "restricoes": [], "observacoes": ""}`.
- Três barreiras: (1) a anamnese vai no prompt com as regras; (2) itens do **plano** que conflitam são apontados
  e, se o paciente perguntar por eles, o bot encaminha para a nutri revisar o plano; (3) **em código**: o bot
  lista em `alimentos_sugeridos` o que sugere, e qualquer conflito é recusado e devolvido ao modelo para
  corrigir (insistir acaba em encaminhamento). Citar um alergênico na mensagem exige confirmação do modelo
  de que é só um aviso.
- Alergia, intolerância e restrição nunca são liberadas. "Não gosta" é preferência: o bot nunca sugere, mas
  libera se o próprio paciente pedir.
- `TERMOS_IMPLICITOS` e `EXCECOES` (ex.: "leite sem lactose" não conflita) são rascunho: a Ray revisa.
- Sem anamnese cadastrada o bot não supõe nada; alergia nova contada pelo paciente é encaminhada.

```sql
update public.pacientes set anamnese = '{"alergias": ["amendoim"], "intolerancias": ["lactose"],
  "nao_gosta": ["beterraba"], "restricoes": [], "observacoes": "Refluxo: evitar refeição volumosa à noite."}'
where id = '<id do paciente>';
```

## Atendimento comercial (quem ainda não é paciente)

```
bot/comercial.py                                   prompt, ferramentas e laço (Claude Haiku 4.5)
supabase/migrations/20260929160000_comercial.sql   dados comerciais da nutri, leads, lead_mensagens, agendamentos.convenio
testes/test_comercial.py                           cenários com Claude, agenda e webhook simulados
testes/test_comercial_banco.py                     SQL dos leads no Postgres real (RODAR_TESTES_BANCO=1), desfeito no fim
```

- `COMERCIAL=off` desliga (número desconhecido fica sem resposta). Atenção: ligado, o bot responde **qualquer**
  número que escrever para o WhatsApp conectado (inclusive contatos pessoais, se for o número pessoal da nutri).
- Número desconhecido vira um lead (`leads`, um por nutri + telefone). O bot acolhe, entende o objetivo,
  responde convênio/reembolso, valor e como é a consulta, e marca a primeira consulta. **Não** dá orientação
  nutricional e não pede dados de saúde.
- Convênio: a ferramenta `consultar_convenio` compara com `convenios_aceitos` no código (ignora caixa, acento e
  complemento, ex.: "UNIMED Porto Alegre"). Nutri só particular ou convênio fora da lista: particular + recibo
  para reembolso (`politica_reembolso`).
- `marcar_primeira_consulta` cria o evento "Primeira consulta – Nome" no Google e, numa só instrução SQL,
  cria o paciente (sem consentimento), grava o agendamento (`tipo = 'primeira'`, `convenio`) e marca o lead
  `convertido`. Em seguida o paciente recebe o termo; o fluxo clínico começa depois do "Aceito".
  A nutri recebe aviso de novo paciente. Horário ocupado no banco desfaz o evento.
- Status do lead: `em_triagem` → `aguardando_escolha_horario` (recebeu horários) → `convertido`.
  `desistiu` fica para a nutri/painel.
- A conversa do lead fica em `lead_mensagens` (o bot precisa do histórico); apague as antigas com
  `select public.limpar_lead_mensagens_antigas(90)`.
- Resumo das 8h: novos contatos nas últimas 24 h e quantos marcaram a primeira consulta.
- Sem os dados comerciais preenchidos, o bot não cita valor e encaminha quem insistir. Preencher por nutri:

```sql
update public.nutricionistas set
  atende_convenio = true, convenios_aceitos = '{Unimed,"Bradesco Saúde"}', valor_consulta = 250.00,
  politica_reembolso = 'Emitimos recibo com CRN para você pedir reembolso ao seu plano.',
  apresentacao_servico = 'Consulta de 1 hora com avaliação, plano individual e acompanhamento pelo WhatsApp.'
where id = '<id da nutri>';
```

## Métricas e painel da nutri

```
bot/metricas.py                                  agregações (7/30 dias), relatório semanal, link assinado do painel
whatsapp/painel.py + whatsapp/templates/painel.html   /painel/<token>?dias=7|30 (Tailwind + Chart.js por CDN)
supabase/migrations/20260929180000_metricas.sql  relatorio_semanal_enviado_em + índices
testes/test_metricas.py                          texto do relatório, token, rota e comando "painel"
testes/test_metricas_banco.py                    agregações no Postgres real (RODAR_TESTES_BANCO=1), desfeitas no fim
```

| Métrica | Como é contada |
| --- | --- |
| Dúvidas resolvidas | mensagens do bot com `acao = 'responder'` (não conta confirmação do termo nem lembretes/cobranças) |
| Fora do horário | dessas, seg–sex antes das 8h ou depois das 18h, sábado e domingo (fuso da nutri) |
| Casos encaminhados | mensagens com `acao = 'encaminhar'` |
| Novos interessados | `leads` criados no período; conversão = quantos deles estão `convertido` |
| Primeiras consultas / retornos | `agendamentos` marcados pelo bot no período (`tipo`), confirmados ou realizados |
| Retornos resgatados | retorno marcado até 30 dias depois de uma cobrança de retorno (paciente 35+ dias sem consulta) |
| Receita estimada | (primeiras + retornos marcados pelo bot) × `valor_consulta` |

- **Relatório semanal**: segunda 08:30 para o `telefone_aviso` da nutri, com o link do painel. Reserva no banco
  como as outras rotinas (não duplica em deploy; falha desfaz a reserva).
- **Painel**: sem login. O link é um token assinado com `PAINEL_SEGREDO` (HMAC-SHA256) que expira em 8 dias.
  A página não é cacheada nem indexada, não manda referrer e tem CSP. Mostra só o primeiro nome dos pacientes
  e o motivo do encaminhamento. Trocar `PAINEL_SEGREDO` invalida todos os links.
- A nutri pede um link novo mandando **painel** do `telefone_aviso` para o número do assistente.
- `/saude` mostra `"painel": true` quando `PAINEL_SEGREDO` e o endereço público estão definidos.

## Avisos para a nutri e rascunho de resposta

Todos os avisos que vão para o `telefone_aviso` saem de `bot/avisos.py`, no mesmo formato: 1ª linha com cor + o que
é + quem (🔴 urgente, 🟡 responder, 📅/🆕/⛔ informativo), uma frase do que aconteceu e o que fazer, com link
`wa.me/...` que abre a conversa com o paciente. O resumo das 8h começa pelo que precisa dela (urgentes no topo).

**Rascunho de resposta** (`bot/rascunho.py`, `RASCUNHO=on` por padrão): quando o bot encaminha uma dúvida de texto, o
assistente pesquisa (busca da Claude limitada a `DOMINIOS`: PubMed, SciELO, diretrizes, Ministério da Saúde...;
lista provisória, a Ray revisa), escreve uma sugestão respeitando plano e anamnese e manda junto com o aviso, com
fontes e o que conferir. A busca não leva dado do paciente; fonte que não veio da busca é descartada.
A nutri responde no WhatsApp de avisos:

| Ela responde | Acontece |
|---|---|
| `sim` (ou `pode`, `ok`, `enviar`, `1`) | a sugestão vai para o paciente como `*Nome da nutri:* texto` |
| `não` (ou `descartar`, `2`) | descarta; nada vai para o paciente |
| texto **citando** o aviso | a versão dela vai direto |
| texto sem citar | o bot mostra o texto e pede `sim` para confirmar (evita mandar conversa solta) |

Com mais de um aviso pendente, texto sem citar pede para citar. Sugestão com mais de 48 h não é enviada; paciente
que mandou PARAR não recebe. Tabela `rascunhos` (migração `20261001180000_rascunhos.sql`): uma linha por aviso,
status `pendente/enviado/editado/descartado`, apagada com o paciente e pela retenção. Custo: ~20 s e ~R$ 1 por
rascunho (Sonnet + buscas). `MODELO_RASCUNHO` troca o modelo; `RASCUNHO=off` desliga (avisos sem sugestão).

## Rotinas automáticas

```
bot/rotinas.py                                   consultas, textos, as 3 rotinas e o agendador (APScheduler)
supabase/migrations/20260929140000_rotinas.sql   ultima_consulta_em, ultimo_aviso_retorno_em, resumo_enviado_em
testes/test_rotinas.py                           uazapi e banco simulados
testes/test_rotinas_banco.py                     consultas SQL no Postgres real (RODAR_TESTES_BANCO=1), desfeitas no fim
```

Rodam dentro do webhook no Railway (sem n8n), em America/Sao_Paulo, só no `MODO=bot` (`ROTINAS=off` desliga).
O `/saude` mostra a próxima execução de cada uma.

| Rotina | Quando | Quem recebe |
| --- | --- | --- |
| Lembrete da consulta | a cada 15 min | consulta `confirmado` que começa entre 23h30 e 24h30 a partir de agora, ainda sem lembrete |
| Cobrança de retorno | todo dia 10:00 | última consulta há 35–60 dias (`ultima_consulta_em` ou consulta do bot), sem consulta futura, sem cobrança nos últimos 30 dias |
| Relatório semanal | segunda 08:30 | nutri com `telefone_aviso`: números da semana + link do painel (ver Métricas) |
| Resumo para a nutri | seg–sex 08:00 | nutri com `telefone_aviso`: respondidas e encaminhadas nas últimas 24 h, consultas de hoje (inclui as que ela marcou à mão no Google), novos contatos e quantos marcaram, cobranças de retorno |

- Só manda mensagem para paciente que aceitou o termo.
- Cada rotina reserva as linhas no banco antes de enviar (`UPDATE ... RETURNING`): duas cópias do app
  (ex.: durante um deploy) não mandam mensagem duplicada. Se o envio falhar, a reserva é desfeita.
- Lembrete e cobrança ficam em `mensagens` (histórico): quando o paciente responde "Sim", o bot entende.
- `lembrete_24h_enviado` é calculada a partir de `lembrete_enviado_em` (que já existia).
- Pacientes de antes do sistema: preencha `ultima_consulta_em` para entrarem na cobrança de retorno.
