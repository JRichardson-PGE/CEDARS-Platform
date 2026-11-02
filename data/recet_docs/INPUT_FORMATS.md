# recet input formats

See also: [USER_GUIDE.md](USER_GUIDE.md) (every GUI control), [INPUT_FIELDS.md](INPUT_FIELDS.md) (every input field with units), [OUTPUT_FIELDS.md](OUTPUT_FIELDS.md) and
[CALCULATIONS.md](CALCULATIONS.md) (every output and its formula).

Everything here can be checked without running a job:

    recet check profiles-8760 FILE      recet check profiles-gas FILE
    recet check acc-electric FILE       recet check acc-gas FILE
    recet check run-parameters FILE     recet check sw-factors FILE
    recet check programs FILE
    recet preflight --measures Measure.csv --acc 2026       (or --workbook job.xlsx)

Templates: `recet template all --out templates` (or the GUI's **Template…** buttons and the **Templates** menu).

**Matching rule for every supplied input: a supplied item that matches a built-in one REPLACES it; one that matches nothing is ADDED.**

---------------------------------------------------------------------------------------------------------------------------------

## 1. The job: CET `Measure.csv` and `ProgramCost.csv`

The same files you upload to the CET: pipe (`|`) delimited, header row, UTF-8. `ProgramCost.csv` is optional. ReCET also reads the same columns saved as an ordinary comma-delimited CSV (Excel's *Save as CSV*; text with commas in quotes) or a tab-delimited file: the delimiter is taken from the header line, and a file that is not UTF-8 is read as Windows-1252. The CET itself still needs the pipe-delimited form (the Edit inputs tab's *Save as CET files* writes it).

ReCET renames seven fields from their E3-calculator names, and **a file may use either the new or CET's legacy name** for each: `E3ClimateZone` is read as
`AC_Climate_Zone`, `E3MeaElecEndUseShape` as `AC_Elec_Impact_Profile`, `E3TargetSector` as `AC_Elec_Target_Sector`, `E3GasSavProfile` as `AC_Gas_Impact_Profile`,
`E3GasSector` as `AC_Gas_Target_Sector` (and the ReCET-only `E3MeaElecEndUseShape2nd` / `E3MeaElecEndUseShapeWater` as `AC_Elec_Impact_Profile_2nd_Baseline` /
`AC_Elec_Impact_Profile_Water`). So a CET file runs unchanged. Outputs use the new names; see [INPUT_FIELDS.md](INPUT_FIELDS.md#1-measurecsv).

**Required `Measure.csv` columns** (a missing one stops the run, the preflight names it):

    CEInputID PrgID MeasDescription ClaimYearQuarter Sector AC_Climate_Zone AC_Gas_Impact_Profile AC_Gas_Target_Sector AC_Elec_Impact_Profile AC_Elec_Target_Sector
    IOU_AC_Territory EUL_Yrs RUL_Yrs NumUnits
    UnitkW1stBaseline UnitkWh1stBaseline UnitTherm1stBaseline UnitkW2ndBaseline UnitkWh2ndBaseline UnitTherm2ndBaseline
    NTGRkW NTGRkWh NTGRTherm NTGRCost RealizationRatekW RealizationRatekWh RealizationRateTherm InstallationRatekW InstallationRatekWh InstallationRateTherm
    UnitMeaCost1stBaseline UnitMeaCost2ndBaseline UnitIncentiveToOthers UnitEndUserRebate UnitDirectInstallLab UnitDirectInstallMat
    MeasInflation UnitGasInfraBens UnitRefrigCosts UnitRefrigBens UnitMiscCosts UnitMiscBens UnitTaxCredits

Optional but used when present: `FuelID`, `MeasImpactType` (fuel-substitution measures are recognised from them), `CombustionType` (gas NOx),
`UnitkWhIOUWater1stBaseline` / `UnitkWhIOUWater2ndBaseline` (water energy: the IOU-supplied kWh embedded in the water a measure saves, or uses if negative). All other CET
columns are carried into the output unchanged.

Rules the preflight enforces: every numeric field CET requires not blank (`NumUnits`, the kWh / kW / therm savings of both baselines, the measure costs, the four `NTGR*`, `EUL_Yrs`, `RUL_Yrs`, the installation and realization rates, and the gas-infrastructure, refrigerant and miscellaneous benefits and costs; enter 0 for none); `EUL_Yrs` and `RUL_Yrs` not negative and `RUL_Yrs` not above `EUL_Yrs` (`RUL_Yrs` equal to `EUL_Yrs` is a warning); values outside the ranges CET's own QC allows are warned about (see INPUT_FIELDS.md); `CEInputID` unique; `ClaimYearQuarter` like `2028Q1`; `IOU_AC_Territory` one of `PGE SCE SDGE SCG`; numeric columns numeric or blank;
`AC_Gas_Target_Sector` one of `Residential Commercial Core` for measures with gas savings; every `PrgID` in `Measure.csv` present in `ProgramCost.csv` and vice versa.

### Things about the CET format that need attention (found on the real 2028-2031 PG&E files)

1. **Run settings are not in the files.** The ACC version, *First Year*, and *Market Effects* are chosen on the CET run form, so here they are options (GUI boxes / flags).
   First Year defaults to the earliest claim year in the job.
2. **The PA comes from `IOU_AC_Territory`** (the avoided-cost territory), not from a PA column. Statewide programs are priced on the measure's own territory; their
   *program costs* carry a PA in `ProgramCost.csv`. Rows with `PA = SW` are given `PA = SCE` (what CET run 121219 did) unless *Allocate statewide programs* is on.
3. **Profile names must match exactly.** `AC_Elec_Impact_Profile` must equal a library profile's end-use name (or a supplied profile's `end_use_name`), else the measure
   is left **unpriced** (`ElecPricingStatus = UNPRICED`). `AC_Gas_Impact_Profile` likewise; 36 measures in the 2028 job have therms but a blank profile, and CET prices those at $0 too.
4. **TOU (H-factor) end uses are priced from 8,760s.** CEDARS has no hourly data for them; the library holds Gen-based 8,760s built from their H-factors on the ACC's
   2018-calendar TOU map (2026 ACC v1b, re-issued 2026-10-20), for every PG&E, SCE, SCG and SDG&E TOU profile and zone, and they are priced like any other profile. A supplied 8,760 with the same name
   replaces the library's.
5. **A brand-new ACC has no run parameters or CET tables of its own.** Discount rates, emission factors and rate schedules are picked by ACC version (C3.3); a new ACC label borrows the tables of an
   ACC version you choose (default: the newest built-in).
6. **Years covered:** 2021 ACC 2020-2050, 2022 ACC 2022-2052, 2024 ACC 2024-2054, 2026 ACC 2026-2055 (gas: 2020-2050, 2022-2052, 2024-2054, 2026-2055). The 2021 and
   2022 ACCs are TRC only (C3.4) and use the 2020 hourly calendar (C4.2). Years outside an ACC's range are priced with its nearest year; the preflight counts the measures affected.
7. **SCG electric** has no ACC of its own. In each zone SCG is priced entirely by one utility: SCE where the ACC has SCE data for the zone, otherwise PG&E (on the
   built-in ACCs, PG&E in CZ 1, 2, 3A, 3B, 4, 5, 11, 12). Generation, T&D and emission rate always come from the same utility. CET's quarterly SCG table is not used.
8. **Bill impacts / RIM** use CET's rate schedules chosen by (PA, `AC_Elec_Target_Sector`); the 2026 ACC uses the same rate tables as the 2024 ACC.
9. **Market Effects** is one option: 5% on benefits (`NTGR + 0.05`) and on costs (CET's `MECost`) together. Checked against CET runs made with 0% and 5%
   (jobs 121842 / 121841, CALCULATIONS C13).

---------------------------------------------------------------------------------------------------------------------------------

## 2. Separate 8,760-hour electric profiles

CSV or Parquet, **long format**, one row per (profile, climate zone, hour):

| column | meaning |
|---|---|
| `end_use_name` | must equal the measure's `AC_Elec_Impact_Profile` (case-insensitive) |
| `pa` | `PGE` `SCE` `SDGE` `SCG`; blank / `*` / `ALL` = any IOU |
| `target_sector` | the measure's `AC_Elec_Target_Sector`; blank / `*` / `ALL` = any sector |
| `climate_zone` | `1`..`16`, `3A`, `3B`, `CZ13` and `01` are accepted; `System` or `ALL` = every zone without its own rows |
| `hour_of_year` | 1..8760, exactly once per (profile, zone) |
| `impact` | the share of the measure's **annual** net savings in that hour; each zone's 8,760 values should sum to 1 (see below) |
| `represents_year` | *(optional, same on every row of a profile)* the calendar year whose weekdays the shape follows; default 2009, the CEDARS default |
| `profile_basis` | *(optional, same on every row of a profile)* `share` (the default: `impact` is a share of annual savings) or `kwh_per_unit` (`impact` is already scaled: the kWh saved per unit in each hour, negative for added load) |

* **Matching:** a measure is matched on (end use, `pa`, `target_sector`); the most specific entry wins (both given > one > neither). A match is used **instead of** the
  library profile.
* **Zones:** a measure's zone is looked up in the profile, then its `System`/`ALL` rows; if neither exists the library profile is used for that measure
  (`ProfileSource = library (user profile has no shape for this zone)`). A measure whose own zone is `System` is priced in the profile's System zone, the zone with the
  lowest total avoided cost over the ACC (CALCULATIONS C4.4); a file's own `System` rows are not used for it.
* **Sum:** within 0.1% of 1 the values are used as given; otherwise they are rescaled to sum to 1 and the run says so (`--no-normalize-profiles` makes it an error).
  A share profile that sums to ~0 cannot be rescaled and is rejected: give it as `kwh_per_unit` instead.
* **Already-scaled profiles** (`profile_basis = kwh_per_unit`): the values are priced exactly as given, per unit, times `NumUnits`, the kWh net-to-gross ratio and the
  installation and realization rates. The measure's annual kWh is the profile's sum, which replaces `UnitkWh1stBaseline` (a 2nd baseline keeps the file's
  `UnitkWh2ndBaseline / UnitkWh1stBaseline` ratio). The sum may be 0: a **load-shifting-only** measure (savings in some hours, the same kWh added in others) is valued in
  one record (CALCULATIONS C4.5). Water energy on such a measure is spread by the profile's shares, so a zero-net profile gives it no value (the preflight warns).
* **Calendar:** like the library, the shape is rotated once from `represents_year`'s weekdays onto the ACC's (2018 or 2020), the short way round (at most 3 days).
* **Start from an existing profile:** `recet template export-profile --end-use "..." --pa PGE --sector Residential --out shape.csv` writes the library profile for editing.

Every measure's output row says where its shape came from in `ProfileSource` (`user profile`, `user profile (kWh per unit)`, `library`, `library (sector-loose match)`,
`library (user profile has no shape for this zone)`).

---------------------------------------------------------------------------------------------------------------------------------

## 3. Separate gas monthly profiles

The same three columns as the built-in library, CSV or Parquet, 12 rows per profile:

| column | meaning |
|---|---|
| `Profile` | must equal the measure's `AC_Gas_Impact_Profile` (e.g. `Annual`, `Winter Only`) |
| `Month` | 1..12 (or Jan..Dec) |
| `Impact` | share of annual therms in that month; sums to 1 (rescaled if not, with a note) |

A `Profile` name in the built-in library is replaced; a new name is added. Gas is monthly, so no calendar alignment is applied.

---------------------------------------------------------------------------------------------------------------------------------

## 4. Separate electric ACC

Parquet (recommended: the built-in files have ~17 million rows) or CSV, **one row per (iou, climate_zone, cost_test, year, hour_of_year)**, $/MWh:

| column | required | meaning |
|---|---|---|
| `iou` | yes | `PG&E` `SCE` `SDG&E` |
| `climate_zone` | yes | `CZ1`..`CZ16` (the zones the IOU serves) |
| `cost_test` | yes | `TRC`, `SCT Base`, `SCT High` (the SCT rows are needed only for SCB/SCH) |
| `year` | yes | consecutive years |
| `hour_of_year` | yes | 1..8760 for every block |
| `total_avoided_cost` | yes | the sum of the component columns |
| `transmission`, `distribution` | for water energy | the T&D part of the total (water is priced on the rest) |
| `emission_rate_tonnes_per_kwh` | for CO2 | grid emission rate |
| component columns | for "components" mode | `energy generation_capacity avoided_as_procurement losses methane_leakage ghg_adder air_quality_adder` plus `ghg_cap_and_trade ghg_portfolio_rebalancing` (2024 ACC) or `ghg_cap_and_invest` (2026 ACC); their sum should equal `total_avoided_cost` |
| `price_zone`, `datetime`, `month`, `day`, `hour` | no | ignored |

**Which version it replaces:** you choose the ACC version label in the GUI (or `--acc`). If the label is one the package ships (`2021`, `2022`, `2024`, `2026`) the supplied file replaces that
version's electric ACC; a new label defines a new version made of the supplied files (an unsupplied fuel falls back to the newest built-in, with a note).

The file is checked before the run: required columns, 8,760 rows per block, consecutive years, all three IOUs, `TRC` present, and the sum-of-components test.

---------------------------------------------------------------------------------------------------------------------------------

## 5. Separate gas ACC

CSV or Parquet, one row per (utility, class, cost_test, scc, year); $/therm:

| column | meaning |
|---|---|
| `utility` | `PG&E`, `Socal Gas` (or `SoCalGas`), `SDG&E` |
| `class` | `Residential`, `Commercial`, `Total Core` |
| `cost_test` | `TRC` or `SCT` |
| `scc` | blank for `TRC`; `Base` or `High` for `SCT` |
| `year` | consecutive years |
| `market_jan` .. `market_dec` | commodity, by month |
| `td_jan` .. `td_dec` | transportation/distribution, by month |
| `nox`, `co2`, `upstream_methane`, `behind_meter_methane`, `air_quality_adder` | annual $/therm |

The gas price is the sum of these seven components (behind-the-meter methane only for residential fuel-substitution profiles). Years outside the file's range use its nearest year.

---------------------------------------------------------------------------------------------------------------------------------

## 6. Run parameters

The discount rates, base year and gas CO2 factor used for each PA. The built-in values are the RunParameters table (a copy of CET's settings) for the run's version; a supplied file **replaces the
row of every PA it lists**, and the others keep their built-in values. Start from `recet template run-parameters` (the GUI's **Template...** button), which holds the
built-in values, and change only what you need. Columns are defined in INPUT_FIELDS.md section 4.5.

| Column | Example (PG&E, built-in) |
|---|---|
| `PA` | `PGE` |
| `DiscountRateAnnual` | `0.0766` (a fraction, not 7.66) |
| `DiscountRateQtr` | `0.01915` (optional: blank = annual / 4) |
| `DiscountRateAnnual_SCT` | `0.0503` |
| `DiscountRateQtr_SCT` | `0.012575` (optional) |
| `BaseYear` | `2024` (optional: blank = built-in) |
| `CO2Gas` | `0.00531` (metric tons CO2 per therm) |

## 7. Statewide (SW) allocation factors

Used only when **Allocate statewide programs** is on. The template is the whole built-in table (304 rows, 35 programs, 2021-2031). A supplied row whose (`Year`,
`ProgramID`) is in the built-in table replaces it; any other row is added. Each IOU's `ElectricFactor`s and `GasFactor`s should sum to 1 (or all be 0 when the program has
no gas or no electricity); the log notes rows that do not. The weighted factors can be given, or computed from `PercentElectric`. Columns: INPUT_FIELDS.md section 4.6.

## 7a. Program metadata

Optional. The built-in table is CEDARS' Programs list (download of 2026-10-03, 1,416 programs, without the `ProgramManager` column); the template is that whole table. A
supplied row whose `PrgID` is in it replaces the row; any other row is added. Only `PA` and `PrgID` are required. Each measure gets its program's row as `Prg_` columns
(matched on the `PrgID` as priced, so with **Allocate statewide programs** each IOU's share gets its own `<IOU>_SW_...` row), the summary gains one row per
`ProgramSegment`, the preflight reports unknown programs, claims outside the program's years, closed programs and PA differences, and **Leave Exclude_From_CE programs out of the
portfolio summary** uses the `Exclude_From_CE` flag. No priced value changes. Columns: INPUT_FIELDS.md section 4.8.

## 8. The job as one Excel workbook

Instead of the separate files, a job can be one `.xlsx` (GUI: **Job workbook**; command line: `--workbook`). Start from `recet template workbook`:

| Sheet | Contents |
|---|---|
| `Instructions` | how to fill it in (ignored by the run) |
| `Measure` | **required**: the `Measure.csv` columns, with one yellow example row to replace |
| `ProgramCost` | the `ProgramCost.csv` columns, with one yellow example row to replace or delete |
| `RunParameters` | filled in with the built-in values for the chosen ACC; edit to change them |
| `SWAllocation` | filled in with the built-in factor table |
| `Programs` | filled in with the built-in program metadata |
| `GasProfiles` | header only (`Profile`, `Month`, `Impact`) |
| `Profiles8760` | header only (the 8,760 long format; Excel fits about 119 profile x zone blocks, so use a separate file for more) |

The run writes each sheet that has data as its own file in the run folder (`_prepared_inputs/workbook/`) and runs on those, so a workbook job and the same job as files
give the same results. A file chosen separately (for example a run-parameters CSV) wins over the sheet, and the log says so. Whole numbers are read without decimals
(a climate zone typed as `13` stays `13`). The ACC files are too large for Excel and stay separate files.

## 9. What a run writes

One folder `<out>/<run name>_acc<version>/`:

* `measure_level_acc<version>.csv` - one row per measure: the input columns, the derived pricing inputs (`Used_Year`, `AC_Net_kWh_1st_Baseline` ...), `ElecPricingStatus` / `ProfileSource` / `GasPricingStatus` /
  `GasProfileSource`, then every output (benefits, supply costs, participant and bill-impact PVs, Gross / NoAdmin variants, TRC / PAC / RIM / SCB / SCH costs and ratios,
  program-cost weighting, CO2 / NOx / PM10), `ElecValue_*` / `GasValue_*` in components mode, `CreditedPA` (the `PA` of the measure's program in `ProgramCost.csv`; with statewide
  allocation, the IOU each statewide share went to), and the program metadata (`Prg_MatchedOn`, `Prg_<column>`).
* `portfolio_summary_*.csv`, `field_definitions_*.csv` (each column of the measure-level file with its definition), `key_metrics_*.csv` (the headline results and their
  main drivers), `preflight.txt`, `preflight_findings.xlsx` (the input check's findings and every flagged cell), `run_settings.json` (everything needed to reproduce the run).
* With CEDARS filing checks: `filing_validation_*.csv` (each record CEDARS would reject or warn on).
* With statewide allocation: `statewide_reconciliation_*.csv` (each allocated field of each statewide program, before allocation and by IOU, and whether it reconciles).
* Optional: `components_summary_*.csv`, `portfolio_8760_*.csv` (by PA and climate zone; + `_peaks`, each one's peak hours, and `_unshaped`), `cet_comparison_*.csv`, and for a two-version run `comparison_acc<A>_vs_acc<B>.csv`.

**Water energy** is priced like the electric benefit but on the `Gen` term alone (no T&D, no kW term) with the measure's own electric profile: `WaterEnergyBen` when
`UnitkWhIOUWater1stBaseline > 0`, `WaterEnergyCost` when it is `< 0` (plus `_SB`/`_SH` and `Gross` variants). Both go into TSB; the cost goes into TRC/PAC/RIM/SCB/SCH costs, the
benefit into every ratio and into the program-cost weighting. `WaterPricingStatus` says `no water energy`, `priced`, or `UNPRICED` (the measure has no electric profile).

`ElecPricingStatus` is `no electric savings`, `priced: hourly profile`, `PARTLY PRICED: ...` (a separate 2nd-baseline shape of which only one of the two shapes was found)
or `UNPRICED: ...`: a benefit of 0 alone cannot tell "no
savings" from "could not price".
