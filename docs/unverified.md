# Appels COM non vérifiés

`ComSwBackend` (`src/swmcp/backend/com_backend.py`) et `constants.py` ont été écrits **sans
SolidWorks ni pywin32** : aucun appel COM de ce tableau n'a été exécuté (spec §14, « Conséquences
d'une v1 sans test réel »). La couche COM reste non prouvée tant que
`pytest -m solidworks tests/integration/test_solidworks.py` et `swmcp selftest` n'ont pas tourné sur
un vrai SolidWorks.

## Comment lire ce document

- **Source des signatures.** « aide 2026 » = API Help SOLIDWORKS 2026 en ligne
  (`help.solidworks.com/2026/english/api/…`), la plus récente accessible, lue le 2026-11-19. Pour
  chaque membre, la déclaration Visual Basic (ordre, type et sens des paramètres), le type de retour,
  la ligne « Availability » et, pour les méthodes de fonction, les remarques sur les marques de
  sélection ont été lus. « de mémoire » = non lu dans l'aide (mécanismes Windows ou pywin32, hors API
  SolidWorks).
- **Ce que « lu dans l'aide » ne prouve pas.** L'aide donne la signature .NET/VBA, pas le
  comportement de pywin32 : conversion des tableaux, objets renvoyés comme `Object`, paramètres de
  sortie, propriétés paramétrées. Ces points sont les hypothèses de la troisième colonne.
- **Marqueur dans le code.** Chaque ligne correspond à un commentaire
  `# UNVERIFIED: <interface.membre> — <aide> — <hypothèse>` ; la liste à jour s'obtient par
  `grep -n "UNVERIFIED:" src/swmcp/backend/*.py`. Quand un appel est validé sur SolidWorks, retirer
  le marqueur et la ligne.
- **Constantes.** Aucune valeur numérique d'énumération n'est recopiée : les noms listés dans
  `constants.REQUIRED` (relus dans l'aide 2026, espace `swconst`) sont résolus à la connexion ;
  un nom absent fait échouer `sw_connect` avec la liste des manquants.

## Bilan

| | Nombre |
| --- | --- |
| Marqueurs `UNVERIFIED` | 197 |
| … dont signature lue dans l'API Help 2026 | 192 |
| … dont écrits de mémoire (hors API SolidWorks) | 5 |
| Membres COM SolidWorks distincts cités | environ 200 |

## Hypothèses transverses (pywin32)

Elles ne sont attachées à aucun membre en particulier et conditionnent presque tous les appels.

| Sujet | Hypothèse | Où | Symptôme si elle est fausse |
| --- | --- | --- | --- |
| Liaison anticipée | `gencache.EnsureDispatch` génère les classes à la demande ; un objet renvoyé comme `Object` est enveloppé dans sa classe générée, sinon `win32com.client.CastTo(obj, "IInterface")` fait le `QueryInterface`. | `_cast`, `_try_cast` | `INTERNAL` « Appel COM … impossible : AttributeError » ; `_try_cast` renvoie `None` et les entités d'esquisse relues deviennent `ENTITY_STALE`. |
| Paramètres de sortie | En liaison anticipée, une méthode à paramètres `ByRef` renvoie le tuple `(retour, sortie1, …)` quand on passe une valeur neutre à la place de chaque sortie ; en liaison tardive, des `VARIANT(VT_BYREF \| type)`. | `_m_out` | Erreurs et avertissements de `OpenDoc6` / `Save3` / `SaveAs3` perdus (valeurs `None`), ou `INTERNAL` de type. |
| Objet nul | `VARIANT(VT_DISPATCH, None)` vaut `Nothing` (callout de `SelectByID2`, `ExportData` de `SaveAs3`). | `_nil` | `INTERNAL` 0x80020005 (type incompatible) à la sélection par position ou à l'export. |
| Tableau vide | `pythoncom.Empty` convient aux tableaux optionnels (`Config_names`, tableaux de `FeatureFillet3`). | `_empty` | `INTERNAL` 0x80020005 dans `dimension_set`, `sketch_add_dimension`. |
| Tableaux en entrée | Réels : `VARIANT(VT_ARRAY \| VT_R8)` ; objets : `VARIANT(VT_ARRAY \| VT_DISPATCH)` ; identifiant persistant : `VARIANT(VT_ARRAY \| VT_UI1, bytes)`. | `_doubles`, `_objects`, `_by_persist` | Repère d'esquisse, `inspect_measure` entre entités ou résolution des handles en erreur. |
| Tableaux en sortie | Un tableau COM arrive en `tuple` ; `Nothing` en `None` ; l'identifiant persistant est convertible par `bytes()`. | `_seq`, `_floats`, `_persist` | `INTERNAL` « Tableau COM de n réels attendu ». |
| Propriétés paramétrées | Lecture comme une méthode (`CustomPropertyManager("")`, `Equation(i)`) ; écriture par `SetEquation(i, texte)`. | `_props`, `set_variable` | `variable_set` ou `prop_get` / `prop_set` en `INTERNAL`. |
| Erreurs COM | `pywintypes.com_error` porte `hresult` (signé) et `excepinfo[5]` ; elle est reconnue par son nom de classe pour que le module s'importe sans pywin32. | `hresult_of` | Une déconnexion ou un refus d'appel remonterait en `INTERNAL` au lieu de `SW_DISCONNECTED` / `SW_BUSY`. |
| Bibliothèque de constantes | `swconst.tlb` est à côté de `sldworks.exe` (`ISldWorks.GetExecutablePath`) ; sinon elle est cherchée dans le registre par son LIBID (de mémoire). | `constants.load` | `sw_connect` échoue : « n constante(s) SolidWorks introuvable(s) ». |

## Hypothèses de comportement les plus risquées

À vérifier en premier ; chacune a son test d'intégration.

1. **Sens de la normale et direction par défaut des fonctions** (`extrude`, `hole`). Hypothèses : l'axe
   Z du repère d'esquisse (`ModelToSketchTransform` inversée) est la « normale de l'esquisse » de
   l'aide ; un bossage part le long de cette normale et un enlèvement à l'opposé ; sur une face, la
   normale sort de la matière. Si c'est faux : extrusion du mauvais côté, perçage dans le vide
   (`FEATURE_FAILED`). Test : `test_hole_lands_where_asked_in_model_coordinates`.
2. **Création d'esquisse par `AddToDB` puis fusion explicite** (`_drawing`, `_weld`). Hypothèses :
   les coordonnées de `CreateLine` / `CreateArc` sont celles du repère de l'esquisse ; `AddToDB` ne
   pose aucune relation ; `SketchAddConstraints("sgMERGEPOINTS")` sur deux points sélectionnés les
   fusionne ; un contour ainsi soudé est accepté par les fonctions. Test :
   `test_sketch_extrude_volume_and_bbox`.
3. **Handles sur identifiants persistants** (`_persist`, `_by_persist`). Hypothèses :
   `GetPersistReference3` fonctionne pour les faces, arêtes, segments et points d'esquisse (y compris
   dans une esquisse en cours d'édition) et rend des octets stables entre deux lectures ;
   `GetObjectByPersistReference3` signale une entité disparue par un code différent de
   `swPersistReferencedObject_Ok`. Sans identifiant, une entité d'esquisse reçoit un handle valable
   seulement tant que l'esquisse est ouverte. Tests : `test_stale_handles`,
   `test_handles_die_with_their_document`.
4. **Cotes d'esquisse** (`sketch_dimension`, `_anchor`). Hypothèses : `AddDimension2` attend la
   position du texte dans le repère du modèle ; cette position détermine le type de cote (alignée,
   quadrant d'un angle) ; avec `swInputDimValOnCreate` désactivé aucun dialogue ne s'ouvre ; une cote
   qui sur-contraint n'ouvre pas le dialogue « pilotée / pilotante ». Risque : `TIMEOUT` sur dialogue
   modal, ou angle supplémentaire. Test : `test_sketch_dimension_drives_geometry`.
5. **Sélections marquées des fonctions** (`fillet`, `chamfer`, `shell`, `pattern_*`, `mirror`).
   Hypothèses : `CreateFeature` avec un `ISimpleFilletFeatureData2` reprend les arêtes
   présélectionnées (marque 1) ; `InsertFeatureChamfer` accepte la marque 0 ; un plan de référence
   sélectionné avec la marque 1 donne la direction d'une répétition linéaire (le long de sa normale,
   `FlipDir1` pour l'opposé) ; `InsertAxis2` crée un axe à l'intersection de deux plans présélectionnés.
   Test : `test_fillet_then_chamfer_on_found_edges` (les répétitions et la symétrie n'ont pas encore
   de test d'intégration).

Autres points à surveiller :

- **Trois plans de référence** : les trois premières fonctions de type `RefPlane` de l'arbre sont, dans
  l'ordre, face, dessus, droite (`_ref_planes`). Test : `test_reference_planes_found_by_type_and_order`.
- **Condition de fin `up_to_next`** : `swEndCondUpToNext` a été retenu ; `swEndCondThroughNext` existe
  aussi et pourrait être la bonne valeur pour `FeatureExtrusion3`.
- **Sens des arcs** : `CreateArc(Direction = +1)` et `GetRotationDir() = 1` sont pris pour le sens
  trigonométrique dans le repère (X, Y) de l'esquisse.
- **`FaceInfo.centroid_mm`** : centre de la boîte englobante de la face, ramené sur la face pour une
  face plane. Exact pour un rectangle, approché sinon (l'API n'expose pas de centroïde de face).
- **Boîte englobante** : `IPartDoc.GetPartBox` est documentée comme approchée ; la tolérance de
  ±0,1 mm de `check_design` peut ne pas tenir sur des formes courbes.
- **Fichier corrompu** : `OpenDoc6` avec `swOpenDocOptions_Silent` est supposé renvoyer `Nothing` sans
  dialogue. Test : `test_open_corrupt_file_is_a_coded_error`.
- **Types de fonction** : `OriginProfileFeature` et plusieurs noms de dossiers de `TREE_NOISE` ne
  figurent pas dans la liste de `IFeature::GetTypeName2` de l'aide (de mémoire) ; au pire l'arbre
  montre quelques dossiers système en trop.
- **Répétition circulaire** : le pas est passé explicitement (`EqualSpacing = False`) pour reproduire
  la sémantique du backend simulé (360° → total / n, sinon total / (n − 1)).
- **Export** : les options propres aux formats (protocole STEP, STL binaire, etc.) sont celles de
  l'utilisateur ; `export_file` ne les modifie pas.

## Tableau des appels

| Méthode (interface.membre) | Aide consultée | Hypothèse faite | Endroit dans le code |
| --- | --- | --- | --- |
| `ROT « SolidWorks_PID_<pid> »` | aide non consultée : de mémoire | chaque instance enregistre un moniker de ce nom ; GetObject renvoie son ISldWorks. | `com_backend.py:385` (`_running_instances`) |
| `ISldWorks.ActiveDoc / IModelDoc2.GetTitle` | aide 2026 | lus en liaison tardive sur une instance non retenue, uniquement pour aider l'utilisateur à choisir le bon pid. | `com_backend.py:408` (`_instance_title`) |
| `GetActiveObject("SldWorks.Application")` | aide non consultée : de mémoire | renvoie l'instance enregistrée ; lève com_error si SolidWorks n'est pas lancé. | `com_backend.py:442` (`connect`) |
| `gencache.EnsureDispatch` | aide non consultée : de mémoire (pywin32) | liaison anticipée depuis la bibliothèque de types de l'instance ; accepte un ProgID ou un IDispatch. | `com_backend.py:457` (`connect`) |
| `ISldWorks.Visible` | aide 2026 | propriété booléenne en écriture ; une instance créée par COM démarre invisible. | `com_backend.py:464` (`connect`) |
| `ISldWorks.RevisionNumber` | aide 2026 | méthode sans argument, « majeure.sp.correctif ». | `com_backend.py:467` (`connect`) |
| `ISldWorks.GetProcessID` | aide 2026 | entier, identifiant du processus. | `com_backend.py:475` (`connect`) |
| `ISldWorks.GetCurrentLanguage` | aide 2026 | chaîne en minuscules (« french »). | `com_backend.py:477` (`connect`) |
| `ISldWorks.GetProcessID` | aide 2026 | appel court sans effet, preuve que SolidWorks répond. | `com_backend.py:501` (`ping`) |
| `IModelDoc2.GetFeatureCount` | aide 2026 | nombre de fonctions hors sous-fonctions (dossiers système de l'arbre compris). | `com_backend.py:533` (`state`) |
| `IModelDocExtension.GetWhatsWrongCount` | aide 2026 | nombre d'entrées du dialogue « Qu'est-ce qui ne va pas » ; 0 est pris pour « reconstruction sans erreur ». | `com_backend.py:536` (`state`) |
| `ISldWorks.SetUserPreferenceToggle` | aide 2026 | (UserPreferenceValue, OnFlag), sans retour ; rétablit l'option système modifiée pendant l'outil. | `com_backend.py:565` (`cleanup`) |
| `ISldWorks.ActiveDoc` | aide 2026 | propriété, objet IModelDoc2 ou Nothing sans document. | `com_backend.py:581` (`_active_doc`) |
| `IModelDoc2.GetTitle` | aide 2026 | titre affiché dans la barre de la fenêtre. | `com_backend.py:591` (`_title`) |
| `IModelDoc2.GetPathName` | aide 2026 | chemin complet, chaîne vide si jamais enregistré. | `com_backend.py:595` (`_path`) |
| `IModelDoc2.GetSaveFlag` | aide 2026 | vrai si le document doit être enregistré. | `com_backend.py:599` (`_dirty`) |
| `IModelDoc2.GetType` | aide 2026 | entier de swDocumentTypes_e. | `com_backend.py:603` (`_doc_type`) |
| `IModelDoc2.Extension` | aide 2026 | propriété en lecture, IModelDocExtension. | `com_backend.py:614` (`_ext`) |
| `IModelDoc2.SketchManager` | aide 2026 | propriété en lecture, ISketchManager. | `com_backend.py:618` (`_skm`) |
| `IModelDoc2.FeatureManager` | aide 2026 | propriété en lecture, IFeatureManager. | `com_backend.py:622` (`_fm`) |
| `IModelDoc2.IsOpenedReadOnly` | aide 2026 | vrai si le document est ouvert en lecture seule. | `com_backend.py:650` (`_doc_ref`) |
| `ISldWorks.GetDocuments` | aide 2026 | tableau des documents de la session, y compris ceux chargés en mémoire sans fenêtre ; filtrés par IModelDoc2.Visible. | `com_backend.py:663` (`_open_docs`) |
| `IModelDoc2.Visible` | aide 2026 | propriété booléenne, vrai si le document a une fenêtre. | `com_backend.py:668` (`_open_docs`) |
| `IModelDoc2.ClearSelection2` | aide 2026 | (All As Boolean), sans retour. | `com_backend.py:685` (`_clear`) |
| `IModelDocExtension.SelectByID2` | aide 2026 | (Name, Type, X, Y, Z, Append, Mark, Callout, SelectOption) ; nom vide = sélection par position (mètres, repère du modèle). | `com_backend.py:697` (`_select`) |
| `IFeature.Select2` | aide 2026 | (Append, Mark) → booléen. | `com_backend.py:704` (`_select`) |
| `IModelDoc2.SelectionManager` | aide 2026 | propriété, ISelectionMgr. | `com_backend.py:707` (`_select`) |
| `ISelectionMgr.CreateSelectData` | aide 2026 | sans argument → ISelectData. | `com_backend.py:709` (`_select`) |
| `ISelectData.Mark` | aide 2026 | propriété entière en écriture. | `com_backend.py:711` (`_select`) |
| `IEntity.Select4 / ISketchSegment.Select4 / ISketchPoint.Select4` | aide 2026 | (Append, Data As SelectData) → booléen ; l'objet doit exposer l'une de ces interfaces. | `com_backend.py:713` (`_select`) |
| `ISldWorks.GetUserPreferenceToggle` | aide 2026 | (UserPreferenceToggle) → booléen. | `com_backend.py:733` (`_toggle`) |
| `ISldWorks.SetUserPreferenceToggle` | aide 2026 | (UserPreferenceValue, OnFlag), sans retour. | `com_backend.py:738` (`_toggle`) |
| `ISketchManager.AddToDB` | aide 2026 | propriété booléenne en lecture et écriture. | `com_backend.py:752` (`_drawing`) |
| `IModelDoc2.FirstFeature` | aide 2026 | méthode sans argument, première fonction ou Nothing. | `com_backend.py:767` (`_features`) |
| `IFeature.GetNextFeature` | aide 2026 | fonction suivante ou Nothing. | `com_backend.py:773` (`_features`) |
| `IFeature.GetTypeName2` | aide 2026 | chaîne indépendante de la langue (« RefPlane », « ProfileFeature »…). | `com_backend.py:778` (`_type_name`) |
| `IFeature.Name` | aide 2026 | propriété chaîne en lecture et écriture. | `com_backend.py:783` (`_fname`) |
| `IPartDoc.FeatureByName` | aide 2026 | (Name) → fonction ou Nothing. | `com_backend.py:791` (`_feature`) |
| `IFeature.Name` | aide 2026 | écriture du nom. | `com_backend.py:821` (`_rename`) |
| `IModelDoc2.FeatureByPositionReverse` | aide 2026 | (Num) → n-ième fonction depuis la fin de l'arbre ; 0 est pris pour la dernière. | `com_backend.py:848` (`_last_feature`) |
| `IModelDocExtension.GetPersistReference3` | aide 2026 | (DispObj) → tableau d'octets ; pywin32 est supposé le rendre sous une forme convertible par bytes(). | `com_backend.py:861` (`_persist`) |
| `IModelDocExtension.GetObjectByPersistReference3` | aide 2026 | (PersistId, ByRef ErrorCode) → objet ; ErrorCode de swPersistReferencedObjectStates_e. | `com_backend.py:885` (`_by_persist`) |
| `ISldWorks.GetUserPreferenceStringValue` | aide 2026 | (UserPreference) → chaîne ; swDefaultTemplatePart / Assembly / Drawing donnent le chemin du modèle par défaut. | `com_backend.py:925` (`new_doc`) |
| `ISldWorks.NewDocument` | aide 2026 | (TemplateName, PaperSize, Width, Height) → document ou Nothing ; PaperSize, Width et Height ne servent qu'aux mises en plan. | `com_backend.py:936` (`new_doc`) |
| `ISldWorks.OpenDoc6` | aide 2026 | (FileName, Type, Options, Configuration, ByRef Errors, ByRef Warnings) → IModelDoc2 ou Nothing ; Errors de swFileLoadError_e, Warnings de swFileLoadWarning_e. | `com_backend.py:962` (`open_doc`) |
| `ISldWorks.ActivateDoc3` | aide 2026 | (Name, UseUserPreferences, Option, ByRef Errors) → document ; Option de swRebuildOnActivation_e. | `com_backend.py:977` (`_activate`) |
| `IModelDocExtension.SaveAs3` | aide 2026 | (Name, Version, Options, ExportData, AdvancedSaveAsOptions, ByRef Errors, ByRef Warnings) → booléen ; format déduit de l'extension. | `com_backend.py:993` (`_save_as`) |
| `IModelDocExtension.SaveAs` | aide 2026 | (Name, Version, Options, ExportData, ByRef Errors, ByRef Warnings) → booléen. | `com_backend.py:999` (`_save_as`) |
| `IModelDoc2.IsOpenedReadOnly` | aide 2026 | Signature et type de retour tels que lus dans l'aide. | `com_backend.py:1025` (`_save`) |
| `IModelDoc2.Save3` | aide 2026 | (Options, ByRef Errors, ByRef Warnings) → booléen. | `com_backend.py:1027` (`_save`) |
| `ISldWorks.CloseDoc` | aide 2026 | (Name), sans retour ; ferme sans enregistrer, même un document modifié. | `com_backend.py:1059` (`close_doc`) |
| `IPartDoc.GetMaterialPropertyName2` | aide 2026 | (ConfigName, ByRef Database) → nom ; nom et base vides sans matériau ; "" est pris pour la configuration active. | `com_backend.py:1073` (`_material`) |
| `IModelDoc2.ConfigurationManager / IConfigurationManager.ActiveConfiguration / IConfiguration.Name` | aide 2026 | propriétés en lecture. | `com_backend.py:1082` (`doc_info`) |
| `IModelDoc2.LengthUnit` | aide 2026 | propriété entière de swLengthUnit_e. | `com_backend.py:1087` (`doc_info`) |
| `IModelDocExtension.CustomPropertyManager` | aide 2026 | propriété paramétrée (ConfigName) → ICustomPropertyManager ; "" = propriétés du document. Appelée comme une méthode en liaison anticipée. | `com_backend.py:1098` (`_props`) |
| `ICustomPropertyManager.Get6` | aide 2026 | (FieldName, UseCached, ByRef ValOut, ByRef ResolvedValOut, ByRef WasResolved, ByRef LinkToProperty) → swCustomInfoGetResult_e. | `com_backend.py:1104` (`_prop_value`) |
| `ICustomPropertyManager.Get5` | aide 2026 | comme Get6 sans LinkToProperty. | `com_backend.py:1108` (`_prop_value`) |
| `ICustomPropertyManager.GetNames` | aide 2026 | tableau de noms, vide sans propriété. | `com_backend.py:1116` (`get_props`) |
| `ICustomPropertyManager.Delete2` | aide 2026 | (FieldName) → code de résultat. | `com_backend.py:1125` (`set_prop`) |
| `ICustomPropertyManager.Add3` | aide 2026 | (FieldName, FieldType, FieldValue, OverwriteExisting) → code ; swCustomPropertyReplaceValue remplace une valeur existante. | `com_backend.py:1128` (`set_prop`) |
| `ISketchManager.ActiveSketch` | aide 2026 | propriété en lecture, ISketch ou Nothing. | `com_backend.py:1144` (`_active_sketch`) |
| `ISketch.GetConstrainedStatus` | aide 2026 | entier de swConstrainedStatus_e. | `com_backend.py:1179` (`_status`) |
| `ISldWorks.GetMathUtility` | aide 2026 | sans argument → IMathUtility. | `com_backend.py:1191` (`_math_utility`) |
| `ISketch.ModelToSketchTransform` | aide 2026 | propriété, IMathTransform modèle → esquisse. | `com_backend.py:1202` (`_frame`) |
| `IMathTransform.Inverse` | aide 2026 | sans argument → IMathTransform inverse. | `com_backend.py:1204` (`_frame`) |
| `IMathUtility.CreatePoint / CreateVector` | aide 2026 | (tableau de 3 réels) → objet. | `com_backend.py:1209` (`image`) |
| `IMathPoint.MultiplyTransform / IMathVector.MultiplyTransform` | aide 2026 | (transformation) → nouvel objet ; un vecteur est supposé ne subir que la rotation. | `com_backend.py:1211` (`image`) |
| `IMathPoint.ArrayData / IMathVector.ArrayData` | aide 2026 | tableau de 3 réels (m). | `com_backend.py:1214` (`image`) |
| `ISketchPoint.X / ISketchPoint.Y` | aide 2026 | propriétés réelles, repère de l'esquisse. | `com_backend.py:1226` (`_xy`) |
| `ISketchPoint.GetID / ISketchSegment.GetID` | aide 2026 | tableau de deux entiers. | `com_backend.py:1234` (`_sk_id`) |
| `ISketchSegment.GetType` | aide 2026 | entier de swSketchSegments_e. | `com_backend.py:1238` (`_seg_kind`) |
| `ISketchArc.IsCircle` | aide 2026 | entier : 1 cercle complet, 0 arc. | `com_backend.py:1243` (`_seg_kind`) |
| `ISketchLine.GetStartPoint2 / GetEndPoint2` | aide 2026 | sans argument → ISketchPoint. | `com_backend.py:1252` (`_seg_points`) |
| `ISketchArc.GetCenterPoint2 / GetStartPoint2 / GetEndPoint2` | aide 2026 | → ISketchPoint. | `com_backend.py:1257` (`_seg_points`) |
| `ISketchSegment.ConstructionGeometry` | aide 2026 | propriété booléenne. | `com_backend.py:1266` (`_seg_model`) |
| `ISketchArc.GetRadius` | aide 2026 | rayon en mètres. | `com_backend.py:1272` (`_seg_model`) |
| `ISketchArc.GetRotationDir` | aide 2026 | 1 = sens trigonométrique du début vers la fin, par rapport à la normale du plan d'esquisse ; pris pour le sens dans le repère (X, Y). | `com_backend.py:1275` (`_seg_model`) |
| `ISketch.GetSketchSegments` | aide 2026 | tableau de ISketchSegment (lignes, arcs, splines…). | `com_backend.py:1294` (`_read_segments`) |
| `ISketch.GetSketchPoints2` | aide 2026 | tableau de tous les ISketchPoint de l'esquisse, extrémités et centres des segments compris (d'où le filtrage par identifiant). | `com_backend.py:1304` (`_read_segments`) |
| `IModelDoc2.SketchAddConstraints` | aide 2026 | (IdStr), sans retour ; « sgMERGEPOINTS » appliqué à deux points sélectionnés est supposé les fusionner en un seul. | `com_backend.py:1385` (`_weld`) |
| `ISketchManager.CreateLine / CreateCenterLine` | aide 2026 | (X1, Y1, Z1, X2, Y2, Z2) en mètres → ISketchSegment ; coordonnées prises dans le repère de l'esquisse active. | `com_backend.py:1412` (`_new_line`) |
| `ISketchLine.GetStartPoint2` | aide 2026 | Signature et type de retour tels que lus dans l'aide. | `com_backend.py:1420` (`start`) |
| `ISketchLine.GetEndPoint2` | aide 2026 | Signature et type de retour tels que lus dans l'aide. | `com_backend.py:1423` (`end`) |
| `ISketchManager.CreateArc` | aide 2026 | (XC, YC, Zc, X1, Y1, Z1, X2, Y2, Z2, Direction) en mètres → ISketchSegment ; Direction +1 = du départ à l'arrivée en sens trigonométrique, -1 = horaire. | `com_backend.py:1431` (`_new_arc`) |
| `ISketchArc.GetStartPoint2` | aide 2026 | Signature et type de retour tels que lus dans l'aide. | `com_backend.py:1443` (`start`) |
| `ISketchArc.GetEndPoint2` | aide 2026 | Signature et type de retour tels que lus dans l'aide. | `com_backend.py:1446` (`end`) |
| `ISketchArc.GetCenterPoint2` | aide 2026 | sans argument → ISketchPoint. | `com_backend.py:1448` (`end`) |
| `ISketch.RelationManager / ISketchRelationManager.GetRelationsCount` | aide 2026 | propriété puis (Filter de swSketchRelationFilterType_e) → entier. | `com_backend.py:1464` (`_relation_count`) |
| `IModelDoc2.SketchAddConstraints` | aide 2026 | (IdStr), sans retour ; identifiants « sg… » de la colonne « 2006 and later ». | `com_backend.py:1474` (`_constrain`) |
| `ISketchManager.InsertSketch` | aide 2026 | (UpdateEditRebuild), sans retour ; ouvre une esquisse sur le plan ou la face sélectionné, ou ferme l'esquisse active. | `com_backend.py:1512` (`sketch_begin`) |
| `ISketchManager.InsertSketch` | aide 2026 | (UpdateEditRebuild) : ferme l'esquisse active et reconstruit. | `com_backend.py:1534` (`sketch_end`) |
| `IModelDocExtension.DeleteSelection2` | aide 2026 | (DeleteOptions) → booléen ; 0 = supprimer la seule sélection. | `com_backend.py:1543` (`sketch_end`) |
| `ISketchManager.CreateCircleByRadius` | aide 2026 | (XC, YC, Zc, Radius) en mètres → ISketchSegment (cercle complet). | `com_backend.py:1595` (`sketch_circle`) |
| `ISketchManager.CreatePoint` | aide 2026 | (X, Y, Z) en mètres → ISketchPoint. | `com_backend.py:1683` (`sketch_point`) |
| `ISketchArc.GetRadius` | aide 2026 | rayon en mètres. | `com_backend.py:1752` (`_anchor`) |
| `IModelDoc2.AddDimension2` | aide 2026 | (X, Y, Z) position du texte en mètres → cote d'affichage ou Nothing ; position supposée exprimée dans le repère du modèle. | `com_backend.py:1797` (`sketch_dimension`) |
| `IDisplayDimension.Diametric` | aide 2026 | propriété booléenne : bascule entre cote de rayon et cote de diamètre. | `com_backend.py:1808` (`sketch_dimension`) |
| `IDisplayDimension.GetDimension2` | aide 2026 | (Index) → IDimension ; 0 = cote principale. | `com_backend.py:1811` (`sketch_dimension`) |
| `IDimension.Name` | aide 2026 | propriété chaîne. | `com_backend.py:1814` (`sketch_dimension`) |
| `IDimension.Name` | aide 2026 | lecture. | `com_backend.py:1818` (`sketch_dimension`) |
| `IFeature.GetSpecificFeature2` | aide 2026 | sans argument → objet spécifique (ISketch pour une fonction « ProfileFeature »). | `com_backend.py:1828` (`_sketch_of`) |
| `ISketch.RelationManager / ISketchRelationManager.GetRelations` | aide 2026 | propriété puis (Filter) → tableau de ISketchRelation. | `com_backend.py:1834` (`_relations`) |
| `ISketchRelation.GetRelationType` | aide 2026 | entier de swConstraintType_e. | `com_backend.py:1839` (`_relations`) |
| `ISketchRelation.GetEntities` | aide 2026 | tableau des entités liées (segments, points, ou entités extérieures à l'esquisse). | `com_backend.py:1843` (`_relations`) |
| `IFeature.GetFirstDisplayDimension` | aide 2026 | première IDisplayDimension ou Nothing. | `com_backend.py:1887` (`_dimensions_of`) |
| `IDisplayDimension.GetDimension2` | aide 2026 | (Index) → IDimension. | `com_backend.py:1891` (`_dimensions_of`) |
| `IFeature.GetNextDisplayDimension` | aide 2026 | (DispIn) → suivante ou Nothing. | `com_backend.py:1895` (`_dimensions_of`) |
| `IDimension.GetType` | aide 2026 | entier de swDimensionParamType_e. | `com_backend.py:1900` (`_dim_unit`) |
| `IDimension.SystemValue` | aide 2026 | propriété réelle en unités système (m ou rad), pour la configuration active. | `com_backend.py:1904` (`_dim_value`) |
| `IDimension.FullName` | aide 2026 | « cote@fonction@document » ; les deux premiers champs forment le nom accepté par IModelDoc2.Parameter. | `com_backend.py:1910` (`_dim_info`) |
| `IDimension.Name` | aide 2026 | Signature et type de retour tels que lus dans l'aide. | `com_backend.py:1916` (`_dim_info`) |
| `IDimension.SetSystemValue3` | aide 2026 | (NewValue, WhichConfigurations, Config_names) → swSetValueReturnStatus_e ; Config_names vide avec swThisConfiguration. | `com_backend.py:1930` (`_set_system_value`) |
| `IModelDoc2.SelectionManager` | aide 2026 | propriété, ISelectionMgr. | `com_backend.py:1958` (`_selection_count`) |
| `ISelectionMgr.GetSelectedObjectCount2` | aide 2026 | (Mark) → entier ; -1 = toutes marques. | `com_backend.py:1960` (`_selection_count`) |
| `IModelDocExtension.GetWhatsWrongCount` | aide 2026 | nombre d'erreurs de reconstruction. | `com_backend.py:1972` (`_failure`) |
| `IModelDocExtension.DeleteSelection2` | aide 2026 | (DeleteOptions de swDeleteSelectionOptions_e) → booléen. | `com_backend.py:2000` (`_delete`) |
| `IFeature.GetErrorCode2` | aide 2026 | (ByRef IsWarning) → swFeatureError_e ; IsWarning n'a de sens que si le code est non nul. | `com_backend.py:2031` (`_create`) |
| `IFeatureManager.FeatureExtrusion3` | aide 2026 | 23 arguments : les 17 communs, puis Merge, UseFeatScope, UseAutoSelect, T0, StartOffset, FlipStartOffset → IFeature ou Nothing. | `com_backend.py:2097` (`build`) |
| `IFeatureManager.FeatureCut4` | aide 2026 | 27 arguments : les 17 communs, puis NormalCut, UseFeatScope, UseAutoSelect, AssemblyFeatureScope, AutoSelectComponents, PropagateFeatureToParts, T0, StartOffset, FlipStartOffset, OptimizeGeometry → IFeature ou Nothing. FeatureCut3 (aide 2026) : les mêmes sans OptimizeGeometry. | `com_backend.py:2103` (`build`) |
| `ISketchSegment.GetSketch` | aide 2026 | sans argument → ISketch propriétaire du segment. | `com_backend.py:2122` (`revolve`) |
| `IFeatureManager.FeatureRevolve2` | aide 2026 | 20 arguments : SingleDir, IsSolid, IsThin, IsCut, ReverseDir, BothDirectionUpToSameEntity, Dir1Type, Dir2Type, Dir1Angle, Dir2Angle, OffsetReverse1, OffsetReverse2, OffsetDistance1, OffsetDistance2, ThinType, ThinThickness1, ThinThickness2, Merge, UseFeatScope, UseAutoSelect → IFeature ; esquisse marque 0, axe marque 16. | `com_backend.py:2135` (`build`) |
| `IFeatureManager.CreateDefinition` | aide 2026 | (Type de swFeatureNameID_e) → objet de définition ; swFmFillet donne un ISimpleFilletFeatureData2. | `com_backend.py:2164` (`build`) |
| `ISimpleFilletFeatureData2.Initialize` | aide 2026 | (FilletType de swSimpleFilletType_e) → booléen. | `com_backend.py:2167` (`build`) |
| `ISimpleFilletFeatureData2.ConicTypeForCrossSectionProfile / DefaultRadius / PropagateToTangentFaces` | aide 2026 | propriétés ; les arêtes présélectionnées (marque 1, comme pour FeatureFillet3) sont supposées reprises par CreateFeature. | `com_backend.py:2171` (`build`) |
| `IFeatureManager.CreateFeature` | aide 2026 | (FeatureData) → IFeature ou Nothing. | `com_backend.py:2177` (`build`) |
| `IFeatureManager.FeatureFillet3` | aide 2026 | (Options, R1, R2, Rho, Ftyp, OverflowType, ConicRhoType, Radii, Dist2Arr, RhoArr, SetBackDistances, PointRadiusArray, PointDist2Array, PointRhoArray) → IFeature ; tableaux vides pour un rayon uniforme. | `com_backend.py:2179` (`build`) |
| `IFeatureManager.InsertFeatureChamfer` | aide 2026 | (Options, ChamferType, Width, Angle, OtherDist, VertexChamDist1, VertexChamDist2, VertexChamDist3) → IFeature ; arêtes présélectionnées (marque non documentée : 0 supposé). | `com_backend.py:2205` (`build`) |
| `IModelDoc2.InsertFeatureShell` | aide 2026 | (Thickness en mètres, Outward), sans retour ; faces à retirer présélectionnées avec la marque 1. | `com_backend.py:2240` (`build`) |
| `IModelDoc2.InsertAxis2` | aide 2026 | (AutoSize) → booléen ; deux plans présélectionnés sont supposés donner l'axe de leur intersection. | `com_backend.py:2288` (`_ref_axis`) |
| `IFeatureManager.FeatureLinearPattern5` | aide 2026 | 22 arguments : Num1, Spacing1, Num2, Spacing2, FlipDir1, FlipDir2, DName1, DName2, GeometryPattern, VaryInstance, HasOffset1, HasOffset2, CtrlByNum1, CtrlByNum2, FromCentroid1, FromCentroid2, RevOffset1, RevOffset2, Offset1, Offset2, D2PatternSeedOnly, SyncSubAssemblies → IFeature ; direction marque 1, fonctions marque 4. FeatureLinearPattern4 (aide 2026) : les 20 premiers. | `com_backend.py:2322` (`build`) |
| `IFeatureManager.FeatureCircularPattern5` | aide 2026 | 14 arguments : Number, Spacing (radians, pas entre instances quand EqualSpacing est faux), FlipDirection, DName, GeometryPattern, EqualSpacing, VaryInstance, SyncSubAssemblies, BDir2, BSymmetric, Number2, Spacing2, DName2, EqualSpacing2 → IFeature ; axe marque 1, fonctions marque 4. FeatureCircularPattern4 : les 7 premiers. | `com_backend.py:2358` (`build`) |
| `IFeatureManager.InsertMirrorFeature2` | aide 2026 | (BMirrorBody, BGeometryPattern, BMerge, BKnit, ScopeOptions de swFeatureScope_e) → IFeature ; fonctions marque 1, plan marque 2. | `com_backend.py:2390` (`build`) |
| `IModelDoc2.EditRebuild3` | aide 2026 | sans argument → booléen ; ne reconstruit que ce qui doit l'être. | `com_backend.py:2400` (`_rebuild_quiet`) |
| `IModelDoc2.Parameter` | aide 2026 | (StringIn « cote@fonction ») → IDimension ou Nothing. | `com_backend.py:2411` (`set_dimension`) |
| `IModelDoc2.GetEquationMgr` | aide 2026 | sans argument → IEquationMgr. | `com_backend.py:2427` (`set_variable`) |
| `IEquationMgr.GetCount` | aide 2026 | nombre d'équations. | `com_backend.py:2432` (`set_variable`) |
| `IEquationMgr.Equation` | aide 2026 | propriété paramétrée (Index) → texte, sous la forme « "nom" = expression » ; lue comme une méthode en liaison anticipée. | `com_backend.py:2434` (`set_variable`) |
| `IEquationMgr.Equation (écriture)` | aide 2026 | pywin32 génère SetEquation(Index, texte) pour une propriété paramétrée en écriture (convention makepy, de mémoire). | `com_backend.py:2442` (`set_variable`) |
| `IEquationMgr.Add2` | aide 2026 | (Index, Equation, Solve) → index ou -1 ; -1 en entrée = ajout en fin de liste. | `com_backend.py:2446` (`set_variable`) |
| `IModelDoc2.EditSuppress2 / EditUnsuppress2` | aide 2026 | sans argument → booléen ; agissent sur la fonction sélectionnée, dans la configuration active. | `com_backend.py:2471` (`suppress_feature`) |
| `IFeature.IsSuppressed` | aide 2026 | sans argument → booléen (configuration active). | `com_backend.py:2475` (`suppress_feature`) |
| `IFeature.GetChildren` | aide 2026 | tableau des fonctions enfants (dépendances directes). | `com_backend.py:2486` (`_children`) |
| `IFeature.GetErrorCode2` | aide 2026 | (ByRef IsWarning) → swFeatureError_e. | `com_backend.py:2524` (`_error_of`) |
| `IFeature.IsSuppressed` | aide 2026 | sans argument → booléen (configuration active). | `com_backend.py:2536` (`_node`) |
| `IAssemblyDoc.ResolveAllLightWeightComponents` | aide 2026 | (WarnUser) → statut de swComponentResolveStatus_e ; faux = pas de dialogue. | `com_backend.py:2547` (`_components`) |
| `IAssemblyDoc.GetComponents` | aide 2026 | (ToplevelOnly) → tableau de IComponent2. | `com_backend.py:2551` (`_components`) |
| `IComponent2.GetSuppression2` | aide 2026 | entier de swComponentSuppressionState_e. | `com_backend.py:2556` (`_components`) |
| `IComponent2.Name2` | aide 2026 | propriété chaîne (« pièce-1 »). | `com_backend.py:2561` (`_components`) |
| `IComponent2.IsSuppressed` | aide 2026 | sans argument → booléen. | `com_backend.py:2564` (`_components`) |
| `IFeature.GetFirstSubFeature / GetNextSubFeature` | aide 2026 | sous-fonctions (esquisse absorbée par une extrusion, par exemple), Nothing en fin de liste. | `com_backend.py:2583` (`tree`) |
| `IModelDoc2.ForceRebuild3` | aide 2026 | (TopOnly) → booléen ; faux = tous les niveaux. | `com_backend.py:2608` (`rebuild`) |
| `IFeature.GetSpecificFeature2` | aide 2026 | → ISketch pour une esquisse. | `com_backend.py:2634` (`rebuild`) |
| `IPartDoc.GetBodies2` | aide 2026 | (BodyType de swBodyType_e, BVisibleOnly) → tableau de IBody2, Nothing sans corps. | `com_backend.py:2646` (`_bodies`) |
| `IPartDoc.GetPartBox` | aide 2026 | (NoConversion) → 6 réels, deux coins opposés ; vrai = unités système (mètres). L'aide précise que la boîte est approchée. | `com_backend.py:2651` (`_part_box`) |
| `IModelDocExtension.CreateMassProperty2 / CreateMassProperty` | aide 2026 | sans argument → IMassProperty2 / IMassProperty. | `com_backend.py:2658` (`_mass_properties`) |
| `IMassProperty2.UseSystemUnits` | aide 2026 | propriété booléenne ; vrai = m, m², m³, kg. | `com_backend.py:2664` (`_mass_properties`) |
| `IBody2.GetFaceCount / GetEdgeCount` | aide 2026 | sans argument → entier. | `com_backend.py:2676` (`body_stats`) |
| `IMassProperty2.Volume` | aide 2026 | propriété réelle, m³ en unités système. | `com_backend.py:2679` (`body_stats`) |
| `IMassProperty2.Volume / SurfaceArea / Mass / Density / CenterOfMass` | aide 2026 | propriétés en lecture ; m³, m², kg, kg/m³ et tableau [x, y, z] en mètres avec UseSystemUnits. | `com_backend.py:2704` (`mass`) |
| `IFace2.GetSurface` | aide 2026 | sans argument → ISurface. | `com_backend.py:2738` (`_surface`) |
| `ISurface.IsPlane` | aide 2026 | sans argument → booléen. | `com_backend.py:2742` (`_is_planar`) |
| `ISurface.IsPlane / IsCylinder / IsCone / IsSphere / IsTorus` | aide 2026 | → booléen. | `com_backend.py:2746` (`_surface_kind`) |
| `IFace2.GetArea` | aide 2026 | aire en m². | `com_backend.py:2757` (`_face_info`) |
| `IFace2.GetBox` | aide 2026 | 6 réels, deux coins opposés de la boîte de la face (mètres). | `com_backend.py:2759` (`_face_info`) |
| `IFace2.Normal` | aide 2026 | tableau de 3 réels ; (0, 0, 0) si la face n'est pas plane ; supposé orienté vers l'extérieur de la matière. | `com_backend.py:2765` (`_face_info`) |
| `IFace2.GetClosestPointOn` | aide 2026 | (X, Y, Z) → 5 réels dont le point de la face le plus proche. Le « centroïde » publié est le centre de la boîte ramené sur la face : exact pour un rectangle, approché sinon. | `com_backend.py:2770` (`_face_info`) |
| `ISurface.CylinderParams` | aide 2026 | 7 réels : origine, axe, rayon (mètres). | `com_backend.py:2778` (`_face_info`) |
| `IFace2.GetFeature` | aide 2026 | sans argument → fonction propriétaire de la face. | `com_backend.py:2780` (`_face_info`) |
| `IBody2.GetFaces` | aide 2026 | sans argument → tableau de IFace2. | `com_backend.py:2796` (`faces`) |
| `IEdge.GetCurve` | aide 2026 | sans argument → ICurve. | `com_backend.py:2802` (`_edge_info`) |
| `IEdge.GetCurveParams3` | aide 2026 | sans argument → ICurveParamData. | `com_backend.py:2804` (`_edge_info`) |
| `ICurveParamData.UMinValue / UMaxValue / StartPoint / EndPoint` | aide 2026 | propriétés : bornes du paramètre et extrémités (3 réels, mètres). | `com_backend.py:2806` (`_edge_info`) |
| `ICurve.GetLength3` | aide 2026 | (StartParam, EndParam) → longueur en mètres. | `com_backend.py:2813` (`_edge_info`) |
| `ICurve.Evaluate2` | aide 2026 | (Parameter, NumberOfDerivatives) → tableau commençant par le point [x, y, z] en mètres. | `com_backend.py:2815` (`_edge_info`) |
| `ICurve.IsLine / IsCircle` | aide 2026 | sans argument → booléen. | `com_backend.py:2822` (`_edge_info`) |
| `ICurve.CircleParams` | aide 2026 | 7 réels : centre, axe, rayon (mètres). | `com_backend.py:2826` (`_edge_info`) |
| `IEdge.GetTwoAdjacentFaces2` | aide 2026 | sans argument → tableau de deux IFace2. | `com_backend.py:2833` (`_edge_info`) |
| `IBody2.GetEdges` | aide 2026 | sans argument → tableau de IEdge. | `com_backend.py:2853` (`edges`) |
| `IEdge.GetCurve` | aide 2026 | Signature et type de retour tels que lus dans l'aide. | `com_backend.py:2869` (`_operand`) |
| `ICurve.IsLine` | aide 2026 | Signature et type de retour tels que lus dans l'aide. | `com_backend.py:2870` (`_operand`) |
| `IFace2.GetClosestPointOn / IEdge.GetClosestPointOn` | aide 2026 | (X, Y, Z en mètres) → 5 réels commençant par le point le plus proche. | `com_backend.py:2885` (`measure`) |
| `IModelDocExtension.CreateMeasure` | aide 2026 | sans argument → IMeasure. | `com_backend.py:2892` (`measure`) |
| `IMeasure.Calculate` | aide 2026 | (Entities : tableau d'entités, ou Nothing pour la sélection) → booléen ; tableau passé en VT_ARRAY\|VT_DISPATCH. | `com_backend.py:2894` (`measure`) |
| `IMeasure.Distance / IMeasure.Angle` | aide 2026 | distance minimale (m) et angle (rad), -1 quand la grandeur n'a pas de sens pour ces entités. | `com_backend.py:2898` (`measure`) |
| `IAssemblyDoc.InterferenceDetectionManager` | aide 2026 | propriété en lecture. | `com_backend.py:2913` (`interference`) |
| `IInterferenceDetectionMgr.GetInterferences` | aide 2026 | tableau de IInterference. | `com_backend.py:2917` (`interference`) |
| `IInterference.Components / Volume` | aide 2026 | tableau de IComponent2 ; volume en m³. | `com_backend.py:2920` (`interference`) |
| `IInterferenceDetectionMgr.Done` | aide 2026 | sans argument, clôt la détection. | `com_backend.py:2927` (`interference`) |
| `IModelDoc2.SaveBMP` | aide 2026 | (FileNameIn, WidthIn, HeightIn) → booléen ; écrit la vue du modèle, sans l'interface. | `com_backend.py:2941` (`_shoot`) |
| `IModelDoc2.ActiveView` | aide 2026 | propriété, IModelView de la vue active. | `com_backend.py:2963` (`capture`) |
| `IModelView.Orientation3 / Translation3 / Scale2` | aide 2026 | propriétés en lecture et écriture (IMathTransform, IMathVector, réel). | `com_backend.py:2967` (`capture`) |
| `IModelDoc2.ShowNamedView2` | aide 2026 | (VName, ViewId de swStandardViews_e), sans retour ; nom vide = ViewId seul. | `com_backend.py:2974` (`capture`) |
| `IModelDoc2.ViewZoomtofit2` | aide 2026 | sans argument. | `com_backend.py:2978` (`capture`) |
| `IModelDoc2.GraphicsRedraw2` | aide 2026 | sans argument. | `com_backend.py:2985` (`capture`) |
| `api_call_raw` | aide non consultée : membre choisi par l'appelant | appel direct du membre nommé ; un membre sans argument est lu comme une propriété s'il n'est pas appelable. | `com_backend.py:3032` (`raw_call`) |
| `LIBID de swconst.tlb` | aide non consultée : de mémoire | n'est utilisé qu'en repli, quand swconst.tlb n'est pas trouvé à côté de l'exécutable. | `constants.py:21` (`(module)`) |
| `ISldWorks.GetExecutablePath` | aide 2026 | renvoie le dossier de sldworks.exe, où se trouve swconst.tlb (emplacement du fichier : de mémoire). | `constants.py:374` (`_load_from_file`) |

## Hors `com_backend.py`

Ces appels n'ont jamais été exécutés non plus ; chacun porte un marqueur `UNVERIFIED` dans le code.

| Fichier | Appels | Hypothèse | Aide consultée |
| --- | --- | --- | --- |
| `src/swmcp/apiindex/indexer.py` | `pythoncom.LoadTypeLib`, `ITypeLib.GetTypeInfo`, `ITypeInfo.GetTypeAttr` / `GetFuncDesc` / `GetNames` / `GetVarDesc`, lecture du registre | Forme des objets pywin32 ; clé `HKLM\SOFTWARE\SolidWorks\SOLIDWORKS <année>\Setup`, valeur `SolidWorks Folder` ; fichiers `sldworks.tlb`, `swconst.tlb`, `swcommands.tlb`, `swpublished.tlb` ; constantes `VT_*`, `TKIND_*`, `PARAMFLAG_*` recopiées du SDK Windows | Non consultée : de mémoire |
| `src/swmcp/doctor.py` | `winreg.OpenKey(HKEY_CLASSES_ROOT, "SldWorks.Application\CLSID")` | Le ProgID est enregistré après un premier lancement de SolidWorks | Non consultée : de mémoire |
| `src/swmcp/doctor.py` | `gencache.EnsureModule("{83A33D31-27C5-11CE-BFD4-00400513BB57}", 0, 0, 0)` | Ce LIBID est celui de `sldworks.tlb` ; un échec ne donne qu'un avertissement | Non consultée : de mémoire |
| `src/swmcp/backend/worker.py` | `pythoncom.CoInitialize` / `CoUninitialize` dans le thread du worker | Appartement mono-thread, créé et libéré par le même thread | Non consultée : de mémoire |
