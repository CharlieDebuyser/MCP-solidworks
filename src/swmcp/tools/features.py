"""Famille 4 : fonctions volumiques, cotes, variables, gestion des fonctions. Unités : mm et degrés."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Literal

from pydantic import Field

from ..backend.models import Direction, EndCondition, FeatureCreated, Vec3
from ..backend.protocol import SwBackend
from ..envelope import ErrorCode, SwError, invalid
from ..runtime import Call
from .outputs import (
    DimensionSetResult,
    FeatureModified,
    FeatureResult,
    FeaturesDeleted,
    VariableSetResult,
    Verification,
)
from .registry import ConfirmToken, Coord, ExpectedDoc, Mm, Name, Verify, tool

SketchName = Annotated[
    str, Field(min_length=1, max_length=80, description="Nom d'une esquisse fermée (sketch_end)")
]
FeatureName = Annotated[
    str, Field(min_length=1, max_length=80, description="Nom exact d'une fonction (inspect_tree)")
]
EdgeHandles = Annotated[
    list[Annotated[str, Field(pattern=r"^ent:edge:\d+$")]],
    Field(min_length=1, max_length=50, description="Handles d'arêtes issus de find_edges"),
]
FaceHandle = Annotated[str, Field(pattern=r"^ent:face:\d+$", description="Handle de face issu de find_faces")]
AxisRef = Annotated[
    str,
    Field(
        pattern=r"^([+-]?[xyz]|ent:(edge|face):\d+)$",
        description="'x', 'y', 'z' (signe admis) ou handle d'arête",
    ),
]
Point3 = tuple[Coord, Coord, Coord]


async def _feature(c: Call, verify: str, make: Callable[[SwBackend], FeatureCreated]) -> FeatureResult:
    """Crée une fonction et renvoie la preuve légère ou mesurée (spec §6)."""

    def job(b: SwBackend) -> FeatureResult:
        before = b.body_stats()
        created = make(b)
        after = b.body_stats()
        state = b.state()
        full = verify == "full"
        return FeatureResult(
            feature=created.feature,
            type=created.type,
            dimensions=created.dimensions,
            sketch=created.sketch,
            volume_before_mm3=before.volume_mm3,
            volume_after_mm3=after.volume_mm3,
            verify=Verification(
                rebuild_ok=state.rebuild_ok,
                feature_count=state.feature_count,
                body_count=after.body_count,
                volume_mm3=after.volume_mm3,
                volume_delta_mm3=round(after.volume_mm3 - before.volume_mm3, 6),
                bbox_mm=after.bbox_mm if full else None,
                face_count=after.face_count if full else None,
                edge_count=after.edge_count if full else None,
            ),
        )

    result = await c.sw(job)
    if result.verify.rebuild_ok is False:
        c.warn("La reconstruction signale des erreurs : appeler inspect_rebuild.")
    if result.verify.body_count > 1:
        c.warn(f"Le modèle compte {result.verify.body_count} corps : vérifier que c'est voulu.")
    if abs(result.verify.volume_delta_mm3) < 1e-9:
        c.warn("Le volume n'a pas changé : vérifier la direction et la profondeur de la fonction.")
    return result


def _depth(depth_mm: float | None, end_condition: str) -> None:
    if end_condition in ("blind", "mid_plane") and depth_mm is None:
        raise invalid(f"depth_mm est obligatoire avec end_condition={end_condition}.")
    if end_condition in ("through_all", "up_to_next") and depth_mm is not None:
        raise invalid(f"depth_mm ne s'applique pas à end_condition={end_condition} : le retirer.")


@tool("feature_extrude", title="Bossage extrudé", profile="design", modifies=True)
async def feature_extrude(
    c: Call,
    sketch: SketchName,
    depth_mm: Mm | None = None,
    end_condition: EndCondition = "blind",
    direction: Annotated[
        Direction, Field(description="normal : selon la normale de l'esquisse ; reverse ; both")
    ] = "normal",
    merge: Annotated[bool, Field(description="Fusionner avec le solide existant")] = True,
    draft_deg: Annotated[float, Field(ge=0, lt=60, description="Dépouille en degrés")] = 0.0,
    name: Name = None,
    verify: Verify = "basic",
    expected_doc: ExpectedDoc = None,
) -> FeatureResult:
    """Ajoute de la matière en extrudant une esquisse fermée. depth_mm en mm (obligatoire pour blind et
    mid_plane). Renvoie le volume avant/après et la cote pilote (ex. D1@Base_1) à réutiliser avec dimension_set.
    Exemple : feature_extrude(sketch="Profil", depth_mm=30, name="Base")."""
    _depth(depth_mm, end_condition)
    return await _feature(
        c,
        verify,
        lambda b: b.extrude(sketch, depth_mm, end_condition, direction, merge, draft_deg, name, False),
    )


@tool("feature_cut_extrude", title="Enlèvement de matière extrudé", profile="design", modifies=True)
async def feature_cut_extrude(
    c: Call,
    sketch: SketchName,
    depth_mm: Mm | None = None,
    end_condition: EndCondition = "blind",
    direction: Annotated[
        Direction, Field(description="normal : vers la matière ; reverse ; both")
    ] = "normal",
    draft_deg: Annotated[float, Field(ge=0, lt=60)] = 0.0,
    name: Name = None,
    verify: Verify = "basic",
    expected_doc: ExpectedDoc = None,
) -> FeatureResult:
    """Enlève de la matière en extrudant une esquisse fermée (poche, rainure, découpe). through_all traverse
    toute la pièce. Si le volume ne diminue pas, la direction est fausse : réessayer avec direction="reverse".
    Exemple : feature_cut_extrude(sketch="Poche", depth_mm=5)."""
    _depth(depth_mm, end_condition)
    return await _feature(
        c,
        verify,
        lambda b: b.extrude(sketch, depth_mm, end_condition, direction, True, draft_deg, name, True),
    )


@tool("feature_revolve", title="Révolution", profile="design", modifies=True)
async def feature_revolve(
    c: Call,
    sketch: SketchName,
    axis: Annotated[
        str, Field(pattern=r"^ent:sk:\d+$", description="Handle d'une ligne de l'esquisse servant d'axe")
    ],
    angle_deg: Annotated[float, Field(gt=0, le=360)] = 360.0,
    cut: Annotated[bool, Field(description="true : enlèvement de matière par révolution")] = False,
    name: Name = None,
    verify: Verify = "basic",
    expected_doc: ExpectedDoc = None,
) -> FeatureResult:
    """Crée un volume de révolution à partir d'un contour fermé situé d'un seul côté de l'axe. L'axe est une
    ligne de la même esquisse, de préférence de construction (sketch_add_line(construction=true)).
    Exemple : feature_revolve(sketch="Profil", axis="ent:sk:1", angle_deg=360)."""
    return await _feature(c, verify, lambda b: b.revolve(sketch, axis, angle_deg, cut, name))


@tool("feature_fillet", title="Congé", profile="design", modifies=True)
async def feature_fillet(
    c: Call,
    edges: EdgeHandles,
    radius_mm: Mm,
    name: Name = None,
    verify: Verify = "basic",
    expected_doc: ExpectedDoc = None,
) -> FeatureResult:
    """Arrondit des arêtes avec un rayon constant (mm). À faire en fin de modélisation ; le rayon doit rester
    inférieur à l'épaisseur locale. Les handles des arêtes consommées deviennent invalides.
    Exemple : feature_fillet(edges=["ent:edge:4"], radius_mm=2)."""
    return await _feature(c, verify, lambda b: b.fillet(list(dict.fromkeys(edges)), radius_mm, name))


@tool("feature_chamfer", title="Chanfrein", profile="design", modifies=True)
async def feature_chamfer(
    c: Call,
    edges: EdgeHandles,
    distance_mm: Mm,
    angle_deg: Annotated[float, Field(gt=0, lt=90)] = 45.0,
    name: Name = None,
    verify: Verify = "basic",
    expected_doc: ExpectedDoc = None,
) -> FeatureResult:
    """Chanfreine des arêtes : distance en mm et angle en degrés (45 par défaut).
    Exemple : feature_chamfer(edges=["ent:edge:7"], distance_mm=1)."""
    return await _feature(
        c, verify, lambda b: b.chamfer(list(dict.fromkeys(edges)), distance_mm, angle_deg, name)
    )


@tool("feature_hole", title="Perçages", profile="design", modifies=True)
async def feature_hole(
    c: Call,
    face: FaceHandle,
    points_mm: Annotated[
        list[Point3],
        Field(min_length=1, max_length=100, description="Centres [x, y, z] en mm, repère du MODÈLE"),
    ],
    diameter_mm: Mm,
    depth_mm: Mm | None = None,
    through: Annotated[bool, Field(description="Perçage débouchant")] = False,
    name: Name = None,
    verify: Verify = "basic",
    expected_doc: ExpectedDoc = None,
) -> FeatureResult:
    """Perce des trous cylindriques simples (fond plat, sans taraudage) sur une face plane. Les centres sont
    donnés en coordonnées du modèle et projetés sur la face ; donner depth_mm OU through=true.
    Exemple : feature_hole(face="ent:face:3", points_mm=[[10,3,10],[50,3,10]], diameter_mm=5, through=true)."""
    if through == (depth_mm is not None):
        raise invalid("Donner exactement un des deux : depth_mm, ou through=true.")
    pts: list[Vec3] = [(float(x), float(y), float(z)) for x, y, z in points_mm]
    for i, p in enumerate(pts):
        for q in pts[:i]:
            if sum((a - b) ** 2 for a, b in zip(p, q, strict=True)) ** 0.5 < diameter_mm:
                c.warn("Des perçages se chevauchent ou se touchent : vérifier les entraxes.")
    return await _feature(c, verify, lambda b: b.hole(face, pts, diameter_mm, depth_mm, name))


@tool("feature_shell", title="Coque", profile="design", modifies=True)
async def feature_shell(
    c: Call,
    faces_to_remove: Annotated[list[FaceHandle], Field(min_length=1, max_length=20)],
    thickness_mm: Mm,
    name: Name = None,
    verify: Verify = "basic",
    expected_doc: ExpectedDoc = None,
) -> FeatureResult:
    """Évide le solide en gardant une paroi d'épaisseur constante (mm) et en ouvrant les faces indiquées.
    À faire avant les congés extérieurs. Exemple : feature_shell(faces_to_remove=["ent:face:2"], thickness_mm=2)."""
    return await _feature(
        c, verify, lambda b: b.shell(list(dict.fromkeys(faces_to_remove)), thickness_mm, name)
    )


Features = Annotated[list[FeatureName], Field(min_length=1, max_length=20, description="Fonctions à répéter")]


@tool("feature_pattern_linear", title="Répétition linéaire", profile="design", modifies=True)
async def feature_pattern_linear(
    c: Call,
    features: Features,
    direction: AxisRef,
    count: Annotated[int, Field(ge=2, le=500, description="Nombre total d'occurrences, original compris")],
    spacing_mm: Mm,
    name: Name = None,
    verify: Verify = "basic",
    expected_doc: ExpectedDoc = None,
) -> FeatureResult:
    """Répète des fonctions le long d'une direction : axe du modèle ('x', '-y'…) ou arête droite.
    Exemple : feature_pattern_linear(features=["Hole_1"], direction="x", count=4, spacing_mm=15)."""
    return await _feature(c, verify, lambda b: b.pattern_linear(features, direction, count, spacing_mm, name))


@tool("feature_pattern_circular", title="Répétition circulaire", profile="design", modifies=True)
async def feature_pattern_circular(
    c: Call,
    features: Features,
    axis: AxisRef,
    count: Annotated[int, Field(ge=2, le=500, description="Nombre total d'occurrences, original compris")],
    angle_deg: Annotated[
        float, Field(gt=0, le=360, description="Angle total couvert ; 360 = répartition régulière")
    ] = 360.0,
    name: Name = None,
    verify: Verify = "basic",
    expected_doc: ExpectedDoc = None,
) -> FeatureResult:
    """Répète des fonctions autour d'un axe : axe du modèle passant par l'origine ('x', 'y', 'z') ou arête
    circulaire. Exemple : feature_pattern_circular(features=["Hole_1"], axis="z", count=6)."""
    return await _feature(c, verify, lambda b: b.pattern_circular(features, axis, count, angle_deg, name))


@tool("feature_mirror", title="Symétrie", profile="design", modifies=True)
async def feature_mirror(
    c: Call,
    features: Features,
    plane: Annotated[str, Field(pattern=r"^(front|top|right|ent:face:\d+)$", description="Plan de symétrie")],
    name: Name = None,
    verify: Verify = "basic",
    expected_doc: ExpectedDoc = None,
) -> FeatureResult:
    """Copie des fonctions par symétrie par rapport à un plan de référence ou à une face plane.
    Exemple : feature_mirror(features=["Nervure"], plane="right")."""
    return await _feature(c, verify, lambda b: b.mirror(features, plane, name))


@tool("dimension_set", title="Modifier une cote", profile="design", modifies=True, idempotent=True)
async def dimension_set(
    c: Call,
    full_name: Annotated[
        str,
        Field(
            pattern=r"^[^@]{1,80}@[^@]{1,80}$",
            description="Nom complet de la cote, ex. D1@Base_1 ou Longueur@Profil",
        ),
    ],
    value: Annotated[float, Field(gt=0, le=100_000, description="mm, ou degrés pour une cote angulaire")],
    expected_doc: ExpectedDoc = None,
) -> DimensionSetResult:
    """Change la valeur d'une cote nommée puis reconstruit. C'est la façon normale de modifier une pièce :
    préférer dimension_set à une reconstruction complète. Renvoie ancienne et nouvelle valeur, volumes et masses
    avant/après. Si la reconstruction échoue, l'ancienne valeur est rétablie (erreur REBUILD_FAILED).
    Exemple : dimension_set(full_name="Epaisseur@Profil", value=4)."""

    def job(b: SwBackend) -> DimensionSetResult:
        v0, m0 = b.body_stats().volume_mm3, b.mass().mass_g
        change = b.set_dimension(full_name, value)
        rb = b.rebuild(True)
        if not rb.rebuild_ok:
            b.set_dimension(full_name, change.old_value)
            restored = b.rebuild(True).rebuild_ok
            raise SwError(
                ErrorCode.REBUILD_FAILED,
                f"La valeur {value:g} pour {full_name} fait échouer la reconstruction ; ancienne valeur "
                f"{change.old_value:g} rétablie.",
                hint="Choisir une valeur compatible avec les fonctions en aval (voir `errors`), ou modifier d'abord "
                "la fonction fautive.",
                details={
                    "reverted": True,
                    "rebuild_ok_after_revert": restored,
                    "errors": [e.model_dump() for e in rb.errors[:10]],
                },
            )
        return DimensionSetResult(
            **change.model_dump(),
            rebuild_ok=True,
            volume_before_mm3=v0,
            volume_after_mm3=b.body_stats().volume_mm3,
            mass_before_g=m0,
            mass_after_g=b.mass().mass_g,
        )

    return await c.sw(job)


@tool("variable_set", title="Variable globale", profile="design", modifies=True, idempotent=True)
async def variable_set(
    c: Call,
    name: Annotated[
        str, Field(pattern=r"^[A-Za-z_À-ÿ][A-Za-z0-9_À-ÿ]{0,39}$", description="Nom de la variable globale")
    ],
    value: Annotated[float, Field(ge=-1e6, le=1e6)],
    unit: Annotated[
        Literal["mm", "deg", ""], Field(description="Unité ; vide pour un nombre sans unité")
    ] = "mm",
    expected_doc: ExpectedDoc = None,
) -> VariableSetResult:
    """Crée ou modifie une variable globale du gestionnaire d'équations, puis reconstruit. La valeur est un
    nombre : aucune expression n'est acceptée. Exemple : variable_set(name="Ep", value=4, unit="mm")."""
    text = f"{value:g}{unit}"

    def job(b: SwBackend) -> VariableSetResult:
        change = b.set_variable(name, text)
        return VariableSetResult(**change.model_dump(), rebuild_ok=b.rebuild(True).rebuild_ok)

    result = await c.sw(job)
    if result.rebuild_ok is False:
        c.warn("La reconstruction signale des erreurs : appeler inspect_rebuild.")
    return result


@tool("feature_rename", title="Renommer une fonction", profile="design", modifies=True)
async def feature_rename(
    c: Call,
    name: FeatureName,
    new_name: Annotated[
        str, Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9À-ÿ_][A-Za-z0-9À-ÿ_ .\-]*$")
    ],
    expected_doc: ExpectedDoc = None,
) -> FeatureModified:
    """Renomme une fonction ou une esquisse. Les noms complets de ses cotes changent en conséquence
    (D1@ancien devient D1@nouveau)."""
    renamed = await c.sw(lambda b: b.rename_feature(name, new_name))
    return FeatureModified(feature=renamed)


@tool("feature_suppress", title="Supprimer/rétablir (état)", profile="design", modifies=True, idempotent=True)
async def feature_suppress(
    c: Call,
    name: FeatureName,
    suppressed: Annotated[
        bool, Field(description="true : désactiver la fonction ; false : la rétablir")
    ] = True,
    expected_doc: ExpectedDoc = None,
) -> FeatureModified:
    """Désactive (suppress) ou rétablit une fonction sans l'effacer. Réversible ; utile pour isoler une erreur."""

    def job(b: SwBackend) -> FeatureModified:
        flag = b.suppress_feature(name, suppressed)
        return FeatureModified(feature=name, suppressed=flag, rebuild_ok=b.state().rebuild_ok)

    return await c.sw(job)


@tool("feature_delete", title="Effacer une fonction", profile="full", modifies=True, destructive=True)
async def feature_delete(
    c: Call,
    name: FeatureName,
    delete_children: Annotated[
        bool, Field(description="Effacer aussi les fonctions qui en dépendent")
    ] = False,
    expected_doc: ExpectedDoc = None,
    confirm_token: ConfirmToken = None,
) -> FeaturesDeleted:
    """Efface définitivement une fonction. Action destructive : le premier appel répond CONFIRMATION_REQUIRED
    avec la liste de ce qui sera effacé ; présenter cet effet à l'utilisateur, attendre son accord, puis rappeler
    avec le même confirm_token. Un point de restauration est créé avant l'effacement si le document est enregistré."""
    children = await c.sw(lambda b: b.feature_children(name), modifies=False)
    if delete_children and children:
        effect = f"La fonction {name} et ses {len(children)} dépendante(s) ({', '.join(children[:8])}) seront effacées."
    elif children:
        effect = (
            f"La fonction {name} sera effacée ; {len(children)} fonction(s) en dépendent "
            f"({', '.join(children[:8])}) et passeront en erreur."
        )
    else:
        effect = f"La fonction {name} sera effacée."
    await c.confirm(effect)

    def job(b: SwBackend) -> FeaturesDeleted:
        info = b.doc_info()
        cp = c.rt.checkpoints.create(info.name, "avant-suppression", b.save_copy).id if info.path else None
        deleted = b.delete_feature(name, delete_children)
        return FeaturesDeleted(deleted=deleted, rebuild_ok=b.state().rebuild_ok, checkpoint=cp)

    result = await c.sw(job)
    if result.rebuild_ok is False:
        c.warn("Des fonctions dépendantes sont en erreur : appeler inspect_rebuild.")
    return result
