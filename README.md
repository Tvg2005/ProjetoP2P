# ProjetoP2P — Sistema Distribuído com Eleição de Master

## Visão Geral

Este projeto implementa um sistema **peer-to-peer (P2P)** onde múltiplos computadores (workers) se conectam a um servidor central (master). O sistema é **tolerante a falhas**: se o master cair, os próprios workers elegem automaticamente um novo master entre si, sem intervenção humana.

---

## Arquivos do Projeto

| Arquivo | Papel |
|---|---|
| `servidor.py` | Roda no master. Recebe heartbeats dos workers e mantém o registro de quem está ativo. |
| `cliente.py` | Roda em cada worker. Envia heartbeats, detecta falha do master e participa da eleição. |
| `.env` | Configuração de cada máquina (IP do master, UUID do worker, porta, etc.). |

---

## Como o Sistema Funciona

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

## Configuração (`.env`)

Cada máquina precisa de um `.env` com sua identidade. O IP do master é o único dado que precisa ser configurado manualmente.

```env
# IP e porta do master original
MASTER_IP=192.168.1.100
MASTER_PORT=8000
SERVER_UUID=SRV-MASTER   # usado apenas no servidor.py

# Identidade deste worker (único por máquina)
WORKER_UUID=WRK-01-ALPHA
WORKER_PORT=8000

# Quantas falhas de heartbeat antes de iniciar eleição
HEARTBEAT_THRESHOLD=4
HEARTBEAT_INTERVAL=5      # segundos entre cada heartbeat
```

> `WORKER_PEERS` pode ficar **vazio** — os peers são descobertos automaticamente via master.

---

## Resumo do Fluxo Completo

```
[INÍCIO]
  servidor.py rodando na máquina master (IP configurado no .env de cada worker)
  cliente.py rodando em cada worker

[OPERAÇÃO NORMAL]
  Workers enviam heartbeat a cada 5s → master responde ALIVE + lista de peers
  Workers atualizam known_peers com a lista recebida

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
