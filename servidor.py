import socket
import threading
import json

HOST = '10.62.206.24'
PORT = 8000

SERVER_UUID = 'SRV-MASTER'

# função para tratar cada conexão
def handle_client(conn, addr):
    print(f"[MASTER] Conectado com {addr}")

    buffer = ""

    try:
        while True:
            data = conn.recv(1024).decode()

            if not data:
                break

            buffer += data

            while "\n" in buffer:
                message, buffer = buffer.split("\n", 1)

                msg_json = json.loads(message)

                task = msg_json.get("TASK")

                print(f"[MASTER] Mensagem recebida: {msg_json}")

                # PROTOCOLO HEARTBEAT
                if task == "HEARTBEAT":

                    response = {
                        "SERVER_UUID": SERVER_UUID,
                        "TASK": "HEARTBEAT",
                        "RESPONSE": "ALIVE"
                    }

                    conn.send((json.dumps(response) + "\n").encode())

    except Exception as e:
        print("Erro:", e)

    finally:
        conn.close()
        print(f"[MASTER] Conexão encerrada {addr}")


def start_server():

    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    server_socket.bind((HOST, PORT))

    server_socket.listen()

    print(f"[MASTER] Servidor iniciado na porta {PORT}")
    print(f"[MASTER] UUID: {SERVER_UUID}")

    while True:

        conn, addr = server_socket.accept()

        # thread para múltiplos workers
        client_thread = threading.Thread(
            target=handle_client,
            args=(conn, addr)
        )

        client_thread.start()


if __name__ == "__main__":
    start_server()