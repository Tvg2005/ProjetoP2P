import os
import json
import queue
import socket
import threading
import time
from dotenv import load_dotenv

load_dotenv()

for _v in ["MASTER_IP", "MASTER_PORT", "SERVER_UUID"]:
    if _v not in os.environ:
        raise EnvironmentError(f"Variável ausente: {_v}")

# Bind em 0.0.0.0 para funcionar tanto no master original quanto no worker eleito
HOST        = "0.0.0.0"
PORT        = int(os.environ["MASTER_PORT"])
SERVER_UUID = os.environ["SERVER_UUID"]

WORKER_STALE_TIMEOUT = int(os.getenv("WORKER_STALE_TIMEOUT", "30"))

# ── Registro de workers ───────────────────────────────────────────────────────
# { worker_uuid: {uuid, host, port, last_seen} }

registry      = {}
registry_lock = threading.Lock()


def _register_worker(uuid, host, port):
    with registry_lock:
        registry[uuid] = {
            "uuid":      uuid,
            "host":      host,
            "port":      int(port),
            "last_seen": time.time(),
        }


def _get_active_peers(exclude_uuid=None):
    """Retorna lista de workers ativos, removendo os inativos."""
    now = time.time()
    with registry_lock:
        stale = [u for u, w in registry.items()
                 if now - w["last_seen"] > WORKER_STALE_TIMEOUT]
        for u in stale:
            print(f"[MASTER] Worker {u} removido do registro (inativo).")
            del registry[u]

        return [
            {"uuid": w["uuid"], "host": w["host"], "port": w["port"]}
            for u, w in registry.items()
            if u != exclude_uuid
        ]


# ── Fila de Tarefas ───────────────────────────────────────────────────────────
# Cada tarefa é um dict: {"id": str, "USER": str}

task_queue    = queue.Queue()
task_log      = []          # histórico de tarefas concluídas
task_log_lock = threading.Lock()

# Popula fila com tarefas iniciais de exemplo
_INITIAL_TASKS = [
    {"id": f"task-{i:03d}", "USER": f"user{i}"}
    for i in range(1, 21)
]
for _t in _INITIAL_TASKS:
    task_queue.put(_t)

print(f"[MASTER] Fila iniciada com {task_queue.qsize()} tarefas.")


def _log_task(worker_uuid, task_id, status, origin):
    """Registra conclusão de tarefa no log."""
    entry = {
        "worker":    worker_uuid,
        "task_id":   task_id,
        "status":    status,
        "origin":    origin,   # 'local' ou 'emprestado de <SERVER_UUID>'
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with task_log_lock:
        task_log.append(entry)
    print(f"[MASTER] LOG | Worker={worker_uuid} ({origin}) | "
          f"Task={task_id} | Status={status}")


# ── Handler de conexão ────────────────────────────────────────────────────────

def handle_client(conn, addr):
    print(f"[MASTER] Conectado: {addr}")
    buffer       = ""
    current_task = None   # tarefa atribuída nesta conexão (aguardando STATUS)

    try:
        while True:
            data = conn.recv(4096).decode()
            if not data:
                break

            buffer += data
            while "\n" in buffer:
                message, buffer = buffer.split("\n", 1)
                try:
                    msg = json.loads(message)
                except json.JSONDecodeError:
                    continue

                print(f"[MASTER] Recebido de {addr}: {msg}")

                # ── HEARTBEAT ─────────────────────────────────────────────────
                if msg.get("TASK") == "HEARTBEAT":
                    worker_uuid = msg.get("WORKER_UUID")
                    worker_host = msg.get("WORKER_HOST")
                    worker_port = msg.get("WORKER_PORT")

                    if worker_uuid and worker_host and worker_port:
                        _register_worker(worker_uuid, worker_host, worker_port)

                    peers = _get_active_peers(exclude_uuid=worker_uuid)

                    conn.send((json.dumps({
                        "SERVER_UUID": SERVER_UUID,
                        "TASK":        "HEARTBEAT",
                        "RESPONSE":    "ALIVE",
                        "PEERS":       peers,
                    }) + "\n").encode())

                # ── APRESENTAÇÃO DO WORKER (Payload 2.1 / 2.1b) ───────────────
                elif msg.get("WORKER") == "ALIVE":
                    worker_uuid = msg.get("WORKER_UUID", "?")
                    origin_uuid = msg.get("SERVER_UUID")   # presente se "emprestado"

                    if origin_uuid:
                        origin = f"emprestado de {origin_uuid}"
                    else:
                        origin = "local"

                    print(f"[MASTER] Worker {worker_uuid} se apresentou ({origin})")

                    # Distribui tarefa ou informa fila vazia
                    try:
                        current_task = task_queue.get_nowait()
                        print(f"[MASTER] Distribuindo {current_task['id']} "
                              f"→ {worker_uuid}")
                        # Payload 2.2
                        conn.send((json.dumps({
                            "TASK":    "QUERY",
                            "USER":    current_task["USER"],
                            "TASK_ID": current_task["id"],
                        }) + "\n").encode())

                    except queue.Empty:
                        print(f"[MASTER] Fila vazia para {worker_uuid}")
                        # Payload 2.3
                        conn.send((json.dumps({
                            "TASK": "NO_TASK",
                        }) + "\n").encode())
                        current_task = None

                # ── RESULTADO DO WORKER (Payload 2.4) ─────────────────────────
                elif msg.get("STATUS") in ("OK", "NOK") and msg.get("TASK") == "QUERY":
                    worker_uuid = msg.get("WORKER_UUID", "?")
                    status      = msg.get("STATUS")
                    task_id     = current_task["id"] if current_task else "?"

                    # Determina origem (local ou emprestado)
                    origin_uuid = msg.get("SERVER_UUID")
                    origin = f"emprestado de {origin_uuid}" if origin_uuid else "local"

                    _log_task(worker_uuid, task_id, status, origin)

                    # Payload 2.5 — ACK imediato
                    conn.send((json.dumps({
                        "STATUS": "ACK",
                    }) + "\n").encode())

                    current_task = None

                else:
                    conn.send((json.dumps({
                        "SERVER_UUID": SERVER_UUID,
                        "TASK":        "ERROR",
                        "RESPONSE":    "UNKNOWN_TASK",
                    }) + "\n").encode())

    except Exception as exc:
        print(f"[MASTER] Erro com {addr}: {exc}")
    finally:
        conn.close()
        print(f"[MASTER] Conexão encerrada: {addr}")


# ── Start ─────────────────────────────────────────────────────────────────────

def start_server():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, PORT))
    srv.listen()

    print(f"[MASTER] Servidor iniciado na porta {PORT}")
    print(f"[MASTER] UUID: {SERVER_UUID}")

    while True:
        conn, addr = srv.accept()
        threading.Thread(target=handle_client, args=(conn, addr),
                         daemon=True).start()


if __name__ == "__main__":
    start_server()