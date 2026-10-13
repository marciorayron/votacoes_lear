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
import json
import os
import secrets
import sqlite3
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Any, Optional

from flask import Flask, g, jsonify, redirect, render_template, request, url_for

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

# Identidade exibida na Ata/relatorio oficial (cabecalho do documento).
FACTORY_NAME = os.environ.get("FACTORY_NAME", "Fábrica")
REPORT_TITLE = "Ata de Fechamento — Relatório Oficial"

POLL_STATUS_OPEN = "open"
POLL_STATUS_CLOSED = "closed"

# Rastreamento de cabines (kiosks). Sem heartbeat por BOOTH_TIMEOUT_SECONDS a
# cabine e considerada offline. Apos receber uma liberacao de check-in, a
# cabine fica reservada por BOOTH_BUSY_SECONDS (ou ate reportar-se ociosa).
BOOTH_TIMEOUT_SECONDS = float(os.environ.get("BOOTH_TIMEOUT_SECONDS", "20"))
BOOTH_BUSY_SECONDS = float(os.environ.get("BOOTH_BUSY_SECONDS", "45"))

# Serializa as operacoes de leitura-modificacao-escrita nos contadores.
_DB_LOCK = threading.RLock()

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False


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
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                question   TEXT NOT NULL,
                options    TEXT NOT NULL,               -- JSON: [{"id","label"}]
                status     TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL,
                closed_at  TEXT
            );

            CREATE TABLE IF NOT EXISTS votes (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                poll_id    INTEGER NOT NULL,
                option_id  TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (poll_id) REFERENCES polls(id) ON DELETE CASCADE
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

            CREATE INDEX IF NOT EXISTS idx_votes_poll ON votes(poll_id);
            CREATE INDEX IF NOT EXISTS idx_booths_number ON booths(number);
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
        conn.commit()
    except sqlite3.Error as exc:
        print(f"[db] ERRO ao criar/atualizar o schema em '{DB_PATH}': {exc}")
        raise
    finally:
        conn.close()


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Adiciona colunas novas a bancos ja existentes (idempotente).

    Necessario porque ``CREATE TABLE IF NOT EXISTS`` nao altera tabelas
    preexistentes: sem isto, um ``votacao.db`` antigo ficaria sem as colunas
    de IP/pareamento e as consultas falhariam.
    """
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(booths)")}
    for column in ("client_ip", "pairing_token"):
        if column not in existing:
            conn.execute(f"ALTER TABLE booths ADD COLUMN {column} TEXT")


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
# Pareamento de cabines (Pairing PIN)
# --------------------------------------------------------------------------- #
PAIRING_PIN_SETTING = "booth_pairing_pin"


def get_pairing_pin(db: sqlite3.Connection) -> str:
    """PIN atual de pareamento (string vazia = pareamento desativado)."""
    return get_setting(db, PAIRING_PIN_SETTING, "").strip()


def generate_pairing_pin() -> str:
    """Gera um PIN numerico temporario de 6 digitos."""
    return f"{secrets.randbelow(1_000_000):06d}"


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


def tally_votes(
    db: sqlite3.Connection, poll_id: int, cutoff_iso: Optional[str] = None
) -> dict:
    """Conta votos por opcao. Se cutoff_iso for dado, so conta votos <= cutoff."""
    params: list[Any] = [poll_id]
    sql = "SELECT option_id, COUNT(*) AS total FROM votes WHERE poll_id = ?"
    if cutoff_iso is not None:
        sql += " AND created_at <= ?"
        params.append(cutoff_iso)
    sql += " GROUP BY option_id"
    rows = db.execute(sql, params).fetchall()
    return {r["option_id"]: r["total"] for r in rows}


def clear_voters(db: sqlite3.Connection) -> None:
    """Remove todos os crachas registrados (libera todos para novo pleito)."""
    db.execute("DELETE FROM voters")


def votes_per_option(poll: dict, counts: dict) -> list[dict]:
    """Monta a lista de opcoes com contagem (garante ordem de declaracao)."""
    result = []
    for opt in poll["options"]:
        opt_id = opt.get("id")
        result.append(
            {
                "id": opt_id,
                "label": opt.get("label", ""),
                "count": int(counts.get(opt_id, 0)),
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


def report_payload(db: sqlite3.Connection) -> dict:
    """Agrega o resultado final do plebiscito ativo/encerrado para a Ata.

    Inclui: pergunta, opcoes com contagem e percentual, total de votos,
    timestamps de abertura/encerramento e o total de votantes unicos.
    """
    poll_id = get_active_poll_id(db)
    poll = get_poll(db, poll_id) if poll_id is not None else None

    payload: dict[str, Any] = {
        "ok": True,
        "factory_name": FACTORY_NAME,
        "title": REPORT_TITLE,
        "generated_at": now_iso(),
        "total_voters_registered": total_voters_for_poll(db, poll),
        "poll": None,
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
    return render_template("painel_tv.html", results_delay=RESULTS_DELAY_SECONDS)


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
        "results_delay": RESULTS_DELAY_SECONDS,
        "server_time": now_iso(),
    }
    booth_id = request.args.get("booth_id", "").strip()
    if booth_id:
        booth = get_booth(db, booth_id)
        payload["booth"] = booth
        payload["assigned"] = bool(booth and booth["assigned"])
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
        if existing is None:
            # Cabine nova: exige o PIN quando o pareamento esta ativo.
            if required_pin and not _pin_matches(provided_pin, required_pin):
                return _user_error(
                    "pairing_required", 403,
                    detail="Informe o PIN de pareamento exibido no painel administrativo.",
                )
        else:
            stored = existing["pairing_token"]
            if stored and provided_token and _token_equal(provided_token, stored):
                pass  # token valido -> segue
            elif required_pin and _pin_matches(provided_pin, required_pin):
                rotate = True  # re-pareamento autorizado pelo PIN -> novo token
            elif not stored:
                pass  # registro legado (sem token) -> segue
            else:
                return _user_error(
                    "pairing_required", 403,
                    detail="Cabine ja pareada. Remova-a no painel para parear novamente.",
                )
        booth = register_booth(db, booth_id, client_ip, rotate=rotate)

    if booth is None:
        return _user_error("invalid_booth_id", 400)
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

        valid_ids = {o.get("id") for o in poll["options"]}
        if option_id not in valid_ids:
            return _user_error("invalid_option", 400)

        tokens = get_voting_tokens(db)
        if tokens <= 0:
            # Outra cabine consumiu o token primeiro.
            return jsonify(
                {"ok": False, "status": "no_token",
                 "detail": "Nenhum token de votacao disponivel."}
            ), 409

        db.execute(
            "INSERT INTO votes(poll_id, option_id, created_at) VALUES(?, ?, ?)",
            (poll["id"], option_id, now_iso()),
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

    Somente votos mais antigos que RESULTS_DELAY_SECONDS entram na contagem
    exibida. Isso evita correlacionar em tempo real quem acabou de votar,
    preservando o sigilo do voto e dando transparencia ao processo.
    """
    db = get_db()
    poll_id = get_active_poll_id(db)
    poll = get_poll(db, poll_id) if poll_id is not None else None

    if poll is None:
        return jsonify(
            {"ok": True, "status": "no_poll", "question": None, "options": [],
             "total": 0, "voting_tokens": get_voting_tokens(db),
             "results_delay": RESULTS_DELAY_SECONDS, "generated_at": now_iso()}
        )

    cutoff = None
    if RESULTS_DELAY_SECONDS > 0:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=RESULTS_DELAY_SECONDS)
        ).isoformat(timespec="seconds")

    counts = tally_votes(db, poll["id"], cutoff_iso=cutoff)
    options = votes_per_option(poll, counts)
    total = sum(o["count"] for o in options)

    # Quantos votos ainda estao "em carencia" (nao exibidos).
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
            "results_delay": RESULTS_DELAY_SECONDS,
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
            "cooldown": CHECKIN_COOLDOWN_SECONDS,
            "results_delay": RESULTS_DELAY_SECONDS,
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
    poll_payload = None
    if poll is not None:
        counts = tally_votes(db, poll["id"])
        options = votes_per_option(poll, counts)
        poll_payload = {
            "id": poll["id"],
            "question": poll["question"],
            "status": poll["status"],
            "created_at": poll["created_at"],
            "closed_at": poll["closed_at"],
            "options": options,
            "total_votes": sum(o["count"] for o in options),
        }

    pairing_pin = get_pairing_pin(db)
    return {
        "ok": True,
        "poll": poll_payload,
        "voting_tokens": get_voting_tokens(db),
        "voters_checked_in": voters,
        "booths": list_booths(db),
        "pairing_pin": pairing_pin,
        "pairing_enabled": bool(pairing_pin),
        "cooldown": CHECKIN_COOLDOWN_SECONDS,
        "results_delay": RESULTS_DELAY_SECONDS,
        "server_time": now_iso(),
    }


@app.get("/api/admin/state")
@require_device_token
def api_admin_state():
    """Estado atual do plebiscito, contadores e configuracao."""
    db = get_db()
    return jsonify(_admin_state(db))


@app.post("/api/admin/poll")
@require_device_token
def api_admin_start_poll():
    """Cria e inicia um novo plebiscito dinamico.

    Body JSON:
        {"question": "Texto da pergunta",
         "options": ["Sim", "Nao"]  }   # ou [{"label": "..."}, ...]

    Iniciar um novo plebiscito:
      * fecha a poll anterior (se aberta);
      * limpa a lista de crachas (todos podem votar de novo);
      * zera a fila de tokens de votacao.
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
            db.execute(
                "UPDATE polls SET status = ?, closed_at = ? "
                "WHERE id = ? AND status = ?",
                (POLL_STATUS_CLOSED, now_iso(), previous_id, POLL_STATUS_OPEN),
            )

        cur = db.execute(
            "INSERT INTO polls(question, options, status, created_at) "
            "VALUES(?, ?, ?, ?)",
            (question, json.dumps(options, ensure_ascii=False),
             POLL_STATUS_OPEN, now_iso()),
        )
        new_id = cur.lastrowid

        clear_voters(db)
        set_setting(db, "active_poll_id", new_id)
        set_voting_tokens(db, 0)
        db.commit()
        state = _admin_state(db)

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
        if clear:
            clear_voters(db)
        set_voting_tokens(db, 0)
        db.commit()
        state = _admin_state(db)

    state["detail"] = "Contadores zerados."
    return jsonify(state)


@app.post("/api/admin/close")
@require_device_token
def api_admin_close():
    """Encerra o plebiscito ativo (resultados permanecem consultaveis)."""
    with _DB_LOCK:
        db = get_db()
        poll_id = get_active_poll_id(db)
        if poll_id is None:
            return _user_error("no_active_poll", 409)

        # Preserva a contagem de votantes ANTES de limpar os crachas (Ata).
        preserve_closed_voters(db, poll_id)
        db.execute(
            "UPDATE polls SET status = ?, closed_at = ? WHERE id = ?",
            (POLL_STATUS_CLOSED, now_iso(), poll_id),
        )
        clear_voters(db)
        set_voting_tokens(db, 0)
        db.commit()
        state = _admin_state(db)

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
    state["detail"] = detail
    return jsonify(state)


@app.delete("/api/admin/booths/<path:booth_id>")
@require_device_token
def api_admin_remove_booth(booth_id: str):
    """Remove/desconecta uma cabine registrada (invalidando seu pareamento)."""
    with _DB_LOCK:
        db = get_db()
        cur = db.execute("DELETE FROM booths WHERE booth_id = ?", (booth_id,))
        db.commit()
        if cur.rowcount == 0:
            return _user_error("unknown_booth", 404)
        state = _admin_state(db)
    state["detail"] = "Cabine removida. O dispositivo precisara parear novamente."
    return jsonify(state)


# --------------------------------------------------------------------------- #
# Relatorio oficial / Ata de fechamento (protegido por X-Device-Token)
# --------------------------------------------------------------------------- #
@app.get("/api/admin/report")
@require_device_token
def api_admin_report():
    """Relatorio oficial (JSON) com o resultado final agregado do plebiscito."""
    db = get_db()
    return jsonify(report_payload(db))


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


@app.get("/admin/relatorio")
def admin_report_page():
    """Ata de fechamento em HTML/A4 pronta para impressao ou "Salvar como PDF".

    Mesma protecao das rotas administrativas: Device Token via header
    ``X-Device-Token`` ou ``?token=...`` (usado no link do painel).
    """
    provided = _extract_token()
    if not _token_valid(provided):
        return (
            "<!doctype html><html lang='pt-BR'><head><meta charset='utf-8'>"
            "<title>Acesso negado</title></head><body "
            "style=\"font-family:system-ui,sans-serif;padding:40px\">"
            "<h1>401 — Acesso negado</h1>"
            "<p>Token de dispositivo ausente ou inválido. "
            "Abra o relatório pelo painel administrativo.</p></body></html>",
            401,
        )
    db = get_db()
    return render_template(
        "relatorio.html", report=report_payload(db), token=provided
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
def _internal_error(_e):
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

