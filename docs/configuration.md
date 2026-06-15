# Configuração — Referência do `.env`

Cada máquina (master e workers) precisa de um arquivo `.env` na raiz do projeto. As variáveis marcadas com ⚠️ devem ser únicas por máquina.

---

## Identificação

| Variável | Obrigatória | Padrão | Descrição |
|---|---|---|---|
| `SERVER_UUID` | ✅ | — | UUID do servidor master alvo. Usado pelo cliente para filtrar broadcasts `FIND_MASTER` e identificar master "original" (conceito Emprestado). O `servidor.py` usa este valor como sua própria identidade. |
| `WORKER_UUID` | ✅ ⚠️ | — | UUID único deste worker. Deve ser diferente em cada máquina. Exemplo: `WRK-01-ALPHA`, `WRK-02-BETA`. |

---

## Portas e Endereço

| Variável | Obrigatória | Padrão | Descrição |
|---|---|---|---|
| `MASTER_PORT` | ✅ | — | Porta TCP onde o `servidor.py` escuta. Workers usam esta porta para heartbeat e tarefas. |
| `WORKER_PORT` | ✅ ⚠️ | — | Porta TCP/UDP onde este worker escuta status, eleição e discovery. **Pode ser igual ao `MASTER_PORT`** pois estão em máquinas diferentes. |
| `DISCOVERY_PORT` | ❌ | `MASTER_PORT + 1` | Porta UDP onde o servidor escuta `FIND_MASTER` broadcasts. Deve ser a mesma em servidor e clientes. |
| `WORKER_HOST` | ❌ | *auto-detectado* | IP local deste worker. Se omitido, detectado automaticamente via socket UDP conectado a `8.8.8.8`. |
| `WORKER_BROADCAST_ADDRESS` | ❌ | *auto-detectado* | Endereço de broadcast para discovery e eleição. Se omitido ou `255.255.255.255`, usa o broadcast direcionado `/24` da rede local (ex: `10.62.206.255`). |
| `MASTER_NEIGHBORS` | ❌ | `""` | Lista de masters vizinhos no formato `id:ip:porta` separados por vírgula. Usado pelo master para enviar `request_help` e negociar empréstimos. |
| `MASTER_CAPACITY` | ❌ | `100` | Capacidade de carga do master em número de tarefas pendentes. Quando `current_load > MASTER_CAPACITY`, o master tenta pedir ajuda. |
| `MASTER_RELEASE_THRESHOLD` | ❌ | `60` | Limite abaixo do qual um master libera workers emprestados de volta ao mestre original. Deve ser menor que `MASTER_CAPACITY` para evitar ping-pong. |
| `MASTER_HELP_TIMEOUT` | ❌ | `5` | Timeout em segundos para aguardar resposta a `request_help` de um master vizinho. |
| `LOAD_MONITOR_INTERVAL` | ❌ | `5` | Intervalo em segundos para o master verificar carga e disparar `request_help` ou `command_release`. |
| `SUPERVISOR_HOST` | ❌ | `"nuted-ia.dev"` | Endereço do host do supervisor de métricas. |
| `SUPERVISOR_PORT` | ❌ | `443` | Porta TCP do supervisor de métricas. |
| `SUPERVISOR_TLS` | ❌ | `true` | Define se a conexão TCP com o supervisor usará criptografia TLS. |
| `SUPERVISOR_SNI` | ❌ | `"nuted-ia.dev"` | Server Name Indication (SNI) usado na negociação TLS. |
| `SUPERVISOR_INTERVAL` | ❌ | `10` | Intervalo em segundos entre cada envio de telemetria. |
| `SUPERVISOR_PAYLOAD_VERSION` | ❌ | `"sprint4-monitor"` | Versão do schema de dados a ser enviado para o supervisor. |

---

## Heartbeat

| Variável | Padrão | Descrição |
|---|---|---|
| `HEARTBEAT_INTERVAL` | `5` | Intervalo em segundos entre heartbeats enviados ao master. |
| `HEARTBEAT_THRESHOLD` | `4` | Número de heartbeats consecutivos com falha antes de detectar o master como inativo e iniciar o processo de eleição. |
| `WORKER_STATUS_TIMEOUT` | `3` | Timeout em segundos para respostas de status TCP (legado). |

---

## Eleição

| Variável | Padrão | Descrição |
|---|---|---|
| `ELECTION_DELAY` | `30` | Segundos a aguardar após detectar master inativo antes de iniciar eleição via broadcast. Permite que um `NEW_MASTER` de eleição paralela chegue e cancele a eleição. |
| `ELECTION_COLLECT_TIMEOUT` | `5` | Janela em segundos para coletar respostas `ELECTION_RESPONSE` após enviar `ELECTION_BROADCAST`. Aumentar em redes com alta latência. |
| `NEW_MASTER_WAIT` | `12` | Segundos que um worker perdedor aguarda o vencedor anunciar `NEW_MASTER`. Se expirar, reinicia a eleição. |
| `ELECTION_STATUS_TIMEOUT` | `4` | Timeout TCP para `WORKER_STATUS` (eleição legado — não mais usado). |

---

## Tarefas

| Variável | Padrão | Descrição |
|---|---|---|
| `TASK_INTERVAL` | `10` | Intervalo em segundos entre cada ciclo de solicitação de tarefa ao master. |

---

## Discovery de Peers (Legado)

> Estas variáveis eram usadas na eleição via TCP com peers conhecidos. Mantidas para compatibilidade mas não têm efeito no comportamento atual de eleição via broadcast.

| Variável | Padrão | Descrição |
|---|---|---|
| `WORKER_PEERS` | `""` | Lista de peers estáticos `host:porta` separados por vírgula. Não mais necessário. |
| `WORKER_DISCOVERY_ENABLED` | `true` | Habilitava discovery UDP de workers via `DISCOVER_WORKER`. |
| `WORKER_DISCOVERY_TIMEOUT` | `2` | Timeout em segundos para respostas de discovery de workers. |

---

## Exemplo Completo — Servidor (master)

```env
# servidor.py — máquina master
SERVER_UUID=SRV-MASTER
MASTER_PORT=8000
DISCOVERY_PORT=8001

# ── Supervisor de Métricas (Sprint 4) ──
SUPERVISOR_HOST=nuted-ia.dev
SUPERVISOR_PORT=443
SUPERVISOR_TLS=true
SUPERVISOR_SNI=nuted-ia.dev
SUPERVISOR_INTERVAL=10
SUPERVISOR_PAYLOAD_VERSION=sprint4-monitor
```

## Exemplo Completo — Worker 1

```env
SERVER_UUID=SRV-MASTER
MASTER_PORT=8000
DISCOVERY_PORT=8001

WORKER_UUID=WRK-01-ALPHA
WORKER_PORT=8000

HEARTBEAT_THRESHOLD=4
HEARTBEAT_INTERVAL=5

ELECTION_DELAY=30
ELECTION_COLLECT_TIMEOUT=5

WORKER_PEERS=
WORKER_DISCOVERY_ENABLED=true
WORKER_DISCOVERY_TIMEOUT=2
```

## Exemplo Completo — Worker 2

```env
SERVER_UUID=SRV-MASTER
MASTER_PORT=8000
DISCOVERY_PORT=8001

WORKER_UUID=WRK-02-BETA        ← UUID diferente do Worker 1
WORKER_PORT=8000

HEARTBEAT_THRESHOLD=4
HEARTBEAT_INTERVAL=5

ELECTION_DELAY=30
ELECTION_COLLECT_TIMEOUT=5
```

---

## Notas de Rede

### Broadcast `255.255.255.255` vs broadcast direcionado

Em redes corporativas e universitárias, o "limited broadcast" `255.255.255.255` é **tipicamente bloqueado** por switches e roteadores. O sistema detecta automaticamente o broadcast correto da subrede:

```
IP local: 10.62.206.23
Broadcast detectado: 10.62.206.255   ✅ (funciona em redes corporativas)
Broadcast genérico:  255.255.255.255 ❌ (bloqueado na maioria das redes)
```

Para sobrescrever manualmente:
```env
WORKER_BROADCAST_ADDRESS=10.62.206.255
```

### Firewall

As seguintes portas precisam estar **abertas** no firewall de cada máquina:

| Porta | Protocolo | Direção | Máquina |
|---|---|---|---|
| `MASTER_PORT` (8000) | TCP | Entrada | Master |
| `DISCOVERY_PORT` (8001) | UDP | Entrada | Master |
| `WORKER_PORT` (8000) | TCP | Entrada | Worker |
| `WORKER_PORT` (8000) | UDP | Entrada | Worker |
