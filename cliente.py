"""
Worker P2P com eleição em DUAS FASES (Two-Phase Deterministic Election)

Algoritmo:
  FASE 1 – CAMPAIGN (janela fixa de ELECTION_WINDOW segundos):
    • Qualquer nó que detecta a queda do master transmite via UDP broadcast
      e via TCP para todos os peers conhecidos uma mensagem ELECTION com seus
      dados (UUID, host, port, free_space).
    • Cada nó que recebe ELECTION entra na eleição (se ainda não estiver) e
      registra o candidato.

  FASE 2 – DECIDE (ao final da janela):
    • Todos os nós que participaram ordenam os candidatos pela mesma chave
      determinística: (-free_space, uuid).
    • O vencedor é [0] da lista ordenada.
    • Se este nó for o vencedor → vira master e anuncia NEW_MASTER.
    • Caso contrário → aguarda o anúncio NEW_MASTER do vencedor com timeout.
      Se não receber → inicia nova eleição.

Garantia: como todos os nós coletam (quase) as mesmas candidaturas e aplicam
a mesma chave, chegam ao mesmo vencedor de forma independente.
"""

import os
import socket
import json
import time
import threading
import subprocess
import shutil
import schedule
from dotenv import load_dotenv

load_dotenv()

# ── Variáveis de ambiente ────────────────────────────────────────────────────
required_env = ["MASTER_IP", "MASTER_PORT", "WORKER_UUID", "WORKER_PORT"]
for name in required_env:
    if name not in os.environ:
        raise EnvironmentError(f"Missing required environment variable: {name}")

MASTER_IP             = os.environ["MASTER_IP"]
MASTER_PORT           = int(os.environ["MASTER_PORT"])
WORKER_UUID           = os.environ["WORKER_UUID"]
_WORKER_HOST_ENV      = os.getenv("WORKER_HOST")
WORKER_PORT           = int(os.environ["WORKER_PORT"])
WORKER_PEERS          = os.getenv("WORKER_PEERS", "")
WORKER_DISCOVERY_ENABLED  = os.getenv("WORKER_DISCOVERY_ENABLED", "true").lower() in ("1","true","yes")
WORKER_BROADCAST_ADDRESS  = os.getenv("WORKER_BROADCAST_ADDRESS", "255.255.255.255")
WORKER_DISCOVERY_TIMEOUT  = int(os.getenv("WORKER_DISCOVERY_TIMEOUT", "2"))
HEARTBEAT_THRESHOLD   = int(os.getenv("HEARTBEAT_THRESHOLD", "4"))
HEARTBEAT_INTERVAL    = int(os.getenv("HEARTBEAT_INTERVAL", "5"))
WORKER_STATUS_TIMEOUT = int(os.getenv("WORKER_STATUS_TIMEOUT", "3"))

# Janela para coletar candidaturas durante a eleição (segundos)
ELECTION_WINDOW = int(os.getenv("ELECTION_WINDOW", "4"))
# Timeout para aguardar o anúncio NEW_MASTER após decidir o vencedor
NEW_MASTER_WAIT = int(os.getenv("NEW_MASTER_WAIT", "6"))

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
    if _WORKER_HOST_ENV:
        return _WORKER_HOST_ENV
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            c = s.getsockname()[0]
            if c and not c.startswith("127."):
                return c
    except Exception:
        pass
    try:
        c = socket.gethostbyname(socket.gethostname())
        if c and not c.startswith("127."):
            return c
    except Exception:
        pass
    return "127.0.0.1"


WORKER_PEERS_LIST = parse_worker_peers(WORKER_PEERS)
WORKER_HOST = detect_worker_host()
print(f"[CONFIG] WORKER_HOST={WORKER_HOST}  UUID={WORKER_UUID}")


def get_free_space():
    return shutil.disk_usage(".").free


# ── Estado global ────────────────────────────────────────────────────────────
state_lock            = threading.Lock()
failed_heartbeat_count = 0
is_master             = False
master_process        = None          # subprocess do servidor.py se este nó for master

# Aponta para o master atual (começa apontando para o master original)
current_master = {
    "uuid":       "MASTER",
    "ip":         MASTER_IP,
    "port":       MASTER_PORT,
    "free_space": 0,
}

# Estado da eleição
election_in_progress  = False
election_candidates   = {}            # uuid → {WORKER_UUID, WORKER_HOST, WORKER_PORT, FREE_SPACE}
election_lock         = threading.Lock()
new_master_event      = threading.Event()   # sinalizado quando NEW_MASTER é recebido


# ── Rede helpers ─────────────────────────────────────────────────────────────

def send_message(host, port, payload, timeout=3):
    """Envia payload JSON via TCP e retorna lista de respostas JSON."""
    messages = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect((host, port))
            sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            buf = ""
            while True:
                try:
                    chunk = sock.recv(4096).decode("utf-8")
                except socket.timeout:
                    break
                if not chunk:
                    break
                buf += chunk
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    try:
                        messages.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    except Exception:
        pass
    return messages


def send_udp(payload, addr=None):
    """Envia payload JSON via UDP. Se addr=None faz broadcast."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            if addr is None:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                addr = (WORKER_BROADCAST_ADDRESS, WORKER_PORT)
            sock.sendto(json.dumps(payload).encode("utf-8"), addr)
    except Exception:
        pass


# ── Descoberta de peers ──────────────────────────────────────────────────────

def discover_peers():
    """Retorna lista de (host, port) de workers ativos na rede, excluindo si mesmo."""
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
                        data, _ = s.recvfrom(4096)
                        resp = json.loads(data.decode("utf-8"))
                    except socket.timeout:
                        break
                    except Exception:
                        continue
                    if resp.get("TASK") != "DISCOVER_RESPONSE":
                        continue
                    if resp.get("WORKER_UUID") == WORKER_UUID:
                        continue
                    h = resp.get("WORKER_HOST")
                    p = resp.get("WORKER_PORT")
                    if h and p:
                        peers.add((h, int(p)))
        except Exception:
            pass

    return [(h, p) for h, p in peers
            if not (h == WORKER_HOST and p == WORKER_PORT)]


# ── Registro de candidato na eleição ─────────────────────────────────────────

def _register_candidate(candidate: dict):
    """
    Adiciona/atualiza um candidato no dicionário de eleição.
    Thread-safe. Também garante que este nó esteja na eleição.
    """
    uuid = candidate.get("WORKER_UUID")
    if not uuid:
        return
    with election_lock:
        election_candidates[uuid] = candidate
    print(f"[ELECTION] Candidato registrado: {uuid} "
          f"({candidate.get('WORKER_HOST')}:{candidate.get('WORKER_PORT')}, "
          f"free={candidate.get('FREE_SPACE', 0) // (1024**3)} GB)")


def _pick_winner(candidates: list) -> dict:
    """
    Critério determinístico e igual para todos os nós:
      1. Mais espaço livre (maior FREE_SPACE) → vence
      2. Em empate: UUID menor em ordem lexicográfica → vence (estável)
    """
    return sorted(candidates,
                  key=lambda c: (-c.get("FREE_SPACE", 0), c.get("WORKER_UUID", "")))[0]


# ── Eleição em duas fases ────────────────────────────────────────────────────

def _broadcast_candidacy():
    """Anuncia a candidatura deste nó via UDP broadcast e TCP para todos os peers."""
    my_candidacy = {
        "TASK":        "ELECTION",
        "WORKER_UUID": WORKER_UUID,
        "WORKER_HOST": WORKER_HOST,
        "WORKER_PORT": WORKER_PORT,
        "FREE_SPACE":  get_free_space(),
    }
    # UDP broadcast (chega em todos na rede)
    send_udp(my_candidacy)
    # TCP direto para peers conhecidos (garante entrega caso broadcast falhe)
    for host, port in discover_peers():
        send_message(host, port, my_candidacy, timeout=WORKER_STATUS_TIMEOUT)


def start_election():
    """
    Algoritmo de eleição em duas fases.
    Deve ser chamado em uma thread separada.
    """
    global election_in_progress, failed_heartbeat_count, is_master

    # ── Guarda de entrada ────────────────────────────────────────────────────
    with state_lock:
        if is_master:
            return
        if election_in_progress:
            return       # outra thread já está conduzindo a eleição
        election_in_progress = True
        failed_heartbeat_count = 0

    print("[ELECTION] ──── FASE 1: CAMPAIGN ────")

    # Limpa candidatos anteriores e registra a si mesmo
    with election_lock:
        election_candidates.clear()

    my_candidacy = {
        "WORKER_UUID": WORKER_UUID,
        "WORKER_HOST": WORKER_HOST,
        "WORKER_PORT": WORKER_PORT,
        "FREE_SPACE":  get_free_space(),
    }
    _register_candidate(my_candidacy)

    # Limpa o evento de novo master antes de começar
    new_master_event.clear()

    # Transmite candidatura
    _broadcast_candidacy()

    # ── Aguarda a janela de coleta ───────────────────────────────────────────
    print(f"[ELECTION] Aguardando {ELECTION_WINDOW}s para coletar candidatos...")
    got_new_master = new_master_event.wait(timeout=ELECTION_WINDOW)

    if got_new_master:
        # Outro nó anunciou NEW_MASTER durante a janela → aceita e sai
        print("[ELECTION] NEW_MASTER recebido durante a janela. Eleição encerrada.")
        with state_lock:
            election_in_progress = False
        return

    # ── FASE 2: DECIDE ───────────────────────────────────────────────────────
    print("[ELECTION] ──── FASE 2: DECIDE ────")

    with election_lock:
        candidates = list(election_candidates.values())

    if not candidates:
        # Nenhum candidato coletado (rede isolada?): auto-elege
        candidates = [my_candidacy]

    print(f"[ELECTION] Candidatos coletados ({len(candidates)}): "
          + ", ".join(c['WORKER_UUID'] for c in candidates))

    winner = _pick_winner(candidates)
    print(f"[ELECTION] Vencedor determinístico: {winner['WORKER_UUID']} "
          f"({winner['WORKER_HOST']}:{winner['WORKER_PORT']})")

    if winner["WORKER_UUID"] == WORKER_UUID:
        # ── Sou o vencedor ────────────────────────────────────────────────
        _become_master()
        _announce_new_master()
        with state_lock:
            election_in_progress = False
    else:
        # ── Não sou o vencedor: aguardo o anúncio do vencedor ─────────────
        print(f"[ELECTION] Aguardando NEW_MASTER de {winner['WORKER_UUID']} "
              f"por {NEW_MASTER_WAIT}s...")
        with state_lock:
            election_in_progress = False

        got_announcement = new_master_event.wait(timeout=NEW_MASTER_WAIT)
        if not got_announcement:
            print("[ELECTION] Vencedor não anunciou a tempo. Reiniciando eleição...")
            threading.Thread(target=start_election, daemon=True).start()


def _join_election_as_peer(received_candidacy: dict):
    """
    Chamado quando este nó recebe uma mensagem ELECTION de outro peer.
    Registra o candidato e, se ainda não estava na eleição, entra nela.
    """
    global election_in_progress

    _register_candidate(received_candidacy)

    with state_lock:
        already_in = election_in_progress
        am_master  = is_master

    if am_master or already_in:
        return  # já está na eleição ou é o master atual

    # Não estava na eleição → entra agora
    print("[ELECTION] Recebida candidatura de peer. Ingressando na eleição...")
    threading.Thread(target=start_election, daemon=True).start()


# ── Ações de master ──────────────────────────────────────────────────────────

def _become_master():
    """Promove este nó a master e lança servidor.py como subprocesso."""
    global is_master, master_process

    with state_lock:
        is_master = True
        current_master["uuid"]       = WORKER_UUID
        current_master["ip"]         = WORKER_HOST
        current_master["port"]       = WORKER_PORT
        current_master["free_space"] = get_free_space()

    print(f"[ELECTION] ★ Worker {WORKER_UUID} é o novo MASTER ({WORKER_HOST}:{WORKER_PORT}) ★")

    # Lança servidor.py com variáveis de ambiente corretas
    env = os.environ.copy()
    env["MASTER_IP"]   = WORKER_HOST
    env["MASTER_PORT"] = str(WORKER_PORT)
    env["SERVER_UUID"] = WORKER_UUID

    script_dir    = os.path.dirname(os.path.abspath(__file__))
    servidor_path = os.path.join(script_dir, "servidor.py")

    if os.path.exists(servidor_path):
        try:
            master_process = subprocess.Popen(
                ["python", servidor_path],
                env=env,
                cwd=script_dir,
            )
            print(f"[ELECTION] servidor.py iniciado (PID {master_process.pid})")
        except Exception as e:
            print(f"[ELECTION] Falha ao iniciar servidor.py: {e}")
    else:
        print(f"[ELECTION] AVISO: servidor.py não encontrado em {servidor_path}")


def _announce_new_master():
    """Anuncia a eleição concluída para todos os peers via TCP e UDP."""
    payload = {
        "TASK":             "NEW_MASTER",
        "MASTER_HOST":      WORKER_HOST,
        "MASTER_PORT":      WORKER_PORT,
        "MASTER_UUID":      WORKER_UUID,
        "MASTER_FREE_SPACE": get_free_space(),
    }

    peers = set(WORKER_PEERS_LIST)
    peers.update(discover_peers())

    for host, port in peers:
        if host == WORKER_HOST and port == WORKER_PORT:
            continue
        print(f"[ELECTION] Notificando peer {host}:{port}")
        send_message(host, port, payload, timeout=WORKER_STATUS_TIMEOUT)

    # Garante via broadcast UDP também
    send_udp(payload)
    print("[ELECTION] Anúncio NEW_MASTER concluído.")


def _accept_new_master(master_host, master_port, master_uuid, master_free_space=0):
    """
    Aceita um anúncio de novo master (TCP ou UDP).
    Atualiza o estado local e sinaliza a eleição em andamento.
    """
    global is_master, election_in_progress, failed_heartbeat_count

    if not master_host or not master_port or not master_uuid:
        return

    print(f"[ELECTION] ✓ Novo master aceito: {master_uuid} em {master_host}:{master_port}")

    with state_lock:
        # Se este nó se achava master mas recebeu NEW_MASTER de outro → cede
        if is_master and master_uuid != WORKER_UUID:
            print("[ELECTION] Este nó cede o título de master para o anunciante.")
            is_master = False

        election_in_progress   = False
        failed_heartbeat_count = 0
        current_master["uuid"]       = master_uuid
        current_master["ip"]         = master_host
        current_master["port"]       = int(master_port)
        current_master["free_space"] = int(master_free_space or 0)

    # Sinaliza threads aguardando NEW_MASTER
    new_master_event.set()


# ── Listener UDP (discovery + election + new_master) ─────────────────────────

def start_discovery_listener():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("0.0.0.0", WORKER_PORT))
    except Exception as e:
        print(f"[DISCOVERY] Falha ao bindear UDP porta {WORKER_PORT}: {e}")
        return

    print(f"[DISCOVERY] Listener UDP ativo na porta {WORKER_PORT}")

    while True:
        try:
            data, addr = sock.recvfrom(4096)
            payload = json.loads(data.decode("utf-8"))
        except Exception:
            continue

        task = payload.get("TASK")

        if task == "DISCOVER_WORKER":
            if payload.get("WORKER_UUID") == WORKER_UUID:
                continue
            sock.sendto(json.dumps({
                "TASK":        "DISCOVER_RESPONSE",
                "WORKER_UUID": WORKER_UUID,
                "WORKER_HOST": WORKER_HOST,
                "WORKER_PORT": WORKER_PORT,
            }).encode("utf-8"), addr)

        elif task == "ELECTION":
            if payload.get("WORKER_UUID") != WORKER_UUID:
                _join_election_as_peer(payload)

        elif task == "NEW_MASTER":
            _accept_new_master(
                payload.get("MASTER_HOST"),
                payload.get("MASTER_PORT"),
                payload.get("MASTER_UUID"),
                payload.get("MASTER_FREE_SPACE", 0),
            )


# ── Status server TCP ────────────────────────────────────────────────────────

def handle_incoming_connection(conn, addr):
    buf = ""
    try:
        while True:
            data = conn.recv(4096).decode("utf-8")
            if not data:
                break
            buf += data
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue

                task = payload.get("TASK")

                if task == "HEARTBEAT":
                    with state_lock:
                        am_master = is_master
                    conn.sendall((json.dumps({
                        "SERVER_UUID": WORKER_UUID,
                        "TASK":        "HEARTBEAT",
                        "RESPONSE":    "ALIVE" if am_master else "NOT_MASTER",
                    }) + "\n").encode("utf-8"))

                elif task == "WORKER_STATUS":
                    conn.sendall((json.dumps({
                        "TASK":        "WORKER_STATUS_RESPONSE",
                        "WORKER_UUID": WORKER_UUID,
                        "WORKER_HOST": WORKER_HOST,
                        "WORKER_PORT": WORKER_PORT,
                        "FREE_SPACE":  get_free_space(),
                    }) + "\n").encode("utf-8"))

                elif task == "ELECTION":
                    if payload.get("WORKER_UUID") != WORKER_UUID:
                        _join_election_as_peer(payload)
                    conn.sendall((json.dumps({
                        "TASK":     "ELECTION_ACK",
                        "RESPONSE": "REGISTERED",
                    }) + "\n").encode("utf-8"))

                elif task == "NEW_MASTER":
                    _accept_new_master(
                        payload.get("MASTER_HOST"),
                        payload.get("MASTER_PORT"),
                        payload.get("MASTER_UUID"),
                        payload.get("MASTER_FREE_SPACE", 0),
                    )
                    conn.sendall((json.dumps({
                        "TASK":     "NEW_MASTER_ACK",
                        "RESPONSE": "RECEIVED",
                    }) + "\n").encode("utf-8"))

                else:
                    conn.sendall((json.dumps({
                        "TASK":     "ERROR",
                        "RESPONSE": "UNKNOWN_TASK",
                    }) + "\n").encode("utf-8"))

    except Exception as e:
        print(f"[STATUS] Erro em {addr}: {e}")
    finally:
        conn.close()


def start_status_server():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((WORKER_HOST, WORKER_PORT))
    srv.listen()
    print(f"[STATUS] Servidor TCP ativo em {WORKER_HOST}:{WORKER_PORT}")
    while True:
        conn, addr = srv.accept()
        threading.Thread(target=handle_incoming_connection,
                         args=(conn, addr), daemon=True).start()


# ── Heartbeat ────────────────────────────────────────────────────────────────

def enviar_heartbeat():
    global failed_heartbeat_count

    with state_lock:
        if is_master:
            return
        master_ip   = current_master["ip"]
        master_port = current_master["port"]

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(5)

    try:
        sock.connect((master_ip, master_port))
        print(f"[HEARTBEAT] Conectado ao master {master_ip}:{master_port}")

        sock.sendall((json.dumps({
            "SERVER_UUID": WORKER_UUID,
            "WORKER_UUID": WORKER_UUID,
            "TASK":        "HEARTBEAT",
        }) + "\n").encode("utf-8"))

        buf  = ""
        data = sock.recv(4096).decode("utf-8")

        if not data:
            raise ConnectionError("Sem resposta do master.")

        buf += data
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            try:
                resp = json.loads(line)
            except json.JSONDecodeError:
                continue

            response_val = resp.get("RESPONSE")
            print(f"[HEARTBEAT] Resposta: {response_val}")

            if response_val == "ALIVE":
                with state_lock:
                    failed_heartbeat_count = 0
                    election_in_progress   = False

            elif response_val == "NOT_MASTER":
                # Conectamos a um worker que não é mais master
                print("[HEARTBEAT] Resposta NOT_MASTER – iniciando eleição.")
                with state_lock:
                    failed_heartbeat_count = HEARTBEAT_THRESHOLD
                threading.Thread(target=start_election, daemon=True).start()

    except (ConnectionRefusedError, socket.timeout, ConnectionError, OSError) as e:
        with state_lock:
            failed_heartbeat_count += 1
            failures = failed_heartbeat_count
        print(f"[HEARTBEAT] Falha #{failures}/{HEARTBEAT_THRESHOLD} "
              f"com master {master_ip}:{master_port} – {e}")

        if failures >= HEARTBEAT_THRESHOLD:
            threading.Thread(target=start_election, daemon=True).start()

    except Exception as e:
        print(f"[HEARTBEAT] Erro inesperado: {e}")
    finally:
        sock.close()
        print("[HEARTBEAT] Conexão encerrada.\n")


# ── Entry point ──────────────────────────────────────────────────────────────

def start_worker():
    print("=" * 60)
    print(f"  Worker {WORKER_UUID} iniciando")
    print(f"  Host: {WORKER_HOST}:{WORKER_PORT}")
    print(f"  Master inicial: {MASTER_IP}:{MASTER_PORT}")
    print("=" * 60)

    threading.Thread(target=start_discovery_listener, daemon=True).start()
    threading.Thread(target=start_status_server,      daemon=True).start()

    # Aguarda os servidores subirem antes do primeiro heartbeat
    time.sleep(1)

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
