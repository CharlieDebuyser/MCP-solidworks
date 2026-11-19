"""`ComSwBackend` : implémentation réelle de `SwBackend` par COM (pywin32), spec §4, §9, §10.

ÉTAT : code NON EXÉCUTÉ. Il a été écrit sans SolidWorks ni pywin32 (spec §14, « Conséquences
d'une v1 sans test réel »). Chaque appel COM porte un marqueur `# UNVERIFIED:` qui donne le membre
de l'API, la version de l'aide consultée et l'hypothèse faite ; `docs/unverified.md` en est la
liste. « aide 2026 » = API Help SOLIDWORKS 2026 (help.solidworks.com), lue le 2026-11-19.

Organisation :
1. erreurs COM et helpers purs ;
2. noyau : `_com` (reprises, traduction des HRESULT, trace), sorties par référence, conversions ;
3. un helper par piège de la spec §10 (sélection, plans de référence, noms, inférence, repère) ;
4. session, documents, esquisse, fonctions, inspection, export.

Règles : unités converties par `units.py` uniquement ; constantes lues par nom (`constants.py`) ;
aucun objet COM ne sort de cette classe ; aucun `print` ; aucun texte de document n'est exécuté.
"""

from __future__ import annotations

import contextlib
import io
import logging
import math
import os
import tempfile
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from ..envelope import DocState, ErrorCode, SwError, invalid
from . import capabilities as caps
from . import constants as k
from . import geometry as g
from . import units as u
from .composite import compose_hole
from .constants import C
from .entities import EntityRegistry, stale
from .models import (
    BBox,
    BodyStats,
    ConnectResult,
    DimensionChange,
    DimensionResult,
    DimInfo,
    DocInfo,
    DocOpenResult,
    DocRef,
    EdgeInfo,
    FaceInfo,
    FeatureCreated,
    FeatureNode,
    InstanceInfo,
    InterferencePair,
    MassResult,
    MeasureResult,
    PropChange,
    Pt2,
    RebuildIssue,
    RebuildResult,
    RelationInfo,
    RelationResult,
    SketchBeginResult,
    SketchEndResult,
    SketchEntityRef,
    SketchFrame,
    SketchInspectResult,
    SketchSegment,
    SketchStatus,
    VariableChange,
    Vec3,
)
from .worker import retry_rejected

log = logging.getLogger("swmcp.com")

PROG_ID = "SldWorks.Application"
ROT_PREFIX = "SolidWorks_PID_"

# HRESULT (spec §9, « Reprises automatiques »).
HR_REJECTED = frozenset({0x80010001, 0x8001010A})  # RPC_E_CALL_REJECTED, RPC_E_SERVERCALL_RETRYLATER
HR_DISCONNECTED = frozenset(
    {0x80010108, 0x800706BA, 0x800706BE}
)  # déconnecté, serveur RPC indisponible / en échec
HR_DISP_EXCEPTION = 0x80020009  # DISP_E_EXCEPTION : le vrai code est dans excepinfo
BUSY_DELAYS: tuple[float, ...] = (0.1, 0.2, 0.4, 0.8, 1.6)

MAX_WALK = 100_000  # garde-fou des parcours de listes chaînées COM
LIVE_PREFIX = b"live:"  # référence d'une entité d'esquisse sans identifiant persistant

# Marques de sélection lues dans l'aide 2026 (remarques de chaque méthode de fonction).
MARK_NONE = 0
MARK_REVOLVE_AXIS = 16  # FeatureRevolve2 : axe = 4 ou 16
MARK_FILLET_EDGE = 1  # FeatureFillet3 : arêtes = 1
MARK_SHELL_FACE = 1  # InsertFeatureShell : faces à retirer = 1
MARK_PATTERN_DIRECTION = 1  # FeatureLinearPattern5 / FeatureCircularPattern5 : direction ou axe = 1
MARK_PATTERN_SEED = 4  # … fonctions à répéter = 4
MARK_MIRROR_SEED = 1  # InsertMirrorFeature2 : fonctions = 1
MARK_MIRROR_PLANE = 2  # … plan ou face plane = 2

PLANE_KEYS = ("front", "top", "right")  # ordre des trois plans de référence dans l'arbre
# Plan de référence dont la normale porte l'axe du modèle (répétition linéaire par 'x', 'y', 'z').
AXIS_PLANE = {"x": "right", "y": "top", "z": "front"}
# Axe du modèle = intersection de deux plans de référence (axe de répétition circulaire).
AXIS_PLANES = {"x": ("top", "front"), "y": ("front", "right"), "z": ("top", "right")}
VIEW_CONSTANTS = {
    "front": "swFrontView",
    "back": "swBackView",
    "left": "swLeftView",
    "right": "swRightView",
    "top": "swTopView",
    "bottom": "swBottomView",
    "iso": "swIsometricView",
}
LENGTH_UNITS = {"swMM": "mm", "swCM": "cm", "swMETER": "m", "swINCHES": "in", "swFEET": "ft"}
RAW_TARGETS = ("app", "doc", "ext")


# =================================================================================================
# 1. Erreurs COM et helpers purs (testables sans pywin32)
# =================================================================================================


def hresult_of(exc: BaseException) -> int | None:
    """HRESULT non signé d'une `pywintypes.com_error`, sinon None (sans importer pywintypes)."""
    if type(exc).__name__ != "com_error":
        return None
    code = getattr(exc, "hresult", None)
    if not isinstance(code, int):
        args = getattr(exc, "args", ())
        code = args[0] if args and isinstance(args[0], int) else None
    if code is None:
        return 0
    code &= 0xFFFFFFFF
    if code == HR_DISP_EXCEPTION:
        info = getattr(exc, "excepinfo", None)
        scode = info[5] if isinstance(info, tuple) and len(info) > 5 else None
        if isinstance(scode, int) and scode:
            return scode & 0xFFFFFFFF
    return code


def is_rejected(exc: BaseException) -> bool:
    """Appel refusé sans avoir été exécuté : seul cas où un nouvel essai est permis."""
    return hresult_of(exc) in HR_REJECTED


def com_text(exc: BaseException) -> str:
    info = getattr(exc, "excepinfo", None)
    if isinstance(info, tuple) and len(info) > 2 and info[2]:
        return str(info[2])[:200]
    text = getattr(exc, "strerror", None)
    return str(text or exc)[:200]


def _seq(value: Any) -> list[Any]:
    """Tableau COM (tuple, None, valeur seule) → liste Python."""
    if value is None:
        return []
    if isinstance(value, (tuple, list)):
        return [v for v in value if v is not None]
    return [value]


def _floats(value: Any, count: int) -> tuple[float, ...]:
    items = [float(v) for v in (value if isinstance(value, (tuple, list)) else ())]
    if len(items) < count:
        raise SwError(ErrorCode.INTERNAL, f"Tableau COM de {count} réels attendu, {len(items)} reçu(s).")
    return tuple(items[:count])


def _early_bound(obj: Any) -> bool:
    """Vrai pour une instance d'une classe générée par makepy (liaison anticipée)."""
    return hasattr(type(obj), "CLSID")


def _clean(value: Any, depth: int = 0) -> Any:
    """Rend un retour COM sérialisable : aucun objet COM ne sort du backend."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, (tuple, list)) and depth < 4:
        return [_clean(v, depth + 1) for v in value[:200]]
    return f"<objet COM {type(value).__name__}>"


def _bbox(values: Sequence[float]) -> BBox:
    lo = u.vec3_m_to_mm((min(values[0], values[3]), min(values[1], values[4]), min(values[2], values[5])))
    hi = u.vec3_m_to_mm((max(values[0], values[3]), max(values[1], values[4]), max(values[2], values[5])))
    return BBox(min=u.rnd3(lo), max=u.rnd3(hi), size=u.rnd3(g.sub(hi, lo)))


@dataclass(frozen=True)
class _At:
    """Cible de sélection par position (coordonnées du modèle, en mètres) et type SelectByID2."""

    x: float
    y: float
    z: float
    type: str


@dataclass
class _Live:
    """Entité de l'esquisse ouverte, gardée vivante entre deux outils."""

    obj: Any
    kind: str  # line, arc, circle, point
    weldable: bool = False  # extrémité de contour : fusionnée avec une extrémité coïncidente
    xy: Pt2 | None = None  # position en mètres, repère de l'esquisse (None = à relire)


# =================================================================================================
# 2. Backend
# =================================================================================================


class ComSwBackend:
    """Pilote l'instance SolidWorks de la machine. Toutes les méthodes s'exécutent dans le thread COM."""

    def __init__(self, busy_delays: Sequence[float] = BUSY_DELAYS) -> None:
        self.registry = EntityRegistry()
        self._busy_delays = tuple(busy_delays)
        self._app: Any = None
        self._math: Any = None
        self.revision = ""
        self.major = 0
        self.pid = 0
        self._doc_ids: dict[str, str] = {}  # titre du document → identifiant stable du serveur
        self._doc_seq = 0
        self._counters: dict[tuple[str, str], int] = {}  # (document, préfixe) → dernier numéro de nom
        self._saved_toggles: dict[str, bool] = {}  # options modifiées, à rétablir dans cleanup()
        self._sk_live: dict[str, _Live] = {}
        self._sk_owner: str | None = None  # document de l'esquisse suivie par _sk_live
        self._live_seq = 0

    # ---------------------------------------------------------------------------------------------
    # Noyau COM : reprises, traduction des erreurs, trace (spec §9)
    # ---------------------------------------------------------------------------------------------
    def _com(self, fn: Callable[..., Any], *args: Any, label: str | None = None) -> Any:
        """Point de passage unique des appels COM.

        - 0x80010001 / 0x8001010A (appel refusé, non exécuté) : nouveaux essais puis `SW_BUSY` ;
        - 0x80010108 / 0x800706BA / 0x800706BE : instance perdue, `SW_DISCONNECTED` ;
        - autre erreur COM : `INTERNAL` avec le HRESULT en `details`.
        """
        name = label or getattr(fn, "__name__", "appel")
        start = time.perf_counter()
        outcome = "ok"
        try:
            return retry_rejected(lambda: fn(*args), is_rejected, self._busy_delays)
        except SwError as exc:
            outcome = exc.code.value
            raise
        except Exception as exc:
            code = hresult_of(exc)
            if code is None:
                outcome = type(exc).__name__
                raise SwError(
                    ErrorCode.INTERNAL,
                    f"Appel COM {name} impossible : {type(exc).__name__} : {str(exc)[:200]}",
                    hint="Membre absent de cette version de l'API ou arguments mal typés : consulter le journal et "
                    "docs/unverified.md.",
                    details={"call": name, "exception": type(exc).__name__},
                ) from exc
            outcome = f"0x{code:08X}"
            if code in HR_DISCONNECTED:
                self.mark_disconnected()
                raise SwError(
                    ErrorCode.SW_DISCONNECTED,
                    f"L'instance SolidWorks ne répond plus ({outcome}) pendant {name}.",
                    details={"call": name, "hresult": outcome},
                ) from exc
            raise SwError(
                ErrorCode.INTERNAL,
                f"Erreur COM {outcome} pendant {name} : {com_text(exc)}",
                details={"call": name, "hresult": outcome},
            ) from exc
        finally:
            if log.isEnabledFor(logging.DEBUG):
                log.debug("com %s %.1f ms %s", name, (time.perf_counter() - start) * 1000.0, outcome)

    def _m(self, obj: Any, member: str, *args: Any) -> Any:
        """Appelle une méthode COM (ou lit un membre sans argument en liaison tardive)."""
        if obj is None:
            raise SwError(ErrorCode.INTERNAL, f"Objet COM nul avant l'appel de {member}.")

        def call() -> Any:
            attr = getattr(obj, member)
            return attr(*args) if callable(attr) else attr

        return self._com(call, label=member)

    def _get(self, obj: Any, prop: str) -> Any:
        if obj is None:
            raise SwError(ErrorCode.INTERNAL, f"Objet COM nul avant la lecture de {prop}.")
        return self._com(getattr, obj, prop, label=prop)

    def _put(self, obj: Any, prop: str, value: Any) -> None:
        if obj is None:
            raise SwError(ErrorCode.INTERNAL, f"Objet COM nul avant l'écriture de {prop}.")
        self._com(setattr, obj, prop, value, label=f"{prop}=")

    def _m_out(self, obj: Any, member: str, args: Sequence[Any], outs: str) -> tuple[Any, ...]:
        """Piège §10 « paramètres de sortie par référence » : renvoie `(retour, sortie1, sortie2…)`.

        `outs` décrit les paramètres de sortie, tous en fin de signature : `i` entier, `b` booléen,
        `s` chaîne. En liaison anticipée, pywin32 renvoie un tuple ; en liaison tardive les
        valeurs seraient perdues, d'où des VARIANT passés par référence.
        """
        defaults: dict[str, Any] = {"i": 0, "b": False, "s": ""}
        if _early_bound(obj):
            result = self._m(obj, member, *args, *[defaults[c] for c in outs])
            values = result if isinstance(result, tuple) else (result,)
        else:
            import pythoncom
            from win32com.client import VARIANT

            types = {"i": pythoncom.VT_I4, "b": pythoncom.VT_BOOL, "s": pythoncom.VT_BSTR}
            refs = [VARIANT(pythoncom.VT_BYREF | types[c], defaults[c]) for c in outs]
            ret = self._m(obj, member, *args, *refs)
            values = (ret, *[r.value for r in refs])
        padded = [*values, *([None] * (1 + len(outs)))]
        return tuple(padded[: 1 + len(outs)])

    @staticmethod
    def _nil() -> Any:
        """Objet nul typé (`Nothing`) : indispensable pour le callout de SelectByID2 (spec §10)."""
        import pythoncom
        from win32com.client import VARIANT

        return VARIANT(pythoncom.VT_DISPATCH, None)

    @staticmethod
    def _empty() -> Any:
        """Variant vide (`Empty`) pour les tableaux optionnels."""
        import pythoncom

        return pythoncom.Empty

    @staticmethod
    def _doubles(values: Sequence[float]) -> Any:
        import pythoncom
        from win32com.client import VARIANT

        return VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [float(v) for v in values])

    @staticmethod
    def _objects(values: Sequence[Any]) -> Any:
        import pythoncom
        from win32com.client import VARIANT

        return VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, list(values))

    def _try_cast(self, obj: Any, interface: str) -> Any:
        """QueryInterface vers une interface générée ; None si l'objet ne l'implémente pas."""
        if obj is None:
            return None
        if type(obj).__name__ == interface:
            return obj
        try:
            import win32com.client as w32

            return w32.CastTo(obj, interface)
        except Exception as exc:
            if log.isEnabledFor(logging.DEBUG):
                log.debug("cast %s -> %s refusé : %s", type(obj).__name__, interface, type(exc).__name__)
            return None

    def _cast(self, obj: Any, interface: str) -> Any:
        """Comme `_try_cast`, mais rend l'objet d'origine en cas d'échec (liaison tardive)."""
        if obj is None:
            return None
        cast = self._try_cast(obj, interface)
        return obj if cast is None else cast

    # ---------------------------------------------------------------------------------------------
    # Session
    # ---------------------------------------------------------------------------------------------
    def _running_instances(self) -> list[tuple[int, Any]]:
        """Piège §10 « plusieurs instances » : (pid, IDispatch) des instances de la Running Object Table."""
        import pythoncom

        found: list[tuple[int, Any]] = []
        try:
            # UNVERIFIED: ROT « SolidWorks_PID_<pid> » — aide non consultée : de mémoire — chaque instance
            # enregistre un moniker de ce nom ; GetObject renvoie son ISldWorks.
            table = pythoncom.GetRunningObjectTable()
            context = pythoncom.CreateBindCtx(0)
            for moniker in table.EnumRunning():
                name = str(moniker.GetDisplayName(context, None))
                if not name.startswith(ROT_PREFIX):
                    continue
                try:
                    pid = int(name[len(ROT_PREFIX) :])
                    dispatch = table.GetObject(moniker).QueryInterface(pythoncom.IID_IDispatch)
                except Exception:
                    continue
                found.append((pid, dispatch))
        except Exception as exc:
            log.debug("lecture de la Running Object Table impossible : %s", type(exc).__name__)
        return sorted(found, key=lambda item: item[0])

    def _instance_title(self, dispatch: Any) -> str | None:
        """Titre du document actif d'une instance (liaison tardive, au mieux)."""
        try:
            import win32com.client as w32

            # UNVERIFIED: ISldWorks.ActiveDoc / IModelDoc2.GetTitle — aide 2026 — lus en liaison tardive sur
            # une instance non retenue, uniquement pour aider l'utilisateur à choisir le bon pid.
            doc = w32.Dispatch(dispatch).ActiveDoc
            if doc is None:
                return None
            title = doc.GetTitle
            return str(title() if callable(title) else title)[:120]
        except Exception:
            return None

    def connect(self, launch_if_needed: bool = False, pid: int | None = None) -> ConnectResult:
        try:
            import win32com.client as w32
            from win32com.client import gencache
        except ImportError as exc:
            raise SwError(
                ErrorCode.INTERNAL,
                "pywin32 n'est pas installé : le backend COM exige Windows, SolidWorks et pywin32.",
                hint="Installer pywin32 puis lancer `swmcp doctor`.",
            ) from exc

        instances = self._running_instances()
        target: Any = None
        if pid is not None:
            target = next((d for p, d in instances if p == pid), None)
            if target is None:
                listed = ", ".join(str(p) for p, _ in instances) or "aucune"
                raise invalid(
                    f"Aucune instance SolidWorks avec le pid {pid}.",
                    hint=f"Instances disponibles : pid {listed}.",
                )
        elif instances:
            target = instances[0][1]
        if target is None:
            try:
                # UNVERIFIED: GetActiveObject("SldWorks.Application") — aide non consultée : de mémoire —
                # renvoie l'instance enregistrée ; lève com_error si SolidWorks n'est pas lancé.
                target = w32.GetActiveObject(PROG_ID)
            except Exception as exc:
                if hresult_of(exc) is None:
                    raise
                if not launch_if_needed:
                    raise SwError(
                        ErrorCode.NOT_CONNECTED,
                        "SolidWorks n'est pas lancé sur cette machine.",
                        hint="Lancer SolidWorks, ou appeler sw_connect avec launch_if_needed=true.",
                    ) from exc
                target = PROG_ID  # EnsureDispatch crée alors l'instance

        launched = isinstance(target, str)
        # UNVERIFIED: gencache.EnsureDispatch — aide non consultée : de mémoire (pywin32) — liaison anticipée
        # depuis la bibliothèque de types de l'instance ; accepte un ProgID ou un IDispatch.
        app = self._com(gencache.EnsureDispatch, target, label="EnsureDispatch")
        self._app = app
        self._math = None
        try:
            if launched:
                # UNVERIFIED: ISldWorks.Visible — aide 2026 — propriété booléenne en écriture ; une instance
                # créée par COM démarre invisible.
                self._put(app, "Visible", True)
            # UNVERIFIED: ISldWorks.RevisionNumber — aide 2026 — méthode sans argument, « majeure.sp.correctif ».
            self.revision = str(self._m(app, "RevisionNumber"))
            self.major = caps.parse_revision(self.revision)[0]
            if self.major < caps.MIN_SUPPORTED_MAJOR:
                log.warning(
                    "SOLIDWORKS %s (révision %s) est antérieur aux versions visées",
                    caps.sw_year(self.major),
                    self.revision,
                )
            self._com(k.load, app, label="constants.load")
            # UNVERIFIED: ISldWorks.GetProcessID — aide 2026 — entier, identifiant du processus.
            self.pid = int(self._m(app, "GetProcessID"))
            # UNVERIFIED: ISldWorks.GetCurrentLanguage — aide 2026 — chaîne en minuscules (« french »).
            language = str(self._m(app, "GetCurrentLanguage") or "") or None
            active = self._active_doc()
            active_name = self._title(active) if active is not None else None
        except SwError:
            if self._app is not None and not k.is_loaded():
                self._release()
            raise
        infos = [InstanceInfo(pid=p, title=self._instance_title(d)) for p, d in instances]
        if not any(i.pid == self.pid for i in infos):
            infos.append(InstanceInfo(pid=self.pid, title=active_name))
        return ConnectResult(
            version=str(caps.sw_year(self.major)),
            revision=self.revision,
            pid=self.pid,
            language=language,
            active_doc=active_name,
            instances=infos,
        )

    def is_connected(self) -> bool:
        return self._app is not None

    def ping(self) -> bool:
        # UNVERIFIED: ISldWorks.GetProcessID — aide 2026 — appel court sans effet, preuve que SolidWorks répond.
        self._m(self._application(), "GetProcessID")
        return True

    def _release(self) -> None:
        self._app = None
        self._math = None
        self._doc_ids.clear()
        self._counters.clear()
        self._saved_toggles.clear()
        self._sk_live.clear()
        self._sk_owner = None

    def mark_disconnected(self) -> None:
        """Instance perdue : libère toutes les références COM et invalide tous les handles."""
        self._release()
        self.registry.clear()

    def shutdown(self) -> None:
        if self._app is not None:
            self.cleanup()
        self.mark_disconnected()

    def state(self) -> DocState:
        """État réel relu dans SolidWorks ; ne lève jamais."""
        if self._app is None:
            return DocState()
        try:
            doc = self._active_doc()
            if doc is None:
                return DocState()
            sketch = self._active_sketch(doc)
            # UNVERIFIED: IModelDoc2.GetFeatureCount — aide 2026 — nombre de fonctions hors sous-fonctions
            # (dossiers système de l'arbre compris).
            count = int(self._m(doc, "GetFeatureCount"))
            # UNVERIFIED: IModelDocExtension.GetWhatsWrongCount — aide 2026 — nombre d'entrées du dialogue
            # « Qu'est-ce qui ne va pas » ; 0 est pris pour « reconstruction sans erreur ».
            wrong = int(self._m(self._ext(doc), "GetWhatsWrongCount"))
            return DocState(
                doc=self._title(doc),
                doc_type=self._doc_type(doc),
                dirty=self._dirty(doc),
                rebuild_ok=wrong == 0,
                feature_count=count,
                sketch_open=self._sketch_name(doc, sketch) if sketch is not None else None,
            )
        except Exception as exc:
            log.debug("state() : lecture impossible (%s)", type(exc).__name__)
            return DocState()

    def cleanup(self) -> None:
        """Retour à un état propre après un outil (spec §9) : sélection vidée, options rétablies. Ne lève jamais."""
        if self._app is None:
            return
        try:
            doc = self._active_doc()
            if doc is not None:
                self._clear(doc)
        except Exception as exc:
            log.debug("cleanup() : sélection non vidée (%s)", type(exc).__name__)
        for name, value in list(self._saved_toggles.items()):
            if self._app is None:
                break
            try:
                # UNVERIFIED: ISldWorks.SetUserPreferenceToggle — aide 2026 — (UserPreferenceValue, OnFlag),
                # sans retour ; rétablit l'option système modifiée pendant l'outil.
                self._m(self._app, "SetUserPreferenceToggle", getattr(C, name), value)
                del self._saved_toggles[name]
            except Exception as exc:
                log.debug("cleanup() : option %s non rétablie (%s)", name, type(exc).__name__)

    # ---------------------------------------------------------------------------------------------
    # Accès de base : application, document, gestionnaires
    # ---------------------------------------------------------------------------------------------
    def _application(self) -> Any:
        if self._app is None:
            raise SwError(ErrorCode.NOT_CONNECTED, "Aucune connexion à SolidWorks.")
        return self._app

    def _active_doc(self) -> Any:
        # UNVERIFIED: ISldWorks.ActiveDoc — aide 2026 — propriété, objet IModelDoc2 ou Nothing sans document.
        return self._cast(self._get(self._application(), "ActiveDoc"), "IModelDoc2")

    def _doc(self) -> Any:
        doc = self._active_doc()
        if doc is None:
            raise SwError(ErrorCode.NO_ACTIVE_DOC, "Aucun document actif.")
        return doc

    def _title(self, doc: Any) -> str:
        # UNVERIFIED: IModelDoc2.GetTitle — aide 2026 — titre affiché dans la barre de la fenêtre.
        return str(self._m(doc, "GetTitle"))

    def _path(self, doc: Any) -> str | None:
        # UNVERIFIED: IModelDoc2.GetPathName — aide 2026 — chemin complet, chaîne vide si jamais enregistré.
        return str(self._m(doc, "GetPathName") or "") or None

    def _dirty(self, doc: Any) -> bool:
        # UNVERIFIED: IModelDoc2.GetSaveFlag — aide 2026 — vrai si le document doit être enregistré.
        return bool(self._m(doc, "GetSaveFlag"))

    def _doc_type(self, doc: Any) -> str:
        # UNVERIFIED: IModelDoc2.GetType — aide 2026 — entier de swDocumentTypes_e.
        value = int(self._m(doc, "GetType"))
        if value == C.swDocPART:
            return "part"
        if value == C.swDocASSEMBLY:
            return "assembly"
        if value == C.swDocDRAWING:
            return "drawing"
        raise SwError(
            ErrorCode.WRONG_DOC_TYPE, f"Type de document non pris en charge (swDocumentTypes_e = {value})."
        )

    def _ext(self, doc: Any) -> Any:
        # UNVERIFIED: IModelDoc2.Extension — aide 2026 — propriété en lecture, IModelDocExtension.
        return self._get(doc, "Extension")

    def _skm(self, doc: Any) -> Any:
        # UNVERIFIED: IModelDoc2.SketchManager — aide 2026 — propriété en lecture, ISketchManager.
        return self._get(doc, "SketchManager")

    def _fm(self, doc: Any) -> Any:
        # UNVERIFIED: IModelDoc2.FeatureManager — aide 2026 — propriété en lecture, IFeatureManager.
        return self._get(doc, "FeatureManager")

    def _part(self) -> tuple[Any, Any]:
        """(IModelDoc2, IPartDoc) du document actif, qui doit être une pièce."""
        doc = self._doc()
        kind = self._doc_type(doc)
        if kind != "part":
            raise SwError(
                ErrorCode.WRONG_DOC_TYPE, f"Cet outil exige une pièce ; le document actif est de type {kind}."
            )
        return doc, self._cast(doc, "IPartDoc")

    def _doc_id(self, doc: Any) -> str:
        """Identifiant stable d'un document pour la session (les handles y sont rattachés)."""
        title = self._title(doc)
        known = self._doc_ids.get(title)
        if known is None:
            self._doc_seq += 1
            known = f"doc{self._doc_seq}"
            self._doc_ids[title] = known
        return known

    def _doc_ref(self, doc: Any, active_title: str | None = None) -> DocRef:
        title = self._title(doc)
        if active_title is None:
            active = self._active_doc()
            active_title = self._title(active) if active is not None else None
        # UNVERIFIED: IModelDoc2.IsOpenedReadOnly — aide 2026 — vrai si le document est ouvert en lecture seule.
        read_only = bool(self._m(doc, "IsOpenedReadOnly"))
        return DocRef(
            id=self._doc_id(doc),
            name=title,
            path=self._path(doc),
            type=self._doc_type(doc),
            dirty=self._dirty(doc),
            read_only=read_only,
            active=title == active_title,
        )

    def _open_docs(self) -> list[Any]:
        # UNVERIFIED: ISldWorks.GetDocuments — aide 2026 — tableau des documents de la session, y compris
        # ceux chargés en mémoire sans fenêtre ; filtrés par IModelDoc2.Visible.
        docs = [self._cast(d, "IModelDoc2") for d in _seq(self._m(self._application(), "GetDocuments"))]
        visible = []
        for doc in docs:
            # UNVERIFIED: IModelDoc2.Visible — aide 2026 — propriété booléenne, vrai si le document a une fenêtre.
            if bool(self._get(doc, "Visible")):
                visible.append(doc)
        return visible

    def _find_doc(self, wanted: str) -> Any:
        for doc in self._open_docs():
            title = self._title(doc)
            path = self._path(doc)
            if wanted in (title, self._doc_ids.get(title)) or (
                path and os.path.normcase(path) == os.path.normcase(wanted)
            ):
                return doc
        raise invalid(
            f"Document introuvable : {wanted[:80]}", hint="Appeler doc_list_open pour la liste des documents."
        )

    # ---------------------------------------------------------------------------------------------
    # Piège §10 : sélection (un seul helper, sélection vidée avant et après chaque outil)
    # ---------------------------------------------------------------------------------------------
    def _clear(self, doc: Any) -> None:
        # UNVERIFIED: IModelDoc2.ClearSelection2 — aide 2026 — (All As Boolean), sans retour.
        self._m(doc, "ClearSelection2", True)

    def _select(
        self, entity: Any, mark: int = MARK_NONE, append: bool = False, *, by_feature: bool = False
    ) -> None:
        """Sélectionne un objet avec une marque. Les fonctions lisent la sélection courante et ses marques.

        - `_At` : sélection par position, `SelectByID2` avec un callout nul typé ;
        - `by_feature` : fonction, plan ou esquisse, `IFeature::Select2` ;
        - sinon face, arête, segment ou point d'esquisse : `Select4` avec `ISelectData`.
        """
        doc = self._doc()
        if isinstance(entity, _At):
            # UNVERIFIED: IModelDocExtension.SelectByID2 — aide 2026 — (Name, Type, X, Y, Z, Append, Mark,
            # Callout, SelectOption) ; nom vide = sélection par position (mètres, repère du modèle).
            ok = self._m(
                self._ext(doc), "SelectByID2", "", entity.type, entity.x, entity.y, entity.z, append, mark,
                self._nil(), C.swSelectOptionDefault,
            )  # fmt: skip
        elif by_feature:
            # UNVERIFIED: IFeature.Select2 — aide 2026 — (Append, Mark) → booléen.
            ok = self._m(self._cast(entity, "IFeature"), "Select2", append, mark)
        else:
            # UNVERIFIED: IModelDoc2.SelectionManager — aide 2026 — propriété, ISelectionMgr.
            manager = self._cast(self._get(doc, "SelectionManager"), "ISelectionMgr")
            # UNVERIFIED: ISelectionMgr.CreateSelectData — aide 2026 — sans argument → ISelectData.
            data = self._m(manager, "CreateSelectData")
            # UNVERIFIED: ISelectData.Mark — aide 2026 — propriété entière en écriture.
            self._put(data, "Mark", mark)
            # UNVERIFIED: IEntity.Select4 / ISketchSegment.Select4 / ISketchPoint.Select4 — aide 2026 —
            # (Append, Data As SelectData) → booléen ; l'objet doit exposer l'une de ces interfaces.
            ok = self._m(entity, "Select4", append, data)
        if not ok:
            raise SwError(
                ErrorCode.SELECTION_FAILED,
                "SolidWorks a refusé la sélection d'une entité.",
                details={"mark": mark, "append": append},
            )

    def _entity(self, obj: Any) -> Any:
        """Face ou arête vue comme `IEntity` (seule interface qui porte Select4)."""
        return self._cast(obj, "IEntity")

    # ---------------------------------------------------------------------------------------------
    # Piège §10 : options temporaires (inférence d'esquisse, saisie de cote), rétablies dans cleanup()
    # ---------------------------------------------------------------------------------------------
    def _toggle(self, name: str, value: bool) -> None:
        app = self._application()
        if name not in self._saved_toggles:
            # UNVERIFIED: ISldWorks.GetUserPreferenceToggle — aide 2026 — (UserPreferenceToggle) → booléen.
            current = bool(self._m(app, "GetUserPreferenceToggle", getattr(C, name)))
            if current == value:
                return
            self._saved_toggles[name] = current
        # UNVERIFIED: ISldWorks.SetUserPreferenceToggle — aide 2026 — (UserPreferenceValue, OnFlag), sans retour.
        self._m(app, "SetUserPreferenceToggle", getattr(C, name), value)

    @contextlib.contextmanager
    def _drawing(self, doc: Any) -> Iterator[Any]:
        """Création d'entités d'esquisse sans inférence ni relation automatique.

        Les entités sont ajoutées directement à la base (`AddToDB`), donc sans accrochage dépendant du
        zoom ; les relations sont ensuite posées explicitement. `AddToDB` est rétabli en sortie, les
        options système dans `cleanup()`.
        """
        self._toggle("swSketchInference", False)
        self._toggle("swSketchAutomaticRelations", False)
        manager = self._skm(doc)
        # UNVERIFIED: ISketchManager.AddToDB — aide 2026 — propriété booléenne en lecture et écriture.
        previous = bool(self._get(manager, "AddToDB"))
        if not previous:
            self._put(manager, "AddToDB", True)
        try:
            yield manager
        finally:
            if not previous and self._app is not None:
                self._put(manager, "AddToDB", False)

    # ---------------------------------------------------------------------------------------------
    # Piège §10 : arbre, noms stables, plans de référence repérés par type et ordre
    # ---------------------------------------------------------------------------------------------
    def _features(self, doc: Any) -> Iterator[Any]:
        """Parcours à plat de l'arbre des fonctions (esquisses absorbées comprises)."""
        # UNVERIFIED: IModelDoc2.FirstFeature — aide 2026 — méthode sans argument, première fonction ou Nothing.
        feature = self._m(doc, "FirstFeature")
        steps = 0
        while feature is not None and steps < MAX_WALK:
            feature = self._cast(feature, "IFeature")
            yield feature
            # UNVERIFIED: IFeature.GetNextFeature — aide 2026 — fonction suivante ou Nothing.
            feature = self._m(feature, "GetNextFeature")
            steps += 1

    def _type_name(self, feature: Any) -> str:
        # UNVERIFIED: IFeature.GetTypeName2 — aide 2026 — chaîne indépendante de la langue (« RefPlane »,
        # « ProfileFeature »…).
        return str(self._m(feature, "GetTypeName2"))

    def _fname(self, feature: Any) -> str:
        # UNVERIFIED: IFeature.Name — aide 2026 — propriété chaîne en lecture et écriture.
        return str(self._get(feature, "Name"))

    def _feature_names(self, doc: Any) -> set[str]:
        return {self._fname(f) for f in self._features(doc)}

    def _feature(self, doc: Any, name: str) -> Any:
        if self._doc_type(doc) == "part":
            # UNVERIFIED: IPartDoc.FeatureByName — aide 2026 — (Name) → fonction ou Nothing.
            found = self._m(self._cast(doc, "IPartDoc"), "FeatureByName", name)
            if found is not None:
                return self._cast(found, "IFeature")
        else:
            for feature in self._features(doc):
                if self._fname(feature) == name:
                    return feature
        raise invalid(
            f"Fonction ou esquisse introuvable : {name[:80]}",
            hint="Appeler inspect_tree pour les noms exacts.",
        )

    def _unique_name(self, doc: Any, prefix: str, wanted: str | None) -> str:
        """Nom du serveur pour un objet créé : `wanted`, sinon `<Type>_<n>` (jamais un nom par défaut localisé)."""
        names = self._feature_names(doc)
        if wanted:
            if "@" in wanted or "/" in wanted:
                raise invalid("Un nom de fonction ne peut contenir ni '@' ni '/'.")
            if wanted in names:
                raise invalid(f"Le nom '{wanted}' est déjà utilisé dans ce document.")
            return wanted
        key = (self._doc_id(doc), prefix)
        while True:
            self._counters[key] = self._counters.get(key, 0) + 1
            candidate = f"{prefix}_{self._counters[key]}"
            if candidate not in names:
                return candidate

    def _rename(self, feature: Any, name: str) -> str:
        """Renommage systématique de chaque objet créé ; renvoie le nom réellement porté."""
        self._put(feature, "Name", name)  # UNVERIFIED: IFeature.Name — aide 2026 — écriture du nom.
        actual = self._fname(feature)
        if actual != name:
            log.warning("renommage refusé par SolidWorks : %r demandé, %r obtenu", name, actual)
        return actual

    def _ref_planes(self, doc: Any) -> dict[str, Any]:
        """Les trois plans de référence, repérés par TYPE et ORDRE dans l'arbre, jamais par nom.

        Hypothèse : dans un modèle de pièce standard, les trois premières fonctions de type « RefPlane »
        sont, dans l'ordre, les plans de face, de dessus et de droite, quelle que soit la langue.
        """
        planes: list[Any] = []
        for feature in self._features(doc):
            if self._type_name(feature) == k.FEAT_REF_PLANE:
                planes.append(feature)
                if len(planes) == len(PLANE_KEYS):
                    break
        if len(planes) < len(PLANE_KEYS):
            raise SwError(
                ErrorCode.SELECTION_FAILED,
                f"Plans de référence introuvables : {len(planes)} plan(s) de type RefPlane dans l'arbre, 3 attendus.",
                hint="Le modèle (template) du document ne contient pas les trois plans standard.",
            )
        return dict(zip(PLANE_KEYS, planes, strict=True))

    def _last_feature(self, doc: Any) -> Any:
        # UNVERIFIED: IModelDoc2.FeatureByPositionReverse — aide 2026 — (Num) → n-ième fonction depuis la fin
        # de l'arbre ; 0 est pris pour la dernière.
        return self._cast(self._m(doc, "FeatureByPositionReverse", 0), "IFeature")

    def _feature_key(self, feature: Any) -> tuple[str, str] | None:
        if feature is None:
            return None
        return (self._type_name(feature), self._fname(feature))

    # ---------------------------------------------------------------------------------------------
    # Handles d'entités : identifiant persistant SolidWorks, résolu à l'usage (spec §5)
    # ---------------------------------------------------------------------------------------------
    def _persist(self, doc: Any, obj: Any) -> bytes | None:
        # UNVERIFIED: IModelDocExtension.GetPersistReference3 — aide 2026 — (DispObj) → tableau d'octets ;
        # pywin32 est supposé le rendre sous une forme convertible par bytes().
        raw = self._m(self._ext(doc), "GetPersistReference3", obj)
        if raw is None:
            return None
        try:
            data = bytes(raw)
        except (TypeError, ValueError):
            return None
        return data or None

    def _register(self, kind: str, doc: Any, obj: Any) -> str:
        ref = self._persist(doc, obj)
        if ref is None:
            raise SwError(
                ErrorCode.INTERNAL,
                f"SolidWorks n'a pas fourni d'identifiant persistant pour une entité ({kind}).",
            )
        return self.registry.register(kind, self._doc_id(doc), ref)

    def _by_persist(self, doc: Any, handle: str, ref: object) -> Any:
        import pythoncom
        from win32com.client import VARIANT

        if not isinstance(ref, bytes) or ref.startswith(LIVE_PREFIX):
            raise stale(handle)
        try:
            # UNVERIFIED: IModelDocExtension.GetObjectByPersistReference3 — aide 2026 — (PersistId, ByRef
            # ErrorCode) → objet ; ErrorCode de swPersistReferencedObjectStates_e.
            obj, state = self._m_out(
                self._ext(doc), "GetObjectByPersistReference3",
                [VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_UI1, ref)], "i",
            )  # fmt: skip
        except SwError as exc:
            if exc.code is ErrorCode.INTERNAL:
                raise stale(handle) from exc
            raise
        if obj is None or state != C.swPersistReferencedObject_Ok:
            raise stale(handle)
        return obj

    def _resolve(self, handle: str, kind: str, doc: Any) -> Any:
        """Handle → objet COM ; `ENTITY_STALE` si l'identifiant persistant ne se résout plus."""
        entry = self.registry.resolve(handle, kind, self._doc_id(doc))
        return self._by_persist(doc, handle, entry.ref)

    def _face(self, doc: Any, handle: str) -> Any:
        return self._cast(self._resolve(handle, "face", doc), "IFace2")

    def _edge(self, doc: Any, handle: str) -> Any:
        return self._cast(self._resolve(handle, "edge", doc), "IEdge")

    # ---------------------------------------------------------------------------------------------
    # Documents
    # ---------------------------------------------------------------------------------------------
    def list_docs(self) -> list[DocRef]:
        active = self._active_doc()
        active_title = self._title(active) if active is not None else ""
        return [self._doc_ref(d, active_title) for d in self._open_docs()]

    def new_doc(self, doc_type: str, template_path: str | None) -> DocRef:
        """Piège §10 « modèle par défaut absent » : lu dans les préférences, sinon INVALID_INPUT."""
        app = self._application()
        if doc_type not in k.TEMPLATE_PREFS:
            raise invalid(f"Type de document inconnu : {doc_type[:40]}")
        template = template_path
        if not template:
            # UNVERIFIED: ISldWorks.GetUserPreferenceStringValue — aide 2026 — (UserPreference) → chaîne ;
            # swDefaultTemplatePart / Assembly / Drawing donnent le chemin du modèle par défaut.
            template = str(
                self._m(app, "GetUserPreferenceStringValue", getattr(C, k.TEMPLATE_PREFS[doc_type])) or ""
            )
        if not template or not os.path.isfile(template):
            raise invalid(
                "Aucun modèle (template) par défaut utilisable n'est configuré dans SolidWorks."
                if not template_path
                else f"Modèle introuvable : {template_path[:120]}",
                hint="Fournir template_path (.prtdot, .asmdot ou .drwdot).",
            )
        paper = C.swDwgPaperA3size if doc_type == "drawing" else 0
        # UNVERIFIED: ISldWorks.NewDocument — aide 2026 — (TemplateName, PaperSize, Width, Height) → document
        # ou Nothing ; PaperSize, Width et Height ne servent qu'aux mises en plan.
        doc = self._cast(self._m(app, "NewDocument", template, paper, 0.0, 0.0), "IModelDoc2")
        if doc is None:
            raise invalid(
                f"SolidWorks n'a pas créé de document à partir du modèle {os.path.basename(template)}.",
                hint="Vérifier que le modèle correspond au type demandé, ou fournir un autre template_path.",
            )
        return self._doc_ref(doc)

    def open_doc(self, path: str, read_only: bool) -> DocOpenResult:
        """Piège §10 : erreurs et avertissements de `OpenDoc6` lus par référence."""
        app = self._application()
        extension = os.path.splitext(path)[1].lower()
        doc_type = next((t for t, e in k.DOC_EXTENSIONS.items() if e == extension), None)
        if doc_type is None:
            raise invalid(f"Extension non reconnue : {extension[:20]}")
        for opened in self._open_docs():
            existing = self._path(opened)
            if existing and os.path.normcase(existing) == os.path.normcase(path):
                activated = self._activate(opened)
                return DocOpenResult(
                    **self._doc_ref(activated).model_dump(), open_warnings=["document déjà ouvert"]
                )
        if not os.path.isfile(path):
            raise invalid(f"Fichier introuvable : {path[:200]}", open_errors=["swFileNotFoundError"])
        type_const = {"part": C.swDocPART, "assembly": C.swDocASSEMBLY, "drawing": C.swDocDRAWING}[doc_type]
        options = C.swOpenDocOptions_Silent | (C.swOpenDocOptions_ReadOnly if read_only else 0)
        # UNVERIFIED: ISldWorks.OpenDoc6 — aide 2026 — (FileName, Type, Options, Configuration, ByRef Errors,
        # ByRef Warnings) → IModelDoc2 ou Nothing ; Errors de swFileLoadError_e, Warnings de swFileLoadWarning_e.
        doc, errors, warnings = self._m_out(app, "OpenDoc6", [path, type_const, options, ""], "ii")
        error_names = k.flags_of(int(errors or 0), names=k.FILE_LOAD_ERRORS)
        warning_names = k.flags_of(int(warnings or 0), prefix=k.FILE_LOAD_WARNING_PREFIX)
        if doc is None:
            raise invalid(
                f"SolidWorks n'a pas pu ouvrir le fichier (corrompu ou illisible) : {os.path.basename(path)}",
                hint="Vérifier le fichier ; il est peut-être corrompu ou d'une version plus récente.",
                open_errors=error_names or ["swGenericError"],
            )
        doc = self._cast(doc, "IModelDoc2")
        return DocOpenResult(
            **self._doc_ref(doc).model_dump(), open_errors=error_names, open_warnings=warning_names
        )

    def _activate(self, doc: Any) -> Any:
        # UNVERIFIED: ISldWorks.ActivateDoc3 — aide 2026 — (Name, UseUserPreferences, Option, ByRef Errors)
        # → document ; Option de swRebuildOnActivation_e.
        activated, _errors = self._m_out(
            self._application(), "ActivateDoc3", [self._title(doc), False, C.swDontRebuildActiveDoc], "i"
        )
        if activated is None:
            raise invalid(f"SolidWorks n'a pas pu activer le document {self._title(doc)[:80]}.")
        return self._cast(activated, "IModelDoc2")

    def activate_doc(self, doc: str) -> DocRef:
        return self._doc_ref(self._activate(self._find_doc(doc)))

    def _save_as(self, doc: Any, path: str, options: int) -> None:
        method = caps.variant("save_as", self.major)
        extension = self._ext(doc)
        if method == "SaveAs3":
            # UNVERIFIED: IModelDocExtension.SaveAs3 — aide 2026 — (Name, Version, Options, ExportData,
            # AdvancedSaveAsOptions, ByRef Errors, ByRef Warnings) → booléen ; format déduit de l'extension.
            ok, errors, _warnings = self._m_out(
                extension,
                "SaveAs3",
                [path, C.swSaveAsCurrentVersion, options, self._nil(), self._nil()],
                "ii",
            )
        else:
            # UNVERIFIED: IModelDocExtension.SaveAs — aide 2026 — (Name, Version, Options, ExportData, ByRef
            # Errors, ByRef Warnings) → booléen.
            ok, errors, _warnings = self._m_out(
                extension, "SaveAs", [path, C.swSaveAsCurrentVersion, options, self._nil()], "ii"
            )
        if not ok or errors:
            names = k.flags_of(int(errors or 0), names=k.FILE_SAVE_ERRORS)
            code = ErrorCode.READ_ONLY_MODE if "swReadOnlySaveError" in names else ErrorCode.INTERNAL
            raise SwError(
                code,
                f"SolidWorks n'a pas pu écrire {os.path.basename(path)} : {', '.join(names) or 'échec sans code'}.",
                hint="Vérifier le chemin, l'extension et les droits d'écriture.",
                details={"save_errors": names},
            )

    def _save(self, doc: Any, path: str | None) -> str:
        doc_type = self._doc_type(doc)
        current = self._path(doc)
        target = path or current
        if target is None:
            raise invalid("Ce document n'a jamais été enregistré : `path` est obligatoire.")
        expected = k.DOC_EXTENSIONS[doc_type]
        if os.path.splitext(target)[1].lower() != expected:
            raise invalid(f"L'extension doit être {expected} pour un document de type {doc_type}.")
        same = current is not None and os.path.normcase(current) == os.path.normcase(target)
        if same:
            if bool(self._m(doc, "IsOpenedReadOnly")):  # UNVERIFIED: IModelDoc2.IsOpenedReadOnly — aide 2026.
                raise SwError(ErrorCode.READ_ONLY_MODE, "Document ouvert en lecture seule.")
            # UNVERIFIED: IModelDoc2.Save3 — aide 2026 — (Options, ByRef Errors, ByRef Warnings) → booléen.
            ok, errors, _warnings = self._m_out(doc, "Save3", [C.swSaveAsOptions_Silent], "ii")
            if not ok or errors:
                names = k.flags_of(int(errors or 0), names=k.FILE_SAVE_ERRORS)
                raise SwError(
                    ErrorCode.INTERNAL,
                    f"SolidWorks n'a pas pu enregistrer le document : {', '.join(names) or 'échec sans code'}.",
                    details={"save_errors": names},
                )
            return target
        old_title = self._title(doc)
        self._save_as(doc, target, C.swSaveAsOptions_Silent)
        known = self._doc_ids.pop(old_title, None)
        if known is not None:
            self._doc_ids[self._title(doc)] = known  # le titre change, l'identifiant et les handles restent
        return target

    def save_doc(self, path: str | None) -> str:
        doc = self._doc()
        self._no_sketch_open(doc)
        return self._save(doc, path)

    def save_copy(self, path: str) -> None:
        """Copie sans changer le document actif (points de restauration, spec §8)."""
        self._save_as(self._doc(), path, C.swSaveAsOptions_Silent | C.swSaveAsOptions_Copy)

    def close_doc(self, doc: str | None, save: bool) -> str:
        target = self._find_doc(doc) if doc else self._doc()
        if save:
            self._save(target, None)
        title = self._title(target)
        doc_id = self._doc_id(target)
        # UNVERIFIED: ISldWorks.CloseDoc — aide 2026 — (Name), sans retour ; ferme sans enregistrer, même un
        # document modifié.
        self._m(self._application(), "CloseDoc", title)
        self.registry.invalidate_doc(doc_id)
        self._doc_ids.pop(title, None)
        if self._sk_owner == doc_id:
            self._sk_live.clear()
            self._sk_owner = None
        return title

    def _material(self, doc: Any) -> str | None:
        """Nom du matériau d'une pièce, None s'il n'y en a pas."""
        if self._doc_type(doc) != "part":
            return None
        # UNVERIFIED: IPartDoc.GetMaterialPropertyName2 — aide 2026 — (ConfigName, ByRef Database) → nom ;
        # nom et base vides sans matériau ; "" est pris pour la configuration active.
        name, _database = self._m_out(self._cast(doc, "IPartDoc"), "GetMaterialPropertyName2", [""], "s")
        return str(name) if name else None

    def doc_info(self) -> DocInfo:
        doc = self._doc()
        configuration: str | None = None
        if self._doc_type(doc) != "drawing":
            # UNVERIFIED: IModelDoc2.ConfigurationManager / IConfigurationManager.ActiveConfiguration /
            # IConfiguration.Name — aide 2026 — propriétés en lecture.
            manager = self._get(doc, "ConfigurationManager")
            active = self._get(manager, "ActiveConfiguration")
            configuration = str(self._get(active, "Name")) if active is not None else None
        # UNVERIFIED: IModelDoc2.LengthUnit — aide 2026 — propriété entière de swLengthUnit_e.
        unit_value = int(self._get(doc, "LengthUnit"))
        unit = next(
            (label for name, label in LENGTH_UNITS.items() if getattr(C, name) == unit_value), "other"
        )
        return DocInfo(
            **self._doc_ref(doc).model_dump(),
            material=self._material(doc),
            configuration=configuration,
            length_unit=unit,
        )

    def _props(self, doc: Any) -> Any:
        # UNVERIFIED: IModelDocExtension.CustomPropertyManager — aide 2026 — propriété paramétrée (ConfigName)
        # → ICustomPropertyManager ; "" = propriétés du document. Appelée comme une méthode en liaison anticipée.
        return self._m(self._ext(doc), "CustomPropertyManager", "")

    def _prop_value(self, manager: Any, name: str) -> str | None:
        if caps.variant("custom_property_get", self.major) == "Get6":
            # UNVERIFIED: ICustomPropertyManager.Get6 — aide 2026 — (FieldName, UseCached, ByRef ValOut, ByRef
            # ResolvedValOut, ByRef WasResolved, ByRef LinkToProperty) → swCustomInfoGetResult_e.
            result, value, _resolved, _was, _link = self._m_out(manager, "Get6", [name, False], "ssbb")
        else:
            # UNVERIFIED: ICustomPropertyManager.Get5 — aide 2026 — comme Get6 sans LinkToProperty.
            result, value, _resolved, _was = self._m_out(manager, "Get5", [name, False], "ssb")
        if result == C.swCustomInfoGetResult_NotPresent:
            return None
        return "" if value is None else str(value)

    def get_props(self) -> dict[str, str]:
        manager = self._props(self._doc())
        # UNVERIFIED: ICustomPropertyManager.GetNames — aide 2026 — tableau de noms, vide sans propriété.
        names = [str(n) for n in _seq(self._m(manager, "GetNames"))]
        return {name: self._prop_value(manager, name) or "" for name in names}

    def set_prop(self, name: str, value: str | None) -> PropChange:
        manager = self._props(self._doc())
        before = self._prop_value(manager, name)
        if value is None:
            if before is not None:
                # UNVERIFIED: ICustomPropertyManager.Delete2 — aide 2026 — (FieldName) → code de résultat.
                self._m(manager, "Delete2", name)
        else:
            # UNVERIFIED: ICustomPropertyManager.Add3 — aide 2026 — (FieldName, FieldType, FieldValue,
            # OverwriteExisting) → code ; swCustomPropertyReplaceValue remplace une valeur existante.
            self._m(manager, "Add3", name, C.swCustomInfoText, value, C.swCustomPropertyReplaceValue)
        after = self._prop_value(manager, name)
        if after != value:
            raise SwError(
                ErrorCode.INTERNAL,
                f"La propriété '{name[:60]}' n'a pas pris la valeur demandée.",
                details={"before": before, "after": after},
            )
        return PropChange(name=name, before=before, after=after)

    # ---------------------------------------------------------------------------------------------
    # Esquisse : état, repère (piège §10), lecture des entités
    # ---------------------------------------------------------------------------------------------
    def _active_sketch(self, doc: Any) -> Any:
        # UNVERIFIED: ISketchManager.ActiveSketch — aide 2026 — propriété en lecture, ISketch ou Nothing.
        return self._get(self._skm(doc), "ActiveSketch")

    def _sketch_feature(self, doc: Any, sketch: Any) -> Any:
        """Fonction (`IFeature`) d'une esquisse : même objet COM, autre interface."""
        feature = self._try_cast(sketch, "IFeature")
        if feature is None:
            # Repli : une esquisse en cours d'édition est supposée être la dernière fonction de l'arbre.
            feature = self._last_feature(doc)
        return feature

    def _sketch_name(self, doc: Any, sketch: Any) -> str:
        return self._fname(self._sketch_feature(doc, sketch))

    def _no_sketch_open(self, doc: Any) -> None:
        """Spec §9 : un outil de fonction ne ferme jamais une esquisse en silence."""
        if self._doc_type(doc) == "drawing":
            return
        sketch = self._active_sketch(doc)
        if sketch is not None:
            raise SwError(
                ErrorCode.SKETCH_OPEN, f"L'esquisse '{self._sketch_name(doc, sketch)}' est encore ouverte."
            )

    def _open_sketch(self) -> tuple[Any, Any, Any]:
        """(document, ISketch, IFeature) de l'esquisse ouverte."""
        doc, _ = self._part()
        sketch = self._active_sketch(doc)
        if sketch is None:
            raise invalid(
                "Aucune esquisse n'est ouverte.", hint="Appeler sketch_begin avant d'ajouter des entités."
            )
        doc_id = self._doc_id(doc)
        if self._sk_owner != doc_id:
            self._sk_live.clear()
            self._sk_owner = doc_id
        return doc, self._cast(sketch, "ISketch"), self._sketch_feature(doc, sketch)

    def _status(self, sketch: Any) -> SketchStatus:
        # UNVERIFIED: ISketch.GetConstrainedStatus — aide 2026 — entier de swConstrainedStatus_e.
        value = int(self._m(sketch, "GetConstrainedStatus"))
        if value == C.swFullyConstrained:
            return "fully_defined"
        if value == C.swUnderConstrained:
            return "under_defined"
        if value == C.swOverConstrained:
            return "over_defined"
        return "failed"

    def _math_utility(self) -> Any:
        if self._math is None:
            # UNVERIFIED: ISldWorks.GetMathUtility — aide 2026 — sans argument → IMathUtility.
            self._math = self._cast(self._m(self._application(), "GetMathUtility"), "IMathUtility")
        return self._math

    def _frame(self, sketch: Any) -> SketchFrame:
        """Piège §10 « repère de l'esquisse » : origine, axes X, Y et normale dans le repère du modèle.

        Calculé depuis `ISketch.ModelToSketchTransform`. L'inverse (esquisse → modèle) est appliqué à
        l'origine et aux vecteurs unitaires par SolidWorks lui-même (`MultiplyTransform`), ce qui
        évite toute hypothèse sur la disposition lignes/colonnes de la matrice.
        """
        # UNVERIFIED: ISketch.ModelToSketchTransform — aide 2026 — propriété, IMathTransform modèle → esquisse.
        to_sketch = self._get(sketch, "ModelToSketchTransform")
        # UNVERIFIED: IMathTransform.Inverse — aide 2026 — sans argument → IMathTransform inverse.
        to_model = self._m(self._cast(to_sketch, "IMathTransform"), "Inverse")
        utility = self._math_utility()

        def image(member: str, values: Vec3) -> Vec3:
            # UNVERIFIED: IMathUtility.CreatePoint / CreateVector — aide 2026 — (tableau de 3 réels) → objet.
            source = self._m(utility, member, self._doubles(values))
            # UNVERIFIED: IMathPoint.MultiplyTransform / IMathVector.MultiplyTransform — aide 2026 —
            # (transformation) → nouvel objet ; un vecteur est supposé ne subir que la rotation.
            moved = self._m(source, "MultiplyTransform", to_model)
            # UNVERIFIED: IMathPoint.ArrayData / IMathVector.ArrayData — aide 2026 — tableau de 3 réels (m).
            x, y, z = _floats(self._get(moved, "ArrayData"), 3)
            return (x, y, z)

        origin = u.vec3_m_to_mm(image("CreatePoint", (0.0, 0.0, 0.0)))
        x_axis = g.unit(image("CreateVector", (1.0, 0.0, 0.0)))
        y_axis = g.unit(image("CreateVector", (0.0, 1.0, 0.0)))
        normal = g.unit(image("CreateVector", (0.0, 0.0, 1.0)))
        return SketchFrame(
            origin=u.rnd3(origin), x_axis=u.rnd3(x_axis), y_axis=u.rnd3(y_axis), normal=u.rnd3(normal)
        )

    def _xy(self, point: Any) -> Pt2:
        """Position d'un point d'esquisse, en mètres, dans le repère de l'esquisse."""
        # UNVERIFIED: ISketchPoint.X / ISketchPoint.Y — aide 2026 — propriétés réelles, repère de l'esquisse.
        return (float(self._get(point, "X")), float(self._get(point, "Y")))

    def _pt2(self, point: Any) -> Pt2:
        x, y = self._xy(point)
        return (u.rnd(u.m_to_mm(x)), u.rnd(u.m_to_mm(y)))

    def _sk_id(self, obj: Any) -> tuple[int, ...]:
        # UNVERIFIED: ISketchPoint.GetID / ISketchSegment.GetID — aide 2026 — tableau de deux entiers.
        return tuple(int(v) for v in _seq(self._m(obj, "GetID")))

    def _seg_kind(self, segment: Any) -> str | None:
        # UNVERIFIED: ISketchSegment.GetType — aide 2026 — entier de swSketchSegments_e.
        value = int(self._m(segment, "GetType"))
        if value == C.swSketchLINE:
            return "line"
        if value == C.swSketchARC:
            # UNVERIFIED: ISketchArc.IsCircle — aide 2026 — entier : 1 cercle complet, 0 arc.
            return "circle" if int(self._m(self._cast(segment, "ISketchArc"), "IsCircle")) == 1 else "arc"
        return None

    def _seg_points(self, segment: Any, kind: str) -> dict[str, Any]:
        """Points caractéristiques d'un segment : start / end / center (objets ISketchPoint)."""
        points: dict[str, Any] = {}
        if kind == "line":
            line = self._cast(segment, "ISketchLine")
            # UNVERIFIED: ISketchLine.GetStartPoint2 / GetEndPoint2 — aide 2026 — sans argument → ISketchPoint.
            points["start"] = self._m(line, "GetStartPoint2")
            points["end"] = self._m(line, "GetEndPoint2")
        else:
            arc = self._cast(segment, "ISketchArc")
            # UNVERIFIED: ISketchArc.GetCenterPoint2 / GetStartPoint2 / GetEndPoint2 — aide 2026 — → ISketchPoint.
            points["center"] = self._m(arc, "GetCenterPoint2")
            if kind == "arc":
                points["start"] = self._m(arc, "GetStartPoint2")
                points["end"] = self._m(arc, "GetEndPoint2")
        return {role: self._cast(p, "ISketchPoint") for role, p in points.items() if p is not None}

    def _seg_model(self, segment: Any, kind: str, handle: str) -> tuple[SketchSegment, list[Any]]:
        points = self._seg_points(segment, kind)
        # UNVERIFIED: ISketchSegment.ConstructionGeometry — aide 2026 — propriété booléenne.
        construction = bool(self._get(segment, "ConstructionGeometry"))
        radius: float | None = None
        ccw = True
        if kind != "line":
            arc = self._cast(segment, "ISketchArc")
            # UNVERIFIED: ISketchArc.GetRadius — aide 2026 — rayon en mètres.
            radius = u.rnd(u.m_to_mm(float(self._m(arc, "GetRadius"))))
            if kind == "arc":
                # UNVERIFIED: ISketchArc.GetRotationDir — aide 2026 — 1 = sens trigonométrique du début vers la
                # fin, par rapport à la normale du plan d'esquisse ; pris pour le sens dans le repère (X, Y).
                ccw = int(self._m(arc, "GetRotationDir")) >= 0
        model = SketchSegment(
            handle=handle,
            type=kind,
            start=self._pt2(points["start"]) if "start" in points else None,
            end=self._pt2(points["end"]) if "end" in points else None,
            center=self._pt2(points["center"]) if "center" in points else None,
            radius_mm=radius,
            ccw=ccw,
            construction=construction,
        )
        return model, list(points.values())

    def _read_segments(self, doc: Any, sketch: Any, live: bool) -> list[SketchSegment]:
        """Segments et points libres d'une esquisse, en mm dans son repère."""
        out: list[SketchSegment] = []
        used: set[tuple[int, ...]] = set()
        # UNVERIFIED: ISketch.GetSketchSegments — aide 2026 — tableau de ISketchSegment (lignes, arcs, splines…).
        for raw in _seq(self._m(sketch, "GetSketchSegments")):
            segment = self._cast(raw, "ISketchSegment")
            kind = self._seg_kind(segment)
            if kind is None:
                continue  # spline, ellipse, texte : hors du périmètre v1
            handle = self._reg_sk(doc, segment, kind, live=live)
            model, points = self._seg_model(segment, kind, handle)
            used.update(self._sk_id(p) for p in points)
            out.append(model)
        # UNVERIFIED: ISketch.GetSketchPoints2 — aide 2026 — tableau de tous les ISketchPoint de l'esquisse,
        # extrémités et centres des segments compris (d'où le filtrage par identifiant).
        for raw in _seq(self._m(sketch, "GetSketchPoints2")):
            point = self._cast(raw, "ISketchPoint")
            if self._sk_id(point) in used:
                continue
            handle = self._reg_sk(doc, point, "point", live=live)
            out.append(SketchSegment(handle=handle, type="point", start=self._pt2(point)))
        return out

    # ---------------------------------------------------------------------------------------------
    # Esquisse : handles `ent:sk:n`, fusion des extrémités coïncidentes
    # ---------------------------------------------------------------------------------------------
    def _reg_sk(self, doc: Any, obj: Any, kind: str, *, live: bool, weldable: bool = False) -> str:
        """Enregistre une entité d'esquisse ; `live` la garde résoluble tant que l'esquisse est ouverte."""
        ref = self._persist(doc, obj)
        if ref is None:
            # Sans identifiant persistant, le handle reste unique mais ne vaut que tant que l'esquisse est
            # ouverte (`live`) ; il sera ensuite ENTITY_STALE.
            self._live_seq += 1
            ref = LIVE_PREFIX + str(self._live_seq).encode()
        handle = self.registry.register("sk", self._doc_id(doc), ref)
        if live:
            entry = self._sk_live.get(handle)
            if entry is None:
                self._sk_live[handle] = _Live(obj, kind, weldable)
            else:
                entry.obj, entry.kind, entry.xy = obj, kind, None
                entry.weldable = entry.weldable or weldable
        return handle

    def _sk_entity(self, doc: Any, handle: str) -> tuple[Any, str]:
        """Handle d'esquisse → (objet COM, nature). `ENTITY_STALE` s'il ne se résout plus."""
        doc_id = self._doc_id(doc)
        entry = self._sk_live.get(handle) if self._sk_owner == doc_id else None
        if entry is not None:
            self.registry.resolve(handle, "sk", doc_id)
            return entry.obj, entry.kind
        obj = self._by_persist(doc, handle, self.registry.resolve(handle, "sk", doc_id).ref)
        segment = self._try_cast(obj, "ISketchSegment")
        if segment is not None:
            kind = self._seg_kind(segment)
            if kind is None:
                raise invalid(f"Le handle {handle} désigne un segment non pris en charge (spline, ellipse…).")
            return segment, kind
        point = self._try_cast(obj, "ISketchPoint")
        if point is not None:
            return point, "point"
        raise stale(handle, "ne désigne plus une entité d'esquisse")

    def _positions_changed(self) -> None:
        for entry in self._sk_live.values():
            entry.xy = None

    def _weld(self, doc: Any, read: Callable[[], Any]) -> Any:
        """Fusionne une extrémité neuve avec une extrémité existante à la même position.

        `AddToDB` ne crée aucune relation : sans fusion explicite, un contour dessiné segment par
        segment serait fermé géométriquement mais se disloquerait à la première cote.
        """
        point = self._cast(read(), "ISketchPoint")
        if point is None:
            raise SwError(ErrorCode.INTERNAL, "Extrémité d'un segment d'esquisse introuvable.")
        x, y = self._xy(point)
        tolerance = u.mm_to_m(g.TOL)
        for entry in list(self._sk_live.values()):
            if not entry.weldable or entry.kind != "point":
                continue
            if entry.xy is None:
                try:
                    entry.xy = self._xy(entry.obj)
                except SwError as exc:
                    if exc.code is not ErrorCode.INTERNAL:
                        raise
                    entry.weldable = (
                        False  # point disparu (fusionné par une relation) : ne plus le considérer
                    )
                    continue
            if abs(entry.xy[0] - x) > tolerance or abs(entry.xy[1] - y) > tolerance:
                continue
            self._clear(doc)
            self._select(entry.obj)
            self._select(point, append=True)
            # UNVERIFIED: IModelDoc2.SketchAddConstraints — aide 2026 — (IdStr), sans retour ; « sgMERGEPOINTS »
            # appliqué à deux points sélectionnés est supposé les fusionner en un seul.
            self._m(doc, "SketchAddConstraints", k.RELATION_IDS["merge"])
            self._clear(doc)
            survivor = self._cast(read(), "ISketchPoint")
            previous = entry.obj
            for other in self._sk_live.values():
                if other.obj is previous:
                    other.obj, other.xy = survivor, None
            return survivor
        return point

    def _missing_entity(self, what: str) -> SwError:
        return SwError(
            ErrorCode.FEATURE_FAILED,
            f"SolidWorks n'a pas créé l'entité d'esquisse demandée ({what}).",
            hint="Vérifier les coordonnées (mm, repère de l'esquisse) puis appeler sketch_inspect.",
            details={"entity": what},
        )

    def _new_line(
        self, doc: Any, manager: Any, x1: float, y1: float, x2: float, y2: float, construction: bool = False
    ) -> tuple[Any, Any, Any]:
        """Crée une ligne (mm) ; renvoie (segment, point de départ, point d'arrivée) après fusion."""
        if math.hypot(x2 - x1, y2 - y1) < g.TOL:
            raise invalid("Segment de longueur nulle : les deux extrémités sont confondues.")
        member = "CreateCenterLine" if construction else "CreateLine"
        # UNVERIFIED: ISketchManager.CreateLine / CreateCenterLine — aide 2026 — (X1, Y1, Z1, X2, Y2, Z2) en
        # mètres → ISketchSegment ; coordonnées prises dans le repère de l'esquisse active.
        raw = self._m(manager, member, u.mm_to_m(x1), u.mm_to_m(y1), 0.0, u.mm_to_m(x2), u.mm_to_m(y2), 0.0)
        if raw is None:
            raise self._missing_entity("ligne")
        line = self._cast(raw, "ISketchLine")

        def start() -> Any:
            return self._m(line, "GetStartPoint2")  # UNVERIFIED: ISketchLine.GetStartPoint2 — aide 2026.

        def end() -> Any:
            return self._m(line, "GetEndPoint2")  # UNVERIFIED: ISketchLine.GetEndPoint2 — aide 2026.

        return self._cast(raw, "ISketchSegment"), self._weld(doc, start), self._weld(doc, end)

    def _new_arc(
        self,
        doc: Any,
        manager: Any,
        cx: float,
        cy: float,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        ccw: bool,
    ) -> tuple[Any, Any, Any, Any]:
        """Crée un arc (mm) ; renvoie (segment, départ, arrivée, centre)."""
        # UNVERIFIED: ISketchManager.CreateArc — aide 2026 — (XC, YC, Zc, X1, Y1, Z1, X2, Y2, Z2, Direction) en
        # mètres → ISketchSegment ; Direction +1 = du départ à l'arrivée en sens trigonométrique, -1 = horaire.
        raw = self._m(
            manager, "CreateArc",
            u.mm_to_m(cx), u.mm_to_m(cy), 0.0, u.mm_to_m(x1), u.mm_to_m(y1), 0.0, u.mm_to_m(x2), u.mm_to_m(y2), 0.0,
            1 if ccw else -1,
        )  # fmt: skip
        if raw is None:
            raise self._missing_entity("arc")
        arc = self._cast(raw, "ISketchArc")

        def start() -> Any:
            return self._m(arc, "GetStartPoint2")  # UNVERIFIED: ISketchArc.GetStartPoint2 — aide 2026.

        def end() -> Any:
            return self._m(arc, "GetEndPoint2")  # UNVERIFIED: ISketchArc.GetEndPoint2 — aide 2026.

        # UNVERIFIED: ISketchArc.GetCenterPoint2 — aide 2026 — sans argument → ISketchPoint.
        center = self._cast(self._m(arc, "GetCenterPoint2"), "ISketchPoint")
        return self._cast(raw, "ISketchSegment"), self._weld(doc, start), self._weld(doc, end), center

    def _refs(self, doc: Any, items: Sequence[tuple[Any, str, str | None, bool]]) -> list[SketchEntityRef]:
        """(objet, type, rôle, extrémité fusionnable) → références renvoyées au LLM."""
        return [
            SketchEntityRef(
                handle=self._reg_sk(doc, obj, kind, live=True, weldable=weldable),
                type=kind,
                role=role,
            )
            for obj, kind, role, weldable in items
        ]

    def _relation_count(self, sketch: Any) -> int:
        # UNVERIFIED: ISketch.RelationManager / ISketchRelationManager.GetRelationsCount — aide 2026 —
        # propriété puis (Filter de swSketchRelationFilterType_e) → entier.
        return int(self._m(self._get(sketch, "RelationManager"), "GetRelationsCount", C.swAll))

    def _constrain(
        self, doc: Any, sketch: Any, targets: Sequence[Any], relation: str, check: bool = True
    ) -> None:
        """Pose une relation explicite sur les entités sélectionnées (jamais d'inférence)."""
        before = self._relation_count(sketch) if check else 0
        self._clear(doc)
        for index, target in enumerate(targets):
            self._select(target, append=index > 0)
        # UNVERIFIED: IModelDoc2.SketchAddConstraints — aide 2026 — (IdStr), sans retour ; identifiants
        # « sg… » de la colonne « 2006 and later ».
        self._m(doc, "SketchAddConstraints", k.RELATION_IDS[relation])
        self._clear(doc)
        if check and self._relation_count(sketch) <= before:
            raise invalid(
                f"SolidWorks a refusé la relation « {relation} » pour ces entités.",
                hint="Vérifier le type des entités (sketch_inspect) ; la relation est peut-être impossible ou déjà posée.",
            )

    def _sk_operand(self, doc: Any, frame: SketchFrame | None, handle: str) -> tuple[Any, str]:
        """Entité désignée dans une relation ou une cote : handle, ou le littéral "origin"."""
        if handle == "origin":
            if frame is None:
                raise SwError(ErrorCode.INTERNAL, "Repère d'esquisse manquant pour sélectionner l'origine.")
            # L'origine de l'esquisse se sélectionne par position : son nom dépend de la langue.
            x, y, z = u.vec3_mm_to_m(frame.origin)
            return _At(x, y, z, k.SEL_EXT_SKETCH_POINT), "origin"
        return self._sk_entity(doc, handle)

    # ---------------------------------------------------------------------------------------------
    # Esquisse : outils
    # ---------------------------------------------------------------------------------------------
    def sketch_begin(self, plane: str, name: str | None) -> SketchBeginResult:
        doc, _ = self._part()
        self._no_sketch_open(doc)
        wanted = self._unique_name(doc, "Sketch", name)
        self._clear(doc)
        if plane in PLANE_KEYS:
            self._select(self._ref_planes(doc)[plane], by_feature=True)
            label = plane
        else:
            face = self._face(doc, plane)
            if not self._is_planar(face):
                raise SwError(ErrorCode.SELECTION_FAILED, "Une esquisse exige un plan ou une face plane.")
            self._select(self._entity(face))
            label = "face"
        manager = self._skm(doc)
        # UNVERIFIED: ISketchManager.InsertSketch — aide 2026 — (UpdateEditRebuild), sans retour ; ouvre une
        # esquisse sur le plan ou la face sélectionné, ou ferme l'esquisse active.
        self._m(manager, "InsertSketch", True)
        sketch = self._active_sketch(doc)
        if sketch is None:
            raise SwError(
                ErrorCode.SELECTION_FAILED,
                "SolidWorks n'a pas ouvert d'esquisse sur ce plan ou cette face.",
                hint="Vérifier que la face est plane et qu'aucun dialogue n'est ouvert dans SolidWorks.",
            )
        sketch = self._cast(sketch, "ISketch")
        actual = self._rename(self._sketch_feature(doc, sketch), wanted)
        self._sk_live.clear()
        self._sk_owner = self._doc_id(doc)
        self._clear(doc)
        return SketchBeginResult(name=actual, plane=label, frame=self._frame(sketch))

    def sketch_end(self) -> SketchEndResult:
        doc, sketch, feature = self._open_sketch()
        name = self._fname(feature)
        segments = self._read_segments(doc, sketch, live=True)
        status = self._status(sketch)
        # UNVERIFIED: ISketchManager.InsertSketch — aide 2026 — (UpdateEditRebuild) : ferme l'esquisse active
        # et reconstruit.
        self._m(self._skm(doc), "InsertSketch", True)
        self._sk_live.clear()
        self._sk_owner = None
        if not segments:
            try:
                self._clear(doc)
                self._select(feature, by_feature=True)
                # UNVERIFIED: IModelDocExtension.DeleteSelection2 — aide 2026 — (DeleteOptions) → booléen ;
                # 0 = supprimer la seule sélection.
                self._m(self._ext(doc), "DeleteSelection2", 0)
            except SwError as exc:
                # SolidWorks écarte parfois de lui-même une esquisse vide en la quittant : rien à supprimer.
                if exc.code not in (ErrorCode.SELECTION_FAILED, ErrorCode.INTERNAL):
                    raise
            return SketchEndResult(
                name=name,
                status="failed",
                closed=False,
                segment_count=0,
                issues=["esquisse vide : supprimée"],
            )
        report = g.analyze_profile(segments)
        return SketchEndResult(
            name=name,
            status=status,
            closed=report.usable,
            segment_count=len(segments),
            issues=report.issues[:10],
        )

    def sketch_line(
        self, x1: float, y1: float, x2: float, y2: float, construction: bool
    ) -> list[SketchEntityRef]:
        doc, _, _ = self._open_sketch()
        with self._drawing(doc) as manager:
            segment, start, end = self._new_line(doc, manager, x1, y1, x2, y2, construction)
        return self._refs(
            doc,
            [(segment, "line", None, False), (start, "point", "start", True), (end, "point", "end", True)],
        )

    def sketch_rect(self, x1: float, y1: float, x2: float, y2: float) -> list[SketchEntityRef]:
        """Rectangle par deux coins : quatre lignes fusionnées, relations horizontales et verticales."""
        doc, sketch, _ = self._open_sketch()
        xa, xb, ya, yb = min(x1, x2), max(x1, x2), min(y1, y2), max(y1, y2)
        if xb - xa < g.TOL or yb - ya < g.TOL:
            raise invalid("Rectangle dégénéré : les deux coins doivent différer en x et en y.")
        corners = [(xa, ya), (xb, ya), (xb, yb), (xa, yb)]
        sides: list[Any] = []
        points: list[Any] = []
        with self._drawing(doc) as manager:
            for index in range(4):
                (px, py), (qx, qy) = corners[index], corners[(index + 1) % 4]
                segment, start, _end = self._new_line(doc, manager, px, py, qx, qy)
                sides.append(segment)
                points.append(start)
            for index, segment in enumerate(sides):
                self._constrain(
                    doc, sketch, [segment], "horizontal" if index % 2 == 0 else "vertical", check=False
                )
        items: list[tuple[Any, str, str | None, bool]] = [
            (segment, "line", role, False)
            for segment, role in zip(sides, ("bottom", "right", "top", "left"), strict=True)
        ]
        # Les coins sont relus après fusion : le point survivant est celui que porte la ligne.
        for index, role in enumerate(("bottom_left", "bottom_right", "top_right", "top_left")):
            corner = self._seg_points(sides[index], "line").get("start", points[index])
            items.append((corner, "point", role, True))
        return self._refs(doc, items)

    def sketch_circle(self, cx: float, cy: float, radius: float) -> list[SketchEntityRef]:
        doc, _, _ = self._open_sketch()
        if radius <= 0:
            raise invalid("Le rayon d'un cercle doit être strictement positif.")
        with self._drawing(doc) as manager:
            # UNVERIFIED: ISketchManager.CreateCircleByRadius — aide 2026 — (XC, YC, Zc, Radius) en mètres →
            # ISketchSegment (cercle complet).
            raw = self._m(
                manager, "CreateCircleByRadius", u.mm_to_m(cx), u.mm_to_m(cy), 0.0, u.mm_to_m(radius)
            )
        if raw is None:
            raise self._missing_entity("cercle")
        segment = self._cast(raw, "ISketchSegment")
        center = self._seg_points(segment, "circle").get("center")
        items: list[tuple[Any, str, str | None, bool]] = [(segment, "circle", None, False)]
        if center is not None:
            items.append((center, "point", "center", False))
        return self._refs(doc, items)

    def sketch_arc(
        self, cx: float, cy: float, x1: float, y1: float, x2: float, y2: float, ccw: bool
    ) -> list[SketchEntityRef]:
        doc, _, _ = self._open_sketch()
        radius = math.hypot(x1 - cx, y1 - cy)
        if radius < g.TOL:
            raise invalid("Arc dégénéré : le point de départ est confondu avec le centre.")
        if abs(radius - math.hypot(x2 - cx, y2 - cy)) > 1e-3:
            raise invalid("Les deux extrémités de l'arc ne sont pas à la même distance du centre.")
        with self._drawing(doc) as manager:
            segment, start, end, center = self._new_arc(doc, manager, cx, cy, x1, y1, x2, y2, ccw)
        return self._refs(
            doc,
            [
                (segment, "arc", None, False),
                (start, "point", "start", True),
                (end, "point", "end", True),
                (center, "point", "center", False),
            ],
        )

    def sketch_polyline(self, points: list[Pt2], closed: bool) -> list[SketchEntityRef]:
        doc, _, _ = self._open_sketch()
        count = len(points)
        if count < 2 or (closed and count < 3):
            raise invalid("Une polyligne exige au moins 2 points (3 si elle est fermée).")
        segments: list[Any] = []
        with self._drawing(doc) as manager:
            for index in range(count if closed else count - 1):
                (px, py), (qx, qy) = points[index], points[(index + 1) % count]
                segment, _start, _end = self._new_line(doc, manager, px, py, qx, qy)
                segments.append(segment)
        items: list[tuple[Any, str, str | None, bool]] = [
            (segment, "line", f"segment_{index + 1}", False) for index, segment in enumerate(segments)
        ]
        # Sommets relus après fusion : le sommet i est le départ du segment i (ou l'arrivée du dernier).
        for index in range(count):
            if index < len(segments):
                vertex = self._seg_points(segments[index], "line")["start"]
            else:
                vertex = self._seg_points(segments[-1], "line")["end"]
            items.append((vertex, "point", f"point_{index + 1}", True))
        return self._refs(doc, items)

    def sketch_slot(self, x1: float, y1: float, x2: float, y2: float, width: float) -> list[SketchEntityRef]:
        """Fente droite : deux lignes et deux demi-cercles, tangences et égalité des rayons posées explicitement."""
        doc, sketch, _ = self._open_sketch()
        length = math.hypot(x2 - x1, y2 - y1)
        if length < g.TOL or width <= 0:
            raise invalid("Fente dégénérée : les deux centres doivent être distincts et la largeur positive.")
        ux, uy = (x2 - x1) / length, (y2 - y1) / length
        nx, ny = -uy * width / 2, ux * width / 2
        p = [(x1 - nx, y1 - ny), (x2 - nx, y2 - ny), (x2 + nx, y2 + ny), (x1 + nx, y1 + ny)]
        with self._drawing(doc) as manager:
            side_1, _, _ = self._new_line(doc, manager, *p[0], *p[1])
            end_arc, _, _, end_center = self._new_arc(doc, manager, x2, y2, *p[1], *p[2], True)
            side_2, _, _ = self._new_line(doc, manager, *p[2], *p[3])
            start_arc, _, _, start_center = self._new_arc(doc, manager, x1, y1, *p[3], *p[0], True)
            for first, second in (
                (side_1, end_arc),
                (end_arc, side_2),
                (side_2, start_arc),
                (start_arc, side_1),
            ):
                self._constrain(doc, sketch, [first, second], "tangent", check=False)
            self._constrain(doc, sketch, [end_arc, start_arc], "equal", check=False)
        return self._refs(
            doc,
            [
                (side_1, "line", "side_1", False),
                (end_arc, "arc", "end_arc", False),
                (side_2, "line", "side_2", False),
                (start_arc, "arc", "start_arc", False),
                (start_center, "point", "start_center", False),
                (end_center, "point", "end_center", False),
            ],
        )

    def sketch_point(self, x: float, y: float) -> list[SketchEntityRef]:
        doc, _, _ = self._open_sketch()
        with self._drawing(doc) as manager:
            # UNVERIFIED: ISketchManager.CreatePoint — aide 2026 — (X, Y, Z) en mètres → ISketchPoint.
            raw = self._m(manager, "CreatePoint", u.mm_to_m(x), u.mm_to_m(y), 0.0)
        if raw is None:
            raise self._missing_entity("point")
        return self._refs(doc, [(self._cast(raw, "ISketchPoint"), "point", None, False)])

    def sketch_relation(self, rel_type: str, entities: list[str]) -> RelationResult:
        doc, sketch, _ = self._open_sketch()
        if rel_type not in k.RELATION_IDS or rel_type in ("merge", "horizontal_points", "vertical_points"):
            raise invalid(f"Type de relation inconnu : {rel_type[:40]}")
        if not entities:
            raise invalid("Une relation exige au moins une entité.")
        frame = self._frame(sketch) if "origin" in entities else None
        items = [self._sk_operand(doc, frame, handle) for handle in entities]
        targets = [target for target, _ in items]
        kinds = [kind for _, kind in items]
        if rel_type in ("horizontal", "vertical"):
            if len(items) == 2 and all(kind in ("point", "origin") for kind in kinds):
                self._constrain(doc, sketch, targets, f"{rel_type}_points")
            else:
                if any(kind != "line" for kind in kinds):
                    raise invalid(f"La relation {rel_type} s'applique à des lignes ou à deux points.")
                for target in targets:
                    self._constrain(doc, sketch, [target], rel_type)
        elif rel_type == "fix":
            for target, kind in items:
                if kind != "origin":
                    self._constrain(doc, sketch, [target], "fix")
        elif rel_type == "coincident":
            if len(items) != 2:
                raise invalid("La relation coincident exige exactement deux entités.")
            if not any(kind in ("point", "origin") for kind in kinds):
                raise invalid("La relation coincident exige au moins un point (ou 'origin').")
            # Deux points coïncidents sont fusionnés par SolidWorks : le nombre de relations ne prouve rien.
            self._constrain(doc, sketch, targets, "coincident", check=False)
            for handle, kind in zip(entities, kinds, strict=True):
                if kind == "point":
                    self._sk_live.pop(
                        handle, None
                    )  # l'un des deux points a pu disparaître : résolution à neuf
        else:
            need = 3 if rel_type == "symmetric" else 2
            if len(items) < need or "origin" in kinds:
                raise invalid(f"La relation {rel_type} exige au moins {need} entités de l'esquisse.")
            self._constrain(doc, sketch, targets, rel_type)
        self._positions_changed()
        return RelationResult(relation=rel_type, entities=entities, status=self._status(sketch))

    def _anchor(self, items: Sequence[tuple[Any, str]], dim_type: str) -> Pt2:
        """Position du texte d'une cote (mm, repère de l'esquisse), choisie pour lever les ambiguïtés.

        Entre deux points, la position décide du type de cote (horizontale, verticale ou alignée) :
        le texte est placé sur la médiatrice pour obtenir la distance vraie.
        """
        marks: list[Pt2] = []
        span = 0.0
        for obj, kind in items:
            if kind == "origin":
                marks.append((0.0, 0.0))
            elif kind == "point":
                marks.append(self._pt2(obj))
            elif kind == "line":
                ends = self._seg_points(obj, "line")
                a, b = self._pt2(ends["start"]), self._pt2(ends["end"])
                span = max(span, math.dist(a, b))
                if dim_type in ("length", "angle"):
                    marks += [a, b]
                else:
                    marks.append(((a[0] + b[0]) / 2, (a[1] + b[1]) / 2))
            else:
                center = self._pt2(self._seg_points(obj, kind)["center"])
                # UNVERIFIED: ISketchArc.GetRadius — aide 2026 — rayon en mètres.
                radius = u.m_to_mm(float(self._m(self._cast(obj, "ISketchArc"), "GetRadius")))
                span = max(span, radius)
                marks.append(center)
        cx = sum(m[0] for m in marks) / len(marks)
        cy = sum(m[1] for m in marks) / len(marks)
        offset = 5.0 + 0.3 * max(span, max((math.dist(m, (cx, cy)) for m in marks), default=0.0))
        if len(marks) >= 2 and dim_type in ("length", "distance"):
            (ax, ay), (bx, by) = marks[0], marks[-1]
            gap = math.hypot(bx - ax, by - ay)
            if gap > g.TOL:
                return (cx - (by - ay) / gap * offset, cy + (bx - ax) / gap * offset)
        return (cx + offset, cy + offset)

    def sketch_dimension(
        self, dim_type: str, entities: list[str], value: float, name: str | None
    ) -> DimensionResult:
        doc, sketch, feature = self._open_sketch()
        need = {"length": 1, "diameter": 1, "radius": 1, "angle": 2, "distance": 2}.get(dim_type)
        if need is None:
            raise invalid(f"Type de cote inconnu : {dim_type[:40]}")
        if len(entities) != need:
            raise invalid(f"Une cote '{dim_type}' exige {need} entité(s), {len(entities)} fournie(s).")
        if value <= 0:
            raise invalid("La valeur d'une cote doit être strictement positive.")
        if name and "@" in name:
            raise invalid("Un nom de cote ne peut pas contenir '@'.")
        frame = self._frame(sketch)
        items = [self._sk_operand(doc, frame, handle) for handle in entities]
        kinds = [kind for _, kind in items]
        if dim_type in ("diameter", "radius") and kinds[0] not in ("circle", "arc"):
            raise invalid(f"Une cote '{dim_type}' s'applique à un cercle ou à un arc.")
        if dim_type == "length" and kinds[0] != "line":
            raise invalid("Une cote 'length' s'applique à une ligne.")
        if dim_type == "angle" and any(kind != "line" for kind in kinds):
            raise invalid("Une cote 'angle' s'applique à deux lignes.")
        if dim_type == "distance" and kinds[1] == "origin":
            raise invalid("La seconde entité d'une cote de distance ne peut pas être l'origine.")
        sketch_name = self._fname(feature)
        if name and any(d.full_name == f"{name}@{sketch_name}" for d in self._dims_of(feature)):
            raise invalid(f"La cote '{name}' existe déjà dans l'esquisse {sketch_name}.")
        anchor = self._anchor(items, dim_type)
        self._toggle(
            "swInputDimValOnCreate", False
        )  # sinon SolidWorks ouvre le dialogue de saisie de la valeur
        self._clear(doc)
        for index, (target, _) in enumerate(items):
            self._select(target, append=index > 0)
        x, y, z = u.vec3_mm_to_m(g.to_model(frame, anchor[0], anchor[1]))
        # UNVERIFIED: IModelDoc2.AddDimension2 — aide 2026 — (X, Y, Z) position du texte en mètres → cote
        # d'affichage ou Nothing ; position supposée exprimée dans le repère du modèle.
        display = self._m(doc, "AddDimension2", x, y, z)
        self._clear(doc)
        if display is None:
            raise invalid(
                "SolidWorks n'a pas créé la cote pour ces entités.",
                hint="Vérifier que les entités sont cotables ensemble et que l'esquisse n'est pas déjà sur-contrainte.",
            )
        display = self._cast(display, "IDisplayDimension")
        if dim_type in ("diameter", "radius"):
            # UNVERIFIED: IDisplayDimension.Diametric — aide 2026 — propriété booléenne : bascule entre cote
            # de rayon et cote de diamètre.
            self._put(display, "Diametric", dim_type == "diameter")
        # UNVERIFIED: IDisplayDimension.GetDimension2 — aide 2026 — (Index) → IDimension ; 0 = cote principale.
        dimension = self._cast(self._m(display, "GetDimension2", 0), "IDimension")
        if name:
            self._put(dimension, "Name", name)  # UNVERIFIED: IDimension.Name — aide 2026 — propriété chaîne.
        unit = "deg" if dim_type == "angle" else "mm"
        self._set_system_value(dimension, u.deg_to_rad(value) if unit == "deg" else u.mm_to_m(value))
        self._positions_changed()
        actual = str(self._get(dimension, "Name"))  # UNVERIFIED: IDimension.Name — aide 2026 — lecture.
        return DimensionResult(
            full_name=f"{actual}@{sketch_name}", value=value, unit=unit, status=self._status(sketch)
        )

    def _sketch_of(self, doc: Any, name: str) -> tuple[Any, Any]:
        """(IFeature, ISketch) d'une esquisse désignée par son nom."""
        feature = self._feature(doc, name)
        if self._type_name(feature) != k.FEAT_SKETCH:
            raise invalid(f"'{name[:80]}' n'est pas une esquisse.")
        # UNVERIFIED: IFeature.GetSpecificFeature2 — aide 2026 — sans argument → objet spécifique (ISketch
        # pour une fonction « ProfileFeature »).
        return feature, self._cast(self._m(feature, "GetSpecificFeature2"), "ISketch")

    def _relations(self, doc: Any, sketch: Any, live: bool) -> list[RelationInfo]:
        out: list[RelationInfo] = []
        # UNVERIFIED: ISketch.RelationManager / ISketchRelationManager.GetRelations — aide 2026 — propriété
        # puis (Filter) → tableau de ISketchRelation.
        manager = self._get(sketch, "RelationManager")
        for raw in _seq(self._m(manager, "GetRelations", C.swAll)):
            relation = self._cast(raw, "ISketchRelation")
            # UNVERIFIED: ISketchRelation.GetRelationType — aide 2026 — entier de swConstraintType_e.
            enum_name = k.name_of(k.CONSTRAINT_PREFIX, int(self._m(relation, "GetRelationType"))) or ""
            suffix = enum_name[len(k.CONSTRAINT_PREFIX) :]
            handles: list[str] = []
            # UNVERIFIED: ISketchRelation.GetEntities — aide 2026 — tableau des entités liées (segments, points,
            # ou entités extérieures à l'esquisse).
            for entity in _seq(self._m(relation, "GetEntities")):
                try:
                    segment = self._try_cast(entity, "ISketchSegment")
                    if segment is not None:
                        handles.append(
                            self._reg_sk(doc, segment, self._seg_kind(segment) or "line", live=live)
                        )
                    else:
                        point = self._try_cast(entity, "ISketchPoint")
                        handles.append(
                            self._reg_sk(doc, point, "point", live=live) if point is not None else "external"
                        )
                except SwError as exc:
                    if exc.code in (ErrorCode.SW_DISCONNECTED, ErrorCode.SW_BUSY):
                        raise
                    handles.append("external")
            out.append(
                RelationInfo(
                    type=k.CONSTRAINT_SUFFIXES.get(suffix, suffix.lower() or "unknown"), entities=handles
                )
            )
        return out

    def sketch_inspect(self, name: str | None) -> SketchInspectResult:
        doc, _ = self._part()
        active = self._active_sketch(doc)
        if name is None:
            doc, sketch, feature = self._open_sketch()
            live = True
        else:
            feature, sketch = self._sketch_of(doc, name)
            live = active is not None and self._sketch_name(doc, active) == name
        segments = self._read_segments(doc, sketch, live=live)
        report = g.analyze_profile(segments)
        return SketchInspectResult(
            name=self._fname(feature),
            status=self._status(sketch),
            closed=report.usable,
            frame=self._frame(sketch),
            segments=segments,
            relations=self._relations(doc, sketch, live),
            dimensions=self._dims_of(feature),
            issues=report.issues[:10],
        )

    # ---------------------------------------------------------------------------------------------
    # Cotes
    # ---------------------------------------------------------------------------------------------
    def _dimensions_of(self, feature: Any) -> Iterator[Any]:
        """Cotes (`IDimension`) portées par une fonction ou une esquisse."""
        # UNVERIFIED: IFeature.GetFirstDisplayDimension — aide 2026 — première IDisplayDimension ou Nothing.
        display = self._m(feature, "GetFirstDisplayDimension")
        steps = 0
        while display is not None and steps < MAX_WALK:
            # UNVERIFIED: IDisplayDimension.GetDimension2 — aide 2026 — (Index) → IDimension.
            dimension = self._m(self._cast(display, "IDisplayDimension"), "GetDimension2", 0)
            if dimension is not None:
                yield self._cast(dimension, "IDimension")
            # UNVERIFIED: IFeature.GetNextDisplayDimension — aide 2026 — (DispIn) → suivante ou Nothing.
            display = self._m(feature, "GetNextDisplayDimension", display)
            steps += 1

    def _dim_unit(self, dimension: Any) -> str:
        # UNVERIFIED: IDimension.GetType — aide 2026 — entier de swDimensionParamType_e.
        return "deg" if int(self._m(dimension, "GetType")) == C.swDimensionParamTypeDoubleAngular else "mm"

    def _dim_value(self, dimension: Any, unit: str) -> float:
        # UNVERIFIED: IDimension.SystemValue — aide 2026 — propriété réelle en unités système (m ou rad),
        # pour la configuration active.
        value = float(self._get(dimension, "SystemValue"))
        return u.rnd(u.rad_to_deg(value) if unit == "deg" else u.m_to_mm(value))

    def _dim_info(self, dimension: Any, owner: str) -> DimInfo:
        # UNVERIFIED: IDimension.FullName — aide 2026 — « cote@fonction@document » ; les deux premiers champs
        # forment le nom accepté par IModelDoc2.Parameter.
        parts = str(self._get(dimension, "FullName") or "").split("@")
        if len(parts) >= 2 and parts[0] and parts[1]:
            short, feature_name = parts[0], parts[1]
        else:
            short, feature_name = (
                str(self._get(dimension, "Name")),
                owner,
            )  # UNVERIFIED: IDimension.Name — aide 2026.
        unit = self._dim_unit(dimension)
        return DimInfo(
            full_name=f"{short}@{feature_name}",
            value=self._dim_value(dimension, unit),
            unit=unit,
            feature=feature_name,
        )

    def _dims_of(self, feature: Any) -> list[DimInfo]:
        owner = self._fname(feature)
        return [self._dim_info(d, owner) for d in self._dimensions_of(feature)]

    def _set_system_value(self, dimension: Any, value: float) -> None:
        # UNVERIFIED: IDimension.SetSystemValue3 — aide 2026 — (NewValue, WhichConfigurations, Config_names)
        # → swSetValueReturnStatus_e ; Config_names vide avec swThisConfiguration.
        status = int(self._m(dimension, "SetSystemValue3", value, C.swThisConfiguration, self._empty()))
        if status != C.swSetValue_Successful:
            reason = k.name_of("swSetValue_", status) or f"code {status}"
            raise invalid(
                f"SolidWorks a refusé la valeur de la cote ({reason}).",
                hint="Une cote pilotée ou une valeur géométriquement impossible ne peut pas être imposée.",
            )

    # ---------------------------------------------------------------------------------------------
    # Fonctions : contrôle du contour, diagnostic sur retour nul (pièges §10), création
    # ---------------------------------------------------------------------------------------------
    def _profile(self, doc: Any, sketch_name: str) -> tuple[Any, Any]:
        """Contrôle côté serveur avant toute fonction : boucles fermées, pas de segments sécants ni dupliqués."""
        feature, sketch = self._sketch_of(doc, sketch_name)
        report = g.analyze_profile(self._read_segments(doc, sketch, live=False))
        if not report.usable:
            raise SwError(
                ErrorCode.SKETCH_NOT_CLOSED,
                f"Le contour de l'esquisse {sketch_name} n'est pas exploitable : "
                + "; ".join(report.issues[:3]),
                hint=f"Corriger l'esquisse {sketch_name} (sketch_inspect) : contour fermé, sans segments sécants ni "
                "dupliqués.",
                details={"sketch": sketch_name, "closed": report.closed, "issues": report.issues[:5]},
            )
        return feature, sketch

    def _selection_count(self, doc: Any) -> int:
        # UNVERIFIED: IModelDoc2.SelectionManager — aide 2026 — propriété, ISelectionMgr.
        manager = self._cast(self._get(doc, "SelectionManager"), "ISelectionMgr")
        # UNVERIFIED: ISelectionMgr.GetSelectedObjectCount2 — aide 2026 — (Mark) → entier ; -1 = toutes marques.
        return int(self._m(manager, "GetSelectedObjectCount2", -1))

    def _failure(
        self,
        doc: Any,
        type_label: str,
        name: str,
        sketch_name: str | None,
        sw_error: str,
        selected: int | None = None,
    ) -> SwError:
        """Échec silencieux d'une méthode de fonction → erreur avec diagnostic (spec §6)."""
        details: dict[str, Any] = {"feature": name, "type": type_label, "sw_error": sw_error, "kept": False}
        if selected is not None:
            details["selected_entities"] = selected
        hint: str | None = None
        try:
            # UNVERIFIED: IModelDocExtension.GetWhatsWrongCount — aide 2026 — nombre d'erreurs de reconstruction.
            details["rebuild_errors"] = int(self._m(self._ext(doc), "GetWhatsWrongCount"))
            if sketch_name:
                _, sketch = self._sketch_of(doc, sketch_name)
                report = g.analyze_profile(self._read_segments(doc, sketch, live=False))
                status = self._status(sketch)
                details["sketch"] = {"name": sketch_name, "closed": report.usable, "status": status}
                if not report.usable:
                    return SwError(
                        ErrorCode.SKETCH_NOT_CLOSED,
                        sw_error,
                        hint=f"Le contour de l'esquisse {sketch_name} n'est pas fermé ou se recoupe : "
                        + "; ".join(report.issues[:2]),
                        details=details,
                    )
                hint = (
                    f"L'esquisse {sketch_name} est fermée ({status}) : vérifier la direction, la profondeur et que "
                    "la fonction rencontre bien de la matière, puis inspect_rebuild."
                )
        except SwError as exc:
            if exc.code is ErrorCode.SW_DISCONNECTED:
                return exc
            log.debug("diagnostic incomplet : %s", exc.message)
        return SwError(
            ErrorCode.FEATURE_FAILED, f"{type_label} '{name}' : {sw_error}", hint=hint, details=details
        )

    def _delete(self, doc: Any, feature: Any, options: int) -> bool:
        self._clear(doc)
        self._select(feature, by_feature=True)
        # UNVERIFIED: IModelDocExtension.DeleteSelection2 — aide 2026 — (DeleteOptions de
        # swDeleteSelectionOptions_e) → booléen.
        done = bool(self._m(self._ext(doc), "DeleteSelection2", options))
        self._clear(doc)
        return done

    def _create(
        self,
        doc: Any,
        type_label: str,
        wanted: str,
        sketch_name: str | None,
        select: Callable[[], None],
        build: Callable[[], Any],
    ) -> FeatureCreated:
        """Sélectionne, crée, renomme, contrôle. En cas d'échec la fonction est annulée : jamais d'état ambigu."""
        self._clear(doc)
        try:
            select()
            selected = self._selection_count(doc)
            feature = build()
        finally:
            if self._app is not None:
                self._clear(doc)
        if feature is None:
            raise self._failure(
                doc, type_label, wanted, sketch_name,
                "SolidWorks n'a créé aucune fonction (retour nul, sans message).", selected,
            )  # fmt: skip
        feature = self._cast(feature, "IFeature")
        actual = self._rename(feature, wanted)
        # UNVERIFIED: IFeature.GetErrorCode2 — aide 2026 — (ByRef IsWarning) → swFeatureError_e ; IsWarning
        # n'a de sens que si le code est non nul.
        code, is_warning = self._m_out(feature, "GetErrorCode2", [], "b")
        if code and not is_warning:
            text = k.name_of(k.FEATURE_ERROR_PREFIX, int(code)) or f"swFeatureError_e = {code}"
            self._delete(doc, feature, 0)
            raise self._failure(doc, type_label, actual, sketch_name, text, selected)
        return FeatureCreated(
            feature=actual,
            type=type_label,
            dimensions=[d.full_name for d in self._dims_of(feature)],
            sketch=sketch_name,
        )

    def _after(self, doc: Any, before: tuple[str, str] | None) -> Any:
        """Fonction créée par une méthode sans valeur de retour : la dernière de l'arbre, si elle a changé."""
        last = self._last_feature(doc)
        return None if last is None or self._feature_key(last) == before else last

    def extrude(
        self,
        sketch: str,
        depth: float | None,
        end_condition: str,
        direction: str,
        merge: bool,
        draft_deg: float,
        name: str | None,
        cut: bool,
    ) -> FeatureCreated:
        doc, _ = self._part()
        self._no_sketch_open(doc)
        ends = {
            "blind": C.swEndCondBlind,
            "mid_plane": C.swEndCondMidPlane,
            "through_all": C.swEndCondThroughAll,
            "up_to_next": C.swEndCondUpToNext,
        }
        if end_condition not in ends:
            raise invalid(f"Condition de fin inconnue : {end_condition[:40]}")
        if direction not in ("normal", "reverse", "both"):
            raise invalid(f"Direction inconnue : {direction[:40]}")
        if end_condition in ("blind", "mid_plane") and (depth is None or depth <= 0):
            raise invalid(f"La condition de fin {end_condition} exige depth_mm > 0.")
        sketch_feature, _ = self._profile(doc, sketch)
        type_label = "CutExtrude" if cut else "Extrude"
        wanted = self._unique_name(doc, "Cut" if cut else "Extrude", name)
        both = direction == "both" and end_condition != "mid_plane"
        end = ends[end_condition]
        distance = u.mm_to_m(depth) if depth is not None and end_condition in ("blind", "mid_plane") else 0.0
        drafted = abs(draft_deg) > 0
        draft = u.deg_to_rad(abs(draft_deg))
        # Arguments communs à FeatureExtrusion3 et FeatureCut4 (aide 2026), dans l'ordre :
        # Sd, Flip, Dir, T1, T2, D1, D2, Dchk1, Dchk2, Ddir1, Ddir2, Dang1, Dang2,
        # OffsetReverse1, OffsetReverse2, TranslateSurface1, TranslateSurface2.
        # Dir = vrai inverse la direction par défaut (bossage : le long de la normale de l'esquisse ;
        # enlèvement : à l'opposé). Ddir = vrai : dépouille vers l'intérieur.
        head: list[Any] = [
            not both, False, direction == "reverse", end, end, distance, distance if both else 0.0,
            drafted, drafted and both, draft_deg > 0, draft_deg > 0, draft, draft, False, False, False, False,
        ]  # fmt: skip
        manager = self._fm(doc)

        def select() -> None:
            self._select(sketch_feature, MARK_NONE, by_feature=True)

        def build() -> Any:
            if not cut:
                caps.require("feature_extrusion3", self.major)
                # UNVERIFIED: IFeatureManager.FeatureExtrusion3 — aide 2026 — 23 arguments : les 17 communs,
                # puis Merge, UseFeatScope, UseAutoSelect, T0, StartOffset, FlipStartOffset → IFeature ou Nothing.
                return self._m(
                    manager, "FeatureExtrusion3", *head, merge, False, True, C.swStartSketchPlane, 0.0, False
                )
            method = caps.variant("feature_cut", self.major)
            # UNVERIFIED: IFeatureManager.FeatureCut4 — aide 2026 — 27 arguments : les 17 communs, puis
            # NormalCut, UseFeatScope, UseAutoSelect, AssemblyFeatureScope, AutoSelectComponents,
            # PropagateFeatureToParts, T0, StartOffset, FlipStartOffset, OptimizeGeometry → IFeature ou Nothing.
            # FeatureCut3 (aide 2026) : les mêmes sans OptimizeGeometry.
            tail: list[Any] = [False, False, True, False, True, False, C.swStartSketchPlane, 0.0, False]
            if method == "FeatureCut4":
                tail.append(False)
            return self._m(manager, method, *head, *tail)

        return self._create(doc, type_label, wanted, sketch, select, build)

    def revolve(
        self, sketch: str, axis: str, angle_deg: float, cut: bool, name: str | None
    ) -> FeatureCreated:
        doc, _ = self._part()
        self._no_sketch_open(doc)
        if not 0 < angle_deg <= 360:
            raise invalid("angle_deg doit être dans l'intervalle ]0, 360].")
        caps.require("feature_revolve2", self.major)
        sketch_feature, _ = self._profile(doc, sketch)
        axis_obj, axis_kind = self._sk_entity(doc, axis)
        # UNVERIFIED: ISketchSegment.GetSketch — aide 2026 — sans argument → ISketch propriétaire du segment.
        owner = self._m(axis_obj, "GetSketch") if axis_kind == "line" else None
        if owner is None or self._sketch_name(doc, owner) != sketch:
            raise invalid(
                f"L'axe doit être une ligne de l'esquisse {sketch} (idéalement une ligne de construction)."
            )
        type_label = "CutRevolve" if cut else "Revolve"
        wanted = self._unique_name(doc, type_label, name)
        manager = self._fm(doc)

        def select() -> None:
            self._select(sketch_feature, MARK_NONE, by_feature=True)
            self._select(axis_obj, MARK_REVOLVE_AXIS, append=True)

        def build() -> Any:
            # UNVERIFIED: IFeatureManager.FeatureRevolve2 — aide 2026 — 20 arguments : SingleDir, IsSolid,
            # IsThin, IsCut, ReverseDir, BothDirectionUpToSameEntity, Dir1Type, Dir2Type, Dir1Angle, Dir2Angle,
            # OffsetReverse1, OffsetReverse2, OffsetDistance1, OffsetDistance2, ThinType, ThinThickness1,
            # ThinThickness2, Merge, UseFeatScope, UseAutoSelect → IFeature ; esquisse marque 0, axe marque 16.
            return self._m(
                manager, "FeatureRevolve2",
                True, True, False, cut, False, False, C.swEndCondBlind, C.swEndCondBlind,
                u.deg_to_rad(angle_deg), 0.0, False, False, 0.0, 0.0, C.swThinWallOneDirection, 0.0, 0.0,
                True, False, True,
            )  # fmt: skip

        return self._create(doc, type_label, wanted, sketch, select, build)

    def _select_edges(self, edges: Sequence[Any], mark: int) -> None:
        for index, edge in enumerate(edges):
            self._select(self._entity(edge), mark, append=index > 0)

    def fillet(self, edges: list[str], radius: float, name: str | None) -> FeatureCreated:
        doc, _ = self._part()
        self._no_sketch_open(doc)
        if not edges or radius <= 0:
            raise invalid("Un congé exige au moins une arête et un rayon strictement positif.")
        resolved = [self._edge(doc, h) for h in edges]
        wanted = self._unique_name(doc, "Fillet", name)
        manager = self._fm(doc)
        method = caps.variant("fillet", self.major)

        def build() -> Any:
            if method == "CreateFeature":
                # UNVERIFIED: IFeatureManager.CreateDefinition — aide 2026 — (Type de swFeatureNameID_e) → objet de
                # définition ; swFmFillet donne un ISimpleFilletFeatureData2.
                data = self._cast(
                    self._m(manager, "CreateDefinition", C.swFmFillet), "ISimpleFilletFeatureData2"
                )
                # UNVERIFIED: ISimpleFilletFeatureData2.Initialize — aide 2026 — (FilletType de
                # swSimpleFilletType_e) → booléen.
                if data is None or not self._m(data, "Initialize", C.swConstRadiusFillet):
                    return None
                # UNVERIFIED: ISimpleFilletFeatureData2.ConicTypeForCrossSectionProfile / DefaultRadius /
                # PropagateToTangentFaces — aide 2026 — propriétés ; les arêtes présélectionnées (marque 1,
                # comme pour FeatureFillet3) sont supposées reprises par CreateFeature.
                self._put(data, "ConicTypeForCrossSectionProfile", C.swFeatureFilletCircular)
                self._put(data, "DefaultRadius", u.mm_to_m(radius))
                self._put(data, "PropagateToTangentFaces", True)
                # UNVERIFIED: IFeatureManager.CreateFeature — aide 2026 — (FeatureData) → IFeature ou Nothing.
                return self._m(manager, "CreateFeature", data)
            # UNVERIFIED: IFeatureManager.FeatureFillet3 — aide 2026 — (Options, R1, R2, Rho, Ftyp, OverflowType,
            # ConicRhoType, Radii, Dist2Arr, RhoArr, SetBackDistances, PointRadiusArray, PointDist2Array,
            # PointRhoArray) → IFeature ; tableaux vides pour un rayon uniforme.
            empty = self._empty()
            return self._m(
                manager, "FeatureFillet3",
                C.swFeatureFilletUniformRadius | C.swFeatureFilletPropagate, u.mm_to_m(radius), 0.0, 0.0,
                C.swFeatureFilletType_Simple, C.swFilletOverFlowType_Default, C.swFeatureFilletCircular,
                empty, empty, empty, empty, empty, empty, empty,
            )  # fmt: skip

        return self._create(
            doc, "Fillet", wanted, None, lambda: self._select_edges(resolved, MARK_FILLET_EDGE), build
        )

    def chamfer(
        self, edges: list[str], distance: float, angle_deg: float, name: str | None
    ) -> FeatureCreated:
        doc, _ = self._part()
        self._no_sketch_open(doc)
        if not edges or distance <= 0 or not 0 < angle_deg < 90:
            raise invalid(
                "Un chanfrein exige au moins une arête, une distance positive et un angle dans ]0, 90[."
            )
        caps.require("feature_chamfer", self.major)
        resolved = [self._edge(doc, h) for h in edges]
        wanted = self._unique_name(doc, "Chamfer", name)
        manager = self._fm(doc)

        def build() -> Any:
            # UNVERIFIED: IFeatureManager.InsertFeatureChamfer — aide 2026 — (Options, ChamferType, Width, Angle,
            # OtherDist, VertexChamDist1, VertexChamDist2, VertexChamDist3) → IFeature ; arêtes présélectionnées
            # (marque non documentée : 0 supposé).
            return self._m(
                manager, "InsertFeatureChamfer",
                C.swFeatureChamferTangentPropagation, C.swChamferAngleDistance,
                u.mm_to_m(distance), u.deg_to_rad(angle_deg), 0.0, 0.0, 0.0, 0.0,
            )  # fmt: skip

        return self._create(
            doc, "Chamfer", wanted, None, lambda: self._select_edges(resolved, MARK_NONE), build
        )

    def hole(
        self, face: str, points: list[Vec3], diameter: float, depth: float | None, name: str | None
    ) -> FeatureCreated:
        """Perçages v1 : esquisse de cercles sur la face puis enlèvement extrudé (`compose_hole`)."""
        doc, _ = self._part()
        self._no_sketch_open(doc)
        if not points or diameter <= 0:
            raise invalid("Un perçage exige au moins un point et un diamètre strictement positif.")
        return compose_hole(self, face, points, diameter, depth, self._unique_name(doc, "Hole", name))

    def shell(self, faces_to_remove: list[str], thickness: float, name: str | None) -> FeatureCreated:
        doc, _ = self._part()
        self._no_sketch_open(doc)
        if thickness <= 0:
            raise invalid("L'épaisseur de la coque doit être strictement positive.")
        resolved = [self._face(doc, h) for h in faces_to_remove]
        wanted = self._unique_name(doc, "Shell", name)
        before = self._feature_key(self._last_feature(doc))

        def select() -> None:
            for index, face in enumerate(resolved):
                self._select(self._entity(face), MARK_SHELL_FACE, append=index > 0)

        def build() -> Any:
            # UNVERIFIED: IModelDoc2.InsertFeatureShell — aide 2026 — (Thickness en mètres, Outward), sans
            # retour ; faces à retirer présélectionnées avec la marque 1.
            self._m(doc, "InsertFeatureShell", u.mm_to_m(thickness), False)
            return self._after(doc, before)

        return self._create(doc, "Shell", wanted, None, select, build)

    def _seeds(self, doc: Any, features: list[str]) -> list[Any]:
        if not features:
            raise invalid("Au moins une fonction à répéter est requise.")
        seeds = []
        for name in features:
            feature = self._feature(doc, name)
            type_name = self._type_name(feature)
            if type_name in (
                k.FEAT_SKETCH,
                k.FEAT_SKETCH_3D,
                k.FEAT_REF_PLANE,
                k.FEAT_REF_AXIS,
                k.FEAT_ORIGIN,
            ):
                raise invalid(
                    f"'{name}' ({type_name}) ne peut pas être répété : une fonction de matière est attendue."
                )
            seeds.append(feature)
        return seeds

    def _direction_target(self, doc: Any, text: str, circular: bool) -> tuple[Any, bool, bool]:
        """Direction ou axe → (objet à sélectionner, sélection par fonction, inversion)."""
        axis = g.parse_axis(text)
        if axis is not None:
            letter = text.strip().lower()[-1]
            flip = min(axis) < 0
            if circular:
                return self._ref_axis(doc, letter), True, flip
            # Un plan de référence donne la direction de sa normale : 'x' → plan de droite, etc.
            return self._ref_planes(doc)[AXIS_PLANE[letter]], True, flip
        entry = self.registry.resolve(text, None, self._doc_id(doc))
        if entry.kind == "edge":
            return self._entity(self._edge(doc, text)), False, False
        if entry.kind == "face" and circular:
            return self._entity(self._face(doc, text)), False, False
        raise invalid("Direction ou axe invalide : 'x', 'y', 'z' ou handle d'une arête attendu.")

    def _ref_axis(self, doc: Any, letter: str) -> Any:
        """Axe de référence du modèle, créé à la demande à l'intersection de deux plans de référence."""
        wanted = f"Axis_{letter.upper()}"
        for feature in self._features(doc):
            if self._type_name(feature) == k.FEAT_REF_AXIS and self._fname(feature) == wanted:
                return feature
        planes = self._ref_planes(doc)
        first, second = (planes[key] for key in AXIS_PLANES[letter])
        before = self._feature_key(self._last_feature(doc))
        self._clear(doc)
        self._select(first, by_feature=True)
        self._select(second, append=True, by_feature=True)
        # UNVERIFIED: IModelDoc2.InsertAxis2 — aide 2026 — (AutoSize) → booléen ; deux plans présélectionnés
        # sont supposés donner l'axe de leur intersection.
        created = bool(self._m(doc, "InsertAxis2", True))
        self._clear(doc)
        axis = self._after(doc, before) if created else None
        if axis is None or self._type_name(axis) != k.FEAT_REF_AXIS:
            raise SwError(
                ErrorCode.FEATURE_FAILED,
                f"L'axe de référence {wanted} n'a pas pu être créé.",
                hint="Désigner l'axe par le handle d'une arête circulaire ou d'une face cylindrique.",
                details={
                    "feature": wanted,
                    "type": "RefAxis",
                    "sw_error": "InsertAxis2 a échoué",
                    "kept": False,
                },
            )
        self._rename(axis, wanted)
        return axis

    def pattern_linear(
        self, features: list[str], direction: str, count: int, spacing: float, name: str | None
    ) -> FeatureCreated:
        doc, _ = self._part()
        self._no_sketch_open(doc)
        if count < 2 or spacing <= 0:
            raise invalid("Une répétition linéaire exige count >= 2 et un pas strictement positif.")
        seeds = self._seeds(doc, features)
        target, target_is_feature, flip = self._direction_target(doc, direction, circular=False)
        wanted = self._unique_name(doc, "LPattern", name)
        manager = self._fm(doc)
        method = caps.variant("linear_pattern", self.major)

        def select() -> None:
            self._select(target, MARK_PATTERN_DIRECTION, by_feature=target_is_feature)
            for seed in seeds:
                self._select(seed, MARK_PATTERN_SEED, append=True, by_feature=True)

        def build() -> Any:
            # UNVERIFIED: IFeatureManager.FeatureLinearPattern5 — aide 2026 — 22 arguments : Num1, Spacing1,
            # Num2, Spacing2, FlipDir1, FlipDir2, DName1, DName2, GeometryPattern, VaryInstance, HasOffset1,
            # HasOffset2, CtrlByNum1, CtrlByNum2, FromCentroid1, FromCentroid2, RevOffset1, RevOffset2, Offset1,
            # Offset2, D2PatternSeedOnly, SyncSubAssemblies → IFeature ; direction marque 1, fonctions marque 4.
            # FeatureLinearPattern4 (aide 2026) : les 20 premiers.
            args: list[Any] = [
                count, u.mm_to_m(spacing), 1, 0.0, flip, False, "", "", False, False,
                False, False, False, False, False, False, False, False, 0.0, 0.0,
            ]  # fmt: skip
            if method == "FeatureLinearPattern5":
                args += [False, False]
            return self._m(manager, method, *args)

        return self._create(doc, "LPattern", wanted, None, select, build)

    def pattern_circular(
        self, features: list[str], axis: str, count: int, angle_deg: float, name: str | None
    ) -> FeatureCreated:
        doc, _ = self._part()
        self._no_sketch_open(doc)
        if count < 2 or not 0 < angle_deg <= 360:
            raise invalid("Une répétition circulaire exige count >= 2 et un angle total dans ]0, 360].")
        seeds = self._seeds(doc, features)
        target, target_is_feature, flip = self._direction_target(doc, axis, circular=True)
        wanted = self._unique_name(doc, "CPattern", name)
        manager = self._fm(doc)
        method = caps.variant("circular_pattern", self.major)
        # Angle total réparti comme dans le backend simulé : 360° → pas = total / n ; sinon total / (n - 1).
        step = angle_deg / (count if abs(angle_deg - 360.0) < 1e-9 else count - 1)

        def select() -> None:
            self._select(target, MARK_PATTERN_DIRECTION, by_feature=target_is_feature)
            for seed in seeds:
                self._select(seed, MARK_PATTERN_SEED, append=True, by_feature=True)

        def build() -> Any:
            # UNVERIFIED: IFeatureManager.FeatureCircularPattern5 — aide 2026 — 14 arguments : Number, Spacing
            # (radians, pas entre instances quand EqualSpacing est faux), FlipDirection, DName, GeometryPattern,
            # EqualSpacing, VaryInstance, SyncSubAssemblies, BDir2, BSymmetric, Number2, Spacing2, DName2,
            # EqualSpacing2 → IFeature ; axe marque 1, fonctions marque 4. FeatureCircularPattern4 : les 7 premiers.
            args: list[Any] = [count, u.deg_to_rad(step), flip, "", False, False, False]
            if method == "FeatureCircularPattern5":
                args += [False, False, False, 1, 0.0, "", False]
            return self._m(manager, method, *args)

        return self._create(doc, "CPattern", wanted, None, select, build)

    def mirror(self, features: list[str], plane: str, name: str | None) -> FeatureCreated:
        doc, _ = self._part()
        self._no_sketch_open(doc)
        caps.require("mirror_feature2", self.major)
        seeds = self._seeds(doc, features)
        if plane in PLANE_KEYS:
            target, target_is_feature = self._ref_planes(doc)[plane], True
        else:
            face = self._face(doc, plane)
            if not self._is_planar(face):
                raise SwError(ErrorCode.SELECTION_FAILED, "Le plan de symétrie doit être une face plane.")
            target, target_is_feature = self._entity(face), False
        wanted = self._unique_name(doc, "Mirror", name)
        manager = self._fm(doc)

        def select() -> None:
            self._select(target, MARK_MIRROR_PLANE, by_feature=target_is_feature)
            for seed in seeds:
                self._select(seed, MARK_MIRROR_SEED, append=True, by_feature=True)

        def build() -> Any:
            # UNVERIFIED: IFeatureManager.InsertMirrorFeature2 — aide 2026 — (BMirrorBody, BGeometryPattern,
            # BMerge, BKnit, ScopeOptions de swFeatureScope_e) → IFeature ; fonctions marque 1, plan marque 2.
            return self._m(
                manager, "InsertMirrorFeature2", False, False, False, False, C.swFeatureScope_AllBodies
            )

        return self._create(doc, "Mirror", wanted, None, select, build)

    # ---------------------------------------------------------------------------------------------
    # Édition : cotes, variables, renommage, suppression
    # ---------------------------------------------------------------------------------------------
    def _rebuild_quiet(self, doc: Any) -> None:
        # UNVERIFIED: IModelDoc2.EditRebuild3 — aide 2026 — sans argument → booléen ; ne reconstruit que ce
        # qui doit l'être.
        self._m(doc, "EditRebuild3")

    def set_dimension(self, full_name: str, value: float) -> DimensionChange:
        doc, _ = self._part()
        self._no_sketch_open(doc)
        if full_name.count("@") != 1:
            raise invalid(
                f"Nom de cote invalide : {full_name[:80]}",
                hint="Format attendu : <cote>@<fonction>, ex. D1@Base_1.",
            )
        # UNVERIFIED: IModelDoc2.Parameter — aide 2026 — (StringIn « cote@fonction ») → IDimension ou Nothing.
        dimension = self._cast(self._m(doc, "Parameter", full_name), "IDimension")
        if dimension is None:
            raise invalid(
                f"Cote introuvable : {full_name[:80]}",
                hint="Appeler inspect_dimensions pour la liste des cotes.",
            )
        unit = self._dim_unit(dimension)
        if value <= 0:
            raise invalid("La valeur d'une cote doit être strictement positive.")
        old = self._dim_value(dimension, unit)
        self._set_system_value(dimension, u.deg_to_rad(value) if unit == "deg" else u.mm_to_m(value))
        self._rebuild_quiet(doc)
        return DimensionChange(full_name=full_name, old_value=old, new_value=value, unit=unit)

    def set_variable(self, name: str, value: str) -> VariableChange:
        doc = self._doc()
        if not name or '"' in name or '"' in value or "\n" in value:
            raise invalid("Nom ou valeur de variable invalide (guillemets et retours à la ligne interdits).")
        # UNVERIFIED: IModelDoc2.GetEquationMgr — aide 2026 — sans argument → IEquationMgr.
        manager = self._m(doc, "GetEquationMgr")
        equation = f'"{name}" = {value}'
        old: str | None = None
        index = -1
        # UNVERIFIED: IEquationMgr.GetCount — aide 2026 — nombre d'équations.
        for position in range(int(self._m(manager, "GetCount"))):
            # UNVERIFIED: IEquationMgr.Equation — aide 2026 — propriété paramétrée (Index) → texte, sous la forme
            # « "nom" = expression » ; lue comme une méthode en liaison anticipée.
            text = str(self._m(manager, "Equation", position) or "")
            left, _, right = text.partition("=")
            if left.strip().strip('"') == name:
                old, index = right.strip(), position
                break
        if index >= 0:
            # UNVERIFIED: IEquationMgr.Equation (écriture) — aide 2026 — pywin32 génère SetEquation(Index, texte)
            # pour une propriété paramétrée en écriture (convention makepy, de mémoire).
            self._m(manager, "SetEquation", index, equation)
        else:
            # UNVERIFIED: IEquationMgr.Add2 — aide 2026 — (Index, Equation, Solve) → index ou -1 ; -1 en entrée
            # = ajout en fin de liste.
            if int(self._m(manager, "Add2", -1, equation, True)) < 0:
                raise invalid(
                    f"SolidWorks a refusé l'équation {equation[:80]}.", hint="Vérifier la syntaxe et l'unité."
                )
        if self._doc_type(doc) == "drawing" or self._active_sketch(doc) is None:
            self._rebuild_quiet(doc)
        return VariableChange(name=name, old_value=old, new_value=value)

    def rename_feature(self, name: str, new_name: str) -> str:
        doc = self._doc()
        feature = self._feature(doc, name)
        if not new_name.strip():
            raise invalid("Le nouveau nom ne peut pas être vide.")
        wanted = self._unique_name(doc, "", new_name)
        actual = self._rename(feature, wanted)
        if actual != wanted:
            raise invalid(f"SolidWorks a refusé le nom '{wanted[:80]}'.")
        return actual

    def suppress_feature(self, name: str, suppressed: bool) -> bool:
        doc = self._doc()
        self._no_sketch_open(doc)
        feature = self._feature(doc, name)
        self._clear(doc)
        self._select(feature, by_feature=True)
        # UNVERIFIED: IModelDoc2.EditSuppress2 / EditUnsuppress2 — aide 2026 — sans argument → booléen ;
        # agissent sur la fonction sélectionnée, dans la configuration active.
        done = bool(self._m(doc, "EditSuppress2" if suppressed else "EditUnsuppress2"))
        self._clear(doc)
        # UNVERIFIED: IFeature.IsSuppressed — aide 2026 — sans argument → booléen (configuration active).
        state = bool(self._m(feature, "IsSuppressed"))
        if not done and state != suppressed:
            raise SwError(
                ErrorCode.FEATURE_FAILED,
                f"SolidWorks n'a pas pu {'supprimer' if suppressed else 'rétablir'} la fonction '{name[:80]}'.",
                details={
                    "feature": name,
                    "type": self._type_name(feature),
                    "sw_error": "refus",
                    "kept": True,
                },
            )
        return state

    def _children(self, feature: Any, seen: dict[str, None], depth: int = 0) -> None:
        # UNVERIFIED: IFeature.GetChildren — aide 2026 — tableau des fonctions enfants (dépendances directes).
        for raw in _seq(self._m(feature, "GetChildren")):
            child = self._cast(raw, "IFeature")
            child_name = self._fname(child)
            if child_name in seen:
                continue
            seen[child_name] = None
            if depth < 50:
                self._children(child, seen, depth + 1)

    def feature_children(self, name: str) -> list[str]:
        doc = self._doc()
        seen: dict[str, None] = {}
        self._children(self._feature(doc, name), seen)
        seen.pop(name, None)
        return list(seen)

    def delete_feature(self, name: str, delete_children: bool) -> list[str]:
        doc = self._doc()
        self._no_sketch_open(doc)
        feature = self._feature(doc, name)
        victims = [name, *(self.feature_children(name) if delete_children else [])]
        # L'esquisse absorbée est conservée (pas de swDelete_Absorbed), comme dans le backend simulé.
        if not self._delete(doc, feature, C.swDelete_Children if delete_children else 0):
            raise SwError(
                ErrorCode.FEATURE_FAILED,
                f"SolidWorks a refusé de supprimer '{name[:80]}'.",
                hint="La fonction a peut-être des enfants : appeler feature_delete avec delete_children=true.",
                details={
                    "feature": name,
                    "type": "delete",
                    "sw_error": "DeleteSelection2 a renvoyé faux",
                    "kept": True,
                },
            )
        remaining = self._feature_names(doc)
        return [victim for victim in victims if victim not in remaining]

    # ---------------------------------------------------------------------------------------------
    # Inspection : arbre, reconstruction
    # ---------------------------------------------------------------------------------------------
    def _error_of(self, feature: Any) -> tuple[int, bool, str | None]:
        """(code, avertissement, texte) de l'erreur d'une fonction ; code 0 = aucune."""
        # UNVERIFIED: IFeature.GetErrorCode2 — aide 2026 — (ByRef IsWarning) → swFeatureError_e.
        code, is_warning = self._m_out(feature, "GetErrorCode2", [], "b")
        code = int(code or 0)
        if not code:
            return 0, False, None
        return code, bool(is_warning), k.name_of(k.FEATURE_ERROR_PREFIX, code) or f"swFeatureError_e = {code}"

    def _node(self, feature: Any, type_name: str, parent: str | None) -> FeatureNode:
        code, is_warning, text = self._error_of(feature)
        return FeatureNode(
            name=self._fname(feature),
            type=k.FEATURE_TYPE_NAMES.get(type_name, type_name),
            # UNVERIFIED: IFeature.IsSuppressed — aide 2026 — sans argument → booléen (configuration active).
            suppressed=bool(self._m(feature, "IsSuppressed")),
            status="ok" if not code else ("warning" if is_warning else "error"),
            message=text,
            parent=parent,
        )

    def _components(self, doc: Any, resolve: bool) -> list[FeatureNode]:
        """Piège §10 « composants allégés » : signalés, résolus seulement sur demande explicite."""
        assembly = self._cast(doc, "IAssemblyDoc")
        if resolve:
            # UNVERIFIED: IAssemblyDoc.ResolveAllLightWeightComponents — aide 2026 — (WarnUser) → statut de
            # swComponentResolveStatus_e ; faux = pas de dialogue.
            self._m(assembly, "ResolveAllLightWeightComponents", False)
        nodes: list[FeatureNode] = []
        # UNVERIFIED: IAssemblyDoc.GetComponents — aide 2026 — (ToplevelOnly) → tableau de IComponent2.
        for raw in _seq(self._m(assembly, "GetComponents", True)):
            component = self._cast(raw, "IComponent2")
            lightweight: bool | None = None
            if caps.supports("component_suppression2", self.major):
                # UNVERIFIED: IComponent2.GetSuppression2 — aide 2026 — entier de swComponentSuppressionState_e.
                state = int(self._m(component, "GetSuppression2"))
                lightweight = state in (C.swComponentLightweight, C.swComponentFullyLightweight)
            nodes.append(
                FeatureNode(
                    # UNVERIFIED: IComponent2.Name2 — aide 2026 — propriété chaîne (« pièce-1 »).
                    name=str(self._get(component, "Name2")),
                    type="Component",
                    # UNVERIFIED: IComponent2.IsSuppressed — aide 2026 — sans argument → booléen.
                    suppressed=bool(self._m(component, "IsSuppressed")),
                    lightweight=lightweight,
                )
            )
        return nodes

    def tree(self, resolve: bool = False) -> list[FeatureNode]:
        doc = self._doc()
        if self._doc_type(doc) == "assembly":
            return self._components(doc, resolve)
        flat: list[tuple[Any, str, str]] = []
        for feature in self._features(doc):
            type_name = self._type_name(feature)
            if type_name not in k.TREE_NOISE:
                flat.append((feature, type_name, self._fname(feature)))
        subs: dict[str, list[Any]] = {}
        parents: dict[str, str] = {}
        for feature, _, name in flat:
            # UNVERIFIED: IFeature.GetFirstSubFeature / GetNextSubFeature — aide 2026 — sous-fonctions (esquisse
            # absorbée par une extrusion, par exemple), Nothing en fin de liste.
            sub = self._m(feature, "GetFirstSubFeature")
            steps = 0
            while sub is not None and steps < MAX_WALK:
                sub = self._cast(sub, "IFeature")
                subs.setdefault(name, []).append(sub)
                parents.setdefault(self._fname(sub), name)
                sub = self._m(sub, "GetNextSubFeature")
                steps += 1
        listed = {name for _, _, name in flat}
        nodes: list[FeatureNode] = []
        for feature, type_name, name in flat:
            nodes.append(self._node(feature, type_name, parents.get(name)))
            for sub in subs.get(name, []):
                # Une sous-fonction absente du parcours à plat est tout de même listée, sous son parent.
                if self._fname(sub) not in listed:
                    nodes.append(self._node(sub, self._type_name(sub), name))
        return nodes

    def rebuild(self, force: bool = True) -> RebuildResult:
        doc = self._doc()
        drawing = self._doc_type(doc) == "drawing"
        if drawing or self._active_sketch(doc) is None:
            if force:
                # UNVERIFIED: IModelDoc2.ForceRebuild3 — aide 2026 — (TopOnly) → booléen ; faux = tous les niveaux.
                self._m(doc, "ForceRebuild3", False)
            else:
                self._rebuild_quiet(doc)
        errors: list[RebuildIssue] = []
        warnings: list[RebuildIssue] = []
        under: list[str] = []
        dangling: list[str] = []
        for feature in self._features(doc):
            type_name = self._type_name(feature)
            if type_name in k.TREE_NOISE:
                continue
            name = self._fname(feature)
            code, is_warning, text = self._error_of(feature)
            if code:
                issue = RebuildIssue(
                    feature=name,
                    type=k.FEATURE_TYPE_NAMES.get(type_name, type_name),
                    severity="warning" if is_warning else "error",
                    code=code,
                    message=text or "",
                )
                (warnings if is_warning else errors).append(issue)
                if text and "dangl" in text.lower():
                    dangling.append(name)
            if type_name == k.FEAT_SKETCH and not drawing:
                # UNVERIFIED: IFeature.GetSpecificFeature2 — aide 2026 — → ISketch pour une esquisse.
                sketch = self._cast(self._m(feature, "GetSpecificFeature2"), "ISketch")
                if sketch is not None and self._status(sketch) != "fully_defined":
                    under.append(name)
        return RebuildResult(
            rebuild_ok=not errors,
            errors=errors,
            warnings=warnings,
            under_defined_sketches=under,
            dangling_references=dangling,
        )

    # ---------------------------------------------------------------------------------------------
    # Inspection : corps, masse, cotes
    # ---------------------------------------------------------------------------------------------
    def _bodies(self, part: Any) -> list[Any]:
        # UNVERIFIED: IPartDoc.GetBodies2 — aide 2026 — (BodyType de swBodyType_e, BVisibleOnly) → tableau de
        # IBody2, Nothing sans corps.
        return [self._cast(b, "IBody2") for b in _seq(self._m(part, "GetBodies2", C.swSolidBody, True))]

    def _part_box(self, part: Any) -> BBox | None:
        # UNVERIFIED: IPartDoc.GetPartBox — aide 2026 — (NoConversion) → 6 réels, deux coins opposés ; vrai =
        # unités système (mètres). L'aide précise que la boîte est approchée.
        values = self._m(part, "GetPartBox", True)
        return _bbox(_floats(values, 6)) if values is not None else None

    def _mass_properties(self, doc: Any) -> Any:
        method = caps.variant("mass_property", self.major)
        # UNVERIFIED: IModelDocExtension.CreateMassProperty2 / CreateMassProperty — aide 2026 — sans argument →
        # IMassProperty2 / IMassProperty.
        properties = self._m(self._ext(doc), method)
        if properties is None:
            raise SwError(
                ErrorCode.INTERNAL, "SolidWorks n'a pas fourni les propriétés de masse du document."
            )
        properties = self._cast(
            properties, "IMassProperty2" if method == "CreateMassProperty2" else "IMassProperty"
        )
        # UNVERIFIED: IMassProperty2.UseSystemUnits — aide 2026 — propriété booléenne ; vrai = m, m², m³, kg.
        self._put(properties, "UseSystemUnits", True)
        return properties

    def body_stats(self) -> BodyStats:
        doc = self._doc()
        if self._doc_type(doc) != "part":
            return BodyStats()
        part = self._cast(doc, "IPartDoc")
        bodies = self._bodies(part)
        if not bodies:
            return BodyStats()
        # UNVERIFIED: IBody2.GetFaceCount / GetEdgeCount — aide 2026 — sans argument → entier.
        faces = sum(int(self._m(b, "GetFaceCount")) for b in bodies)
        edges = sum(int(self._m(b, "GetEdgeCount")) for b in bodies)
        # UNVERIFIED: IMassProperty2.Volume — aide 2026 — propriété réelle, m³ en unités système.
        volume = u.m3_to_mm3(float(self._get(self._mass_properties(doc), "Volume")))
        return BodyStats(
            body_count=len(bodies),
            volume_mm3=u.rnd(volume),
            face_count=faces,
            edge_count=edges,
            bbox_mm=self._part_box(part),
        )

    def mass(self) -> MassResult:
        """Piège §10 « masse nulle sans matériau » : `mass_g = 0` et `material = None`."""
        doc = self._doc()
        kind = self._doc_type(doc)
        if kind == "drawing":
            raise SwError(
                ErrorCode.WRONG_DOC_TYPE, "Les propriétés de masse exigent une pièce ou un assemblage."
            )
        material = self._material(doc)
        body_count = 0
        box: BBox | None = None
        if kind == "part":
            part = self._cast(doc, "IPartDoc")
            body_count = len(self._bodies(part))
            if body_count == 0:
                return MassResult(
                    volume_mm3=0.0, surface_area_mm2=0.0, mass_g=0.0, center_of_mass_mm=None, bbox_mm=None,
                    material=material, density_g_cm3=None, body_count=0,
                )  # fmt: skip
            box = self._part_box(part)
        properties = self._mass_properties(doc)
        # UNVERIFIED: IMassProperty2.Volume / SurfaceArea / Mass / Density / CenterOfMass — aide 2026 —
        # propriétés en lecture ; m³, m², kg, kg/m³ et tableau [x, y, z] en mètres avec UseSystemUnits.
        volume = u.m3_to_mm3(float(self._get(properties, "Volume")))
        area = u.m2_to_mm2(float(self._get(properties, "SurfaceArea")))
        mass_g: float = u.kg_to_g(float(self._get(properties, "Mass")))
        density: float | None = u.kg_m3_to_g_cm3(float(self._get(properties, "Density")))
        center = u.vec3_m_to_mm(_floats(self._get(properties, "CenterOfMass"), 3))
        if kind == "part" and material is None:
            mass_g, density = 0.0, None  # SolidWorks appliquerait une densité par défaut : valeur trompeuse
        return MassResult(
            volume_mm3=u.rnd(volume),
            surface_area_mm2=u.rnd(area),
            mass_g=u.rnd(mass_g),
            center_of_mass_mm=u.rnd3(center),
            bbox_mm=box,
            material=material,
            density_g_cm3=u.rnd(density) if density is not None else None,
            body_count=body_count,
        )

    def dimensions(self) -> list[DimInfo]:
        doc = self._doc()
        found: dict[str, DimInfo] = {}
        for feature in self._features(doc):
            if self._type_name(feature) in k.TREE_NOISE:
                continue
            for info in self._dims_of(feature):
                found.setdefault(info.full_name, info)
        return list(found.values())

    # ---------------------------------------------------------------------------------------------
    # Inspection : faces, arêtes, mesures
    # ---------------------------------------------------------------------------------------------
    def _surface(self, face: Any) -> Any:
        # UNVERIFIED: IFace2.GetSurface — aide 2026 — sans argument → ISurface.
        return self._cast(self._m(face, "GetSurface"), "ISurface")

    def _is_planar(self, face: Any) -> bool:
        # UNVERIFIED: ISurface.IsPlane — aide 2026 — sans argument → booléen.
        return bool(self._m(self._surface(face), "IsPlane"))

    def _surface_kind(self, surface: Any) -> str:
        # UNVERIFIED: ISurface.IsPlane / IsCylinder / IsCone / IsSphere / IsTorus — aide 2026 — → booléen.
        for member, label in (
            ("IsPlane", "plane"), ("IsCylinder", "cylinder"), ("IsCone", "cone"), ("IsSphere", "sphere"), ("IsTorus", "torus"),
        ):  # fmt: skip
            if self._m(surface, member):
                return label
        return "other"

    def _face_info(self, doc: Any, face: Any) -> FaceInfo:
        surface = self._surface(face)
        kind = self._surface_kind(surface)
        # UNVERIFIED: IFace2.GetArea — aide 2026 — aire en m².
        area = u.m2_to_mm2(float(self._m(face, "GetArea")))
        # UNVERIFIED: IFace2.GetBox — aide 2026 — 6 réels, deux coins opposés de la boîte de la face (mètres).
        box = _floats(self._m(face, "GetBox"), 6)
        point: Vec3 = ((box[0] + box[3]) / 2, (box[1] + box[4]) / 2, (box[2] + box[5]) / 2)
        normal: Vec3 | None = None
        radius: float | None = None
        if kind == "plane":
            # UNVERIFIED: IFace2.Normal — aide 2026 — tableau de 3 réels ; (0, 0, 0) si la face n'est pas plane ;
            # supposé orienté vers l'extérieur de la matière.
            nx, ny, nz = _floats(self._get(face, "Normal"), 3)
            if g.norm((nx, ny, nz)) > 1e-9:
                normal = u.rnd3(g.unit((nx, ny, nz)))
            # UNVERIFIED: IFace2.GetClosestPointOn — aide 2026 — (X, Y, Z) → 5 réels dont le point de la face
            # le plus proche. Le « centroïde » publié est le centre de la boîte ramené sur la face :
            # exact pour un rectangle, approché sinon.
            closest = self._m(face, "GetClosestPointOn", *point)
            if closest is not None:
                px, py, pz = _floats(closest, 3)
                point = (px, py, pz)
        elif kind == "cylinder":
            # UNVERIFIED: ISurface.CylinderParams — aide 2026 — 7 réels : origine, axe, rayon (mètres).
            radius = u.rnd(u.m_to_mm(_floats(self._get(surface, "CylinderParams"), 7)[6]))
        # UNVERIFIED: IFace2.GetFeature — aide 2026 — sans argument → fonction propriétaire de la face.
        owner = self._m(face, "GetFeature")
        return FaceInfo(
            handle=self._register("face", doc, face),
            surface=kind,
            area_mm2=u.rnd(area),
            centroid_mm=u.rnd3(u.vec3_m_to_mm(point)),
            normal=normal,
            radius_mm=radius,
            feature=self._fname(self._cast(owner, "IFeature")) if owner is not None else None,
        )

    def faces(self) -> list[FaceInfo]:
        doc, part = self._part()
        out: list[FaceInfo] = []
        for body in self._bodies(part):
            # UNVERIFIED: IBody2.GetFaces — aide 2026 — sans argument → tableau de IFace2.
            for raw in _seq(self._m(body, "GetFaces")):
                out.append(self._face_info(doc, self._cast(raw, "IFace2")))
        return out

    def _edge_info(self, doc: Any, edge: Any) -> EdgeInfo:
        # UNVERIFIED: IEdge.GetCurve — aide 2026 — sans argument → ICurve.
        curve = self._cast(self._m(edge, "GetCurve"), "ICurve")
        # UNVERIFIED: IEdge.GetCurveParams3 — aide 2026 — sans argument → ICurveParamData.
        params = self._cast(self._m(edge, "GetCurveParams3"), "ICurveParamData")
        # UNVERIFIED: ICurveParamData.UMinValue / UMaxValue / StartPoint / EndPoint — aide 2026 — propriétés :
        # bornes du paramètre et extrémités (3 réels, mètres).
        u_min = float(self._get(params, "UMinValue"))
        u_max = float(self._get(params, "UMaxValue"))
        sx, sy, sz = _floats(self._get(params, "StartPoint"), 3)
        ex, ey, ez = _floats(self._get(params, "EndPoint"), 3)
        start, end = u.vec3_m_to_mm((sx, sy, sz)), u.vec3_m_to_mm((ex, ey, ez))
        # UNVERIFIED: ICurve.GetLength3 — aide 2026 — (StartParam, EndParam) → longueur en mètres.
        length = u.m_to_mm(float(self._m(curve, "GetLength3", u_min, u_max)))
        # UNVERIFIED: ICurve.Evaluate2 — aide 2026 — (Parameter, NumberOfDerivatives) → tableau commençant
        # par le point [x, y, z] en mètres.
        mx, my, mz = _floats(self._m(curve, "Evaluate2", (u_min + u_max) / 2, 0), 3)
        middle = u.vec3_m_to_mm((mx, my, mz))
        kind = "other"
        center: Vec3 | None = None
        radius: float | None = None
        # UNVERIFIED: ICurve.IsLine / IsCircle — aide 2026 — sans argument → booléen.
        if self._m(curve, "IsLine"):
            kind = "line"
        elif self._m(curve, "IsCircle"):
            # UNVERIFIED: ICurve.CircleParams — aide 2026 — 7 réels : centre, axe, rayon (mètres).
            values = _floats(self._get(curve, "CircleParams"), 7)
            center = u.vec3_m_to_mm(values[:3])
            radius = u.rnd(u.m_to_mm(values[6]))
            kind = "circle" if g.dist(start, end) < 1e-6 else "arc"
        closed = kind == "circle"
        adjacent: list[str] = []
        # UNVERIFIED: IEdge.GetTwoAdjacentFaces2 — aide 2026 — sans argument → tableau de deux IFace2.
        for raw in _seq(self._m(edge, "GetTwoAdjacentFaces2")):
            adjacent.append(self._register("face", doc, self._cast(raw, "IFace2")))
        return EdgeInfo(
            handle=self._register("edge", doc, edge),
            curve=kind,
            length_mm=u.rnd(length),
            midpoint_mm=u.rnd3(center if closed and center is not None else middle),
            start_mm=None if closed else u.rnd3(start),
            end_mm=None if closed else u.rnd3(end),
            center_mm=u.rnd3(center) if center is not None else None,
            radius_mm=radius,
            faces=adjacent,
        )

    def edges(self) -> list[EdgeInfo]:
        doc, part = self._part()
        caps.require("curve_params3", self.major)
        out: list[EdgeInfo] = []
        for body in self._bodies(part):
            # UNVERIFIED: IBody2.GetEdges — aide 2026 — sans argument → tableau de IEdge.
            for raw in _seq(self._m(body, "GetEdges")):
                out.append(self._edge_info(doc, self._cast(raw, "IEdge")))
        return out

    def _operand(self, doc: Any, value: str | Vec3) -> tuple[Any, Vec3 | None, str]:
        """Opérande de mesure → (objet COM ou None, point en mm ou None, libellé)."""
        if not isinstance(value, str):
            x, y, z = (float(c) for c in value)
            return None, (x, y, z), "point"
        entry = self.registry.resolve(value, None, self._doc_id(doc))
        if entry.kind == "face":
            face = self._face(doc, value)
            return face, None, f"face {self._surface_kind(self._surface(face))}"
        if entry.kind == "edge":
            edge = self._edge(doc, value)
            curve = self._cast(self._m(edge, "GetCurve"), "ICurve")  # UNVERIFIED: IEdge.GetCurve — aide 2026.
            straight = bool(self._m(curve, "IsLine"))  # UNVERIFIED: ICurve.IsLine — aide 2026.
            return edge, None, "arête droite" if straight else "arête courbe"
        raise invalid("inspect_measure accepte des handles de face ou d'arête, ou des points [x, y, z].")

    def measure(self, a: str | Vec3, b: str | Vec3) -> MeasureResult:
        doc, _ = self._part()
        obj_a, point_a, label_a = self._operand(doc, a)
        obj_b, point_b, label_b = self._operand(doc, b)
        angle: float | None = None
        if point_a is not None and point_b is not None:
            distance = g.dist(point_a, point_b)
        elif point_a is not None or point_b is not None:
            point = point_a if point_a is not None else point_b
            entity = obj_b if point_a is not None else obj_a
            assert point is not None
            # UNVERIFIED: IFace2.GetClosestPointOn / IEdge.GetClosestPointOn — aide 2026 — (X, Y, Z en mètres)
            # → 5 réels commençant par le point le plus proche.
            closest = self._m(entity, "GetClosestPointOn", *u.vec3_mm_to_m(point))
            cx, cy, cz = _floats(closest, 3)
            distance = g.dist(point, u.vec3_m_to_mm((cx, cy, cz)))
        else:
            caps.require("measure", self.major)
            # UNVERIFIED: IModelDocExtension.CreateMeasure — aide 2026 — sans argument → IMeasure.
            tool = self._m(self._ext(doc), "CreateMeasure")
            # UNVERIFIED: IMeasure.Calculate — aide 2026 — (Entities : tableau d'entités, ou Nothing pour la
            # sélection) → booléen ; tableau passé en VT_ARRAY|VT_DISPATCH.
            if not self._m(tool, "Calculate", self._objects([obj_a, obj_b])):
                raise invalid("SolidWorks ne sait pas mesurer cette combinaison d'entités.")
            # UNVERIFIED: IMeasure.Distance / IMeasure.Angle — aide 2026 — distance minimale (m) et angle (rad),
            # -1 quand la grandeur n'a pas de sens pour ces entités.
            raw_distance = float(self._get(tool, "Distance"))
            raw_angle = float(self._get(tool, "Angle"))
            if raw_distance < 0:
                raise invalid("Distance indéfinie pour ces entités.")
            distance = u.m_to_mm(raw_distance)
            angle = u.rnd(u.rad_to_deg(raw_angle)) if raw_angle >= 0 else None
        return MeasureResult(distance_mm=u.rnd(distance), angle_deg=angle, a=label_a, b=label_b)

    def interference(self) -> list[InterferencePair]:
        doc = self._doc()
        if self._doc_type(doc) != "assembly":
            raise SwError(ErrorCode.WRONG_DOC_TYPE, "La détection d'interférences exige un assemblage.")
        caps.require("interference_manager", self.major)
        # UNVERIFIED: IAssemblyDoc.InterferenceDetectionManager — aide 2026 — propriété en lecture.
        manager = self._get(self._cast(doc, "IAssemblyDoc"), "InterferenceDetectionManager")
        out: list[InterferencePair] = []
        try:
            # UNVERIFIED: IInterferenceDetectionMgr.GetInterferences — aide 2026 — tableau de IInterference.
            for raw in _seq(self._m(manager, "GetInterferences")):
                item = self._cast(raw, "IInterference")
                # UNVERIFIED: IInterference.Components / Volume — aide 2026 — tableau de IComponent2 ; volume en m³.
                names = [
                    str(self._get(self._cast(c, "IComponent2"), "Name2"))
                    for c in _seq(self._get(item, "Components"))
                ]
                volume = u.m3_to_mm3(float(self._get(item, "Volume")))
                if len(names) >= 2:
                    out.append(InterferencePair(a=names[0], b=names[1], volume_mm3=u.rnd(volume)))
        finally:
            if self._app is not None:
                # UNVERIFIED: IInterferenceDetectionMgr.Done — aide 2026 — sans argument, clôt la détection.
                self._m(manager, "Done")
        return out

    # ---------------------------------------------------------------------------------------------
    # Captures et export
    # ---------------------------------------------------------------------------------------------
    def _shoot(self, doc: Any, width_px: int) -> bytes:
        from PIL import Image

        height = max(1, width_px * 3 // 4)
        handle, path = tempfile.mkstemp(prefix="swmcp-", suffix=".bmp")
        os.close(handle)
        try:
            # UNVERIFIED: IModelDoc2.SaveBMP — aide 2026 — (FileNameIn, WidthIn, HeightIn) → booléen ; écrit la
            # vue du modèle, sans l'interface.
            if not self._m(doc, "SaveBMP", path, width_px, height) or os.path.getsize(path) == 0:
                raise SwError(
                    ErrorCode.INTERNAL,
                    "SolidWorks n'a pas produit l'image de la vue.",
                    hint="Vérifier que la fenêtre du document est visible et qu'aucun dialogue n'est ouvert.",
                )
            with Image.open(path) as image:
                buffer = io.BytesIO()
                image.convert("RGB").save(buffer, format="PNG", optimize=True)
            return buffer.getvalue()
        finally:
            with contextlib.suppress(OSError):
                os.remove(path)

    def capture(self, views: list[str], width_px: int, zoom_to_fit: bool) -> list[bytes]:
        """Une image PNG par vue ; l'orientation de l'utilisateur est restaurée (spec §6)."""
        doc = self._doc()
        unknown = [v for v in views if v != "current" and v not in VIEW_CONSTANTS]
        if unknown or not views:
            raise invalid(f"Vue inconnue : {', '.join(v[:20] for v in unknown) or '(aucune)'}")
        # UNVERIFIED: IModelDoc2.ActiveView — aide 2026 — propriété, IModelView de la vue active.
        view = self._cast(self._get(doc, "ActiveView"), "IModelView")
        saved: tuple[Any, Any, Any] | None = None
        if view is not None:
            # UNVERIFIED: IModelView.Orientation3 / Translation3 / Scale2 — aide 2026 — propriétés en lecture et
            # écriture (IMathTransform, IMathVector, réel).
            saved = (
                self._get(view, "Orientation3"),
                self._get(view, "Translation3"),
                self._get(view, "Scale2"),
            )
        images: list[bytes] = []
        try:
            for name in views:
                if name != "current":
                    # UNVERIFIED: IModelDoc2.ShowNamedView2 — aide 2026 — (VName, ViewId de swStandardViews_e),
                    # sans retour ; nom vide = ViewId seul.
                    self._m(doc, "ShowNamedView2", "", getattr(C, VIEW_CONSTANTS[name]))
                if zoom_to_fit:
                    self._m(
                        doc, "ViewZoomtofit2"
                    )  # UNVERIFIED: IModelDoc2.ViewZoomtofit2 — aide 2026 — sans argument.
                images.append(self._shoot(doc, width_px))
        finally:
            if saved is not None and self._app is not None:
                self._put(view, "Orientation3", saved[0])
                self._put(view, "Translation3", saved[1])
                self._put(view, "Scale2", saved[2])
                self._m(
                    doc, "GraphicsRedraw2"
                )  # UNVERIFIED: IModelDoc2.GraphicsRedraw2 — aide 2026 — sans argument.
        return images

    def export(self, fmt: str, path: str, options: dict[str, Any]) -> None:
        doc = self._doc()
        extensions = k.EXPORT_EXTENSIONS.get(fmt)
        if extensions is None:
            raise invalid(f"Format d'export inconnu : {fmt[:20]}")
        if os.path.splitext(path)[1].lower() not in extensions:
            raise invalid(f"L'extension du fichier doit être {' ou '.join(extensions)} pour le format {fmt}.")
        kind = self._doc_type(doc)
        if fmt == "png":
            width = max(64, min(int(options.get("width_px", 1024)), 4096))
            with open(path, "wb") as stream:
                stream.write(self.capture([str(options.get("view", "iso"))], width, True)[0])
            return
        if fmt in ("pdf", "dxf") and kind != "drawing":
            raise SwError(
                ErrorCode.WRONG_DOC_TYPE,
                f"L'export {fmt} s'applique à une mise en plan ; document actif : {kind}.",
            )
        if fmt in ("step", "stl", "iges") and kind == "drawing":
            raise SwError(ErrorCode.WRONG_DOC_TYPE, f"L'export {fmt} exige une pièce ou un assemblage.")
        if kind != "drawing":
            self._no_sketch_open(doc)
        # Les options propres à chaque format (protocole STEP, STL binaire…) restent celles de l'utilisateur.
        self._save_as(doc, path, C.swSaveAsOptions_Silent)
        if not os.path.isfile(path):
            raise SwError(
                ErrorCode.INTERNAL,
                f"SolidWorks a signalé un export réussi mais {os.path.basename(path)} est absent.",
                hint="Vérifier les dialogues ouverts dans SolidWorks et les droits d'écriture du dossier.",
            )

    # ---------------------------------------------------------------------------------------------
    # Échappatoire (désactivée par défaut ; la liste blanche est appliquée par l'appelant)
    # ---------------------------------------------------------------------------------------------
    def raw_call(self, target: str, method: str, args: list[Any]) -> Any:
        if target not in RAW_TARGETS:
            raise invalid(f"Cible inconnue : {target[:20]}", hint="Cibles admises : app, doc, ext.")
        if not method.isidentifier() or method.startswith("_") or "macro" in method.lower():
            # Défense en profondeur : jamais de membre privé ni d'exécution de macro, quelle que soit la liste.
            raise invalid(f"Membre refusé : {method[:60]}")
        if any(not (a is None or isinstance(a, (bool, int, float, str))) for a in args):
            raise invalid("Seuls des arguments simples (nombre, chaîne, booléen) sont admis.")
        if target == "app":
            obj = self._application()
        else:
            doc = self._doc()
            obj = doc if target == "doc" else self._ext(doc)
        # UNVERIFIED: api_call_raw — aide non consultée : membre choisi par l'appelant — appel direct du membre
        # nommé ; un membre sans argument est lu comme une propriété s'il n'est pas appelable.
        return _clean(self._m(obj, method, *args))
