# claude-telegram-central

Responda perguntas e pedidos de permissão do Claude Code, e converse com uma sessão coordenadora do Claude Code, por um bot do Telegram no celular.

Quando uma sessão do Claude Code para para perguntar algo (pergunta com opções, plano para aprovar, pedido de permissão), o bot manda a pergunta ao seu Telegram com um botão por opção e um botão **Outra** para texto livre ou áudio. A resposta fica numa fila num servidor pequeno, então não se perde se o PC estiver desligado. Tudo o mais que você mandar ao bot (texto, áudio, foto, vídeo, arquivo de até 2 GB) entra numa fila para uma sessão coordenadora no PC; o áudio (de voz e de vídeo) é transcrito por um Whisper local, e de cada vídeo saem 4 quadros.

Não há IA no transporte: são scripts simples, SSH e cron.

## Arquitetura

```
 Sessões do Claude Code (PC)                   Servidor (VPS Linux pequeno)             Celular
 ───────────────────────────                   ────────────────────────────             ───────
 gancho ──► hook-pending/*.json
 watcher.py  (le as transcricoes,
   acha perguntas paradas) ───┐
                              │  SSH, JSON na entrada e na saida
 bridge.py ◄──────────────────┴────────────────► central.py --bridge
   answers.jsonl  (respostas nao entregues)         │  estado, fila de respostas, caixa de entrada
   inbox-queue.jsonl (suas mensagens/arquivos)      │
                                                    ├─ central.py (cron, long-poll) ◄──► API do Telegram ◄──► chat do seu bot
 listener.py --wait  (sessao coordenadora)          ├─ receive_files.py (cron): baixa, sha256, Whisper, quadros de video
   imprime mensagens novas e termina,               └─ servidor local da Bot API (127.0.0.1) opcional, para arquivos de ate 2 GB
   o que acorda a sessao
 reply_telegram.py / send_files_telegram.py ──────► so o seu chat
 answers.py list|ack  ◄── a coordenadora entrega cada resposta a sessao parada
```

Caminho de uma pergunta: a sessao para -> o gancho/watcher percebe -> a ponte empurra para o servidor -> o bot posta os botoes -> voce toca -> a resposta fica em `answers.jsonl` no servidor -> na proxima rodada a ponte copia para o PC -> a coordenadora (ou um script seu) entrega a resposta a sessao e roda `answers.py ack <id>` -> o Telegram mostra "Delivered".

Este projeto **não** digita sozinho dentro de uma sessão parada. Entregar a resposta é tarefa da coordenadora (por exemplo, uma sessão do Claude Code que consegue mandar mensagem a outras sessões) ou de um script seu sobre `answers.py list`.

## Requisitos

- Token de bot do @BotFather e o seu id numérico de chat.
- Servidor Linux com Python 3.8+, cron, `flock` e (opcional) `ffmpeg` para quadros de vídeo e áudio.
- PC com Python 3.8+, `ssh`/`scp` e acesso por chave ao servidor.
- Opcional: `faster-whisper` num venv próprio no servidor, para transcrição; um `telegram-bot-api` compilado, para arquivos acima de 20 MB.

## Instalação

1. **Crie o bot.** Fale com o @BotFather, crie o bot e guarde o token. Mande uma mensagem ao seu bot e abra uma vez `https://api.telegram.org/bot<token>/getUpdates` no navegador para ler o seu `chat.id`. Nunca cole o token num chat com IA nem num repositório.

2. **Servidor.** Copie `server/` para o servidor (por exemplo `~/claude-telegram-central/server`).
   ```
   mkdir -p ~/.config/claude-telegram-central
   cp server/config.example.env ~/.config/claude-telegram-central/telegram.env
   chmod 600 ~/.config/claude-telegram-central/telegram.env   # depois preencha TELEGRAM_TOKEN e TELEGRAM_CHAT_ID
   crontab -e                                                  # adicione as duas linhas de server/crontab.example
   ```
   Os dados vão para `CENTRAL_HOME` (padrão `~/claude-telegram-central/data`). Rode `python3 server/central.py` uma vez à mão para conferir que ele escuta.

3. **PC.** Copie `pc/config.example.env` para `~/.config/claude-telegram-central/pc.env` e preencha `CENTRAL_SSH_HOST` e `CENTRAL_SSH_KEY`. Confira que `ssh <host> true` funciona sem pedir nada. Opcional: defina `CENTRAL_DATA_DIR` no ambiente para mudar a pasta de dados do PC (padrão `~/.claude-telegram-central`).

4. **Gancho do Claude Code.** Registre `pc/central_hook.py` nas configurações do Claude Code (`~/.claude/settings.json`), como em `pc/claude-settings.example.json`: uma entrada `PreToolUse` com matcher `AskUserQuestion|ExitPlanMode` e uma entrada `PermissionRequest`, ambas rodando `python3 -I "/caminho/para/claude-telegram-central/pc/central_hook.py"` (no Windows use `pythonw.exe`, para nenhuma janela piscar). O gancho só registra o pedido; nunca responde, bloqueia ou muda a decisão.

5. **Watcher.** Agende `python -I pc/watcher.py` a cada ~15 minutos e `python -I pc/watcher.py --fast` a cada ~3 minutos (cron, ou `schtasks` no Windows). A passada rápida lê só transcrições mexidas nos últimos 30 minutos.

6. **Sessão coordenadora** (opcional, para conversar pelo celular). Peça a uma sessão do Claude Code, no `CLAUDE.md` dela ou por prompt, que rode `python -I pc/listener.py --wait` como tarefa em segundo plano. Ele termina quando você manda uma mensagem; a saída acorda a sessão. A sessão responde com `python -I pc/reply_telegram.py <id> "texto"` e liga o listener de novo. Ela também pode usar `answers.py list` / `ack` para entregar respostas às sessões paradas.

7. **Arquivos e voz (opcional).** No servidor: `apt install ffmpeg`, depois `python3 -m venv ~/claude-telegram-central/whisper/venv && ~/claude-telegram-central/whisper/venv/bin/pip install -r server/requirements-whisper.txt`. Sem isso, áudio e vídeo chegam sem transcrição. Para arquivos acima de 20 MB, compile o `telegram-bot-api` (github.com/tdlib/telegram-bot-api) em `~/claude-telegram-central/telegram-local/bin/`, ponha `TELEGRAM_API_ID`/`TELEGRAM_API_HASH` em `telegram-local.env` (chmod 600, veja `server/telegram-local.example.env`) e rode `server/setup_local_bot_api.sh` uma vez. Depois defina `TELEGRAM_VIA_SERVER=1` no `pc.env`: a API oficial deixa de aceitar o token vindo do PC.

8. **Tarefas (opcional).** `/job` no chat lista um conjunto fechado de tarefas nomeadas de `jobs.json` (veja `server/jobs.example.json`) e põe a escolhida em `jobs-queue.jsonl` no PC. Nada as executa por você: aponte o seu próprio executor para esse arquivo.

## Segurança

- **Só o dono.** Toda mensagem e todo clique de botão precisa vir do id de chat e do id de usuário de `TELEGRAM_CHAT_ID`. Qualquer outro recebe uma recusa e nada é guardado.
- **Filtro de segredos.** Chaves de API coladas, tokens de bot e números de cartão (conferidos por Luhn; links e ids dentro de URLs são ignorados) não são guardados nem repassados, na entrada e na saída (`reply_telegram.py`). O filtro nunca apaga as suas mensagens: só se recusa a guardá-las e avisa. Se um áudio for transcrito como algo secreto, o arquivo fica e só a transcrição é retida.
- **Perguntas sobre segredos** (senhas, chaves, códigos 2FA, números de cartão ou documento) não ganham botões nem texto no servidor: o bot só diz "faça no PC".
- **Tokens só em arquivos de configuração/ambiente** com `chmod 600`, nunca no código, em URLs registradas ou em linhas de comando (o script do servidor local passa o token ao curl pela entrada padrão). Os logs de erro guardam só o tipo da exceção, porque a URL traz o token.
- **Nada aprova nada sozinho.** O gancho é passivo. A sua resposta é um dado entregue a uma sessão; pedidos de permissão ainda exigem a decisão real no Claude Code. O texto das sessões mostrado no Telegram e as suas mensagens mostradas à coordenadora são tratados como dado, nunca como comando. Anexos são salvos e nunca abertos nem executados.
- **Nenhuma porta aberta.** O servidor só faz chamadas de saída ao Telegram; o servidor local opcional da Bot API escuta em 127.0.0.1.
- Trate a chave SSH como qualquer chave de produção: dê a ela uma conta própria no servidor, sem sudo.

## Testes

```
python -I tests/run_all.py
```
Roda offline, com Telegram e SSH simulados; nada toca a rede.

## Limites conhecidos

- Um áudio mandado como resposta a uma pergunta aberta é guardado como caminho de arquivo no servidor, sem transcrição (áudios mandados fora de uma pergunta são transcritos). Use texto para responder perguntas, ou acrescente a transcrição em `handle_message`.
- `send_files_telegram.py` lê cada arquivo inteiro na memória do servidor durante o envio.
- As mensagens do bot estão em inglês; são textos simples em `server/central.py`.

## Licença

MIT, veja `LICENSE`.
