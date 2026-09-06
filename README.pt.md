# multi-model-agentflow

[English](README.md) | [简体中文](README.zh-CN.md) | [Español](README.es.md) | [Português](README.pt.md)

`multi-model-agentflow` é um plano de controle independente de plataforma e domínio, sensível a risco, para coordenar múltiplos modelos de IA em planejamento, autorização, execução, verificação, revisão, rastreamento de custos, verificações de privacidade e recuperação.

O projeto parte de uma ideia central: modelos devem gerar, implementar e revisar dentro de limites explícitos, enquanto software determinístico controla autorização, estado, orçamentos, barreiras de segurança, evidências e recuperação.

## Por Que Existe

Fluxos complexos com agentes podem falhar de formas difíceis de auditar:

- um plano muda, mas o limite de autorização não muda junto;
- um modelo escreve, testa, revisa e declara o próprio trabalho como concluído;
- modelos remotos recebem mais contexto ou dados sensíveis do que o necessário;
- chamadas pagas são repetidas depois de falhas incertas;
- tarefas longas são interrompidas sem um estado claro de recuperação;
- edições humanas invalidam alguns resultados, mas não necessariamente todos;
- o estado fica espalhado entre chat, terminais, logs, saídas de modelos e diffs do Git.

`multi-model-agentflow` tenta tornar esses fluxos explícitos, inspecionáveis e recuperáveis.

## Estado Atual

O MVP local foi implementado e validado com testes automatizados. Ele cobre o ciclo principal:

```text
plan -> authorize -> implement -> self-test -> deterministic gates
-> independent review -> revise/rereview -> approve
```

Ele também cobre pausa segura, congelamento imediato, tomada de controle humana, execução recuperável, rastreamento idempotente de chamadas, evidência de custos, verificações de limite de arquivos e evidência de revisão.

Os roles remotos baseados em OpenCode estão incluídos no escopo atual:

- Reviewer remoto: somente leitura, apenas packet, sem acesso ao workspace do repositório;
- Worker remoto: role de implementation/revision, somente quando explicitamente permitido pelo contrato da tarefa;
- a execução do Worker remoto acontece dentro de um staging sandbox mínimo;
- as entradas do Worker remoto são declaradas por `input_artifacts` e verificadas com SHA-256;
- as saídas do Worker remoto são sincronizadas de volta apenas para `allowed_files`, com limites tudo-ou-nada;
- chamadas remotas são restringidas por hash do plano, snapshot de autorização, orçamento, política de privacidade, escopo de arquivos, permissões de role e política de rede.

O desenvolvimento e a validação padrão usam test doubles sem custo, caminhos de modelos locais, processos simulados e executáveis stub do OpenCode. Chamadas reais a providers pagos e smoke tests remotos reais exigem um plano separado, hash exibido, aprovação explícita e autorização.

## Garantias Principais

- **Ativação explícita**: a descoberta automática de Skills pode sugerir AgentFlow, mas isso não autoriza iniciar modelos.
- **Autorização por hash do plano**: a autorização é vinculada ao JSON canônico do plano e a um hash SHA-256 do conteúdo.
- **Invalidação por mudança de escopo**: mudar modelos, arquivos, orçamento, exposição de dados, permissões ou efeitos colaterais exige nova autorização.
- **Risco em dois eixos**: importância de negócio usa B0-B3; segurança operacional usa S0-S3.
- **Barreiras de privacidade**: dados são classificados como D0-D3; dados D3 e segredos nunca vão para remoto.
- **Separação de roles**: implementação, self-test e revisão não podem virar um único caminho de autoaprovação por modelo.
- **Revisão independente**: Reviewers usam contexto novo e somente leitura; trabalho importante ou crítico exige revisão entre famílias de modelos.
- **Isolamento do Reviewer remoto**: Reviewers remotos recebem apenas um Review Packet mínimo, não o workspace do repositório.
- **Sandbox do Worker remoto**: Workers remotos executam em um staging sandbox mínimo com rede negada por padrão.
- **Snapshots de entrada**: entradas do Worker remoto são arquivos relativos ao projeto verificados por SHA-256.
- **Sincronização atômica de saídas**: saídas do Worker remoto são copiadas de volta apenas dentro de arquivos autorizados e somente quando as verificações passam.
- **Política de aceitação de revisão**: tarefas podem usar `block_p0_p1` ou `zero_findings`.
- **Honestidade de custos**: chamadas registram tokens, duração, custo, estado de custo indisponível e chaves de idempotência.
- **Estado determinístico**: eventos, projeções, chamadas, testes, revisões e evidências vivem em uma única fonte de estado SQLite.
- **Checkpoints de supervisor**: eventos deterministas de wake podem criar supervisor checkpoints limitados para supervisão com baixo uso de tokens.
- **Pausa e recuperação**: execuções podem ser pausadas, congeladas, assumidas e retomadas sem repetir chamadas pagas concluídas.
- **Plano de controle opcional**: AgentFlow pode ser parado; Git worktrees, diffs e evidências continuam disponíveis para trabalho manual.

## Início Rápido

Executar testes:

```bash
pytest
```

Mostrar o plano atual:

```bash
agentflow plan show
```

Autorizar um hash de plano exibido:

```bash
agentflow plan authorize --hash <sha256>
```

Iniciar uma execução:

```bash
agentflow start <plan-id>
```

Inspecionar status:

```bash
agentflow status <run-id>
```

Acompanhar status e logs:

```bash
agentflow status <run-id> --watch
agentflow logs <run-id> --follow
```

Inspecionar evidência de custos:

```bash
agentflow cost <run-id>
```

Ler ou registrar checkpoints de supervisor:

```bash
agentflow supervisor-next <run-id>
agentflow supervisor-record <run-id> <checkpoint-id> --decision '<json>'
```

Pausar, congelar, assumir controle, resolver, retomar ou cancelar:

```bash
agentflow pause <run-id>
agentflow pause <run-id> --immediate
agentflow takeover <run-id>
agentflow handoff <run-id>
agentflow resolve-call <run-id> <call-id>
agentflow resume <run-id>
agentflow cancel <run-id>
```

Executar contra uma raiz de projeto explícita:

```bash
agentflow --project <root> status <run-id>
```

## Fluxo Típico

1. Criar ou gerar `.agentflow/plan.json`.
2. Executar `agentflow plan show`.
3. Revisar o plano normalizado, escopo efetivo, modelos, arquivos, orçamento, política de privacidade, modo, expiração e SHA-256.
4. Aprovar explicitamente o hash exibido.
5. Executar `agentflow plan authorize --hash <sha256>`.
6. Executar `agentflow start <plan-id>`.
7. Observar com `status`, `logs`, `cost` e `supervisor-next`.
8. Usar pausa segura, congelamento, tomada de controle ou retomada quando for necessária intervenção humana.
9. Aprovar a conclusão somente depois que as barreiras determinísticas e a revisão independente passarem.

## Roles Remotos

### Reviewer

Um Reviewer remoto pode executar apenas `review` ou `rereview`.

Ele é somente leitura por padrão, não recebe o workspace do repositório e recebe apenas o Review Packet mínimo gerado pelo AgentFlow. O Review Packet está sujeito a barreiras de privacidade, orçamento, autorização e independência.

Um Reviewer remoto não deve editar arquivos, escrever em disco, executar comandos shell, acessar diretórios externos, navegar na web, invocar Skills, iniciar tarefas/subagentes ou solicitar upgrades interativos de permissão.

A saída do Reviewer deve ser um único objeto JSON válido pelo protocolo. Prosa não JSON, JSON em fences, campos ausentes, tipos inválidos, severidades inválidas ou aprovação contraditória não podem aprovar uma tarefa.

### Worker

Um Worker remoto pode executar `implementation` ou `revision` somente quando o contrato da tarefa define explicitamente:

```yaml
allow_remote_implementation: true
```

O Worker executa dentro de um staging sandbox mínimo contendo apenas:

- `input_artifacts` somente leitura verificados por hash;
- `allowed_files` autorizados;
- um briefing de tarefa gerado.

O acesso de rede é negado por padrão. Atualmente, host allowlists não podem ser expressas com segurança pela camada de permissões do OpenCode, então o modo allowlist falha fechado.

As saídas do Worker são sincronizadas de volta apenas para `allowed_files`. A sincronização usa verificações de baseline e manifest e segue um limite tudo-ou-nada. Mutação de entrada, saída fora do escopo, conflito no destino, manifest danificado ou rollback com falha bloqueiam a integração.

## Contrato de Tarefa

Um contrato de tarefa descreve o limite de execução antes que qualquer modelo execute:

```yaml
task_id:
objective:
risk_level:
allowed_files:
forbidden_actions:
acceptance_criteria:
data_sensitivity:
implementation_model:
review_model:
fallback_model:
max_remote_cost:
max_retry_count:
escalation_conditions:
expected_outputs:
implementation_max_steps:
implementation_timeout_seconds:
implementation_max_continuations:
allow_remote_implementation:
remote_worker_network_mode:
remote_worker_allowed_hosts:
remote_worker_max_steps:
remote_worker_timeout_seconds:
input_artifacts:
review_acceptance_policy:
```

`risk_level` contém tanto importância de negócio quanto segurança operacional. O contrato da tarefa faz parte do JSON canônico do plano e do hash de autorização.

## Modos de Operação

- **Managed**: o plano aprovado pode executar dentro de seu escopo autorizado sem confirmações adicionais.
- **Supervised**: cada chamada de implementation, review, revision e rereview exige confirmação.
- **Adaptive**: confirmações são determinadas apenas pelos limiares B/S estáticos do plano e pelos marcadores de nós críticos.

O modo Adaptive não faz roteamento baseado em aprendizado nem muda a estratégia silenciosamente.

## Estado e Recuperação

AgentFlow armazena o estado da execução em um banco SQLite local. Eventos e projeções de estado atual são atualizados na mesma transação, então a recuperação consegue distinguir trabalho concluído, trabalho com falha, chamadas desconhecidas, revisão pendente e estados de intervenção humana.

Se uma chamada paga ou remota tiver resultado incerto, AgentFlow registra como `UNKNOWN` e pausa para reconciliação. Ele não deve tentar novamente de forma especulativa.

Se uma chamada local de implementation ou revision atinge um limite de passos conhecido, ela é registrada como uma falha incompleta conhecida. Ela pode continuar apenas como um segmento autorizado da mesma sessão e apenas dentro de `implementation_max_continuations`. Reviewers remotos e Workers remotos não continuam dessa forma.

## Mapa de Documentação

- `docs/requirements.md`: requisitos normativos do produto com IDs estáveis e evidência de aceitação.
- `docs/mvp.md`: escopo do MVP, não objetivos, cenários de aceitação e estado de validação.
- `docs/architecture-decisions.md`: decisões de arquitetura aceitas, provisórias e pendentes.
- `task_plan.md`: marcos de implementação e plano de trabalho atual.
- `progress.md`: notas de progresso e registros de validação.
- `skills/multi-model-agentflow/SKILL.md`: ponto de entrada do Codex Skill.
- `AGENTS.md`: regras de colaboração do repositório e limites de segurança.

## Limites de Segurança

Por padrão, este projeto não:

- baixa nem instala modelos;
- registra contas de providers;
- compra créditos;
- coleta nem armazena segredos de providers;
- chama modelos fora da autorização do plano;
- envia dados D3, segredos, tokens ou dados privados de usuário para modelos remotos;
- trata modelos OpenCode configured/discoverable como modelos callable-verified;
- permite que Reviewers remotos editem arquivos, executem shells, naveguem ou leiam o workspace do repositório;
- permite que Workers remotos usem rede, shell, diretórios externos, Skills, tarefas, subagentes ou upgrades interativos;
- usa prosa de modelos como substituto para testes, registros de revisão, evidência de banco de dados ou diffs do Git;
- usa modelos de IA para implementar a máquina de estados, locks, verificações de orçamento, lógica de espera ou barreiras determinísticas;
- gera custo de API sem autorização explícita.

## Notas de Desenvolvimento

O núcleo genérico deve permanecer independente de provedor de modelo, agent engine, plataforma e domínio de aplicação. Políticas específicas de domínio pertencem à configuração do projeto, documentação do projeto ou estratégia fornecida pelo chamador.

Novos requisitos devem ser adicionados a `docs/requirements.md` com IDs estáveis, comportamento observável e evidência de aceitação. Mudanças no escopo do MVP devem ser refletidas em `docs/mvp.md`. Decisões de arquitetura e perguntas em aberto devem ser registradas em `docs/architecture-decisions.md`.

Mudanças de implementação devem preservar evidências de testes, revisão, custo, autorização, estado e recuperação. Test doubles devem ser claramente rotulados e não devem ser apresentados como execuções reais de modelos.

## Aviso Legal

Este projeto é software experimental para fluxos de pesquisa e desenvolvimento. Ele é fornecido no estado em que se encontra, sem garantia de qualquer tipo.

Ele não oferece aconselhamento jurídico, financeiro, de segurança, conformidade ou outro aconselhamento profissional. Usuários são responsáveis por revisar planos, permissões, saídas de modelos, custos, limites de privacidade, comportamento de providers e efeitos posteriores antes de usá-lo em projetos reais ou com providers reais de modelos.

Chamadas reais a providers remotos, uso de modelos pagos, processamento de dados sensíveis e implantação em produção exigem revisão separada e autorização explícita.

## Licença

Este projeto está licenciado sob a Apache License 2.0. Consulte `LICENSE` para detalhes.
