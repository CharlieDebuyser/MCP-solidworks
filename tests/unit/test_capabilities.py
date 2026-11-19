"""Table de capacités, constantes et noyau COM du backend réel, sans SolidWorks ni pywin32."""

from __future__ import annotations

import importlib
import inspect
import sys
from collections.abc import Iterator
from typing import Any

import pytest

from swmcp.backend import capabilities as caps
from swmcp.backend import constants as k
from swmcp.backend.protocol import SwBackend
from swmcp.envelope import ErrorCode, SwError


# --- RevisionNumber ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("revision", "expected"),
    [
        ("33.1.0", (33, 1, 0)),
        ("31.0.0", (31, 0, 0)),
        ("32.5.1", (32, 5, 1)),
        (" 33.2 ", (33, 2, 0)),
        ("33", (33, 0, 0)),
        ("23.-3.0", (23, -3, 0)),  # pré-version (API Help, ISldWorks::RevisionNumber)
        ("33.1.0.4", (33, 1, 0)),
    ],
)
def test_parse_revision(revision: str, expected: tuple[int, int, int]) -> None:
    assert caps.parse_revision(revision) == expected


@pytest.mark.parametrize("revision", ["", "abc", "33.x.0", "."])
def test_parse_revision_rejects_garbage(revision: str) -> None:
    with pytest.raises(SwError) as err:
        caps.parse_revision(revision)
    assert err.value.code is ErrorCode.INTERNAL


@pytest.mark.parametrize(("major", "year"), [(31, 2023), (32, 2024), (33, 2025), (34, 2026), (8, 2000)])
def test_sw_year(major: int, year: int) -> None:
    assert caps.sw_year(major) == year


# --- capacités --------------------------------------------------------------------------------
def test_recent_versions_support_everything() -> None:
    for major in (31, 32, 33, 34):
        assert all(caps.summary(major).values()), major
        for cap in caps.CAPABILITIES:
            caps.require(cap, major)  # ne lève pas


def test_require_raises_unsupported_version() -> None:
    assert not caps.supports("fillet_definition", 27)
    with pytest.raises(SwError) as err:
        caps.require("fillet_definition", 27)
    assert err.value.code is ErrorCode.UNSUPPORTED_VERSION
    assert err.value.retryable is False
    assert err.value.details == {"capability": "fillet_definition", "min_major": 28, "major": 27}
    assert "2020" in err.value.message and "2019" in err.value.message


def test_unknown_capability_is_an_internal_error() -> None:
    with pytest.raises(SwError) as err:
        caps.require("teleportation", 33)
    assert err.value.code is ErrorCode.INTERNAL
    with pytest.raises(SwError):
        caps.variant("teleportation", 33)


@pytest.mark.parametrize(
    ("family", "major", "method"),
    [
        ("feature_cut", 33, "FeatureCut4"),
        ("feature_cut", 25, "FeatureCut4"),
        ("feature_cut", 24, "FeatureCut3"),
        ("save_as", 33, "SaveAs3"),
        ("save_as", 28, "SaveAs"),  # SaveAs3 date de 2020 SP02 : compté à partir de 2021
        ("fillet", 33, "CreateFeature"),
        ("fillet", 27, "FeatureFillet3"),
        ("linear_pattern", 33, "FeatureLinearPattern5"),
        ("linear_pattern", 24, "FeatureLinearPattern4"),
        ("circular_pattern", 33, "FeatureCircularPattern5"),
        ("circular_pattern", 22, "FeatureCircularPattern4"),
        ("mass_property", 33, "CreateMassProperty2"),
        ("mass_property", 27, "CreateMassProperty"),
        ("custom_property_get", 33, "Get6"),
        ("custom_property_get", 25, "Get5"),
    ],
)
def test_variant_by_version(family: str, major: int, method: str) -> None:
    assert caps.variant(family, major) == method


def test_variant_too_old() -> None:
    with pytest.raises(SwError) as err:
        caps.variant("fillet", 20)
    assert err.value.code is ErrorCode.UNSUPPORTED_VERSION
    assert err.value.details is not None and err.value.details["family"] == "fillet"


def test_variant_table_is_consistent() -> None:
    for family, candidates in caps.VARIANTS.items():
        minimums = [caps.CAPABILITIES[cap] for _, cap in candidates]
        assert minimums == sorted(minimums, reverse=True), (
            f"{family} : variantes à lister de la plus récente à la plus ancienne"
        )
        assert len({method for method, _ in candidates}) == len(candidates)
    assert all(isinstance(v, int) and v > 0 for v in caps.CAPABILITIES.values())
    assert caps.MIN_SUPPORTED_MAJOR == 31 and caps.sw_year(caps.MIN_SUPPORTED_MAJOR) == 2023


# --- constantes -------------------------------------------------------------------------------
@pytest.fixture
def clean_constants() -> Iterator[None]:
    k.reset()
    yield
    k.reset()


def test_constants_are_read_by_name_never_copied(clean_constants: None) -> None:
    assert len(set(k.REQUIRED)) == len(k.REQUIRED)
    assert not k.is_loaded() and k.missing() == list(k.REQUIRED)
    with pytest.raises(SwError) as err:
        _ = k.C.swDocPART
    assert err.value.code is ErrorCode.INTERNAL
    k.install({name: index for index, name in enumerate(k.REQUIRED)})
    assert k.is_loaded() and k.missing() == []
    assert k.C.swDocPART == k.REQUIRED.index("swDocPART")
    with pytest.raises(SwError):
        _ = k.C.swInexistant


def test_constants_names_and_flags(clean_constants: None) -> None:
    k.install(
        {
            "swFileLoadWarning_ReadOnly": 2,
            "swFileLoadWarning_NeedsRegen": 32,
            "swFileNotFoundError": 2,
            "swGenericError": 1,
            "swFeatureErrorNone": 0,
            "swFeatureErrorUnknown": 5,
        }
    )
    assert k.name_of(k.FEATURE_ERROR_PREFIX, 5) == "swFeatureErrorUnknown"
    assert k.name_of(k.FEATURE_ERROR_PREFIX, 99) is None
    assert k.flags_of(0, names=k.FILE_LOAD_ERRORS) == []
    assert k.flags_of(3, names=k.FILE_LOAD_ERRORS) == ["swFileNotFoundError", "swGenericError"]
    assert k.flags_of(34, prefix=k.FILE_LOAD_WARNING_PREFIX) == [
        "swFileLoadWarning_NeedsRegen",
        "swFileLoadWarning_ReadOnly",
    ]
    assert k.flags_of(64, prefix=k.FILE_LOAD_WARNING_PREFIX) == ["code 0x40"]


def test_constants_load_without_pywin32(clean_constants: None) -> None:
    if importlib.util.find_spec("win32com") is not None:
        pytest.skip("pywin32 est installé : le chemin sans pywin32 ne peut pas être testé ici")
    with pytest.raises(SwError) as err:
        k.load()
    assert err.value.code is ErrorCode.INTERNAL and "pywin32" in err.value.message


def test_relation_and_type_tables() -> None:
    public = {
        "horizontal",
        "vertical",
        "coincident",
        "tangent",
        "equal",
        "parallel",
        "perpendicular",
        "concentric",
    }
    public |= {"symmetric", "fix", "midpoint"}
    assert public <= set(k.RELATION_IDS)
    assert all(value.startswith("sg") for value in k.RELATION_IDS.values())
    assert set(k.TEMPLATE_PREFS) == set(k.DOC_EXTENSIONS) == {"part", "assembly", "drawing"}
    assert set(k.TEMPLATE_PREFS.values()) <= set(k.REQUIRED)
    assert k.FEATURE_TYPE_NAMES[k.FEAT_SKETCH] == "Sketch"


# --- import et conformité au Protocol ---------------------------------------------------------
def test_com_modules_import_without_pywin32() -> None:
    had = {name for name in ("pythoncom", "pywintypes", "win32com") if name in sys.modules}
    module = importlib.import_module("swmcp.backend.com_backend")
    importlib.import_module("swmcp.backend.constants")
    assert hasattr(module, "ComSwBackend")
    loaded = {name for name in ("pythoncom", "pywintypes", "win32com") if name in sys.modules}
    assert loaded == had, "l'import du backend COM ne doit pas charger pywin32"


def _public_methods(cls: type) -> dict[str, Any]:
    return {name: value for name, value in vars(cls).items() if callable(value) and not name.startswith("_")}


def test_com_backend_implements_the_protocol_exactly() -> None:
    from swmcp.backend.com_backend import ComSwBackend

    expected = _public_methods(SwBackend)
    assert len(expected) >= 50
    for name, declared in expected.items():
        implemented = getattr(ComSwBackend, name, None)
        assert callable(implemented), f"méthode manquante : {name}"
        assert str(inspect.signature(implemented)) == str(inspect.signature(declared)), name
    backend = ComSwBackend()  # constructeur sans argument obligatoire
    assert isinstance(backend, SwBackend)
    assert type(backend.registry).__name__ == "EntityRegistry"


def test_com_backend_adds_no_public_method_beyond_the_protocol() -> None:
    from swmcp.backend.com_backend import ComSwBackend

    assert set(_public_methods(ComSwBackend)) == set(_public_methods(SwBackend))


# --- noyau COM : traduction des HRESULT, reprises, sorties par référence ----------------------
class com_error(Exception):  # même nom que pywintypes.com_error : reconnu par son nom
    def __init__(self, hresult: int, text: str = "", excepinfo: tuple[Any, ...] | None = None) -> None:
        super().__init__(hresult, text, excepinfo, None)
        self.hresult = hresult
        self.strerror = text
        self.excepinfo = excepinfo


def _signed(code: int) -> int:
    return code - (1 << 32) if code & 0x80000000 else code


@pytest.fixture
def backend() -> Any:
    from swmcp.backend.com_backend import ComSwBackend

    b = ComSwBackend(busy_delays=(0.0, 0.0))
    b._app = object()
    return b


def test_not_connected_is_safe() -> None:
    from swmcp.backend.com_backend import ComSwBackend

    b = ComSwBackend()
    assert b.is_connected() is False
    assert b.state().doc is None  # ne lève jamais
    b.cleanup()  # ne lève jamais
    b.shutdown()
    with pytest.raises(SwError) as err:
        b.ping()
    assert err.value.code is ErrorCode.NOT_CONNECTED


def test_hresult_of_recognises_com_errors_only() -> None:
    from swmcp.backend.com_backend import hresult_of, is_rejected

    assert hresult_of(ValueError("x")) is None
    assert hresult_of(com_error(_signed(0x80010001))) == 0x80010001
    assert is_rejected(com_error(_signed(0x8001010A))) and not is_rejected(com_error(_signed(0x80004005)))
    wrapped = com_error(_signed(0x80020009), "Exception", (0, "src", "texte", None, 0, _signed(0x800706BA)))
    assert hresult_of(wrapped) == 0x800706BA


@pytest.mark.parametrize("code", [0x80010001, 0x8001010A])
def test_rejected_call_is_retried_then_busy(backend: Any, code: int) -> None:
    calls: list[int] = []

    def rejected() -> None:
        calls.append(1)
        raise com_error(_signed(code), "appel refusé")

    with pytest.raises(SwError) as err:
        backend._com(rejected)
    assert err.value.code is ErrorCode.SW_BUSY and err.value.retryable
    assert len(calls) == 3  # un essai + un par délai
    assert backend.is_connected()


def test_rejected_call_succeeds_on_retry(backend: Any) -> None:
    attempts = iter([com_error(_signed(0x80010001)), None])

    def flaky() -> str:
        failure = next(attempts)
        if failure is not None:
            raise failure
        return "ok"

    assert backend._com(flaky) == "ok"


@pytest.mark.parametrize("code", [0x80010108, 0x800706BA, 0x800706BE])
def test_lost_instance_invalidates_everything(backend: Any, code: int) -> None:
    handle = backend.registry.register("face", "doc1", b"\x01\x02")
    calls: list[int] = []

    def gone() -> None:
        calls.append(1)
        raise com_error(_signed(code), "serveur RPC indisponible")

    with pytest.raises(SwError) as err:
        backend._com(gone, label="GetTitle")
    assert err.value.code is ErrorCode.SW_DISCONNECTED
    assert err.value.details == {"call": "GetTitle", "hresult": f"0x{code:08X}"}
    assert len(calls) == 1, "un appel perdu n'est jamais rejoué"
    assert not backend.is_connected() and len(backend.registry) == 0
    with pytest.raises(SwError) as stale:
        backend.registry.resolve(handle)
    assert stale.value.code is ErrorCode.ENTITY_STALE


def test_other_com_error_is_internal_with_hresult(backend: Any) -> None:
    def broken() -> None:
        raise com_error(_signed(0x80020005), "Type mismatch")

    with pytest.raises(SwError) as err:
        backend._com(broken, label="FeatureExtrusion3")
    assert err.value.code is ErrorCode.INTERNAL
    assert err.value.details == {"call": "FeatureExtrusion3", "hresult": "0x80020005"}
    assert backend.is_connected()


def test_python_error_in_a_com_call_is_internal(backend: Any) -> None:
    with pytest.raises(SwError) as err:
        backend._m(object(), "MembreAbsent")
    assert err.value.code is ErrorCode.INTERNAL
    assert err.value.details == {"call": "MembreAbsent", "exception": "AttributeError"}
    with pytest.raises(SwError):
        backend._m(None, "GetTitle")


def test_sw_error_passes_through_unchanged(backend: Any) -> None:
    def refuse() -> None:
        raise SwError(ErrorCode.INVALID_INPUT, "non")

    with pytest.raises(SwError) as err:
        backend._com(refuse)
    assert err.value.code is ErrorCode.INVALID_INPUT


def test_out_parameters_with_early_binding(backend: Any) -> None:
    class Generated:
        CLSID = "{00000000-0000-0000-0000-000000000000}"  # marque d'une classe makepy

        def OpenDoc6(
            self, name: str, kind: int, options: int, config: str, errors: int, warnings: int
        ) -> Any:
            assert (errors, warnings) == (0, 0)
            return ("document", 2, 32)

        def Save3(self, options: int, errors: int, warnings: int) -> Any:
            return True  # liaison dégradée : une seule valeur

    assert backend._m_out(Generated(), "OpenDoc6", ["a.sldprt", 1, 3, ""], "ii") == ("document", 2, 32)
    assert backend._m_out(Generated(), "Save3", [1], "ii") == (True, None, None)


def test_raw_call_refuses_macros_and_private_members(backend: Any) -> None:
    for method in ("RunMacro2", "_oleobj_", "Run Macro", "runmacro"):
        with pytest.raises(SwError) as err:
            backend.raw_call("app", method, [])
        assert err.value.code is ErrorCode.INVALID_INPUT
    with pytest.raises(SwError):
        backend.raw_call("fichier", "GetTitle", [])
    with pytest.raises(SwError):
        backend.raw_call("app", "GetTitle", [object()])


def test_raw_call_returns_serialisable_values(backend: Any) -> None:
    class App:
        def RevisionNumber(self) -> str:
            return "33.1.0"

        def Tableau(self) -> Any:
            return (1, 2.5, ("a", object()), b"\x01\xff")

    backend._app = App()
    assert backend.raw_call("app", "RevisionNumber", []) == "33.1.0"
    values = backend.raw_call("app", "Tableau", [])
    assert values[:2] == [1, 2.5] and values[2][0] == "a" and values[2][1].startswith("<objet COM")
    assert values[3] == "01ff"
