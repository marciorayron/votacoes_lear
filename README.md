# Sistema de Votação Eletrônica Local (rede isolada)

Sistema de votação para **ambiente industrial com rede isolada** (roteador
dedicado, sem internet). O backend Flask — servido em produção pelo **Waitress**
(WSGI) — roda na máquina central: a mesma
onde está conectado o **leitor NFC USB** (emulador de teclado/HID). Cabines
*touch* e o painel de TV acessam o servidor pela rede local via navegador.

---

## 1. Componentes

| Rota          | Papel                                                                 |
|---------------|-----------------------------------------------------------------------|
| `/checkin`    | Interface de check-in exibida **no próprio servidor** (leitor NFC HID). |
| `/cabine`     | Cabine de votação (tablet/touch em *kiosk mode*; numera-se sozinha).  |
| `/painel-tv`  | Painel público de resultados (TV) com gráfico de barras.              |
| `/admin`      | Painel administrativo — cria/reseta/encerra plebiscitos dinâmicos.    |
| `/admin/relatorio` | **Ata de Fechamento / Relatório Oficial** em A4 (imprimir/Salvar PDF). |
| `/`           | Índice com links para todas as telas.                                 |

## 2. Instalação

```bash
pip install -r requirements.txt
# (rede isolada) baixe os pacotes em outra máquina e transfira:
#   pip download -r requirements.txt -d ./wheels
#   pip install --no-index --find-links=./wheels -r requirements.txt
```

## 3. Execução

O servidor sobe, por padrão, em **modo produção** com o
[Waitress](https://github.com/Pylons/waitress) (servidor WSGI robusto) — sem o
aviso de "development server" do Flask.

```bash
# gera um DEVICE_TOKEN seguro e o imprime no console
python3 app.py
# equivalente a:
python3 app.py --production
```

Apenas para depuração local, é possível usar o servidor de desenvolvimento do
Flask (não use em produção):

```bash
python3 app.py --dev          # alias: --development
```

Para fixar o token e/ou ajustar parâmetros:

```bash
DEVICE_TOKEN="meu-token-forte" \
PORT=5000 \
CHECKIN_COOLDOWN_SECONDS=2 \
RESULTS_DELAY_SECONDS=5 \
python3 app.py
```

> No modo produção o log de inicialização mostra `Modo : Production
> (Waitress)` e o endereço de bind (`IP:porta`). Se o Waitress não estiver
> instalado, o servidor avisa e recua automaticamente para o servidor de
> desenvolvimento do Flask (compatibilidade).

### Variáveis de ambiente

| Variável                    | Padrão          | Descrição                                              |
|-----------------------------|-----------------|--------------------------------------------------------|
| `DEVICE_TOKEN`              | gerado no boot  | Token das rotas de administração.                      |
| `FACTORY_NAME`              | `Fábrica`       | Nome da fábrica/entidade no cabeçalho da Ata.          |
| `VOTING_DB`                 | `./votacao.db`  | Caminho do banco SQLite (relativo ou absoluto).        |
| `CHECKIN_COOLDOWN_SECONDS`  | `2`             | Cooldown anti-duplo-swap no check-in.                  |
| `RESULTS_DELAY_SECONDS`     | `5`             | Atraso controlado dos resultados no painel de TV.      |
| `BOOTH_TIMEOUT_SECONDS`     | `20`            | Sem heartbeat por este tempo, a cabine fica offline.   |
| `BOOTH_BUSY_SECONDS`        | `45`            | Reserva da cabine após uma liberação de check-in.      |
| `HOST` / `PORT`             | `0.0.0.0:5000`  | Bind do servidor.                                      |

Abra o administrador em: `http://SERVIDOR:5000/admin?token=SEU_TOKEN`.

### Banco de dados: diretório e fallback

- Se o diretório de `VOTING_DB` **não existir**, ele é criado
  automaticamente (`os.makedirs(..., exist_ok=True)`) antes de abrir o SQLite.
- Se o caminho for **relativo**, é resolvido a partir do diretório de trabalho.
- Se o caminho **não tiver permissão de escrita** (ou a abertura falhar), o
  sistema avisa no console e cai para um arquivo local, nesta ordem:
  1. `voting.db` no diretório do script (`app.py`);
  2. `voting.db` no diretório temporário do sistema.
- Cada falha imprime uma mensagem `[db] ...` clara, em vez de um
  `sqlite3.OperationalError` cru.
- No boot, o banner mostra `Banco de dados` e, quando houver substituição,
  `(fallback em uso - VOTING_DB pedido: ...)`.

> Em rede isolada, prefira um disco **gravável**: se o volume inteiro estiver
> somente-leitura (ex.: ext4 em `emergency_ro` após erro de I/O), o fallback
> precisa de um diretório gravável para funcionar.

### Persistência do estado (sobrevive a reboot)

Todo o estado do sistema é gravado no SQLite — nada depende de variáveis em
memória do processo:

- a tabela `polls` guarda a **pergunta**, as **opções** (JSON) e o **`status`**
  (`open`/`closed`) do plebiscito, com `created_at`/`closed_at`;
- a tabela `settings` (chave/valor) guarda **`active_poll_id`** (qual poll está
  ativa) e **`voting_tokens`** (tamanho atual da fila de votação);
- a tabela `booths` guarda as **cabines** (número amigável, rótulo, último
  *heartbeat* e reserva atual), então a numeração sobrevive a reinícios.

As leituras/escritas usam uma conexão por requisição (`get_db`) e um lock
(`threading.RLock`) que serializa o incremento/decremento da fila e o registro
do voto dentro de **uma única transação** (`commit`). Assim, reiniciar o
servidor (incluindo depois de queda de energia) preserva o plebiscito ativo, a
fila de tokens, os crachás e os votos — e, se duas cabines disputarem o último
token, apenas uma é aceita.

## 4. Fluxo de operação

1. **Admin** (`/admin`) → informa o Device Token → digita a pergunta, define as
   opções (ex.: `Sim` / `Não`) → **Iniciar plebiscito**.
2. **Mesa de check-in** (`/checkin`, no servidor) → o trabalhador aproxima o
   crachá; o leitor NFC digita o número + `Enter`. O sistema valida no SQLite,
   incrementa a fila global `voting_tokens` e **aloca a próxima cabine ociosa**,
   respondendo com a orientação personalizada (ex.: *"Liberado! Dirija-se à
   Cabine 02."*). Sem cabines online/idle, cai no fallback genérico (*"Dirija-se
   a qualquer cabine disponível."*). Um **bipe agudo** confirma a liberação; um
   **zumbido grave** sinaliza erro (crachá já votou, etc.).
   * Cooldown de 2s no front-end + PK no banco = **nenhum voto duplo**.
3. **Cabines** (`/cabine`) → cada kiosk registra-se (`/api/booth/register`) e
   envia *heartbeat* a cada 5s, recebendo um número amigável estável
   (**Cabine 01**, **Cabine 02**, …). A cada 1s consulta
   `/api/booth/status?booth_id=…`; apenas a cabine **alocada** no check-in busca
   a pergunta/opções (`/api/poll`), mostra botões grandes e envia o voto
   (`/api/vote`), consumindo **exatamente um** token. Ao concluir, a cabine
   libera sua reserva (heartbeat `idle`) e volta a ficar disponível.
4. **Painel TV** (`/painel-tv`) → consulta `/api/results` periodicamente e
   exibe o gráfico de barras com **atraso controlado** (`RESULTS_DELAY_SECONDS`),
   garantindo sigilo do voto e transparência.


## 5. API

### Públicas / cabines
| Método | Rota                    | Descrição                                            |
|--------|-------------------------|------------------------------------------------------|
| POST   | `/api/checkin`          | `{badge_id}` → valida, incrementa token e aloca cabine. |
| GET    | `/api/booth/status`     | Tokens e estado do plebiscito. Com `?booth_id=…`, informa `assigned`/`booth`. |
| POST   | `/api/booth/register`   | `{booth_id}` → registra a cabine e devolve número/rótulo. |
| POST   | `/api/booth/heartbeat`  | `{booth_id, status?}` → mantém `online`; `status:"idle"` libera a reserva. |
| GET    | `/api/booths`           | Lista das cabines (online / ociosa / alocada).       |
| GET    | `/api/poll`             | Pergunta + opções ativas (sem contagens).            |
| POST   | `/api/vote`             | `{option_id}` → consome 1 token e conta o voto.      |
| GET    | `/api/results`          | Resultados (com atraso controlado).                  |
| GET    | `/api/health`           | Health-check.                                        |

### Administrativas (exigem header `X-Device-Token`)
| Método | Rota                   | Descrição                                        |
|--------|------------------------|--------------------------------------------------|
| GET    | `/api/admin/state`     | Estado atual (poll, contadores, config).         |
| POST   | `/api/admin/poll`      | `{question, options:[...]}` → cria e inicia.     |
| POST   | `/api/admin/reset`     | `{clear_voters}` → zera contadores.              |
| POST   | `/api/admin/close`     | Encerra o plebiscito ativo.                      |
| POST   | `/api/admin/tokens`    | `{count}` ou `{delta}` → ajuste manual da fila.  |

Exemplo:

```bash
TOKEN="SEU_TOKEN"
curl -X POST http://localhost:5000/api/admin/poll \
  -H "X-Device-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"question":"Aprova o novo turno?","options":["Sim","Não"]}'
```

## 6. Segurança

- Rotas administrativas exigem **`X-Device-Token`** (comparação em tempo
  constante). O token aparece no console ao iniciar; defina `DEVICE_TOKEN` para
  fixá-lo entre reinícios.
- `/admin` é um *shell* sem dados; o desbloqueio guarda o token em
  `sessionStorage` e todas as chamadas enviam o header.
- Votos são **anônimos**: a tabela `votes` guarda apenas `poll_id`/`option_id`.
- A fila de tokens é atualizada de forma **atômica** (lock + SQLite WAL); se
  duas cabines disputarem o último token, apenas uma é aceita.

## 7. Kiosk mode (cabines e TV)

As cabines rodam em **modo quiosque**: tela cheia, sem barras, sem acesso ao
sistema operacional pelo operador. Use `google-chrome` (ou `chromium`).

### 7.1. Comando recomendado (cabine)

```bash
google-chrome --kiosk \
  --noerrdialogs --disable-session-crashed-bubble --disable-infobars \
  --disable-pinch --overscroll-history-navigation=0 \
  --disable-features=TranslateUI,MediaRouter,InfiniteSessionRestore \
  --no-first-run --disable-translate --start-fullscreen \
  --user-data-dir="$HOME/.kiosk-cabine" \
  http://SERVIDOR:5000/cabine
```

| Flag | Por quê |
|------|---------|
| `--kiosk` | Tela cheia de verdade; **esconde barra de endereço, abas e menus**. |
| `--disable-infobars` | Remove a barra *"Chrome está sendo controlado…"* / avisos. |
| `--noerrdialogs` / `--disable-session-crashed-bubble` | Não mostra diálogos de erro/restauração que revelariam o SO. |
| `--disable-pinch` | Bloqueia zoom por pinça (essencial em telas *touch*). |
| `--overscroll-history-navigation=0` | Desliga o "arrastar para voltar" que sairia da cabine. |
| `--user-data-dir=…` | Perfil dedicado: mantém *localStorage* (o número da cabine) estável e isola do perfil do operador. |

> **TV (painel de resultados):** troque a URL por
> `google-chrome --kiosk --noerrdialogs http://SERVIDOR:5000/painel-tv`.

### 7.2. Bloquear a saída (trava do operador)

`--kiosk` já impede `F11`, `Ctrl+T`, `Ctrl+N` e a barra de endereço. Para
impedir que o operador **saia** para a área de trabalho, reforce no sistema
operacional (exemplos para GNOME em Linux):

```bash
# Desabilita atalhos que trocam de janela/área de trabalho
gsettings set org.gnome.desktop.wm.keybindings switch-applications "[]"
gsettings set org.gnome.desktop.wm.keybindings switch-windows "[]"
gsettings set org.gnome.shell.keybindings toggle-overview "[]"
# Bloqueia a troca para consoles de texto (Ctrl+Alt+F1..F12)
sudo systemctl mask getty@tty2.service   # repita p/ tty3..tty6
```

Complementos:

- Use um **usuário dedicado e restrito** (sem sudo) autenticado automaticamente
  na cabine — o operador nunca vê a tela de login nem o desktop do admin.
- Desabilite a área de trabalho: no GNOME, o modo quiosque com o Chrome em
  `--kiosk` já cobre o ecrã inteiro; combine com um *autologin* dessa conta.
- Segurança física: a cabine não deve ter teclado/mouse expostos — apenas a
  tela *touch*.

### 7.3. Iniciar sozinho no boot (autostart)

Crie `~/.config/autostart/cabine-votacao.desktop` (do usuário da cabine):

```ini
[Desktop Entry]
Type=Application
Name=Cabine de Votação
Exec=google-chrome --kiosk --noerrdialogs --disable-infobars \
     --disable-pinch --overscroll-history-navigation=0 \
     --user-data-dir=/home/CABINE/.kiosk-cabine http://SERVIDOR:5000/cabine
X-GNOME-Autostart-enabled=true
```

Assim, depois de uma queda de energia, a cabine volta sozinha à tela de voto.

### 7.4. Manutenção e recuperação

- **Sair de propósito:** `Alt+F4` (ou `Ctrl+W`) fecha a janela do kiosk.
- **Travar novamente / reiniciar a cabine** a partir de outra máquina:
  ```bash
  ssh CASERVICO@IP_DA_CABINE "pkill chrome; DISPLAY=:0 google-chrome --kiosk http://SERVIDOR:5000/cabine &"
  ```
- Se você desabilitou os consoles (`mask getty@tty2`), o acesso remoto passa a
  ser por **SSH** — mantenha o serviço SSH ativo na cabine.

### 7.5. Áudio e leitor NFC

- **Feedback sonoro:** as telas `/checkin` e `/cabine` emitem *bipes* via
  **Web Audio API** (sem arquivos). O navegador só libera áudio após o
  primeiro toque/tecla — em quiosque, o primeiro toque já desbloqueia. Em
  `/checkin` há um botão **🔊 Som / 🔇 Mudo** no rodapé.
- **Leitor NFC (HID):** emula teclado; **mantenha `/checkin` em foco** no
  servidor (a página refocaliza o campo automaticamente a cada 800 ms).

> Chart.js é carregado via CDN (conforme requisito). Sem internet, o painel
> usa automaticamente um **fallback de barras em CSS puro**.

## 8. Testes

```bash
bash test_e2e.sh       # fluxo funcional (52 cenários)
bash test_db_path.sh   # resolução/fallback do caminho do SQLite (10 cenários)
bash test_persist.sh   # persistência após reboot + atomicidade (24 cenários)
```

- `test_e2e.sh` — fluxo funcional em porta isolada: autenticação por token,
  check-in, bloqueio de voto duplo, consumo de tokens, atraso dos resultados,
  reset/close, todas as páginas, o **registro/numeração/alocação de cabines** e a
  **Ata de Fechamento / Relatório Oficial** (endpoint `/api/admin/report` e página
  `/admin/relatorio`: HTTP 200 com token, 401 sem token, e estatísticas agregadas
  corretas — votos por opção, total de votos, votantes únicos e timestamps).
- `test_db_path.sh` — criação automática do diretório, caminho relativo e
  fallback quando o local é somente-leitura / inválido.
- `test_persist.sh` — **reinicia** o servidor sobre o mesmo banco e confirma que
  o plebiscito ativo (pergunta/opções/status), a fila de `voting_tokens`, os
  crachás e os votos permanecem; em seguida dispara votos em paralelo para
  provar que o decremento da fila é atômico (nunca se consome mais que o
  disponível). Também confirma que a **numeração das cabines** sobrevive ao
  reinício.

## 9. Estrutura

```
app.py               # Backend Flask (rotas + SQLite + token de dispositivo)
                     #   - servidor de produção Waitress (padrão); --dev = Flask
requirements.txt     # Flask + Waitress
templates/
  index.html         # Indice
  admin.html         # Painel administrativo (X-Device-Token)
  relatorio.html     # Ata de Fechamento / Relatorio Oficial (A4, impressao/PDF)
  checkin.html       # Check-in (leitor NFC HID + cooldown)
  cabine.html        # Cabine touch (kiosk)
  painel_tv.html     # Painel de resultados (Chart.js / fallback CSS)
test_e2e.sh          # Testes end-to-end (fluxo funcional)
test_db_path.sh      # Testes de caminho/fallback do banco SQLite
test_persist.sh      # Testes de persistencia (reboot) + atomicidade da fila
```
