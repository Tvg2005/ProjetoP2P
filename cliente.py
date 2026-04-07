import os
import socket
import json
import time
import random
import threading
import subprocess
import shutil
import schedule
from dotenv import load_dotenv

load_dotenv()

required_env = ["MASTER_IP", "MASTER_PORT", "WORKER_UUID", "WORKER_PORT"]
for name in required_env:
    if name not in os.environ:
        raise EnvironmentError(f"Missing required environment variable: {name}")

MASTER_IP = os.environ["MASTER_IP"]
MASTER_PORT = int(os.environ["MASTER_PORT"])
WORKER_UUID = os.environ["WORKER_UUID"]
WORKER_HOST = os.getenv("WORKER_HOST")
WORKER_PORT = int(os.environ["WORKER_PORT"])
WORKER_PEERS = os.getenv("WORKER_PEERS", "")
WORKER_DISCOVERY_ENABLED = os.getenv("WORKER_DISCOVERY_ENABLED", "true").lower() in ("1", "true", "yes")
WORKER_BROADCAST_ADDRESS = os.getenv("WORKER_BROADCAST_ADDRESS", "255.255.255.255")
WORKER_DISCOVERY_TIMEOUT = int(os.getenv("WORKER_DISCOVERY_TIMEOUT", "2"))
HEARTBEAT_THRESHOLD = int(os.getenv("HEARTBEAT_THRESHOLD", "4"))
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", "5"))
WORKER_STATUS_TIMEOUT = int(os.getenv("WORKER_STATUS_TIMEOUT", "3"))

# ── Estado global ────────────────────────────────────────────────────────────
failed_heartbeat_count = 0
current_master = {"uuid": "MASTER", "ip": MASTER_IP, "port": MASTER_PORT, "free_space": 0}
election_in_progress = False
is_master = False
state_lock = threading.Lock()
master_process = None          # subprocesso do servidor.py quando este nó é master


# ── Utilitários ──────────────────────────────────────────────────────────────

def parse_worker_peers(peers_str):
    peers = []
    for item in peers_str.split(","):
        item = item.strip()
        if not item or ":" not in item:
            continue
        host, port = item.split(":", 1)
        try:
            peers.append((host.strip(), int(port.strip())))
        except ValueError:
            continue
    return peers


def detect_worker_host() -> str:
    if WORKER_HOST:
        return WORKER_HOST
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            candidate = s.getsockname()[0]
            if candidate and not candidate.startswith("127."):
                return candidate
    except Exception:
        pass
    try:
        candidate = socket.gethostbyname(socket.gethostname())
        if candidate and not candidate.startswith("127."):
            return candidate
    except Exception:
        pass
    return "127.0.0.1"


WORKER_PEERS_LIST = parse_worker_peers(WORKER_PEERS)
WORKER_HOST = detect_worker_host()
print(f"[CONFIG] WORKER_HOST definido como {WORKER_HOST}")


def get_free_space():
    return shutil.disk_usage(".").free


# ── Rede: send / broadcast ───────────────────────────────────────────────────

def send_message(host, port, payload, timeout=3):
    messages = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect((host, port))
            sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))

            buffer = ""
            while True:
                try:
                    data = sock.recv(1024).decode("utf-8")
                except socket.timeout:
                    break
                if not data:
                    break
                buffer += data
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    try:
                        messages.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except Exception:
        return []
    return messages


def broadcast_udp(payload):
    """Envia payload via UDP broadcast na porta dos workers."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.sendto(json.dumps(payload).encode("utf-8"),
                        (WORKER_BROADCAST_ADDRESS, WORKER_PORT))
    except Exception:
        pass


# ── Descoberta de peers ──────────────────────────────────────────────────────

def discover_peers():
    peers = set(WORKER_PEERS_LIST)

    if WORKER_DISCOVERY_ENABLED:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                s.settimeout(WORKER_DISCOVERY_TIMEOUT)
                s.sendto(json.dumps({
                    "TASK": "DISCOVER_WORKER",
                    "WORKER_UUID": WORKER_UUID
                }).encode("utf-8"), (WORKER_BROADCAST_ADDRESS, WORKER_PORT))

                deadline = time.time() + WORKER_DISCOVERY_TIMEOUT
                while time.time() < deadline:
                    try:
                        data, _ = s.recvfrom(1024)
                        resp = json.loads(data.decode("utf-8"))
                    except socket.timeout:
                        break
                    except Exception:
                        continue
                    if resp.get("TASK") != "DISCOVER_RESPONSE":
                        continue
                    if resp.get("WORKER_UUID") == WORKER_UUID:
                        continue
                    host = resp.get("WORKER_HOST")
                    port = resp.get("WORKER_PORT")
                    if host and port:
                        peers.add((host, int(port)))
        except Exception:
            pass

    return [(h, p) for h, p in peers
            if not (h == WORKER_HOST and p == WORKER_PORT)]


def start_discovery_listener():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("0.0.0.0", WORKER_PORT))
    except Exception as e:
        print(f"[DISCOVERY] Não foi possível bindear UDP em {WORKER_PORT}: {e}")
        return

    while True:
        try:
            data, addr = sock.recvfrom(1024)
            payload = json.loads(data.decode("utf-8"))
        except Exception:
            continue

        task = payload.get("TASK")

        if task == "DISCOVER_WORKER":
            if payload.get("WORKER_UUID") == WORKER_UUID:
                continue
            reply = {
                "TASK": "DISCOVER_RESPONSE",
                "WORKER_UUID": WORKER_UUID,
                "WORKER_HOST": WORKER_HOST,
                "WORKER_PORT": WORKER_PORT
            }
            sock.sendto(json.dumps(reply).encode("utf-8"), addr)

        elif task == "NEW_MASTER":
            # Outro nó ganhou a eleição – atualiza sem disputar
            _accept_new_master(
                payload.get("MASTER_HOST"),
                payload.get("MASTER_PORT"),
                payload.get("MASTER_UUID"),
                payload.get("MASTER_FREE_SPACE", 0)
            )


# ── Status server (TCP) ──────────────────────────────────────────────────────

def handle_incoming_connection(conn, addr):
    buffer = ""
    try:
        while True:
            data = conn.recv(1024).decode("utf-8")
            if not data:
                break
            buffer += data
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue

                task = payload.get("TASK")

                if task == "HEARTBEAT":
                    with state_lock:
                        am_master = is_master
                    response = {
                        "SERVER_UUID": WORKER_UUID,
                        "TASK": "HEARTBEAT",
                        "RESPONSE": "ALIVE" if am_master else "NOT_MASTER"
                    }
                    conn.sendall((json.dumps(response) + "\n").encode("utf-8"))

                elif task == "WORKER_STATUS":
                    response = {
                        "TASK": "WORKER_STATUS_RESPONSE",
                        "WORKER_UUID": WORKER_UUID,
                        "WORKER_HOST": WORKER_HOST,
                        "WORKER_PORT": WORKER_PORT,
                        "FREE_SPACE": get_free_space()
                    }
                    conn.sendall((json.dumps(response) + "\n").encode("utf-8"))

                elif task == "NEW_MASTER":
                    _accept_new_master(
                        payload.get("MASTER_HOST"),
                        payload.get("MASTER_PORT"),
                        payload.get("MASTER_UUID"),
                        payload.get("MASTER_FREE_SPACE", 0)
                    )
                    conn.sendall((json.dumps({"TASK": "NEW_MASTER_ACK", "RESPONSE": "RECEIVED"}) + "\n").encode("utf-8"))

                else:
                    conn.sendall((json.dumps({"TASK": "ERROR", "RESPONSE": "UNKNOWN_TASK"}) + "\n").encode("utf-8"))

    except Exception as e:
        print(f"[STATUS] Erro ao processar conexão de {addr}: {e}")
    finally:
        conn.close()


def start_status_server():
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind((WORKER_HOST, WORKER_PORT))
    server_socket.listen()
    print(f"[STATUS] Servidor de status iniciado em {WORKER_HOST}:{WORKER_PORT}")
    while True:
        conn, addr = server_socket.accept()
        threading.Thread(target=handle_incoming_connection,
                         args=(conn, addr), daemon=True).start()


# ── Lógica de master ─────────────────────────────────────────────────────────

def _accept_new_master(master_host, master_port, master_uuid, master_free_space=0):
    """
    Aceita um anúncio de novo master vindo de outro nó.
    Deve ser chamado FORA do state_lock para evitar deadlock.
    """
    global is_master, election_in_progress, failed_heartbeat_count

    if not master_host or not master_port or not master_uuid:
        return

    print(f"[ELECTION] Anúncio recebido: novo master é {master_uuid} em {master_host}:{master_port}")

    with state_lock:
        is_master = False
        election_in_progress = False
        failed_heartbeat_count = 0
        current_master["uuid"] = master_uuid
        current_master["ip"] = master_host
        current_master["port"] = int(master_port)
        current_master["free_space"] = int(master_free_space or 0)

    print(f"[ELECTION] Este nó agora aponta para master {master_host}:{master_port}")


def _become_master():
    """Promove este nó a master e lança servidor.py como subprocesso."""
    global is_master, master_process

    is_master = True
    current_master["uuid"] = WORKER_UUID
    current_master["ip"] = WORKER_HOST
    current_master["port"] = WORKER_PORT
    current_master["free_space"] = get_free_space()

    print(f"[ELECTION] *** Worker {WORKER_UUID} foi eleito MASTER em {WORKER_HOST}:{WORKER_PORT} ***")

    # Lança servidor.py passando as variáveis necessárias via ambiente
    env = os.environ.copy()
    env["MASTER_IP"] = WORKER_HOST
    env["MASTER_PORT"] = str(WORKER_PORT)
    env["SERVER_UUID"] = WORKER_UUID

    script_dir = os.path.dirname(os.path.abspath(__file__))
    servidor_path = os.path.join(script_dir, "servidor.py")

    if os.path.exists(servidor_path):
        try:
            master_process = subprocess.Popen(
                ["python", servidor_path],
                env=env,
                cwd=script_dir
            )
            print(f"[ELECTION] servidor.py iniciado (PID {master_process.pid})")
        except Exception as e:
            print(f"[ELECTION] Falha ao iniciar servidor.py: {e}")
    else:
        print(f"[ELECTION] AVISO: servidor.py não encontrado em {servidor_path}")


def _announce_new_master():
    """Anuncia via TCP para todos os peers e via UDP broadcast que este nó é o master."""
    payload = {
        "TASK": "NEW_MASTER",
        "MASTER_HOST": WORKER_HOST,
        "MASTER_PORT": WORKER_PORT,
        "MASTER_UUID": WORKER_UUID,
        "MASTER_FREE_SPACE": get_free_space()
    }

    peers = set(WORKER_PEERS_LIST)
    peers.update(discover_peers())

    for host, port in peers:
        if host == WORKER_HOST and port == WORKER_PORT:
            continue
        print(f"[ELECTION] Notificando peer {host}:{port}")
        send_message(host, port, payload, timeout=WORKER_STATUS_TIMEOUT)

    # Também envia via broadcast UDP
    broadcast_udp(payload)


# ── Eleição coordenada ───────────────────────────────────────────────────────

def start_election():
    """
    Algoritmo de eleição coordenada:
    1. Espera um tempo aleatório (backoff) proporcional ao espaço livre INVERTIDO
       → nó com MAIS espaço livre espera MENOS e dispara primeiro.
    2. Durante a espera, qualquer NEW_MASTER recebido cancela a eleição local.
    3. Após o backoff, verifica se ainda não há master – se sim, se elege e anuncia.
    """
    global election_in_progress, failed_heartbeat_count

    with state_lock:
        if election_in_progress or is_master:
            return
        election_in_progress = True
        failed_heartbeat_count = 0

    print("[ELECTION] Master parece offline. Iniciando processo de eleição...")

    # Backoff inversamente proporcional ao espaço livre:
    # quem tem mais espaço livre espera menos e age primeiro.
    free = get_free_space()
    # Normaliza: 0-3 segundos de backoff (mais espaço → menos espera)
    # Usa um jitter leve para desempate entre nós com espaço idêntico
    max_free_gb = 500 * 1024 ** 3  # referência de 500 GB
    ratio = max(0.0, min(1.0, free / max_free_gb))
    backoff = (1.0 - ratio) * 3.0 + random.uniform(0, 0.5)
    print(f"[ELECTION] Backoff calculado: {backoff:.2f}s (free_space={free // (1024**3)} GB)")

    time.sleep(backoff)

    # Depois do backoff, verifica se outro nó já se elegeu
    with state_lock:
        if not election_in_progress:
            # Outro nó anunciou NEW_MASTER durante o backoff → desiste
            print("[ELECTION] Outro nó já foi eleito durante o backoff. Eleição cancelada.")
            return
        if is_master:
            election_in_progress = False
            return

    # Ainda não há master → este nó se elege
    with state_lock:
        election_in_progress = False

    _become_master()
    _announce_new_master()


# ── Heartbeat ────────────────────────────────────────────────────────────────

def enviar_heartbeat():
    global failed_heartbeat_count

    with state_lock:
        if is_master:
            # Masters não enviam heartbeat para si mesmos
            return
        master_ip = current_master["ip"]
        master_port = current_master["port"]

    client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client_socket.settimeout(5)

    try:
        client_socket.connect((master_ip, master_port))
        print(f"[CONECTADO] Conectado ao Master {master_ip}:{master_port}")

        payload_envio = {
            "SERVER_UUID": WORKER_UUID,
            "WORKER_UUID": WORKER_UUID,
            "TASK": "HEARTBEAT"
        }
        client_socket.sendall((json.dumps(payload_envio) + "\n").encode("utf-8"))
        print("[ENVIADO] Heartbeat enviado.")

        buffer = ""
        data = client_socket.recv(1024).decode("utf-8")

        if data:
            buffer += data
            while "\n" in buffer:
                mensagem_str, buffer = buffer.split("\n", 1)
                try:
                    payload_resposta = json.loads(mensagem_str)
                    response_val = payload_resposta.get("RESPONSE")
                    print(f"[RECEBIDO] Resposta do Master: {response_val}")
                    if response_val == "ALIVE":
                        with state_lock:
                            failed_heartbeat_count = 0
                            election_in_progress = False
                    elif response_val == "NOT_MASTER":
                        # Conectamos a um nó que não é mais master; força re-eleição
                        print("[HEARTBEAT] Resposta NOT_MASTER – iniciando eleição.")
                        with state_lock:
                            failed_heartbeat_count = HEARTBEAT_THRESHOLD
                except json.JSONDecodeError:
                    print("Erro ao decodificar JSON do Master.")
        else:
            raise ConnectionError("Nenhuma resposta recebida do master.")

    except (ConnectionRefusedError, socket.timeout, ConnectionError, OSError) as e:
        with state_lock:
            failed_heartbeat_count += 1
            failures = failed_heartbeat_count
        print(f"[ERRO] Falha na conexão com Master {master_ip}:{master_port} ({e}). "
              f"Contagem de falhas: {failures}/{HEARTBEAT_THRESHOLD}")

        if failures >= HEARTBEAT_THRESHOLD:
            threading.Thread(target=start_election, daemon=True).start()

    except Exception as e:
        print(f"[ERRO] Falha na comunicação: {e}")
    finally:
        client_socket.close()
        print("[DESCONECTADO] Conexão encerrada.\n")


# ── Entry point ──────────────────────────────────────────────────────────────

def start_worker():
    print("Iniciando o Worker com agendamento (Schedule)...")

    threading.Thread(target=start_discovery_listener, daemon=True).start()
    threading.Thread(target=start_status_server, daemon=True).start()

    enviar_heartbeat()
    schedule.every(HEARTBEAT_INTERVAL).seconds.do(enviar_heartbeat)

    try:
        while True:
            schedule.run_pending()
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[ENCERRANDO] Worker desligado pelo usuário.")


if __name__ == "__main__":
    start_worker()
