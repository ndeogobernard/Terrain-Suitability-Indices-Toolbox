"""
Development Suitability Indices Toolbox
====================================================================
An ArcGIS Pro Python Toolbox that classifies TAZ (or any zonal polygon
layer) area into an 8-category development suitability scheme based on
water service availability and terrain constraints (floodplain, slope):

    WF, WL, WM, WS, NWF, NWL, NWM, NWS

    W  = inside the Water Service Area layer      NW = outside it
    F  = inside the Floodplain layer (overrides slope classification)
    L / M / S = Level / Moderate / Steep, by percent-slope breakpoints
                (both breakpoints are user-configurable)

Two tools are provided:
    1. Validate Inputs      -- read-only diagnostic. Checks field types,
                                spatial reference agreement across layers,
                                zone-ID uniqueness, and flags any zone
                                geometry that's wildly out of scale with
                                the rest of the dataset.
    2. Calculate Dev Indices -- the production calculation. Automatically
                                detects whether Pairwise (parallelized)
                                geoprocessing tools are available on the
                                current license and falls back to chained
                                2-input Union calls on Standard/Basic
                                licenses. Writes a wide-format table with
                                one row per zone plus an independent
                                geodesic-area QA column.

Performance note: the Slope input is expected to potentially be a very
large polygon count (SSURGO-scale soils data, 1M+ features is common).
The tool splits it into 3 selections by percent-slope and dissolves each
down BEFORE any overlay -- this is the key optimization, since overlaying
the raw high-feature-count layer directly against the zone layer is the
dominant cost otherwise.

Install: in ArcGIS Pro's Catalog pane, right-click Toolboxes > Add
Toolbox, and select this .pyt file. The two tools will appear under it.
"""

import arcpy
import os
import time
from collections import defaultdict

SQFT_PER_ACRE = 43560.0


# =====================================================================
# Shared helpers
# =====================================================================
def find_field(fc, base_name):
    """Union's join-indicator field naming (e.g. FID_<layer>) shifts
    slightly between ArcGIS versions -- find it by prefix instead of
    hard-coding the exact name."""
    for f in arcpy.ListFields(fc):
        if f.name.startswith(base_name):
            return f.name
    raise RuntimeError(f"Could not find a field starting with '{base_name}' "
                        f"in {fc}. Available fields: "
                        f"{[f.name for f in arcpy.ListFields(fc)]}")


def dissolve(in_fc, out_fc, where=None):
    """Dissolve in_fc (optionally filtered) into one multipart shape.
    Tries PairwiseDissolve first (parallelized, Advanced license only),
    falls back to classic Dissolve."""
    lyr = "tmp_dissolve_lyr"
    if arcpy.Exists(lyr):
        arcpy.management.Delete(lyr)
    arcpy.management.MakeFeatureLayer(in_fc, lyr, where_clause=where)

    if arcpy.Exists(out_fc):
        arcpy.management.Delete(out_fc)

    if hasattr(arcpy.analysis, "PairwiseDissolve"):
        try:
            arcpy.analysis.PairwiseDissolve(lyr, out_fc, multi_part="MULTI_PART")
            arcpy.management.Delete(lyr)
            return
        except Exception:
            pass

    arcpy.management.Dissolve(lyr, out_fc, multi_part="MULTI_PART")
    arcpy.management.Delete(lyr)


def union(in_fcs, out_fc, messages):
    """Union a list of feature classes. Tries PairwiseUnion first (no
    input-count limit). Falls back to chained 2-input Union calls, since
    a Basic/Standard license caps classic Union at 2 inputs."""
    if arcpy.Exists(out_fc):
        arcpy.management.Delete(out_fc)

    if hasattr(arcpy.analysis, "PairwiseUnion"):
        try:
            arcpy.analysis.PairwiseUnion(in_fcs, out_fc)
            return
        except Exception:
            messages.addMessage("PairwiseUnion failed; falling back to "
                                 "chained Union.")

    if len(in_fcs) <= 2:
        arcpy.analysis.Union(in_fcs, out_fc)
        return

    messages.addMessage(f"Chaining {len(in_fcs)} inputs through "
                         f"{len(in_fcs) - 1} sequential 2-input Union "
                         f"calls (Standard/Basic license limit).")
    running_fc = in_fcs[0]
    for i, next_fc in enumerate(in_fcs[1:], start=1):
        step_out = out_fc if i == len(in_fcs) - 1 else f"{out_fc}_step{i}"
        if arcpy.Exists(step_out):
            arcpy.management.Delete(step_out)
        arcpy.analysis.Union([running_fc, next_fc], step_out)
        messages.addMessage(f"  Union step {i}/{len(in_fcs) - 1} done.")
        running_fc = step_out


def check_sr_agreement(fcs, messages):
    """Warn (don't fail) if the input layers aren't in the same spatial
    reference -- overlay tools will reproject on the fly, but silently,
    which can mask a mismatch the user should know about."""
    srs = {}
    for name, fc in fcs.items():
        sr = arcpy.Describe(fc).spatialReference
        srs[name] = (sr.name, sr.factoryCode)
    unique_srs = set(srs.values())
    if len(unique_srs) > 1:
        messages.addWarningMessage(
            "Input layers are NOT all in the same spatial reference:")
        for name, (sr_name, code) in srs.items():
            messages.addWarningMessage(f"  {name}: {sr_name} ({code})")
        messages.addWarningMessage(
            "Overlay tools will reproject on the fly, but consider "
            "reprojecting explicitly first for predictable results.")
    return srs


# =====================================================================
# Toolbox definition
# =====================================================================
class Toolbox(object):
    def __init__(self):
        self.label = "Development Suitability Indices"
        self.alias = "devindices"
        self.tools = [ValidateInputs, CalculateDevIndices]


# =====================================================================
# Tool 1 -- Validate Inputs
# =====================================================================
class ValidateInputs(object):
    def __init__(self):
        self.label = "1 - Validate Inputs"
        self.description = (
            "Read-only diagnostic. Checks field types, spatial reference "
            "agreement, zone-ID uniqueness/range, and flags any zone "
            "geometry that's out of scale with the rest of the dataset. "
            "Run this before Calculate Dev Indices, especially on a new "
            "dataset."
        )
        self.canRunInBackground = False

    def getParameterInfo(self):
        p_zone = arcpy.Parameter(
            displayName="Zone Polygons (e.g. TAZ)", name="zone_fc",
            datatype="GPFeatureLayer", parameterType="Required", direction="Input")
        p_zone_id = arcpy.Parameter(
            displayName="Zone ID Field", name="zone_id_field",
            datatype="Field", parameterType="Required", direction="Input")
        p_zone_id.parameterDependencies = [p_zone.name]
        p_water = arcpy.Parameter(
            displayName="Water Service Area Polygons", name="water_fc",
            datatype="GPFeatureLayer", parameterType="Required", direction="Input")
        p_flood = arcpy.Parameter(
            displayName="Floodplain Polygons", name="flood_fc",
            datatype="GPFeatureLayer", parameterType="Required", direction="Input")
        p_slope = arcpy.Parameter(
            displayName="Slope Polygons", name="slope_fc",
            datatype="GPFeatureLayer", parameterType="Required", direction="Input")
        p_slope_field = arcpy.Parameter(
            displayName="Percent Slope Field", name="slope_field",
            datatype="Field", parameterType="Required", direction="Input")
        p_slope_field.parameterDependencies = [p_slope.name]
        return [p_zone, p_zone_id, p_water, p_flood, p_slope, p_slope_field]

    def execute(self, parameters, messages):
        zone_fc, zone_id_field, water_fc, flood_fc, slope_fc, slope_field = \
            [p.valueAsText for p in parameters]

        messages.addMessage("=" * 70)
        messages.addMessage("Spatial reference check")
        messages.addMessage("=" * 70)
        check_sr_agreement(
            {"Zone": zone_fc, "Water": water_fc, "Flood": flood_fc, "Slope": slope_fc},
            messages)

        messages.addMessage("\n" + "=" * 70)
        messages.addMessage(f"Zone ID field check ({zone_id_field})")
        messages.addMessage("=" * 70)
        ids = []
        nulls = 0
        with arcpy.da.SearchCursor(zone_fc, [zone_id_field]) as cur:
            for (val,) in cur:
                if val is None:
                    nulls += 1
                else:
                    ids.append(val)
        dupes = len(ids) - len(set(ids))
        messages.addMessage(f"  Total zones: {len(ids) + nulls}, nulls: {nulls}, "
                             f"duplicates: {dupes}")
        if ids:
            messages.addMessage(f"  ID range: {min(ids)} to {max(ids)}")
        if nulls or dupes:
            messages.addWarningMessage(
                "  Null or duplicate IDs found -- these zones will be "
                "dropped or merged incorrectly by Calculate Dev Indices. "
                "Fix before running it.")

        messages.addMessage("\n" + "=" * 70)
        messages.addMessage(f"Slope field check ({slope_field})")
        messages.addMessage("=" * 70)
        fld = [f for f in arcpy.ListFields(slope_fc) if f.name == slope_field][0]
        if fld.type not in ("Double", "Single", "Integer", "SmallInteger"):
            messages.addWarningMessage(
                f"  Field type is {fld.type}, not numeric. Calculate Dev "
                f"Indices expects a raw numeric percent-slope value, not "
                f"a pre-classified category.")
        else:
            messages.addMessage(f"  Field type OK ({fld.type}).")

        messages.addMessage("\n" + "=" * 70)
        messages.addMessage("Zone geometry scale check")
        messages.addMessage("=" * 70)
        exts = [arcpy.Describe(fc).extent for fc in (water_fc, flood_fc, slope_fc)]
        ref_xmin = min(e.XMin for e in exts)
        ref_xmax = max(e.XMax for e in exts)
        ref_ymin = min(e.YMin for e in exts)
        ref_ymax = max(e.YMax for e in exts)
        pad_x = (ref_xmax - ref_xmin) * 0.25
        pad_y = (ref_ymax - ref_ymin) * 0.25

        outliers = []
        with arcpy.da.SearchCursor(zone_fc, [zone_id_field, "OID@", "SHAPE@"]) as cur:
            for zid, oid, shp in cur:
                if shp is None:
                    continue
                e = shp.extent
                if (e.XMin < ref_xmin - pad_x or e.XMax > ref_xmax + pad_x or
                        e.YMin < ref_ymin - pad_y or e.YMax > ref_ymax + pad_y):
                    outliers.append((oid, zid))
        if outliers:
            messages.addWarningMessage(
                f"  {len(outliers)} zone(s) have extent far outside the "
                f"combined Water/Flood/Slope extent -- likely bad geometry "
                f"or the zone layer covers a larger area than intended "
                f"(e.g. filter to your study area first). First few: "
                f"{outliers[:10]}")
        else:
            messages.addMessage("  No zone geometry outliers found.")

        messages.addMessage("\nValidation complete.")


# =====================================================================
# Tool 2 -- Calculate Dev Indices
# =====================================================================
class CalculateDevIndices(object):
    def __init__(self):
        self.label = "2 - Calculate Dev Indices"
        self.description = (
            "Computes acreage in 8 categories (WF/WL/WM/WS/NWF/NWL/NWM/NWS) "
            "per zone, based on water service, floodplain, and slope. "
            "Pre-dissolves the Slope layer by class before overlay for "
            "performance on large soils datasets. Automatically detects "
            "and adapts to Standard/Basic vs Advanced license."
        )
        self.canRunInBackground = False

    def getParameterInfo(self):
        p_zone = arcpy.Parameter(
            displayName="Zone Polygons (e.g. TAZ)", name="zone_fc",
            datatype="GPFeatureLayer", parameterType="Required", direction="Input")
        p_zone_id = arcpy.Parameter(
            displayName="Zone ID Field", name="zone_id_field",
            datatype="Field", parameterType="Required", direction="Input")
        p_zone_id.parameterDependencies = [p_zone.name]
        p_zone_query = arcpy.Parameter(
            displayName="Zone Selection Query (optional)", name="zone_query",
            datatype="GPSQLExpression", parameterType="Optional", direction="Input")
        p_zone_query.parameterDependencies = [p_zone.name]

        p_water = arcpy.Parameter(
            displayName="Water Service Area Polygons", name="water_fc",
            datatype="GPFeatureLayer", parameterType="Required", direction="Input")
        p_flood = arcpy.Parameter(
            displayName="Floodplain Polygons", name="flood_fc",
            datatype="GPFeatureLayer", parameterType="Required", direction="Input")
        p_slope = arcpy.Parameter(
            displayName="Slope Polygons", name="slope_fc",
            datatype="GPFeatureLayer", parameterType="Required", direction="Input")
        p_slope_field = arcpy.Parameter(
            displayName="Percent Slope Field", name="slope_field",
            datatype="Field", parameterType="Required", direction="Input")
        p_slope_field.parameterDependencies = [p_slope.name]

        p_break1 = arcpy.Parameter(
            displayName="Level/Moderate Slope Breakpoint (%)", name="break1",
            datatype="GPDouble", parameterType="Required", direction="Input")
        p_break1.value = 12.0
        p_break2 = arcpy.Parameter(
            displayName="Moderate/Steep Slope Breakpoint (%)", name="break2",
            datatype="GPDouble", parameterType="Required", direction="Input")
        p_break2.value = 25.0

        p_out_gdb = arcpy.Parameter(
            displayName="Output Geodatabase", name="out_gdb",
            datatype="DEWorkspace", parameterType="Required", direction="Input")
        p_out_name = arcpy.Parameter(
            displayName="Output Table Name", name="out_name",
            datatype="GPString", parameterType="Required", direction="Input")
        p_out_name.value = "DevIndices_Results"

        return [p_zone, p_zone_id, p_zone_query, p_water, p_flood, p_slope,
                p_slope_field, p_break1, p_break2, p_out_gdb, p_out_name]

    def execute(self, parameters, messages):
        t0 = time.time()

        def log(msg):
            messages.addMessage(f"[{time.time() - t0:7.1f}s] {msg}")

        (zone_fc, zone_id_field, zone_query, water_fc, flood_fc, slope_fc,
         slope_field, break1, break2, out_gdb, out_name) = \
            [p.valueAsText for p in parameters]
        break1 = float(break1)
        break2 = float(break2)

        check_sr_agreement(
            {"Zone": zone_fc, "Water": water_fc, "Flood": flood_fc, "Slope": slope_fc},
            messages)

        # ---- Prepare zone selection --------------------------------
        log("Preparing zone layer...")
        zone_lyr = "zone_lyr"
        if arcpy.Exists(zone_lyr):
            arcpy.management.Delete(zone_lyr)
        arcpy.management.MakeFeatureLayer(zone_fc, zone_lyr, where_clause=zone_query or None)
        zone_selected = os.path.join(out_gdb, "Zone_Selected")
        if arcpy.Exists(zone_selected):
            arcpy.management.Delete(zone_selected)
        arcpy.management.CopyFeatures(zone_lyr, zone_selected)
        arcpy.management.Delete(zone_lyr)
        n_zones = int(arcpy.management.GetCount(zone_selected)[0])
        log(f"  {n_zones} zones selected.")

        # ---- Dissolve Water / Flood (defensive re: self-overlap) ----
        log("Dissolving Water Service Area...")
        water_out = os.path.join(out_gdb, "Water_Dissolved")
        dissolve(water_fc, water_out)

        log("Dissolving Floodplain...")
        flood_out = os.path.join(out_gdb, "Flood_Dissolved")
        dissolve(flood_fc, flood_out)

        # ---- Split + dissolve Slope by class (key perf step) --------
        log("Splitting and dissolving Slope by class...")
        class_defs = [
            ("Level",    f"{slope_field} >= 0 AND {slope_field} <= {break1}"),
            ("Moderate", f"{slope_field} > {break1} AND {slope_field} <= {break2}"),
            ("Steep",    f"{slope_field} > {break2}"),
        ]
        class_fcs = []
        for class_name, where in class_defs:
            t = time.time()
            cfc = os.path.join(out_gdb, f"Slope_{class_name}")
            dissolve(slope_fc, cfc, where=where)
            arcpy.management.AddField(cfc, "SlopeClass", "TEXT", field_length=12)
            with arcpy.da.UpdateCursor(cfc, ["SlopeClass"]) as cur:
                for row in cur:
                    cur.updateRow([class_name])
            log(f"  {class_name} done in {time.time() - t:.1f}s")
            class_fcs.append(cfc)

        slope_classified = os.path.join(out_gdb, "Slope_Classified")
        if arcpy.Exists(slope_classified):
            arcpy.management.Delete(slope_classified)
        arcpy.management.Merge(class_fcs, slope_classified)

        # ---- Union ----------------------------------------------------
        log("Running Union...")
        union_fc = os.path.join(out_gdb, "Union_Result")
        union([zone_selected, water_out, flood_out, slope_classified], union_fc, messages)
        n_frag = int(arcpy.management.GetCount(union_fc)[0])
        log(f"  Union produced {n_frag} fragments.")

        # ---- Aggregate --------------------------------------------
        log("Aggregating fragments to zone x category...")
        fid_water_field = find_field(union_fc, "FID_Water_Dissolved")
        fid_flood_field = find_field(union_fc, "FID_Flood_Dissolved")
        zone_id_out_field = find_field(union_fc, zone_id_field)

        fields = [zone_id_out_field, fid_water_field, fid_flood_field,
                  "SlopeClass", "SHAPE@"]
        agg = defaultdict(lambda: defaultdict(float))
        unclassified = defaultdict(float)

        with arcpy.da.SearchCursor(union_fc, fields) as cur:
            for zid, fid_water, fid_flood, slope_class, shape in cur:
                if zid is None:
                    continue
                acres = shape.getArea("GEODESIC", "ACRES") if shape else 0.0
                has_water = fid_water is not None and fid_water != -1
                has_flood = fid_flood is not None and fid_flood != -1

                if has_flood:
                    code = "F"
                elif slope_class == "Level":
                    code = "L"
                elif slope_class == "Moderate":
                    code = "M"
                elif slope_class == "Steep":
                    code = "S"
                else:
                    unclassified[zid] += acres
                    continue

                bucket = f"{'W' if has_water else 'NW'}{code}"
                agg[zid][bucket] += acres

        log("Computing independent geodesic zone area for QA...")
        zone_areas = {}
        with arcpy.da.SearchCursor(zone_selected, [zone_id_field, "SHAPE@"]) as cur:
            for zid, shape in cur:
                zone_areas[zid] = shape.getArea("GEODESIC", "ACRES") if shape else 0.0

        # ---- Build output table -------------------------------------
        log("Building output table...")
        categories = ["WF", "WL", "WM", "WS", "NWF", "NWL", "NWM", "NWS"]
        out_table = os.path.join(out_gdb, out_name)
        if arcpy.Exists(out_table):
            arcpy.management.Delete(out_table)
        arcpy.management.CreateTable(out_gdb, out_name)
        arcpy.management.AddField(out_table, "ZONE_ID", "DOUBLE")
        for cat in categories:
            arcpy.management.AddField(out_table, f"{cat}_Acres", "DOUBLE")
        arcpy.management.AddField(out_table, "TotalAcres", "DOUBLE")
        arcpy.management.AddField(out_table, "UnclassifiedAcres", "DOUBLE")
        arcpy.management.AddField(out_table, "ZoneGeodesicAcres", "DOUBLE")
        arcpy.management.AddField(out_table, "AcreageDiff", "DOUBLE")

        out_fields = (["ZONE_ID"] + [f"{c}_Acres" for c in categories] +
                      ["TotalAcres", "UnclassifiedAcres", "ZoneGeodesicAcres", "AcreageDiff"])
        max_abs_diff = 0.0
        flagged = 0
        with arcpy.da.InsertCursor(out_table, out_fields) as icur:
            for zid in sorted(zone_areas.keys()):
                cat_vals = agg.get(zid, {})
                cat_acres = [cat_vals.get(c, 0.0) for c in categories]
                total = sum(cat_acres)
                unc = unclassified.get(zid, 0.0)
                geo = zone_areas[zid]
                diff = geo - total - unc
                max_abs_diff = max(max_abs_diff, abs(diff))
                if abs(diff) > 0.1:
                    flagged += 1
                icur.insertRow([zid] + cat_acres + [total, unc, geo, diff])

        log(f"QA: max |AcreageDiff| = {max_abs_diff:.4f} acres, "
            f"{flagged} zone(s) over 0.1-acre threshold.")
        log(f"DONE. Output table: {out_table}")


# =====================================================================
# Allow running the .pyt's tools directly for command-line testing
# =====================================================================
if __name__ == "__main__":
    pass
