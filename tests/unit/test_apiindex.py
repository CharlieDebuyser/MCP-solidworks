"""Tests de l'index de l'API (spec §7) sur un index synthétique, sans SolidWorks."""

from __future__ import annotations

import sqlite3
import sys
import threading
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from swmcp.apiindex import indexer
from swmcp.apiindex.indexer import build_index, find_typelibs
from swmcp.apiindex.store import ApiIndex, ApiParam, IndexWriter, split_identifier

LIB = "SldWorks"


def _populate(writer: IndexWriter) -> None:
    writer.add_interface(LIB, "IFeatureManager")
    writer.add_member(
        LIB,
        "IFeatureManager",
        "FeatureExtrusion3",
        "method",
        [
            ApiParam(name="Sd", type="bool", direction="in"),
            ApiParam(name="Flip", type="bool", direction="in"),
            ApiParam(name="D1", type="float", direction="in", optional=True),
        ],
        "Feature",
    )
    writer.add_member(LIB, "IFeatureManager", "FeatureCut4", "method", [], "Feature")
    writer.add_member(LIB, "IFeatureManager", "FeatureCount", "property", [], "int")
    writer.add_interface(LIB, "IModelDoc2")
    writer.add_member(
        LIB,
        "IModelDoc2",
        "Save3",
        "method",
        [
            ApiParam(name="Options", type="int", direction="in"),
            ApiParam(name="Errors", type="int", direction="inout"),
        ],
        "bool",
    )
    writer.add_member(LIB, "IModelDoc2", "GetTitle", "method", [], "str")
    writer.add_interface(LIB, "ISketchManager")
    writer.add_member(LIB, "ISketchManager", "GetTitle", "method", [], "str")
    writer.add_enum("SwConst", "swEndConditions_e", {"swEndCondBlind": 0, "swEndCondThroughAll": 1})
    writer.add_enum("SwConst", "swDocumentTypes_e", {"swDocPART": 1, "swDocASSEMBLY": 2})


@pytest.fixture(params=["fts", "like"])
def index(request: pytest.FixtureRequest, tmp_path: Path) -> ApiIndex:
    """Le même index synthétique, avec FTS5 puis avec le repli LIKE."""
    db = tmp_path / "api.sqlite"
    with IndexWriter(db, use_fts=None if request.param == "fts" else False) as writer:
        _populate(writer)
    idx = ApiIndex(db)
    assert idx.meta()["fts"] == ("1" if request.param == "fts" else "0")
    return idx


def _names(hits: list[Any]) -> list[str]:
    return [h.name for h in hits]


def test_split_identifier() -> None:
    assert split_identifier("FeatureExtrusion3") == ["feature", "extrusion", "3"]
    assert split_identifier("swEndConditions_e") == ["sw", "end", "conditions", "e"]
    assert split_identifier("IModelDoc2") == ["i", "model", "doc", "2"]
    assert split_identifier(' "*() ') == []


def test_search_camelcase_word(index: ApiIndex) -> None:
    hits, total = index.search("extrusion")
    assert total == 1
    assert hits[0].name == "IFeatureManager.FeatureExtrusion3"
    assert hits[0].kind == "method"
    assert hits[0].library == LIB
    assert hits[0].signature == "FeatureExtrusion3(Sd: bool, Flip: bool, D1?: float) -> Feature"


def test_search_prefix_and_multiple_words(index: ApiIndex) -> None:
    hits, total = index.search("end cond")
    assert total == 3
    assert hits[0].name == "swEndConditions_e"  # l'énumération avant ses membres
    assert set(_names(hits)) == {
        "swEndConditions_e",
        "swEndConditions_e.swEndCondBlind",
        "swEndConditions_e.swEndCondThroughAll",
    }
    assert _names(index.search("featureextr")[0]) == ["IFeatureManager.FeatureExtrusion3"]
    assert _names(index.search("EXTRU")[0]) == ["IFeatureManager.FeatureExtrusion3"]


def test_search_exact_and_qualified_name_rank_first(index: ApiIndex) -> None:
    hits, _ = index.search("IFeatureManager")
    assert hits[0].name == "IFeatureManager"
    hits, total = index.search("IModelDoc2.Save3")
    assert total == 1
    assert hits[0].name == "IModelDoc2.Save3"
    # « feature » : les noms qui commencent par le mot passent avant ceux qui le contiennent.
    hits, total = index.search("feature")
    assert total == 4
    assert hits[-1].name == "IFeatureManager"
    # « manager » : seul le parent des membres contient le mot ; ils suivent l'interface.
    hits, total = index.search("manager cut")
    assert (_names(hits), total) == (["IFeatureManager.FeatureCut4"], 1)


def test_search_kind_filter(index: ApiIndex) -> None:
    hits, total = index.search("feature", kind="property")
    assert (_names(hits), total) == (["IFeatureManager.FeatureCount"], 1)
    hits, total = index.search("sw", kind="enum")
    assert total == 2
    assert {h.kind for h in hits} == {"enum"}
    assert index.search("feature", kind="inconnu") == ([], 0)


def test_search_pagination_and_total(index: ApiIndex) -> None:
    everything, total = index.search("sw", limit=50)
    assert total == len(everything) == 6
    page1, total1 = index.search("sw", limit=4)
    page2, total2 = index.search("sw", limit=4, offset=4)
    assert total1 == total2 == 6
    assert _names(page1) + _names(page2) == _names(everything)
    assert index.search("sw", limit=4, offset=100) == ([], 6)
    assert index.search("sw", limit=0) == ([], 6)


def test_search_empty_and_no_match(index: ApiIndex) -> None:
    assert index.search("") == ([], 0)
    assert index.search("   ") == ([], 0)
    assert index.search("zzzintrouvable") == ([], 0)


@pytest.mark.parametrize(
    "query",
    ['"', '" OR "', "*", "extrusion*", "(extrusion", "NEAR(", "tokens:extrusion", "a AND", "-", "%", "_"],
)
def test_search_special_characters_are_neutralised(index: ApiIndex, query: str) -> None:
    hits, total = index.search(query)  # ne doit jamais lever
    assert total >= len(hits)


def test_search_special_characters_do_not_change_semantics(index: ApiIndex) -> None:
    assert _names(index.search('"extrusion"*')[0]) == ["IFeatureManager.FeatureExtrusion3"]
    # « OR » est un mot comme un autre, pas un opérateur : aucun nom ne le contient.
    assert index.search("extrusion OR cut") == ([], 0)
    # Les jokers LIKE ne sont pas interprétés.
    assert index.search("%") == ([], 0)
    assert _names(index.search("sw_end%cond")[0])[0] == "swEndConditions_e"


def test_describe_method_with_params(index: ApiIndex) -> None:
    doc = index.describe("ifeaturemanager.featureextrusion3")
    assert doc is not None
    assert doc.name == "IFeatureManager.FeatureExtrusion3"
    assert doc.kind == "method"
    assert doc.returns == "Feature"
    assert [(p.name, p.type, p.direction, p.optional) for p in doc.params] == [
        ("Sd", "bool", "in", False),
        ("Flip", "bool", "in", False),
        ("D1", "float", "in", True),
    ]
    assert doc.members == []
    assert doc.value is None
    save = index.describe("IModelDoc2.Save3")
    assert save is not None
    assert save.signature == "Save3(Options: int, inout Errors: int) -> bool"


def test_describe_enum_interface_and_member(index: ApiIndex) -> None:
    enum = index.describe("SWENDCONDITIONS_E")
    assert enum is not None
    assert enum.kind == "enum"
    assert enum.library == "SwConst"
    assert enum.members == ["swEndCondBlind", "swEndCondThroughAll"]
    assert enum.members_total == 2
    assert enum.signature == "enum swEndConditions_e { swEndCondBlind = 0, swEndCondThroughAll = 1 }"

    member = index.describe("swEndConditions_e.swEndCondThroughAll")
    assert member is not None
    assert (member.kind, member.value) == ("enum_member", 1)

    iface = index.describe("IFeatureManager")
    assert iface is not None
    assert iface.kind == "interface"
    assert iface.members == ["FeatureCount", "FeatureCut4", "FeatureExtrusion3"]
    assert iface.members_total == 3


def test_describe_short_name_and_unknown(index: ApiIndex) -> None:
    doc = index.describe("featureextrusion3")
    assert doc is not None
    assert doc.name == "IFeatureManager.FeatureExtrusion3"
    blind = index.describe("swEndCondBlind")
    assert blind is not None
    assert blind.value == 0
    # Ambigu : le premier par ordre alphabétique du nom complet.
    title = index.describe("GetTitle")
    assert title is not None
    assert title.name == "IModelDoc2.GetTitle"
    assert index.describe("NExistePas") is None
    assert index.describe("") is None


def test_members_are_capped(tmp_path: Path) -> None:
    db = tmp_path / "api.sqlite"
    with IndexWriter(db) as writer:
        writer.add_enum("SwConst", "swBig_e", {f"swBig{i:03d}": i for i in range(250)})
    doc = ApiIndex(db).describe("swBig_e")
    assert doc is not None
    assert len(doc.members) == 200
    assert doc.members[0] == "swBig000"
    assert doc.members_total == 250


def test_retval_param_becomes_return_type(tmp_path: Path) -> None:
    db = tmp_path / "api.sqlite"
    with IndexWriter(db) as writer:
        writer.add_member(
            LIB,
            "IBody2",
            "GetFaceCount",
            "method",
            [ApiParam(name="Count", type="int", direction="retval")],
            None,
        )
    doc = ApiIndex(db).describe("GetFaceCount")
    assert doc is not None
    assert doc.signature == "GetFaceCount() -> int"
    assert (doc.params, doc.returns) == ([], "int")


def test_missing_index(tmp_path: Path) -> None:
    idx = ApiIndex(tmp_path / "absent.sqlite")
    assert idx.exists is False
    assert idx.meta() == {}
    assert idx.search("extrusion") == ([], 0)
    assert idx.describe("IFeatureManager") is None


def test_corrupt_index_does_not_raise(tmp_path: Path) -> None:
    db = tmp_path / "api.sqlite"
    db.write_bytes(b"ceci n'est pas une base SQLite" * 100)
    idx = ApiIndex(db)
    assert idx.exists is True
    assert idx.meta() == {}
    assert idx.search("extrusion") == ([], 0)
    assert idx.describe("x") is None


def test_meta_and_duplicates(tmp_path: Path) -> None:
    db = tmp_path / "sub" / "api.sqlite"  # le dossier parent est créé
    with IndexWriter(db) as writer:
        _populate(writer)
        writer.add_interface(LIB, "IFeatureManager")  # doublon ignoré
        writer.set_meta("sw_version", "2025")
        assert writer.counts == {"interfaces": 3, "members": 6, "enums": 2, "enum_members": 4}
    meta = ApiIndex(db).meta()
    assert meta["sw_version"] == "2025"
    assert meta["libraries"] == "SldWorks, SwConst"
    assert '"members": 6' in meta["counts"]
    assert "built_at" in meta


def test_atomic_rewrite(tmp_path: Path) -> None:
    db = tmp_path / "api.sqlite"
    with IndexWriter(db) as writer:
        _populate(writer)
    idx = ApiIndex(db)

    # Tant que le nouvel index n'est pas fermé, l'ancien reste servi tel quel.
    writer = IndexWriter(db)
    writer.add_interface(LIB, "INouvelle")
    assert idx.describe("IFeatureManager") is not None
    assert idx.describe("INouvelle") is None
    writer.close()
    writer.close()  # idempotent
    assert idx.describe("INouvelle") is not None
    assert idx.describe("IFeatureManager") is None

    # Une écriture qui échoue ne remplace rien et ne laisse pas de fichier temporaire.
    with pytest.raises(ValueError, match="panne"), IndexWriter(db) as failing:
        failing.add_interface(LIB, "IPerdue")
        raise ValueError("panne")
    assert idx.describe("INouvelle") is not None
    assert idx.describe("IPerdue") is None
    assert [p.name for p in tmp_path.iterdir()] == ["api.sqlite"]


def test_index_is_opened_read_only_and_usable_from_threads(index: ApiIndex) -> None:
    with index._connect() as conn:
        assert conn is not None
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM entries")

    results: list[int] = []

    def worker() -> None:
        for _ in range(20):
            results.append(index.search("feature")[1])

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [4] * 80


# --------------------------------------------------------------------------------------
# indexer
# --------------------------------------------------------------------------------------


def test_find_typelibs_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    assert find_typelibs() == []


def test_build_index_off_windows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    db = tmp_path / "api.sqlite"
    with pytest.raises(RuntimeError, match="Windows"):
        build_index(db)
    with pytest.raises(RuntimeError, match="Windows"):
        build_index(db, [tmp_path / "sldworks.tlb"])
    assert not db.exists()


def test_build_index_without_pywin32(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "pythoncom", None)  # « import pythoncom » lève ImportError
    with pytest.raises(RuntimeError, match="pywin32"):
        build_index(tmp_path / "api.sqlite", [tmp_path / "sldworks.tlb"])


# Faux objets imitant la forme SUPPOSÉE des objets pywin32 (cf. commentaires UNVERIFIED de
# indexer.py). Ces tests exercent la logique de l'indexeur, pas la réalité de pywin32.


class _FakeTypeInfo:
    def __init__(
        self,
        name: str,
        typekind: int,
        funcs: list[tuple[SimpleNamespace, list[str]]] | None = None,
        consts: dict[str, int] | None = None,
        refs: dict[int, _FakeTypeInfo] | None = None,
    ) -> None:
        self.name = name
        self.typekind = typekind
        self.funcs = funcs or []
        self.consts = list((consts or {}).items())
        self.refs = refs or {}

    def GetTypeAttr(self) -> SimpleNamespace:
        return SimpleNamespace(typekind=self.typekind, cFuncs=len(self.funcs), cVars=len(self.consts))

    def GetFuncDesc(self, i: int) -> SimpleNamespace:
        return self.funcs[i][0]

    def GetVarDesc(self, i: int) -> SimpleNamespace:
        return SimpleNamespace(memid=1000 + i, value=self.consts[i][1])

    def GetNames(self, memid: int) -> tuple[str, ...]:
        if memid >= 1000:
            return (self.consts[memid - 1000][0],)
        return tuple(next(names for fdesc, names in self.funcs if fdesc.memid == memid))

    def GetRefTypeInfo(self, href: int) -> _FakeTypeInfo:
        return self.refs[href]

    def GetDocumentation(self, i: int) -> tuple[str, None, int, None]:
        return (self.name, None, 0, None)


class _FakeTypeLib:
    def __init__(self, name: str, infos: list[_FakeTypeInfo]) -> None:
        self.name = name
        self.infos = infos

    def GetTypeInfoCount(self) -> int:
        return len(self.infos)

    def GetTypeInfo(self, i: int) -> _FakeTypeInfo:
        return self.infos[i]

    def GetDocumentation(self, i: int) -> tuple[str, None, int, None]:
        return (self.name if i == -1 else self.infos[i].name, None, 0, None)


def _func(memid: int, args: list[tuple[Any, int, None]], ret: Any, invkind: int = 1, flags: int = 0) -> Any:
    return SimpleNamespace(
        memid=memid, args=tuple(args), rettype=(ret, 0, None), invkind=invkind, wFuncFlags=flags
    )


def _fake_typelib() -> _FakeTypeLib:
    feature = _FakeTypeInfo("IFeature", 4)
    manager = _FakeTypeInfo(
        "IFeatureManager",
        4,  # TKIND_DISPATCH
        funcs=[
            (_func(1, [], 25, flags=0x1), ["QueryInterface"]),  # restreint : ignoré
            (
                _func(
                    2,
                    [(11, 0x1, None), (5, 0x11, None), ((26, 3), 0x3, None), ((26, (27, 8)), 0x2, None)],
                    (26, (29, 7)),
                ),
                ["FeatureExtrusion3", "Sd", "D1", "Errors", "Names"],
            ),
            (_func(3, [], 8, invkind=2), ["Name"]),
            (_func(3, [(8, 0x1, None)], 24, invkind=4), ["Name"]),
            (_func(4, [(3, 0x1, None)], 24, invkind=4), ["Visible"]),  # propriété en écriture seule
        ],
        refs={7: feature},
    )
    vtable = _FakeTypeInfo(
        "IRaw",
        3,  # TKIND_INTERFACE : HRESULT + paramètre [out, retval]
        funcs=[(_func(1, [(3, 0x1, None), ((26, 5), 0xA, None)], 25), ["GetLength", "Index", "Value"])],
    )
    enum = _FakeTypeInfo("swEndConditions_e", 0, consts={"swEndCondBlind": 0, "swEndCondThroughAll": 1})
    coclass = _FakeTypeInfo("SldWorks", 5)
    return _FakeTypeLib("SldWorks", [manager, vtable, enum, coclass])


def test_index_typelib_with_fake_com_objects(tmp_path: Path) -> None:
    db = tmp_path / "api.sqlite"
    with IndexWriter(db) as writer:
        assert indexer._index_typelib(_fake_typelib(), writer, "repli") == "SldWorks"
        assert writer.counts == {"interfaces": 2, "members": 4, "enums": 1, "enum_members": 2}
    idx = ApiIndex(db)

    extrusion = idx.describe("FeatureExtrusion3")
    assert extrusion is not None
    assert extrusion.signature == (
        "FeatureExtrusion3(Sd: bool, D1?: float, inout Errors: int, out Names: list[str]) -> IFeature"
    )
    name = idx.describe("IFeatureManager.Name")
    assert name is not None
    assert (name.kind, name.signature) == ("property", "Name: str")
    visible = idx.describe("Visible")
    assert visible is not None
    assert visible.signature == "Visible: int"
    raw = idx.describe("IRaw.GetLength")
    assert raw is not None
    assert raw.signature == "GetLength(Index: int) -> float"
    assert idx.describe("QueryInterface") is None
    assert idx.describe("SldWorks") is None  # coclasse non indexée
    blind = idx.describe("swEndCondBlind")
    assert blind is not None
    assert blind.value == 0


def test_build_index_with_fake_pythoncom(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = types.ModuleType("pythoncom")

    def load_type_lib(path: str) -> _FakeTypeLib:
        if path.endswith("cassee.tlb"):
            raise OSError("illisible")
        return _fake_typelib()

    fake.LoadTypeLib = load_type_lib  # type: ignore[attr-defined]
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "pythoncom", fake)
    db = tmp_path / "api.sqlite"

    with pytest.raises(RuntimeError, match=r"\.tlb"):
        build_index(db, [])
    with pytest.raises(RuntimeError, match="Aucune des bibliothèques"):
        build_index(db, [tmp_path / "cassee.tlb"])
    assert not db.exists()
    assert list(tmp_path.iterdir()) == []

    stats = build_index(db, [tmp_path / "cassee.tlb", tmp_path / "sldworks.tlb"])
    assert stats == {"interfaces": 2, "members": 4, "enums": 1}
    assert ApiIndex(db).meta()["sources"] == "sldworks.tlb"
