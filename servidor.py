import os
import json
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

# Tempo (segundos) sem heartbeat para considerar um worker inativo
WORKER_STALE_TIMEOUT = int(os.getenv("WORKER_STALE_TIMEOUT", "30"))

# ── Registro de workers ───────────────────────────────────────────────────────
# Mantém todos os workers que enviaram heartbeat recentemente.
# Estrutura: { worker_uuid: {uuid, host, port, last_seen} }

registry      = {}
registry_lock = threading.Lock()


def _register_worker(uuid, host, port):
    """Atualiza ou adiciona um worker no registro."""
    with registry_lock:
        registry[uuid] = {
            "uuid":      uuid,
            "host":      host,
            "port":      int(port),
            "last_seen": time.time(),
        }


def _get_active_peers(exclude_uuid=None):
    """
    Retorna lista de workers ativos (vistos dentro de WORKER_STALE_TIMEOUT).
    Exclui o worker com exclude_uuid (normalmente o próprio solicitante).
    """
    now = time.time()
    with registry_lock:
        # Remove workers inativos
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


# ── Handler de conexão ────────────────────────────────────────────────────────

def handle_client(conn, addr):
    print(f"[MASTER] Conectado: {addr}")
    buffer = ""

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

                task = msg.get("TASK")
                print(f"[MASTER] Recebido de {addr}: {msg}")

                if task == "HEARTBEAT":
                    worker_uuid = msg.get("WORKER_UUID")
                    worker_host = msg.get("WORKER_HOST")
                    worker_port = msg.get("WORKER_PORT")

                    # Registra o worker se ele informou seus dados de contato
                    if worker_uuid and worker_host and worker_port:
                        _register_worker(worker_uuid, worker_host, worker_port)

                    # Devolve a lista de peers ativos para o worker usar na eleição
                    peers = _get_active_peers(exclude_uuid=worker_uuid)

                    response = {
                        "SERVER_UUID": SERVER_UUID,
                        "TASK":        "HEARTBEAT",
                        "RESPONSE":    "ALIVE",
                        "PEERS":       peers,   # ← lista de outros workers ativos
                    }
                    conn.send((json.dumps(response) + "\n").encode())

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