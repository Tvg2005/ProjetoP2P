# ProjetoP2P — Sistema Distribuído com Eleição de Master

## Visão Geral

Este projeto implementa um sistema **peer-to-peer (P2P)** onde múltiplos computadores (workers) se conectam a um servidor central (master). O sistema é **tolerante a falhas**: se o master cair, os próprios workers elegem automaticamente um novo master entre si, sem intervenção humana. Além disso, o master distribui tarefas para os workers processarem, formando um sistema de computação distribuída completo.

---

## Arquivos do Projeto

| Arquivo | Papel |
|---|---|
| `servidor.py` | Roda no master. Recebe heartbeats, mantém registro de workers, gerencia fila de tarefas e distribui trabalho. |
| `cliente.py` | Roda em cada worker. Envia heartbeats, solicita e executa tarefas, participa da eleição se o master cair. |
| `.env` | Configuração de cada máquina (IP do master, UUID do worker, porta, intervalos, etc.). |

---

## Sprint 1 — Infraestrutura e Eleição de Master

### 1. Situação Normal

Cada worker envia uma mensagem de **heartbeat** ao master a cada N segundos (padrão: 5s). Essa mensagem serve para dois propósitos:

- **Confirmar que o worker está vivo** (o master recebe)
- **Confirmar que o master está vivo** (o worker recebe a resposta `ALIVE`)

```
Worker A ──── heartbeat ────► Master
Worker A ◄─── ALIVE + PEERS ── Master
Worker B ──── heartbeat ────► Master
Worker C ──── heartbeat ────► Master
```

---

### 2. Registro de Peers (Descoberta Automática)

Um dos problemas clássicos em sistemas distribuídos é: **como cada nó descobre quem são os outros nós da rede?**

A solução implementada usa o **master como ponto central de registro**:

**No worker (`cliente.py`):** o heartbeat inclui o próprio endereço (IP e porta):
```json
{
  "TASK": "HEARTBEAT",
  "WORKER_UUID": "WRK-01-ALPHA",
  "WORKER_HOST": "192.168.1.10",
  "WORKER_PORT": 8000
}
```

**No master (`servidor.py`):** ao receber o heartbeat, ele:
1. Registra o worker no dicionário interno `registry`
2. Devolve na resposta a lista de **todos os outros workers ativos**:
```json
{
  "RESPONSE": "ALIVE",
  "PEERS": [
    {"uuid": "WRK-02-BETA",  "host": "192.168.1.11", "port": 8000},
    {"uuid": "WRK-03-GAMMA", "host": "192.168.1.12", "port": 8000}
  ]
}
```

**No worker:** ao receber o `ALIVE`, ele salva a lista de peers em memória (`known_peers`). Isso acontece a cada ciclo de heartbeat, mantendo o mapa da rede sempre atualizado.

> Com isso, **nenhuma configuração manual de IPs é necessária**. Os workers se descobrem automaticamente ao passar pelo master.

---

### 3. Detecção de Falha do Master

O worker conta quantos heartbeats consecutivos falharam. Se o número de falhas atingir o limite (`HEARTBEAT_THRESHOLD`, padrão: 4 falhas), o worker conclui que o master caiu e **inicia o processo de eleição**.

```
Tentativa 1: sem resposta  → falha 1/4
Tentativa 2: sem resposta  → falha 2/4
Tentativa 3: sem resposta  → falha 3/4
Tentativa 4: sem resposta  → falha 4/4  ✗  → INICIA ELEIÇÃO
```

---

### 4. Eleição do Novo Master (Algoritmo Bully Adaptado)

O algoritmo utilizado é uma adaptação do **Bully Algorithm**, clássico em sistemas distribuídos. Ele garante que **apenas um nó** se torne o novo master.

#### Passo a passo da eleição:

```
┌─────────────────────────────────────────────────────────────────┐
│                    MASTER CAIU                                  │
│                                                                 │
│  Worker A, B e C detectam a falha (após 4 heartbeats falhos)   │
└──────────────────────────┬──────────────────────────────────────┘
                           │
        ┌──────────────────▼──────────────────┐
        │  PASSO 1: Consultar peers via TCP   │
        │                                     │
        │  A → pergunta status de B e C       │
        │  B → pergunta status de A e C       │
        │  C → pergunta status de A e B       │
        │                                     │
        │  Cada um responde com:              │
        │   {uuid, host, port, free_space}    │
        └──────────────────┬──────────────────┘
                           │
        ┌──────────────────▼──────────────────┐
        │  PASSO 2: Decidir o vencedor        │
        │                                     │
        │  Todos ordenam os candidatos pela   │
        │  MESMA chave determinística:        │
        │                                     │
        │  → Mais espaço livre em disco vence │
        │  → Empate: UUID menor vence         │
        │                                     │
        │  Como a chave é igual para todos,   │
        │  todos chegam ao MESMO vencedor     │
        └──────────────────┬──────────────────┘
                           │
        ┌──────────────────▼──────────────────┐
        │  PASSO 3: Agir conforme o resultado │
        │                                     │
        │  Se sou o vencedor:                 │
        │   → inicio servidor.py localmente   │
        │   → notifico todos via TCP: NEW_MASTER│
        │                                     │
        │  Se não sou o vencedor:             │
        │   → aguardo a notificação NEW_MASTER│
        │   → atualizo meu ponteiro de master │
        └─────────────────────────────────────┘
```

#### Por que o Bully funciona aqui?

A chave do algoritmo é que **todos os nós aplicam exatamente a mesma função de ordenação** sobre os mesmos dados. Isso elimina a necessidade de "votar" ou "negociar" — cada nó chega ao mesmo vencedor de forma independente. Apenas o vencedor age; os demais aguardam e obedecem.

#### Por que TCP e não UDP?

A eleição usa **TCP** (não UDP broadcast) para contatar os peers porque:
- TCP é confiável: garante entrega das mensagens
- UDP broadcast pode ser bloqueado pelo firewall do Windows entre máquinas diferentes
- Os IPs dos peers já são conhecidos (aprendidos via master), então broadcast não é necessário

---

## Sprint 2 — Comunicação de Tarefas e Ciclo de Vida

### 5. Apresentação do Worker ao Master

Além do heartbeat periódico, cada worker se **apresenta formalmente** ao master a cada `TASK_INTERVAL` segundos (padrão: 10s) para solicitar uma tarefa.

Existem dois tipos de apresentação:

**Payload 2.1 — Worker local** (conectado ao seu master original):
```json
{
  "WORKER": "ALIVE",
  "WORKER_UUID": "WRK-01-ALPHA"
}
```

**Payload 2.1b — Worker "Emprestado"** (após eleição, conectado a um master diferente do original):
```json
{
  "WORKER": "ALIVE",
  "WORKER_UUID": "WRK-01-ALPHA",
  "SERVER_UUID": "SRV-MASTER"
}
```

> Um worker é considerado **"Emprestado"** quando o master atual (eleito via Bully Algorithm) tem UUID diferente do master original configurado no `.env`. O campo `SERVER_UUID` identifica de qual master original esse worker veio.

---

### 6. Fila de Tarefas e Distribuição

O master mantém uma **fila de tarefas** (`task_queue`). Ao receber a apresentação de um worker, ele verifica a fila:

**Com tarefa disponível — Payload 2.2:**
```json
{
  "TASK": "QUERY",
  "USER": "user1",
  "TASK_ID": "task-001"
}
```

**Fila vazia — Payload 2.3:**
```json
{
  "TASK": "NO_TASK"
}
```

A fila é inicializada com tarefas na inicialização do servidor. A distribuição é **FIFO** (primeiro a entrar, primeiro a sair) e cada tarefa é entregue a apenas um worker.

---

### 7. Processamento e Relatório de Status

Ao receber uma tarefa `QUERY`, o worker:
1. Simula o processamento (tempo aleatório entre 1 e 4 segundos)
2. Determina o resultado (90% de chance de `OK`, 10% de `NOK`)
3. Reporta o resultado ao master

**Payload 2.4 — Relatório de resultado:**
```json
{
  "STATUS": "OK",
  "TASK": "QUERY",
  "WORKER_UUID": "WRK-01-ALPHA"
}
```

Se o worker for "Emprestado", inclui também o `SERVER_UUID` no relatório.

---

### 8. Confirmação (ACK) e Log de Conclusão

Ao receber o resultado do worker, o master:
1. Registra no log interno: worker, tarefa, status, origem (local/emprestado) e timestamp
2. Responde imediatamente com ACK para liberar o worker

**Payload 2.5 — Confirmação:**
```json
{
  "STATUS": "ACK"
}
```

Exemplo de log no master:
```
[MASTER] LOG | Worker=WRK-02-BETA (local) | Task=task-001 | Status=OK
[MASTER] LOG | Worker=WRK-01-ALPHA (emprestado de SRV-MASTER) | Task=task-002 | Status=OK
```

---

### 9. Fluxo Completo de uma Tarefa

```
Worker                              Master
  │                                   │
  │── {"WORKER": "ALIVE", ...} ──────►│  (Payload 2.1 ou 2.1b)
  │                                   │  Verifica fila
  │◄── {"TASK": "QUERY", "USER": ...} │  (Payload 2.2) — se há tarefa
  │    ou {"TASK": "NO_TASK"}         │  (Payload 2.3) — se fila vazia
  │                                   │
  │  [Processa por 1–4 segundos]      │
  │                                   │
  │── {"STATUS": "OK", ...} ─────────►│  (Payload 2.4)
  │                                   │  Loga conclusão
  │◄── {"STATUS": "ACK"} ─────────────│  (Payload 2.5)
  │                                   │
```

---

## Sprint 3 — Negociação entre Masters e Redirecionamento de Workers

A última fase implementada adiciona suporte a **masters colaborativos** e **workers temporariamente emprestados**.

### 10. Quando o master fica sobrecarregado

O master monitora sua carga de tarefas e, quando ultrapassa a capacidade configurada, pode solicitar ajuda a seus vizinhos definidos em `MASTER_NEIGHBORS`.

- `MASTER_CAPACITY` define quantas tarefas o master pode suportar antes de pedir ajuda.
- Quando saturado, ele envia `request_help` a outros masters.
- Se um vizinho aceitar, ele devolve `response_accepted` com workers disponíveis.

### 11. Redirecionamento de workers

Quando um master vizinho concorda em emprestar um worker, ele envia um comando `command_redirect` diretamente ao worker.

O worker então:
- reconecta ao novo master
- registra-se como `register_temporary_worker`
- continua pedindo tarefas ao master temporário

### 12. Retorno do worker ao master original

Quando a carga normaliza no master que recebeu o worker, ele envia `command_release` para o worker, que:
- encerra o vínculo com o master temporário
- retorna ao master original
- recomeça o ciclo de requisição de tarefas com o master original

Além disso, o master original é notificado com `notify_worker_returned` para atualizar seu registro de workers.

### 13. Principais novos campos e comandos

- `request_help` → solicitação de ajuda entre masters
- `response_accepted` / `response_rejected` → resposta ao pedido de ajuda
- `command_redirect` → instrui um worker a trocar de master
- `register_temporary_worker` → worker informa ao novo master que está emprestado
- `command_release` → libera worker de volta ao master original
- `notify_worker_returned` → master temporário informa o master de origem sobre o retorno

### 14. Configuração adicional

As variáveis de ambiente novas ou ampliadas para Sprint 3 incluem:

- `MASTER_NEIGHBORS` — lista de peers do master no formato `MASTER_ID:IP:PORT,...`
- `MASTER_CAPACITY` — limite de tarefas antes de pedir ajuda
- `MASTER_RELEASE_THRESHOLD` — carga mínima para liberar workers emprestados
- `MASTER_HELP_TIMEOUT` — timeout para comunicação entre masters
- `LOAD_MONITOR_INTERVAL` — intervalo de verificação de carga

> Essa extensão permite que a rede de masters funcione de forma mais equilibrada, reduzindo sobrecarga e evitando que um único nó fique saturado.

---

## Configuração (`.env`)

Cada máquina precisa de um `.env` com sua identidade. O IP do master é o único dado que precisa ser configurado manualmente.

```env
# IP e porta do master original
MASTER_IP=192.168.1.100
MASTER_PORT=8000
SERVER_UUID=SRV-MASTER      # UUID do master (usado em servidor.py e para detectar "Emprestado")

# Identidade deste worker (único por máquina)
WORKER_UUID=WRK-01-ALPHA
WORKER_PORT=8000

# Controle de heartbeat e eleição
HEARTBEAT_THRESHOLD=4       # falhas consecutivas antes de iniciar eleição
HEARTBEAT_INTERVAL=5        # segundos entre cada heartbeat

# Controle de tarefas (Sprint 2)
TASK_INTERVAL=10            # segundos entre cada solicitação de tarefa

# Novas configurações de Sprint 3
MASTER_NEIGHBORS=SRV-NEIGHBOR:192.168.1.101:8000,SRV-NEIGHBOR2:192.168.1.102:8000
MASTER_CAPACITY=100         # limite de tarefas antes de pedir ajuda
MASTER_RELEASE_THRESHOLD=60 # carga mínima para liberar workers emprestados
MASTER_HELP_TIMEOUT=5       # timeout para comunicação entre masters
LOAD_MONITOR_INTERVAL=5     # intervalo em segundos para verificação de carga
```

> `WORKER_PEERS` pode ficar **vazio** — os peers são descobertos automaticamente via master.

---

## Resumo do Fluxo Completo

```
[INÍCIO]
  servidor.py rodando na máquina master
  cliente.py rodando em cada worker

[OPERAÇÃO NORMAL — Sprint 1]
  Workers enviam heartbeat a cada 5s
  → Master responde ALIVE + lista de peers ativos
  → Workers atualizam known_peers automaticamente

[CICLO DE TAREFAS — Sprint 2]
  Workers se apresentam ao master a cada 10s
  → Master verifica fila e envia QUERY ou NO_TASK
  → Se QUERY: worker processa e reporta STATUS (OK/NOK)
  → Master confirma com ACK e registra no log

[FALHA DO MASTER]
  Workers detectam 4 heartbeats sem resposta
  → Cada worker consulta o status dos peers conhecidos via TCP
  → Todos ordenam os candidatos pela mesma chave
  → Mesmo vencedor determinado por todos
  → Vencedor inicia servidor.py localmente e notifica os demais
  → Demais workers apontam para o novo master

[SISTEMA RESTAURADO]
  1 novo master rodando (servidor.py no worker eleito)
  2 workers apontam para o novo master e continuam operando normalmente
```

---

## Tecnologias Utilizadas

- **Python 3** — linguagem principal
- **Sockets TCP/UDP** — comunicação entre nós (biblioteca `socket` padrão)
- **Threading** — múltiplas conexões simultâneas e eleição em paralelo
- **python-dotenv** — leitura de configuração via `.env`
- **schedule** — agendamento periódico do heartbeat
