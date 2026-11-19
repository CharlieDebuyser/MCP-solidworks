"""Constantes et énumérations SolidWorks, lues par nom (spec §10 « Constantes et énumérations »).

Aucun entier n'est recopié ici : les valeurs viennent des classes générées par pywin32 depuis la
bibliothèque de types `swconst.tlb` de l'installation (`win32com.client.constants`). Le reste du
code écrit `C.swDocPART`, jamais `1`.

Le chargement est paresseux : `load()` est appelé dans le thread COM, à la connexion. Le module
s'importe sans pywin32 (macOS, Linux). Les noms d'énumérateurs ont été relus dans l'API Help
SOLIDWORKS 2026 (espace de noms `SolidWorks.Interop.swconst`), consultée le 2026-11-19.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from typing import Any

from ..envelope import ErrorCode, SwError

# Identifiant de la bibliothèque « SOLIDWORKS Constant type library » (swconst.tlb).
# UNVERIFIED: LIBID de swconst.tlb — aide non consultée : de mémoire — n'est utilisé qu'en repli,
# quand swconst.tlb n'est pas trouvé à côté de l'exécutable.
SWCONST_LIBID = "{4687F359-55D0-4CD3-B6CF-2EB42C11F989}"
SWCONST_FILE = "swconst.tlb"

# Tous les énumérateurs utilisés par com_backend.py ; `load()` vérifie qu'ils existent tous.
REQUIRED: tuple[str, ...] = (
    # swDocumentTypes_e
    "swDocPART",
    "swDocASSEMBLY",
    "swDocDRAWING",
    # swUserPreferenceStringValue_e
    "swDefaultTemplatePart",
    "swDefaultTemplateAssembly",
    "swDefaultTemplateDrawing",
    # swUserPreferenceToggle_e
    "swSketchInference",
    "swSketchAutomaticRelations",
    "swInputDimValOnCreate",
    # swDwgPaperSizes_e
    "swDwgPaperA3size",
    # swOpenDocOptions_e
    "swOpenDocOptions_Silent",
    "swOpenDocOptions_ReadOnly",
    # swFileLoadError_e (les autres membres sont retrouvés par préfixe pour les messages)
    "swFileNotFoundError",
    "swGenericError",
    # swSaveAsVersion_e / swSaveAsOptions_e
    "swSaveAsCurrentVersion",
    "swSaveAsOptions_Silent",
    "swSaveAsOptions_Copy",
    # swRebuildOnActivation_e
    "swDontRebuildActiveDoc",
    # swSelectOption_e
    "swSelectOptionDefault",
    # swBodyType_e
    "swSolidBody",
    # swSketchSegments_e
    "swSketchLINE",
    "swSketchARC",
    # swConstrainedStatus_e
    "swUnderConstrained",
    "swFullyConstrained",
    "swOverConstrained",
    # swSketchRelationFilterType_e
    "swAll",
    # swEndConditions_e / swStartConditions_e
    "swEndCondBlind",
    "swEndCondThroughAll",
    "swEndCondMidPlane",
    "swEndCondUpToNext",
    "swStartSketchPlane",
    # swThinWallType_e
    "swThinWallOneDirection",
    # swFeatureNameID_e, swSimpleFilletType_e, swFeatureFillet*_e, swFilletOverFlowType_e
    "swFmFillet",
    "swConstRadiusFillet",
    "swFeatureFilletCircular",
    "swFeatureFilletUniformRadius",
    "swFeatureFilletPropagate",
    "swFeatureFilletType_Simple",
    "swFilletOverFlowType_Default",
    # swChamferType_e / swFeatureChamferOption_e
    "swChamferAngleDistance",
    "swFeatureChamferTangentPropagation",
    # swFeatureScope_e
    "swFeatureScope_AllBodies",
    # swInConfigurationOpts_e / swSetValueReturnStatus_e
    "swThisConfiguration",
    "swSetValue_Successful",
    # swDimensionParamType_e
    "swDimensionParamTypeDoubleAngular",
    # swDeleteSelectionOptions_e
    "swDelete_Children",
    "swDelete_Absorbed",
    # swCustomInfoType_e / swCustomPropertyAddOption_e / swCustomInfoGetResult_e
    "swCustomInfoText",
    "swCustomPropertyReplaceValue",
    "swCustomInfoGetResult_NotPresent",
    # swPersistReferencedObjectStates_e
    "swPersistReferencedObject_Ok",
    # swStandardViews_e
    "swFrontView",
    "swBackView",
    "swLeftView",
    "swRightView",
    "swTopView",
    "swBottomView",
    "swIsometricView",
    # swComponentSuppressionState_e
    "swComponentLightweight",
    "swComponentFullyLightweight",
    # swLengthUnit_e
    "swMM",
    "swCM",
    "swMETER",
    "swINCHES",
    "swFEET",
)

# --- identifiants textuels documentés par l'API (ce ne sont pas des énumérations) ---------------

# IFeature::GetTypeName2 (API Help 2026). « OriginProfileFeature » n'y figure pas : de mémoire.
FEAT_REF_PLANE = "RefPlane"
FEAT_REF_AXIS = "RefAxis"
FEAT_SKETCH = "ProfileFeature"
FEAT_SKETCH_3D = "3DProfileFeature"
FEAT_ORIGIN = "OriginProfileFeature"

# Nom de type SolidWorks → type publié par le serveur (mêmes libellés que le backend simulé).
FEATURE_TYPE_NAMES: dict[str, str] = {
    FEAT_SKETCH: "Sketch",
    "Extrusion": "Extrude",
    "Boss": "Extrude",
    "BaseBody": "Extrude",
    "Cut": "CutExtrude",
    "ICE": "CutExtrude",
    "Revolution": "Revolve",
    "RevCut": "CutRevolve",
    "Fillet": "Fillet",
    "Chamfer": "Chamfer",
    "Shell": "Shell",
    "LPattern": "LPattern",
    "CirPattern": "CPattern",
    "MirrorPattern": "Mirror",
}

# Dossiers et objets système de l'arbre, sans intérêt pour le LLM (noms de type, pas de nom localisé).
TREE_NOISE: frozenset[str] = frozenset(
    {
        "CommentsFolder",
        "FavoriteFolder",
        "HistoryFolder",
        "SelectionSetFolder",
        "SensorFolder",
        "DocsFolder",
        "DetailCabinet",
        "SurfaceBodyFolder",
        "SolidBodyFolder",
        "EnvFolder",
        "AmbientLight",
        "DirectionLight",
        "InkMarkupFolder",
        "EqnFolder",
        "MaterialFolder",
        "BlockFolder",
        "MarkupCommentFolder",
        "NotesAreaFtrFolder",
        "LiveSectionFolder",
        "SwiftAnnotations",
        "SwiftSchemes",
        "SwiftFeatures",
        "ProfileFtrFolder",
        "RefAxisFtrFolder",
        "RefPlaneFtrFolder",
    }
)

# IModelDoc2::SketchAddConstraints (API Help 2026, colonne « SOLIDWORKS 2006 and later »).
RELATION_IDS: dict[str, str] = {
    "horizontal": "sgHORIZONTAL2D",
    "vertical": "sgVERTICAL2D",
    "horizontal_points": "sgHORIZONTALPOINTS2D",
    "vertical_points": "sgVERTICALPOINTS2D",
    "coincident": "sgCOINCIDENT",
    "merge": "sgMERGEPOINTS",
    "tangent": "sgTANGENT",
    "equal": "sgSAMELENGTH",
    "parallel": "sgPARALLEL",
    "perpendicular": "sgPERPENDICULAR",
    "concentric": "sgCONCENTRIC",
    "symmetric": "sgSYMMETRIC",
    "fix": "sgFIXED",
    "midpoint": "sgATMIDDLE",
}

# swConstraintType_e (suffixe du nom de l'énumérateur) → type de relation publié par le serveur.
CONSTRAINT_SUFFIXES: dict[str, str] = {
    "HORIZONTAL": "horizontal",
    "HORIZPOINTS": "horizontal",
    "VERTICAL": "vertical",
    "VERTPOINTS": "vertical",
    "COINCIDENT": "coincident",
    "MERGEPOINTS": "coincident",
    "TANGENT": "tangent",
    "SAMELENGTH": "equal",
    "PARALLEL": "parallel",
    "PERPENDICULAR": "perpendicular",
    "CONCENTRIC": "concentric",
    "SYMMETRIC": "symmetric",
    "FIXED": "fix",
    "ATMIDDLE": "midpoint",
}
CONSTRAINT_PREFIX = "swConstraintType_"

# IModelDocExtension::SelectByID2, argument Type (API Help 2026, remarques).
SEL_EXT_SKETCH_POINT = "EXTSKETCHPOINT"

# Extensions de fichier.
TEMPLATE_PREFS: dict[str, str] = {
    "part": "swDefaultTemplatePart",
    "assembly": "swDefaultTemplateAssembly",
    "drawing": "swDefaultTemplateDrawing",
}
DOC_EXTENSIONS: dict[str, str] = {"part": ".sldprt", "assembly": ".sldasm", "drawing": ".slddrw"}
EXPORT_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "step": (".step", ".stp"),
    "stl": (".stl",),
    "iges": (".igs", ".iges"),
    "pdf": (".pdf",),
    "dxf": (".dxf",),
    "png": (".png",),
}

# Énumérateurs sans préfixe commun, listés pour traduire les masques d'erreur en texte.
FILE_LOAD_ERRORS: tuple[str, ...] = (
    "swGenericError",
    "swFileNotFoundError",
    "swInvalidFileTypeError",
    "swFutureVersion",
    "swFileWithSameTitleAlreadyOpen",
    "swLiquidMachineDoc",
    "swLowResourcesError",
    "swNoDisplayData",
    "swAddinInteruptError",
    "swFileRequiresRepairError",
    "swFileCriticalDataRepairError",
    "swApplicationBusy",
    "swConnectedIsOffline",
)
FILE_LOAD_WARNING_PREFIX = "swFileLoadWarning_"
FILE_SAVE_ERRORS: tuple[str, ...] = (
    "swGenericSaveError",
    "swReadOnlySaveError",
    "swFileNameEmpty",
    "swFileNameContainsAtSign",
    "swFileLockError",
    "swFileSaveFormatNotAvailable",
    "swFileSaveAsDoNotOverwrite",
    "swFileSaveAsInvalidFileExtension",
    "swFileSaveAsNoSelection",
    "swFileSaveAsBadEDrawingsVersion",
    "swFileSaveAsNameExceedsMaxPathLength",
    "swFileSaveAsNotSupported",
    "swFileSaveRequiresSavingReferences",
)
FILE_SAVE_WARNING_PREFIX = "swFileSaveWarning_"
FEATURE_ERROR_PREFIX = "swFeatureError"

# --- chargement ---------------------------------------------------------------------------------

_installed: dict[str, int] | None = None  # valeurs injectées (tests, autre backend)
_source: Any = None  # win32com.client.constants une fois chargé


class _Constants:
    """Accès par nom : `C.swDocPART`. Lève `INTERNAL` si la constante n'est pas chargée."""

    def __getattr__(self, name: str) -> int:
        if name.startswith("_"):
            raise AttributeError(name)
        value = lookup(name)
        if value is None:
            raise SwError(
                ErrorCode.INTERNAL,
                f"Constante SolidWorks indisponible : {name}.",
                hint="Les énumérations se chargent à la connexion (sw_connect) ; lancer `swmcp doctor` pour "
                "vérifier la génération des classes pywin32.",
            )
        return value


C = _Constants()


def lookup(name: str) -> int | None:
    if _installed is not None and name in _installed:
        return _installed[name]
    if _source is not None:
        try:
            return int(getattr(_source, name))
        except (AttributeError, TypeError, ValueError):
            return None
    return None


def _tables() -> list[Mapping[str, Any]]:
    tables: list[Mapping[str, Any]] = []
    if _installed is not None:
        tables.append(_installed)
    if _source is not None:
        tables.extend(getattr(_source, "__dicts__", []))
    return tables


def names_with_prefix(prefix: str) -> dict[str, int]:
    """Tous les énumérateurs chargés dont le nom commence par `prefix`."""
    out: dict[str, int] = {}
    for table in _tables():
        for key, value in table.items():
            if isinstance(key, str) and key.startswith(prefix) and isinstance(value, int):
                out.setdefault(key, value)
    return out


def name_of(prefix: str, value: int) -> str | None:
    """Nom de l'énumérateur de valeur `value` parmi ceux de préfixe `prefix` (textes de diagnostic)."""
    for key, candidate in sorted(names_with_prefix(prefix).items()):
        if candidate == value:
            return key
    return None


def flags_of(mask: int, prefix: str | None = None, names: Iterable[str] = ()) -> list[str]:
    """Décompose un masque de bits en noms d'énumérateurs (erreurs de OpenDoc6, de SaveAs…).

    Les candidats sont les énumérateurs de préfixe `prefix` et ceux nommés dans `names`.
    """
    if not mask:
        return []
    found: dict[str, int] = names_with_prefix(prefix) if prefix else {}
    for name in names:
        value = lookup(name)
        if value is not None:
            found.setdefault(name, value)
    hits = sorted(name for name, bit in found.items() if bit > 0 and bit & (bit - 1) == 0 and bit & mask)
    return hits or [f"code {mask:#x}"]


def missing() -> list[str]:
    return [name for name in REQUIRED if lookup(name) is None]


def is_loaded() -> bool:
    return (_installed is not None or _source is not None) and not missing()


def install(values: Mapping[str, int]) -> None:
    """Injecte des valeurs sans pywin32 (tests unitaires, backend non COM)."""
    global _installed
    _installed = dict(values)


def reset() -> None:
    global _installed, _source
    _installed = None
    _source = None


def _load_from_file(app: Any) -> bool:
    import pythoncom
    from win32com.client import gencache

    # UNVERIFIED: ISldWorks.GetExecutablePath — aide 2026 — renvoie le dossier de sldworks.exe, où se
    # trouve swconst.tlb (emplacement du fichier : de mémoire).
    folder = str(app.GetExecutablePath())
    for candidate in (
        os.path.join(folder, SWCONST_FILE),
        os.path.join(os.path.dirname(folder), SWCONST_FILE),
    ):
        if os.path.isfile(candidate):
            typelib = pythoncom.LoadTypeLib(candidate)
            gencache.EnsureModuleForTypelibInterface(typelib, bForDemand=False)
            return True
    return False


def _load_from_registry() -> bool:
    import winreg

    from win32com.client import gencache

    versions: list[tuple[int, int]] = []
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, f"TypeLib\\{SWCONST_LIBID}") as key:  # type: ignore[attr-defined,unused-ignore]
            index = 0
            while True:
                try:
                    text = winreg.EnumKey(key, index)  # type: ignore[attr-defined,unused-ignore]
                except OSError:
                    break
                index += 1
                major, _, minor = text.partition(".")
                try:
                    versions.append((int(major, 16), int(minor or "0", 16)))
                except ValueError:
                    continue
    except OSError:
        return False
    for major_v, minor_v in sorted(versions, reverse=True):
        if gencache.EnsureModule(SWCONST_LIBID, 0, major_v, minor_v) is not None:
            return True
    return False


def load(app: Any = None) -> None:
    """Charge les énumérations depuis la bibliothèque de types de l'installation (thread COM).

    `app` : objet `ISldWorks` connecté, pour localiser `swconst.tlb` ; sans lui, ou si le fichier
    est introuvable, la bibliothèque est cherchée dans le registre.
    """
    global _source
    if is_loaded():
        return
    try:
        from win32com.client import constants as generated
    except ImportError as exc:
        raise SwError(
            ErrorCode.INTERNAL,
            "pywin32 n'est pas installé : le backend COM exige Windows et pywin32.",
            hint="Installer pywin32 (`pip install pywin32`) puis lancer `swmcp doctor`.",
        ) from exc
    _source = generated
    if missing() and app is not None:
        _load_from_file(app)
    if missing():
        _load_from_registry()
    absent = missing()
    if absent:
        raise SwError(
            ErrorCode.INTERNAL,
            f"{len(absent)} constante(s) SolidWorks introuvable(s) dans swconst.tlb : "
            + ", ".join(absent[:8]),
            hint="La bibliothèque de constantes n'a pas pu être chargée ou ne correspond pas à cette version : "
            "lancer `swmcp doctor`.",
            details={"missing": absent},
        )
