# Implementação — Como Foi Feito

## Visão Geral

O sistema está dividido em dois componentes principais:
- `servidor.py` — servidor master que gerencia workers, tarefas e negociação entre masters
- `cliente.py` — worker que descobre o master, envia heartbeat, executa tarefas e participa de eleição

A comunicação combina TCP para controle confiável e UDP para discovery e eleição.

---

## `servidor.py` — Master

### Auto-detecção de IP

O servidor detecta seu IP local abrindo um socket UDP de saída sem enviar pacotes. Isso retorna a interface correta usada para rotear tráfego para a internet local.

### Descoberta do master

O master escuta broadcasts UDP em `DISCOVERY_PORT` (`MASTER_PORT + 1`). Quando recebe `FIND_MASTER` com o `SERVER_UUID` correto, responde com `MASTER_FOUND` e o IP/porta reais do servidor.

### Registro de Workers e Heartbeat

O master mantém um registro de workers ativos e atualiza o estado a cada `HEARTBEAT`:
- registra `WORKER_UUID`, `WORKER_HOST`, `WORKER_PORT`
- retorna `RESPONSE: ALIVE`
- devolve a lista de peers ativos para permitir descobertas legadas

### Fila de tarefas

`task_queue` é uma fila FIFO com 20 tarefas iniciais. Ao receber um worker válido, o master distribui a próxima tarefa disponível ou responde `TASK: NO_TASK` se a fila estiver vazia.

### Resultado e ACK

Quando o worker envia `STATUS: OK` ou `STATUS: NOK`, o master:
- registra o resultado no `task_log`
- identifica se o worker é local ou emprestado via `SERVER_UUID`
- devolve `STATUS: ACK`

### Protocolo master-to-master

O servidor implementa o protocolo de negociação de capacidade:
- `request_help` — master saturado solicita workers a vizinhos
- `response_accepted` / `response_rejected` — vizinho responde com oferta ou motivo de recusa
- `command_redirect` — master vizinho instrui workers a se reportarem para o master saturado
- `command_release` — master receptor devolve o worker ao master original
- `notify_worker_returned` — master receptor notifica o master de origem

### Monitor de carga

Uma thread periódica (`_load_monitor`) avalia:
- se o master está acima de `MASTER_CAPACITY` → solicita ajuda
- se o master normalizou abaixo de `MASTER_RELEASE_THRESHOLD` → libera workers emprestados

### Concurrency e threading

Cada conexão TCP é tratada em `handle_client()` com loop de leitura contínua baseada em `\n`. Mensagens JSON são processadas em tempo real e cada socket é fechado somente ao término da conexão.

---

## `cliente.py` — Worker

### Variáveis de ambiente e broadcast

O worker lê:
- `MASTER_PORT`
- `WORKER_PORT`
- `SERVER_UUID`
- `WORKER_BROADCAST_ADDRESS` opcional

Ele auto-detecta o endereço de broadcast da subrede `/24` quando `WORKER_BROADCAST_ADDRESS` não é fornecido.

### Descoberta do master

`discover_master()` envia `FIND_MASTER` via UDP broadcast e aguarda `MASTER_FOUND`. Isso elimina a necessidade de `MASTER_IP` fixo no `.env`.

### Heartbeat e apresentação

O worker envia `HEARTBEAT` periódico para o master e, no ciclo de tarefa, apresenta-se com `WORKER: ALIVE`.
Se o master original não corresponde a `ORIGINAL_SERVER_UUID`, o worker marca-se como emprestado e inclui `SERVER_UUID` no payload.

### Eleição via broadcast

Ao detectar falha do master, o worker executa `_trigger_election_with_delay()`:
- aguarda `ELECTION_DELAY` para cancelar caso `NEW_MASTER` chegue
- inicia `start_election_broadcast()` se não houver novo master
- envia `ELECTION_BROADCAST` para a subrede
- coleta `ELECTION_RESPONSE` por `ELECTION_COLLECT_TIMEOUT`
- ordena candidatos por `(-free_space, WORKER_UUID)`
- vencedor vira master e envia `NEW_MASTER`

### Tornar-se master

O worker vencedor invoca `_become_master()`:
- atualiza estado local
- encerra servidor de status local
- inicia `servidor.py` como subprocesso
- aguarda bind na porta master

Após subir, `_notify_new_master()` envia `NEW_MASTER` por UDP broadcast.

### Redirecionamento e retorno

O worker processa comandos recebidos pelo servidor TCP de status:
- `command_redirect` → define `redirect_target` e reconecta ao novo master como `register_temporary_worker`
- `command_release` → define `return_target` e reconecta ao master original

`_connect_to_master(..., register_temporary=True)` envia `register_temporary_worker` com `original_master_address`.

### Listener UDP

O worker mantém um listener UDP permanente em `WORKER_PORT` para:
- responder a `ELECTION_BROADCAST`
- processar `NEW_MASTER`
- responder a `DISCOVER_WORKER` (compatibilidade)

### Estado e sincronização

O código usa `state_lock` para proteger:
- `is_master`
- `election_in_progress`
- `current_master`
- `redirect_target` / `return_target`

`new_master_event` sincroniza espera de eleição com a chegada de `NEW_MASTER`.

---

## Padrões de protocolo

### Delimitador de mensagem TCP

- Cada objeto JSON termina com `\n`
- O receptor acumula bytes até encontrar `\n`
- Esta abordagem garante que múltiplas mensagens no mesmo socket sejam lidas corretamente

### Campos obrigatórios e extensibilidade

- Campos desconhecidos são ignorados para suportar futuras extensões
- Campos obrigatórios são validados antes do processamento
- Valores de controle são tratados em caixa alta (`ALIVE`, `QUERY`, `NO_TASK`, `OK`, `NOK`, `ACK`)

### Envelope master-to-master

Exemplo genérico:
```json
{
  "type": "request_help",
  "request_id": "uuid_unico_para_rastreio",
  "payload": { ... }
}
```

---

## Sprint 4 — Supervisor de Métricas e Telemetria

### Coleta de métricas de hardware e SO

O master coleta métricas usando a biblioteca `psutil` com fallbacks robustos para a biblioteca padrão (`shutil`, `os` e `platform`):
- **CPU e RAM:** O master utiliza `psutil.cpu_percent` e `psutil.virtual_memory` para obter o consumo instantâneo.
- **Emulação de Load Average:** Como o Windows não suporta `os.getloadavg()`, o master mantém um histórico deslizante de uso de CPU a cada 10s e calcula a carga média equivalente multiplicando pelo número de processadores lógicos.
- **Espaço em Disco:** Obtido via `shutil.disk_usage()`, funcionando nativamente em qualquer SO.

### Telemetria da Farm e Conectividade P2P

- **Estado dos Workers:** O dicionário `registry` mantém o registro dos workers. Workers inativos (stale) não são deletados imediatamente para que o supervisor possa reportar `workers_failed`. O master filtra workers inativos nas buscas de fila e negociações de empréstimo.
- **Liveness de Vizinhos:** Uma thread de monitoramento (`_neighbor_monitor`) tenta se conectar a cada nó em `MASTER_NEIGHBORS` a cada 10s para validar o status (`available` ou `unavailable`) e registrar o último timestamp de atividade.
- **Idade da Tarefa mais Antiga:** Calculada verificando o menor timestamp `created_at` nas tarefas pendentes em `task_queue` ou executando em `running_tasks`.
- **Prevenção de Perda de Tarefa:** Ao despachar uma tarefa, ela é registrada em `running_tasks`. Se o socket de um worker cair inesperadamente, o master intercepta a falha e reinsere a tarefa na fila para reprocessamento.

### Comunicação TLS e Thread de Envio

Uma thread separada (`_supervisor_sender`) coleta os dados e envia-os a cada 10s. A conexão utiliza o módulo padrão `ssl` do Python para envelopar o socket TCP comum de forma segura (com suporte opcional a TLS e SNI). O envio é assíncrono para garantir que lentidão de rede externa ou instabilidade do supervisor não afetem a resposta a workers locais e negociações P2P.

---

## Status da implementação

O código atual implementa todos os requisitos definidos nas Sprints 1 a 4:
- Descoberta de master via UDP broadcast e heartbeat TCP
- Distribuição confiável de tarefas e ciclo completo de processamento (QUERY -> STATUS -> ACK)
- Tolerância a falhas com eleição automática de novo master via Bully Algorithm adaptado
- Negociação P2P colaborativa (empréstimo de workers, redirecionamento e devolução controlada)
- Telemetria de hardware e estado em tempo real integrada ao Supervisor Dashboard via conexões seguras TCP/TLS

