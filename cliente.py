import os
import socket
import json
import time
import threading
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

failed_heartbeat_count = 0
current_master = {"uuid": "MASTER", "ip": MASTER_IP, "port": MASTER_PORT, "free_space": 0}
election_in_progress = False
is_master = False
state_lock = threading.Lock()


def parse_worker_peers(peers_str):
    peers = []
    for item in peers_str.split(","):
        item = item.strip()
        if not item:
            continue
        if ":" not in item:
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
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            candidate = sock.getsockname()[0]
            if candidate and not candidate.startswith("127."):
                return candidate
    except Exception:
        pass

    try:
        hostname = socket.gethostname()
        candidate = socket.gethostbyname(hostname)
        if candidate and not candidate.startswith("127."):
            return candidate
    except Exception:
        pass

    return "127.0.0.1"

WORKER_PEERS_LIST = parse_worker_peers(WORKER_PEERS)
WORKER_HOST = detect_worker_host()
print(f"[CONFIG] WORKER_HOST definido como {WORKER_HOST}")


def compare_master_priority(candidate, current):
    candidate_key = (candidate.get("free_space", 0), candidate.get("uuid", ""))
    current_key = (current.get("free_space", 0), current.get("uuid", ""))
    return candidate_key > current_key


def process_new_master_announcement(master_host, master_port, master_uuid, master_free_space=0):
    global is_master
    candidate = {
        "uuid": master_uuid,
        "ip": master_host,
        "port": int(master_port),
        "free_space": int(master_free_space or 0)
    }

    with state_lock:
        if candidate["ip"] == WORKER_HOST and candidate["port"] == WORKER_PORT:
            if not is_master:
                current_master.update(candidate)
                become_master()
            else:
                current_master.update(candidate)
            return

        if compare_master_priority(candidate, current_master):
            if is_master:
                print("[ELECTION] Novo master com prioridade maior detectado; este worker deixará de ser master.")
                is_master = False
            current_master.update(candidate)
            print(f"[ELECTION] Atualizado master para {candidate['uuid']} em {candidate['ip']}:{candidate['port']}")
        else:
            print(f"[ELECTION] Anúncio de novo master ignorado; prioridade menor ou igual: {master_uuid}.")


def start_discovery_listener():
    discovery_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    discovery_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        discovery_socket.bind(("0.0.0.0", WORKER_PORT))
    except Exception as e:
        print(f"[DISCOVERY] Não foi possível bindear UDP em {WORKER_PORT}: {e}")
        return

    while True:
        try:
            data, addr = discovery_socket.recvfrom(1024)
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
            discovery_socket.sendto(json.dumps(reply).encode("utf-8"), addr)
        elif task == "NEW_MASTER":
            process_new_master_announcement(
                payload.get("MASTER_HOST"),
                payload.get("MASTER_PORT"),
                payload.get("MASTER_UUID"),
                payload.get("MASTER_FREE_SPACE", 0)
            )


def discover_peers():
    peers = set(WORKER_PEERS_LIST)

    if WORKER_DISCOVERY_ENABLED:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as discover_socket:
                discover_socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                discover_socket.settimeout(WORKER_DISCOVERY_TIMEOUT)
                payload = {
                    "TASK": "DISCOVER_WORKER",
                    "WORKER_UUID": WORKER_UUID
                }
                discover_socket.sendto(json.dumps(payload).encode("utf-8"), (WORKER_BROADCAST_ADDRESS, WORKER_PORT))

                start_time = time.time()
                while time.time() - start_time < WORKER_DISCOVERY_TIMEOUT:
                    try:
                        data, addr = discover_socket.recvfrom(1024)
                        response = json.loads(data.decode("utf-8"))
                    except socket.timeout:
                        break
                    except Exception:
                        continue

                    if response.get("TASK") != "DISCOVER_RESPONSE":
                        continue

                    if response.get("WORKER_UUID") == WORKER_UUID:
                        continue

                    host = response.get("WORKER_HOST")
                    port = response.get("WORKER_PORT")
                    if host and port:
                        peers.add((host, int(port)))
        except Exception:
            pass

    return [peer for peer in peers if not (peer[0] == WORKER_HOST and peer[1] == WORKER_PORT)]


def get_free_space():
    return shutil.disk_usage(".").free


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
                    message, buffer = buffer.split("\n", 1)
                    try:
                        messages.append(json.loads(message))
                    except json.JSONDecodeError:
                        continue
    except Exception:
        return []

    return messages


def handle_incoming_connection(conn, addr):
    print(f"[STATUS] Conexão recebida de {addr}")
    buffer = ""

    try:
        while True:
            data = conn.recv(1024).decode("utf-8")
            if not data:
                break

            buffer += data
            while "\n" in buffer:
                message, buffer = buffer.split("\n", 1)
                try:
                    payload = json.loads(message)
                except json.JSONDecodeError:
                    continue

                task = payload.get("TASK")

                if task == "HEARTBEAT":
                    if is_master:
                        response = {
                            "SERVER_UUID": WORKER_UUID,
                            "TASK": "HEARTBEAT",
                            "RESPONSE": "ALIVE"
                        }
                    else:
                        response = {
                            "SERVER_UUID": WORKER_UUID,
                            "TASK": "HEARTBEAT",
                            "RESPONSE": "NOT_MASTER"
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
                    process_new_master_announcement(
                        payload.get("MASTER_HOST"),
                        payload.get("MASTER_PORT"),
                        payload.get("MASTER_UUID"),
                        payload.get("MASTER_FREE_SPACE", 0)
                    )

                    ack = {
                        "TASK": "NEW_MASTER_ACK",
                        "RESPONSE": "RECEIVED"
                    }
                    conn.sendall((json.dumps(ack) + "\n").encode("utf-8"))

                else:
                    error_response = {
                        "TASK": "ERROR",
                        "RESPONSE": "UNKNOWN_TASK"
                    }
                    conn.sendall((json.dumps(error_response) + "\n").encode("utf-8"))
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
        thread = threading.Thread(target=handle_incoming_connection, args=(conn, addr), daemon=True)
        thread.start()


def become_master():
    global is_master
    with state_lock:
        is_master = True
        current_master["uuid"] = WORKER_UUID
        current_master["ip"] = WORKER_HOST
        current_master["port"] = WORKER_PORT
        current_master["free_space"] = get_free_space()
    print(f"[ELECTION] Worker {WORKER_UUID} foi eleito MASTER em {WORKER_HOST}:{WORKER_PORT}")


def broadcast_new_master(master_host, master_port, master_uuid, free_space):
    payload = {
        "TASK": "NEW_MASTER",
        "MASTER_HOST": master_host,
        "MASTER_PORT": master_port,
        "MASTER_UUID": master_uuid,
        "MASTER_FREE_SPACE": free_space
    }
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.sendto(json.dumps(payload).encode("utf-8"), (WORKER_BROADCAST_ADDRESS, WORKER_PORT))
    except Exception:
        pass


def announce_new_master(master_host, master_port):
    payload = {
        "TASK": "NEW_MASTER",
        "MASTER_HOST": master_host,
        "MASTER_PORT": master_port,
        "MASTER_UUID": WORKER_UUID,
        "MASTER_FREE_SPACE": get_free_space()
    }

    discovered_peers = set(WORKER_PEERS_LIST)
    discovered_peers.update(discover_peers())

    for host, port in discovered_peers:
        if host == WORKER_HOST and port == WORKER_PORT:
            continue

        send_message(host, port, payload, timeout=WORKER_STATUS_TIMEOUT)

    broadcast_new_master(master_host, master_port, WORKER_UUID, payload["MASTER_FREE_SPACE"])


def start_election():
    global election_in_progress, failed_heartbeat_count

    with state_lock:
        if election_in_progress:
            return
        election_in_progress = True

    print("[ELECTION] Iniciando eleição de master...")
    local_status = {
        "WORKER_UUID": WORKER_UUID,
        "WORKER_HOST": WORKER_HOST,
        "WORKER_PORT": WORKER_PORT,
        "FREE_SPACE": get_free_space()
    }
    statuses = [local_status]

    for host, port in discover_peers():
        if host == WORKER_HOST and port == WORKER_PORT:
            continue

        responses = send_message(host, port, {
            "TASK": "WORKER_STATUS",
            "WORKER_UUID": WORKER_UUID
        }, timeout=WORKER_STATUS_TIMEOUT)

        for response in responses:
            if response.get("TASK") == "WORKER_STATUS_RESPONSE":
                statuses.append(response)

    if not statuses:
        print("[ELECTION] Nenhum peer disponível para a eleição. Este worker se auto-elegerá.")
        winner = local_status
    else:
        winner = sorted(
            statuses,
            key=lambda item: (-item.get("FREE_SPACE", 0), item.get("WORKER_UUID", ""))
        )[0]

    print(f"[ELECTION] Candidatos: {statuses}")
    print(f"[ELECTION] Vencedor: {winner.get('WORKER_UUID')} ({winner.get('WORKER_HOST')}:{winner.get('WORKER_PORT')})")

    if winner.get("WORKER_UUID") == WORKER_UUID:
        become_master()
        announce_new_master(WORKER_HOST, WORKER_PORT)
    else:
        with state_lock:
            current_master["ip"] = winner.get("WORKER_HOST")
            current_master["port"] = int(winner.get("WORKER_PORT"))
            failed_heartbeat_count = 0
        print(f"[ELECTION] Novo master definido: {current_master['ip']}:{current_master['port']}")

    with state_lock:
        election_in_progress = False


def enviar_heartbeat():
    global failed_heartbeat_count

    with state_lock:
        if is_master:
            print("[HEARTBEAT] Este nó já é master; nenhum heartbeat será enviado.")
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
                    print(f"[RECEBIDO] Resposta do Master: {payload_resposta.get('RESPONSE')}")
                    if payload_resposta.get("RESPONSE") == "ALIVE":
                        with state_lock:
                            failed_heartbeat_count = 0
                            election_in_progress = False
                except json.JSONDecodeError:
                    print("Erro ao decodificar JSON do Master.")
        else:
            raise ConnectionError("Nenhuma resposta recebida do master.")

    except (ConnectionRefusedError, socket.timeout, ConnectionError, OSError) as e:
        with state_lock:
            failed_heartbeat_count += 1
            failures = failed_heartbeat_count
        print(f"[ERRO] Falha na conexão com Master {master_ip}:{master_port} ({e}). Contagem de falhas: {failures}/{HEARTBEAT_THRESHOLD}")

        if failures >= HEARTBEAT_THRESHOLD:
            start_election()
    except Exception as e:
        print(f"[ERRO] Falha na comunicação: {e}")
    finally:
        client_socket.close()
        print("[DESCONECTADO] Conexão encerrada.\n")


def start_worker():
    print("Iniciando o Worker com agendamento (Schedule)...")

    discovery_thread = threading.Thread(target=start_discovery_listener, daemon=True)
    discovery_thread.start()

    status_thread = threading.Thread(target=start_status_server, daemon=True)
    status_thread.start()

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
