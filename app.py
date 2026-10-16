"""
Sistema de Votacao Eletronica Local (Flask)
===========================================

Servidor central para uma rede ISOLADA (roteador dedicado, ambiente
industrial). Roda no computador que possui o leitor NFC USB (emulador de
teclado / HID) fisicamente conectado.

Componentes
-----------
  * /checkin    -> Interface de check-in exibida no proprio servidor. O leitor
                   NFC "digita" o numero do cracha (badge) + Enter.
  * /cabine     -> Cabine de votacao (terminal touch / tablet em kiosk mode).
  * /painel-tv  -> Painel publico de resultados (TV) com grafico de barras.
  * /admin      -> Painel administrativo (cria/reseta plebiscitos de forma
                   dinamica, sem alterar codigo).

Configuracao (variaveis de ambiente)
------------------------------------
  DEVICE_TOKEN .............. token de acesso as rotas de admin.
                              Se ausente, um token seguro e gerado no boot.
  VOTING_DB ................. caminho do banco SQLite (default: ./votacao.db).
                              Aceita caminho relativo ou absoluto; o diretorio
                              e criado automaticamente. Sem permissao de
                              escrita, cai para ./voting.db (dir. do script).
  CHECKIN_COOLDOWN_SECONDS .. cooldown anti duplo-swap no check-in (default 2)
  RESULTS_DELAY_SECONDS ..... atraso controlado para liberar resultados (def. 5)
  HOST / PORT ............... bind do servidor (default 0.0.0.0:5000)

Pareamento de cabines (opcional)
--------------------------------
  O administrador pode gerar/definir um PIN temporario em /admin (guardado na
  tabela settings, chave 'booth_pairing_pin'). Enquanto houver PIN ativo, uma
  cabine NOVA so se registra informando o PIN; aprovado o pareamento, o token
  e guardado no localStorage do navegador da cabine. Sem PIN configurado o
  registro permanece aberto (compatibilidade).

Execucao
--------
  Por padrao sobe em modo PRODUCAO com o servidor WSGI Waitress (sem o aviso
  de "development server" do Flask). Use --dev para o servidor de
  desenvolvimento do Flask (apenas para depuracao local).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import secrets
import sqlite3
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from functools import wraps
from logging.handlers import RotatingFileHandler
from typing import Any, Optional

from flask import (
    Flask, g, jsonify, redirect, render_template, request, send_file, url_for,
)

# --------------------------------------------------------------------------- #
# Configuracao
# --------------------------------------------------------------------------- #
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Caminho preferido do banco (configuravel via VOTING_DB). Pode ser relativo
# (resolvido a partir do diretorio de trabalho) ou absoluto.
REQUESTED_DB_PATH = os.environ.get("VOTING_DB", os.path.join(BASE_DIR, "votacao.db"))

# Fallback usado quando o caminho preferido nao pode ser criado/escrito:
# um arquivo local dentro do diretorio do proprio script. Se ate o diretorio
# do script for somente-leitura, ha ainda um candidato no diretorio temporario.
FALLBACK_DB_PATH = os.path.join(BASE_DIR, "voting.db")

# Caminho efetivamente em uso. Resolvido por _resolve_db_path() no import.
DB_PATH = REQUESTED_DB_PATH

# Token de dispositivo: usado exclusivamente nas rotas administrativas.
DEVICE_TOKEN = os.environ.get("DEVICE_TOKEN") or secrets.token_urlsafe(24)
_TOKEN_WAS_GENERATED = "DEVICE_TOKEN" not in os.environ

CHECKIN_COOLDOWN_SECONDS = float(os.environ.get("CHECKIN_COOLDOWN_SECONDS", "2"))
RESULTS_DELAY_SECONDS = int(os.environ.get("RESULTS_DELAY_SECONDS", "5"))
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "5000"))

# --------------------------------------------------------------------------- #
# Isolamento de rede / status offline
# --------------------------------------------------------------------------- #
# O sistema roda em uma rede LOCAL dedicada (roteador isolado, sem internet).
# O rotulo e exibido no painel administrativo, no /api/health e no cabecalho da
# Ata de Fechamento, declarando que a apuracao ocorre "Rede Local / Offline".
NETWORK_MODE = os.environ.get("NETWORK_MODE", "Rede Local / Offline")

# --------------------------------------------------------------------------- #
# Atraso / batching configuravel dos resultados (anti-inferencia em tempo real)
# --------------------------------------------------------------------------- #
# A chave `results_delay_minutes` (tabela `settings`) permite agrupar a
# divulgacao PUBLICA dos votos em janelas de N minutos, dificultando que um
# observador externo correlacione o voto recem-cast ao votante que acabou de se
# credenciar. Quando 0 (default), mantem-se o atraso em segundos
# (RESULTS_DELAY_SECONDS). Limite: 0..1440 (24h).
RESULTS_DELAY_MINUTES_SETTING = "results_delay_minutes"
RESULTS_DELAY_MINUTES_DEFAULT = int(os.environ.get("RESULTS_DELAY_MINUTES", "0"))

# --------------------------------------------------------------------------- #
# Inspetor de banco de dados (somente leitura) e PIN de encerramento
# --------------------------------------------------------------------------- #
# Tabelas exibiveis pelo endpoint /api/admin/db/inspect (whitelist) e limite de
# linhas retornadas por consulta (evita respostas gigantes no painel).
DB_INSPECT_TABLES = ("polls", "voters", "votes", "booths", "settings")
DB_INSPECT_ROW_LIMIT = 500

# PIN numerico de encerramento exigido em /api/admin/close (4 a 6 digitos).
CLOSURE_PIN_MIN_LEN = 4
CLOSURE_PIN_MAX_LEN = 6

# Tabela de log de auditoria (eventos criticos do painel).
AUDIT_LOG_LIMIT_DEFAULT = 100
AUDIT_LOG_LIMIT_MAX = 500

# Identidade exibida na Ata/relatorio oficial (cabecalho do documento).
FACTORY_NAME = os.environ.get("FACTORY_NAME", "Fábrica")
REPORT_TITLE = "Ata de Fechamento — Relatório Oficial"

# --------------------------------------------------------------------------- #
# Integridade criptografica (SHA-256)
# --------------------------------------------------------------------------- #
# Ao ENCERRAR um plebiscito calculamos um digest SHA-256 sobre o estado final
# (metadados do plebiscito + votos anonimos na ordem de insercao) e o
# persisitimos em `polls.integrity_hash`. Qualquer alteracao manual posterior
# no banco (sqlite3, DB Browser, script externo) faz o digest recalculado
# divergir do armazenado -> `verify_poll_integrity()` acusa ADULTERACAO.
#
# O digest inclui um segredo de aplicacao (prefixo) para dificultar que um
# editor malicioso simplesmente recalcule o hash. O segredo pode ser fixado
# via variavel de ambiente INTEGRITY_SECRET (recomendado em producao).
INTEGRITY_ALGORITHM = "SHA-256"
INTEGRITY_SECRET = os.environ.get(
    "INTEGRITY_SECRET", "votacao-local-integrity-v1"
)

# Linhas de assinatura padrao da Ata (usadas quando nenhuma e informada).
DEFAULT_SIGNATORIES = [
    {"name": "", "role": "Presidente da Mesa", "doc": ""},
    {"name": "", "role": "Mesário(a) 1", "doc": ""},
    {"name": "", "role": "Mesário(a) 2", "doc": ""},
    {"name": "", "role": "Testemunha", "doc": ""},
]
MAX_SIGNATORIES = 8

POLL_STATUS_OPEN = "open"
POLL_STATUS_CLOSED = "closed"

# Limite de fila do check-in (padrao do sistema). Quando a fila de tokens
# atingir este valor, a tela /checkin passa a exibir ALERTA AMARELO para o
# operador pausar a leitura dos crachas. Fica persistido na tabela `settings`
# (chave `queue_warning_threshold`) e pode ser ajustado dinamicamente pelo
# painel administrativo (endpoint /api/admin/settings/queue-threshold).
QUEUE_THRESHOLD_SETTING = "queue_warning_threshold"
QUEUE_WARNING_THRESHOLD_DEFAULT = int(os.environ.get("QUEUE_WARNING_THRESHOLD", "5"))

# Chave da tabela `settings` que guarda um PIN secundario opcional exigido na
# purga do banco (alem do proprio Device Token). Vazio = apenas o token vale.
PURGE_PIN_SETTING = "purge_pin"

# Tabelas minimas que um arquivo .db precisa conter para ser aceito como
# backup restauravel (validacao de schema no /api/admin/backup/restore).
REQUIRED_TABLES = frozenset({"voters", "polls", "votes", "settings", "booths"})

# Rastreamento de cabines (kiosks). Sem heartbeat por BOOTH_TIMEOUT_SECONDS a
# cabine e considerada offline. Apos receber uma liberacao de check-in, a
# cabine fica reservada por BOOTH_BUSY_SECONDS (ou ate reportar-se ociosa).
BOOTH_TIMEOUT_SECONDS = float(os.environ.get("BOOTH_TIMEOUT_SECONDS", "20"))
BOOTH_BUSY_SECONDS = float(os.environ.get("BOOTH_BUSY_SECONDS", "45"))

# Serializa as operacoes de leitura-modificacao-escrita nos contadores.
_DB_LOCK = threading.RLock()

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

# Habilitar no modo DEV para expor o traceback tambem na resposta (uso local).
if os.environ.get("FLASK_DEBUG") not in (None, "", "0", "false", "False"):
    app.config["PROPAGATE_EXCEPTIONS"] = True


# --------------------------------------------------------------------------- #
# Logging (console + arquivo rotativo)
# --------------------------------------------------------------------------- #
# Todos os eventos relevantes (boot, autenticacao admin, ciclo de vida dos
# plebiscitos, cabines e erros internos) passam por este logger. A saida vai
# para o terminal (stdout) e para um arquivo rotativo ``app.log`` na raiz do
# projeto, para diagnostico posterior.
LOG_PATH = os.environ.get("APP_LOG", os.path.join(BASE_DIR, "app.log"))
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"
LOG_MAX_BYTES = int(os.environ.get("APP_LOG_MAX_BYTES", str(1_000_000)))
LOG_BACKUP_COUNT = int(os.environ.get("APP_LOG_BACKUP_COUNT", "3"))


def _configure_logging() -> logging.Logger:
    """Configura o logger da aplicacao (idempotente).

    Anexa um handler de console (stdout, com timestamp e nivel) e um handler
    de arquivo rotativo (``app.log``). Se o arquivo nao puder ser aberto (ex.:
    diretorio somente-leitura), a aplicacao segue apenas com o console, sem
    abortar o boot.
    """
    log = logging.getLogger("votacao")
    log.setLevel(logging.INFO)
    log.propagate = False  # evita duplicar no root logger do Flask/Waitress
    if log.handlers:       # ja configurado (ex.: reimport do modulo)
        return log

    formatter = logging.Formatter(LOG_FORMAT, LOG_DATEFMT)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)
    log.addHandler(console)

    try:
        file_handler = RotatingFileHandler(
            LOG_PATH, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(formatter)
        log.addHandler(file_handler)
    except OSError as exc:
        log.warning("Nao foi possivel abrir o arquivo de log '%s': %s",
                    LOG_PATH, exc)

    return log


logger = _configure_logging()


# --------------------------------------------------------------------------- #
# Camada de banco de dados
# --------------------------------------------------------------------------- #
def _ensure_db_directory(path: str) -> bool:
    """Garante que o diretorio do banco exista e seja gravavel.

    Cria o diretorio pai (se houver) com ``os.makedirs(..., exist_ok=True)``
    ANTES de qualquer ``sqlite3.connect()``. Retorna True se o diretorio esta
    pronto para receber o arquivo .db (e os auxiliares -wal/-shm).
    """
    target = os.path.abspath(path)
    directory = os.path.dirname(target) or BASE_DIR
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as exc:
        print(f"[db] Nao foi possivel criar o diretorio '{directory}': {exc}")
        return False

    if not os.path.isdir(directory):
        print(f"[db] O caminho pai '{directory}' nao e um diretorio.")
        return False

    # O diretorio precisa permitir escrita: alem do .db, o SQLite cria os
    # arquivos auxiliares -wal e -shm no mesmo diretorio (modo WAL).
    if not os.access(directory, os.W_OK):
        print(f"[db] Sem permissao de escrita no diretorio '{directory}'.")
        return False

    # Se o arquivo ja existe, ele proprio tambem precisa ser gravavel.
    if os.path.exists(target) and not os.access(target, os.W_OK):
        print(f"[db] Sem permissao de escrita no arquivo '{target}'.")
        return False

    return True


def _can_open(path: str) -> bool:
    """Testa abrir/conectar no banco sem deixar conexao pendurada."""
    try:
        probe = sqlite3.connect(path, timeout=10)
    except sqlite3.Error as exc:
        print(f"[db] Falha ao abrir '{path}': {exc}")
        return False
    try:
        probe.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error as exc:
        print(f"[db] Arquivo '{path}' nao parece ser um banco valido: {exc}")
        return False
    finally:
        probe.close()
    return True


def _fallback_candidates() -> list[str]:
    """Caminhos alternativos, em ordem de preferencia.

    1) 'voting.db' no diretorio do script (requisito);
    2) 'voting.db' no diretorio temporario do sistema (ultimo recurso, util
       quando o proprio diretorio do script esta em um volume somente-leitura).
    """
    candidates = [os.path.abspath(FALLBACK_DB_PATH)]
    try:
        tmp = os.path.abspath(os.path.join(tempfile.gettempdir(), "voting.db"))
        if tmp not in candidates:
            candidates.append(tmp)
    except OSError:
        pass
    return candidates


def _resolve_db_path() -> str:
    """Escolhe o caminho de banco utilizavel.

    Tenta o caminho configurado (VOTING_DB). Se o diretorio nao puder ser
    criado/escrito ou a abertura falhar, percorre os candidatos de fallback
    (diretorio do script e, por fim, diretorio temporario).
    """
    global DB_PATH

    requested = os.path.abspath(REQUESTED_DB_PATH)
    if _ensure_db_directory(requested) and _can_open(requested):
        DB_PATH = requested
        return DB_PATH

    print(f"[db] AVISO: nao foi possivel usar '{requested}'.")
    for candidate in _fallback_candidates():
        if _ensure_db_directory(candidate) and _can_open(candidate):
            print(f"[db]        Usando fallback '{candidate}'.")
            DB_PATH = candidate
            return DB_PATH
        print(f"[db]        Fallback '{candidate}' indisponivel.")

    print(
        "[db] ERRO FATAL: nenhum caminho gravavel disponivel.\n"
        "[db]             Defina VOTING_DB para um diretorio com permissoes."
    )
    DB_PATH = os.path.abspath(FALLBACK_DB_PATH)
    return DB_PATH


def _connect() -> sqlite3.Connection:
    """Abre uma conexao SQLite configurada (WAL + row factory).

    Garante que o diretorio exista antes de conectar; se a abertura falhar,
    emite uma mensagem clara e tenta os fallbacks antes de propagar o erro.
    """
    global DB_PATH

    path = os.path.abspath(DB_PATH)
    _ensure_db_directory(path)
    try:
        conn = sqlite3.connect(path, timeout=30)
    except sqlite3.OperationalError as exc:
        print(f"[db] ERRO: nao foi possivel abrir o banco em '{path}': {exc}")
        conn = None
        last_error: sqlite3.Error = exc
        for candidate in _fallback_candidates():
            if candidate == path:
                continue
            print(f"[db] Tentando fallback '{candidate}'...")
            _ensure_db_directory(candidate)
            try:
                conn = sqlite3.connect(candidate, timeout=30)
            except sqlite3.Error as exc2:
                last_error = exc2
                print(f"[db] Fallback '{candidate}' falhou: {exc2}")
                continue
            DB_PATH = candidate
            print(f"[db] Conectado ao fallback '{candidate}'.")
            break
        if conn is None:
            print("[db] ERRO FATAL: nenhum fallback disponivel.")
            raise last_error

    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
    except sqlite3.Error as exc:
        conn.close()
        print(f"[db] ERRO ao configurar a conexao em '{DB_PATH}': {exc}")
        raise
    return conn


# Resolve o caminho assim que o modulo carrega (falha cedo, com mensagem clara).
_resolve_db_path()


def get_db() -> sqlite3.Connection:
    """Retorna a conexao da requisicao atual (uma por request)."""
    if "db" not in g:
        g.db = _connect()
    return g.db


@app.teardown_appcontext
def _close_db(_exc: Optional[BaseException]) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    """Cria o schema se ainda nao existir. Idempotente."""
    try:
        conn = _connect()
    except sqlite3.Error as exc:
        print(
            f"[db] ERRO FATAL: nao foi possivel inicializar o banco em "
            f"'{DB_PATH}': {exc}\n"
            "[db]             Verifique as permissoes do diretorio ou defina "
            "VOTING_DB para um caminho gravavel."
        )
        raise
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS voters (
                badge_id   TEXT PRIMARY KEY,
                voted_at   TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS polls (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                question             TEXT NOT NULL,
                options              TEXT NOT NULL,          -- JSON: [{"id","label"}]
                status               TEXT NOT NULL DEFAULT 'open',
                created_at           TEXT NOT NULL,
                closed_at            TEXT,
                integrity_hash       TEXT,                   -- digest SHA-256 do estado selado
                integrity_algorithm  TEXT,                   -- ex.: 'SHA-256'
                closure_pin          TEXT                    -- PIN (4-6 digitos) p/ encerrar
            );

            -- ----------------------------------------------------------------- #
            -- VOTOS ANONIMOS.
            -- A tabela `votes` contem ESTRITAMENTE (id, poll_id, option_label):
            --   * SEM chave estrangeira (nenhum vinculo com `voters`/cabines);
            --   * SEM carimbo de tempo por voto (created_at);
            --   * SEM badge_id / voter_id / IP.
            -- Como nao existe nenhuma coluna que identifique o votante, e
            -- MATEMATICAMENTE IMPOSSIVEL correlacionar um votante a sua escolha.
            -- ----------------------------------------------------------------- #
            CREATE TABLE IF NOT EXISTS votes (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                poll_id      INTEGER NOT NULL,
                option_label TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS booths (
                booth_id      TEXT PRIMARY KEY,        -- id estavel do kiosk (cliente)
                number        INTEGER NOT NULL,        -- numero amigavel (1,2,3...)
                label         TEXT NOT NULL,            -- "Cabine 01"
                status        TEXT NOT NULL DEFAULT 'idle',  -- idle | voting
                last_seen     TEXT NOT NULL,            -- ultimo heartbeat (ISO/UTC)
                busy_until    TEXT,                     -- reserva ate (ISO/UTC) ou NULL
                client_ip     TEXT,                     -- ultimo IP observado (registro/heartbeat)
                pairing_token TEXT,                     -- token de pareamento (prova de emparelhamento)
                registered_at TEXT NOT NULL
            );

            -- ----------------------------------------------------------------- #
            -- LOG DE AUDITORIA (eventos criticos do painel administrativo).
            -- Registra QUEM fez O QUE e QUANDO (IP + instante UTC). Nunca guarda
            -- o vinculo votante->voto; apenas eventos operacionais.
            -- ----------------------------------------------------------------- #
            CREATE TABLE IF NOT EXISTS audit_logs (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                event      TEXT NOT NULL,            -- ex.: 'poll_start','poll_close','booth_pair'
                detail     TEXT NOT NULL DEFAULT '', -- descricao legivel (sem dados sensiveis)
                poll_id    INTEGER,                  -- plebiscito relacionado (quando aplicavel)
                actor_ip   TEXT,                     -- IP do operador/cliente
                created_at TEXT NOT NULL             -- instante UTC (ISO)
            );

            CREATE INDEX IF NOT EXISTS idx_votes_poll ON votes(poll_id);
            CREATE INDEX IF NOT EXISTS idx_booths_number ON booths(number);
            CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_logs(created_at);
            """
        )
        # Migra bancos ja existentes (adiciona colunas novas de forma idempotente).
        _migrate_schema(conn)
        # Valores iniciais dos contadores globais.
        conn.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES('voting_tokens', '0')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES('active_poll_id', '')"
        )
        # Limite de fila do check-in (Alerta Amarelo) - default configuravel.
        conn.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)",
            (QUEUE_THRESHOLD_SETTING, str(QUEUE_WARNING_THRESHOLD_DEFAULT)),
        )
        # ------------------------------------------------------------------ #
        # Reset do estado VOLATIL de runtime a cada boot do servidor.
        # Evita que check-ins/reservas orfaos de execucoes anteriores deixem a
        # fila de tokens "presa" ou cabines travadas como ocupadas.
        # NAO toca em dados persistentes: polls, votos, historico e crachas
        # (voters) permanecem intactos.
        # ------------------------------------------------------------------ #
        conn.execute("UPDATE booths SET status = 'idle', busy_until = NULL")
        conn.execute(
            "INSERT INTO settings(key, value) VALUES('voting_tokens', '0') "
            "ON CONFLICT(key) DO UPDATE SET value = '0'"
        )
        conn.commit()
    except sqlite3.Error as exc:
        print(f"[db] ERRO ao criar/atualizar o schema em '{DB_PATH}': {exc}")
        raise
    finally:
        conn.close()


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Adiciona colunas/tabelas novas a bancos ja existentes (idempotente).

    Necessario porque ``CREATE TABLE IF NOT EXISTS`` nao altera tabelas
    preexistentes: sem isto um ``votacao.db`` antigo ficaria sem as colunas
    de IP/pareamento (booths), sem as colunas de integridade (polls) e, o mais
    importante, com a tabela ``votes`` no formato antigo (``option_id`` +
    ``created_at`` + FK), que permite correlacionar o voto ao votante. Aqui a
    ``votes`` e reconstruida para o formato ANONIMO estrito
    ``(id, poll_id, option_label)``.
    """
    # --- booths: colunas de IP/pareamento --------------------------------- #
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(booths)")}
    for column in ("client_ip", "pairing_token"):
        if column not in existing:
            conn.execute(f"ALTER TABLE booths ADD COLUMN {column} TEXT")

    # --- polls: colunas de integridade (SHA-256) -------------------------- #
    poll_cols = {row["name"] for row in conn.execute("PRAGMA table_info(polls)")}
    if "integrity_hash" not in poll_cols:
        conn.execute("ALTER TABLE polls ADD COLUMN integrity_hash TEXT")
    if "integrity_algorithm" not in poll_cols:
        conn.execute("ALTER TABLE polls ADD COLUMN integrity_algorithm TEXT")
    # PIN de encerramento (bancos criados antes desta funcionalidade).
    if "closure_pin" not in poll_cols:
        conn.execute("ALTER TABLE polls ADD COLUMN closure_pin TEXT")

    # --- audit_logs: garante a tabela em bancos antigos/restaurados -------- #
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_logs (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            event      TEXT NOT NULL,
            detail     TEXT NOT NULL DEFAULT '',
            poll_id    INTEGER,
            actor_ip   TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_logs(created_at)"
    )

    # --- votes: migra o formato antigo para o anonimo estrito ------------- #
    _migrate_votes_schema(conn)


def _migrate_votes_schema(conn: sqlite3.Connection) -> None:
    """Reconstroi ``votes`` para ``(id, poll_id, option_label)`` se preciso.

    Bancos antigos guardavam ``option_id`` + ``created_at`` e uma FK para
    ``polls`` - o ``created_at`` permitia, em tese, correlacionar a ordem dos
    check-ins com a ordem dos votos. A migracao converte ``option_id`` em
    ``option_label`` (via o JSON de opcoes do plebiscito) e DESCARTA o
    carimbo de tempo, garantindo o anonimato absoluto.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(votes)")}
    if not cols or "option_label" in cols:
        # Tabela no formato novo (ou recem-criada) -> nada a fazer.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_votes_poll ON votes(poll_id)")
        return

    # Mapeia option_id -> label usando o JSON de opcoes de cada plebiscito.
    label_by_id: dict[str, str] = {}
    for row in conn.execute("SELECT options FROM polls"):
        try:
            opts = json.loads(row["options"])
        except (TypeError, ValueError):
            continue
        for opt in opts or []:
            if isinstance(opt, dict):
                label_by_id[str(opt.get("id"))] = str(opt.get("label", ""))

    legacy = []
    if "option_id" in cols:
        legacy = conn.execute(
            "SELECT id, poll_id, option_id FROM votes ORDER BY id"
        ).fetchall()

    conn.execute("DROP INDEX IF EXISTS idx_votes_poll")
    conn.execute("ALTER TABLE votes RENAME TO votes_legacy")
    conn.execute(
        """
        CREATE TABLE votes (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            poll_id      INTEGER NOT NULL,
            option_label TEXT NOT NULL
        )
        """
    )
    for row in legacy:
        conn.execute(
            "INSERT INTO votes(id, poll_id, option_label) VALUES(?, ?, ?)",
            (row["id"], row["poll_id"],
             label_by_id.get(str(row["option_id"]), str(row["option_id"]))),
        )
    conn.execute("DROP TABLE votes_legacy")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_votes_poll ON votes(poll_id)")


# --------------------------------------------------------------------------- #
# Helpers de dominio
# --------------------------------------------------------------------------- #
def now_iso() -> str:
    """Timestamp UTC ISO-8601 truncado ao segundo (ordenavel lexicograficamente)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_setting(db: sqlite3.Connection, key: str, default: str = "") -> str:
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(db: sqlite3.Connection, key: str, value: Any) -> None:
    db.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


# --------------------------------------------------------------------------- #
# Limite de fila do check-in (Alerta Amarelo)
# --------------------------------------------------------------------------- #
def get_queue_threshold(db: sqlite3.Connection) -> int:
    """Limite da fila que dispara o alerta amarelo no check-in (>= 1)."""
    raw = get_setting(db, QUEUE_THRESHOLD_SETTING, str(QUEUE_WARNING_THRESHOLD_DEFAULT))
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        value = QUEUE_WARNING_THRESHOLD_DEFAULT
    return max(1, value)


def set_queue_threshold(db: sqlite3.Connection, value: Any) -> int:
    """Define o limite da fila (1..9999). Levanta ``ValueError`` se invalido."""
    try:
        threshold = int(value)
    except (TypeError, ValueError):
        raise ValueError("invalid_threshold")
    if threshold < 1 or threshold > 9999:
        raise ValueError("threshold_out_of_range")
    set_setting(db, QUEUE_THRESHOLD_SETTING, threshold)
    return threshold


# --------------------------------------------------------------------------- #
# Atraso / batching configuravel dos resultados (anti-inferencia)
# --------------------------------------------------------------------------- #
def get_results_delay_minutes(db: sqlite3.Connection) -> int:
    """Janela PUBLICA de divulgacao em minutos (0 = usa o atraso em segundos)."""
    raw = get_setting(
        db, RESULTS_DELAY_MINUTES_SETTING, str(RESULTS_DELAY_MINUTES_DEFAULT)
    )
    try:
        minutes = int(str(raw).strip())
    except (TypeError, ValueError):
        minutes = RESULTS_DELAY_MINUTES_DEFAULT
    return max(0, min(1440, minutes))


def set_results_delay_minutes(db: sqlite3.Connection, value: Any) -> int:
    """Define a janela em minutos (0..1440). Levanta ``ValueError`` se invalida."""
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        raise ValueError("invalid_minutes")
    if minutes < 0 or minutes > 1440:
        raise ValueError("minutes_out_of_range")
    set_setting(db, RESULTS_DELAY_MINUTES_SETTING, minutes)
    return minutes


def effective_results_delay_seconds(db: sqlite3.Connection) -> int:
    """Atraso efetivo em SEGUNDOS usado por ``/api/results``.

    Quando ``results_delay_minutes`` > 0, a divulgacao publica e agrupada em
    janelas de N minutos (batching anti-inferencia). Caso contrario, mantem-se o
    atraso classico em segundos (``RESULTS_DELAY_SECONDS``).
    """
    minutes = get_results_delay_minutes(db)
    return minutes * 60 if minutes > 0 else RESULTS_DELAY_SECONDS


# --------------------------------------------------------------------------- #
# Log de auditoria (eventos criticos do painel)
# --------------------------------------------------------------------------- #
def ensure_audit_logs_table(db: sqlite3.Connection) -> None:
    """Garante a existencia da tabela ``audit_logs`` (bancos antigos/restaurados)."""
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_logs (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            event      TEXT NOT NULL,
            detail     TEXT NOT NULL DEFAULT '',
            poll_id    INTEGER,
            actor_ip   TEXT,
            created_at TEXT NOT NULL
        )
        """
    )


def write_audit_log(
    db: sqlite3.Connection,
    event: str,
    detail: str = "",
    poll_id: Optional[int] = None,
    actor_ip: Optional[str] = None,
) -> None:
    """Grava um evento de auditoria (nunca dados sensiveis nem vinculo voto-votante)."""
    ensure_audit_logs_table(db)
    db.execute(
        "INSERT INTO audit_logs(event, detail, poll_id, actor_ip, created_at) "
        "VALUES(?, ?, ?, ?, ?)",
        (str(event)[:60], str(detail)[:500], poll_id, actor_ip, now_iso()),
    )
    db.commit()


def list_audit_logs(db: sqlite3.Connection, limit: int = AUDIT_LOG_LIMIT_DEFAULT) -> list[dict]:
    """Lista os eventos mais recentes do log de auditoria (ordem decrescente)."""
    ensure_audit_logs_table(db)
    limit = max(1, min(AUDIT_LOG_LIMIT_MAX, int(limit)))
    rows = db.execute(
        "SELECT id, event, detail, poll_id, actor_ip, created_at "
        "FROM audit_logs ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


# --------------------------------------------------------------------------- #
# Verificacao secundaria da purga (Device Token ou PIN dedicado)
# --------------------------------------------------------------------------- #
def get_purge_pin(db: sqlite3.Connection) -> str:
    """PIN secundario opcional exigido na purga (string vazia = desativado)."""
    return get_setting(db, PURGE_PIN_SETTING, "").strip()


def secondary_verified(db: sqlite3.Connection, provided: str) -> bool:
    """True se a confirmacao secundaria confere com o Device Token ou o PIN.

    Aceita tanto o ``DEVICE_TOKEN`` ativo (o mesmo header que autentica o
    painel) quanto um ``purge_pin`` dedicado, quando configurado. Comparacoes em
    tempo constante; string vazia nunca e valida.
    """
    provided = str(provided or "").strip()
    if not provided:
        return False
    if _token_equal(provided, DEVICE_TOKEN):
        return True
    pin = get_purge_pin(db)
    return bool(pin) and _pin_matches(provided, pin)


# --------------------------------------------------------------------------- #
# Pareamento de cabines (Pairing PIN)
# --------------------------------------------------------------------------- #
PAIRING_PIN_SETTING = "booth_pairing_pin"


def get_pairing_pin(db: sqlite3.Connection) -> str:
    """PIN atual de pareamento (string vazia = pareamento desativado)."""
    return get_setting(db, PAIRING_PIN_SETTING, "").strip()


def generate_pairing_pin() -> str:
    """Gera um PIN numerico temporario de 6 digitos."""
    return f"{secrets.randbelow(1_000_000):06d}"


def generate_closure_pin(length: int = CLOSURE_PIN_MAX_LEN) -> str:
    """Gera um PIN numerico de encerramento (default 6 digitos, min. 4).

    O PIN e sorteado no inicio do plebiscito e exibido SOMENTE no painel
    administrativo. Encerrar o plebiscito (``/api/admin/close``) exige este
    codigo, evitando que um operador encerre a votacao por engano/sozinho.
    """
    digits = max(CLOSURE_PIN_MIN_LEN, min(CLOSURE_PIN_MAX_LEN, int(length)))
    return "".join(str(secrets.randbelow(10)) for _ in range(digits))


def set_pairing_pin(db: sqlite3.Connection, pin: Any) -> str:
    """Define (ou limpa, com string vazia) o PIN de pareamento.

    Levanta ``ValueError`` se o PIN nao for alfanumerico ou tiver tamanho
    fora de 4..16 caracteres (quando nao vazio).
    """
    pin = str(pin or "").strip()
    if pin == "":
        set_setting(db, PAIRING_PIN_SETTING, "")
        db.commit()
        return ""
    if len(pin) < 4 or len(pin) > 16:
        raise ValueError("pin_length")
    if not pin.isalnum():
        raise ValueError("pin_alphanumeric")
    set_setting(db, PAIRING_PIN_SETTING, pin)
    db.commit()
    return pin


def _pin_matches(provided: str, required: str) -> bool:
    """Compara o PIN informado com o configurado (tempo constante)."""
    if not provided or not required:
        return False
    try:
        return secrets.compare_digest(str(provided), str(required))
    except (TypeError, ValueError):
        return False


def _token_equal(a: str, b: str) -> bool:
    """Compara dois tokens em tempo constante."""
    if not a or not b:
        return False
    try:
        return secrets.compare_digest(str(a), str(b))
    except (TypeError, ValueError):
        return False


def get_voting_tokens(db: sqlite3.Connection) -> int:
    try:
        return int(get_setting(db, "voting_tokens", "0"))
    except (TypeError, ValueError):
        return 0


def set_voting_tokens(db: sqlite3.Connection, value: int) -> int:
    value = max(0, int(value))
    set_setting(db, "voting_tokens", value)
    return value


def get_active_poll_id(db: sqlite3.Connection) -> Optional[int]:
    raw = get_setting(db, "active_poll_id", "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _row_to_poll(row: sqlite3.Row) -> dict:
    """Serializa uma linha de poll (inclui options decodificado)."""
    try:
        options = json.loads(row["options"])
    except (TypeError, ValueError):
        options = []
    return {
        "id": row["id"],
        "question": row["question"],
        "options": options,
        "status": row["status"],
        "created_at": row["created_at"],
        "closed_at": row["closed_at"],
    }


def get_poll(db: sqlite3.Connection, poll_id: int) -> Optional[dict]:
    row = db.execute("SELECT * FROM polls WHERE id = ?", (poll_id,)).fetchone()
    return _row_to_poll(row) if row else None


def get_open_poll(db: sqlite3.Connection) -> Optional[dict]:
    """Retorna a poll ativa SOMENTE se ela existe e esta aberta."""
    poll_id = get_active_poll_id(db)
    if poll_id is None:
        return None
    poll = get_poll(db, poll_id)
    if poll is None or poll["status"] != POLL_STATUS_OPEN:
        return None
    return poll


def tally_votes(db: sqlite3.Connection, poll_id: int) -> dict:
    """Conta votos por OPTION_LABEL (a tabela `votes` nao guarda option_id).

    O voto e armazenado apenas como rotulo textual da opcao escolhida; nao ha
    carimbo de tempo nem identificacao do votante. Nao existe, portanto,
    qualquer filtro temporal aqui: o atraso controlado dos resultados e
    aplicado exclusivamente na divulgacao PUBLICA (`/api/results`).
    """
    rows = db.execute(
        "SELECT option_label, COUNT(*) AS total FROM votes "
        "WHERE poll_id = ? GROUP BY option_label",
        (poll_id,),
    ).fetchall()
    return {r["option_label"]: r["total"] for r in rows}


def _revealable_vote_count(
    db: sqlite3.Connection, poll: dict, cutoff_iso: Optional[str]
) -> int:
    """Quantos votos podem ser divulgados PUBLICAMENTE agora.

    Combina duas fontes que NAO identificam o voto nem o votante:
      * o plebiscito ja FECHADO -> revela 100% dos votos;
      * o plebiscito ABERTO -> revela apenas os votos mais antigos que a
        janela de carencia, estimada pela quantidade de check-ins cujo
        ``voted_at`` ja e anterior ao cutoff (tabela `voters`, que registra
        apenas QUEM compareceu e QUANDO - nunca o voto).

    Desta forma o painel publico nao consegue associar o voto recem-cast ao
    votante que acabou de se credenciar, preservando o sigilo.
    """
    total = db.execute(
        "SELECT COUNT(*) AS c FROM votes WHERE poll_id = ?", (poll["id"],)
    ).fetchone()["c"]
    if total == 0:
        return 0
    if poll.get("status") == POLL_STATUS_CLOSED:
        return total
    if cutoff_iso is None:
        return total
    checked_in = db.execute(
        "SELECT COUNT(*) AS c FROM voters WHERE voted_at <= ?", (cutoff_iso,)
    ).fetchone()["c"]
    return max(0, min(int(checked_in), int(total)))


def tally_visible_votes(
    db: sqlite3.Connection, poll: dict, cutoff_iso: Optional[str]
) -> dict:
    """Contagem PUBLICA (com atraso controlado) por option_label.

    Pega os N votos mais antigos (`N = _revealable_vote_count`) - N e derivado
    da carencia, nunca de um vinculo votante->voto.
    """
    limit = _revealable_vote_count(db, poll, cutoff_iso)
    if limit <= 0:
        return {}
    rows = db.execute(
        "SELECT option_label FROM votes WHERE poll_id = ? ORDER BY id LIMIT ?",
        (poll["id"], limit),
    ).fetchall()
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["option_label"]] = counts.get(r["option_label"], 0) + 1
    return counts


def clear_voters(db: sqlite3.Connection) -> None:
    """Remove todos os crachas registrados (libera todos para novo pleito)."""
    db.execute("DELETE FROM voters")


def votes_per_option(poll: dict, counts: dict) -> list[dict]:
    """Monta a lista de opcoes com contagem (garante ordem de declaracao).

    Os votos sao agregados por LABEL (a tabela `votes` guarda apenas o rotulo
    textual e nenhum id de opcao).
    """
    result = []
    for opt in poll["options"]:
        label = opt.get("label", "")
        result.append(
            {
                "id": opt.get("id"),
                "label": label,
                "count": int(counts.get(label, 0)),
            }
        )
    return result


# --------------------------------------------------------------------------- #
# Relatorio oficial (Ata de fechamento)
# --------------------------------------------------------------------------- #
def count_registered_voters(db: sqlite3.Connection) -> int:
    """Numero de votantes unicos registrados (chaves da tabela `voters`)."""
    return db.execute("SELECT COUNT(*) AS c FROM voters").fetchone()["c"]


def _closed_voters_key(poll_id: int) -> str:
    return f"closed_poll_voters:{int(poll_id)}"


def preserve_closed_voters(db: sqlite3.Connection, poll_id: int) -> None:
    """Guarda a contagem de votantes ANTES de limpar os crachas no fechamento.

    A tabela `voters` e limpa ao encerrar/trocar de plebiscito (privacidade e
    liberacao para novo voto). Persistimos o total em `settings` para que a Ata
    de Fechamento continue exibindo o numero de votantes. Idempotente: so grava
    na transicao OPEN -> CLOSED (evita sobrescrever com 0 em re-fechamentos).
    """
    poll = get_poll(db, poll_id)
    if poll is None or poll.get("status") != POLL_STATUS_OPEN:
        return
    set_setting(db, _closed_voters_key(poll_id), count_registered_voters(db))


def total_voters_for_poll(
    db: sqlite3.Connection, poll: Optional[dict]
) -> int:
    """Votantes unicos do plebiscito (usa a contagem preservada se encerrado)."""
    if poll is not None and poll.get("status") == POLL_STATUS_CLOSED:
        stored = get_setting(db, _closed_voters_key(poll["id"]), "").strip()
        if stored:
            try:
                return int(stored)
            except ValueError:
                pass
    return count_registered_voters(db)


def _sha256_hex(payload: str) -> str:
    """Digest SHA-256 (hex) sobre o payload, prefixado pelo segredo da app."""
    material = (INTEGRITY_SECRET + "\x1f" + payload).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _integrity_canonical_payload(db: sqlite3.Connection, poll: dict) -> str:
    """Serializacao CANONICA e determinista do estado de um plebiscito.

    Inclui os metadados do plebiscito e TODOS os votos anonimos na ordem de
    insercao (id|poll_id|option_label). Qualquer mudanca em pergunta, opcoes,
    timestamps, status ou em QUALQUER voto altera o digest resultante.
    """
    votes = db.execute(
        "SELECT id, poll_id, option_label FROM votes WHERE poll_id = ? "
        "ORDER BY id",
        (poll["id"],),
    ).fetchall()
    vote_lines = ";".join(
        f"{v['id']}|{v['poll_id']}|{v['option_label']}" for v in votes
    )
    meta = "|".join(
        [
            f"id={poll['id']}",
            f"question={poll['question']}",
            "options=" + json.dumps(
                poll.get("options") or [], ensure_ascii=False, sort_keys=True
            ),
            f"status={poll.get('status') or ''}",
            f"created_at={poll.get('created_at') or ''}",
            f"closed_at={poll.get('closed_at') or ''}",
            f"votes={len(votes)}",
        ]
    )
    return f"{INTEGRITY_ALGORITHM}\n{meta}\n--votes--\n{vote_lines}"


def compute_poll_integrity_hash(
    db: sqlite3.Connection, poll_id: int
) -> Optional[str]:
    """Calcula (sem gravar) o digest SHA-256 do estado atual do plebiscito."""
    poll = get_poll(db, poll_id)
    if poll is None:
        return None
    return _sha256_hex(_integrity_canonical_payload(db, poll))


def seal_poll_integrity(db: sqlite3.Connection, poll_id: int) -> Optional[str]:
    """Sela o plebiscito gravando o digest SHA-256 em ``polls.integrity_hash``.

    Deve ser chamado DEPOIS do fechamento (``status='closed'`` e
    ``closed_at`` preenchidos) e com todos os votos ja persistidos.
    """
    digest = compute_poll_integrity_hash(db, poll_id)
    if digest is None:
        return None
    db.execute(
        "UPDATE polls SET integrity_hash = ?, integrity_algorithm = ? "
        "WHERE id = ?",
        (digest, INTEGRITY_ALGORITHM, poll_id),
    )
    return digest


def verify_poll_integrity(db: sqlite3.Connection, poll_id: int) -> dict:
    """Verifica se o estado atual do plebiscito confere com o digest gravado.

    Retorna um dicionario com ``valid`` True/False. Se o banco foi alterado
    externamente (sqlite3, DB Browser, script) DEPOIS do fechamento, o digest
    recalculado diverge do selado e ``valid`` e False -> aduteracao detectada.
    """
    row = db.execute(
        "SELECT integrity_hash, integrity_algorithm FROM polls WHERE id = ?",
        (poll_id,),
    ).fetchone()
    if row is None:
        return {
            "ok": False, "valid": False, "reason": "unknown_poll",
            "poll_id": poll_id, "algorithm": INTEGRITY_ALGORITHM,
            "expected": None, "actual": None,
        }

    stored = row["integrity_hash"]
    algorithm = row["integrity_algorithm"] or INTEGRITY_ALGORITHM
    actual = compute_poll_integrity_hash(db, poll_id)

    if not stored:
        return {
            "ok": True, "valid": False, "reason": "not_sealed",
            "poll_id": poll_id, "algorithm": algorithm,
            "expected": None, "actual": actual, "sealed": False,
        }

    valid = bool(actual) and _token_equal(str(stored), str(actual))
    return {
        "ok": True,
        "valid": valid,
        "reason": "ok" if valid else "checksum_mismatch",
        "poll_id": poll_id,
        "algorithm": algorithm,
        "expected": stored,
        "actual": actual,
        "sealed": True,
    }


def poll_integrity_report(
    db: sqlite3.Connection, poll: Optional[dict]
) -> Optional[dict]:
    """Bloco de integridade para o relatorio/Ata (hash + validacao)."""
    if poll is None:
        return None
    result = verify_poll_integrity(db, poll["id"])
    polls_row = db.execute(
        "SELECT integrity_hash FROM polls WHERE id = ?", (poll["id"],)
    ).fetchone()
    stored = polls_row["integrity_hash"] if polls_row else None
    return {
        "algorithm": result["algorithm"],
        "hash": stored or result["actual"],
        "computed": result["actual"],
        "sealed": bool(stored),
        "valid": result["valid"] if stored else None,
        "reason": result["reason"],
        "votes_hashed": db.execute(
            "SELECT COUNT(*) AS c FROM votes WHERE poll_id = ?", (poll["id"],)
        ).fetchone()["c"],
    }


def _business_rules(db: sqlite3.Connection) -> dict:
    """Parametros de negocio usados nas declaracoes dos relatorios."""
    return {
        "checkin_cooldown": CHECKIN_COOLDOWN_SECONDS,
        "results_delay": effective_results_delay_seconds(db),
        "results_delay_seconds": RESULTS_DELAY_SECONDS,
        "results_delay_minutes": get_results_delay_minutes(db),
        "network_mode": NETWORK_MODE,
        "single_token_policy": True,
        "pairing_enabled": bool(get_pairing_pin(db)),
    }



def report_payload(db: sqlite3.Connection) -> dict:
    """Agrega o resultado final do plebiscito ativo/encerrado para a Ata.

    Inclui: pergunta, opcoes com contagem e percentual, total de votos,
    timestamps de abertura/encerramento, o total de votantes unicos, o digest
    de integridade (SHA-256) e os parametros de negocio/declaracao de sigilo.
    """
    poll_id = get_active_poll_id(db)
    poll = get_poll(db, poll_id) if poll_id is not None else None

    rules = _business_rules(db)
    checkins = total_voters_for_poll(db, poll)
    payload: dict[str, Any] = {
        "ok": True,
        "factory_name": FACTORY_NAME,
        "title": REPORT_TITLE,
        "generated_at": now_iso(),
        "total_voters_registered": checkins,
        "rules": rules,
        "checkin_cooldown": rules["checkin_cooldown"],
        "results_delay": rules["results_delay"],
        "results_delay_minutes": rules["results_delay_minutes"],
        "network_isolation": rules["network_mode"],
        "pairing_enabled": rules["pairing_enabled"],
        "single_token_policy": rules["single_token_policy"],
        "integrity": None,
        "poll": None,
        "reconciliation": None,
    }
    if poll is None:
        return payload

    counts = tally_votes(db, poll["id"])
    options = votes_per_option(poll, counts)
    total = sum(opt["count"] for opt in options)
    for opt in options:
        opt["percent"] = round((opt["count"] / total * 100) if total else 0.0, 1)

    payload["poll"] = {
        "id": poll["id"],
        "question": poll["question"],
        "status": poll["status"],
        "created_at": poll["created_at"],
        "closed_at": poll["closed_at"],
        "options": options,
        "total_votes": total,
    }
    payload["integrity"] = poll_integrity_report(db, poll)
    # Divergencia check-ins x votos (auditoria de comparecimento).
    payload["checkins_vs_votes"] = {
        "checkins": checkins,
        "votes": total,
        "variance": checkins - total,
    }
    # Conciliacao de fechamento: cada credencial (check-in) deve corresponder a
    # um voto registrado OU a um token ainda na fila (votante aguardando cabine).
    #   Check-ins Totais == Votos Apurados + Tokens Ativos
    active_tokens = get_voting_tokens(db)
    expected = total + active_tokens
    payload["reconciliation"] = {
        "checkins_total": checkins,
        "votes_cast": total,
        "active_tokens": active_tokens,
        "expected_checkins": expected,
        "difference": checkins - expected,
        "balanced": checkins == expected,
    }
    return payload


# --------------------------------------------------------------------------- #
# Cabines (kiosks): registro, heartbeat e alocacao
# --------------------------------------------------------------------------- #
def _iso_cutoff(seconds: float) -> str:
    """'Agora' (UTC) menos ``seconds`` em ISO-8601 - comparavel lexicograficamente."""
    return (
        datetime.now(timezone.utc) - timedelta(seconds=seconds)
    ).isoformat(timespec="seconds")


def _iso_future(seconds: float) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(seconds=seconds)
    ).isoformat(timespec="seconds")


def _booth_label(number: int) -> str:
    return f"Cabine {int(number):02d}"


def _row_to_booth(row: sqlite3.Row) -> dict:
    """Serializa uma linha de booth calculando online / ociosa / atribuida."""
    online = row["last_seen"] > _iso_cutoff(BOOTH_TIMEOUT_SECONDS)
    busy = bool(row["busy_until"] and row["busy_until"] > now_iso())
    return {
        "id": row["booth_id"],
        "number": row["number"],
        "label": row["label"],
        "status": row["status"],
        "online": online,
        "idle": online and not busy,
        "assigned": online and busy,
        "last_seen": row["last_seen"],
        "busy_until": row["busy_until"],
        "client_ip": row["client_ip"],
        "paired": bool(row["pairing_token"]),
    }


def get_booth(db: sqlite3.Connection, booth_id: str) -> Optional[dict]:
    row = db.execute(
        "SELECT * FROM booths WHERE booth_id = ?", (booth_id,)
    ).fetchone()
    return _row_to_booth(row) if row else None


def list_booths(db: sqlite3.Connection) -> list[dict]:
    rows = db.execute("SELECT * FROM booths ORDER BY number ASC").fetchall()
    return [_row_to_booth(r) for r in rows]


def register_booth(
    db: sqlite3.Connection,
    booth_id: str,
    client_ip: str = "",
    rotate: bool = False,
) -> Optional[dict]:
    """Registra (idempotente) uma cabine e atribui um numero amigavel estavel.

    Em cabines NOVAS (ou quando ``rotate=True``) gera e persiste um
    ``pairing_token`` devolvido em ``booth["pairing_token"]``: o navegador o
    guarda em localStorage como prova de pareamento. O ``client_ip`` observado
    e sempre atualizado quando informado.
    """
    booth_id = booth_id.strip()
    if not booth_id or len(booth_id) > 128:
        return None
    row = db.execute(
        "SELECT * FROM booths WHERE booth_id = ?", (booth_id,)
    ).fetchone()
    token: Optional[str] = None
    if row is None:
        number = db.execute(
            "SELECT COALESCE(MAX(number), 0) + 1 AS n FROM booths"
        ).fetchone()["n"]
        token = secrets.token_urlsafe(24)
        db.execute(
            "INSERT INTO booths(booth_id, number, label, status, last_seen, "
            "client_ip, pairing_token, registered_at) "
            "VALUES(?, ?, ?, 'idle', ?, ?, ?, ?)",
            (booth_id, number, _booth_label(number), now_iso(),
             client_ip or None, token, now_iso()),
        )
    elif rotate:
        token = secrets.token_urlsafe(24)
        db.execute(
            "UPDATE booths SET last_seen = ?, client_ip = COALESCE(?, client_ip), "
            "pairing_token = ? WHERE booth_id = ?",
            (now_iso(), client_ip or None, token, booth_id),
        )
    else:
        db.execute(
            "UPDATE booths SET last_seen = ?, client_ip = COALESCE(?, client_ip) "
            "WHERE booth_id = ?",
            (now_iso(), client_ip or None, booth_id),
        )
    db.commit()
    booth = get_booth(db, booth_id)
    if booth is not None and token is not None:
        # So devolve o token quando (re)emitido, e nunca em respostas publicas.
        booth["pairing_token"] = token
    return booth


def touch_booth(
    db: sqlite3.Connection,
    booth_id: str,
    status: Optional[str] = None,
    client_ip: str = "",
) -> Optional[dict]:
    """Atualiza o heartbeat da cabine.

    status='idle'   -> libera a reserva (volta a ficar disponivel).
    status='voting' -> mantem/renova a reserva (cabine em uso).
    O ``client_ip``, quando informado, e gravado como ultimo IP observado.
    """
    booth_id = booth_id.strip()
    if not booth_id:
        return None
    ip = client_ip or None
    if status == "idle":
        db.execute(
            "UPDATE booths SET last_seen = ?, client_ip = COALESCE(?, client_ip), "
            "status = 'idle', busy_until = NULL WHERE booth_id = ?",
            (now_iso(), ip, booth_id),
        )
    elif status == "voting":
        db.execute(
            "UPDATE booths SET last_seen = ?, client_ip = COALESCE(?, client_ip), "
            "status = 'voting', busy_until = ? WHERE booth_id = ?",
            (now_iso(), ip, _iso_future(BOOTH_BUSY_SECONDS), booth_id),
        )
    else:
        db.execute(
            "UPDATE booths SET last_seen = ?, client_ip = COALESCE(?, client_ip) "
            "WHERE booth_id = ?",
            (now_iso(), ip, booth_id),
        )
    db.commit()
    return get_booth(db, booth_id)


def allocate_booth(db: sqlite3.Connection) -> Optional[dict]:
    """Reserva a proxima cabine ONLINE e OCIOSA (de menor numero).

    NAO faz commit: roda dentro da transacao do check-in (mesmo commit do
    incremento da fila de tokens). Retorna {id, number, label} ou None.
    """
    cutoff = _iso_cutoff(BOOTH_TIMEOUT_SECONDS)
    now = now_iso()
    row = db.execute(
        "SELECT * FROM booths WHERE last_seen > ? "
        "AND (busy_until IS NULL OR busy_until <= ?) "
        "ORDER BY number ASC LIMIT 1",
        (cutoff, now),
    ).fetchone()
    if row is None:
        return None
    db.execute(
        "UPDATE booths SET status = 'voting', busy_until = ?, last_seen = ? "
        "WHERE booth_id = ?",
        (_iso_future(BOOTH_BUSY_SECONDS), now, row["booth_id"]),
    )
    return {"id": row["booth_id"], "number": row["number"], "label": row["label"]}


def _client_ip() -> str:
    """IP do cliente que originou a requisicao.

    Prioriza o primeiro endereco de ``X-Forwarded-For`` (quando o servidor esta
    atras de um proxy/roteador) e recai para ``request.remote_addr``.
    """
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return request.remote_addr or ""


# --------------------------------------------------------------------------- #
# Seguranca: token de dispositivo
# --------------------------------------------------------------------------- #
def _extract_token() -> str:
    """Token vindo do header X-Device-Token (fallback: query ?token=)."""
    return request.headers.get("X-Device-Token") or request.args.get("token", "")


def _token_valid(candidate: str) -> bool:
    if not candidate:
        return False
    try:
        return secrets.compare_digest(str(candidate), DEVICE_TOKEN)
    except (TypeError, ValueError):
        return False


def require_device_token(view):
    """Protege rotas de API administrativas via header X-Device-Token."""

    @wraps(view)
    def wrapper(*args, **kwargs):
        if not _token_valid(_extract_token()):
            logger.warning(
                "Falha de autenticacao admin (401) em %s %s - IP %s",
                request.method, request.path, _client_ip() or "desconhecido",
            )
            return (
                jsonify({"ok": False, "error": "unauthorized",
                         "detail": "Header X-Device-Token ausente ou invalido."}),
                401,
            )
        return view(*args, **kwargs)

    return wrapper


def _user_error(message: str, status: int = 400, **extra):
    payload = {"ok": False, "error": message}
    payload.update(extra)
    return jsonify(payload), status


# --------------------------------------------------------------------------- #
# Paginas (interface)
# --------------------------------------------------------------------------- #
@app.get("/")
def index():
    """Landing minima com links para cada terminal."""
    return render_template(
        "index.html",
        cooldown=CHECKIN_COOLDOWN_SECONDS,
        results_delay=RESULTS_DELAY_SECONDS,
    )


@app.get("/checkin")
def checkin_page():
    """Interface de check-in (mesa / servidor com leitor NFC HID)."""
    return render_template("checkin.html", cooldown=CHECKIN_COOLDOWN_SECONDS)


@app.get("/cabine")
def cabine_page():
    """Cabine de votacao (terminal touch / kiosk)."""
    return render_template("cabine.html")


@app.get("/painel-tv")
def painel_tv_page():
    """Painel publico de resultados (TV)."""
    db = get_db()
    return render_template(
        "painel_tv.html",
        results_delay=effective_results_delay_seconds(db),
        results_delay_minutes=get_results_delay_minutes(db),
        network_mode=NETWORK_MODE,
    )


@app.get("/admin")
def admin_page():
    """Shell do painel administrativo.

    A pagina nao expoe dados por si so: uma etapa de 'unlock' guarda o token
    em sessionStorage e todas as chamadas /api/admin/* enviam o header
    X-Device-Token. Tambem aceitamos ?token=... para conveniencia.
    """
    provided = request.args.get("token", "")
    accepted = _token_valid(provided)
    return render_template("admin.html", prefill_token=provided if accepted else "")


# --------------------------------------------------------------------------- #
# API: Check-in (servidor + leitor NFC HID)
# --------------------------------------------------------------------------- #
@app.post("/api/checkin")
def api_checkin():
    """Valida um cracha lido pelo leitor NFC.

    Fluxo:
      1. Existe plebiscito aberto? Senao -> 'no_poll'.
      2. O cracha ja votou?              Senao -> 'already_voted'.
      3. Registra o cracha e incrementa voting_tokens += 1.
      4. Reserva a proxima cabine online e ociosa (menor numero) e devolve
         seu rotulo em 'instruction' (ou o fallback generico, se nao houver).

    A prevencao real de voto duplo esta no banco (PK em voters); o cooldown
    do front-end e apenas uma protecao extra contra duplo-swap do leitor.
    """
    data = request.get_json(silent=True) or {}
    badge = str(data.get("badge_id", "")).strip()

    if not badge:
        return _user_error("badge_id_required", 400)
    if len(badge) > 128:
        return _user_error("badge_id_too_long", 400)

    with _DB_LOCK:
        db = get_db()
        poll = get_open_poll(db)
        if poll is None:
            return jsonify(
                {"ok": False, "status": "no_poll",
                 "detail": "Nenhum plebiscito aberto para votacao."}
            ), 409

        already = db.execute(
            "SELECT voted_at FROM voters WHERE badge_id = ?", (badge,)
        ).fetchone()
        if already is not None:
            return jsonify(
                {"ok": False, "status": "already_voted",
                 "detail": "Este cracha ja realizou o check-in.",
                 "voted_at": already["voted_at"],
                 "voting_tokens": get_voting_tokens(db)}
            ), 409

        db.execute(
            "INSERT INTO voters(badge_id, voted_at) VALUES(?, ?)",
            (badge, now_iso()),
        )
        tokens = set_voting_tokens(db, get_voting_tokens(db) + 1)
        # Reserva a proxima cabine online/ociosa na MESMA transacao do token.
        booth = allocate_booth(db)
        db.commit()

    if booth is not None:
        instruction = f"Liberado! Dirija-se à {booth['label']}."
    else:
        instruction = "Liberado! Dirija-se a qualquer cabine disponível."

    return jsonify(
        {"ok": True, "status": "authorized",
         "voting_tokens": tokens,
         "booth": booth,
         "instruction": instruction,
         "detail": "Cracha liberado. Dirija-se a uma cabine."}
    )


# --------------------------------------------------------------------------- #
# API: Cabine de votacao
# --------------------------------------------------------------------------- #
@app.get("/api/booth/status")
def api_booth_status():
    """Estado consumido pelas cabines (poll a cada ~1s).

    Sem ``?booth_id=`` retorna apenas a disponibilidade global (compativel com
    a versao anterior). Com ``?booth_id=`` informa tambem se ESTA cabine foi a
    alocada no check-in (``assigned``), alem de quantas cabines estao online.
    """
    db = get_db()
    poll = get_open_poll(db)
    tokens = get_voting_tokens(db)
    booths = list_booths(db)
    payload = {
        "ok": True,
        "voting_tokens": tokens,
        "available": tokens > 0 and poll is not None,
        "poll_open": poll is not None,
        "poll_id": poll["id"] if poll else None,
        "booths_online": sum(1 for b in booths if b["online"]),
        "queue_warning_threshold": get_queue_threshold(db),
        "results_delay": RESULTS_DELAY_SECONDS,
        "server_time": now_iso(),
    }
    booth_id = request.args.get("booth_id", "").strip()
    if booth_id:
        with _DB_LOCK:
            # Reavalia sob o lock: um token "solto" (check-in em que NENHUMA
            # cabine conseguiu ser reservada -> fallback global) deve ser
            # assumido pela primeira cabine ONLINE e OCIOSA que consultar o
            # status, em vez de deixa-la presa em "Aguardando a sua vez".
            poll = get_open_poll(db)
            tokens = get_voting_tokens(db)
            payload["voting_tokens"] = tokens
            payload["available"] = tokens > 0 and poll is not None
            payload["poll_open"] = poll is not None
            payload["poll_id"] = poll["id"] if poll else None

            booth = get_booth(db, booth_id)
            assigned = bool(booth and booth["assigned"])
            # Reserva dirigida a ESTA cabine OU token global sem dono: aloca.
            # Nunca travamos uma cabine online/ociosa enquanto houver token
            # sem reserva na fila.
            if (not assigned and booth is not None and booth["idle"]
                    and poll is not None and tokens > 0):
                reserved = sum(1 for b in list_booths(db) if b["assigned"])
                if tokens > reserved:   # sobra token sem reserva -> fallback global
                    booth = touch_booth(db, booth_id, "voting",
                                        client_ip=_client_ip())
                    assigned = bool(booth and booth["assigned"])
                    if assigned:
                        logger.info(
                            "Cabine %s assumiu/recebeu um token de votacao.",
                            booth_id,
                        )

            payload["booth"] = booth
            payload["assigned"] = assigned
            if assigned and poll is not None:
                # Detalhes do plebiscito na propria resposta: a cabine pode
                # renderizar direto, sem depender de um fetch extra a /api/poll.
                payload["active_poll"] = {
                    "id": poll["id"],
                    "question": poll["question"],
                    "options": [
                        {"id": o.get("id"), "label": o.get("label", "")}
                        for o in poll["options"]
                    ],
                }
    logger.debug(
        "Status da cabine %s: tokens=%s, assigned=%s, online=%s",
        booth_id or "-", payload.get("voting_tokens"),
        payload.get("assigned"), payload.get("booths_online"),
    )
    return jsonify(payload)


@app.post("/api/booth/register")
def api_booth_register():
    """Registra uma cabine/kiosk e devolve seu numero amigavel ("Cabine 01").

    Body: {"booth_id": "...",
           "pairing_token": "..."?,   # prova de pareamento (localStorage)
           "pairing_pin": "..."?}     # PIN para parear uma cabine nova

    Regras de pareamento:
      * Sem PIN configurado -> registro aberto (modo legado).
      * Com PIN configurado -> uma cabine NOVA exige ``pairing_pin`` correto;
        uma cabine ja conhecida e aceita apresentando seu ``pairing_token``
        (ou o PIN atual, que rotaciona o token).
    """
    data = request.get_json(silent=True) or {}
    booth_id = str(data.get("booth_id", "")).strip()
    if not booth_id:
        return _user_error("booth_id_required", 400)
    provided_pin = str(data.get("pairing_pin", "")).strip()
    provided_token = str(data.get("pairing_token", "")).strip()
    client_ip = _client_ip()

    with _DB_LOCK:
        db = get_db()
        required_pin = get_pairing_pin(db)
        existing = db.execute(
            "SELECT * FROM booths WHERE booth_id = ?", (booth_id,)
        ).fetchone()

        rotate = False
        paired_with_pin = False
        if existing is None:
            # Cabine nova: exige o PIN quando o pareamento esta ativo.
            if required_pin and not _pin_matches(provided_pin, required_pin):
                write_audit_log(
                    db, "booth_pair_denied",
                    f"Pareamento negado (PIN ausente/invalido) - cabine {booth_id}",
                    actor_ip=client_ip,
                )
                return _user_error(
                    "pairing_required", 403,
                    detail="Informe o PIN de pareamento exibido no painel administrativo.",
                )
            paired_with_pin = bool(required_pin)
        else:
            stored = existing["pairing_token"]
            if stored and provided_token and _token_equal(provided_token, stored):
                pass  # token valido -> segue
            elif required_pin and _pin_matches(provided_pin, required_pin):
                rotate = True  # re-pareamento autorizado pelo PIN -> novo token
                paired_with_pin = True
            elif not stored:
                pass  # registro legado (sem token) -> segue
            else:
                write_audit_log(
                    db, "booth_pair_denied",
                    f"Pareamento negado (cabine {booth_id} ja pareada)",
                    actor_ip=client_ip,
                )
                return _user_error(
                    "pairing_required", 403,
                    detail="Cabine ja pareada. Remova-a no painel para parear novamente.",
                )
        booth = register_booth(db, booth_id, client_ip, rotate=rotate)
        if booth is not None and paired_with_pin:
            write_audit_log(
                db, "booth_pair",
                f"Cabine {booth.get('label')} ({booth_id}) pareada"
                + (" (token rotacionado)" if rotate else " (nova)"),
                actor_ip=client_ip,
            )

    if booth is None:
        return _user_error("invalid_booth_id", 400)
    logger.info(
        "Cabine registrada: %s (%s) via IP %s%s",
        booth.get("label"), booth_id, client_ip or "desconhecido",
        " - novo token de pareamento emitido"
        if booth.get("pairing_token") else "",
    )
    return jsonify({"ok": True, "booth": booth})


@app.post("/api/booth/heartbeat")
def api_booth_heartbeat():
    """Heartbeat da cabine: mantem 'online' e (opcional) libera/renova a reserva.

    Body: {"booth_id": "...", "status": "idle"|"voting"}  (status opcional)
    Registra tambem o IP de origem (ultimo observado).
    """
    data = request.get_json(silent=True) or {}
    booth_id = str(data.get("booth_id", "")).strip()
    status = str(data.get("status", "")).strip() or None
    if not booth_id:
        return _user_error("booth_id_required", 400)
    with _DB_LOCK:
        db = get_db()
        booth = touch_booth(db, booth_id, status, client_ip=_client_ip())
    if booth is None:
        return jsonify({"ok": False, "status": "unknown_booth",
                        "detail": "Cabine nao registrada."}), 404
    logger.info(
        "Heartbeat da cabine %s (status=%s, IP=%s)",
        booth_id, status or "n/a", _client_ip() or "desconhecido",
    )
    return jsonify({"ok": True, "booth": booth})


@app.get("/api/booths")
def api_booths():
    """Lista publica das cabines (rotulos e estado online/ocioso/alocado)."""
    db = get_db()
    booths = list_booths(db)
    return jsonify({
        "ok": True,
        "count": len(booths),
        "online": sum(1 for b in booths if b["online"]),
        "idle": sum(1 for b in booths if b["idle"]),
        "booths": booths,
    })


@app.get("/api/poll")
def api_poll():
    """Pergunta e opcoes do plebiscito ativo (sem contagens).

    As cabines buscam isto apenas quando ha token disponivel.
    """
    db = get_db()
    poll = get_open_poll(db)
    if poll is None:
        return jsonify({"ok": False, "status": "no_poll"}), 404

    return jsonify(
        {
            "ok": True,
            "poll": {
                "id": poll["id"],
                "question": poll["question"],
                "options": [{"id": o.get("id"), "label": o.get("label", "")}
                            for o in poll["options"]],
            },
            "voting_tokens": get_voting_tokens(db),
        }
    )


@app.post("/api/vote")
def api_vote():
    """Registra um voto anonimo consumindo UM token disponivel.

    A verificacao-e-decremento do token e atomica (sob _DB_LOCK); se duas
    cabines disputarem o ultimo token, apenas uma recebe sucesso.
    """
    data = request.get_json(silent=True) or {}
    option_id = str(data.get("option_id", "")).strip()
    if not option_id:
        return _user_error("option_id_required", 400)

    with _DB_LOCK:
        db = get_db()
        poll = get_open_poll(db)
        if poll is None:
            return jsonify({"ok": False, "status": "no_poll"}), 409

        valid = {
            str(o.get("id")): str(o.get("label", "")) for o in poll["options"]
        }
        if option_id not in valid:
            return _user_error("invalid_option", 400)

        tokens = get_voting_tokens(db)
        if tokens <= 0:
            # Outra cabine consumiu o token primeiro.
            return jsonify(
                {"ok": False, "status": "no_token",
                 "detail": "Nenhum token de votacao disponivel."}
            ), 409

        # Grava APENAS (poll_id, option_label): sem cracha, sem timestamp e
        # sem qualquer referencia ao votante -> voto absolutamente anonimo.
        db.execute(
            "INSERT INTO votes(poll_id, option_label) VALUES(?, ?)",
            (poll["id"], valid[option_id]),
        )
        remaining = set_voting_tokens(db, tokens - 1)
        db.commit()

    return jsonify(
        {"ok": True, "status": "recorded",
         "voting_tokens": remaining,
         "detail": "Voto registrado com sucesso."}
    )


# --------------------------------------------------------------------------- #
# API: Resultados (Painel TV)
# --------------------------------------------------------------------------- #
@app.get("/api/results")
def api_results():
    """Resultados publicos com ATRASO CONTROLADO.

    Somente os votos mais antigos que a janela de carencia aparecem. A janela
    e estimada pelos check-ins cujo ``voted_at`` ja passou do cutoff (tabela
    ``voters``) - nunca por um vinculo votante->voto (que nao existe). Isso
    evita correlacionar em tempo real quem acabou de votar, preservando o
    sigilo do voto e dando transparencia ao processo.
    """
    db = get_db()
    poll_id = get_active_poll_id(db)
    poll = get_poll(db, poll_id) if poll_id is not None else None
    delay_seconds = effective_results_delay_seconds(db)
    delay_minutes = get_results_delay_minutes(db)

    if poll is None:
        return jsonify(
            {"ok": True, "status": "no_poll", "question": None, "options": [],
             "total": 0, "voting_tokens": get_voting_tokens(db),
             "results_delay": delay_seconds, "results_delay_minutes": delay_minutes,
             "network_isolation": NETWORK_MODE, "generated_at": now_iso()}
        )

    cutoff = None
    if delay_seconds > 0 and poll["status"] != POLL_STATUS_CLOSED:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=delay_seconds)
        ).isoformat(timespec="seconds")

    # Contagem PUBLICA (respeita a carencia) x contagem TOTAL (sigilosa).
    counts = tally_visible_votes(db, poll, cutoff)
    options = votes_per_option(poll, counts)
    total = sum(o["count"] for o in options)

    all_counts = tally_votes(db, poll["id"])
    total_actual = sum(all_counts.values())

    return jsonify(
        {
            "ok": True,
            "status": poll["status"],
            "poll_id": poll["id"],
            "question": poll["question"],
            "options": options,
            "total": total,
            "total_pending_display": max(0, total_actual - total),
            "voting_tokens": get_voting_tokens(db),
            "results_delay": delay_seconds,
            "results_delay_minutes": delay_minutes,
            "network_isolation": NETWORK_MODE,
            "generated_at": now_iso(),
        }
    )


@app.get("/api/health")
def api_health():
    """Checagem simples de disponibilidade (usada por monitoramento local)."""
    db = get_db()
    poll = get_open_poll(db)
    return jsonify(
        {
            "ok": True,
            "poll_open": poll is not None,
            "voting_tokens": get_voting_tokens(db),
            "queue_warning_threshold": get_queue_threshold(db),
            "cooldown": CHECKIN_COOLDOWN_SECONDS,
            "results_delay": effective_results_delay_seconds(db),
            "results_delay_minutes": get_results_delay_minutes(db),
            "network_isolation": NETWORK_MODE,
        }
    )


# --------------------------------------------------------------------------- #
# API: Administracao (protegida por X-Device-Token)
# --------------------------------------------------------------------------- #
def _normalize_options(raw: Any) -> list[dict]:
    """Converte entrada do admin em [{"id","label"}] validado.

    Aceita lista de strings ou lista de dicts {"label": ...} (com id opcional).
    Gera ids estaveis (opt_1, opt_2, ...) quando ausentes.
    """
    if not isinstance(raw, list):
        raise ValueError("options_must_be_list")

    options: list[dict] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw, start=1):
        if isinstance(item, dict):
            label = str(item.get("label", "")).strip()
            opt_id = str(item.get("id", "")).strip() or f"opt_{index}"
        else:
            label = str(item).strip()
            opt_id = f"opt_{index}"

        if not label:
            continue  # ignora opcoes vazias
        if opt_id in seen_ids:
            opt_id = f"opt_{index}"
        seen_ids.add(opt_id)
        options.append({"id": opt_id, "label": label})

    if len(options) < 2:
        raise ValueError("need_at_least_two_options")
    return options


def _admin_state(db: sqlite3.Connection) -> dict:
    """Snapshot completo para o painel administrativo."""
    poll_id = get_active_poll_id(db)
    poll = get_poll(db, poll_id) if poll_id is not None else None

    voters = db.execute("SELECT COUNT(*) AS c FROM voters").fetchone()["c"]
    tokens = get_voting_tokens(db)
    poll_payload = None
    closure_pin = None
    if poll is not None:
        counts = tally_votes(db, poll["id"])
        options = votes_per_option(poll, counts)
        pin_row = db.execute(
            "SELECT closure_pin FROM polls WHERE id = ?", (poll["id"],)
        ).fetchone()
        closure_pin = pin_row["closure_pin"] if pin_row else None
        poll_payload = {
            "id": poll["id"],
            "question": poll["question"],
            "status": poll["status"],
            "created_at": poll["created_at"],
            "closed_at": poll["closed_at"],
            "options": options,
            "total_votes": sum(o["count"] for o in options),
            "closure_pin": closure_pin,
        }

    pairing_pin = get_pairing_pin(db)
    if poll is not None:
        # Conciliacao previa (mesma regra da Ata): check-ins x votos + tokens.
        votes_total = poll_payload["total_votes"] if poll_payload else 0
        checkins = total_voters_for_poll(db, poll)
        reconciliation = {
            "checkins_total": checkins,
            "votes_cast": votes_total,
            "active_tokens": tokens,
            "expected_checkins": votes_total + tokens,
            "difference": checkins - (votes_total + tokens),
            "balanced": checkins == (votes_total + tokens),
        }
    else:
        reconciliation = None

    return {
        "ok": True,
        "poll": poll_payload,
        "voting_tokens": tokens,
        "voters_checked_in": voters,
        "booths": list_booths(db),
        "pairing_pin": pairing_pin,
        "pairing_enabled": bool(pairing_pin),
        "queue_warning_threshold": get_queue_threshold(db),
        "cooldown": CHECKIN_COOLDOWN_SECONDS,
        "results_delay": effective_results_delay_seconds(db),
        "results_delay_minutes": get_results_delay_minutes(db),
        "results_delay_seconds": RESULTS_DELAY_SECONDS,
        "network_isolation": NETWORK_MODE,
        "closure_pin": closure_pin,
        "reconciliation": reconciliation,
        "server_time": now_iso(),
    }


def _preserved_voters(db: sqlite3.Connection, poll_id: int) -> Optional[int]:
    """Votantes unicos preservados de um plebiscito (ou None se desconhecido).

    Ao encerrar/trocar de plebiscito a tabela `voters` e limpa; o total e
    guardado em settings (chave `closed_poll_voters:<id>`) para a Ata e para
    o historico. Retorna None quando nao ha valor persistido.
    """
    stored = get_setting(db, _closed_voters_key(poll_id), "").strip()
    if not stored:
        return None
    try:
        return int(stored)
    except ValueError:
        return None


def history_payload(db: sqlite3.Connection) -> dict:
    """Historico dos plebiscitos registrados, do mais recente ao mais antigo.

    Cada item traz a pergunta, o status, os timestamps e as estatisticas
    agregadas (votos por opcao com percentual, total de votos e, quando
    conhecido, o numero de votantes unicos). Somente leitura.
    """
    active_id = get_active_poll_id(db)
    rows = db.execute("SELECT * FROM polls ORDER BY id DESC").fetchall()

    elections: list[dict] = []
    for row in rows:
        try:
            raw_options = json.loads(row["options"])
        except (TypeError, ValueError):
            raw_options = []
        counts = tally_votes(db, row["id"])
        options = votes_per_option({"options": raw_options}, counts)
        total = sum(opt["count"] for opt in options)
        for opt in options:
            opt["percent"] = round(
                (opt["count"] / total * 100) if total else 0.0, 1
            )
        elections.append(
            {
                "id": row["id"],
                "question": row["question"],
                "status": row["status"],
                "created_at": row["created_at"],
                "closed_at": row["closed_at"],
                "active": row["id"] == active_id,
                "options": options,
                "total_votes": total,
                "voters": _preserved_voters(db, row["id"]),
                "integrity": poll_integrity_report(db, {"id": row["id"]}),
            }
        )

    return {"ok": True, "count": len(elections), "elections": elections}


def history_report_payload(db: sqlite3.Connection) -> dict:
    """Dados agregados de TODOS os plebiscitos para o Relatorio Geral.

    Reaproveita ``history_payload`` (ja com votos/percentuais/votantes por
    plebiscito) e soma os totais globais exibidos no documento A4: numero de
    plebiscitos, quantos encerrados/em andamento, votos apurados e votantes
    registrados (quando conhecidos).
    """
    history = history_payload(db)
    elections = history["elections"]
    stats = {
        "polls": len(elections),
        "closed": sum(1 for e in elections if e["status"] == POLL_STATUS_CLOSED),
        "open": sum(1 for e in elections if e["status"] == POLL_STATUS_OPEN),
        "total_votes": sum(e["total_votes"] for e in elections),
        "total_voters": sum((e["voters"] or 0) for e in elections),
        # Integridade: quantos selados conferem / divergem / nao selados.
        "integrity_valid": sum(
            1 for e in elections
            if e.get("integrity") and e["integrity"].get("valid") is True
        ),
        "integrity_invalid": sum(
            1 for e in elections
            if e.get("integrity") and e["integrity"].get("valid") is False
        ),
        "integrity_unsealed": sum(
            1 for e in elections
            if not e.get("integrity") or e["integrity"].get("sealed") is False
        ),
    }
    stats["checkins_vs_votes"] = stats["total_voters"] - stats["total_votes"]
    return {
        "ok": True,
        "factory_name": FACTORY_NAME,
        "title": "Relatório Geral de Histórico",
        "generated_at": now_iso(),
        "stats": stats,
        "rules": _business_rules(db),
        "elections": elections,
    }


def booth_audit_payload(db: sqlite3.Connection) -> dict:
    """Dados das cabines para o Relatorio de Auditoria (A4).

    Lista todas as cabines registradas com IP do cliente, status, ultimo
    heartbeat e situacao de pareamento, alem dos contadores de auditoria e do
    estado atual do PIN de pareamento.
    """
    booths = list_booths(db)
    pairing_pin = get_pairing_pin(db)
    stats = {
        "total": len(booths),
        "online": sum(1 for b in booths if b["online"]),
        "in_use": sum(1 for b in booths if b["assigned"]),
        "idle": sum(1 for b in booths if b["idle"]),
        "paired": sum(1 for b in booths if b["paired"]),
    }
    return {
        "ok": True,
        "factory_name": FACTORY_NAME,
        "title": "Relatório de Auditoria de Cabines",
        "generated_at": now_iso(),
        "server_time": now_iso(),
        "booths": booths,
        "stats": stats,
        "pairing_enabled": bool(pairing_pin),
        "network_mode": NETWORK_MODE,
    }


@app.get("/api/admin/state")
@require_device_token
def api_admin_state():
    """Estado atual do plebiscito, contadores e configuracao."""
    db = get_db()
    return jsonify(_admin_state(db))


@app.post("/api/admin/poll")
@app.post("/api/admin/start")
@require_device_token
def api_admin_start_poll():
    """Cria e inicia um novo plebiscito dinamico.

    Body JSON:
        {"question": "Texto da pergunta",
         "options": ["Sim", "Nao"]  }   # ou [{"label": "..."}, ...]

    Iniciar um novo plebiscito:
      * fecha a poll anterior (se aberta);
      * limpa a lista de crachas (todos podem votar de novo);
      * zera a fila de tokens de votacao;
      * sorteia um PIN de encerramento (4-6 digitos) devolvido apenas ao
        operador - exigido depois por ``POST /api/admin/close``.
    """
    data = request.get_json(silent=True) or {}
    question = str(data.get("question", "")).strip()
    if not question:
        return _user_error("question_required", 400)
    if len(question) > 500:
        return _user_error("question_too_long", 400)

    try:
        options = _normalize_options(data.get("options"))
    except ValueError as exc:
        return _user_error(str(exc), 400)

    with _DB_LOCK:
        db = get_db()
        previous_id = get_active_poll_id(db)
        if previous_id is not None:
            # Preserva a contagem de votantes do plebiscito anterior (Ata).
            preserve_closed_voters(db, previous_id)
            cur = db.execute(
                "UPDATE polls SET status = ?, closed_at = ? "
                "WHERE id = ? AND status = ?",
                (POLL_STATUS_CLOSED, now_iso(), previous_id, POLL_STATUS_OPEN),
            )
            # Sela a integridade (SHA-256) apenas na transicao open -> closed.
            if cur.rowcount:
                seal_poll_integrity(db, previous_id)

        cur = db.execute(
            "INSERT INTO polls(question, options, status, created_at, closure_pin) "
            "VALUES(?, ?, ?, ?, ?)",
            (question, json.dumps(options, ensure_ascii=False),
             POLL_STATUS_OPEN, now_iso(), generate_closure_pin()),
        )
        new_id = cur.lastrowid

        clear_voters(db)
        set_setting(db, "active_poll_id", new_id)
        set_voting_tokens(db, 0)
        write_audit_log(
            db, "poll_start",
            f"Plebiscito #{new_id} iniciado: {question[:120]}",
            poll_id=new_id, actor_ip=_client_ip(),
        )
        db.commit()
        state = _admin_state(db)
        logger.info(
            "Plebiscito #%s criado (encerra #%s): %r",
            new_id, previous_id if previous_id is not None else "-", question,
        )

    state["detail"] = "Plebiscito iniciado."
    return jsonify(state), 201



@app.post("/api/admin/reset")
@require_device_token
def api_admin_reset():
    """Zera o plebiscito ativo (mantem a pergunta/opcoes).

    Body opcional:
        {"clear_voters": true}   # limpa crachas para permitir novo voto
    """
    data = request.get_json(silent=True) or {}
    clear = bool(data.get("clear_voters", True))

    with _DB_LOCK:
        db = get_db()
        poll_id = get_active_poll_id(db)
        if poll_id is None:
            return _user_error("no_active_poll", 409)

        db.execute("DELETE FROM votes WHERE poll_id = ?", (poll_id,))
        # Estado alterado: o selo SHA-256 anterior deixa de valer.
        db.execute(
            "UPDATE polls SET integrity_hash = NULL, integrity_algorithm = NULL "
            "WHERE id = ?",
            (poll_id,),
        )
        if clear:
            clear_voters(db)
        set_voting_tokens(db, 0)
        db.commit()
        state = _admin_state(db)
        logger.info(
            "Contadores do plebiscito #%s zerados (clear_voters=%s).",
            poll_id, clear,
        )

    state["detail"] = "Contadores zerados."
    return jsonify(state)


@app.post("/api/admin/close")
@require_device_token
def api_admin_close():
    """Encerra o plebiscito ativo (resultados permanecem consultaveis).

    Exige o PIN de encerramento sorteado no inicio do plebiscito. O PIN pode
    ser informado no body JSON (``{"closure_pin": "123456"}``), no header
    ``X-Closure-Pin`` ou na query string (``?closure_pin=123456``). Ausente ou
    incorreto responde 403 (``invalid_closure_pin``) e a tentativa negada fica
    registrada no log de auditoria. Bancos antigos (PIN NULL) encerram sem PIN.
    """
    data = request.get_json(silent=True) or {}
    provided = (
        str(data.get("closure_pin", "")).strip()
        or str(request.headers.get("X-Closure-Pin", "")).strip()
        or str(request.args.get("closure_pin", "")).strip()
    )

    with _DB_LOCK:
        db = get_db()
        poll_id = get_active_poll_id(db)
        if poll_id is None:
            return _user_error("no_active_poll", 409)

        pin_row = db.execute(
            "SELECT closure_pin FROM polls WHERE id = ?", (poll_id,)
        ).fetchone()
        required = (pin_row["closure_pin"] if pin_row else None) or ""
        if required and not _pin_matches(provided, required):
            write_audit_log(
                db, "poll_close_denied",
                f"PIN de encerramento invalido no plebiscito #{poll_id}",
                poll_id=poll_id, actor_ip=_client_ip(),
            )
            logger.warning(
                "Encerramento do plebiscito #%s negado (PIN invalido) - IP %s",
                poll_id, _client_ip() or "desconhecido",
            )
            return _user_error("invalid_closure_pin", 403)

        # Preserva a contagem de votantes ANTES de limpar os crachas (Ata).
        preserve_closed_voters(db, poll_id)
        db.execute(
            "UPDATE polls SET status = ?, closed_at = ? WHERE id = ?",
            (POLL_STATUS_CLOSED, now_iso(), poll_id),
        )
        # Sela a integridade DEPOIS do fechamento e com todos os votos ja
        # persistidos: grava o digest SHA-256 do estado final para auditoria.
        seal_poll_integrity(db, poll_id)
        clear_voters(db)
        set_voting_tokens(db, 0)
        write_audit_log(
            db, "poll_close", f"Plebiscito #{poll_id} encerrado",
            poll_id=poll_id, actor_ip=_client_ip(),
        )
        db.commit()
        state = _admin_state(db)
        logger.info("Plebiscito #%s encerrado.", poll_id)

    state["detail"] = "Plebiscito encerrado."
    return jsonify(state)


@app.post("/api/admin/tokens")
@require_device_token
def api_admin_tokens():
    """Ajuste manual da fila de tokens (uso administrativo/emergencial).

    Body: {"count": 3}  -> define o total
          {"delta": 1}  -> soma/subtrai
    """
    data = request.get_json(silent=True) or {}
    with _DB_LOCK:
        db = get_db()
        if "count" in data:
            try:
                tokens = set_voting_tokens(db, int(data["count"]))
            except (TypeError, ValueError):
                return _user_error("invalid_count", 400)
        elif "delta" in data:
            try:
                delta = int(data["delta"])
            except (TypeError, ValueError):
                return _user_error("invalid_delta", 400)
            tokens = set_voting_tokens(db, get_voting_tokens(db) + delta)
        else:
            return _user_error("count_or_delta_required", 400)

        db.commit()
        state = _admin_state(db)
        logger.info("Fila de tokens de votacao ajustada para %s.", tokens)
    state["detail"] = f"Fila de tokens ajustada para {tokens}."
    return jsonify(state)


@app.post("/api/admin/pairing")
@require_device_token
def api_admin_pairing():
    """Gera, define ou desativa o PIN de pareamento de novas cabines.

    Body (opcional):
        {"generate": true}    -> gera um novo PIN numerico de 6 digitos;
        {"pin": "A1B2C3"}     -> define um PIN alfanumerico (4..16 chars);
        {"clear": true}       -> desativa o pareamento (PIN vazio);
        {}                    -> equivale a {"generate": true}.
    """
    data = request.get_json(silent=True) or {}
    with _DB_LOCK:
        db = get_db()
        if data.get("clear"):
            pin = set_pairing_pin(db, "")
            detail = "Pareamento de cabines desativado."
        elif data.get("generate") or "pin" not in data:
            pin = set_pairing_pin(db, generate_pairing_pin())
            detail = f"PIN de pareamento: {pin}"
        else:
            try:
                pin = set_pairing_pin(db, data.get("pin"))
            except ValueError as exc:
                return _user_error(str(exc), 400)
            detail = "PIN de pareamento atualizado." if pin else \
                "Pareamento de cabines desativado."
        state = _admin_state(db)
        write_audit_log(db, "pairing_change", detail, actor_ip=_client_ip())
        db.commit()
        logger.info("Pareamento de cabines: %s", detail)
    state["detail"] = detail
    return jsonify(state)


@app.delete("/api/admin/booths/<path:booth_id>")
@require_device_token
def api_admin_remove_booth(booth_id: str):
    """Remove/desconecta uma cabine registrada (invalidando seu pareamento)."""
    with _DB_LOCK:
        db = get_db()
        cur = db.execute("DELETE FROM booths WHERE booth_id = ?", (booth_id,))
        if cur.rowcount == 0:
            db.commit()
            return _user_error("unknown_booth", 404)
        write_audit_log(db, "booth_remove", f"Cabine removida: {booth_id}",
                        actor_ip=_client_ip())
        db.commit()
        state = _admin_state(db)
        logger.info("Cabine removida (deregistro): %s", booth_id)
    state["detail"] = "Cabine removida. O dispositivo precisara parear novamente."
    return jsonify(state)


# --------------------------------------------------------------------------- #
# API: Historico e gerenciamento do banco (protegido por X-Device-Token)
# --------------------------------------------------------------------------- #
PURGE_SCOPES = ("all", "logs")


@app.get("/api/admin/history")
@require_device_token
def api_admin_history():
    """Historico dos plebiscitos (somente leitura).

    Devolve todos os plebiscitos registrados, do mais recente ao mais antigo,
    com as estatisticas agregadas de cada um (votos por opcao, total de votos
    e votantes unicos quando conhecidos). Usado pela secao "Banco de Dados &
    Historico" do painel administrativo.
    """
    db = get_db()
    return jsonify(history_payload(db))


@app.route("/api/admin/db/purge", methods=["DELETE", "POST"])
@app.route("/api/admin/purge", methods=["POST", "DELETE"])
@require_device_token
def api_admin_db_purge():
    """Remove dados de TESTE do banco (acao destrutiva e IRREVERSIVEL).

    Exige confirmacao explicita, via query ``?confirm=1`` ou body JSON
    ``{"confirm": true}``, E uma verificacao SECUNDARIA (``secondary`` no body
    ou ``?secondary=``) que deve conferir com o Device Token ativo ou com um
    PIN dedicado (setting ``purge_pin``). Falha de verificacao -> 403.

    O escopo vem de ``?scope=`` ou do body (``{"scope": "all"|"logs"}``):

      * ``all``  (default): apaga TODOS os plebiscitos, votos e check-ins e
        reinicia o ponteiro do plebiscito ativo e a fila de tokens;
      * ``logs``: apaga apenas votos e check-ins (crachas), preservando os
        plebiscitos cadastrados.

    Retorna o estado do painel e o historico resultantes, prontos para
    re-renderizacao no cliente.
    """
    data = request.get_json(silent=True) or {}
    db = get_db()

    raw_confirm = data.get("confirm")
    if raw_confirm is None:
        raw_confirm = request.args.get("confirm", "")
    confirmed = str(raw_confirm).strip().lower() in ("1", "true", "yes", "sim", "purge")
    if not confirmed:
        return _user_error(
            "confirmation_required", 400,
            detail="Envie confirmacao explicita (confirm=1) para purgar o banco.",
        )

    scope = str(data.get("scope") or request.args.get("scope") or "all").strip().lower()
    if scope not in PURGE_SCOPES:
        return _user_error("invalid_scope", 400)

    # Verificacao SECUNDARIA obrigatoria (Device Token ou PIN dedicado).
    secondary = str(
        data.get("secondary") or data.get("secondary_pin") or data.get("pin")
        or request.args.get("secondary", "")
    ).strip()
    if not secondary_verified(db, secondary):
        write_audit_log(
            db, "purge_denied",
            f"Purga recusada (verificacao secundaria invalida) - scope={scope}",
            actor_ip=_client_ip(),
        )
        logger.warning(
            "Purga recusada (403): verificacao secundaria invalida - scope=%s IP %s",
            scope, _client_ip() or "desconhecido",
        )
        return _user_error(
            "secondary_verification_required", 403,
            detail="Confirmacao secundaria invalida: informe o Device Token "
                   "ou o PIN de purga.",
        )

    with _DB_LOCK:
        removed_votes = db.execute("SELECT COUNT(*) AS c FROM votes").fetchone()["c"]
        removed_voters = db.execute("SELECT COUNT(*) AS c FROM voters").fetchone()["c"]
        removed_polls = db.execute("SELECT COUNT(*) AS c FROM polls").fetchone()["c"]

        db.execute("DELETE FROM votes")
        db.execute("DELETE FROM voters")
        if scope == "all":
            db.execute("DELETE FROM polls")
            db.execute(
                "DELETE FROM settings WHERE key LIKE 'closed_poll_voters:%'"
            )
            set_setting(db, "active_poll_id", "")
            set_voting_tokens(db, 0)
            # Reinicia os contadores AUTOINCREMENT para um banco realmente limpo.
            try:
                db.execute(
                    "DELETE FROM sqlite_sequence WHERE name IN ('polls', 'votes')"
                )
            except sqlite3.Error:
                pass  # tabela sqlite_sequence ausente em bancos sem AUTOINCREMENT
        else:
            # scope == 'logs': os plebiscitos permanecem, mas os votos foram
            # apagados -> o selo SHA-256 anterior deixa de valer para todos.
            db.execute(
                "UPDATE polls SET integrity_hash = NULL, integrity_algorithm = NULL"
            )
        write_audit_log(
            db, "purge",
            f"Purga scope={scope}: {removed_votes} voto(s), {removed_voters} check-in(s), "
            f"{removed_polls if scope == 'all' else 0} plebiscito(s)",
            actor_ip=_client_ip(),
        )
        db.commit()

        state = _admin_state(db)
        history = history_payload(db)

    logger.info(
        "Banco de dados purgado (scope=%s): %s voto(s), %s check-in(s), "
        "%s plebiscito(s) removido(s).",
        scope, removed_votes, removed_voters,
        removed_polls if scope == "all" else 0,
    )
    return jsonify({
        "ok": True,
        "scope": scope,
        "removed": {
            "votes": removed_votes,
            "voters": removed_voters,
            "polls": removed_polls if scope == "all" else 0,
        },
        "state": state,
        "history": history,
        "detail": "Banco de dados purgado (dados de teste removidos).",
    })


@app.delete("/api/admin/polls/<int:poll_id>")
@require_device_token
def api_admin_delete_poll(poll_id: int):
    """Exclui um plebiscito individual e todos os votos associados.

    Acao destrutiva e IRREVERSIVEL. Remove a linha do plebiscito em ``polls``,
    os votos correlatos em ``votes`` e o snapshot de votantes preservado em
    ``settings``. Quando o alvo e o plebiscito ATIVO, o estado ativo e
    reiniciado (ponteiro ``active_poll_id``, fila de tokens e crachas), para
    que o painel nao aponte para um plebiscito inexistente.

    Retorna o estado do painel e o historico resultantes, prontos para
    re-renderizacao no cliente.
    """
    with _DB_LOCK:
        db = get_db()
        row = db.execute("SELECT id FROM polls WHERE id = ?", (poll_id,)).fetchone()
        if row is None:
            return _user_error("unknown_poll", 404)

        was_active = get_active_poll_id(db) == poll_id
        removed_votes = db.execute(
            "SELECT COUNT(*) AS c FROM votes WHERE poll_id = ?", (poll_id,)
        ).fetchone()["c"]

        db.execute("DELETE FROM votes WHERE poll_id = ?", (poll_id,))
        db.execute("DELETE FROM polls WHERE id = ?", (poll_id,))
        db.execute(
            "DELETE FROM settings WHERE key = ?", (_closed_voters_key(poll_id),)
        )

        if was_active:
            # Reseta o estado ativo: nada mais pode apontar para o excluido.
            set_setting(db, "active_poll_id", "")
            set_voting_tokens(db, 0)
            clear_voters(db)
        write_audit_log(
            db, "poll_delete",
            f"Plebiscito #{poll_id} excluido ({removed_votes} voto(s))"
            + (" - era o ativo" if was_active else ""),
            poll_id=poll_id, actor_ip=_client_ip(),
        )
        db.commit()

        state = _admin_state(db)
        history = history_payload(db)

    logger.info(
        "Plebiscito %s excluido%s (%s voto(s) removido(s)).",
        poll_id, " - era o ativo" if was_active else "", removed_votes,
    )
    return jsonify({
        "ok": True,
        "poll_id": poll_id,
        "was_active": was_active,
        "removed": {"votes": removed_votes, "polls": 1},
        "state": state,
        "history": history,
        "detail": "Plebiscito excluido com sucesso.",
    })


# --------------------------------------------------------------------------- #
# API: Configuracoes (limite de fila do check-in)
# --------------------------------------------------------------------------- #
@app.route("/api/admin/settings/queue-threshold", methods=["GET", "POST"])
@require_device_token
def api_admin_queue_threshold():
    """Le/define o limite de fila que dispara o Alerta Amarelo no check-in.

    GET  -> ``{"ok": true, "queue_warning_threshold": N}``
    POST -> body ``{"threshold": N}`` (aliases: ``value``/``queue_warning_threshold``)
            Grava na tabela ``settings`` e devolve o novo valor.
    """
    db = get_db()
    if request.method == "GET":
        return jsonify({
            "ok": True,
            "queue_warning_threshold": get_queue_threshold(db),
        })

    data = request.get_json(silent=True) or {}
    raw = data.get("threshold")
    if raw is None:
        raw = data.get("value")
    if raw is None:
        raw = data.get("queue_warning_threshold")
    if raw is None:
        return _user_error("threshold_required", 400,
                           detail="Informe o campo 'threshold' (numero >= 1).")

    with _DB_LOCK:
        try:
            value = set_queue_threshold(db, raw)
        except ValueError as exc:
            return _user_error(str(exc), 400,
                               detail="O limite deve ser um numero entre 1 e 9999.")
        db.commit()
        state = _admin_state(db)

    logger.info("Limite de fila do check-in ajustado para %s (IP %s).",
                value, _client_ip() or "desconhecido")
    return jsonify({
        "ok": True,
        "queue_warning_threshold": value,
        "state": state,
        "detail": f"Limite de fila definido em {value}.",
    })


# --------------------------------------------------------------------------- #
# API: Inspetor de banco de dados (SOMENTE LEITURA) + log de auditoria
# --------------------------------------------------------------------------- #
@app.get("/api/admin/db/inspect")
@require_device_token
def api_admin_db_inspect():
    """Inspetor SOMENTE LEITURA das tabelas do banco.

    Query: ``?table=<polls|voters|votes|booths|settings>`` (default ``polls``).
    Devolve as colunas (``PRAGMA table_info``) e ate ``DB_INSPECT_ROW_LIMIT``
    linhas de cada tabela (whitelist). A tabela ``votes`` e exibida no formato
    ANONIMO ESTRITO - sem badge_id/voter_id/IP/carimbo de tempo - acompanhada de
    uma nota de esquema que comprova a ausencia de vinculo votante->voto.
    """
    requested = (request.args.get("table") or "").strip().lower()
    table = requested if requested in DB_INSPECT_TABLES else DB_INSPECT_TABLES[0]
    db = get_db()

    columns = [row["name"] for row in db.execute(f"PRAGMA table_info({table})")]
    total = db.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
    rows = [
        {column: row[column] for column in columns}
        for row in db.execute(
            f"SELECT * FROM {table} LIMIT ?", (DB_INSPECT_ROW_LIMIT,)
        ).fetchall()
    ]

    payload: dict[str, Any] = {
        "ok": True,
        "table": table,
        "tables": list(DB_INSPECT_TABLES),
        "columns": columns,
        "rows": rows,
        "count": total,
        "shown": len(rows),
        "limit": DB_INSPECT_ROW_LIMIT,
        "truncated": total > len(rows),
        "read_only": True,
    }
    if table == "votes":
        payload["anonymous"] = True
        payload["schema_note"] = (
            "Esquema ANONIMO estrito: a tabela guarda apenas (id, poll_id, "
            "option_label). Nao existe badge_id, voter_id, IP nem carimbo de "
            "tempo por voto - logo e impossivel correlacionar um votante a "
            "sua escolha."
        )
    elif table == "polls":
        payload["schema_note"] = (
            "Cada plebiscito traz o PIN de encerramento (coluna closure_pin), "
            "exigido por POST /api/admin/close."
        )
    elif table == "settings":
        payload["schema_note"] = (
            "Chaves de configuracao de runtime (fila de tokens, limite de "
            "alerta, PINs de pareamento/purga e atraso de resultados)."
        )
    return jsonify(payload)


@app.get("/api/admin/audit")
@require_device_token
def api_admin_audit():
    """Lista os eventos recentes do log de auditoria (mais novo primeiro).

    Query opcional: ``?limit=N`` (1..``AUDIT_LOG_LIMIT_MAX``, default 100).
    """
    db = get_db()
    try:
        limit = int(request.args.get("limit", AUDIT_LOG_LIMIT_DEFAULT))
    except (TypeError, ValueError):
        limit = AUDIT_LOG_LIMIT_DEFAULT
    entries = list_audit_logs(db, limit)
    return jsonify({
        "ok": True,
        "count": len(entries),
        "limit": max(1, min(AUDIT_LOG_LIMIT_MAX, limit)),
        "entries": entries,
    })


@app.route("/api/admin/settings/results-delay", methods=["GET", "POST"])
@require_device_token
def api_admin_results_delay():
    """Le/define o atraso configuravel dos resultados (anti-inferencia).

    GET  -> ``{"ok": true, "results_delay_minutes": N, ...}``
    POST -> body ``{"minutes": N}`` (aliases: ``results_delay_minutes``/``value``);
            ``0`` volta ao atraso classico em segundos (``RESULTS_DELAY_SECONDS``);
            ``>0`` agrupa a divulgacao publica em janelas de N minutos.
    """
    db = get_db()
    if request.method == "GET":
        return jsonify({
            "ok": True,
            "results_delay_minutes": get_results_delay_minutes(db),
            "results_delay_seconds": RESULTS_DELAY_SECONDS,
            "effective_results_delay": effective_results_delay_seconds(db),
        })

    data = request.get_json(silent=True) or {}
    raw = data.get("minutes")
    if raw is None:
        raw = data.get("results_delay_minutes")
    if raw is None:
        raw = data.get("value")
    if raw is None:
        return _user_error("minutes_required", 400,
                           detail="Informe o campo 'minutes' (numero entre 0 e 1440).")

    with _DB_LOCK:
        try:
            minutes = set_results_delay_minutes(db, raw)
        except ValueError as exc:
            return _user_error(str(exc), 400,
                               detail="Informe um numero de minutos entre 0 e 1440.")
        write_audit_log(db, "results_delay_set",
                        f"Atraso de resultados definido em {minutes} min",
                        actor_ip=_client_ip())
        db.commit()
        state = _admin_state(db)

    effective = effective_results_delay_seconds(db)
    logger.info("Atraso de resultados ajustado para %s min (IP %s).",
                minutes, _client_ip() or "desconhecido")
    return jsonify({
        "ok": True,
        "results_delay_minutes": minutes,
        "results_delay_seconds": RESULTS_DELAY_SECONDS,
        "effective_results_delay": effective,
        "state": state,
        "detail": (f"Resultados agrupados em janelas de {minutes} minuto(s)."
                   if minutes else
                   f"Resultados divulgados apos {effective}s (atraso padrao)."),
    })


# --------------------------------------------------------------------------- #
# API: Backup e restauracao do banco (1 clique)
# --------------------------------------------------------------------------- #
def _safe_remove(path: str) -> None:
    """Remove um arquivo temporario, ignorando erros (best-effort)."""
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


@app.get("/api/admin/backup/download")
@require_device_token
def api_admin_backup_download():
    """Gera e serve um backup CONSISTENTE do banco (API de backup do SQLite).

    Usa ``sqlite3.Connection.backup`` para copiar a base ativa (mesmo com a
    conexao em modo WAL e sob concorrencia) para um arquivo temporario e o
    entrega como download ``votacao_backup_YYYYMMDD_HHMMSS.db``.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"votacao_backup_{stamp}.db"

    fd, tmp_path = tempfile.mkstemp(prefix="votacao_backup_", suffix=".db")
    os.close(fd)
    try:
        source = get_db()
        dest = sqlite3.connect(tmp_path)
        try:
            with dest:
                source.backup(dest)
        finally:
            dest.close()
    except sqlite3.Error as exc:
        _safe_remove(tmp_path)
        logger.error("Falha ao gerar o backup do banco: %s", exc)
        return _user_error("backup_failed", 500, detail=str(exc))

    logger.info("Backup do banco gerado: %s (IP %s).",
                filename, _client_ip() or "desconhecido")

    response = send_file(
        tmp_path, as_attachment=True, download_name=filename,
        mimetype="application/octet-stream", max_age=0,
    )
    response.headers["Cache-Control"] = "no-store"
    # Apaga o arquivo temporario assim que a resposta for concluida.
    response.call_on_close(lambda: _safe_remove(tmp_path))
    return response


@app.post("/api/admin/backup/restore")
@require_device_token
def api_admin_backup_restore():
    """Restaura o banco a partir de um arquivo .db enviado (multipart ``file``).

    Valida que o upload e um SQLite INTEGRO e com o schema esperado antes de
    sobrescrever o banco ativo (via API de backup do SQLite, de forma atomica).
    Apos restaurar, reinicia o estado VOLATIL (cabines ociosas, fila zerada) e
    registra o evento em ``app.log``.
    """
    upload = request.files.get("file")
    if upload is None or not (upload.filename or "").strip():
        return _user_error("file_required", 400,
                           detail="Envie o arquivo de backup (.db) no campo 'file'.")

    fd, tmp_path = tempfile.mkstemp(prefix="votacao_restore_", suffix=".db")
    os.close(fd)
    probe = None
    try:
        upload.save(tmp_path)

        try:
            probe = sqlite3.connect(tmp_path)
            integrity = probe.execute("PRAGMA integrity_check").fetchone()[0]
        except sqlite3.Error as exc:
            logger.warning("Restauracao recusada: arquivo invalido (%s).", exc)
            return _user_error("invalid_sqlite", 400,
                               detail="O arquivo enviado nao e um banco SQLite valido.")

        if str(integrity).lower() != "ok":
            logger.warning("Restauracao recusada: integridade='%s'.", integrity)
            return _user_error(
                "corrupt_backup", 400,
                detail="O banco de backup esta corrompido (integrity_check falhou).",
            )

        tables = {
            row[0]
            for row in probe.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        missing = sorted(REQUIRED_TABLES - tables)
        if missing:
            logger.warning("Restauracao recusada: tabelas ausentes %s.", missing)
            return _user_error(
                "invalid_schema", 400,
                detail="Backup incompativel. Tabelas ausentes: " + ", ".join(missing),
            )

        with _DB_LOCK:
            db = get_db()
            probe.backup(db)  # sobrescreve o banco ativo com o conteudo do backup
            db.execute("UPDATE booths SET status = 'idle', busy_until = NULL")
            set_voting_tokens(db, 0)
            # O backup pode ser de uma versao anterior sem audit_logs.
            ensure_audit_logs_table(db)
            write_audit_log(
                db, "db_restore",
                f"Banco restaurado de '{upload.filename}'",
                actor_ip=_client_ip(),
            )
            db.commit()
            state = _admin_state(db)
            history = history_payload(db)
    finally:
        if probe is not None:
            probe.close()
        _safe_remove(tmp_path)

    logger.info(
        "Banco de dados RESTAURADO a partir de '%s' (IP %s). Estado volatil "
        "reiniciado (cabines ociosas, fila zerada).",
        upload.filename, _client_ip() or "desconhecido",
    )
    return jsonify({
        "ok": True,
        "state": state,
        "history": history,
        "detail": "Banco de dados restaurado com sucesso.",
    })


# --------------------------------------------------------------------------- #
# Relatorio oficial / Ata de fechamento (protegido por X-Device-Token)
# --------------------------------------------------------------------------- #
@app.get("/api/admin/report")
@require_device_token
def api_admin_report():
    """Relatorio oficial (JSON) com o resultado final agregado do plebiscito."""
    db = get_db()
    return jsonify(report_payload(db))


@app.get("/api/admin/polls/<int:poll_id>/verify")
@app.get("/api/admin/verify")
@require_device_token
def api_admin_verify(poll_id: Optional[int] = None):
    """Verifica a integridade (SHA-256) de um plebiscito.

    Sem ``poll_id`` na URL verifica o plebiscito ATIVO. Compara o digest
    recalculado com o selo gravado em ``polls.integrity_hash`` no fechamento.
    Uma divergencia (``valid=false``, ``reason=checksum_mismatch``) indica que
    o banco foi alterado externamente DEPOIS do encerramento.
    """
    db = get_db()
    target = poll_id if poll_id is not None else get_active_poll_id(db)
    if target is None:
        return _user_error("no_poll", 409)
    return jsonify(verify_poll_integrity(db, target))


@app.template_filter("fmt_dt")
def _fmt_dt(value: Optional[str]) -> str:
    """Formata um timestamp ISO/UTC para leitura humana (dd/mm/aaaa HH:MM UTC)."""
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return str(value)
    return dt.strftime("%d/%m/%Y %H:%M UTC")


def _admin_page_denied(label: str):
    """Resposta HTML 401 padrao para as paginas de relatorio (Ata/relatorios).

    As paginas de relatorio sao abertas em nova aba e nao consomem JSON:
    devolvemos um documento minimo em portugues com o mesmo status HTTP das
    APIs administrativas. Retorna ``None`` quando o token e valido.
    """
    provided = _extract_token()
    if _token_valid(provided):
        return None
    logger.warning(
        "Falha de autenticacao admin (401) em %s - IP %s",
        label, _client_ip() or "desconhecido",
    )
    return (
        "<!doctype html><html lang='pt-BR'><head><meta charset='utf-8'>"
        "<title>Acesso negado</title></head><body "
        "style=\"font-family:system-ui,sans-serif;padding:40px\">"
        "<h1>401 — Acesso negado</h1>"
        "<p>Token de dispositivo ausente ou inválido. "
        "Abra o relatório pelo painel administrativo.</p></body></html>",
        401,
    )


def _signatories_from_request() -> list[dict]:
    """Le as linhas de assinatura dinamicas da Ata a partir da query string.

    O painel envia listas paralelas (``sig_nome``, ``sig_cargo``, ``sig_doc``),
    permitindo adicionar/remover signatarios dinamicamente. Linhas sem nome sao
    ignoradas; se nenhuma sobrar, retorna a lista padrao (linhas em branco para
    assinatura manual). Limita a ``MAX_SIGNATORIES`` entradas.
    """
    names = request.args.getlist("sig_nome")
    roles = request.args.getlist("sig_cargo")
    docs = request.args.getlist("sig_doc")

    signatories: list[dict] = []
    for idx, raw_name in enumerate(names):
        name = (raw_name or "").strip()
        if not name:
            continue
        role = roles[idx].strip() if idx < len(roles) else ""
        doc = docs[idx].strip() if idx < len(docs) else ""
        signatories.append(
            {"name": name[:120], "role": role[:120], "doc": doc[:60]}
        )
        if len(signatories) >= MAX_SIGNATORIES:
            break
    return signatories or list(DEFAULT_SIGNATORIES)


@app.get("/admin/relatorio")
def admin_report_page():
    """Ata de fechamento em HTML/A4 pronta para impressao ou "Salvar como PDF".

    Mesma protecao das rotas administrativas: Device Token via header
    ``X-Device-Token`` ou ``?token=...`` (usado no link do painel).
    """
    denied = _admin_page_denied("Ata de fechamento")
    if denied is not None:
        return denied
    db = get_db()
    return render_template(
        "relatorio.html",
        report=report_payload(db),
        signatories=_signatories_from_request(),
        token=_extract_token(),
    )


@app.get("/admin/relatorio/historico")
def admin_history_report_page():
    """Relatorio Geral de Historico (A4): resumo de todos os plebiscitos.

    Mesma protecao das rotas administrativas (Device Token via header ou
    ``?token=...``). Traz o resumo consolidado (plebiscitos concluidos,
    comparecimento e totais) e o detalhamento por plebiscito.
    """
    denied = _admin_page_denied("Relatorio Geral de Historico")
    if denied is not None:
        return denied
    db = get_db()
    return render_template(
        "relatorio_historico.html",
        report=history_report_payload(db),
        token=_extract_token(),
    )


@app.get("/admin/relatorio/cabines")
def admin_booth_report_page():
    """Relatorio de Auditoria de Cabines (A4): cabines, IPs e heartbeats.

    Mesma protecao das rotas administrativas (Device Token via header ou
    ``?token=...``). Lista cabines ativas/offline, endereco IP observado,
    ultimo heartbeat e situacao de pareamento.
    """
    denied = _admin_page_denied("Relatorio de Auditoria de Cabines")
    if denied is not None:
        return denied
    db = get_db()
    return render_template(
        "relatorio_cabines.html",
        report=booth_audit_payload(db),
        token=_extract_token(),
    )


# --------------------------------------------------------------------------- #
# Handlers de erro
# --------------------------------------------------------------------------- #
@app.errorhandler(404)
def _not_found(_e):
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "not_found"}), 404
    return redirect(url_for("index"))


@app.errorhandler(405)
def _method_not_allowed(_e):
    return jsonify({"ok": False, "error": "method_not_allowed"}), 405


@app.errorhandler(500)
def _internal_error(e):
    original = getattr(e, "original_exception", None) or e
    logger.error(
        "Erro interno do servidor (500) em %s %s - IP %s: %s",
        request.method, request.path, _client_ip() or "desconhecido", original,
        exc_info=original,
    )
    return jsonify({"ok": False, "error": "internal_error"}), 500


def _banner(mode: str = "Production (Waitress)") -> None:
    line = "=" * 68
    print(line)
    print(" SISTEMA DE VOTACAO LOCAL - servidor iniciado")
    print(line)
    print(f" Modo                        : {mode}")
    print(f" Bind (IP:porta)            : {HOST}:{PORT}")
    print(line)
    print(f" Check-in (servidor/leitor NFC) : http://localhost:{PORT}/checkin")
    print(f" Cabine de votacao              : http://localhost:{PORT}/cabine")
    print(f" Painel TV (resultados)         : http://localhost:{PORT}/painel-tv")
    print(f" Admin                          : http://localhost:{PORT}/admin"
          f"?token={DEVICE_TOKEN}")
    print(f" Relatorio / Ata de fechamento  : http://localhost:{PORT}/admin/relatorio"
          f"?token={DEVICE_TOKEN}")
    print(line)
    print(f" DEVICE_TOKEN : {DEVICE_TOKEN}")
    if _TOKEN_WAS_GENERATED:
        print(" (token gerado automaticamente - defina DEVICE_TOKEN para fixar)")
    print(f" Banco de dados              : {DB_PATH}")
    if os.path.abspath(DB_PATH) != os.path.abspath(REQUESTED_DB_PATH):
        print(f" (fallback em uso - VOTING_DB pedido: {REQUESTED_DB_PATH})")
    print(f" Cooldown check-in           : {CHECKIN_COOLDOWN_SECONDS}s")
    print(f" Atraso dos resultados (TV)  : {RESULTS_DELAY_SECONDS}s")
    print(line)
    # Garante que o log de inicializacao apareca imediatamente, mesmo com a
    # saida redirecionada para pipe/arquivo (o servidor WSGI nao faz flush).
    sys.stdout.flush()

    # Registro estruturado do boot (vai tambem para app.log).
    logger.info("Servidor iniciado (%s) - bind %s:%s", mode, HOST, PORT)
    logger.info(
        "DEVICE_TOKEN ativo: %s%s", DEVICE_TOKEN,
        " (gerado automaticamente - defina DEVICE_TOKEN para fixar)"
        if _TOKEN_WAS_GENERATED else "",
    )
    logger.info("Banco de dados em uso: %s", DB_PATH)
    if os.path.abspath(DB_PATH) != os.path.abspath(REQUESTED_DB_PATH):
        logger.warning("Fallback de banco em uso - VOTING_DB pedido: %s",
                       REQUESTED_DB_PATH)


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """Interpreta os argumentos de linha de comando do servidor.

    O modo padrao e PRODUCAO (Waitress). Use ``--dev`` para forcar o servidor
    de desenvolvimento do Flask (apenas para depuracao local).
    """
    parser = argparse.ArgumentParser(
        description="Sistema de Votacao Eletronica Local (Flask + Waitress).",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--production",
        dest="production",
        action="store_true",
        help="Executa com o servidor WSGI de producao (Waitress). Padrao.",
    )
    group.add_argument(
        "--dev",
        "--development",
        dest="production",
        action="store_false",
        help="Executa com o servidor de desenvolvimento do Flask (nao use "
        "em producao).",
    )
    parser.set_defaults(production=True)
    return parser.parse_args(argv)


def _run_production() -> None:
    """Sobe o servidor WSGI de producao (Waitress).

    Waitress e um servidor WSGI puro-Python, robusto e multiplataforma,
    adequado a rede ISOLADA (roteador dedicado, ambiente industrial) sem
    expor o aviso de "development server" do Flask.
    """
    try:
        from waitress import serve
    except ImportError:
        print(" [AVISO] Waitress nao esta instalado - caindo para o servidor de")
        print("         desenvolvimento do Flask. Instale com:")
        print("             pip install -r requirements.txt")
        app.run(host=HOST, port=PORT, threaded=True, debug=False)
        return
    # threads: varias cabines + painel TV consultando ao mesmo tempo.
    # clear_untrusted_proxy_headers=False: preserva o X-Forwarded-For para o
    # monitor de IP das cabines (rede isolada, sem proxy externo hostil).
    try:
        serve(app, host=HOST, port=PORT, threads=8,
              ident="Sistema de Votacao Local (Waitress)",
              clear_untrusted_proxy_headers=False)
    except TypeError:
        # Waitress antigo (sem o parametro) - segue com o comportamento padrao.
        serve(app, host=HOST, port=PORT, threads=8,
              ident="Sistema de Votacao Local (Waitress)")


if __name__ == "__main__":
    args = _parse_args()
    init_db()
    _banner("Development (Flask)" if not args.production
            else "Production (Waitress)")
    if args.production:
        _run_production()
    else:
        # threaded=True: varias cabines + TV consultando ao mesmo tempo.
        app.run(host=HOST, port=PORT, threaded=True, debug=False)

