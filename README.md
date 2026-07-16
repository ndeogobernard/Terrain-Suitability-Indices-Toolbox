# Development Suitability Indices Toolbox

An ArcGIS Pro Python Toolbox that classifies land within any zonal polygon
layer (traffic analysis zones, census tracts, parcels, planning districts)
into an 8-category development suitability scheme based on water/utility
service, floodplain, and slope constraints.

```
WF   WL   WM   WS      Water-served:     Floodplain / Level / Moderate / Steep
NWF  NWL  NWM  NWS      Not water-served: Floodplain / Level / Moderate / Steep
```

Floodplain is treated as an absolute constraint that overrides slope
classification wherever the two overlap.

## Why this exists

Planning agencies routinely need more than a zone's total acreage - they
need to know how much of it is realistically developable given
infrastructure and terrain constraints. This toolbox automates that
classification for any zone/constraint layer combination, at any scale,
including statewide datasets with 1M+ input terrain polygons.

## What's included

- **`DevelopmentIndicesToolbox.pyt`** - the ArcGIS Pro Python Toolbox.
  Contains two tools:
  - **1 - Validate Inputs**: a read-only diagnostic that checks field
    types, spatial reference agreement across layers, zone-ID
    uniqueness/range, and flags any zone geometry that's out of scale with
    the rest of the dataset. Run this first on any new dataset.
  - **2 - Calculate Dev Indices**: the production classification tool.
    Every input - zone layer, water/floodplain/slope layers, field names,
    even the slope classification breakpoints - is a user-supplied
    parameter, so it adapts to any dataset rather than assuming a fixed
    schema.

## Requirements

- ArcGIS Pro (developed and tested against a Standard-license install; the
  tool automatically detects and uses parallelized Pairwise geoprocessing
  tools if an Advanced license is available, and falls back to standard
  tools otherwise - no manual configuration needed either way)
- No additional Python packages required beyond what ships with
  `arcgispro-py3`

## Installation

1. Click the green **Code** button above and choose **Download ZIP**, or
   clone the repository.
2. Open ArcGIS Pro.
3. In the **Catalog** pane, right-click **Toolboxes**, choose **Add
   Toolbox**, and select `DevelopmentIndicesToolbox.pyt`.
4. Both tools will appear under the toolbox in the Catalog pane, ready to
   run from there or added to a model/script.

## Usage

1. Run **1 - Validate Inputs** first against your zone layer and the three
   constraint layers. Review any warnings in the geoprocessing messages
   pane before proceeding.
2. Run **2 - Calculate Dev Indices**, supplying:
   - Zone polygon layer + its unique ID field
   - (Optional) a SQL query to restrict which zones are processed
   - Water service area, floodplain, and slope polygon layers
   - The slope layer's percent-slope field
   - Slope classification breakpoints (defaults: 12% and 25%)
   - An output geodatabase and table name
3. The output table includes one row per zone with all 8 category
   acreages, a `TotalAcres` sum, an `UnclassifiedAcres` column (area inside
   a zone but outside both the Slope and Flood layers' coverage - should be
   near zero), an independently-computed `ZoneGeodesicAcres` for QA, and an
   `AcreageDiff` column that should be ~0 for every zone if everything
   closes correctly.

## Performance design

The slope/terrain input is expected to potentially be very large (SSURGO-
scale soils data commonly exceeds a million polygons for a state). Rather
than overlaying that layer directly against the zone layer, the tool first
splits it into three selections by percent-slope and dissolves each down
to a small number of large polygons *before* any overlay - this is the
dominant factor in making the tool practical at statewide scale.

## Validation

The output's built-in QA check (independently-computed zone area vs. the
sum of the 8 category acreages) was run against a real statewide dataset
of several thousand zones. Maximum discrepancy across all zones: a
fraction of an acre on zones tens of thousands of acres in size - a
relative error on the order of 0.0004%.

## License

MIT - see `LICENSE`.

## More

A narrative walkthrough of the design decisions behind this tool is
available as an ArcGIS StoryMap: **[link once published]**


