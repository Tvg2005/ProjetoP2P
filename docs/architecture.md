# Arquitetura do Sistema

## Visão Geral

O ProjetoP2P é um sistema distribuído **tolerante a falhas** composto por múltiplos nós que se comunicam via TCP e UDP. Um nó assume o papel de **master** (coordenador) e os demais atuam como **workers**. Se o master falhar, os workers automaticamente elegem um novo master entre si via broadcast UDP, sem qualquer configuração manual.

---

## Componentes

### `servidor.py` — Master

Responsável por:
- **Receber heartbeats** dos workers e mantê-los registrados
- **Responder a broadcasts FIND_MASTER** para que novos workers descubram o IP do servidor
- **Distribuir tarefas** da fila FIFO para os workers
- **Confirmar resultados** e manter log de execução

### `cliente.py` — Worker

Responsável por:
- **Descobrir o master** via broadcast UDP ao iniciar
- **Enviar heartbeats** periódicos e detectar falha do master
- **Solicitar e executar tarefas** recebidas do master
- **Participar da eleição** quando o master falha (envia ELECTION_BROADCAST e responde)
- **Assumir o papel de master** se eleito (sobe servidor.py como subprocesso)

---

## Topologia de Rede

```
                    ┌─────────────────────┐
                    │    MASTER           │
                    │    servidor.py      │
                    │    TCP :8000        │
                    │    UDP :8001 (disc) │
                    └──────┬──────────────┘
                           │  TCP (heartbeat, tarefas)
           ┌───────────────┼───────────────┐
           │               │               │
    ┌──────▼──────┐ ┌──────▼──────┐ ┌──────▼──────┐
    │  Worker A   │ │  Worker B   │ │  Worker C   │
    │  cliente.py │ │  cliente.py │ │  cliente.py │
    │  TCP :8000  │ │  TCP :8000  │ │  TCP :8000  │
    │  UDP :8000  │ │  UDP :8000  │ │  UDP :8000  │
    └─────────────┘ └─────────────┘ └─────────────┘
```

> Quando o master cai, os workers se comunicam **diretamente entre si via broadcast UDP** na porta `WORKER_PORT` para realizar a eleição — sem precisar conhecer os IPs uns dos outros previamente.

---

## Portas Utilizadas

| Porta | Protocolo | Usado por | Finalidade |
|---|---|---|---|
| `MASTER_PORT` (8000) | TCP | servidor.py | Heartbeat, tarefas, status |
| `DISCOVERY_PORT` (8001) | UDP | servidor.py | Responder FIND_MASTER broadcasts |
| `WORKER_PORT` (8000) | TCP | cliente.py | Status, WORKER_STATUS (eleição legado) |
| `WORKER_PORT` (8000) | UDP | cliente.py | ELECTION_BROADCAST, NEW_MASTER, DISCOVER_WORKER |

> `MASTER_PORT` e `WORKER_PORT` podem ter o mesmo valor (8000). O servidor TCP do worker e o servidor TCP do master usam portas distintas por serem em máquinas diferentes.

---

## Fases de Operação

### Fase 1 — Discovery (Inicialização do Worker)

```
Worker                 Rede (broadcast)             Servidor
  │                          │                          │
  │── FIND_MASTER ──────────►│ (UDP :8001 broadcast)    │
  │                          │◄── MASTER_FOUND ─────────│
  │◄── {ip, port} ───────────│                          │
  │                                                     │
  │──────────── TCP connect(ip:8000) ─────────────────►│
```

### Fase 2 — Operação Normal

```
Worker                                          Master
  │                                               │
  │── HEARTBEAT {uuid, host, port} ─────────────►│ (TCP, a cada HEARTBEAT_INTERVAL s)
  │◄── {ALIVE, PEERS:[...]} ─────────────────────│
  │                                               │
  │── {WORKER: "ALIVE"} ────────────────────────►│ (TCP, a cada TASK_INTERVAL s)
  │◄── {TASK: "QUERY", user, task_id} ───────────│
  │  [processa]                                   │
  │── {STATUS: "OK", TASK: "QUERY"} ────────────►│
  │◄── {STATUS: "ACK"} ──────────────────────────│
```

### Fase 3 — Falha do Master e Eleição

```
[4 heartbeats falhos]
        │
[Aguarda ELECTION_DELAY=30s]  ← todos os workers fazem isso simultaneamente
        │
        ▼
Worker A envia ELECTION_BROADCAST ──────────────► UDP broadcast :8000
Worker B envia ELECTION_BROADCAST ──────────────► UDP broadcast :8000
Worker C envia ELECTION_BROADCAST ──────────────► UDP broadcast :8000
        │
        │ Cada um recebe ELECTION_RESPONSE dos outros
        │ (resposta unicast de volta ao socket do remetente)
        │
        ▼
[Todos ordenam: (-free_space, uuid) → mesmo vencedor]
        │
   ┌────┴──────────────────────────────────┐
   │                                       │
Vencedor (ex: Worker A)              Perdedores (B, C)
   │                                       │
   ├─ _become_master()                     ├─ aguardam NEW_MASTER
   ├─ sobe servidor.py                     │
   └─ broadcast NEW_MASTER ───────────────►│
                                           │
                               _accept_new_master()
                               reconectam ao novo master
```

---

## Decisão de Vencedor (Determinística)

Todos os nós aplicam a **mesma função de ordenação** sobre os **mesmos dados**:

```python
def _election_key(node):
    return (-node["FREE_SPACE"], node["WORKER_UUID"])
```

1. **Mais espaço livre em disco** → prioridade (assumindo que tem mais capacidade)
2. **UUID lexicograficamente menor** → desempate estável

Como todos chegam ao mesmo resultado, **não há votação nem negociação**.

---

## Diagrama de Estados do Worker

```
         ┌──────────────────┐
    ───► │    DISCOVERY     │  (broadcast FIND_MASTER)
         └────────┬─────────┘
                  │ master encontrado
         ┌────────▼─────────┐
    ┌───►│    WORKER ATIVO  │  (heartbeat + tarefas)
    │    └────────┬─────────┘
    │             │ N heartbeats falhos
    │    ┌────────▼─────────┐
    │    │  AGUARDANDO      │  (ELECTION_DELAY segundos)
    │    │  ELECTION_DELAY  │
    │    └────────┬─────────┘
    │             │ sem NEW_MASTER recebido
    │    ┌────────▼─────────┐
    │    │    ELEIÇÃO       │  (ELECTION_BROADCAST + coleta)
    │    └────────┬─────────┘
    │             │
    │    ┌────────▼─────────────────────────┐
    │    │ Perdedor?  ──► aguarda NEW_MASTER │
    │    │ Vencedor?  ──► MASTER ATIVO       │
    │    └──────────────────────────────────┘
    │                        │
    │ (novo master detectado)│
    └────────────────────────┘
```
