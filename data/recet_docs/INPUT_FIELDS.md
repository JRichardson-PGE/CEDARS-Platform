# Input field reference

Every input `recet` reads, field by field: what it means, its unit, whether the column must be present, and where it is used (the `C..` numbers are sections of
[CALCULATIONS.md](CALCULATIONS.md)). Formats of the *supplied* files, matching rules and templates are in [INPUT_FORMATS.md](INPUT_FORMATS.md); what each GUI control does is in
[USER_GUIDE.md](USER_GUIDE.md); the outputs are in [OUTPUT_FIELDS.md](OUTPUT_FIELDS.md).

* [1. `Measure.csv`](#1-measurecsv) - the job's measures (CET format)
* [2. `ProgramCost.csv`](#2-programcostcsv) - program-level costs (CET format)
* [3. Run settings](#3-run-settings) - options chosen in the GUI, on the command line or in a saved settings file
* [4. Supplied input files](#4-supplied-input-files) - 8,760 profiles, gas monthly profiles, electric ACC, gas ACC
* [5. Reference data files](#5-reference-data-files-the-large-files) - the large files kept outside the package
* [6. Built-in tables](#6-built-in-tables-shipped-inside-the-package) - small tables shipped inside the package
* [7. CET outputs file](#7-cet-outputs-file-for-comparison) - the optional file to compare against

**The minimal input set (BRD BR-06.15).** For an exploratory run only the columns that change a result are needed: the *required* columns below except
`MeasDescription` and `Sector`, which may be left out (they are then read as blank, with a `missing_descriptive_column` warning; CET's own upload still needs
them). *Carried* and *optional* columns may always be left out. Values are annual, per unit and gross, with the net-to-gross ratios given separately, as CET
takes them: ReCET does not take lifecycle savings to derive annual values from.

**Reading the tables.** *Required* = the column must exist in the file (a missing one stops the run and the preflight names it; a *blank cell* in a numeric column is read as the default stated in "Blank means").
*Carried* = read if present and copied to the output unchanged, never used in a calculation. Numeric columns must hold numbers or be blank; text in them is a blocking preflight error.
Every numeric field CET marks NotEmpty cannot be blank, as in CET: `NumUnits`, the six `UnitkW` / `UnitkWh` / `UnitTherm` 1st/2nd-baseline fields, `UnitMeaCost1stBaseline`, `UnitMeaCost2ndBaseline`, the four `NTGR*`, `EUL_Yrs`, `RUL_Yrs`, the six installation and realization rates, `UnitGasInfraBens`, `UnitRefrigCosts`, `UnitRefrigBens`, `UnitMiscCosts` and `UnitMiscBens`; a blank is a blocking preflight error (enter 0 for none), and the pricing uses them as filed. `EUL_Yrs` and `RUL_Yrs` may not be negative, and `RUL_Yrs` may not exceed `EUL_Yrs` (blocking errors).
The preflight also **warns** (the run still proceeds) when a value is outside the range CET's own QC allows: the four `NTGR*` in (0, 1.5], `EUL_Yrs` in [0, 40], `RUL_Yrs` in [0, 30], the three `RealizationRate*` in (0, 2],
the three `InstallationRate*` in (0, 1], `MeasInflation` in [0, 1], the two `UnitMeaCost*` at least 0, and an `RUL_Yrs` equal to `EUL_Yrs` (`rul_not_below_eul`).

---------------------------------------------------------------------------------------------------------------------------------

## 1. `Measure.csv`

The same file you upload to the CET: pipe (`|`) delimited (or comma-delimited, see [INPUT_FORMATS.md](INPUT_FORMATS.md)), one header row, UTF-8, one row per `CEInputID`. All 74 CET columns are echoed to the measure-level output in the input's own text form.

**Field names: CET's legacy names are accepted.** Seven fields carry names from the E3 calculator the CET grew out of; ReCET names them for what they are.
A file may use **either name** for each of them (in `Measure.csv`, a comma-delimited file or the job workbook's Measure sheet; the legacy name is matched
without regard to case, as CET does). It is read under ReCET's name, and every output, template and saved workbook uses ReCET's name. A file that has both
names of the same field is rejected, since which one to use would be a guess. Files the Edit inputs tab saves for the CET (*Save as CET files*) use CET's names,
so they can be uploaded to the CET as they are.

| ReCET name | CET's name (also accepted) | What it is |
|---|---|---|
| `AC_Climate_Zone` | `E3ClimateZone` | the climate zone whose avoided costs and profile shape price the measure |
| `AC_Elec_Impact_Profile` | `E3MeaElecEndUseShape` | the electric impact profile (8,760 end-use shape) |
| `AC_Elec_Impact_Profile_2nd_Baseline` | `E3MeaElecEndUseShape2nd` | the electric impact profile of the 2nd-baseline years (ReCET only; not a CET field) |
| `AC_Elec_Impact_Profile_Water` | `E3MeaElecEndUseShapeWater` | the electric impact profile of the water energy (ReCET only; not a CET field) |
| `AC_Elec_Target_Sector` | `E3TargetSector` | the electric target sector (profile match and retail rate) |
| `AC_Gas_Impact_Profile` | `E3GasSavProfile` | the gas monthly impact profile |
| `AC_Gas_Target_Sector` | `E3GasSector` | the gas target sector (gas ACC class and retail rate) |

### 1.1 Identification and program link

| Field | Type / unit | Column | Description and use |
|---|---|---|---|
| `CEInputID` | text, unique | required | The measure's unique, persistent ID. The key of every output row and the join key to a CET outputs file. Blank or duplicate values stop the run. |
| `ACCVersion` | text | optional, **not a CET column** | The ACC version this measure is priced on. Accepts values such as 2024, 2026, as well as values like "2022 ACC", "2024 ACC". Blank, or a file without the column, means the run's "ACC version" setting is used. Measures on different versions are priced in one run, each on its own ACC's avoided costs, run parameters and E3 tables; program costs are then shared over all of them and discounted at the run's version's rates. A supplied ACC file replaces only the run's own version and is not saved for future use. |
| `PrgID` | text | required | The program the measure belongs to; must equal a `PrgID` in `ProgramCost.csv` (and vice versa). Groups measures for the program-cost share and, through the program's PA in `ProgramCost.csv`, gives `CreditedPA`. |
| `MeasDescription` | text | required (may be left out: read as blank) | Description of measure; should include enough information to allow understanding of what EE measure(s) are being installed. |
| `MeasureID` | text | carried | The measure's unique ID in the source workpaper. |
| `MeasCode` | text | carried | PA-specific measure code. |
| `ClaimYearQuarter` | `YYYYQn`, e.g. `2028Q3` | required | The year and quarter when the measure(s) were both installed and paid in e.g. '2023Q1'. Its year and quarter set when savings start and how they are discounted. Must match the pattern or the run stops. |

### 1.2 Classification (carried, no effect on any result)

| Field | Type | Column | Description |
|---|---|---|---|
| `Sector` | text | required (may be left out: read as blank) | Measure-level sector aligned to the program business plan sectors. |
| `DeliveryType` | text | carried | From DEER. Identifies program implementation strategy/method of delivering a measure to a customer (e.g. direct install). |
| `BldgType` | text | carried | From DEER. Text codes that identify the building type/use and other parameters specific to that building use. For example, 'Com' = Commercial buildings. |
| `MeasAppType` | text | carried | From DEER. The measure application identifies the context for the measure's installation. (e.g. accelerated replacement, new construction or behavioral). |
| `TechGroup` | text | carried | From DEER. All Technology Types in an end use are associated with a high-level Technology Group. For example, in the 'lighting' end use all screw-in lighting technologies, including compact fluorescent lamps (CFLs), light-emitting diodes (LEDs), and incandescent lamps are part of a Technology Group. All hard-wired lighting fixtures are part of a different Technology Group. |
| `TechType` | text | carried | From DEER. Technology Type is more specific than Technology Group. For example, a Technology Group can be further classified into individual parts. For example, all screw-in lighting technologies, including compact fluorescent lamps (CFLs), light-emitting diodes (LEDs), and incandescent lamps are included in the same Technology Group. |
| `UseCategory` | text | carried | From DEER. Describes the end use of the measure in broad terms (e.g. HVAC, lighting, motors, refrigeration, process). |
| `UseSubCategory` | text | carried | From DEER. This is a more specific end use subcategory (e.g. the Lighting `UseCategory` contains Indoor General, Exit Signs, etc.). |
| `WaterUse` | text | carried | Water Energy Calculator water used to calculate water energy savings. |
| `PreDesc` | text | carried | Pre-existing measure description. Should include enough information to allow understanding of what the pre-existing measure(s) were. |
| `StdDesc` | text | carried | Standard practice or code measure description. Should include enough information to allow understanding of what the standard measure(s) are. |
| `SourceDesc` | text | carried | The measure package ID for deemed measures. |
| `Version` | text | carried | The version of the work paper or DEER (e.g. 2013 Ex-Ante). |
| `NormUnit` | text | carried | Type of normalizing units, or basis of `NumUnits` or quantity used by the cost effectiveness calculation (e.g. 'Lamp', 'hp', 'tons', 'facility', etc.). |
| `NTG_ID` | text | carried | From DEER. The Ex Ante `NTG_ID` associated with the NTG values being claimed. |
| `EUL_ID` | text | carried | From DEER. Identifier from the EUL table which specifies the estimated useful life of the measure technology. |
| `RUL_ID` | text | carried | From DEER. For dual baseline measures, specifies a row in the EUL table that identifies the remaining useful life of the existing technology. |
| `GSIA_ID` | text | carried | Reference to the Ex Ante Gross Savings and Installation Adjustment table. |
| `Residential_Flag` | 0/1 | carried | The measure was installed in a residential setting. |
| `Upstream_Flag` | 0/1 | carried | The measure was delivered via an upstream delivery type, i.e. targeted the beginning of the supply chain (e.g. the manufacturer). |
| `MiscCostsDesc` | text | carried | A description of the miscellaneous costs reported in the `UnitMiscCosts` field. |
| `MiscBensDesc` | text | carried | A description of the miscellaneous benefits reported in the `UnitMiscBens` field. |
| `Comments` | text | carried | Text field for clarifying comments. |

### 1.3 Keys that select prices, profiles and rates

| Field | Type | Column | Description and use |
|---|---|---|---|
| `IOU_AC_Territory` | `PGE` `SCE` `SDGE` `SCG` | required | The utility avoided costs being used (PGE, SCE, SCG, SDGE). This field was previously called PA. **In ReCET:** Selects the ACC utility (C3.1, C3.2), the discount rate (C2.4), the retail rates (C9) and the profile match; any other value stops the run. |
| `AC_Climate_Zone` | text, e.g. `13`, `3A` | required | The geographic area where the installation is completed. The CEC designates micro-climate and ranges from 1 - 16. Use 'System' if a program or measure installation is not specific to an area or city. Required for cost effectiveness calc; Must be compatible with defined values (e.g. '3A'). Previously called E3ClimateZone. |
| `AC_Elec_Impact_Profile` | text | required | Identifies the measure electric impact profile shape. Required for cost effectiveness calculation for measures with electric savings. Previously called E3MeaElecEndUseShape. **In ReCET:** Must equal a supplied or library profile's `end_use_name` (the library includes the TOU end uses as 8,760s), or the measure's electricity is **not priced** (C4.1). |
| `AC_Elec_Impact_Profile_2nd_Baseline` | text | optional, **not a CET column** | Identifies the measure electric impact profile shape for the 2nd baseline. Optionally used for cost effectiveness calculation for measures with electric savings. If left blank, `AC_Elec_Impact_Profile` will be used. **In ReCET:** Not a CET column. The 2nd-baseline years of an early replacement (`0 < RUL_Yrs < EUL_Yrs`) are priced on this shape (C4.6). |
| `AC_Elec_Impact_Profile_Water` | text | optional, **not a CET column** | Identifies the measure electric impact profile shape for the water energy savings. Optionally used for cost effectiveness calculation for measures with electric savings. If left blank, `AC_Elec_Impact_Profile` will be used. **In ReCET:** Not a CET column. The water energy (`UnitkWhIOUWater*`) is spread over this shape for both baselines, matched like `AC_Elec_Impact_Profile` (C4.6). |
| `AC_Elec_Impact_Profile_Parent` | text | optional, **not a CET column** | A NMEC true-up's parent profile: the electric impact profile of the claim the record adjusts (BRD BR-05.6). Blank = the record has no parent. **In ReCET:** Not a CET column. The record is also priced on this profile, with its own ACC, timing and quantities, and the `...Incremental` columns are its value as filed less that value (C21); nothing else changes. Matched like `AC_Elec_Impact_Profile`. |
| `AC_Elec_Impact_Profile_Parent_2nd_Baseline` | text | optional, **not a CET column** | The parent's profile for the 2nd-baseline years of an early replacement. If left blank, `AC_Elec_Impact_Profile_Parent` will be used. **In ReCET:** Not a CET column. Used only when `AC_Elec_Impact_Profile_Parent` is filled in (C21). |
| `AC_Elec_Target_Sector` | text | required | Identifies the electric sector being targeted by the measure intervention. Required for cost effectiveness calculation for measures with electric savings. Previously called E3TargetSector. **In ReCET:** Part of the profile match; selects the retail rate schedule (C9) and the NOx/PM10 factors (C14). |
| `AC_Gas_Impact_Profile` | text | required | Used to profile typical period of use for gas appliances/measures. Required for cost effectiveness calculation for measures with gas savings (e.g. 'Summer Only', 'Winter Only', 'Annual'). Previously called E3GasSavProfile. **In ReCET:** A name that matches nothing leaves gas savings **unpriced** (C4.3); a `...FuelSub...` name on a residential measure adds behind-the-meter methane (C3.2). |
| `AC_Gas_Target_Sector` | `Residential` `Commercial` `Core` | required | Used to determine how gas is purchased by the customer (e.g. 'Residential', 'Commercial', 'Core'). Required for cost effectiveness calculation for measures with gas savings. Previously called E3GasSector. **In ReCET:** Selects the gas ACC class (C3.2) and the gas rate schedule (C9); must be one of the three for any measure with gas savings. |
| `FuelID` | text | optional | This field references a CEDARS value list used to identify fuel substitution for Deemed measures. FuelID distinguishes exempt and non-exempt Deemed measure packages under the Viable Electric Alternative (VEA) framework established in D.23-04-035. **In ReCET:** `AllElec-New`, `FuelSub-ToElec` and `FuelSub-ToGas` mark the measure as fuel substitution (`Fuel_Sub_Flag`, C1.5). |
| `MeasImpactType` | text | optional | From DEER. Describes how the savings impact was determined (e.g. Deemed-DEER, Deemed-Workpaper, Site-specific calculation, etc.). **In ReCET:** A value containing `FuelSub` also marks fuel substitution (C1.5). |
| `CombustionType` | text | optional | Field that determines the rate of emission savings per MMBTU of reduced natural gas usage from gas conservation. Res Furnace is used as a default if there is no user selection. If not applicable or unknown, then leave the field blank. **In ReCET:** A `LookupCode` of the combustion table (e.g. `Residential Furnaces (<0.3):Uncontrolled`) gives the gas NOx rate (C14.3). There is no default: blank or unknown means gas NOx = 0. |
| `RateScheduleElec` | text | carried | Field to specify measure-level electric rate schedule, overrides default. Used to differentiate between customer types and system average rates. **In ReCET:** **Not used**: the schedule follows `IOU_AC_Territory` and `AC_Elec_Target_Sector` (C9). |
| `RateScheduleGas` | text | carried | Field to specify measure-level gas rate schedule, overrides default. Used to differentiate between customer types and system average rates. **In ReCET:** **Not used**: the schedule follows `IOU_AC_Territory` and `AC_Gas_Target_Sector` (C9). |

### 1.4 Quantities

| Field | Type / unit | Column | Blank means | Description and use |
|---|---|---|---|---|
| `NumUnits` | Decimal number (units of `NormUnit`) | required | error (enter 0 for none) | Number of units or quantity. `NumUnits` must work in conjunction with the `NormUnit` value for the claim. Cross reference `NormUnit` and `NumUnits` before interpreting quantities of measures installed. |
| `UnitkWh1stBaseline` | kWh (per unit per year) | required | error (enter 0 for none) | The first baseline kWh Impact per Unit. |
| `UnitkWh2ndBaseline` | kWh (per unit per year) | required | error (enter 0 for none) | The second baseline kWh Impact per Unit. Only used for accelerated replacement situations. |
| `UnitkW1stBaseline` | kW (per unit) | required | error (enter 0 for none) | The first baseline kW Impact per Unit. **In ReCET:** Reported in the kW savings columns (`FirstYearNetKW` ...) only: pricing is on kWh through the 8,760 shape, which carries the T&D value. |
| `UnitkW2ndBaseline` | kW (per unit) | required | error (enter 0 for none) | The second baseline kW Impact per Unit. Only used for accelerated replacement situations. **In ReCET:** Reported in the kW savings columns only, as `UnitkW1stBaseline`. |
| `UnitTherm1stBaseline` | Therms (per unit per year) | required | error (enter 0 for none) | The first baseline therm Impact per Unit. |
| `UnitTherm2ndBaseline` | Therms (per unit per year) | required | error (enter 0 for none) | The second baseline therm Impact per Unit. Only used for accelerated replacement situations. |
| `UnitkWhIOUWater1stBaseline` | kWh (per unit per year) | optional | 0 | The first baseline kWh impact per unit due to water savings. |
| `UnitkWhIOUWater2ndBaseline` | kWh (per unit per year) | optional | 0 | The second baseline kWh impact per unit due to water savings. Only used for accelerated replacement situations. |

### 1.5 Useful life

| Field | Type / unit | Column | Blank means | Description and use |
|---|---|---|---|---|
| `EUL_Yrs` | Years (fractional allowed) | required | error | Effective useful life of measure in years. **In ReCET:** Savings run from the claim quarter in whole quarters, the last one prorated (C2.1); the value is used as filed (0 gives no savings quarters). |
| `RUL_Yrs` | Years | required | error | For dual baseline measures, specifies the remaining useful life of pre-existing measure in years. **In ReCET:** 0 means a single baseline for the whole EUL; a positive value below `EUL_Yrs` makes the measure an early replacement: 1st-baseline savings until the RUL, 2nd-baseline after (C2.3). A value equal to `EUL_Yrs` is treated as 0, as CET does (a warning); above it is an error. |

### 1.6 Net-to-gross, realization and installation rates

| Field | Type | Column | Blank means | Description and use |
|---|---|---|---|---|
| `NTGRkWh` | Decimal number | required | error | kWh-specific NTG value. Usually, there is one NTG value entered for all NTGR fields. Developed through evaluation studies. NTG describes the free-ridership that may be occurring within energy efficiency programs. |
| `NTGRkW` | Decimal number | required | error | kW-specific NTG value. Usually, there is one NTG value entered for all NTGR fields. Developed through evaluation studies. NTG describes the free-ridership that may be occurring within energy efficiency programs. **In ReCET:** Used only in the kW savings columns. |
| `NTGRTherm` | Decimal number | required | error | Therm-specific NTG value. Usually, there is one NTG value entered for all NTGR fields. Developed through evaluation studies. NTG describes the free-ridership that may be occurring within energy efficiency programs. |
| `NTGRCost` | Decimal number | required | error | Cost-specific NTG value. Usually, there is one NTG value entered for all NTGR fields. Developed through evaluation studies. NTG describes the free-ridership that may be occurring within energy efficiency programs. |
| `RealizationRatekWh` | Decimal number | required | error | Ex ante adjustment factor: kWh weighted Realization rate for all measures included in this claim record. |
| `RealizationRatekW` | Decimal number | required | error | Ex ante adjustment factor: kW weighted Realization rate for all measures included in this claim record. |
| `RealizationRateTherm` | Decimal number | required | error | Ex ante adjustment factor: Therm weighted Realization rate for all measures included in this claim record. |
| `InstallationRatekWh` | Decimal number | required | error | Ex ante adjustment factor, generally developed by evaluation studies of prior year programs. kWh weighted installation rate for all measures included in the associated claim. |
| `InstallationRatekW` | Decimal number | required | error | Ex ante adjustment factor, generally developed by evaluation studies of prior year programs. kW weighted installation rate for all measures included in the associated claim. |
| `InstallationRateTherm` | Decimal number | required | error | Ex ante adjustment factor, generally developed by evaluation studies of prior year programs. Therm weighted installation rate for all measures included in the associated claim. |

### 1.7 Costs, incentives and inflation

All are dollars **per unit** (multiplied by `NumUnits`). `UnitMeaCost1stBaseline` and `UnitMeaCost2ndBaseline` are required (blank is an error); the others read blank as 0.

| Field | Unit | Column | Description and use |
|---|---|---|---|
| `UnitMeaCost1stBaseline` | Dollars (per unit) | required | This field is the installed measure cost. Incremental Measure Cost or Full Measure Cost for the first baseline period, as appropriate for the `MeasAppType` and `DeliveryType`. Incremental measure cost over Code/Std for single baseline measures. Full measure cost for accelerated replacement situations. **In ReCET:** Also used for the excess-incentive test (C8). |
| `UnitMeaCost2ndBaseline` | Dollars (per unit) | required | This field is the incremental measure cost over Code/Std for accelerated replacement situations, and is zero in other situations. **In ReCET:** The measure cost the cost tests count for an early replacement (`RUL_Yrs > 0`); ignored when `RUL_Yrs = 0` (C8). |
| `UnitDirectInstallLab` | Dollars (per unit) | required | This field is the actual cost in dollars of installation labor. Enter the unit incentive for labor. Cost effectiveness calculator uses this field; and overrides ExAnte values for deemed claims, and is one of two incentive fields for Direct Install. Only use if a direct install. |
| `UnitDirectInstallMat` | Dollars (per unit) | required | This field is the actual cost in dollars of installation materials. Enter the unit incentive for materials. Cost effectiveness calculator uses this field; and overrides ExAnte values for deemed claims, and is one of two incentive fields for Direct Install. Only use if direct install. |
| `UnitIncentiveToOthers` | Dollars (per unit) | required | This field is the actual cost in dollars of incentives to others besides the end user. Enter the incentive for others for each unit of measure. Cost effectiveness calculator uses this field; and overrides ExAnte values for deemed claims. Only use if midstream or upstream delivery type. Input should always be '0' for custom measures. |
| `UnitEndUserRebate` | Dollars (per unit) | required | This field is the actual cost of rebates to the end user in dollars. Enter rebate to the customer for each unit of the measure. Cost effectiveness calculator uses this field, and overrides ExAnte values for deemed claims. Only use if downstream delivery type. |
| `MeasInflation` | Decimal number (per year, e.g. 0.02) | required | CET field to specify measure-level inflation rate, overrides CET default. **In ReCET:** Used only for early-replacement measures (`RUL_Yrs > 0`), in `GrossMeasCostPV` (C8); blank = 0. |

### 1.8 Other benefits, other costs, tax credits

Dollars per unit, undiscounted (C7). All but `UnitTaxCredits` are required (blank is an error); a blank `UnitTaxCredits` is 0.

| Field | Unit | Column | Description and use |
|---|---|---|---|
| `UnitGasInfraBens` | Dollars (per unit) | required | The unit benefits due to gas infrastructure savings. |
| `UnitRefrigBens` | Dollars (per unit) | required | The unit benefits due to refrigeration savings. |
| `UnitMiscBens` | Dollars (per unit) | required | The unit benefits not captured in other benefit fields. |
| `UnitRefrigCosts` | Dollars (per unit) | required | The unit cost in dollars for refrigeration. |
| `UnitMiscCosts` | Dollars (per unit) | required | The unit cost in dollars that is not captured in other cost fields. |
| `UnitTaxCredits` | Dollars (per unit) | required | Inflation Reduction Act tax incentives for energy, per unit. The CET sums `UnitTaxCredits` with other benefits in the TRC calculation where IRA-eligible measures apply. **In ReCET:** Summed into the TRC ratio's numerator (`TaxCredits`); not part of any cost or of `TotalSystemBenefit`. |

**A cost-test set's own columns.** A cost-test set (`cost_test_set`, section 3) may read Measure columns beyond these, for a non-energy benefit say
([COST_TESTS.md](COST_TESTS.md) section 3, input streams): numbers per unit, blank = 0, after the columns above (`recet template measure --cost-tests SET`
writes the header with them). A file without such a column prices it as 0, with a note; a value that is not a number stops the run before pricing. With
statewide allocation they take the Weighted Factor.

---------------------------------------------------------------------------------------------------------------------------------

## 2. `ProgramCost.csv`

Optional. One row per program per year; pipe or comma delimited. Every `PrgID` must appear in both files. Dollar amounts are **nominal dollars for the whole program-year** (not per unit). All ten cost fields are summed (C11.1);
blank = 0.

| Field | Type / unit | Description and use |
|---|---|---|
| `PrgID` | text | The unique and persistent program identifier. The ProgramCost primary key is `PrgID` and `PrgYear` in combination. |
| `PA` | `PGE` `SCE` `SDGE` `SCG` (or `SW`) | The program administrator; A SW (statewide) row is treated as SCE unless statewide allocation is on (which sets it to each IOU). **In ReCET:** Selects the discount rate for this row's costs. |
| `PrgYear` | year | The claim year. The ProgramCost primary key is `PrgID` and `PrgYear` in combination, must match the year in `ClaimYearQuarter`. |
| `ClaimYearQuarter` | `YYYYQn` | The year and claim quarter, e.g. '2023Q1'. **In ReCET:** Its year is the year the cost is incurred and discounted from (C11.1). |
| `AdminCostsOverheadAndGA` | Dollars | Program-level general administration costs and overhead. |
| `AdminCostsOther` | Dollars | Program-level other administration costs (not general administration or overhead). |
| `MarketingOutreach` | Dollars | Program-level marketing outreach costs. |
| `DIActivity` | Dollars | Direct-implementation activity costs. |
| `DIInstallation` | Dollars | Direct-implementation installation costs. |
| `DIHardwareAndMaterials` | Dollars | Direct implementation hardware and materials costs. |
| `DIRebateAndInspection` | Dollars | Direct-implementation rebate and inspection costs. |
| `EMV` | Dollars | Program-level evaluation, measurement, and verification costs. |
| `UserInputIncentive` | Dollars | Program-level incentives and rebates. |
| `CostsRecoveredFromOtherSources` | Dollars | This field captures program-level energy efficiency (EE) activity costs that are recovered from ratepayers outside of the EE Public Purpose Program (PPP) budget, such as those covered by the General Rate Case (GRC). These costs are included to ensure accurate cost-effectiveness calculations but should not be confused with Integrated Demand-Side Management (IDSM) fields. **In ReCET:** Enter it as a negative to reduce the total, as CET does. |
| `OnBillFinancing` | Dollars | Program-level on-bill financing. **In ReCET:** **Not** included in the cost sum (CET's `SumCosts` omits it). |

---------------------------------------------------------------------------------------------------------------------------------

## 3. Run settings

Chosen in the GUI (each control is described in [USER_GUIDE.md](USER_GUIDE.md#the-gui-control-by-control)), on the command line, or in a saved settings file (`recet run --config job.json`; the GUI's *Settings > Save settings* writes one).
The JSON key is the name in `RunConfig`; command-line flags override a settings file.

| JSON key | GUI control | Command-line flag | Default | Meaning |
|---|---|---|---|---|
| `measures` | Measure.csv | `--measures` | (required unless a workbook is given) | Path to the CET `Measure.csv`. |
| `programcost` | ProgramCost.csv | `--programcost` | none | Path to `ProgramCost.csv`; without it there are no program costs (NoAdmin = regular costs, `WeightedProgramCost` = 0). |
| `workbook` | Job workbook (.xlsx) | `--workbook` | none | An Excel job workbook (section 4.7) used instead of the separate files; each of its data sheets supplies the matching input. A file given separately wins over its sheet. |
| `out_dir` | Output folder | `--out` | `recet_results` (GUI: `~/recet_results`) | Folder in which the run's folder is created. |
| `run_name` | Run name | `--name` | a timestamp | Prefix of the run folder name. |
| `engine` | (web: Calculation engine) | `--engine` | `recet` | The calculation engine (D13, BRD BR-06.4): blank or `recet` = ReCET's own hourly pricing; `cet-<release>` (`cet-25.1`, `cet-25.0`, `cet-24.3`, `cet-24.2`, `cet-24.1`, `cet-22.1`, `cet-21.0`) = an exact replica of that CET release on its own quarterly tables, CET's errors included (CALCULATIONS C20). A replica prices the ACCs its release has (`recet cet-version list`), writes the release's SavedCE / SavedSavings / SavedEmissions / SavedCost tables plus a measure-level table in ReCET's layout, and refuses the options that are ReCET's own: supplied ACC, profile, run-parameter or statewide-factor files, statewide allocation, ACC components, the combined 8,760, `compare_with`, `cet_outputs`, Check inputs only and the CEDARS filing checks (a filing accepts only ReCET's own engine). Its SCT and emissions columns are those its release wrote, whatever `include_societal` / `emissions` say. Its tables are read from the reference data folder's `cet_versions` export. |
| `acc_version` | Run against | `--acc` | `2024` | `2021`, `2022`, `2024` or `2026` (the ACC of that year; 2021 and 2022 are TRC only), or a new label defined by supplied ACC files. |
| `acc_electric_file`, `acc_gas_file` | Electric ACC file, Gas ACC file | `--acc-electric-file`, `--acc-gas-file` | none | Supplied ACC file(s); replace the built-in file of `acc_version`, or define a new version. |
| `acc_draft` | (none) | `--acc-draft` | false | The supplied ACC file(s) are a draft ACC (BRD v0.5 BR-11.8): the run is refused when it asks for CEDARS filing checks (`filing_validation`) or is priced for a filing (`acc_purpose`); any other run labels the ACC "(draft)" in `Used_ACCVersion` and `run_settings.json` and notes it. An ACC in the reference folder is marked draft in its pack's manifest instead (`recet data build --draft 2028`, `recet data draft FOLDER 2028`). |
| `settings_from` | Run parameters of ACC version (for a NEW ACC label) | `--settings-from` | `2026` | The ACC version whose built-in run parameters and other E3 tables (discount rates, emissions, rate schedules) a *new* ACC label uses. A built-in version uses its own unless this is set. Saved configurations from before 0.1.17 with `e3_version` (CET's numbering) are converted. |
| `run_parameters_file` | Run parameters | `--run-parameters` | none | Run parameters per PA (section 4.5): a listed PA's discount rates, base year and gas CO2 replace the built-in values for this run. |
| `programs_file` | Program metadata | `--programs` | none | Program metadata in the CEDARS Programs list layout (section 4.8): a `PrgID` replaces the built-in row, a new one is added. |
| `programs_replace` | (none) | `--programs-replace` | off | The `programs_file` table is the whole program table: the built-in CEDARS Programs list is not used, so a `PrgID` the file leaves out is not found (its Prg_ columns are blank and preflight warns `program_not_in_metadata`). The file may then have no rows. This is how a caller with its own program registry runs: the CEDARS platform passes the registry's programs with this on, on ReCET's engine and on a CET replica, and the worker's `receipt.json` records `programs.mode` "replace" (else "merge"). Needs `programs_file`. |
| `sw_factors_file` | SW allocation factors | `--sw-factors` | none | Statewide allocation factors (section 4.6): a (`Year`, `ProgramID`) row replaces the built-in row, a new one is added. Used only with `allocate_statewide`. |
| `sw_factors_replace` | (none) | `--sw-factors-replace` | off | The `sw_factors_file` table is the whole factor table: no built-in row is used, so a (`Year`, `ProgramID`) the file leaves out is not allocated. This is how a caller pins an allocation version (BR-13.8): the CEDARS platform passes the version's full table with this on, and the worker's `receipt.json` records `allocation.mode` "replace" (else "merge"). Needs `sw_factors_file`. |
| `profiles_8760_file`, `profiles_gas_file` | 8,760 electric profiles, Gas monthly profiles | `--profiles-8760`, `--profiles-gas` | none | Supplied profile files; a match replaces the library profile, a non-match is added. |
| `normalize_profiles` | (settings file / flag only) | `--no-normalize-profiles` | true | Rescale supplied profiles that do not sum to 1 (false = stop with an error instead). |
| `reference_dir` | Reference data folder | `--data-dir` | saved default | Folder holding the large reference files (section 5). |
| `components` | Calculate ACC components separately | `--components` | false | Price each ACC component separately (C16.1). |
| `portfolio_8760` | Produce combined 8760 electric load profile | `--portfolio-8760` | false | Write the portfolio's hourly net kWh (C16.2). |
| `profile_years` | Year(s) | `--profile-years` | First Year | Calendar years of the combined profile. |
| `market_effects` | Apply 5% market effects (benefits and costs) | `--market-effects` | 0.0 | 0 or 0.05, added to the NTG of benefits and of costs (CET's `MEBens` and `MECost` together; C13). |
| `include_societal` | Include societal cost tests | `--no-societal` (turns it off) | true | Compute SCB / SCH; off = those columns blank and the run about 3x faster. |
| `emissions` | Include emissions | `--no-emissions` (turns it off) | true | Compute the CO2 / NOx / PM10 columns (C14). |
| `allocate_statewide` | Allocate statewide (SW_) programs | `--allocate-statewide` | false | Split statewide programs across the IOUs before pricing (C15). |
| `apply_exclude_from_ce` | Leave Exclude_From_CE programs out of the portfolio summary | `--apply-exclude-from-ce` | false | Measures of programs whose `Exclude_From_CE` is true in the program metadata are left out of every summary row and shown in one `excluded` row (OUTPUT_FIELDS 2.2). They are still priced and in the measure-level table. |
| `base_year` | First Year | `--base-year` | earliest claim year | CET's First Year: the reference point of all discounting (C2.5). |
| `output_template` | Output template | `--output-template` | every column | The measure-level file's columns, in order (BRD BR-06.10): a built-in template (*Key results*, *Participant costs*), one you saved, or a file (one column per line, or JSON `{"name": ..., "columns": [...]}`). Only the measure-level file is shaped: the Results tables, the summary and the key metrics still use every column. A column the run does not produce (an SCT column with the societal tests off) is left out, with a note. Names are those of OUTPUT_FIELDS.md section 1 and the input columns. |
| `output_template_name` | (the template's name) | (from `--output-template`) | blank | The template's name, for the record in `run_settings.json`. |
| `acc_purpose` | Priced for | `--priced-for` | any filing | What the run is priced for, for the ACC check (BRD BR-06.5; OUTPUT_FIELDS 2.9b): blank = any filing (a record is flagged when no filing covering its program year uses its ACC), `claims` (the ACC of the program year's final budget filing), or one budget filing by key, e.g. `2028-2035-application`, `true-up-advice-letter-september-2027` (`recet run --help` lists them). Flagged records are still priced. |
| `cost_test_set` | Cost tests | `--cost-tests` | the set in force | The cost-test definition set the tests are priced on (BRD BR-06.8; [COST_TESTS.md](COST_TESTS.md)): a built-in set's id (`EE-2026`; `recet cost-tests list`), or the path of a set's YAML file for an analysis run. Blank = the built-in set in force for the job's first program year. A run with CEDARS filing checks or priced for a filing accepts only a published built-in set (a supplied file or a draft set is refused); a set not in force for the job's program years is a warning (`cost_tests_not_for_program_year`), never a stop. The set is recorded in `Used_CostTestSet` and `run_settings.json` (`cost_tests`, with the file's SHA-256). |
| `cet_outputs` | Compare with a CET outputs file | `--cet-outputs` | none | Path to a CET `*_outputs.csv` to compare against (section 7). |
| `compare_with` | Also run another ACC version | `--compare-with` | none | A second built-in ACC version to run and compare measure by measure. |
| `output_format` | Output format | `--format` | `csv` | `csv`, `csv.zip`, `xlsx` or `parquet` for every table written. `csv.zip` writes each CSV inside its own zip (`<table>.csv.zip` holding `<table>.csv`), about a fifth of the size: a 1,000,000-measure measure-level table is 3.5 GB as CSV and 0.74 GB zipped; the web app always uses it when CSV is chosen. CSV is formatted by pyarrow (about 8x faster than pandas) and, for the measure-level table, written from a parquet copy kept in the run's `_results` folder for the Results tables. Values are the same as pandas would write; the text can differ (`0` for `0.0`, `0.0000704` for `7.04e-05`, text values quoted). Zips are compressed with ISA-L when the optional `isal` package is installed (`pip install ".[fast]"`; part of the web app's install), else zlib. `xlsx` writes one formatted Excel file per table, for a job of up to `excel_max_rows` measures; a larger job writes every table as `excel_fallback` (zipped CSV) instead, so its files are all one format, and the run log says so. |
| `excel_max_rows` | (none) | `--excel-max-rows` | 50,000 | The most measures (after statewide allocation) a job may have for Excel output (Excel itself holds 1,048,575 rows). Writing runs at about 400 rows a second for the 344-column measure-level table, so 50,000 rows take about 2 minutes and make about a 100 MB workbook; a million would take 40 minutes. A larger Excel job writes every table as `excel_fallback`. |
| `excel_fallback` | (none) | `--excel-fallback` | `csv.zip` | The format of every table of an Excel job over `excel_max_rows`: `csv.zip`, `csv` or `parquet`. |
| `preflight_only` | Preflight check only | `--preflight-only` / `preflight` | false | Check the inputs and stop before pricing. |
| `filing_validation` | CEDARS filing checks | `--filing-validation` | none | `claims` or `budget_filing`: also apply CEDARS' upload checks for that filing and flag the records it would reject or warn on (OUTPUT_FIELDS 2.10). They are still priced. |
| `filing_year` | Filing year | `--filing-year` | each record's claim year | The filing's year, for the rules that name it (budget filing: `CEInputID` starts with `<PA>-<year>`; `PrgYear` equals it). Blank: each record's claim year, and for `ProgramCost` the job's earliest claim year. |
| `parquet_row_group` | (none) | `--parquet-row-group` | 200,000 | Rows per row group when a job run in parts writes parquet. Bigger groups compress much better (a 1,000,000-measure measure-level table is 666 MB in 20,000-row groups, 302 MB in 100,000 and 253 MB in 200,000) but each is held in memory while it is written: about 0.4 GB per 100,000 rows, so 200,000 adds about 0.7 GB to the run's peak. Use 50,000-100,000 on a 2 GB server. |
| `chunk_size` | (none) | `--chunk-size` | 50,000 | A job whose `Measure.csv` has more rows than this is run in parts of this many rows, so the run holds about one part at a time (about 1 GiB peak at 1,000,000 records on the web server, against 17.8 GB as one piece; smaller parts use less memory and are a little slower: 0.96 GB at 20,000 on a laptop, the default before 0.1.56; README Performance, USER_GUIDE "What a run does"). The files written are the same as a whole-job run's, values equal to rounding. 0 = always run the whole job at once. |
| `max_records` | (none) | `--max-records` | 2,000,000 | The record cap (BRD v0.5 BR-06.3): a job whose `Measure.csv` has more data rows than this is refused before anything is split or priced, with the preflight error `too_many_records` (a CET replica engine refuses it the same way). ReCET's run time grows about linearly with the job and its memory stays about that of one part (`chunk_size`), so the cap can be raised without a redesign; 0 = no cap. The web server can set it in `RECET_WEB_RUN_DEFAULTS`. |
| `replica_part_size` | (none) | `--replica-part-size` | 10,000 | With a CET replica engine (`engine` = `cet-<release>`): the measures priced at a time (`cet_versions.price`, two passes: benefits and program sums, then each part finished with the whole job's sums). A replica expands each measure to every quarter of its avoided costs, as CET's SQL does, so its parts are smaller than `chunk_size`: a 200,000-measure job on CET 25.1 peaks at 1.2 GB with 10,000 and at 4.2 GB with 50,000, in the same time. Values equal a whole-job run's to rounding. 0 = price the whole job at once. |
| `subset_step` | (none) | (none) | 1 | Test hook: price every Nth measure only. Program costs then weight a partial job; never use for a deliverable. |

---------------------------------------------------------------------------------------------------------------------------------

## 4. Supplied input files

Templates for each: GUI **Template...** buttons or `recet template ...`. Matching rules (a match *replaces* the built-in item, a non-match *adds* one) are in [INPUT_FORMATS.md](INPUT_FORMATS.md).

### 4.1 8,760-hour electric profiles (CSV or Parquet, long format)

| Column | Type / unit | Required | Description |
|---|---|---|---|
| `end_use_name` | text | yes | Must equal the measure's `AC_Elec_Impact_Profile` (case-insensitive). |
| `pa` | `PGE` `SCE` `SDGE` `SCG`; blank / `*` / `ALL` = any | yes | The measure's `IOU_AC_Territory`. |
| `target_sector` | text; blank / `*` / `ALL` = any | yes | The measure's `AC_Elec_Target_Sector`. |
| `climate_zone` | `1`..`16`, `3A`, `3B`; `System` or `ALL` = every zone without its own rows | yes | The measure's `AC_Climate_Zone`. |
| `hour_of_year` | integer 1-8760 | yes | The hour, exactly once per (profile, zone). Hour 1 is 00:00 on 1 January of `represents_year`. |
| `impact` | fraction of the measure's annual net kWh | yes | The share of the year's savings that falls in the hour; each zone's 8,760 values should sum to 1 (a shape summing to about 0, i.e. pure load shifting, cannot be rescaled and is rejected). |
| `represents_year` | calendar year | optional | The year whose weekdays the shape follows (same on every row of a profile); default 2009. The shape is rotated once onto the ACC's weekdays (2018 or 2020, C4.2). |
| `profile_basis` | `share` or `kwh_per_unit` | optional | Same on every row of a profile. `share` (the default): `impact` is the share of annual savings, as above. `kwh_per_unit`: `impact` is already the kWh saved per unit in each hour (negative = added load), priced as given and not rescaled (C4.5, BRD BR-06.6). |

### 4.2 Gas monthly profiles (CSV or Parquet)

| Column | Type / unit | Required | Description |
|---|---|---|---|
| `Profile` | text | yes | Must equal the measure's `AC_Gas_Impact_Profile`. |
| `Month` | 1-12 or Jan..Dec | yes | Exactly one row per month per profile. |
| `Impact` | fraction of annual therms | yes | The month's share; the 12 values sum to 1 (rescaled, with a note, if not). |

### 4.3 Electric ACC (Parquet recommended; CSV/xlsx for small files)

One row per `(iou, climate_zone, cost_test, year, hour_of_year)`; dollar columns are **$/MWh**.

| Column | Type / unit | Required | Description |
|---|---|---|---|
| `iou` | `PG&E` `SCE` `SDG&E` | yes | The utility (`PGE`, `SDGE` accepted in CSV and canonicalized). |
| `climate_zone` | `CZ1`..`CZ16` (`CZ3A`, `CZ3B`) | yes | The climate zone. Only the zones the utility serves need rows. |
| `cost_test` | `TRC`, `SCT Base`, `SCT High` | yes | `TRC` rows are required; the two SCT rows are needed for SCB / SCH only. |
| `year` | consecutive calendar years | yes | The pricing year. |
| `hour_of_year` | 1-8760 | yes | Every (iou, zone, cost test, year) block must have all 8,760 hours. |
| `total_avoided_cost` | $/MWh | yes | The sum of the component columns below (checked to 1e-6). |
| `transmission`, `distribution` | $/MWh | for water energy | The T&D part; `Gen` = total minus these (water energy is priced on `Gen`). |
| `emission_rate_tonnes_per_kwh` | metric tons CO2 per kWh | for electric CO2 | The grid emission rate of that hour. |
| `energy`, `generation_capacity`, `avoided_as_procurement`, `losses`, `methane_leakage`, `ghg_cap_and_trade`, `ghg_portfolio_rebalancing` (2024 ACC) or `ghg_cap_and_invest` (2026 ACC), `ghg_adder`, `air_quality_adder` | $/MWh | for components mode | The avoided-cost components. |
| `price_zone`, `datetime`, `month`, `day`, `hour` | - | no | Ignored. |

### 4.4 Gas ACC (CSV or Parquet)

One row per `(utility, class, cost_test, scc, year)`; dollar columns are **$/therm**.

| Column | Type / unit | Description |
|---|---|---|
| `utility` | `PG&E`, `Socal Gas` (or `SoCalGas`), `SDG&E` | The gas utility. |
| `class` | `Residential`, `Commercial`, `Total Core` | The customer class (`AC_Gas_Target_Sector`: Residential, Commercial, Core). |
| `cost_test` | `TRC` or `SCT` | `TRC` rows are required. |
| `scc` | blank for `TRC`; `Base` or `High` for `SCT` | The social-cost-of-carbon scenario. |
| `year` | consecutive calendar years | The pricing year; years outside the file use its nearest year. |
| `market_jan` ... `market_dec` | $/therm | Commodity price by month. |
| `td_jan` ... `td_dec` | $/therm | Transportation / distribution by month. |
| `nox`, `co2`, `upstream_methane`, `air_quality_adder` | $/therm | Annual values (used in every month). |
| `behind_meter_methane` | $/therm | Annual value; included only for residential fuel-substitution profiles (C3.2). |

---------------------------------------------------------------------------------------------------------------------------------

### 4.5 Run parameters (CSV, Parquet or xlsx)

The built-in run parameters are the RunParameters table (section 6). A supplied file replaces them for the PAs it lists; other PAs keep the built-in values. The template
(`recet template run-parameters`) is the built-in rows of the chosen ACC version.

| Column | Type / unit | Required | Description |
|---|---|---|---|
| `PA` | `PGE` `SCE` `SDGE` `SCG` (`PG&E` / `SDG&E` / `SoCalGas` accepted) | yes | The PA whose built-in run parameters this row replaces. One row per PA. |
| `DiscountRateAnnual` | fraction (0.0766 = 7.66%) | yes | Annual discount rate: program costs and bill impacts (C2.4, C9, C11). |
| `DiscountRateQtr` | fraction | optional (default `DiscountRateAnnual / 4`) | Quarterly rate for measure-level streams (C2.4). CET stores the annual rate / 4. |
| `DiscountRateAnnual_SCT` | fraction | yes | Annual societal discount rate (SCB / SCH). |
| `DiscountRateQtr_SCT` | fraction | optional (default `DiscountRateAnnual_SCT / 4`) | Quarterly societal rate. |
| `BaseYear` | year | optional (default: the built-in value for the run's version) | CalcCE's `Yr1` base for the bill-impact window (C9). |
| `CO2Gas` | metric tons CO2 per therm | yes | Gas CO2 factor for the emission columns (C14). |

### 4.6 SW allocation factors (CSV, Parquet or xlsx)

The built-in table's layout (section 6); the template (`recet template sw-factors`) is the whole built-in table. A row whose (`Year`, `ProgramID`) is in the built-in
table replaces it; any other row is added (with `sw_factors_replace`, the file is the whole table instead). Factor sums that are not 1 (or 0) are reported in the log.

| Column | Type / unit | Required | Description |
|---|---|---|---|
| `Year` | year | yes | The claim year the factors apply to (a measure's `ClaimYearQuarter` year; a program cost's `PrgYear`). |
| `ProgramID` | text | yes | The statewide program's `PrgID`. |
| `PGEGasFactor`, `SDGEGasFactor`, `SCEGasFactor`, `SCGGasFactor` | fraction | yes | Each IOU's share of the program's therms; should sum to 1 (or all be 0 when the program has no gas). |
| `PGEElectricFactor`, `SDGEElectricFactor`, `SCEElectricFactor`, `SCGElectricFactor` | fraction | yes | Each IOU's share of the program's kWh, kW and water kWh; should sum to 1 (or all 0). |
| `PGEWeightedFactor`, `SDGEWeightedFactor`, `SCEWeightedFactor`, `SCGWeightedFactor` | fraction | one of these or `PercentElectric` | Each IOU's share of costs and other benefits. If absent, computed as `PercentElectric x ElectricFactor + (1 - PercentElectric) x GasFactor`. |
| `PercentElectric` | fraction | see above | The program's electric share, used only to compute the weighted factors. |
| `ProgramName`, `Summary` | text | optional | Carried, not used. |

### 4.7 Job workbook (xlsx)

One Excel file instead of the separate files (`recet template workbook`). Sheet names are matched ignoring case, spaces and underscores; other sheets are ignored, and
a sheet with only a header row counts as not given.

| Sheet | Required | Same columns as |
|---|---|---|
| `Measure` | yes | `Measure.csv` (section 1) |
| `ProgramCost` | no | `ProgramCost.csv` (section 2) |
| `RunParameters` | no | section 4.5 |
| `SWAllocation` | no | section 4.6 |
| `Programs` | no | section 4.8 |
| `GasProfiles` | no | section 4.2 |
| `Profiles8760` | no | section 4.1 (Excel fits about 119 profile x zone blocks) |

Cells are read as CET text: a whole number without decimals (`13`, not `13.0`), other numbers exactly, empty cells blank, `TRUE`/`FALSE` as `1`/`0`; no cell may contain `|`.
The ACC files are not sheets: supply them as separate files.

### 4.8 Program metadata (CSV or xlsx)

The CEDARS Programs list layout (the Programs page's download); the template (`recet template programs`) is the whole built-in table. A row whose `PrgID` is in the
built-in table replaces that row as a whole; any other row is added (with `programs_replace`, the file is the whole table instead). Only `PA` and `PrgID` are required; a missing column is blank and an extra column is carried as
`Prg_<column>`. Values are text, kept as written; a flag counts as true when it reads `True`, `1`, `Yes`, `Y` or `T` (any case). The metadata changes no priced value
(OUTPUT_FIELDS 1.14). Statewide programs are listed once per funding IOU (`PGE_SW_...`, `SCE_SW_...`, `SDGE_SW_...`, `SCG_SW_...`) plus an `SW_...` row.

| Column | Type / unit | Required | Description |
|---|---|---|---|
| `PA` | text | yes | The program administrator. |
| `PrgID` | text | yes | The program ID, unique; matched to the measure's `PrgID` as priced. |
| `ProgramName`, `ProgramStatus`, `ProgramSegment`, `StatewideProgram`, `PrimarySector`, `Sector`, `BPSector`, `ProgramImplementer`, `ProgramCategory`, `ImplementationContractor`, `ParentProgram`, `DirectInstall`, `Financing` | text | no | Descriptive fields (OUTPUT_FIELDS 1.14). `ProgramSegment` gives the summary's `program segment` rows. |
| `StartYear`, `EndYear` | year | no | The program's years; a claim year outside them is reported by the preflight (`claim_outside_program_years`). |
| `Resource_Flag`, `NonResource_Flag`, `Deemed_Flag`, `Custom_Flag`, `Upstream_Flag`, `Midstream_Flag`, `Downstream_Flag`, `Audit_Flag`, `Exclude_From_Budget`, `Exclude_From_Statewide_Flag` | flag | no | Carried. |
| `Exclude_From_CE` | flag | no | Used by `apply_exclude_from_ce`. |

## 5. Reference data files (the large files)

Kept in the *reference data folder* (GUI box, `--data-dir`, `RECET_DATA`, or `recet data set-dir`), not in the package. `recet data status` lists what is found.

| File in the folder | Size | Contents |
|---|---|---|
| `acc/2021/electric_hourly.parquet` | smaller | The 2021 ACC's hourly electric avoided costs (TRC only); optional: needed only to run on the 2021 ACC. |
| `acc/2022/electric_hourly.parquet` | smaller | The 2022 ACC's (TRC only), same layout; optional: needed only to run on the 2022 ACC. |
| `acc/2024/electric_hourly.parquet` | ~1 GB | The 2024 ACC's hourly electric avoided costs; the layout of section 4.3. |
| `acc/2026/electric_hourly.parquet` | ~1 GB | The 2026 ACC's (final, v1b), same layout. |
| `impact_profiles/catalog.parquet` | small | The CEDARS impact-profile catalog: one row per profile. Columns used: `id` (profile ID), `end_use_name` (= `AC_Elec_Impact_Profile`), `iou_ac_territory` (`PGE`...), `target_sector`, `year_represented` (the calendar year the shape follows; blank = 2009), and `shape_id` when present (the hourly row group holding the row's 8,760s: in a consolidated library every name of a profile points at one stored copy, `recet data consolidate-profiles`; blank = no hourly data; without the column each row reads its own `id`). Other columns (`pa` long name, `climate_zones`, `start_year`, `explicit_end_year`, `description`, `source_impact_type`, `ac_version`) are informational. |
| `impact_profiles/hourly.parquet` | ~9 GB | The 8,760-hour profiles: `impact_profile_id` (matches the catalog's `shape_id`, else `id`), `climate_zone` (a zone or `System`), `hour_of_year` (1-8760), `impact` (fraction of annual savings in that hour; each (profile, zone) sums to 1). One row group per stored profile. |

---------------------------------------------------------------------------------------------------------------------------------

## 6. Built-in tables (shipped inside the package)

ReCET's names drop the `E3` prefix of the CET tables they copy; the CET name is given in brackets.

| File (`recet/data/...`) | Columns used | Role |
|---|---|---|
| `run_parameters.csv` (RunParameters; CET `E3Settings`) | `PA`, `Version`, `DiscountRateAnnual`, `DiscountRateQtr` (annual / 4), `DiscountRateAnnual_SCT`, `DiscountRateQtr_SCT`, `BaseYear` (2021, 2022, or 2024 for the 2024 and 2026 ACCs), `CO2Gas` (metric tons CO2 per therm: 0.00585 for the 2021 ACC, 0.00531 for the others) | The built-in **run parameters**: discount rates and the gas CO2 constant, by utility and CET `Version`, read by ACC version through the mapping in C3.3 (C2.4, C9, C14). `AvoidedCostVersion` is the publication date; `NOxGas` and the `Societal...` rate columns are not used. |
| `cet_tables/emissions.parquet` (Emissions; CET `E3Emissions`) | `PA`, `Version`, `TS` (target sector), `EU` (end use), `CZ`, `NOx`, `PM10` | Electric NOx and PM10 factors per annual kWh; no matching row means 0 (C14.3). Its `CO2` column is not used (electric CO2 comes from the ACC's hourly emission rate). |
| `cet_tables/combustion_type.parquet` (CombustionType; CET `E3CombustionType`) | `LookupCode` (= `CombustionType`), `NOx` (per therm) | Gas NOx factor (C14.3). |
| `cet_tables/rate_schedule_elec.parquet` (RateScheduleElec; CET `E3RateScheduleElec`) | `PA`, `Version`, `Year`, `Schedule`, `RateE` ($/kWh) | Retail electric rates by year (C9). |
| `cet_tables/rate_schedule_elec_mapping.parquet` (RateScheduleElecMapping; CET `E3RateScheduleElecMapping`) | `PA`, `Version`, `TargetSector`, `Schedule` | Maps a target sector to its schedule. |
| `cet_tables/rate_schedule_gas.parquet`, `rate_schedule_gas_mapping.parquet` (RateScheduleGas, RateScheduleGasMapping; CET `E3RateScheduleGas`, `E3RateScheduleGasMapping`) | `PA`, `Version`, `Year`, `Schedule`, `RateG` ($/therm); `GasSector`, `GasRateSchedule` | The same for gas. |
| `gas_savings_profiles.csv` | `Profile`, `Month`, `Impact` | The library of gas monthly profiles (C4.3). |
| `acc/2021/gas_components.parquet`, `acc/2022/gas_components.parquet`, `acc/2024/gas_components.parquet`, `acc/2026/gas_components.parquet` | the gas ACC layout of section 4.4 | The four gas ACCs, extracted from E3's workbooks (2021 and 2022: TRC only). |
| `statewide_allocation/statewide_factors_20260918.csv` | `Year`, `ProgramID`, `PercentElectric`, `<IOU>GasFactor`, `<IOU>ElectricFactor`, `<IOU>WeightedFactor` for PGE, SDGE, SCE, SCG | The IOU shares of each statewide program (C15). |
| `programs/cedars_programs_20261003.csv` | the columns of section 4.8 | The built-in program metadata: CEDARS' Programs list of 2026-10-03 (1,416 programs), without its `ProgramManager` column. |
| `statewide_allocation/field_rules.csv` | `table`, `field`, `rule` | Which factor (or pass-through) applies to each field of `Measure.csv`, `ProgramCost.csv` and the CET output (C15). |

---------------------------------------------------------------------------------------------------------------------------------

## 7. CET outputs file (for comparison)

Optional. A CET run's `*_outputs.csv` (pipe or comma delimited) with a `CET_ID` column; measures are matched to the POC's `CEInputID`. Columns compared when present: `TotalSystemBenefit`, `ElecBen`, `GasBen`, `WaterEnergyBen` (+ `_SB`, `_SH`,
`Gross`), `OtherBen`, `ElecSupplyCost`, `GasSupplyCost`, `WaterEnergyCost`, `TRCCost`, `PACCost`, `RIMCost`, `SCBCost`, `SCHCost`, `TRCRatio`, `PACRatio`, `RIMRatio`, and CET's `NetElecCO2`, `NetGasCO2`, `BillReducElec`, `BillReducGas` (compared with ReCET's `FirstYearNetElecCO2`, `FirstYearNetGasCO2`, `LifecycleBillReducElec`, `LifecycleBillReducGas`). The comparison is only
meaningful when the job is run the way the CET run was (same First Year, ACC version, and statewide allocation).
