# Protocolo de Mensagens

Todas as mensagens são JSON terminadas com `\n` (newline) em TCP, e JSON puro em UDP.

---

## Discovery — Descoberta do Master

### FIND_MASTER
**Direção:** Worker → broadcast UDP `:DISCOVERY_PORT`  
**Quando:** Ao iniciar o worker

```json
{
  "TASK":        "FIND_MASTER",
  "SERVER_UUID": "SRV-MASTER",
  "WORKER_UUID": "WRK-01-ALPHA"
}
```

| Campo | Tipo | Descrição |
|---|---|---|
| `TASK` | string | Identificador da mensagem |
| `SERVER_UUID` | string | UUID do master procurado (vazio = qualquer) |
| `WORKER_UUID` | string | UUID do worker solicitante |

### MASTER_FOUND
**Direção:** Servidor → unicast UDP para o remetente  
**Quando:** Em resposta ao `FIND_MASTER` com UUID correspondente

```json
{
  "TASK":        "MASTER_FOUND",
  "SERVER_UUID": "SRV-MASTER",
  "MASTER_IP":   "10.62.206.48",
  "MASTER_PORT": 8000
}
```

---

## Heartbeat — Operação Normal

### HEARTBEAT (Worker → Master)
**Protocolo:** TCP `:MASTER_PORT`  
**Frequência:** `HEARTBEAT_INTERVAL` segundos (padrão: 5s)

```json
{
  "TASK":        "HEARTBEAT",
  "WORKER_UUID": "WRK-01-ALPHA",
  "WORKER_HOST": "10.62.206.23",
  "WORKER_PORT": 8000
}
```

### HEARTBEAT Response (Master → Worker)

```json
{
  "SERVER_UUID": "SRV-MASTER",
  "TASK":        "HEARTBEAT",
  "RESPONSE":    "ALIVE",
  "PEERS": [
    {"uuid": "WRK-02-BETA",  "host": "10.62.206.24", "port": 8000},
    {"uuid": "WRK-03-GAMMA", "host": "10.62.206.25", "port": 8000}
  ]
}
```

| `RESPONSE` | Significado |
|---|---|
| `"ALIVE"` | Master ativo, heartbeat registrado |
| `"NOT_MASTER"` | Este nó não é o master → iniciar eleição |

---

## Tarefas — Ciclo de Trabalho (Sprint 2)

### Payload 2.1 — Apresentação do Worker (local)
**Direção:** Worker → Master TCP

```json
{
  "WORKER":      "ALIVE",
  "WORKER_UUID": "WRK-01-ALPHA"
}
```

### Payload 2.1b — Apresentação do Worker (emprestado)
Usado quando o master atual é diferente do `SERVER_UUID` original no `.env`:

```json
{
  "WORKER":      "ALIVE",
  "WORKER_UUID": "WRK-01-ALPHA",
  "SERVER_UUID": "SRV-MASTER"
}
```

### Payload 2.2 — Tarefa (Master → Worker)

```json
{
  "TASK":    "QUERY",
  "USER":    "user1",
  "TASK_ID": "task-001"
}
```

### Payload 2.3 — Sem Tarefas (Master → Worker)

```json
{
  "TASK": "NO_TASK"
}
```

### Payload 2.4 — Resultado (Worker → Master)

```json
{
  "STATUS":      "OK",
  "TASK":        "QUERY",
  "WORKER_UUID": "WRK-01-ALPHA"
}
```

Se emprestado, inclui também:
```json
{
  "SERVER_UUID": "SRV-MASTER"
}
```

| `STATUS` | Significado |
|---|---|
| `"OK"` | Tarefa processada com sucesso (90% dos casos) |
| `"NOK"` | Tarefa falhou (10% dos casos — simulado) |

### Payload 2.5 — ACK (Master → Worker)

```json
{
  "STATUS": "ACK"
}
```

---

## Eleição — Falha do Master

### ELECTION_BROADCAST
**Direção:** Worker → broadcast UDP `:WORKER_PORT`  
**Quando:** Após `ELECTION_DELAY` segundos de heartbeats falhos

```json
{
  "TASK":        "ELECTION_BROADCAST",
  "WORKER_UUID": "WRK-01-ALPHA",
  "WORKER_HOST": "10.62.206.23",
  "WORKER_PORT": 8000,
  "FREE_SPACE":  107374182400
}
```

### ELECTION_RESPONSE
**Direção:** Worker → unicast UDP para a porta efêmera do remetente  
**Quando:** Em resposta a `ELECTION_BROADCAST` de outro worker

```json
{
  "TASK":        "ELECTION_RESPONSE",
  "WORKER_UUID": "WRK-02-BETA",
  "WORKER_HOST": "10.62.206.24",
  "WORKER_PORT": 8000,
  "FREE_SPACE":  53687091200
}
```

> `FREE_SPACE` em bytes. Usado como critério de eleição: maior valor vence.

### NEW_MASTER
**Direção:** Worker vencedor → broadcast UDP `:WORKER_PORT`  
**Quando:** Após vencer a eleição

```json
{
  "TASK":              "NEW_MASTER",
  "MASTER_HOST":       "10.62.206.23",
  "MASTER_PORT":       8000,
  "MASTER_UUID":       "WRK-01-ALPHA",
  "MASTER_FREE_SPACE": 107374182400
}
```

### NEW_MASTER_ACK *(legado TCP)*
**Direção:** Worker → Master TCP  
**Quando:** Ao receber NEW_MASTER via TCP (código legado, não mais usado ativamente)

```json
{
  "TASK":     "NEW_MASTER_ACK",
  "RESPONSE": "RECEIVED"
}
```

---

## Status do Worker — Eleição Legado (TCP)

> Estes payloads foram usados na eleição via TCP (Sprint 1). Estão comentados no código mas mantidos aqui para referência histórica.

### WORKER_STATUS (request)
```json
{ "TASK": "WORKER_STATUS" }
```

### WORKER_STATUS_RESPONSE
```json
{
  "TASK":        "WORKER_STATUS_RESPONSE",
  "WORKER_UUID": "WRK-02-BETA",
  "WORKER_HOST": "10.62.206.24",
  "WORKER_PORT": 8000,
  "FREE_SPACE":  53687091200
}
```

---

## Erros

```json
{
  "SERVER_UUID": "SRV-MASTER",
  "TASK":        "ERROR",
  "RESPONSE":    "UNKNOWN_TASK"
}
```
