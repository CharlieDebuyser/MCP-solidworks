"""Table de capacités par version de SolidWorks (spec §10 « Suffixes de version des méthodes »).

`RevisionNumber` est lu à la connexion ; ce module, purement Python, dit quelles méthodes de l'API
existent dans la version connectée et quelle variante suffixée (`...2`, `...3`) appeler.

Les numéros de version viennent de la ligne « Availability » de l'API Help SOLIDWORKS 2026
(help.solidworks.com), consultée le 2026-11-19. Une méthode introduite dans un service pack
(ex. `SaveAs3`, 2020 SP02) est comptée à partir de la version majeure suivante, pour qu'un seul
entier suffise.
"""

from __future__ import annotations

from ..envelope import ErrorCode, SwError

# Révision majeure 8 = SOLIDWORKS 2000 (API Help, ISldWorks::RevisionNumber) ; +1 par version annuelle.
_MAJOR_OF_YEAR_2000 = 8
# Versions visées par la v1 (spec §1 et §14) : 2023 et suivantes.
MIN_SUPPORTED_MAJOR = 31


def parse_revision(revision: str) -> tuple[int, int, int]:
    """`"33.1.0"` → `(33, 1, 0)` : majeure, service pack, correctif.

    Les pré-versions renvoient un service pack négatif (`"23.-3.0"`), accepté tel quel.
    """
    parts = [p.strip() for p in str(revision).strip().split(".")]
    if not parts or not parts[0]:
        raise SwError(ErrorCode.INTERNAL, f"RevisionNumber illisible : {str(revision)[:40]!r}")
    numbers: list[int] = []
    for part in parts[:3]:
        try:
            numbers.append(int(part))
        except ValueError as exc:
            raise SwError(ErrorCode.INTERNAL, f"RevisionNumber illisible : {str(revision)[:40]!r}") from exc
    while len(numbers) < 3:
        numbers.append(0)
    return numbers[0], numbers[1], numbers[2]


def sw_year(major: int) -> int:
    """Année commerciale d'une révision majeure : 31 → 2023, 32 → 2024, 33 → 2025."""
    return major - _MAJOR_OF_YEAR_2000 + 2000


# Capacité → révision majeure minimale.
CAPABILITIES: dict[str, int] = {
    # sélection et références persistantes
    "persist_reference3": 16,  # IModelDocExtension::GetPersistReference3, 2007 SP3 (15.3)
    "select_by_id2": 13,  # IModelDocExtension::SelectByID2, 2005
    # documents
    "open_doc6": 10,  # ISldWorks::OpenDoc6, 2001Plus
    "activate_doc3": 20,  # ISldWorks::ActivateDoc3, 2012
    "save_as": 16,  # IModelDocExtension::SaveAs, 2007 SP1 (15.1)
    "save_as3": 29,  # IModelDocExtension::SaveAs3, 2020 SP02 (28.2)
    "custom_property_add3": 22,  # ICustomPropertyManager::Add3 / Delete2 / Get5, 2014
    "custom_property_get6": 26,  # ICustomPropertyManager::Get6, 2018
    # esquisse
    "sketch_manager": 16,  # ISketchManager::CreateLine…, 2008
    "sketch_relation_manager": 12,  # ISketch::RelationManager, 2004
    # fonctions
    "feature_extrusion3": 22,  # IFeatureManager::FeatureExtrusion3, 2014
    "feature_cut3": 18,  # IFeatureManager::FeatureCut3, 2009 SP4 (17.4)
    "feature_cut4": 25,  # IFeatureManager::FeatureCut4, 2017
    "feature_revolve2": 19,  # IFeatureManager::FeatureRevolve2, 2011
    "feature_fillet3": 23,  # IFeatureManager::FeatureFillet3, 2015 (obsolète pour le rayon constant en 2020)
    "fillet_definition": 28,  # ISimpleFilletFeatureData2::Initialize + CreateFeature, 2020
    "feature_chamfer": 13,  # IFeatureManager::InsertFeatureChamfer, 2005
    "linear_pattern4": 23,  # IFeatureManager::FeatureLinearPattern4, 2015
    "linear_pattern5": 25,  # IFeatureManager::FeatureLinearPattern5, 2017
    "circular_pattern4": 21,  # IFeatureManager::FeatureCircularPattern4, 2013
    "circular_pattern5": 25,  # IFeatureManager::FeatureCircularPattern5, 2017
    "mirror_feature2": 16,  # IFeatureManager::InsertMirrorFeature2, 2008
    # inspection
    "mass_property": 12,  # IModelDocExtension::CreateMassProperty, 2003 SP2 (11.2)
    "mass_property2": 28,  # IModelDocExtension::CreateMassProperty2, 2020
    "curve_params3": 19,  # IEdge::GetCurveParams3, 2011
    "whats_wrong": 18,  # IModelDocExtension::GetWhatsWrongCount, 2009 SP3 (17.3)
    "measure": 16,  # IModelDocExtension::CreateMeasure, 2008
    "component_suppression2": 27,  # IComponent2::GetSuppression2, 2019
    "interference_manager": 15,  # IAssemblyDoc::InterferenceDetectionManager, 2007
}

# Famille → variantes de méthode, de la plus récente à la plus ancienne : (nom du membre COM, capacité).
VARIANTS: dict[str, tuple[tuple[str, str], ...]] = {
    "save_as": (("SaveAs3", "save_as3"), ("SaveAs", "save_as")),
    "feature_cut": (("FeatureCut4", "feature_cut4"), ("FeatureCut3", "feature_cut3")),
    "fillet": (("CreateFeature", "fillet_definition"), ("FeatureFillet3", "feature_fillet3")),
    "linear_pattern": (
        ("FeatureLinearPattern5", "linear_pattern5"),
        ("FeatureLinearPattern4", "linear_pattern4"),
    ),
    "circular_pattern": (
        ("FeatureCircularPattern5", "circular_pattern5"),
        ("FeatureCircularPattern4", "circular_pattern4"),
    ),
    "mass_property": (("CreateMassProperty2", "mass_property2"), ("CreateMassProperty", "mass_property")),
    "custom_property_get": (("Get6", "custom_property_get6"), ("Get5", "custom_property_add3")),
}


def _min_major(cap: str) -> int:
    try:
        return CAPABILITIES[cap]
    except KeyError as exc:
        raise SwError(ErrorCode.INTERNAL, f"Capacité inconnue de la table : {cap!r}") from exc


def supports(cap: str, major: int) -> bool:
    return major >= _min_major(cap)


def require(cap: str, major: int) -> None:
    """Lève `UNSUPPORTED_VERSION` si la capacité n'existe pas dans la révision majeure donnée."""
    needed = _min_major(cap)
    if major < needed:
        raise SwError(
            ErrorCode.UNSUPPORTED_VERSION,
            f"« {cap} » exige SOLIDWORKS {sw_year(needed)} ou plus récent ; version connectée : {sw_year(major)}.",
            details={"capability": cap, "min_major": needed, "major": major},
        )


def variant(family: str, major: int) -> str:
    """Nom du membre COM à appeler pour une famille de méthodes suffixées, selon la version."""
    try:
        candidates = VARIANTS[family]
    except KeyError as exc:
        raise SwError(ErrorCode.INTERNAL, f"Famille de méthodes inconnue : {family!r}") from exc
    for method, cap in candidates:
        if supports(cap, major):
            return method
    oldest = candidates[-1][1]
    raise SwError(
        ErrorCode.UNSUPPORTED_VERSION,
        f"Aucune variante de « {family} » n'existe dans SOLIDWORKS {sw_year(major)} "
        f"(minimum : {sw_year(_min_major(oldest))}).",
        details={"family": family, "min_major": _min_major(oldest), "major": major},
    )


def summary(major: int) -> dict[str, bool]:
    """Capacités disponibles pour une version (diagnostic, `swmcp doctor`)."""
    return {cap: supports(cap, major) for cap in sorted(CAPABILITIES)}
