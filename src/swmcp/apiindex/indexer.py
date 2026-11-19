"""Construction de l'index de l'API à partir des bibliothèques de types COM de SolidWorks (spec §7).

ATTENTION : la partie COM de ce module n'a jamais été exécutée contre un vrai SolidWorks ni même
contre pywin32 (développement sur macOS). Chaque appel COM / pythoncom / winreg porte un commentaire
`# UNVERIFIED:` décrivant l'hypothèse faite. Les constantes numériques (VT_*, TKIND_*, PARAMFLAG_*,
INVOKE_*) sont celles du SDK Windows (oaidl.h / wtypes.h), recopiées ici pour ne pas dépendre de
leur exposition par pythoncom.

Seuls des noms et des types sont lus (GetNames, descriptions de fonctions et de variables) : les
chaînes d'aide (docstrings) des bibliothèques de types ne sont jamais copiées dans l'index.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from swmcp.apiindex.store import ApiParam, IndexWriter

logger = logging.getLogger(__name__)

# Bibliothèques de types cherchées dans le dossier d'installation.
# UNVERIFIED: noms de fichiers supposés d'après les installations SolidWorks usuelles.
TYPELIB_NAMES: tuple[str, ...] = ("sldworks.tlb", "swconst.tlb", "swcommands.tlb", "swpublished.tlb")

# -- Constantes du SDK Windows ----------------------------------------------------------
_TKIND_ENUM = 0
_TKIND_INTERFACE = 3
_TKIND_DISPATCH = 4

_INVOKE_FUNC = 1
_INVOKE_PROPERTYGET = 2

_FUNCFLAG_FRESTRICTED = 0x1

_PARAMFLAG_FIN = 0x1
_PARAMFLAG_FOUT = 0x2
_PARAMFLAG_FLCID = 0x4
_PARAMFLAG_FRETVAL = 0x8
_PARAMFLAG_FOPT = 0x10
_PARAMFLAG_FHASDEFAULT = 0x20

_VT_VOID = 24
_VT_HRESULT = 25
_VT_PTR = 26
_VT_SAFEARRAY = 27
_VT_CARRAY = 28
_VT_USERDEFINED = 29
_VT_TYPEMASK = 0x0FFF
_VT_ARRAY = 0x2000

_VT_NAMES: dict[int, str] = {
    0: "None",  # VT_EMPTY
    1: "None",  # VT_NULL
    2: "int",  # VT_I2
    3: "int",  # VT_I4
    4: "float",  # VT_R4
    5: "float",  # VT_R8
    6: "currency",  # VT_CY
    7: "date",  # VT_DATE
    8: "str",  # VT_BSTR
    9: "object",  # VT_DISPATCH
    10: "int",  # VT_ERROR
    11: "bool",  # VT_BOOL
    12: "variant",  # VT_VARIANT
    13: "object",  # VT_UNKNOWN
    14: "decimal",  # VT_DECIMAL
    16: "int",  # VT_I1
    17: "int",  # VT_UI1
    18: "int",  # VT_UI2
    19: "int",  # VT_UI4
    20: "int",  # VT_I8
    21: "int",  # VT_UI8
    22: "int",  # VT_INT
    23: "int",  # VT_UINT
    _VT_VOID: "None",
    _VT_HRESULT: "HRESULT",
    30: "str",  # VT_LPSTR
    31: "str",  # VT_LPWSTR
    36: "record",  # VT_RECORD
    37: "int",  # VT_INT_PTR
    38: "int",  # VT_UINT_PTR
}

_NOT_WINDOWS = (
    "La construction de l'index de l'API n'est possible que sous Windows, sur un poste où SolidWorks "
    "est installé (plateforme actuelle : {platform}). Construisez l'index sur le poste SolidWorks avec "
    "`swmcp index-api`."
)
_NO_PYWIN32 = (
    "pywin32 est introuvable : impossible de lire les bibliothèques de types COM. "
    "Installez-le dans l'environnement du serveur (`pip install pywin32`) puis relancez `swmcp index-api`."
)
_NO_TYPELIB = (
    "Aucune bibliothèque de types SolidWorks (.tlb) trouvée. Vérifiez que SolidWorks est installé, ou "
    "indiquez explicitement les chemins des fichiers sldworks.tlb et swconst.tlb."
)


# --------------------------------------------------------------------------------------
# Localisation des .tlb
# --------------------------------------------------------------------------------------


def find_typelibs() -> list[Path]:
    """Localise les .tlb de SolidWorks. Retourne [] hors Windows ou si rien n'est trouvé."""
    if sys.platform != "win32":
        return []
    found: dict[str, Path] = {}
    for folder in _install_dirs():
        try:
            by_name = {p.name.lower(): p for p in folder.iterdir() if p.suffix.lower() == ".tlb"}
        except OSError:
            continue
        for wanted in TYPELIB_NAMES:
            if wanted in by_name and wanted not in found:
                found[wanted] = by_name[wanted]
    return [found[name] for name in TYPELIB_NAMES if name in found]


def _install_dirs() -> list[Path]:
    """Dossiers d'installation candidats, du plus fiable au moins fiable (Windows seulement)."""
    if sys.platform != "win32":
        return []
    import winreg

    candidates: list[Path] = []

    def read_value(root: int, key_path: str, value_name: str) -> str | None:
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                # UNVERIFIED: OpenKey(root, sous-clé, 0, KEY_READ | vue 32/64 bits) puis
                # QueryValueEx(clé, nom) -> (valeur, type).
                with winreg.OpenKey(root, key_path, 0, winreg.KEY_READ | view) as key:
                    value, _ = winreg.QueryValueEx(key, value_name)
            except OSError:
                continue
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    # 1) HKLM\SOFTWARE\SolidWorks\SOLIDWORKS <année>\Setup -> "SolidWorks Folder"
    # UNVERIFIED: structure de registre supposée ; la valeur « SolidWorks Folder » contiendrait le
    # dossier d'installation. Les versions les plus récentes sont essayées en premier.
    base = r"SOFTWARE\SolidWorks"
    versions: list[str] = []
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, base, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY
        ) as key:
            index = 0
            while True:
                try:
                    # UNVERIFIED: EnumKey lève OSError quand il n'y a plus de sous-clé.
                    versions.append(winreg.EnumKey(key, index))
                except OSError:
                    break
                index += 1
    except OSError:
        logger.debug("Clé de registre HKLM\\%s absente.", base)
    for version in sorted((v for v in versions if v.upper().startswith("SOLIDWORKS ")), reverse=True):
        folder = read_value(winreg.HKEY_LOCAL_MACHINE, rf"{base}\{version}\Setup", "SolidWorks Folder")
        if folder:
            candidates.append(Path(folder))

    # 2) Serveur COM enregistré : HKCR\SldWorks.Application\CLSID -> HKCR\CLSID\{...}\LocalServer32
    # UNVERIFIED: la valeur par défaut de LocalServer32 serait le chemin de SLDWORKS.exe, parfois
    # entre guillemets et suivi d'arguments.
    clsid = read_value(winreg.HKEY_CLASSES_ROOT, r"SldWorks.Application\CLSID", "")
    if clsid:
        server = read_value(winreg.HKEY_CLASSES_ROOT, rf"CLSID\{clsid}\LocalServer32", "")
        if server:
            exe = server.split('"')[1] if server.startswith('"') else server.split(" /")[0]
            candidates.append(Path(exe).parent)

    # 3) Emplacements par défaut.
    # UNVERIFIED: dossier d'installation par défaut supposé.
    for env in ("ProgramFiles", "ProgramW6432"):
        root_dir = os.environ.get(env)
        if root_dir:
            candidates.append(Path(root_dir) / "SOLIDWORKS Corp" / "SOLIDWORKS")

    unique: dict[str, Path] = {}
    for path in candidates:
        unique.setdefault(str(path).lower(), path)
    return [p for p in unique.values() if p.is_dir()]


# --------------------------------------------------------------------------------------
# Lecture d'une bibliothèque de types
# --------------------------------------------------------------------------------------


@dataclass
class _Member:
    kind: Literal["method", "property"]
    params: list[ApiParam]
    returns: str | None


def _type_name(tdesc: Any, info: Any) -> str:
    """Nom lisible d'un TYPEDESC tel que pywin32 le représente.

    UNVERIFIED: pywin32 représenterait un TYPEDESC soit par un entier VT_*, soit par un tuple
    (VT_PTR | VT_SAFEARRAY | VT_CARRAY, sous-type) ou (VT_USERDEFINED, hreftype) — c'est la forme
    que traite `win32com.client.build._ResolveType`.
    """
    if isinstance(tdesc, tuple) and len(tdesc) == 2 and isinstance(tdesc[0], int):
        vt, sub = tdesc
        if vt == _VT_PTR:
            return _type_name(sub, info)
        if vt in (_VT_SAFEARRAY, _VT_CARRAY):
            return f"list[{_type_name(sub, info)}]"
        if vt == _VT_USERDEFINED:
            try:
                # UNVERIFIED: ITypeInfo.GetRefTypeInfo(hreftype) -> ITypeInfo du type référencé ;
                # GetDocumentation(-1) -> (nom, docstring, contexte d'aide, fichier d'aide).
                # Seul le nom [0] est lu.
                return str(info.GetRefTypeInfo(sub).GetDocumentation(-1)[0])
            except Exception:  # pythoncom.com_error : type référencé d'une bibliothèque absente
                return "object"
        return _type_name(vt, info)
    if isinstance(tdesc, int):
        name = _VT_NAMES.get(tdesc & _VT_TYPEMASK, f"VT_{tdesc & _VT_TYPEMASK}")
        return f"list[{name}]" if tdesc & _VT_ARRAY else name
    return "variant"


def _elem(elemdesc: Any) -> tuple[Any, int]:
    """(typedesc, drapeaux) d'un ELEMDESC.

    UNVERIFIED: pywin32 représenterait un ELEMDESC par un tuple (typedesc, drapeaux PARAMFLAG_*,
    valeur par défaut) ; un typedesc nu est toléré.
    """
    if isinstance(elemdesc, tuple) and len(elemdesc) == 3:
        flags = elemdesc[1]
        return elemdesc[0], flags if isinstance(flags, int) else 0
    return elemdesc, 0


def _read_function(info: Any, fdesc: Any) -> tuple[str, _Member, bool] | None:
    """(nom, membre, est_un_getter_ou_une_méthode) pour un FUNCDESC ; None s'il faut l'ignorer."""
    # UNVERIFIED: FUNCDESC expose les attributs memid, args (tuple d'ELEMDESC), rettype (ELEMDESC),
    # invkind et wFuncFlags.
    if fdesc.wFuncFlags & _FUNCFLAG_FRESTRICTED:
        return None  # QueryInterface, AddRef, Invoke… (membres d'IUnknown / IDispatch)
    # UNVERIFIED: ITypeInfo.GetNames(memid) -> (nom du membre, nom du 1er paramètre, …) ; la valeur
    # affectée par un propput n'a pas forcément de nom.
    names = list(info.GetNames(fdesc.memid))
    if not names:
        return None
    name = str(names[0])
    ret_tdesc, _ = _elem(fdesc.rettype)
    returns: str | None = _type_name(ret_tdesc, info)
    if returns in ("None", "HRESULT"):
        returns = None

    params: list[ApiParam] = []
    for position, elemdesc in enumerate(fdesc.args):
        tdesc, flags = _elem(elemdesc)
        if flags & _PARAMFLAG_FLCID:
            continue
        type_name = _type_name(tdesc, info)
        if flags & _PARAMFLAG_FRETVAL:
            # Vue vtable (TKIND_INTERFACE) : la vraie valeur de retour est le paramètre [out, retval].
            returns = type_name
            continue
        if flags & _PARAMFLAG_FOUT:
            direction: Literal["in", "out", "inout"] = "inout" if flags & _PARAMFLAG_FIN else "out"
        else:
            direction = "in"
        param_name = str(names[position + 1]) if position + 1 < len(names) else f"arg{position + 1}"
        optional = bool(flags & (_PARAMFLAG_FOPT | _PARAMFLAG_FHASDEFAULT))
        params.append(ApiParam(name=param_name, type=type_name, direction=direction, optional=optional))

    if fdesc.invkind == _INVOKE_FUNC:
        return name, _Member("method", params, returns), True
    if fdesc.invkind == _INVOKE_PROPERTYGET:
        return name, _Member("property", params, returns), True
    # propput / propputref : le dernier argument est la valeur affectée, son type est celui de la propriété.
    value_type = params[-1].type if params else None
    return name, _Member("property", params[:-1], value_type), False


def _index_typeinfo(info: Any, name: str, library: str, writer: IndexWriter) -> None:
    # UNVERIFIED: ITypeInfo.GetTypeAttr() -> TYPEATTR avec les attributs typekind, cFuncs, cVars.
    attr = info.GetTypeAttr()
    typekind = attr.typekind

    if typekind == _TKIND_ENUM:
        values: dict[str, int] = {}
        for i in range(attr.cVars):
            # UNVERIFIED: ITypeInfo.GetVarDesc(i) -> VARDESC avec memid et, pour une constante
            # (VAR_CONST), l'attribut `value` contenant l'entier.
            vdesc = info.GetVarDesc(i)
            member_names = info.GetNames(vdesc.memid)
            if member_names and isinstance(vdesc.value, int):
                values[str(member_names[0])] = int(vdesc.value)
        writer.add_enum(library, name, values)
        return

    if typekind not in (_TKIND_DISPATCH, _TKIND_INTERFACE):
        return  # coclasses, alias, structures, modules : hors périmètre de l'index

    writer.add_interface(library, name)
    members: dict[str, _Member] = {}
    for i in range(attr.cFuncs):
        # UNVERIFIED: ITypeInfo.GetFuncDesc(i) -> FUNCDESC. Pour une interface duale vue comme
        # TKIND_DISPATCH, les membres hérités seraient inclus et le [retval] déjà porté par rettype.
        parsed = _read_function(info, info.GetFuncDesc(i))
        if parsed is None:
            continue
        member_name, member, preferred = parsed
        # Une propriété apparaît deux fois (get puis put) : la description du getter l'emporte.
        if preferred or member_name not in members:
            members[member_name] = member
    if typekind == _TKIND_DISPATCH:
        for i in range(attr.cVars):
            # UNVERIFIED: une dispinterface pure peut déclarer ses propriétés comme variables ;
            # VARDESC.elemdescVar serait alors l'ELEMDESC du type de la propriété.
            vdesc = info.GetVarDesc(i)
            var_names = info.GetNames(vdesc.memid)
            if var_names and str(var_names[0]) not in members:
                tdesc, _ = _elem(vdesc.elemdescVar)
                members[str(var_names[0])] = _Member("property", [], _type_name(tdesc, info))
    for member_name, member in members.items():
        writer.add_member(library, name, member_name, member.kind, member.params, member.returns)


def _index_typelib(tlb: Any, writer: IndexWriter, fallback_name: str) -> str:
    """Indexe une ITypeLib déjà chargée ; retourne le nom de la bibliothèque."""
    try:
        # UNVERIFIED: ITypeLib.GetDocumentation(-1) -> (nom de la bibliothèque, …), ex. « SldWorks ».
        library = str(tlb.GetDocumentation(-1)[0]) or fallback_name
    except Exception:
        library = fallback_name
    # UNVERIFIED: ITypeLib.GetTypeInfoCount() -> nombre de types ; GetTypeInfo(i) -> ITypeInfo ;
    # GetDocumentation(i) -> (nom du type, …).
    for i in range(tlb.GetTypeInfoCount()):
        try:
            type_name = str(tlb.GetDocumentation(i)[0])
            _index_typeinfo(tlb.GetTypeInfo(i), type_name, library, writer)
        except Exception as exc:  # un type illisible ne doit pas faire échouer tout l'index
            logger.warning("Type n°%d de %s ignoré : %s", i, library, exc)
    return library


def build_index(db_path: Path, tlb_paths: list[Path] | None = None) -> dict[str, int]:
    """Construit l'index à `db_path` et retourne {"interfaces": n, "members": n, "enums": n}.

    Lève RuntimeError (message en français) hors Windows, sans pywin32, ou sans .tlb exploitable.
    L'index existant n'est remplacé que si la construction réussit.
    """
    if sys.platform != "win32":
        raise RuntimeError(_NOT_WINDOWS.format(platform=sys.platform))
    try:
        import pythoncom
    except ImportError as exc:
        raise RuntimeError(_NO_PYWIN32) from exc

    paths = list(tlb_paths) if tlb_paths is not None else find_typelibs()
    if not paths:
        raise RuntimeError(_NO_TYPELIB)

    writer = IndexWriter(db_path)
    try:
        loaded: list[str] = []
        for path in paths:
            try:
                # UNVERIFIED: pythoncom.LoadTypeLib(chemin) -> ITypeLib, sans inscrire la
                # bibliothèque au registre ; lève pythoncom.com_error si le fichier est illisible.
                tlb = pythoncom.LoadTypeLib(str(path))
            except Exception as exc:
                logger.warning("Bibliothèque de types illisible (%s) : %s", path, exc)
                continue
            _index_typelib(tlb, writer, Path(path).stem)
            loaded.append(Path(path).name)
        if not loaded:
            raise RuntimeError(
                "Aucune des bibliothèques de types n'a pu être lue : "
                + ", ".join(str(p) for p in paths)
                + ". Vérifiez les chemins et l'installation de SolidWorks."
            )
        writer.set_meta("sources", ", ".join(loaded))
        counts = writer.counts
    except BaseException:
        writer.abort()
        raise
    writer.close()
    stats = {key: counts[key] for key in ("interfaces", "members", "enums")}
    logger.info("Index de l'API construit (%s) : %s", db_path, stats)
    return stats
