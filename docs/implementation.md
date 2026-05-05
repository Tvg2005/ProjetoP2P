# Implementação — Como Foi Feito

## `servidor.py` — Servidor Master

### Auto-detecção de IP

O servidor não usa IP fixo. Ao iniciar, detecta o próprio IP conectando um socket UDP sem enviar dados:

```python
def _detect_local_ip() -> str:
    for remote in [("8.8.8.8", 80), ("1.1.1.1", 80)]:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(remote)           # não envia dados
                return s.getsockname()[0]   # retorna IP da interface usada
        except Exception:
            pass
```

O truque: conectar um socket UDP a um endereço externo faz o SO escolher a interface de roteamento correta. `getsockname()` retorna o IP local usado para aquela rota — sem enviar nenhum pacote.

### Listener UDP de Discovery (`_start_discovery_listener`)

Escuta broadcasts `FIND_MASTER` na `DISCOVERY_PORT` (padrão: `MASTER_PORT + 1 = 8001`):

```python
# Roda em thread daemon
def _start_discovery_listener():
    sock.bind(("0.0.0.0", DISCOVERY_PORT))
    while True:
        data, addr = sock.recvfrom(4096)
        payload = json.loads(data.decode())
        if payload.get("TASK") != "FIND_MASTER":
            continue
        # Filtra por UUID se especificado
        requested_uuid = payload.get("SERVER_UUID", "")
        if requested_uuid and requested_uuid != SERVER_UUID:
            continue
        # Responde com IP real do servidor
        sock.sendto(json.dumps({
            "TASK":        "MASTER_FOUND",
            "SERVER_UUID": SERVER_UUID,
            "MASTER_IP":   SERVER_HOST,   # IP detectado automaticamente
            "MASTER_PORT": PORT,
        }).encode(), addr)
```

Detalhe importante: o bind é em `0.0.0.0` para receber broadcasts de qualquer interface, mas a resposta usa `SERVER_HOST` (IP real da máquina) para que o cliente possa conectar via TCP.

### Registro de Workers (`registry`)

O master mantém um dicionário `{ worker_uuid → {uuid, host, port, last_seen} }`. Workers são registrados a cada heartbeat e removidos se `last_seen` exceder `WORKER_STALE_TIMEOUT` (30s padrão):

```python
def _get_active_peers(exclude_uuid=None):
    now = time.time()
    stale = [u for u, w in registry.items()
             if now - w["last_seen"] > WORKER_STALE_TIMEOUT]
    for u in stale:
        del registry[u]  # limpa inativos
    return [{"uuid": w["uuid"], "host": w["host"], "port": w["port"]}
            for u, w in registry.items() if u != exclude_uuid]
```

### Fila de Tarefas (`task_queue`)

Usa `queue.Queue` (thread-safe) do Python padrão. Populada com 20 tarefas na inicialização. Distribuição FIFO: `task_queue.get_nowait()` — se vazia, lança `queue.Empty`.

---

## `cliente.py` — Worker

### Auto-detecção de Broadcast (`_detect_broadcast`)

Mesma lógica do servidor para detectar o IP local, depois deriva o broadcast da subrede `/24`:

```python
def _detect_broadcast(local_ip: str) -> str:
    env_val = os.getenv("WORKER_BROADCAST_ADDRESS", "")
    if env_val and env_val != "255.255.255.255":
        return env_val   # respeita configuração manual
    prefix = ".".join(local_ip.split(".")[:3])
    return f"{prefix}.255"   # 10.62.206.23 → 10.62.206.255
```

### Discovery do Master (`discover_master`)

Envia `FIND_MASTER` via broadcast UDP e aguarda `MASTER_FOUND` com o IP real do servidor:

```python
def discover_master(retries=3, timeout=3.0):
    request = json.dumps({
        "TASK":        "FIND_MASTER",
        "SERVER_UUID": ORIGINAL_SERVER_UUID,
        "WORKER_UUID": WORKER_UUID,
    }).encode()

    for attempt in range(1, retries + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.settimeout(timeout)
            s.sendto(request, (WORKER_BROADCAST_ADDRESS, DISCOVERY_PORT))

            deadline = time.time() + timeout
            while time.time() < deadline:
                data, addr = s.recvfrom(4096)
                resp = json.loads(data.decode())
                if (resp.get("TASK") == "MASTER_FOUND"
                        and resp.get("SERVER_UUID") == ORIGINAL_SERVER_UUID):
                    return (resp["MASTER_IP"], resp["MASTER_PORT"])
    return None
```

Chamada em `start_worker()` antes do primeiro heartbeat. Se falhar após `retries=5` tentativas, encerra com `SystemExit(1)` e mensagem de erro clara.

### Eleição via Broadcast (`start_election_broadcast`)

Função central da eleição. Roda em thread separada após `_trigger_election_with_delay`:

```python
def start_election_broadcast():
    # Guarda de entrada única
    with state_lock:
        if is_master or election_in_progress:
            return
        election_in_progress = True
        failed_hb = 0

    candidates = [{"WORKER_UUID": WORKER_UUID, "WORKER_HOST": WORKER_HOST,
                   "WORKER_PORT": WORKER_PORT, "FREE_SPACE": get_free_space()}]

    # Abre socket efêmero — respostas vêm de volta a esse socket
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.settimeout(ELECTION_COLLECT_TIMEOUT)
        s.sendto(json.dumps({
            "TASK": "ELECTION_BROADCAST", ...
        }).encode(), (WORKER_BROADCAST_ADDRESS, WORKER_PORT))

        # Coleta ELECTION_RESPONSE por ELECTION_COLLECT_TIMEOUT segundos
        deadline = time.time() + ELECTION_COLLECT_TIMEOUT
        while time.time() < deadline:
            data, _ = s.recvfrom(4096)
            resp = json.loads(data.decode())
            if resp.get("TASK") == "ELECTION_RESPONSE":
                candidates.append(resp)

    candidates.sort(key=_election_key)   # determinístico
    winner = candidates[0]

    if winner["WORKER_UUID"] == WORKER_UUID:
        _become_master()
        _notify_new_master()   # broadcast NEW_MASTER
    else:
        received = new_master_event.wait(timeout=NEW_MASTER_WAIT)
        if not received:
            threading.Thread(target=start_election_broadcast, daemon=True).start()
```

**Detalhe crítico: socket efêmero**
O ELECTION_BROADCAST é enviado de um socket efêmero (porta aleatória). Os workers respondem com `ELECTION_RESPONSE` para o endereço de origem — ou seja, voltam para a porta efêmera, não para o `WORKER_PORT` fixo. Isso evita conflito com o listener UDP permanente.

### Resposta a ELECTION_BROADCAST (UDP Listener)

O `_start_udp_listener` roda em thread permanente e responde a broadcasts de eleição:

```python
if task == "ELECTION_BROADCAST":
    sender_uuid = payload.get("WORKER_UUID")
    if sender_uuid == WORKER_UUID:
        continue  # ignora eco do próprio broadcast
    sock.sendto(json.dumps({
        "TASK":        "ELECTION_RESPONSE",
        "WORKER_UUID": WORKER_UUID,
        "WORKER_HOST": WORKER_HOST,
        "WORKER_PORT": WORKER_PORT,
        "FREE_SPACE":  get_free_space(),
    }).encode(), addr)   # addr = porta efêmera do remetente
```

### Delay antes da Eleição (`_trigger_election_with_delay`)

Chamada quando heartbeats falham. Aguarda `ELECTION_DELAY` segundos usando `threading.Event.wait()`:

```python
def _trigger_election_with_delay():
    new_master_event.clear()
    received = new_master_event.wait(timeout=ELECTION_DELAY)
    if received:
        return  # NEW_MASTER chegou durante a espera → cancela
    threading.Thread(target=start_election_broadcast, daemon=True).start()
```

O `new_master_event` é um `threading.Event` global. Se outro worker já elegeu um master e enviou `NEW_MASTER` via broadcast, o listener UDP chama `_accept_new_master()` que faz `new_master_event.set()`, cancelando o delay.

### Tornar-se Master (`_become_master` + `_notify_new_master`)

```python
def _become_master():
    with state_lock:
        is_master = True
        current_master.update({"uuid": WORKER_UUID, "ip": WORKER_HOST, "port": WORKER_PORT})

    # Sobe servidor.py como subprocesso com o UUID deste worker
    env = os.environ.copy()
    env.update({"MASTER_IP": WORKER_HOST, "MASTER_PORT": str(WORKER_PORT),
                "SERVER_UUID": WORKER_UUID})
    master_proc = subprocess.Popen(["python", "servidor.py"], env=env)

def _notify_new_master():
    send_udp({"TASK": "NEW_MASTER", "MASTER_HOST": WORKER_HOST,
              "MASTER_PORT": WORKER_PORT, "MASTER_UUID": WORKER_UUID, ...})
```

`servidor.py` sobe como processo filho, herdando as variáveis de ambiente atualizadas. O listener UDP de discovery do novo servidor passa a responder broadcasts `FIND_MASTER` com o novo IP.

---

## Threading e Concorrência

### Threads por Worker

| Thread | Função | Tipo |
|---|---|---|
| Principal | `start_worker()`, loop `schedule` | Permanente |
| UDP Listener | `_start_udp_listener()` | Daemon permanente |
| TCP Status Server | `_start_status_server()` | Daemon permanente |
| Heartbeat | via `schedule` → `enviar_heartbeat()` | Periódica |
| Tarefas | via `schedule` → `pedir_tarefa()` | Periódica |
| Eleição | `_trigger_election_with_delay()` → `start_election_broadcast()` | Sob demanda |

### `state_lock` (threading.Lock)

Protege as variáveis de estado global compartilhadas:
- `failed_hb` — contador de heartbeats falhos
- `is_master` — se este nó é o master atual
- `election_in_progress` — flag para evitar eleições simultâneas
- `current_master` — dicionário `{uuid, ip, port}` do master atual

### `new_master_event` (threading.Event)

Sincroniza `_trigger_election_with_delay()` com `_accept_new_master()`:
- `.clear()` → reseta antes da espera
- `.wait(timeout)` → bloqueia até evento ou timeout
- `.set()` → disparado pelo UDP listener quando NEW_MASTER chega

---

## Código Legado Comentado

O código das versões anteriores foi comentado (não deletado) com marcador `# [LEGADO]`:

| Bloco comentado | Localização | Substituído por |
|---|---|---|
| `WORKER_PEERS_STR` | linha ~46 | Broadcast auto-discovery |
| `_parse_peers()` | linha ~66 | Não necessário |
| `STATIC_PEERS` | linha ~115 | `known_peers = []` |
| `_update_known_peers()` | linha ~250 | Peers não são mais aprendidos |
| `get_peers()` | linha ~270 | `start_election_broadcast()` usa broadcast |
| `_query_status()` + `start_election()` | linha ~313 | `start_election_broadcast()` |
| Loop TCP em `_notify_new_master()` | linha ~450 | `send_udp()` broadcast |
| `_update_known_peers()` em heartbeat | linha ~649 | Comentado |
