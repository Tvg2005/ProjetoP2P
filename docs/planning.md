# Planning da Implementação

## Visão Geral do Projeto

O ProjetoP2P é um sistema distribuído autônomo que demonstra balanceamento de carga horizontal em uma arquitetura P2P. Cada nó pode atuar como **master** ou **worker**. O master coordena uma farm de workers e, quando saturado, pode negociar dinamicamente o empréstimo de workers de masters vizinhos.

O sistema foi projetado para:
- descobrir automaticamente o master por broadcast UDP
- registrar workers via heartbeat TCP
- distribuir tarefas FIFO
- eleger um novo master automaticamente quando o atual falha
- permitir redirecionamento e devolução de workers entre masters
- manter interoperabilidade entre equipes via protocolo JSON padronizado

---

## Objetivos do SDD

- O1: Implementar a arquitetura P2P com master e workers
- O2: Simular carga de trabalho e monitorar saturação
- O3: Detectar saturação no master e acionar negociação de ajuda
- O4: Projetar protocolo consensual entre masters
- O5: Implementar redirecionamento dinâmico de workers
- O6: Garantir autonomia e interoperabilidade entre implementações diferentes

---

## Requisitos Funcionais

| # | Requisito | Sprint |
|---|---|---|
| RF-01 | Workers enviam heartbeat periódico ao master | Sprint 1 |
| RF-02 | Master registra workers que enviam heartbeat | Sprint 1 |
| RF-03 | Workers detectam falha do master por heartbeats consecutivos sem resposta | Sprint 1 |
| RF-04 | Workers elegem novo master de forma automática e determinística | Sprint 2.1 |
| RF-05 | Novo master notifica todos os outros workers | Sprint 2.1 |
| RF-06 | Master distribui tarefas da fila FIFO para workers | Sprint 2 |
| RF-07 | Workers reportam resultado (OK/NOK) e master confirma com ACK | Sprint 2 |
| RF-08 | Worker "emprestado" identifica master original no payload | Sprint 2 |
| RF-09 | IP do master descoberto via broadcast UDP (sem IP fixo) | Sprint 2.1 |
| RF-10 | Eleição via broadcast UDP puro (sem peers pré-configurados) | Sprint 2.1 |
| RF-11 | Masters negociam recursos via protocolo P2P | Sprint 3 |
| RF-12 | Masters redirecionam workers emprestados e permitem retorno | Sprint 3 |
| RF-13 | Master envia relatórios periódicos de telemetria e desempenho para o supervisor via TCP/TLS | Sprint 4 |

---

## Requisitos Não-Funcionais

- Tolerância a falhas: o sistema continua operando após queda do master
- Determinismo: os nós devem chegar ao mesmo vencedor na eleição
- Configuração mínima: apenas UUID e portas no `.env`
- Descoberta automática: broadcast UDP direcionado, não broadcast limitado
- Confiabilidade: mensagens JSON embutidas em stream TCP terminam em `\n`
- Extensibilidade: campos desconhecidos devem ser ignorados, campos obrigatórios validados

---

## Decisões de Design do SDD

### 1. Padrão de mensagem JSON
- Toda mensagem TCP usa JSON terminado em `\n`
- Permite múltiplos objetos em um mesmo stream
- Facilita parsing incremental e compatibilidade com sockets

### 2. Envelope master-to-master
- Mensagens entre masters usam:
  - `type`
  - `request_id`
  - `payload`
- `request_id` correlaciona request/resposta em conexões concorrentes

### 3. Descoberta e eleição
- Master é descoberto via broadcast UDP na subrede
- Workers elegem novo master por `ELECTION_BROADCAST` / `ELECTION_RESPONSE`
- Vencedor é calculado determinísticamente por `(-free_space, uuid)`
- `NEW_MASTER` é enviado via broadcast UDP para sincronizar os demais

### 4. Worker emprestado
- Um worker emprestado envia `SERVER_UUID` para indicar o master original
- O master receptor registra a origem e inclui o campo no log de tarefa
- Devolução é coordenada por `command_release` e `notify_worker_returned`

### 5. Broadcast seguro em redes corporativas
- `255.255.255.255` pode ser bloqueado
- O sistema detecta o broadcast direto `/24` a partir do IP local
- O usuário pode sobrescrever `WORKER_BROADCAST_ADDRESS` no `.env`

---

## Sprints e Evolução

### Sprint 1 — Infraestrutura Base

**Objetivo:** Estabelecer comunicação básica e heartbeat.

- Heartbeat TCP entre worker e master
- Registro de workers no master
- Resposta `ALIVE` com lista de peers ativos
- Mensagem JSON delimitada por `\n`
- Worker mantém conexão de status e heartbeat periódico

### Sprint 2 — Ciclo de Tarefas

**Objetivo:** Distribuir tarefas e confirmar resultados.

- Worker apresenta-se com `WORKER: ALIVE`
- Master responde com `TASK: QUERY` ou `TASK: NO_TASK`
- Worker simula processamento e envia `STATUS: OK/NOK`
- Master confirma com `STATUS: ACK`
- Suporte a `SERVER_UUID` quando o worker é emprestado

### Sprint 2.1 — Discovery e Eleição UDP

**Objetivo:** Eliminar IP fixo e tornar a eleição independente.

- `FIND_MASTER` broadcast UDP para discovery
- `MASTER_FOUND` unicast de retorno
- Eleição via broadcast UDP puro sem peers conhecidos
- Novo master anuncia via `NEW_MASTER`
- Mecanismo de atraso `ELECTION_DELAY` para evitar eleições concorrentes

### Sprint 3 — Negociação Master-to-Master

**Objetivo:** Permitir master saturado solicitar workers de um vizinho.

- `request_help` para pedir workers extras
- `response_accepted` ou `response_rejected`
- `command_redirect` para ligar workers ao master saturado
- `register_temporary_worker` para apresentação do worker emprestado
- `command_release` para devolução quando a carga normaliza
- `notify_worker_returned` para atualizar o master original

### Sprint 4 — Supervisor de Métricas e Apresentação Final

**Objetivo:** Enviar telemetria detalhada de hardware, rede e estado do cluster P2P para o painel de visualização em tempo real.

- Envio periódico (a cada 10s) de métricas de sistema (CPU, RAM, disco e load average)
- Envio do estado da farm de workers (ativos, ociosos, emprestados, recebidos e falhos)
- Envio do estado das tarefas (pendentes, executando, completadas, falhas e idade da mais antiga)
- Monitoramento e anúncio da disponibilidade de masters vizinhos
- Suporte a conexões seguras TCP com criptografia TLS/SNI

---

## Protocolo e Payloads

### Heartbeat
- Worker → Master
  ```json
  {"SERVER_UUID":"Master_A","TASK":"HEARTBEAT"}
  ```
- Master → Worker
  ```json
  {"SERVER_UUID":"Master_A","TASK":"HEARTBEAT","RESPONSE":"ALIVE"}
  ```

### Apresentação de Worker
- Worker local
  ```json
  {"WORKER":"ALIVE","WORKER_UUID":"..."}
  ```
- Worker emprestado
  ```json
  {"WORKER":"ALIVE","WORKER_UUID":"...","SERVER_UUID":"Master_B"}
  ```

### Distribuição de tarefa
- Com tarefa
  ```json
  {"TASK":"QUERY","USER":"...","TASK_ID":"..."}
  ```
- Sem tarefa
  ```json
  {"TASK":"NO_TASK"}
  ```

### Resultado e ACK
- Worker → Master
  ```json
  {"STATUS":"OK","TASK":"QUERY","WORKER_UUID":"..."}
  ```
- Master → Worker
  ```json
  {"STATUS":"ACK"}
  ```

### Comunicação Master-to-Master
- `request_help`
- `response_accepted`
- `response_rejected`
- `command_redirect`
- `register_temporary_worker`
- `command_release`
- `notify_worker_returned`

### Telemetria e Monitoramento (Sprint 4)
- `performance_report` (Master → Supervisor via TCP/TLS)

---

## Critérios de Aceitação

1. Worker abre conexão TCP com o master
2. Master identifica e processa JSON corretamente
3. Mensagens no stream TCP terminam em `\n`
4. Worker recebe `QUERY` ou `NO_TASK` corretamente
5. Worker recebe `ACK` após REPORT de status
6. Worker emprestado é identificado por `SERVER_UUID`
7. Eleição e descoberta funcionam sem IP fixo
8. Master coleta métricas locais e de rede sem bloquear operações principais
9. Master estabelece conexão TLS e envia JSON estruturado de telemetria periodicamente
10. Painel do Supervisor atualiza com dados reais do nó master e workers associados

---

## Observação

Este documento reflete a visão do SDD para o sistema e sua evolução. Ele descreve tanto o que já foi implementado quanto o design esperado para próximos passos de master-to-master e redirecionamento de workers.
