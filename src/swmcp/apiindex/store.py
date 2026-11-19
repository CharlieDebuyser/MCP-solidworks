"""Index local de l'API SolidWorks : stockage SQLite et recherche plein texte (spec §7).

L'index ne contient que des noms et des signatures extraits des bibliothèques de types COM ;
aucun texte de la documentation Dassault n'y figure.

- `IndexWriter` crée l'index (fichier temporaire, puis remplacement atomique à la fermeture).
- `ApiIndex` l'interroge en lecture seule, avec une connexion par appel (donc sans état partagé
  entre threads).

La recherche repose sur une colonne de jetons calculée à l'écriture (découpage CamelCase,
minuscules). FTS5 sert de filtre quand il est disponible ; sinon le même filtre est exprimé en LIKE.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Literal, Self

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "1"
MEMBERS_CAP = 200
MAX_LIMIT = 200
_MAX_QUERY_TOKENS = 16
_ENUM_PREVIEW = 6

Kind = Literal["interface", "method", "property", "enum", "enum_member"]


class ApiParam(BaseModel):
    name: str
    type: str
    direction: Literal["in", "out", "inout", "retval"]
    optional: bool = False


class ApiHit(BaseModel):
    name: str
    kind: Kind
    signature: str
    library: str


class ApiDoc(ApiHit):
    params: list[ApiParam] = Field(default_factory=list)
    returns: str | None = None
    members: list[str] = Field(default_factory=list)
    members_total: int = 0
    value: int | None = None


# --------------------------------------------------------------------------------------
# Jetons
# --------------------------------------------------------------------------------------

_WORD_RE = re.compile(r"[^\W_]+")
_CAMEL_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+|[^\W\d_A-Za-z]+")


def split_identifier(text: str) -> list[str]:
    """Découpe un identifiant ou une requête en jetons minuscules.

    « FeatureExtrusion3 » → feature, extrusion, 3 ; « swEndConditions_e » → sw, end, conditions, e.
    Seuls des caractères alphanumériques subsistent : les jetons sont donc sûrs à injecter dans une
    requête FTS (entre guillemets) ou un motif LIKE.
    """
    out: list[str] = []
    for word in _WORD_RE.findall(text):
        out.extend(part.lower() for part in _CAMEL_RE.findall(word))
    return out


def _doc_tokens(identifier: str) -> list[str]:
    """Jetons indexés pour un identifiant : ses morceaux + sa forme compacte (« featureextrusion3 »)."""
    tokens = split_identifier(identifier)
    compact = "".join(w.lower() for w in _WORD_RE.findall(identifier))
    if compact:
        tokens.append(compact)
    return list(dict.fromkeys(tokens))


def _query_tokens(query: str) -> list[str]:
    return list(dict.fromkeys(split_identifier(query)))[:_MAX_QUERY_TOKENS]


def _pad(tokens: list[str]) -> str:
    return " " + " ".join(tokens) + " " if tokens else " "


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# --------------------------------------------------------------------------------------
# Signatures
# --------------------------------------------------------------------------------------


def _format_param(param: ApiParam) -> str:
    prefix = f"{param.direction} " if param.direction in ("out", "inout") else ""
    suffix = "?" if param.optional else ""
    return f"{prefix}{param.name}{suffix}: {param.type}"


def _split_retval(params: list[ApiParam], returns: str | None) -> tuple[list[ApiParam], str | None]:
    """Sépare les paramètres [retval] : ils n'apparaissent pas dans la liste d'arguments."""
    args = [p for p in params if p.direction != "retval"]
    if returns is None:
        returns = next((p.type for p in params if p.direction == "retval"), None)
    return args, returns


def _member_signature(
    name: str, kind: Literal["method", "property"], params: list[ApiParam], returns: str | None
) -> str:
    args, ret = _split_retval(params, returns)
    if kind == "property" and not args:
        return f"{name}: {ret}" if ret else name
    return f"{name}({', '.join(_format_param(p) for p in args)}) -> {ret or 'None'}"


def _enum_signature(name: str, values: dict[str, int]) -> str:
    items = [f"{k} = {v}" for k, v in list(values.items())[:_ENUM_PREVIEW]]
    if len(values) > _ENUM_PREVIEW:
        items.append("...")
    return f"enum {name} {{ {', '.join(items)} }}" if items else f"enum {name} {{ }}"


# --------------------------------------------------------------------------------------
# Écriture
# --------------------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE entries (
    id INTEGER PRIMARY KEY,
    library TEXT NOT NULL,
    name TEXT NOT NULL,
    name_lower TEXT NOT NULL,
    short TEXT NOT NULL,
    short_lower TEXT NOT NULL,
    parent_lower TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    signature TEXT NOT NULL,
    params TEXT NOT NULL DEFAULT '[]',
    returns TEXT,
    value INTEGER,
    own_tokens TEXT NOT NULL,
    parent_tokens TEXT NOT NULL,
    tokens TEXT NOT NULL,
    UNIQUE (library, name_lower, kind)
);
CREATE INDEX entries_name ON entries (name_lower);
CREATE INDEX entries_short ON entries (short_lower);
CREATE INDEX entries_parent ON entries (parent_lower, library);
"""


def _fts5_available(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("CREATE VIRTUAL TABLE temp._fts5_probe USING fts5(x)")
        conn.execute("DROP TABLE temp._fts5_probe")
    except sqlite3.Error:
        return False
    return True


class IndexWriter:
    """Construit un index dans un fichier temporaire ; `close()` le met en place par `os.replace`.

    Tant que `close()` n'a pas été appelé, l'index existant (s'il y en a un) reste intact et lisible.
    Les doublons (même bibliothèque, même nom, même nature) sont ignorés.

    `use_fts` : None = FTS5 si disponible (défaut) ; False force le repli LIKE (utile en test).
    """

    def __init__(self, db_path: Path, use_fts: bool | None = None) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._tmp_path = self._db_path.with_name(f".{self._db_path.name}.{os.getpid()}.tmp")
        self._tmp_path.unlink(missing_ok=True)
        self._conn: sqlite3.Connection | None = sqlite3.connect(self._tmp_path)
        self._conn.executescript(_SCHEMA)
        self._fts = (use_fts is not False) and _fts5_available(self._conn)
        if self._fts:
            self._conn.execute("CREATE VIRTUAL TABLE entries_fts USING fts5(tokens, tokenize='unicode61')")
        self._meta: dict[str, str] = {}
        self._libraries: dict[str, None] = {}
        self._counts = {"interfaces": 0, "members": 0, "enums": 0, "enum_members": 0}

    # -- API publique ------------------------------------------------------------------

    @property
    def counts(self) -> dict[str, int]:
        """Nombre d'entrées réellement insérées (doublons exclus)."""
        return dict(self._counts)

    def add_interface(self, library: str, name: str) -> None:
        if self._insert(library, name, name, "", "interface", f"interface {name}"):
            self._counts["interfaces"] += 1

    def add_member(
        self,
        library: str,
        interface: str,
        name: str,
        kind: Literal["method", "property"],
        params: list[ApiParam],
        returns: str | None,
    ) -> None:
        args, ret = _split_retval(params, returns)
        inserted = self._insert(
            library,
            f"{interface}.{name}",
            name,
            interface,
            kind,
            _member_signature(name, kind, params, returns),
            params=json.dumps([p.model_dump() for p in args], ensure_ascii=False),
            returns=ret,
        )
        if inserted:
            self._counts["members"] += 1

    def add_enum(self, library: str, name: str, values: dict[str, int]) -> None:
        if self._insert(library, name, name, "", "enum", _enum_signature(name, values)):
            self._counts["enums"] += 1
        for member, value in values.items():
            full = f"{name}.{member}"
            if self._insert(library, full, member, name, "enum_member", f"{full} = {value}", value=value):
                self._counts["enum_members"] += 1

    def set_meta(self, key: str, value: str) -> None:
        self._meta[key] = value

    def close(self) -> None:
        """Valide, ferme et remplace atomiquement l'index précédent. Sans effet si déjà fermé."""
        conn = self._conn
        if conn is None:
            return
        self._conn = None
        try:
            meta = {
                "schema_version": SCHEMA_VERSION,
                "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "libraries": ", ".join(self._libraries),
                "counts": json.dumps(self._counts),
                "fts": "1" if self._fts else "0",
            }
            meta.update(self._meta)
            conn.executemany("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", list(meta.items()))
            conn.commit()
        except BaseException:
            conn.close()
            self._tmp_path.unlink(missing_ok=True)
            raise
        conn.close()
        os.replace(self._tmp_path, self._db_path)

    def abort(self) -> None:
        """Abandonne l'écriture : l'index existant n'est pas touché."""
        conn = self._conn
        if conn is None:
            return
        self._conn = None
        conn.close()
        self._tmp_path.unlink(missing_ok=True)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        if exc_type is None:
            self.close()
        else:
            self.abort()

    # -- interne -----------------------------------------------------------------------

    def _insert(
        self,
        library: str,
        name: str,
        short: str,
        parent: str,
        kind: Kind,
        signature: str,
        *,
        params: str = "[]",
        returns: str | None = None,
        value: int | None = None,
    ) -> bool:
        if self._conn is None:
            raise RuntimeError("IndexWriter déjà fermé.")
        self._libraries.setdefault(library)
        own = _doc_tokens(short)
        inherited = _doc_tokens(parent)
        every = list(dict.fromkeys([*own, *inherited]))
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO entries (library, name, name_lower, short, short_lower, parent_lower,"
            " kind, signature, params, returns, value, own_tokens, parent_tokens, tokens)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                library,
                name,
                name.lower(),
                short,
                short.lower(),
                parent.lower(),
                kind,
                signature,
                params,
                returns,
                value,
                _pad(own),
                _pad(inherited),
                _pad(every),
            ),
        )
        if cur.rowcount != 1:
            return False
        if self._fts:
            self._conn.execute(
                "INSERT INTO entries_fts (rowid, tokens) VALUES (?, ?)", (cur.lastrowid, " ".join(every))
            )
        return True


# --------------------------------------------------------------------------------------
# Lecture
# --------------------------------------------------------------------------------------


class ApiIndex:
    """Lecture seule de l'index. Chaque appel ouvre puis ferme sa propre connexion (thread-safe)."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)

    @property
    def exists(self) -> bool:
        return self._db_path.is_file()

    def meta(self) -> dict[str, str]:
        try:
            with self._connect() as conn:
                if conn is None:
                    return {}
                return {str(k): str(v) for k, v in conn.execute("SELECT key, value FROM meta")}
        except sqlite3.Error as exc:
            logger.warning("Index de l'API illisible (%s) : %s", self._db_path, exc)
            return {}

    def search(
        self, query: str, kind: str | None = None, limit: int = 20, offset: int = 0
    ) -> tuple[list[ApiHit], int]:
        tokens = _query_tokens(query)
        if not tokens:
            return [], 0
        limit = max(0, min(limit, MAX_LIMIT))
        offset = max(0, offset)
        try:
            with self._connect() as conn:
                if conn is None:
                    return [], 0
                if _has_fts_table(conn):
                    try:
                        return _search(conn, query, tokens, kind, limit, offset, use_fts=True)
                    except sqlite3.OperationalError:
                        # Index construit avec FTS5 mais lu par un SQLite qui ne l'a pas.
                        logger.info("FTS5 indisponible à la lecture : repli sur LIKE.")
                return _search(conn, query, tokens, kind, limit, offset, use_fts=False)
        except sqlite3.Error as exc:
            logger.warning("Index de l'API illisible (%s) : %s", self._db_path, exc)
            return [], 0

    def describe(self, name: str) -> ApiDoc | None:
        key = name.strip().lower()
        if not key:
            return None
        try:
            with self._connect() as conn:
                if conn is None:
                    return None
                return _describe(conn, key)
        except sqlite3.Error as exc:
            logger.warning("Index de l'API illisible (%s) : %s", self._db_path, exc)
            return None

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection | None]:
        if not self.exists:
            yield None
            return
        conn = sqlite3.connect(f"{self._db_path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            yield conn
        finally:
            conn.close()


def _has_fts_table(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'entries_fts'").fetchone()
    return row is not None


def _search(
    conn: sqlite3.Connection,
    query: str,
    tokens: list[str],
    kind: str | None,
    limit: int,
    offset: int,
    *,
    use_fts: bool,
) -> tuple[list[ApiHit], int]:
    where: list[str] = []
    where_args: list[str] = []
    if use_fts:
        # Chaque jeton est purement alphanumérique et cité : aucune syntaxe FTS ne peut s'y glisser.
        where.append("e.id IN (SELECT rowid FROM entries_fts WHERE entries_fts MATCH ?)")
        where_args.append(" AND ".join(f'"{t}"*' for t in tokens))
    else:
        for token in tokens:
            where.append("e.tokens LIKE ?")
            where_args.append(f"% {token}%")
    if kind is not None:
        where.append("e.kind = ?")
        where_args.append(kind)
    where_sql = " AND ".join(where)

    total = int(conn.execute(f"SELECT COUNT(*) FROM entries e WHERE {where_sql}", where_args).fetchone()[0])
    if total == 0 or limit == 0:
        return [], total

    # Classement : nom exact, puis préfixe du nom, puis jetons tous présents dans le nom propre
    # (et non seulement dans celui du parent), puis le conteneur avant ses membres (un membre dont
    # le parent satisfait déjà la requête passe après), puis noms courts d'abord.
    exact = query.strip().lower()
    own_sql = " AND ".join("e.own_tokens LIKE ?" for _ in tokens)
    parent_sql = " AND ".join("e.parent_tokens LIKE ?" for _ in tokens)
    patterns = [f"% {t}%" for t in tokens]
    order_args: list[str] = [exact, exact, _like_escape(exact) + "%", *patterns, *patterns]
    rows = conn.execute(
        "SELECT e.name, e.kind, e.signature, e.library FROM entries e"
        f" WHERE {where_sql}"
        " ORDER BY (e.short_lower = ? OR e.name_lower = ?) DESC,"
        " (e.short_lower LIKE ? ESCAPE '\\') DESC,"
        f" ({own_sql}) DESC, ({parent_sql}) ASC, length(e.short_lower), e.name_lower, e.library, e.id"
        " LIMIT ? OFFSET ?",
        [*where_args, *order_args, limit, offset],
    ).fetchall()
    hits = [ApiHit(name=r[0], kind=r[1], signature=r[2], library=r[3]) for r in rows]
    return hits, total


_DESCRIBE_COLUMNS = "name, kind, signature, library, params, returns, value, name_lower"


def _describe(conn: sqlite3.Connection, key: str) -> ApiDoc | None:
    row = None
    for column in ("name_lower", "short_lower"):
        row = conn.execute(
            f"SELECT {_DESCRIBE_COLUMNS} FROM entries WHERE {column} = ?"
            " ORDER BY name_lower, library, id LIMIT 1",
            (key,),
        ).fetchone()
        if row is not None:
            break
    if row is None:
        return None
    name, kind, signature, library, params_json, returns, value, name_lower = row
    doc = ApiDoc(
        name=name,
        kind=kind,
        signature=signature,
        library=library,
        params=[ApiParam.model_validate(p) for p in json.loads(params_json)],
        returns=returns,
        value=value,
    )
    if kind in ("interface", "enum"):
        child_args = (name_lower, library)
        doc.members_total = int(
            conn.execute(
                "SELECT COUNT(*) FROM entries WHERE parent_lower = ? AND library = ?", child_args
            ).fetchone()[0]
        )
        doc.members = [
            str(r[0])
            for r in conn.execute(
                "SELECT short FROM entries WHERE parent_lower = ? AND library = ?"
                " ORDER BY value, short_lower, id LIMIT ?",
                (*child_args, MEMBERS_CAP),
            )
        ]
    return doc
