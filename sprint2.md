Sprint 2: Comunicação de Tarefas e Apresentação de Workers

Implementar o fluxo completo de ciclo de vida de uma tarefa, desde a apresentação do Worker (identificando sua origem) até o processamento e a confirmação final de recebimento do status pelo Master.

1. Backlog de Tarefas (To-Do)
Tarefa 01: Lógica de Apresentação e Identificação (Worker → Master)
Implementar no Worker a capacidade de se apresentar ao Master enviando seu UUID.
Diferencial de Origem: O Worker deve ser capaz de enviar o payload de "Emprestado" caso esteja vinculado a um Master original diferente.
Payloads (2.1 e 2.1b):
{"WORKER": "ALIVE", "WORKER_UUID": "..."}
ou {"WORKER": "ALIVE", "WORKER_UUID": "...", "SERVER_UUID": "..."}
Tarefa 02: Distribuição de Carga e Gestão de Fila (Master → Worker)
Configurar o Master para gerenciar uma fila (queue) de tarefas pendentes.
Ao receber um pedido de tarefa, o Master deve verificar a fila:
Com Tarefa (Payload 2.2): Enviar {"TASK": "QUERY", "USER": "..."}
Fila Vazia (Payload 2.3): Enviar {"TASK": "NO_TASK"}
Tarefa 03: Simulação de Processamento e Relatório de Status (Worker → Master)
Desenvolver no Worker o "executor": ao receber uma QUERY, ele deve simular um processamento (ex: sleep aleatório ou cálculo).
Após o trabalho, o Worker deve reportar o resultado.
Payload (2.4): {"STATUS": "OK | NOK", "TASK": "QUERY", "WORKER_UUID": "..."}
Tarefa 04: Mecanismo de Confirmação (ACK) e Persistência (Master → Worker)
Implementar no Master o recebimento do status e a resposta de confirmação imediata para liberar o Worker.
Payload (2.5): {"STATUS": "ACK"}
Garantir que o Master registre (log) qual Worker (local ou emprestado) concluiu qual tarefa.