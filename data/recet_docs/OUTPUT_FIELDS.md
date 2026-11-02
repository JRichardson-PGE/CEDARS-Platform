# Output field reference

Every column and file `recet` writes: what it means, its unit, **its complete formula in terms of the input fields**, and a worked number.
[INPUT_FIELDS.md](INPUT_FIELDS.md) defines the input fields; [CALCULATIONS.md](CALCULATIONS.md) explains the method behind the formulas (which avoided-cost rows and load shape are
used and why, the validation against CET, the limitations).

This page can be read without leaving it:

* **Section 0** defines, once, the handful of intermediate quantities the formulas are built from: the quarters of a measure's life, the discount factors, the baseline phasing, the
  annual quantities that are priced, the price of a quarter, and the lifetime values `V_elec`, `V_gas` and `V_water`. Each is defined from input fields.
* **Section 1** gives every measure-level column its own entry: meaning, unit, formula, what each symbol in the formula is, the inputs it depends on, and a worked number.
* **Section 2** does the same for every other file a run writes.
* **Section 3** holds the two worked examples behind the numbers in the entries: their inputs, quarter-by-quarter tables, and every output column for both.

Every number on an `Example` line and in section 3 is checked against a real run by the test-suite (`tests/test_output_docs.py`), and so is the arithmetic written on each line, so the examples cannot drift from the tool.

**How an entry reads.**

* **Formula** - the column written out in input-field names. Where a step is shared it uses a named intermediate from section 0 (`V_elec`, `DF(k)`, `phased(...)`).
* **Where** - what each intermediate or symbol in that formula is, defined from input fields (or a pointer to the entry of the output column it comes from).
* **Inputs** - the input fields, run settings and reference data the column depends on. A change in anything not listed cannot change it.
* **Example A / Example B** - the column's value for the two worked examples of section 3, with the arithmetic where it fits on one line.

**Units used throughout.** Money is nominal US dollars. A "PV" is present-valued to the run's First Year at the quarterly discount rate (bill impacts and program costs annually).
Ratios have no unit. "Net" values include net-to-gross (and the market-effects adder if on); "Gross" values do not. Emissions: CO2 in metric tons; NOx and PM10 in pounds.
A column is blank (`NaN`) only where it is switched off (societal columns without SCB/SCH, emissions without "Include emissions", components without components mode)
or cannot be computed.

* [0. Terms used in the formulas](#0-terms-used-in-the-formulas)
* [1. The measure-level table](#1-the-measure-level-table) (`measure_level_acc<version>.csv`)
* [2. Other files in a run folder](#2-other-files-in-a-run-folder)
* [3. The two worked examples](#3-the-two-worked-examples)

---------------------------------------------------------------------------------------------------------------------------------

## 0. Terms used in the formulas

### 0.1 Notation, defaults and run-level values

* **Input fields** are written exactly as in the file (`NumUnits`, `UnitkWh1stBaseline`); **output fields** the same way, and a formula that uses another output column says so.
* **Blank inputs.** Every numeric field CET requires is used as filed: a blank one stops the run (preflight `blank_required_value`). The fields CET does not
  require read a blank as 0: `UnitkWhIOUWater1stBaseline`, `UnitkWhIOUWater2ndBaseline`, `UnitDirectInstallLab`, `UnitDirectInstallMat`, `UnitEndUserRebate`,
  `UnitIncentiveToOthers`, `UnitTaxCredits`, `MeasInflation`. `EUL_Yrs` and `RUL_Yrs` may not be negative, nor `RUL_Yrs` above `EUL_Yrs` (preflight errors).
* **Operators.** `SUM over k ... of` is a sum; `pos(x) = max(x, 0)`; `ratio(n, d) = n / d`, and 0 when `d = 0` (the way CET's ratio columns behave); `^` is a power;
  `floor(x)` rounds down.
* **Run settings.** `F` is the run's **First Year** (the "First Year" box or `--base-year`; blank = the earliest claim year in the job). `MEB` is the market-effects adder on
  benefits and `MEC` the adder on costs: both 0.05 when "Apply 5% market effects (benefits and costs)" is on, both 0 otherwise.
* **Territory and discount rates.** `PA` is `IOU_AC_Territory`. The rates come from the run parameters (CALCULATIONS C3.3) for `PA` and the ACC version: `DiscountRateAnnual`,
  `DiscountRateQtr` (the annual rate divided by 4), and the societal `DiscountRateAnnual_SCT`, `DiscountRateQtr_SCT`. For PG&E (every built-in ACC):
  0.0766, 0.01915, 0.0503 and 0.012575.

```text
Rqf = 1 + DiscountRateQtr          Rqf_sct = 1 + DiscountRateQtr_SCT          the quarterly growth factors
Raf = 1 + DiscountRateAnnual       Raf_sct = 1 + DiscountRateAnnual_SCT       the annual ones (bill impacts, program costs)
```

### 0.2 The quarters of a measure's life

CET prices in **calendar quarters**. A measure delivers savings in `n` quarters, numbered `k = 0 ... n-1`; `k = 0` is the claim quarter itself.

```text
Y0 = first four characters of ClaimYearQuarter        q0 = its last character (1-4)
EQ = 4 * EUL_Yrs                                      the useful life in quarters (EUL_Yrs as filed; 0 gives no quarters)
n  = floor(EQ) + 1   if EQ - floor(EQ) > 0.000000001, else floor(EQ)       the number of quarters (a part-quarter counts as one)

a(k)       = (q0 - 1) + k
year(k)    = Y0 + floor(a(k) / 4)                     the calendar year of quarter k
quarter(k) = a(k) mod 4                               0 = Q1, 1 = Q2, 2 = Q3, 3 = Q4
w(k)       = EQ - floor(EQ)   if k = n-1 and EQ - floor(EQ) > 0.000000001        the quarter's weight: the last, part-quarter of a fractional life
           = 1                for every other quarter
```

A claim in Q3 delivers two quarters in its first calendar year. A life of 2.4 years is 9.6 quarters: ten quarters, the last weighted 0.6.
The hours of a quarter are numbered on the ACC's 8,760-hour calendar: Q1 = hours 1-2160, Q2 = 2161-4344, Q3 = 4345-6552, Q4 = 6553-8760 (2160, 2184, 2208 and 2208 hours).
*Example A* (claim 2028Q3, `EUL_Yrs` 2.4): `n` = 10, `k = 0` is 2028Q3, and `w(9)` = 0.6. *Example B* (claim 2028Q2, `EUL_Yrs` 2.5): `n` = 10, `k = 0` is 2028Q2. Tables 3.2 and 3.4 list every quarter.

### 0.3 Discounting

```text
Qac(k) = (year(k) - F) * 4 + quarter(k)          calendar quarters from Q1 of the First Year (not from the claim; negative before it)
DF(k)  = 1 / Rqf ^ Qac(k)                             the quarterly discount factor;  DF_sct(k) = 1 / Rqf_sct ^ Qac(k)

Qm  = (Y0 - F) * 4 + (q0 - 1)                         the claim quarter's own Qac
PVF = Rqf ^ Qm                                        one-time amounts at the claim are divided by PVF  (PVF_sct with Rqf_sct)
```

Streams CET holds annually (bill impacts, program costs) use `Raf` and whole years instead (entries `LifecycleBillReducElec`, `PACCost`).
*Example A*: `F` = 2028, so `DF(0)` = 1 / 1.01915^2 = 0.962773 (2028Q3 is 2 quarters after Q1 of 2028) and `PVF` = 1.01915^2 = 1.038667.

### 0.4 Baseline phasing

A measure may have a second baseline: the savings are measured against the 1st baseline until the remaining useful life (`RUL_Yrs`) ends and against the 2nd baseline after it.
With `RQ = 4 * RUL_Yrs` (RUL counted from the claim quarter), a quantity `a` (1st baseline) and `b` (2nd baseline) is phased as

```text
phased(k; a, b) = a                                    if RQ <= 0 or k + 1 <= RQ      single baseline, or quarter k entirely before the RUL ends
                = b                                    if k >= RQ                     quarter k entirely after it
                = (RQ - k) * a + (1 - (RQ - k)) * b    otherwise                      the quarter that contains the end of the RUL: a blend
```

`RUL_Yrs` of 0 is a single baseline: `a` applies for the whole life and every 2nd-baseline field is ignored (a blank, negative or above-`EUL_Yrs` `RUL_Yrs` stops the run in
preflight). An `RUL_Yrs` equal to `EUL_Yrs` (preflight warning `rul_not_below_eul`) is not an early replacement either: as in CalcCE (its InputMeasurevw sets an RUL
that is not below the EUL to 0), every formula in this file then uses `RUL_Yrs` = 0, so the measure is single-baseline for its whole life. *Example B*: `RUL_Yrs` 1.125, so `RQ` = 4.5; quarters 0-3 use
the 1st baseline, quarter 4 is a half-and-half blend (`RQ - k` = 0.5) and quarters 5-9 use the 2nd baseline.

### 0.5 The annual quantities that are priced

Electricity, gas and the electricity embedded in water are each priced as an annual quantity spread over the year. These are output columns of section 1.2, in kWh per year, therms per year and kWh per year embedded in water, for the 1st and 2nd baseline:

```text
AC_Net_kWh_1st_Baseline       = NumUnits * UnitkWh1stBaseline * NTGRkWh * InstallationRatekWh * RealizationRatekWh
AC_Net_kWh_2nd_Baseline       = NumUnits * UnitkWh2ndBaseline * NTGRkWh * InstallationRatekWh * RealizationRatekWh
AC_Net_Therms_1st_Baseline    = NumUnits * UnitTherm1stBaseline * NTGRTherm * InstallationRateTherm * RealizationRateTherm
AC_Net_Therms_2nd_Baseline    = NumUnits * UnitTherm2ndBaseline * NTGRTherm * InstallationRateTherm * RealizationRateTherm
AC_Net_kWh_Water_1st_Baseline = NumUnits * UnitkWhIOUWater1stBaseline * NTGRkWh * InstallationRatekWh * RealizationRatekWh
AC_Net_kWh_Water_2nd_Baseline = NumUnits * UnitkWhIOUWater2ndBaseline * NTGRkWh * InstallationRatekWh * RealizationRatekWh
```

These hold for every measure on a share profile (the library's profiles, and supplied ones whose hours sum to 1). A measure priced on a supplied **kWh-per-unit**
profile (`Scaled_Profile_Flag` true, CALCULATIONS C4.5) differs in three ways: the profile's own annual kWh per unit replaces `UnitkWh1stBaseline` (`UnitkWh2ndBaseline`)
in `AC_Net_kWh_*_Baseline` and in every other kWh formula of this file (savings quantities, bill impacts, emissions); the electric value multiplies the profile by
`AC_Net_kWh_ScaledProfile_*_Baseline` = `NumUnits * NTGRkWh * InstallationRatekWh * RealizationRatekWh` (not written) in place of `AC_Net_kWh_*_Baseline` in `Elec(k)`
below, because the profile carries the kWh; and the water energy is spread by the profile divided by its annual kWh.

When the run applies market effects to benefits, each is scaled by `(NTG + MEB) / NTG` (`NTG` = `NTGRkWh` for electric and water, `NTGRTherm` for gas; the scaling is 1 when `MEB = 0`
or `NTG <= 0`). Then, in quarter `k`, the quantities in force are

```text
Elec(k) = phased( k ;  AC_Net_kWh_1st_Baseline       * (NTGRkWh + MEB) / NTGRkWh ,
                       AC_Net_kWh_2nd_Baseline       * (NTGRkWh + MEB) / NTGRkWh )           annual kWh in force in quarter k
Gas(k)  = phased( k ;  AC_Net_Therms_1st_Baseline    * (NTGRTherm + MEB) / NTGRTherm ,
                       AC_Net_Therms_2nd_Baseline    * (NTGRTherm + MEB) / NTGRTherm )       annual therms
Wat(k)  = phased( k ;  AC_Net_kWh_Water_1st_Baseline * (NTGRkWh + MEB) / NTGRkWh ,
                       AC_Net_kWh_Water_2nd_Baseline * (NTGRkWh + MEB) / NTGRkWh )           annual kWh embedded in water
```

*Example A* (no market effects): `Elec(k)` = 34,200 kWh, `Gas(k)` = 1,620 therms, `Wat(k)` = 205.2 kWh in every quarter. *Example B* (`MEB` = 0.05, second baseline): `Elec(0)` = -27,000 x (0.75 + 0.05) / 0.75
= -28,800 kWh, `Elec(9)` = -18,000 kWh, and `Elec(4)` = -23,400 kWh (the blend). Tables 3.2 and 3.4 give all three quantities in every quarter.

### 0.6 The price of one unit in a quarter

**Electric** (`ElecPricingStatus` = `priced: hourly profile`; the only electric route -- TOU end uses are priced from their 8,760s in the library).

```text
shape(i)  = the measure's 8,760-hour load shape: the share of its annual kWh falling in hour i of the year (the shares sum to 1).
            The profile used is reported in `ProfileSource`; it is rotated once so its weekdays line up with the ACC's, which follow one calendar (2018, or 2020 for the 2021/2022 ACCs) in every
            forecast year (CALCULATIONS C4.1, C4.2).
AC(y, i)  = total_avoided_cost of the ACC row (utility of PA, "CZ" + AC_Climate_Zone, cost-test row, year y, hour i) / 1000
Gen(y, i) = AC(y, i) - (transmission + distribution)(y, i) / 1000
y*(k)     = year(k), or the nearest year in the ACC when year(k) lies outside it
U_e(k)    = SUM over the hours i of quarter(k) of  shape(i) * AC(y*(k), i)
U_w(k)    = the same with Gen in place of AC (water energy is valued on the generation part alone)
```

`AC` and `Gen` are in dollars per kWh; `U_e(k)` and `U_w(k)` in dollars per annual kWh delivered in quarter `k`. The cost-test row is `TRC` for the TRC, PAC and RIM columns, `SCT Base` for the societal-Base (`_SB`) columns and `SCT High` for the societal-High (`_SH`) columns. The utility, zone
substitutions and the price-year rule are in CALCULATIONS C3.1. Because `total_avoided_cost` already contains transmission and distribution, this route has no separate kW term.

**Gas.**

```text
gasshape(m) = the 12 monthly shares (sum 1) of the measure's AC_Gas_Impact_Profile
ACg(y, m)   = market_m + td_m + nox + co2 + upstream_methane + air_quality_adder [ + behind_meter_methane ]       $ per therm, month m of year y
y^(k)       = year(k) clamped to the gas ACC's first and last year
U_g(k)      = SUM over the months m of quarter(k) of  gasshape(m) * ACg(y^(k), m)
```

`ACg` is in dollars per therm and `U_g(k)` in dollars per annual therm delivered in quarter `k`. It comes from the gas ACC row for the utility of `PA`, the class of `AC_Gas_Target_Sector` and the cost test (`TRC`, `SCT Base`, `SCT High`); `behind_meter_methane` is included only for a Residential-class
measure whose `AC_Gas_Impact_Profile` contains `FuelSub`. The months of the quarters are Q1 Jan-Mar, Q2 Apr-Jun, Q3 Jul-Sep, Q4 Oct-Dec.

### 0.7 Lifetime values

The three signed lifetime values that sections 1.4 and 1.6 split into benefits and costs :

```text
V_elec  = SUM over k = 0 ... n-1 of  w(k) * DF(k) * Elec(k) * U_e(k)          $ PV, TRC price rows
V_gas   = SUM over k = 0 ... n-1 of  w(k) * DF(k) * Gas(k)  * U_g(k)
V_water = SUM over k = 0 ... n-1 of  w(k) * DF(k) * Wat(k)  * U_w(k)
```

`V_elec_SB`, `V_gas_SB`, `V_water_SB` (and `_SH`) are the same sums with the `SCT Base` (`SCT High`) price rows and `DF_sct(k)`. A value is negative when the measure adds load or use.
Tables 3.2 and 3.4 show each term of these sums for both examples. The entries `ElecBen`, `GasBen` and `WaterEnergyBen` say how each is split.

---------------------------------------------------------------------------------------------------------------------------------

## 1. The measure-level table

One row per measure, in the order of the input file, column groups left to right. The CSV is UTF-8 with a byte-order mark so Excel opens it directly; Parquet is available with the
"Output format" option. The measure-level files delivered for the 2024-versus-2026 comparison have one extra first column, `Job` (the claim year of the consolidated job each row
came from), added by the export script `poc/tou/export_measure_level.py`, not by the package. The columns renamed in 0.1.50, and those no longer written, are listed in 2.1.

### 1.1 `Used_ACCVersion`, `Used_CostTestSet` and the echoed input columns

#### `Used_ACCVersion`

Which ACC version the row was priced against. Unit: Text.

**Formula:** the run's ACC label, which names both the ACC (the year the calculator was released) and CET's Avoided Cost Version (the first year it was in effect
for claims or forecasts): `2021 ACC (Avoided Cost Version 2022)`, `2022 ACC (Avoided Cost Version 2024)`, `2024 ACC (Avoided Cost Version 2026)` or
`2026 ACC (Avoided Cost Version 2028)` for a built-in version, `Custom ACC '<label>'` for a new version defined by supplied files. **Inputs:** the "ACC version" setting (INPUT_FIELDS.md section 3).

#### `Used_CostTestSet`

Which cost-test definition set the run priced the tests on (BRD BR-06.8; [COST_TESTS.md](COST_TESTS.md)). Unit: Text.

**Formula:** a built-in set's id (`EE-2026`); a set supplied as a file is `custom: <file name>` (its id and SHA-256 are in `run_settings.json`); a draft set ends in ` (draft)`.
**Inputs:** the "Cost tests" setting (`cost_test_set`, INPUT_FIELDS.md section 3); blank = the set in force for the job's first program year.

#### The 74 `Measure.csv` columns (`CEInputID` ... `Comments`)

Every column of the input file, unchanged, as text (a blank stays blank), under ReCET's names: a file that used CET's legacy names (`E3ClimateZone` ...) gets the `AC_...` names here. Each is described in [INPUT_FIELDS.md](INPUT_FIELDS.md#1-measurecsv). **Formula:** copied.

### 1.2 Derived pricing inputs

The few values the pricing derives from the inputs that the inputs do not show directly: the claim year and quarter used, the annual quantities each baseline is priced on,
and the fuel-substitution flag. The pricing's other working values (the useful life in quarters, the market-effects factor, the quantity each profile is multiplied by ...)
restate an input and are not written; CALCULATIONS C1 lists them all. Every formula below uses only input fields. Each column's value for the two worked examples is in
section 3.5.

| Field | Meaning and unit | Formula from the input fields |
|---|---|---|
| `Used_Year` | The start year of the measure claim year quarter. Year. | first four characters of `ClaimYearQuarter` |
| `Used_Quarter` | The start quarter of the measure claim year quarter. 1-4. | last character of `ClaimYearQuarter` |
| `AC_Net_kWh_1st_Baseline` | The annual kWh the electric benefit for the 1st baseline is priced on. kWh per year. | `NumUnits * UnitkWh1stBaseline * NTGRkWh * InstallationRatekWh * RealizationRatekWh` |
| `AC_Net_kWh_2nd_Baseline` | The annual kWh the electric benefit for the 2nd baseline is priced on. kWh per year. | `NumUnits * UnitkWh2ndBaseline * NTGRkWh * InstallationRatekWh * RealizationRatekWh` |
| `AC_Net_Therms_1st_Baseline` | The annual Therms the gas benefit for the 1st baseline is priced on. Therms per year. | `NumUnits * UnitTherm1stBaseline * NTGRTherm * InstallationRateTherm * RealizationRateTherm` |
| `AC_Net_Therms_2nd_Baseline` | The annual Therms the gas benefit for the 2nd baseline is priced on. Therms per year. | `NumUnits * UnitTherm2ndBaseline * NTGRTherm * InstallationRateTherm * RealizationRateTherm` |
| `AC_Net_kWh_Water_1st_Baseline` | The annual kWh the water savings electric benefit for the 1st baseline is priced on. kWh per year. | `NumUnits * UnitkWhIOUWater1stBaseline * NTGRkWh * InstallationRatekWh * RealizationRatekWh` |
| `AC_Net_kWh_Water_2nd_Baseline` | The annual kWh the water savings electric benefit for the 2nd baseline is priced on. kWh per year. | `NumUnits * UnitkWhIOUWater2ndBaseline * NTGRkWh * InstallationRatekWh * RealizationRatekWh` |
| `Fuel_Sub_Flag` | Whether the measure is treated as fuel substitution, so its negative fuel values become supply costs. True / false. | true if `FuelID` is `AllElec-New`, `FuelSub-ToElec` or `FuelSub-ToGas`, or if `MeasImpactType` contains `FuelSub` |
| `Scaled_Profile_Flag` | Whether the measure's annual kWh per unit came from a scaled (kWh-per-unit) 8,760 profile rather than from `UnitkWh1stBaseline` / `UnitkWh2ndBaseline`. True / false. | true if a baseline is priced on a user profile with `profile_basis = kwh_per_unit` (CALCULATIONS C4.5); its `AC_Net_kWh_*_Baseline` then use the profile's annual kWh per unit in place of `UnitkWh1stBaseline` (`UnitkWh2ndBaseline`) |

Every numeric field CET requires is used as filed: a blank one stops the run (preflight `blank_required_value`). The fields CET does not require read a blank as 0:
`UnitkWhIOUWater1stBaseline`, `UnitkWhIOUWater2ndBaseline`, `UnitDirectInstallLab`, `UnitDirectInstallMat`, `UnitEndUserRebate`, `UnitIncentiveToOthers`, `UnitTaxCredits`,
`MeasInflation`.
The priced quantities are net (`NTGR...`) and after installation and realization rates, without market effects (the benefit formulas add `MEB`).

**A measure priced on an already-scaled 8,760 profile** (kWh per unit per hour, `ProfileSource = user profile (kWh per unit)`, CALCULATIONS C4.5) is the exception: its
profile carries the kWh, so its annual kWh per unit is the profile's own sum, which replaces `UnitkWh1stBaseline` above and in every kWh formula of this file (for the 2nd baseline: its own
profile's sum when `AC_Elec_Impact_Profile_2nd_Baseline` names one, else the 1st's sum times `UnitkWh2ndBaseline / UnitkWh1stBaseline`), and the electric value
multiplies the profile by `NumUnits * NTGRkWh * InstallationRatekWh * RealizationRatekWh` (0.5). A load-shifting-only profile has an annual kWh of 0.

### 1.3 Pricing status

A benefit of 0 alone cannot tell "no savings" from "could not price"; these columns say which. Each is decided by the first condition that holds, in the order listed.

| Field | Values and the rule that gives each |
|---|---|
| `ElecPricingStatus` | `no electric savings` when 1st and 2nd baseline net kWh and kW are all 0; `UNPRICED: no hourly profile` when no shape was found; `PARTLY PRICED: no hourly profile for one baseline's shape` when the measure has a separate 2nd-baseline shape and only one of its two shapes was found; `priced: hourly profile` when the value was produced; otherwise `UNPRICED: no value returned` |
| `ProfileSource` | Where the electric shape came from: `user profile` (a normalized user profile), `user profile (kWh per unit)` (a scaled user profile), `library`, `library (sector-loose match)`, or `library (user profile has no shape for this zone)`; blank when nothing was priced or there are no electric savings |
| `SystemClimateZone` | For a measure with `AC_Climate_Zone = System`: the real climate zone it was priced in, the profile's System zone; blank for every other measure and when nothing was priced |
| `SystemZoneSource` | Where `SystemClimateZone` came from: `catalog` (the shipped System-zone catalog), or `calculated (...)` with the reason (`user profile`, `supplied ACC`, `reference data differs from the catalog's`, `profile not in catalog`); `no zone could be priced: System shape and blended prices` when no zone was an option; blank otherwise |
| `GasPricingStatus` | `no gas savings` when 1st and 2nd baseline net therms are both 0; `priced` when the gas value was computed; otherwise `UNPRICED: no gas savings profile match` (`AC_Gas_Impact_Profile` names no known profile) |
| `GasProfileSource` | Where the gas monthly shape came from: `user profile` or `library`; blank when not priced |
| `WaterPricingStatus` | `no water energy` when 1st and 2nd baseline net water kWh are both 0; `priced` when the water value was computed (it needs the measure's electric profile); otherwise `UNPRICED: no hourly profile` |

### 1.4 Benefits and supply costs (TRC price rows, primary discount rate)

All are present values in dollars. `V_elec`, `V_gas` and `V_water` are the signed lifetime values of section 0.7; each column below says how that value is split.

#### `ElecBen`

Electric avoided-cost benefit. Unit: Dollars (PV at the First Year, primary discount rate).

**Formula**

```text
ElecBen = V_elec              if Fuel_Sub_Flag is false          (a negative V_elec stays negative)
ElecBen = pos(V_elec)         if Fuel_Sub_Flag is true           (the negative part is booked as ElecSupplyCost)
```

**Where** `V_elec` is the signed lifetime electric value (0.7). Written out for the hourly-profile route:

```text
V_elec = SUM over k = 0 ... n-1 of
           w(k) * DF(k)
           * phased( k ;  AC_Net_kWh_1st_Baseline * (NTGRkWh + MEB) / NTGRkWh ,
                          AC_Net_kWh_2nd_Baseline * (NTGRkWh + MEB) / NTGRkWh )
           * ( SUM over the hours i of quarter(k) of  shape(i) * total_avoided_cost(y*(k), i) / 1000 )

AC_Net_kWh_1st_Baseline = NumUnits * UnitkWh1stBaseline * NTGRkWh * InstallationRatekWh * RealizationRatekWh
AC_Net_kWh_2nd_Baseline = NumUnits * UnitkWh2ndBaseline * NTGRkWh * InstallationRatekWh * RealizationRatekWh
```

On a kWh-per-unit profile the multiplier is `AC_Net_kWh_ScaledProfile_*_Baseline` instead (0.5). `n`, `w(k)`, `year(k)`, `quarter(k)`: 0.2. `DF(k)`: 0.3. `phased`: 0.4. `shape(i)`, the ACC row and `y*(k)`: 0.6. `Fuel_Sub_Flag`: 1.2.

**Inputs:** `NumUnits`, `UnitkWh1stBaseline`, `UnitkWh2ndBaseline`, `NTGRkWh`, `InstallationRatekWh`, `RealizationRatekWh`, `EUL_Yrs`, `RUL_Yrs`, `ClaimYearQuarter`,
`IOU_AC_Territory`, `AC_Climate_Zone`, `AC_Elec_Target_Sector`, `AC_Elec_Impact_Profile`, `FuelID`, `MeasImpactType`. Settings: First Year, ACC version, market effects; the ACC's hourly prices and the 8,760 profile.

- Example A, `ElecBen`: the sum of the ten rows of table 3.2 = **5,841.50**
- Example B, `ElecBen`: `V_elec` is negative (the measure adds load), so pos(V_elec) = **0.00**

#### `ElecSupplyCost`

Electric cost booked as a supply cost: the added electric load of a fuel-substitution measure. Unit: Dollars (PV).

**Formula**

```text
ElecSupplyCost = pos(-V_elec)     if Fuel_Sub_Flag is true
ElecSupplyCost = 0                otherwise
```

**Where** `V_elec` is defined under `ElecBen`. **Inputs:** those of `ElecBen`.

- Example A, `ElecSupplyCost`: not a fuel-substitution measure = **0.00**
- Example B, `ElecSupplyCost`: V_elec = -4172.17 (sum of the ten `V_elec` terms of table 3.4), so max(-V_elec, 0) = **4,172.17**

#### `GasBen`

Gas avoided-cost benefit. Unit: Dollars (PV).

**Formula**

```text
GasBen = V_gas                if Fuel_Sub_Flag is false
GasBen = pos(V_gas)           if Fuel_Sub_Flag is true
```

**Where**

```text
V_gas = SUM over k = 0 ... n-1 of
          w(k) * DF(k)
          * phased( k ;  AC_Net_Therms_1st_Baseline * (NTGRTherm + MEB) / NTGRTherm ,
                         AC_Net_Therms_2nd_Baseline * (NTGRTherm + MEB) / NTGRTherm )
          * ( SUM over the months m of quarter(k) of  gasshape(m) * ACg(y^(k), m) )

AC_Net_Therms_1st_Baseline = NumUnits * UnitTherm1stBaseline * NTGRTherm * InstallationRateTherm * RealizationRateTherm
AC_Net_Therms_2nd_Baseline = NumUnits * UnitTherm2ndBaseline * NTGRTherm * InstallationRateTherm * RealizationRateTherm
ACg(y, m) = market_m + td_m + nox + co2 + upstream_methane + air_quality_adder  [ + behind_meter_methane ]
```

`gasshape(m)` is the monthly share of the measure's `AC_Gas_Impact_Profile`; `y^(k)` and the gas ACC row are in 0.6. A measure is gas-priced only if it has gas savings and its profile name matches.
**Inputs:** `NumUnits`, `UnitTherm1stBaseline`, `UnitTherm2ndBaseline`, `NTGRTherm`, `InstallationRateTherm`, `RealizationRateTherm`, `EUL_Yrs`, `RUL_Yrs`, `ClaimYearQuarter`,
`IOU_AC_Territory`, `AC_Gas_Target_Sector`, `AC_Gas_Impact_Profile`, `FuelID`, `MeasImpactType`. Settings: First Year, ACC version, market effects; the gas ACC and the gas profile.

- Example A, `GasBen`: the sum of the ten rows of table 3.2 = **3,454.03**
- Example B, `GasBen`: the sum of the ten rows of table 3.4 (`AC_Gas_Impact_Profile` is `Winter Only`, so only quarters containing Jan-Mar and Oct-Dec carry value) = **6,990.56**

#### `GasSupplyCost`

Gas cost booked as a supply cost: the added gas use of a fuel-substitution measure. Unit: Dollars (PV).

**Formula**

```text
GasSupplyCost = pos(-V_gas)       if Fuel_Sub_Flag is true
GasSupplyCost = 0                 otherwise
```

**Where** `V_gas` is defined under `GasBen`. **Inputs:** those of `GasBen`.

- Example A, `GasSupplyCost`: not a fuel-substitution measure = **0.00**
- Example B, `GasSupplyCost`: V_gas is positive (the measure saves gas), so max(-V_gas, 0) = **0.00**

#### `WaterEnergyBen`

Value of the electricity embedded in the water the measure saves. It is priced on the measure's own electric profile, on the generation part of the price only. Unit: Dollars (PV).

**Formula**

```text
WaterEnergyBen = V_water     if UnitkWhIOUWater1stBaseline > 0
WaterEnergyBen = 0           otherwise
```

**Where**

```text
V_water = SUM over k = 0 ... n-1 of
            w(k) * DF(k)
            * phased( k ;  AC_Net_kWh_Water_1st_Baseline * (NTGRkWh + MEB) / NTGRkWh ,
                           AC_Net_kWh_Water_2nd_Baseline * (NTGRkWh + MEB) / NTGRkWh )
            * ( SUM over the hours i of quarter(k) of  shape(i) * Gen(y*(k), i) )

AC_Net_kWh_Water_1st_Baseline = NumUnits * UnitkWhIOUWater1stBaseline * NTGRkWh * InstallationRatekWh * RealizationRatekWh
AC_Net_kWh_Water_2nd_Baseline = NumUnits * UnitkWhIOUWater2ndBaseline * NTGRkWh * InstallationRatekWh * RealizationRatekWh
Gen(y, i) = ( total_avoided_cost - transmission - distribution )(y, i) / 1000
```

The sign of the *first-baseline* water kWh decides the column, not the sign of the value. A water measure with no electric profile cannot be priced (`WaterPricingStatus` = `UNPRICED ...`). **Inputs:** `NumUnits`, `UnitkWhIOUWater1stBaseline`,
`UnitkWhIOUWater2ndBaseline`, `NTGRkWh`, `InstallationRatekWh`, `RealizationRatekWh`, `EUL_Yrs`, `RUL_Yrs`, `ClaimYearQuarter`, and the electric-profile fields of `ElecBen`.

- Example A, `WaterEnergyBen`: `UnitkWhIOUWater1stBaseline` = 3 > 0, so the sum of the ten `V_water` terms of table 3.2 = **30.67**
- Example B, `WaterEnergyBen`: `UnitkWhIOUWater1stBaseline` = -6 < 0 = **0.00**

#### `WaterEnergyCost`

Value of the electricity embedded in water the measure uses. Unit: Dollars (PV).

**Formula**

```text
WaterEnergyCost = -V_water     if UnitkWhIOUWater1stBaseline < 0
WaterEnergyCost = 0            otherwise
```

**Where** `V_water` is defined under `WaterEnergyBen`. **Inputs:** those of `WaterEnergyBen`.

- Example A, `WaterEnergyCost`: `UnitkWhIOUWater1stBaseline` = 3 (not below 0) = **0.00**
- Example B, `WaterEnergyCost`: `UnitkWhIOUWater1stBaseline` = -6, and the sum of the ten `V_water` terms of table 3.4 is -5.63, so -V_water = **5.63**

#### `OtherBen`, `OtherCost`, `TaxCredits`

- `OtherBen`: Net Other benefits (includes gas infrastructure, refrigerant, and miscellaneous benefits). Unit: Dollars (undiscounted).
- `OtherCost`: Net Other costs (includes refrigerant and miscellaneous costs). Unit: Dollars (undiscounted).
- `TaxCredits`: Net Total Tax Credits used in the cost effectiveness calculations where applicable. Unit: Dollars (undiscounted).

**Formula**

```text
OtherBen   = NumUnits * (NTGRkWh + MEB) * (UnitGasInfraBens + UnitRefrigBens + UnitMiscBens)
OtherCost  = NumUnits * (NTGRkWh + MEC) * (UnitRefrigCosts + UnitMiscCosts)
TaxCredits = NumUnits * (NTGRkWh + MEB) * UnitTaxCredits
```

**Inputs:** `NumUnits`, `NTGRkWh`, `UnitGasInfraBens`, `UnitRefrigBens`, `UnitMiscBens`, `UnitRefrigCosts`, `UnitMiscCosts`, `UnitTaxCredits` (`UnitTaxCredits` alone reads a blank as 0); the market-effects settings.

- Example A, `OtherBen`: 100 * (0.8 + 0) * (0 + 0 + 2) = **160.00**
- Example A, `OtherCost`: 100 * (0.8 + 0) * (1 + 0) = **80.00**
- Example A, `TaxCredits`: 100 * (0.8 + 0) * 5 = **400.00**
- Example B, `OtherBen`: 10 * (0.75 + 0.05) * (20 + 8 + 12) = **320.00**
- Example B, `OtherCost`: 10 * (0.75 + 0.05) * (10 + 5) = **120.00**
- Example B, `TaxCredits`: 10 * (0.75 + 0.05) * 100 = **800.00**

#### `OtherBenGross`, `OtherCostGross`, `TaxCreditsGross`

- `OtherBenGross`: Gross Other benefits (includes gas infrastructure, refrigerant, and miscellaneous benefits). Unit: Dollars (undiscounted).
- `OtherCostGross`: Gross Other costs (includes refrigerant and miscellaneous costs). Unit: Dollars (undiscounted).
- `TaxCreditsGross`: Gross Total Tax Credits used in the cost effectiveness calculations where applicable. Unit: Dollars (undiscounted).

**Formula**

```text
OtherBenGross   = NumUnits * (UnitGasInfraBens + UnitRefrigBens + UnitMiscBens)
OtherCostGross  = NumUnits * (UnitRefrigCosts + UnitMiscCosts)
TaxCreditsGross = NumUnits * UnitTaxCredits
```

**Inputs:** `NumUnits` and the unit fields of the entry above.

- Example A, `OtherBenGross`: 100 * (0 + 0 + 2) = **200.00**
- Example B, `OtherBenGross`: 10 * (20 + 8 + 12) = **400.00**
- Example A, `OtherCostGross`: 100 * (1 + 0) = **100.00**
- Example B, `OtherCostGross`: 10 * (10 + 5) = **150.00**
- Example A, `TaxCreditsGross`: 100 * 5 = **500.00**
- Example B, `TaxCreditsGross`: 10 * 100 = **1,000.00**

### 1.5 Participant costs and bill impacts (primary rate)

One-time cash flows at the claim quarter, discounted once with `PVF` (0.3); the primary discount rate here, the societal one in 1.6.

#### `RebatesandIncents`

Incentives, direct-install costs and rebates paid to or for participants, in nominal dollars (not discounted). Part of `TotalExpenditures`. Unit: Dollars (nominal).

**Formula**

```text
RebatesandIncents = IncentsDI + Rebates          IncentsDI, Rebates: see RebatesandIncentsPV
```

Matches CET's own `RebatesandIncents` column to the cent on CET jobs 121219, 121841 and 121842. **Inputs:** `NumUnits`, `UnitIncentiveToOthers`, `UnitDirectInstallLab`,
`UnitDirectInstallMat`, `UnitEndUserRebate`.

- Example A, `RebatesandIncents`: 4000 + 2000 = **6,000.00** (100 * (30 + 5 + 5) and 100 * 20)
- Example B, `RebatesandIncents`: 9500 + 4000 = **13,500.00** (10 * (800 + 100 + 50) and 10 * 400)

#### `RebatesandIncentsPV`

Incentives, direct-install costs and rebates paid to or for participants, present-valued at the claim quarter. Unit: Dollars (PV).

**Formula**

```text
RebatesandIncentsPV = ( IncentsDI + Rebates ) / PVF

IncentsDI = NumUnits * ( UnitIncentiveToOthers + UnitDirectInstallLab + UnitDirectInstallMat )
Rebates   = NumUnits * UnitEndUserRebate
PVF       = Rqf ^ Qm          Qm = (Y0 - F) * 4 + (q0 - 1)          Rqf = 1 + DiscountRateQtr(PA)          (section 0.3)
```

**Inputs:** `NumUnits`, `UnitIncentiveToOthers`, `UnitDirectInstallLab`, `UnitDirectInstallMat`, `UnitEndUserRebate`, `ClaimYearQuarter`, `IOU_AC_Territory`; First Year, ACC version (E3 discount rates).

- Example A, `RebatesandIncentsPV`: (4000 + 2000) / 1.01915^2 = **5,776.64**
- Example B, `RebatesandIncentsPV`: (9500 + 4000) / 1.01915^1 = **13,246.33**

#### `GrossParticipantCostPV`

Participant cost before net-to-gross: the measure cost less the incentives and rebates. Unit: Dollars (PV).

**Formula**

```text
GrossParticipantCostPV = GrossMeasCostPV - ( IncentsDI + Rebates ) / PVF

GrossMeasCostPV = NumUnits * ( UnitMeaCost1stBaseline - PVsub ) / PVF
PVsub = 0                                                                                    if RUL_Yrs = 0 (single baseline)
PVsub = ( UnitMeaCost1stBaseline - UnitMeaCost2ndBaseline )
        * ( (1 + MeasInflation / 4) / Rqf ) ^ ( 4 * RUL_Yrs )                                if RUL_Yrs > 0 (early replacement)
```

For an early replacement (`RUL_Yrs` > 0) only the *incremental* cost counts: the normal-replacement cost of the 2nd baseline, brought forward to the claim at the inflation-adjusted quarterly rate,
is taken off. `IncentsDI`, `Rebates`, `PVF`: see `RebatesandIncentsPV`. **Inputs:** those of `RebatesandIncentsPV`, plus `UnitMeaCost1stBaseline`, `UnitMeaCost2ndBaseline`, `MeasInflation`, `RUL_Yrs`.

- Example A, `GrossParticipantCostPV`: 100 * 200 / 1.038667 - (4000 + 2000) / 1.038667 = **13,478.82** (`RUL_Yrs` = 0, so `PVsub` = 0)
- Example B, `GrossParticipantCostPV`: 10 * (9000 - 2817.0661) / 1.01915 - (9500 + 4000) / 1.01915 = **47,421.22** (`RUL_Yrs` > 0, so `PVsub` is needed; it is worked in the next line)
- Check, `PVsub` of example B: (9000 - 6000) * ((1 + 0.02 / 4) / 1.01915)^4.5 = **2,817.0661**

#### `NetParticipantCostPV`

Participant cost after net-to-gross, plus the excess-incentive term; without the market-effects term (that is `NetParticipantCostAdjustedPV`), as CET exports it. Unit: Dollars (PV).

**Formula**

```text
NetParticipantCostPV = NTGRCost * ( GrossParticipantCostPV + ExcessIncentivesPV )

ExcessIncentivesPV = pos( IncentsDI / PVF - GrossMeasCostPV )
```

`ExcessIncentivesPV` is the incentives paid beyond the measure cost (end-user rebates are not part of this test). `GrossParticipantCostPV`, `GrossMeasCostPV`, `IncentsDI`, `PVF`: see the two entries above.
Matches CET's own `NetParticipantCostPV` column to the cent with market effects on and off (CET jobs 121842 / 121841).
**Inputs:** those of `GrossParticipantCostPV`, plus `NTGRCost`.

- Example A, `NetParticipantCostPV`: 0.9 * (13478.8183 + 0.0000) = **12,130.94**
- Example B, `NetParticipantCostPV`: 0.8 * (47421.2222 + 0.0000) = **37,936.98**
  (`ExcessIncentivesPV` = pos(9500 / 1.01915 - 60667.55) = 0)

#### `MarkEffectPlusExcessIncPV`

The market-effects term on the participant's cost (CET's `MECost` term). Unit: Dollars (PV).

**Formula**

```text
MarkEffectPlusExcessIncPV = MEC * ( GrossMeasCostPV + ExcessIncentivesPV )
```

`MEC` is 0.05 with "Apply 5% market effects (benefits and costs)", else 0 (so this column is then 0). `GrossMeasCostPV`, `ExcessIncentivesPV`: see the two entries above.
**Inputs:** those of `GrossParticipantCostPV`; the market-effects setting.

- Example A, `MarkEffectPlusExcessIncPV`: 0 * (19255.4547 + 0.0000) = **0.00**
- Example B, `MarkEffectPlusExcessIncPV`: 0.05 * (60667.5550 + 0.0000) = **3,033.38**

#### `NetParticipantCostAdjustedPV`

The participant cost the TRC cost uses: `NetParticipantCostPV` plus the market-effects term (CET's column of the same name; it is what CET's own TRC calculation adds). Unit: Dollars (PV).

**Formula**

```text
NetParticipantCostAdjustedPV = NetParticipantCostPV + MarkEffectPlusExcessIncPV
```

**Inputs:** those of the two columns it adds.

- Example A, `NetParticipantCostAdjustedPV`: 12130.9364 + 0.0000 = **12,130.94**
- Example B, `NetParticipantCostAdjustedPV`: 37936.9778 + 3033.3778 = **40,970.36**

#### `IncentiveToOthers`, `DILaborCost`, `DIMaterialCost`, `EndUserRebate`

- `IncentiveToOthers`: The amount of incentives paid to manufacturers or distributors to reduce the cost of equipment to customers. Unit: Dollars (nominal).
- `DILaborCost`: The amount of direct install labor paid for the measure. Unit: Dollars (nominal).
- `DIMaterialCost`: The amount of direct install material cost paid for the measure. Unit: Dollars (nominal).
- `EndUserRebate`: The amount of rebate or incentive funds paid directly to the customer or towards the customers costs for the measure. Unit: Dollars (nominal).

**Formula**

```text
IncentiveToOthers = NumUnits * UnitIncentiveToOthers
DILaborCost       = NumUnits * UnitDirectInstallLab
DIMaterialCost    = NumUnits * UnitDirectInstallMat
EndUserRebate     = NumUnits * UnitEndUserRebate
```

CET's columns of the same names (CalcCost); they match CET to the cent on CET jobs 121219-121222, 121842 and 121841. **Inputs:** `NumUnits` and the four unit fields.

- Example A, `IncentiveToOthers`: 100 * 30 = **3,000.00**
- Example A, `EndUserRebate`: 100 * 20 = **2,000.00**
- Example B, `DILaborCost`: 10 * 100 = **1,000.00**
- Example B, `DIMaterialCost`: 10 * 50 = **500.00**

#### `GrossMeasureCost`, `GrossMeasCostPV`

- `GrossMeasureCost`: The gross incremental cost of the measure. Unit: Dollars (nominal).
- `GrossMeasCostPV`: Present value of the gross incremental cost of the measure. Unit: Dollars (PV).

**Formula**

```text
GrossMeasureCost = NumUnits * UnitMeaCost1stBaseline                    if RUL_Yrs = 0
                 = NumUnits * UnitMeaCost2ndBaseline                    if RUL_Yrs > 0
GrossMeasCostPV  = NumUnits * ( UnitMeaCost1stBaseline - PVsub ) / PVF
```

`PVsub` and `PVF`: see `GrossParticipantCostPV` and `RebatesandIncentsPV`. For an early replacement the two are built differently, as in CET: the nominal column is the 2nd-baseline cost, the PV
column the 1st-baseline cost less the 1st-to-2nd difference brought forward at the inflation-adjusted rate. Both match CET to the cent on the jobs above. **Inputs:** `NumUnits`,
`UnitMeaCost1stBaseline`, `UnitMeaCost2ndBaseline`, `MeasInflation`, `RUL_Yrs`, `ClaimYearQuarter`, `IOU_AC_Territory`; First Year, ACC version (discount rates).

- Example A, `GrossMeasureCost`: 100 * 200 = **20,000.00** (`RUL_Yrs` = 0)
- Example A, `GrossMeasCostPV`: 100 * 200 / 1.01915^2 = **19,255.45**
- Example B, `GrossMeasureCost`: 10 * 6000 = **60,000.00** (`RUL_Yrs` > 0: the 2nd-baseline cost)
- Example B, `GrossMeasCostPV`: 10 * (9000 - 2817.0661) / 1.01915 = **60,667.55** (`PVsub` as worked under `GrossParticipantCostPV`)

#### `ExcessIncentives`, `ExcessIncentivesPV`

- `ExcessIncentives`: Excess incentives paid for direct install or midstream/upstream over the cost of the measures. Unit: Dollars (nominal).
- `ExcessIncentivesPV`: Present value of excess incentives paid for direct install or midstream/upstream over the cost of the measures. Unit: Dollars (PV).

**Formula**

```text
ExcessIncentives   = pos( IncentsDI - GrossMeasureCost )
ExcessIncentivesPV = pos( IncentsDI / PVF - GrossMeasCostPV )
```

`IncentsDI`, `PVF`: see `RebatesandIncentsPV`. **Inputs:** those of `RebatesandIncentsPV` and `GrossMeasureCost`.

- Example A, `ExcessIncentives`: max(4000 - 20000, 0) = **0.00**
- Example B, `ExcessIncentivesPV`: max(9500 / 1.01915 - 60667.5550, 0) = **0.00**

#### `MarkEffectPlusExcessInc`

The market-effects term on the participant's cost, nominal. Unit: Dollars.

**Formula**

```text
MarkEffectPlusExcessInc = MEC * ( GrossMeasureCost + ExcessIncentivesPV )
```

CET adds the *present-valued* excess incentives to the *nominal* measure cost here (CalcCost's `GrossMeasureCostAdjusted` step); ReCET does the same, and the column matches CET's on job
121841 (5% market effects). It is 0 without market effects. **Inputs:** those of `GrossMeasureCost` and `ExcessIncentivesPV`; the market-effects setting.

- Example A, `MarkEffectPlusExcessInc`: 0 * (20000 + 0) = **0.00**
- Example B, `MarkEffectPlusExcessInc`: 0.05 * (60000 + 0) = **3,000.00**

#### `GrossParticipantCost`, `GrossParticipantCostAdjusted`, `GrossParticipantCostAdjustedPV`

- `GrossParticipantCost`: The gross cost to the participant after incentives. Unit: Dollars (nominal).
- `GrossParticipantCostAdjusted`: The gross cost to the participant after incentives, adjusted for market effects and excess incentives. Unit: Dollars (nominal).
- `GrossParticipantCostAdjustedPV`: The present value of the gross cost to the participant after incentives, adjusted for market effects and excess incentives. Unit: Dollars (PV).

**Formula**

```text
GrossParticipantCost           = GrossMeasureCost - ( IncentsDI + Rebates )
GrossParticipantCostAdjusted   = GrossParticipantCost + ExcessIncentives + MarkEffectPlusExcessInc
GrossParticipantCostAdjustedPV = GrossParticipantCostPV + ExcessIncentivesPV + MarkEffectPlusExcessIncPV
```

**Inputs:** those of the columns they add.

- Example A, `GrossParticipantCost`: 20000 - (4000 + 2000) = **14,000.00**
- Example A, `GrossParticipantCostAdjustedPV`: 13478.8183 + 0 + 0 = **13,478.82**
- Example B, `GrossParticipantCost`: 60000 - (9500 + 4000) = **46,500.00**
- Example B, `GrossParticipantCostAdjusted`: 46500 + 0 + 3000 = **49,500.00**
- Example B, `GrossParticipantCostAdjustedPV`: 47421.2222 + 0 + 3033.3777 = **50,454.60**

#### `NetParticipantCost`, `NetParticipantCostAdjusted`

- `NetParticipantCost`: The net participant cost when accounting for free-ridership, without market effects or excess incentives. Unit: Dollars.
- `NetParticipantCostAdjusted`: The net participant cost when accounting for free-ridership, with market effects and excess incentives. Unit: Dollars.

**Formula**

```text
NetParticipantCost         = NTGRCost * ( GrossParticipantCost + ExcessIncentives )
NetParticipantCostAdjusted = NetParticipantCost + MarkEffectPlusExcessInc
```

**Inputs:** those of `GrossParticipantCost`, plus `NTGRCost`.

- Example A, `NetParticipantCost`: 0.9 * (14000 + 0) = **12,600.00**
- Example B, `NetParticipantCost`: 0.8 * (46500 + 0) = **37,200.00**
- Example B, `NetParticipantCostAdjusted`: 37200 + 3000 = **40,200.00**

#### `LifecycleBillReducElec`, `LifecycleBillIncrElec`, `LifecycleBillReducGas`, `LifecycleBillIncrGas`

The customer's bill change, the ratepayer cost in the RIM test: the present value of the bill reduction (...Reduc...) or, for a fuel-substitution measure that adds load, the bill increase (...Incr...). Unlike section 1.4 this is annual and uses CET's retail rate schedules; like the benefits it uses net-to-gross plus market effects (NTG + MEB). The measure-level `RateScheduleElec` and `RateScheduleGas` columns are not read. Unit: Dollars (PV, annual discounting).

**Formula**

```text
PV_elec = PV( fe * AC_Net_kWh_1st_Baseline ,     fe * AC_Net_kWh_2nd_Baseline ,     RateE )        fe = ( NTGRkWh + MEB ) / NTGRkWh
PV_gas  = PV( ft * AC_Net_Therms_1st_Baseline ,  ft * AC_Net_Therms_2nd_Baseline ,  RateG )        ft = ( NTGRTherm + MEB ) / NTGRTherm
   (fe, ft = 1 when MEB = 0 or the NTG is 0: the quantities of 1.2 already carry NTG, this swaps it for NTG + MEB, as CET's bill calculation does)

LifecycleBillReducElec = PV_elec      LifecycleBillIncrElec = 0               if Fuel_Sub_Flag is false
LifecycleBillReducElec = pos(PV_elec) LifecycleBillIncrElec = pos(-PV_elec)   if Fuel_Sub_Flag is true
LifecycleBillReducGas, LifecycleBillIncrGas: the same with PV_gas
```

**Where** `PV(s1, s2, rate)` is the annual stream, with `E = EUL_Yrs`, `R = RUL_Yrs`, `full = floor(E)`, `rem = E - full`, the number of billed years `ny = full + 1` if `rem > 0.000000001` else `full`,
`yr1 = Y0 - BaseYear` (the run parameters' `BaseYear` of the ACC version: 2021, 2022, or 2024 for the 2024 and 2026 ACCs), `Raf = 1 + DiscountRateAnnual(PA)`:

```text
floor_rul = floor(R) if R > 0 else full
b         = R - floor(R) if R > 0 else 1              the share of the boundary year on the 1st baseline

PV(s1, s2, rate) =   SUM over j = 0 ... min(floor_rul, ny) - 1 of        s1 * rate(Y0 + j) / Raf ^ j
                   + SUM over j = floor_rul ... ny - 1 of                wt(j) * v(j) * rate(Y0 + j) / Raf ^ (j + yr1)
    wt(j) = rem  if j = ny - 1 and rem > 0.000000001, else 1
    v(j)  = b * s1 + (1 - b) * s2    if j = floor_rul,  else s2
```

`RateE` is the retail electric rate ($ per kWh, by calendar year) of the schedule that (`PA`, `AC_Elec_Target_Sector`) maps to and `RateG` the gas rate ($ per therm) for (`PA`, `AC_Gas_Target_Sector`), both from CET's rate
tables. The second sum's exponent `j + yr1` (years since `BaseYear`, not since the claim) reproduces what CET's own calculation does for the boundary year and every 2nd-baseline year, so it is kept.
A measure whose sector has no schedule mapping, or a year outside the schedule's range, gets no bill impact for that fuel.
**Inputs:** the fields of `AC_Net_kWh_1st_Baseline` / `AC_Net_Therms_1st_Baseline` (0.5), `EUL_Yrs`, `RUL_Yrs`, `ClaimYearQuarter`, `IOU_AC_Territory`, `AC_Elec_Target_Sector`, `AC_Gas_Target_Sector`, `FuelID`, `MeasImpactType`; ACC version (rate tables, `BaseYear` and discount rate); the market-effects setting.
Scaling by market effects matches CET exactly (every `BillReduc` of CET job 121841 is (NTG + 0.05) / NTG times job 121842's).

Example A (single baseline, `EUL_Yrs` 2.4: `ny` = 3, `floor_rul` = 2, `yr1` = 4, so the last, partial year has `wt` = 0.4 and is discounted by `Raf^6`). `RateE` for 2028 / 2029 / 2030 is
0.406249518 / 0.418437003 / 0.430990113 $ per kWh and `RateG` is 2.815469667 / 2.899933757 / 2.986931770 $ per therm:

- Example A, `LifecycleBillReducElec`: 34200 * 0.406249518 + 34200 * 0.418437003 / 1.0766^1 + 0.4 * 34200 * 0.430990113 / 1.0766^6 = **30,972.49**
- Example A, `LifecycleBillIncrElec`: not a fuel-substitution measure, so 0 = **0.00**
- Example A, `LifecycleBillReducGas`: 1620 * 2.815469667 + 1620 * 2.899933757 / 1.0766^1 + 0.4 * 1620 * 2.986931770 / 1.0766^6 = **10,167.71**
- Example A, `LifecycleBillIncrGas`: not a fuel-substitution measure, so 0 = **0.00**

Example B (`EUL_Yrs` 2.5, `RUL_Yrs` 1.125: `ny` = 3, `floor_rul` = 1, `b` = 0.125 so the boundary year 2029 is 12.5% 1st baseline and 87.5% 2nd baseline, `wt` = 0.5 for 2030, `yr1` = 4). `RateE` for 2028 / 2029 / 2030
is 0.426280328 / 0.439068738 / 0.452240800 and `RateG` is 1.485816933 / 1.530391441 / 1.576303184. Example B has 5% market effects, so the streams are scaled by `fe` and `ft`. The
measure adds electric load, so `PV_elec` is negative:

- Check, electric streams of example B: -27000 * (0.75 + 0.05) / 0.75 = **-28,800** (1st baseline; the 2nd, -16875, becomes -18,000)
- Check, gas streams of example B: 4845 * (0.85 + 0.05) / 0.85 = **5,130** (1st baseline; the 2nd, 3230, becomes 3,420)
- Check, `PV_elec` of example B: -28800 * 0.426280328 + (0.125 * -28800 + 0.875 * -18000) * 0.439068738 / 1.0766^5 + 0.5 * -18000 * 0.452240800 / 1.0766^6 = **-20,764.86**
- Example B, `LifecycleBillReducElec`: max(0, PV_elec) with PV_elec = -20764.86, so 0 = **0.00**
- Example B, `LifecycleBillIncrElec`: max(0, -PV_elec) = **20,764.86**
- Check, `PV_gas` of example B: 5130 * 1.485816933 + (0.125 * 5130 + 0.875 * 3420) * 1.530391441 / 1.0766^5 + 0.5 * 3420 * 1.576303184 / 1.0766^6 = **13,198.20**
- Example B, `LifecycleBillReducGas`: max(0, PV_gas) with PV_gas = 13198.20 = **13,198.20**
- Example B, `LifecycleBillIncrGas`: max(0, -PV_gas), and PV_gas is positive, so 0 = **0.00**

#### `LifecycleBillReducElecGross`, `LifecycleBillIncrElecGross`, `LifecycleBillReducGasGross`, `LifecycleBillIncrGasGross`

The participant's own bill change for the Participant Cost Test (1.10): the bill impacts above on gross savings, without net-to-gross and without market effects (free riders are participants too). The CET has no such columns. Unit: Dollars (PV, annual discounting).

**Formula**

```text
PV_elec_gross = PV( NumUnits * UnitkWh1stBaseline * IR * RR ,  NumUnits * UnitkWh2ndBaseline * IR * RR ,  RateE )      IR, RR: InstallationRatekWh, RealizationRatekWh
PV_gas_gross  = PV( NumUnits * UnitTherm1stBaseline * IRt * RRt ,  NumUnits * UnitTherm2ndBaseline * IRt * RRt ,  RateG )   IRt, RRt: the therm rates
the Reduc / Incr split and PV(s1, s2, rate): exactly as for LifecycleBillReducElec ... LifecycleBillIncrGas above
```

(On a kWh-per-unit profile the profile's own annual kWh per unit replaces `UnitkWh1stBaseline` / `UnitkWh2ndBaseline`: 0.5.)

**Inputs:** as for `LifecycleBillReducElec` ... `LifecycleBillIncrGas`, without the NTG columns and the market-effects setting.

The gross streams are the net streams of the entry above divided by `NTG + MEB` (example A: 0.8 electric, 0.9 gas; example B: 0.75 + 0.05 and 0.85 + 0.05):

- Example A, `LifecycleBillReducElecGross`: 42750 * 0.406249518 + 42750 * 0.418437003 / 1.0766^1 + 0.4 * 42750 * 0.430990113 / 1.0766^6 = **38,715.61**
- Example A, `LifecycleBillIncrElecGross`: not a fuel-substitution measure, so 0 = **0.00**
- Example A, `LifecycleBillReducGasGross`: 1800 * 2.815469667 + 1800 * 2.899933757 / 1.0766^1 + 0.4 * 1800 * 2.986931770 / 1.0766^6 = **11,297.45**
- Example A, `LifecycleBillIncrGasGross`: not a fuel-substitution measure, so 0 = **0.00**
- Check, `PV_elec_gross` of example B: -36000 * 0.426280328 + (0.125 * -36000 + 0.875 * -22500) * 0.439068738 / 1.0766^5 + 0.5 * -22500 * 0.452240800 / 1.0766^6 = **-25,956.07**
- Example B, `LifecycleBillReducElecGross`: max(0, PV_elec_gross) with PV_elec_gross = -25956.07, so 0 = **0.00**
- Example B, `LifecycleBillIncrElecGross`: max(0, -PV_elec_gross) = **25,956.07**
- Example B, `LifecycleBillReducGasGross`: 5700 * 1.485816933 + (0.125 * 5700 + 0.875 * 3800) * 1.530391441 / 1.0766^5 + 0.5 * 3800 * 1.576303184 / 1.0766^6 = **14,664.66**
- Example B, `LifecycleBillIncrGasGross`: max(0, -PV_gas_gross), and PV_gas_gross is positive, so 0 = **0.00**

### 1.6 Societal (SB = Base, SH = High) and societal-rate columns

Blank when "Include societal cost tests" is off. These are the columns of 1.4 and 1.5 repeated for the Societal Cost tests. Two things change, and only these two: the **price rows** are
`SCT Base` (columns ending `_SB`) or `SCT High` (`_SH`) instead of `TRC` (for gas, the `SCT` rows with `scc` = `Base` or `High`), and the **discount factor** is the societal one,
`DF_sct(k) = 1 / Rqf_sct ^ Qac(k)` with `Rqf_sct = 1 + DiscountRateQtr_SCT(PA)` (0.3). Quantities, phasing, market effects and the benefit / supply-cost split are exactly as in 1.4.

#### `ElecBen_SB`, `ElecBen_SH`, `ElecSupplyCost_SB`, `ElecSupplyCost_SH`

- `ElecBen_SB`: Electric Benefits using the Societal Cost Test Base Calculation Method. Unit: Dollars (PV at the societal rate).
- `ElecBen_SH`: Electric Benefits using the Societal Cost Test High Calculation Method. Unit: Dollars (PV at the societal rate).
- `ElecSupplyCost_SB`: Electric Supply Cost using the Societal Cost Test Base Calculation Method. Unit: Dollars (PV at the societal rate).
- `ElecSupplyCost_SH`: Electric Supply Cost using the Societal Cost Test High Calculation Method. Unit: Dollars (PV at the societal rate).

**Formula**

```text
ElecBen_SB        = V_elec_SB           if Fuel_Sub_Flag is false        ElecBen_SB        = pos(V_elec_SB)     if it is true
ElecSupplyCost_SB = 0                   if Fuel_Sub_Flag is false        ElecSupplyCost_SB = pos(-V_elec_SB)    if it is true
(ElecBen_SH, ElecSupplyCost_SH: the same with V_elec_SH)

V_elec_SB = SUM over k = 0 ... n-1 of
              w(k) * DF_sct(k)
              * phased( k ;  AC_Net_kWh_1st_Baseline * (NTGRkWh + MEB) / NTGRkWh ,  AC_Net_kWh_2nd_Baseline * (NTGRkWh + MEB) / NTGRkWh )
              * ( SUM over the hours i of quarter(k) of  shape(i) * total_avoided_cost[SCT Base](y*(k), i) / 1000 )
V_elec_SH: the same with the SCT High row
```

For TOU end uses these differ from CET's by design: CET's TOU societal values keep the cap-and-trade carbon component alongside the social cost of carbon (counting carbon twice)
and carry its TOU T&D error; ReCET prices them on the ACC's own SCT rows like every other end use (CALCULATIONS.md C17, deliberate difference 8).
**Inputs:** those of `ElecBen`; the ACC's `SCT Base` / `SCT High` rows; `DiscountRateQtr_SCT`.

- Example A, `ElecBen_SB`: the sum of the ten `V_elec_SB` terms of table 3.6 = **6,688.34**
- Example A, `ElecBen_SH`: the sum of the ten `V_elec_SH` terms of table 3.6 = **7,296.37**
- Example B, `ElecSupplyCost_SB`: -(the sum of the ten `V_elec_SB` terms of table 3.7), the measure adding load = **4,734.01**
- Example B, `ElecSupplyCost_SH`: -(the sum of the ten `V_elec_SH` terms of table 3.7) = **5,164.37**
- Example A, `ElecSupplyCost_SB`: not a fuel-substitution measure = **0.00**
- Example A, `ElecSupplyCost_SH`: not a fuel-substitution measure = **0.00**
- Example B, `ElecBen_SB`: V_elec_SB is negative, so pos(V_elec_SB) = **0.00**
- Example B, `ElecBen_SH`: V_elec_SH is negative, so pos(V_elec_SH) = **0.00**

#### `GasBen_SB`, `GasBen_SH`, `GasSupplyCost_SB`, `GasSupplyCost_SH`

- `GasBen_SB`: Gas Benefits using the Societal Cost Test Base Calculation Method. Unit: Dollars (PV at the societal rate).
- `GasBen_SH`: Gas Benefits using the Societal Cost Test High Calculation Method. Unit: Dollars (PV at the societal rate).
- `GasSupplyCost_SB`: Gas Supply Cost using the Societal Cost Test Base Calculation Method. Unit: Dollars (PV at the societal rate).
- `GasSupplyCost_SH`: Gas Supply Cost using the Societal Cost Test High Calculation Method. Unit: Dollars (PV at the societal rate).

**Formula**

```text
GasBen_SB        = V_gas_SB           if Fuel_Sub_Flag is false        GasBen_SB        = pos(V_gas_SB)     if it is true
GasSupplyCost_SB = 0                  if Fuel_Sub_Flag is false        GasSupplyCost_SB = pos(-V_gas_SB)    if it is true
(GasBen_SH, GasSupplyCost_SH: the same with V_gas_SH)

V_gas_SB = SUM over k = 0 ... n-1 of
             w(k) * DF_sct(k)
             * phased( k ;  AC_Net_Therms_1st_Baseline * (NTGRTherm + MEB) / NTGRTherm ,
                            AC_Net_Therms_2nd_Baseline * (NTGRTherm + MEB) / NTGRTherm )
             * ( SUM over the months m of quarter(k) of  gasshape(m) * ACg[SCT, scc = Base](y^(k), m) )
V_gas_SH: the same with the SCT rows with scc = High
```

**Inputs:** those of `GasBen`; the gas ACC's `SCT` rows; `DiscountRateQtr_SCT`.

- Example A, `GasBen_SB`: the sum of the ten `V_gas_SB` terms of table 3.6 = **4,314.29**
- Example A, `GasBen_SH`: the sum of the ten `V_gas_SH` terms of table 3.6 = **5,033.34**
- Example B, `GasBen_SB`: the sum of the ten `V_gas_SB` terms of table 3.7 = **8,827.11**
- Example B, `GasBen_SH`: the sum of the ten `V_gas_SH` terms of table 3.7 = **10,432.03**
- Example A, `GasSupplyCost_SB`: not a fuel-substitution measure = **0.00**
- Example B, `GasSupplyCost_SB`: V_gas_SB is positive, so pos(-V_gas_SB) = **0.00**

#### `WaterEnergyBen_SB`, `WaterEnergyBen_SH`, `WaterEnergyCost_SB`, `WaterEnergyCost_SH`

- `WaterEnergyBen_SB`: Water Energy Electric Benefits using the Societal Cost Test Base Calculation Method. Unit: Dollars (PV at the societal rate).
- `WaterEnergyBen_SH`: Water Energy Electric Benefits using the Societal Cost Test High Calculation Method. Unit: Dollars (PV at the societal rate).
- `WaterEnergyCost_SB`: Water Energy Electric Supply Cost using the Societal Cost Test Base Calculation Method. Unit: Dollars (PV at the societal rate).
- `WaterEnergyCost_SH`: Water Energy Electric Supply Cost using the Societal Cost Test High Calculation Method. Unit: Dollars (PV at the societal rate).

**Formula**

```text
WaterEnergyBen_SB  = V_water_SB   if UnitkWhIOUWater1stBaseline > 0, else 0
WaterEnergyCost_SB = -V_water_SB  if UnitkWhIOUWater1stBaseline < 0, else 0                (_SH the same with V_water_SH)

V_water_SB = SUM over k = 0 ... n-1 of
               w(k) * DF_sct(k)
               * phased( k ; AC_Net_kWh_Water_1st_Baseline * (NTGRkWh + MEB) / NTGRkWh , AC_Net_kWh_Water_2nd_Baseline * (NTGRkWh + MEB) / NTGRkWh )
               * ( SUM over the hours i of quarter(k) of  shape(i) * Gen[SCT Base](y*(k), i) )
Gen[SCT Base](y, i) = ( total_avoided_cost - transmission - distribution )[SCT Base](y, i) / 1000
```

**Inputs:** those of `WaterEnergyBen`; the ACC's `SCT` rows; `DiscountRateQtr_SCT`.

- Example A, `WaterEnergyBen_SB`: the sum of the ten `V_water_SB` terms of table 3.6 = **35.57**
- Example A, `WaterEnergyBen_SH`: the sum of the ten `V_water_SH` terms of table 3.6 = **39.22**
- Example B, `WaterEnergyCost_SB`: -(the sum of the ten `V_water_SB` terms of table 3.7) = **6.47**
- Example B, `WaterEnergyCost_SH`: -(the sum of the ten `V_water_SH` terms of table 3.7) = **7.13**
- Example A, `WaterEnergyCost_SB`: `UnitkWhIOUWater1stBaseline` = 3, not below 0 = **0.00**
- Example B, `WaterEnergyBen_SB`: `UnitkWhIOUWater1stBaseline` = -6, not above 0 = **0.00**

#### `RebatesandIncentsPV_SCT`, `GrossParticipantCostPV_SCT`, `NetParticipantCostPV_SCT`, `NetParticipantCostAdjustedPV_SCT`

- `RebatesandIncentsPV_SCT`: Incentives, direct-install costs and rebates paid to or for participants, present-valued at the claim quarter using the Societal discount rate. Unit: Dollars (PV at the societal rate).
- `GrossParticipantCostPV_SCT`: Participant cost before net-to-gross: the measure cost less the incentives and rebates. Discounted using the Societal discount rate. Unit: Dollars (PV at the societal rate).
- `NetParticipantCostPV_SCT`: Participant cost after net-to-gross, plus the excess-incentive term; without the market-effects term. Discounted using the Societal discount rate. Unit: Dollars (PV at the societal rate).
- `NetParticipantCostAdjustedPV_SCT`: Participant cost after net-to-gross, plus the excess-incentive term and the market-effects term. Discounted using the Societal discount rate. Unit: Dollars (PV at the societal rate).

**Formula:** exactly the formulas of `RebatesandIncentsPV`, `GrossParticipantCostPV`, `NetParticipantCostPV` and `NetParticipantCostAdjustedPV` with `Rqf_sct` in place of `Rqf` everywhere (in `PVF` and in `PVsub`):

```text
RebatesandIncentsPV_SCT    = ( IncentsDI + Rebates ) / PVF_sct                                       PVF_sct = Rqf_sct ^ Qm
GrossParticipantCostPV_SCT = GrossMeasCostPV_sct - ( IncentsDI + Rebates ) / PVF_sct
NetParticipantCostPV_SCT   = NTGRCost * ( GrossParticipantCostPV_SCT + ExcessIncentivesPV_sct )
NetParticipantCostAdjustedPV_SCT = NetParticipantCostPV_SCT + MEC * ( GrossMeasCostPV_sct + ExcessIncentivesPV_sct )
   GrossMeasCostPV_sct = NumUnits * ( UnitMeaCost1stBaseline - PVsub_sct ) / PVF_sct
   PVsub_sct = ( UnitMeaCost1stBaseline - UnitMeaCost2ndBaseline ) * ( (1 + MeasInflation / 4) / Rqf_sct ) ^ ( 4 * RUL_Yrs )
               if RUL_Yrs > 0, else 0
   ExcessIncentivesPV_sct = pos( IncentsDI / PVF_sct - GrossMeasCostPV_sct )
```

**Inputs:** those of the 1.5 entries; `DiscountRateQtr_SCT`.

- Example A, `RebatesandIncentsPV_SCT`: (4000 + 2000) / 1.012575^2 = **5,851.90**
- Example A, `GrossParticipantCostPV_SCT`: 100 * 200 / 1.012575^2 - (4000 + 2000) / 1.012575^2 = **13,654.43**
- Example A, `NetParticipantCostPV_SCT`: 0.9 * (13654.4319 + 0.0000) = **12,288.99**
- Example B, `RebatesandIncentsPV_SCT`: (9500 + 4000) / 1.012575^1 = **13,332.35**
- Example B, `GrossParticipantCostPV_SCT`: 10 * (9000 - 2900.3214) / 1.012575^1 - (9500 + 4000) / 1.012575^1 = **46,906.93** (`PVsub_sct` = (9000 - 6000) * ((1 + 0.02 / 4) / 1.012575)^4.5)
- Example A, `NetParticipantCostAdjustedPV_SCT`: 12288.9887 + 0.0000 = **12,288.99**
- Example B, `NetParticipantCostPV_SCT`: 0.8 * (46906.9312 + 0.0000) = **37,525.54**
- Example B, `NetParticipantCostAdjustedPV_SCT`: 37525.5450 + 0.05 * (60239.2769 + 0.0000) = **40,537.51**

### 1.7 Gross variants

The same quantity **without net-to-gross and without the market-effects adder** (`Gross`). Every net value above was scaled by `NTG + MEB`, so gross = net / (`NTG + MEB`):
the value that would come out with `NTG` = 1 and no market effects. Societal versions are blank when SCB/SCH are off. `ratio(n, d)` is 0 when `d = 0`. Unit: Dollars (PV).

| Field | Meaning | Formula |
|---|---|---|
| `ElecBenGross` | Gross Electric Benefits, without the NTG applied. | `ratio( ElecBen, NTGRkWh + MEB )` |
| `ElecBenGross_SB` | Gross Societal Base Electric Benefits, without the NTG applied. | `ratio( ElecBen_SB, NTGRkWh + MEB )` |
| `ElecBenGross_SH` | Gross Societal High Electric Benefits, without the NTG applied. | `ratio( ElecBen_SH, NTGRkWh + MEB )` |
| `ElecSupplyCostGross` | Gross Electric Supply Cost, without the NTG applied. | `ratio( ElecSupplyCost, NTGRkWh + MEB )` |
| `ElecSupplyCostGross_SB` | Gross Societal Base Electric Supply Cost, without the NTG applied. | `ratio( ElecSupplyCost_SB, NTGRkWh + MEB )` |
| `ElecSupplyCostGross_SH` | Gross Societal High Electric Supply Cost, without the NTG applied. | `ratio( ElecSupplyCost_SH, NTGRkWh + MEB )` |
| `GasBenGross` | Gross Gas Benefits, without the NTG applied. | `ratio( GasBen, NTGRTherm + MEB )` |
| `GasBenGross_SB` | Gross Societal Base Gas Benefits, without the NTG applied. | `ratio( GasBen_SB, NTGRTherm + MEB )` |
| `GasBenGross_SH` | Gross Societal High Gas Benefits, without the NTG applied. | `ratio( GasBen_SH, NTGRTherm + MEB )` |
| `GasSupplyCostGross` | Gross Gas Supply Cost, without the NTG applied. | `ratio( GasSupplyCost, NTGRTherm + MEB )` |
| `GasSupplyCostGross_SB` | Gross Societal Base Gas Supply Cost, without the NTG applied. | `ratio( GasSupplyCost_SB, NTGRTherm + MEB )` |
| `GasSupplyCostGross_SH` | Gross Societal High Gas Supply Cost, without the NTG applied. | `ratio( GasSupplyCost_SH, NTGRTherm + MEB )` |
| `WaterEnergyBenGross` | Gross Water Energy Electric Benefits, without the NTG applied. | `ratio( WaterEnergyBen, NTGRkWh + MEB )` |
| `WaterEnergyBenGross_SB` | Gross Water Energy Societal Base Electric Benefits, without the NTG applied. | `ratio( WaterEnergyBen_SB, NTGRkWh + MEB )` |
| `WaterEnergyBenGross_SH` | Gross Water Energy Societal High Electric Benefits, without the NTG applied. | `ratio( WaterEnergyBen_SH, NTGRkWh + MEB )` |
| `WaterEnergyCostGross` | Gross Water Energy Electric Supply Cost, without the NTG applied. | `ratio( WaterEnergyCost, NTGRkWh + MEB )` |
| `WaterEnergyCostGross_SB` | Gross Water Energy Societal Base Electric Supply Cost, without the NTG applied. | `ratio( WaterEnergyCost_SB, NTGRkWh + MEB )` |
| `WaterEnergyCostGross_SH` | Gross Water Energy Societal High Electric Supply Cost, without the NTG applied. | `ratio( WaterEnergyCost_SH, NTGRkWh + MEB )` |

Written out, `ElecBenGross` (for a measure that is not fuel substitution) is the sum of `ElecBen` with `Elec(k)` replaced by `phased( k ; NumUnits * UnitkWh1stBaseline * InstallationRatekWh * RealizationRatekWh, ... )`:
the price of the quarter is unchanged, only the net-to-gross factor is removed. **Inputs:** those of the net column each is divided by, plus `NTGRkWh` or `NTGRTherm` and the market-effects setting.

- Example A, `ElecBenGross`: 5841.4973 / (0.8 + 0) = **7,301.87**
- Example A, `GasBenGross`: 3454.0256 / (0.9 + 0) = **3,837.81**
- Example A, `WaterEnergyBenGross`: 30.6679 / (0.8 + 0) = **38.33**
- Example A, `ElecBenGross_SB`: 6688.3412 / (0.8 + 0) = **8,360.43**
- Example A, `GasBenGross_SH`: 5033.3438 / (0.9 + 0) = **5,592.60**
- Example B, `ElecSupplyCostGross`: 4172.1680 / (0.75 + 0.05) = **5,215.21**
- Example B, `GasBenGross`: 6990.5647 / (0.85 + 0.05) = **7,767.29**
- Example B, `WaterEnergyCostGross`: 5.6259 / (0.75 + 0.05) = **7.03**
- Example B, `ElecSupplyCostGross_SB`: 4734.0060 / (0.75 + 0.05) = **5,917.51**
- Example B, `ElecBenGross`: 0 / (0.75 + 0.05) = **0.00**

### 1.8 Total system benefit and program-cost weighting

#### `TotalSystemBenefit`, `TotalSystemBenefitGross`

- `TotalSystemBenefit`: Net lifecycle system benefit: all benefits less all supply-type costs. Unit: Dollars (PV; the "other" terms undiscounted).
- `TotalSystemBenefitGross`: Gross lifecycle system benefit: all benefits less all supply-type costs, without the NTG applied. Unit: Dollars (PV; the "other" terms undiscounted).

**Formula**

```text
TotalSystemBenefit      = ( ElecBen + GasBen + WaterEnergyBen + OtherBen )
                          - ( ElecSupplyCost + GasSupplyCost + WaterEnergyCost + OtherCost )
TotalSystemBenefitGross = ( ElecBenGross + GasBenGross + WaterEnergyBenGross + OtherBenGross )
                          - ( ElecSupplyCostGross + GasSupplyCostGross + WaterEnergyCostGross + OtherCostGross )
```

Each term is the column of that name (1.4, 1.7). Tax credits and participant costs are not in it. **Inputs:** the benefit, supply-cost and other-cost columns named, and through them every input of 1.4 and 1.7.

- Example A, `TotalSystemBenefit`: (5841.4973 + 3454.0256 + 30.6679 + 160.00) - (0.00 + 0.00 + 0.00 + 80.00) = **9,406.19**
- Example B, `TotalSystemBenefit`: (0.0000 + 6990.5647 + 0.0000 + 320.00) - (4172.1680 + 0.0000 + 5.6259 + 120.00) = **3,012.77**
- Example A, `TotalSystemBenefitGross`: (7301.8716 + 3837.8062 + 38.3348 + 200.00) - (0.00 + 0.00 + 0.00 + 100.00) = **11,278.01**
- Example B, `TotalSystemBenefitGross`: (0.0000 + 7767.2941 + 0.0000 + 400.00) - (5215.2100 + 0.0000 + 7.0323 + 150.00) = **2,795.05**

#### `WeightedBenefits`

The measure's share of its program's positive benefits: the weight that spreads the program's costs over its measures. Unit: Decimal Number.

**Formula**

```text
WeightedBenefits = share_m = B_m / SUM over the measures j of the same program (same PrgID) of B_j        if that sum is not 0
                           = 1 / (the number of measures in the program)                                    if it is 0

B_m = pos(ElecBen) + pos(GasBen) + pos(WaterEnergyBen) + pos(OtherBen)               (the measure's own columns of 1.4)
```

Five further shares are computed the same way from other benefit sets and are used only inside the cost columns of 1.9: `share_gross` (from `ElecBenGross`, `GasBenGross`, `WaterEnergyBenGross`, `OtherBenGross`),
`share_SB` (from `ElecBen_SB`, `GasBen_SB`, `WaterEnergyBen_SB`, `OtherBen`), `share_SH`, `share_SB_gross` and `share_SH_gross`. **Inputs:** the benefit columns and `PrgID`.

- Example A, `WeightedBenefits`: the only measure of program `PRG1`, so 9486.1908 / 9486.1908 = **1.00**
- Example B, `WeightedBenefits`: the only measure of program `PRG2`, so 7310.5647 / 7310.5647 = **1.00**

#### `WeightedProgramCost`

The measure's share of its program's nominal program cost. 0 without `ProgramCost.csv`. Unit: Dollars (nominal, undiscounted).

**Formula**

```text
WeightedProgramCost = share_m * SumCosts

SumCosts = SUM over the ProgramCost rows r with the measure's PrgID of T_r
T_r = AdminCostsOverheadAndGA + AdminCostsOther + MarketingOutreach + DIActivity + DIInstallation
      + DIHardwareAndMaterials + DIRebateAndInspection + EMV + UserInputIncentive + CostsRecoveredFromOtherSources
      (the fields of row r; blank = 0)
```

`OnBillFinancing` is not part of CET's sum. A `ProgramCost` row whose `PA` is `SW` is treated as `SCE`, as CET did in the real runs. `share_m` is `WeightedBenefits`. **Inputs:** `ProgramCost.csv`
(the ten cost fields, `PrgID`, `PA`) and the measure's benefit columns.

- Example A, `WeightedProgramCost`: 1 * ((2000) + (1000)) = **3,000.00** (the 2028 row: 1500 + 500; the 2029 row: 1000)
- Example B, `WeightedProgramCost`: 1 * ((1200) + (600)) = **1,800.00** (the 2028 row: 900 + 300; the 2029 row: 600)

#### `TotalExpenditures`

The measure's budget / expenditures: what the program spends on it, in nominal dollars - both its rebates and incentives plus its share of the program costs. Unit: Dollars (nominal, undiscounted).

**Formula**

```text
TotalExpenditures = RebatesandIncents + WeightedProgramCost
```

The run-receipt Budget equals this sum to the cent on CET jobs 121219 ($254,427,868.17) and 121841. A measure's own value depends on its share of program costs
(`WeightedBenefits`), which follows its benefits, so per-measure values match CET only as closely as the benefits do; per-program totals do not depend on the shares.
**Inputs:** those of `RebatesandIncents` and `WeightedProgramCost`.

- Example A, `TotalExpenditures`: 6000.00 + 3000.00 = **9,000.00**
- Example B, `TotalExpenditures`: 13500.00 + 1800.00 = **15,300.00**

#### `WtdAdminCostsOverheadAndGA`, `WtdAdminCostsOther`, `WtdMarketingOutreach`, `WtdDIActivity`, `WtdDIInstallation`, `WtdDIHardwareAndMaterials`, `WtdDIRebateAndInspection`, `WtdEMV`, `WtdUserInputIncentive`, `WtdCostsRecoveredFromOtherSources`

- `WtdAdminCostsOverheadAndGA`: The measure's share of the AdminCostsOverheadAndGA program-cost field, in nominal dollars. 0 without `ProgramCost.csv`. Unit: Dollars (nominal, undiscounted).
- `WtdAdminCostsOther`: The measure's share of the AdminCostsOther program-cost field, in nominal dollars. 0 without `ProgramCost.csv`. Unit: Dollars (nominal, undiscounted).
- `WtdMarketingOutreach`: The measure's share of the MarketingOutreach program-cost field, in nominal dollars. 0 without `ProgramCost.csv`. Unit: Dollars (nominal, undiscounted).
- `WtdDIActivity`: The measure's share of the DIActivity program-cost field, in nominal dollars. 0 without `ProgramCost.csv`. Unit: Dollars (nominal, undiscounted).
- `WtdDIInstallation`: The measure's share of the DIInstallation program-cost field, in nominal dollars. 0 without `ProgramCost.csv`. Unit: Dollars (nominal, undiscounted).
- `WtdDIHardwareAndMaterials`: The measure's share of the DIHardwareAndMaterials program-cost field, in nominal dollars. 0 without `ProgramCost.csv`. Unit: Dollars (nominal, undiscounted).
- `WtdDIRebateAndInspection`: The measure's share of the DIRebateAndInspection program-cost field, in nominal dollars. 0 without `ProgramCost.csv`. Unit: Dollars (nominal, undiscounted).
- `WtdEMV`: The measure's share of the EMV program-cost field, in nominal dollars. 0 without `ProgramCost.csv`. Unit: Dollars (nominal, undiscounted).
- `WtdUserInputIncentive`: The measure's share of the UserInputIncentive program-cost field, in nominal dollars. 0 without `ProgramCost.csv`. Unit: Dollars (nominal, undiscounted).
- `WtdCostsRecoveredFromOtherSources`: The measure's share of the CostsRecoveredFromOtherSources program-cost field, in nominal dollars. 0 without `ProgramCost.csv`. Unit: Dollars (nominal, undiscounted).

**Formula**

```text
Wtd<field>   = share_m * SUM over the ProgramCost rows r with the measure's PrgID of <field>_r          (blank = 0)
SUM of the ten Wtd columns = share_m * SumCosts = WeightedProgramCost          (CET's ProgramCosts; not written)
```

`<field>` is each of the ten cost fields of `T_r` (see `WeightedProgramCost`); `share_m` is `WeightedBenefits`. Per program they add up to the program's own totals; a measure's own values follow its
share, so they match CET per measure only as closely as the benefits do (the program totals match exactly on CET jobs 121219-121222). **Inputs:** `ProgramCost.csv` (the ten cost fields,
`PrgID`) and the measure's benefit columns.

- Example A, `WtdAdminCostsOverheadAndGA`: 1 * 1500 = **1,500.00**
- Example A, `WtdEMV`: 1 * 1000 = **1,000.00** (the 2029 row)
- Example B, `WtdDIActivity`: 1 * 300 = **300.00**
- Example B, `WtdMarketingOutreach`: 1 * 600 = **600.00**

#### `WeightedElecAlloc`

The electric share of the measure's own positive benefits: the share of its costs the levelized columns (1.16) charge to electricity. Unit: Decimal Number.

**Formula**

```text
WeightedElecAlloc = ratio( pos(ElecBen) , pos(ElecBen) + pos(GasBen) + pos(WaterEnergyBen) + pos(OtherBen) )
```

A measure-level share (not across the program). It follows CET's formula exactly: computed from CET's own benefit columns it reproduces CET's `WeightedElecAlloc` on every measure of CET jobs
121219-121222. **Inputs:** the benefit columns of 1.4.

- Example A, `WeightedElecAlloc`: 5841.4973 / (5841.4973 + 3454.0256 + 30.6679 + 160.0000) = **0.615790**
- Example B, `WeightedElecAlloc`: 0 / (0 + 6990.5647 + 0 + 320.0000) = **0.000000** (a fuel-substitution measure with no electric benefit)

### 1.9 Cost-test costs

`NoAdmin` leaves out the program-level term. All are present values in dollars. The quantities used by more than one entry:

```text
Sup         = ElecSupplyCost + GasSupplyCost + WaterEnergyCost + OtherCost
SumCostsNPV = SUM over the program's ProgramCost rows r of  T_r / Raf ^ (Year_r - F)
Year_r = first four characters of the row's ClaimYearQuarter;  Raf = 1 + DiscountRateAnnual(PA of the row)
SumCostsNPV_SCT = the same with Raf_sct = 1 + DiscountRateAnnual_SCT
share_m = WeightedBenefits (1.8)         T_r: see WeightedProgramCost
```

#### `PACCostNoAdmin`, `PACCost`

- `PACCostNoAdmin`: Program Administrator Cost test cost without program costs. Unit: Dollars (PV).
- `PACCost`: Program Administrator Cost test cost. Unit: Dollars (PV).

**Formula**

```text
PACCostNoAdmin = RebatesandIncentsPV + Sup
PACCost        = share_m * SumCostsNPV + PACCostNoAdmin
```

**Inputs:** the columns named, `ProgramCost.csv` and the First Year.

- Example A, `PACCostNoAdmin`: 5776.6364 + (0.00 + 0.00 + 0.00 + 80.00) = **5,856.64**
- Example A, `PACCost`: 1 * (2000 / 1.0766^0 + 1000 / 1.0766^1) + 5856.6364 = **8,785.49**
- Example B, `PACCostNoAdmin`: 13246.3327 + (4172.1680 + 0.00 + 5.6259 + 120.00) = **17,544.13**
- Example B, `PACCost`: 1 * (1200 / 1.0766^0 + 600 / 1.0766^1) + 17544.1265 = **19,301.44**

#### `TRCCostNoAdmin`, `TRCCost`

- `TRCCostNoAdmin`: Total Resource Cost test cost without program costs. Unit: Dollars (PV).
- `TRCCost`: Total Resource Cost test cost. Unit: Dollars (PV).

**Formula**

```text
TRCCostNoAdmin = RebatesandIncentsPV + NetParticipantCostAdjustedPV + Sup
TRCCost        = share_m * SumCostsNPV + TRCCostNoAdmin
```

**Inputs:** `RebatesandIncentsPV`, `NetParticipantCostAdjustedPV`, the four columns of `Sup`, `WeightedBenefits`, `ProgramCost.csv`, the First Year and `DiscountRateAnnual`.

- Example A, `TRCCostNoAdmin`: 5776.6364 + 12130.9364 + 80.00 = **17,987.57**
- Example A, `TRCCost`: 2928.8501 + 17987.5728 = **20,916.42** (`SumCostsNPV` = 2000 / 1.0766^0 + 1000 / 1.0766^1)
- Example B, `TRCCostNoAdmin`: 13246.3327 + 40970.3555 + 4297.7938 = **58,514.48**
- Example B, `TRCCost`: 1757.3101 + 58514.4821 = **60,271.79** (`SumCostsNPV` = 1200 / 1.0766^0 + 600 / 1.0766^1)

#### `TRCCostGross`

Total Resource Cost without NTG applied. Unit: Dollars (PV).

**Formula**

```text
TRCCostGross = share_gross_m * SumCostsNPV + RebatesandIncentsPV + GrossParticipantCostPV + SupGross
SupGross     = ElecSupplyCostGross + GasSupplyCostGross + WaterEnergyCostGross + OtherCostGross
```

`share_gross_m` is the share of 1.8 computed on the gross benefits. (The tool uses *gross* supply costs here; CET adds the net supply costs to the gross participant cost.)
**Inputs:** `RebatesandIncentsPV`, `GrossParticipantCostPV`, the four Gross columns of `SupGross`, the gross benefit columns, `ProgramCost.csv`, the First Year and `DiscountRateAnnual`.

- Example A, `TRCCostGross`: 2928.8501 + 5776.6364 + 13478.8183 + 100.00 = **22,284.30**
- Example B, `TRCCostGross`: 1757.3101 + 13246.3327 + 47421.2222 + 5372.2423 = **67,797.11**

#### `RIMCostNoAdmin`, `RIMCost`

- `RIMCostNoAdmin`: Ratepayer Impact Measure test cost without program costs. Unit: Dollars (PV).
- `RIMCost`: Ratepayer Impact Measure test cost. Unit: Dollars (PV).

**Formula**

```text
RIMCostNoAdmin = LifecycleBillReducElec + LifecycleBillReducGas + PACCostNoAdmin
RIMCost        = LifecycleBillReducElec + LifecycleBillReducGas + PACCost
```

**Inputs:** the bill-impact columns of 1.5 and the inputs of `PACCost`.

- Example A, `RIMCostNoAdmin`: 30972.4907 + 10167.7073 + 5856.6364 = **46,996.83**
- Example A, `RIMCost`: 30972.4907 + 10167.7073 + 8785.4865 = **49,925.68**
- Example B, `RIMCostNoAdmin`: 0.0000 + 13198.1966 + 17544.1265 = **30,742.32**
- Example B, `RIMCost`: 0.0000 + 13198.1966 + 19301.4366 = **32,499.63**

#### `SCBCostNoAdmin`, `SCBCost`, `SCBCostGross`, `SCHCostNoAdmin`, `SCHCost`, `SCHCostGross`

- `SCBCostNoAdmin`: Societal Cost Test Base cost, without program costs. Unit: Dollars (PV at the societal rate).
- `SCBCost`: Societal Cost Test Base cost. Unit: Dollars (PV at the societal rate).
- `SCBCostGross`: Societal Cost Test Base cost, without the NTG applied. Unit: Dollars (PV at the societal rate).
- `SCHCostNoAdmin`: Societal Cost Test High cost, without program costs. Unit: Dollars (PV at the societal rate).
- `SCHCost`: Societal Cost Test High cost. Unit: Dollars (PV at the societal rate).
- `SCHCostGross`: Societal Cost Test High cost, without the NTG applied. Unit: Dollars (PV at the societal rate).

**Formula**

```text
SCBCostNoAdmin = RebatesandIncentsPV_SCT + NetParticipantCostAdjustedPV_SCT + Sup_SB
SCBCost        = share_SB_m * SumCostsNPV_SCT + SCBCostNoAdmin
SCBCostGross   = share_SB_gross_m * SumCostsNPV_SCT + RebatesandIncentsPV_SCT + GrossParticipantCostPV_SCT + SupSBGross

Sup_SB      = ElecSupplyCost_SB + GasSupplyCost_SB + WaterEnergyCost_SB + OtherCost
SupSBGross  = ElecSupplyCostGross_SB + GasSupplyCostGross_SB + WaterEnergyCostGross_SB + OtherCostGross
share_SB_m  = the share of 1.8 computed on  B_SB = pos(ElecBen_SB) + pos(GasBen_SB) + pos(WaterEnergyBen_SB) + pos(OtherBen)
share_SB_gross_m = the same share computed on the gross versions of those benefits
(SCHCostNoAdmin, SCHCost, SCHCostGross: the same with _SH)
```

`SumCostsNPV_SCT` discounts program costs at the societal rate (CET uses the primary rate for them; see CALCULATIONS C17). **Inputs:** `RebatesandIncentsPV_SCT`, `NetParticipantCostAdjustedPV_SCT`, `GrossParticipantCostPV_SCT`, the `_SB` / `_SH` supply-cost and gross columns, `OtherCost`, `ProgramCost.csv`, the First Year and `DiscountRateAnnual_SCT`.

- Example A, `SCBCostNoAdmin`: 5851.8994 + 12288.9887 + 80.00 = **18,220.89**
- Example A, `SCBCost`: 2952.1089 + 18220.8880 = **21,173.00** (`SumCostsNPV_SCT` = 2000 / 1.0503^0 + 1000 / 1.0503^1)
- Example A, `SCHCost`: 2952.1089 + 18220.8880 = **21,173.00**
- Example B, `SCBCostNoAdmin`: 13332.3458 + 40537.5088 + 4860.4757 = **58,730.33**
- Example B, `SCBCost`: 1771.2654 + 58730.3302 = **60,501.60**
- Example B, `SCHCostNoAdmin`: 13332.3458 + 40537.5088 + 5291.5034 = **59,161.36**
- Example B, `SCHCost`: 1771.2654 + 59161.3579 = **60,932.62**
- Example A, `SCBCostGross`: 2952.1089 + 5851.8994 + 13654.4319 + 100 = **22,558.44** (`SupSBGross` = `OtherCostGross` = 100)

### 1.10 Benefit-cost ratios

`B = ElecBen + GasBen + WaterEnergyBen + OtherBen`. A ratio is 0 when its cost is 0, and blank for the societal ratios when SCB/SCH are off. Unit: ratio (no unit). Supply costs are inside the *costs*, not netted
from the numerator. Each test's numerator is written as its own column just before its ratios (`TRCBen`, `PACBen`, `RIMBen`, `SCBBen`, `SCHBen`; the Participant Cost Test's is
`PCTBen`), so every ratio is two columns of the same file: `<test>Ratio = <test>Ben / <test>Cost` and `<test>RatioNoAdmin = <test>Ben / <test>CostNoAdmin` (0 when the cost is 0).

#### `TRCBen`, `TRCRatio`, `TRCRatioNoAdmin`

- `TRCBen`: Total Resource Cost test benefits: the numerator of `TRCRatio` and `TRCRatioNoAdmin`. Unit: Dollars (PV).
- `TRCRatio`: Total Resource Cost Test Ratio. Unit: Decimal Number.
- `TRCRatioNoAdmin`: Total Resource Cost Test Ratio, without program costs. Unit: Decimal Number.

**Formula**

```text
TRCBen          = B + TaxCredits
TRCRatio        = TRCBen / TRCCost
TRCRatioNoAdmin = TRCBen / TRCCostNoAdmin
```

**Inputs:** the four benefit columns of `B`, `TaxCredits`, `TRCCost` and `TRCCostNoAdmin`.

- Example A, `TRCBen`: 9486.1908 + 400.00 = **9,886.19**
- Example B, `TRCBen`: 7310.5647 + 800.00 = **8,110.56**

- Example A, `TRCRatio`: (9486.1908 + 400.00) / 20916.4229 = **0.4727**
- Example A, `TRCRatioNoAdmin`: (9486.1908 + 400.00) / 17987.5728 = **0.5496**
- Example B, `TRCRatio`: (7310.5647 + 800.00) / 60271.7921 = **0.1346**
- Example B, `TRCRatioNoAdmin`: (7310.5647 + 800.00) / 58514.4821 = **0.1386**

#### `PACBen`, `PACRatio`, `PACRatioNoAdmin`

- `PACBen`: Program Administrator Cost test benefits: the numerator of `PACRatio` and `PACRatioNoAdmin`. Unit: Dollars (PV).
- `PACRatio`: Program Administrator Cost test ratio. Unit: Decimal Number.
- `PACRatioNoAdmin`: Program Administrator Cost test ratio, without program costs. Unit: Decimal Number.

**Formula**

```text
PACBen          = B = ElecBen + GasBen + WaterEnergyBen + OtherBen
PACRatio        = PACBen / PACCost
PACRatioNoAdmin = PACBen / PACCostNoAdmin
```

**Inputs:** the four benefit columns of `B`, `PACCost` and `PACCostNoAdmin`.

- Example A, `PACBen`: 5841.4973 + 3454.0256 + 30.6679 + 160.00 = **9,486.19**
- Example B, `PACBen`: 0.0000 + 6990.5647 + 0.0000 + 320.00 = **7,310.56**

- Example A, `PACRatio`: 9486.1908 / 8785.4865 = **1.0798**
- Example A, `PACRatioNoAdmin`: 9486.1908 / 5856.6364 = **1.6197**
- Example B, `PACRatio`: 7310.5647 / 19301.4366 = **0.3788**
- Example B, `PACRatioNoAdmin`: 7310.5647 / 17544.1265 = **0.4167**

#### `RIMBen`, `RIMRatio`, `RIMRatioNoAdmin`

- `RIMBen`: Ratepayer Impact Measure test benefits: the numerator of `RIMRatio` and `RIMRatioNoAdmin`. Unit: Dollars (PV).
- `RIMRatio`: Ratepayer Impact Measure test ratio. Unit: Decimal Number.
- `RIMRatioNoAdmin`: Ratepayer Impact Measure test ratio, without program costs. Unit: Decimal Number.

**Formula**

```text
RIMBen          = B + LifecycleBillIncrElec + LifecycleBillIncrGas
RIMRatio        = RIMBen / RIMCost
RIMRatioNoAdmin = RIMBen / RIMCostNoAdmin
```

**Inputs:** the four benefit columns of `B`, `LifecycleBillIncrElec`, `LifecycleBillIncrGas`, `RIMCost` and `RIMCostNoAdmin`.

- Example A, `RIMBen`: 9486.1908 + 0.00 + 0.00 = **9,486.19**
- Example B, `RIMBen`: 7310.5647 + 20764.8592 + 0.00 = **28,075.42**

- Example A, `RIMRatio`: (9486.1908 + 0.00 + 0.00) / 49925.6844 = **0.1900**
- Example A, `RIMRatioNoAdmin`: (9486.1908 + 0.00 + 0.00) / 46996.8343 = **0.2018**
- Example B, `RIMRatio`: (7310.5647 + 20764.8592 + 0.00) / 32499.6332 = **0.8639**
- Example B, `RIMRatioNoAdmin`: (7310.5647 + 20764.8592 + 0.00) / 30742.3231 = **0.9132**

#### `SCBBen`, `SCBRatio`, `SCBRatioNoAdmin`, `SCHBen`, `SCHRatio`, `SCHRatioNoAdmin`

- `SCBBen`: Societal Cost Test Base benefits: the numerator of `SCBRatio` and `SCBRatioNoAdmin`. Unit: Dollars (PV at the societal rate).
- `SCBRatio`: Societal Cost Test Base Ratio. Unit: Decimal Number.
- `SCBRatioNoAdmin`: Societal Cost Test Base Ratio, without program costs. Unit: Decimal Number.
- `SCHBen`: Societal Cost Test High benefits: the numerator of `SCHRatio` and `SCHRatioNoAdmin`. Unit: Dollars (PV at the societal rate).
- `SCHRatio`: Societal Cost Test High Ratio. Unit: Decimal Number.
- `SCHRatioNoAdmin`: Societal Cost Test High Ratio, without program costs. Unit: Decimal Number.

**Formula**

```text
SCBBen          = ElecBen_SB + GasBen_SB + WaterEnergyBen_SB + OtherBen
SCBRatio        = SCBBen / SCBCost
SCBRatioNoAdmin = SCBBen / SCBCostNoAdmin
SCHBen          = ElecBen_SH + GasBen_SH + WaterEnergyBen_SH + OtherBen
SCHRatio        = SCHBen / SCHCost
SCHRatioNoAdmin = SCHBen / SCHCostNoAdmin
```

**Inputs:** the `_SB` / `_SH` benefit columns, `OtherBen`, and the societal cost columns of 1.9. `SCBBen` and `SCHBen` are blank when SCB/SCH are off.

- Example A, `SCBBen`: 6688.3412 + 4314.2947 + 35.5698 + 160.00 = **11,198.21**
- Example A, `SCHBen`: 7296.3723 + 5033.3438 + 39.2180 + 160.00 = **12,528.93**
- Example B, `SCBBen`: 0.00 + 8827.1057 + 0.00 + 320.00 = **9,147.11**
- Example B, `SCHBen`: 0.00 + 10432.0340 + 0.00 + 320.00 = **10,752.03**

- Example A, `SCBRatio`: (6688.3412 + 4314.2947 + 35.5698 + 160.00) / 21172.9970 = **0.5289**
- Example A, `SCBRatioNoAdmin`: 11198.2057 / 18220.8880 = **0.6146**
- Example A, `SCHRatio`: (7296.3723 + 5033.3438 + 39.2180 + 160.00) / 21172.9970 = **0.5917**
- Example A, `SCHRatioNoAdmin`: 12528.9340 / 18220.8880 = **0.6876**
- Example B, `SCBRatio`: (0.00 + 8827.1057 + 0.00 + 320.00) / 60501.5955 = **0.1512**
- Example B, `SCBRatioNoAdmin`: 9147.1057 / 58730.3302 = **0.1557**
- Example B, `SCHRatio`: (0.00 + 10432.0340 + 0.00 + 320.00) / 60932.6233 = **0.1765**
- Example B, `SCHRatioNoAdmin`: 10752.0340 / 59161.3579 = **0.1817**

#### `PCTBen`, `PCTCost`, `PCTRatio`

- `PCTBen`: The Participant Cost Test (CA Standard Practice Manual, Chapter 2) benefits, which the CET does not calculate: the participant's benefits (bill reductions, incentives received, tax credits) against the participant's costs (the full measure cost and any bill increase), on gross savings and without market effects. `PCTBen` and `PCTCost` are $ (PV); `PCTRatio` is a ratio (0 when `PCTCost` is 0). Not yet checked against a CPUC definition: CPUC has not published how the CET will calculate the test. Unit: Dollars.
- `PCTCost`: The Participant Cost Test (CA Standard Practice Manual, Chapter 2) costs, which the CET does not calculate: the participant's benefits (bill reductions, incentives received, tax credits) against the participant's costs (the full measure cost and any bill increase), on gross savings and without market effects. `PCTBen` and `PCTCost` are $ (PV); `PCTRatio` is a ratio (0 when `PCTCost` is 0). Not yet checked against a CPUC definition: CPUC has not published how the CET will calculate the test. Unit: Dollars.
- `PCTRatio`: The Participant Cost Test (CA Standard Practice Manual, Chapter 2) ratios, which the CET does not calculate: the participant's benefits (bill reductions, incentives received, tax credits) against the participant's costs (the full measure cost and any bill increase), on gross savings and without market effects. `PCTBen` and `PCTCost` are $ (PV); `PCTRatio` is a ratio (0 when `PCTCost` is 0). Not yet checked against a CPUC definition: CPUC has not published how the CET will calculate the test. Unit: Decimal Number.

**Formula**

```text
PCTBen   = LifecycleBillReducElecGross + LifecycleBillReducGasGross + RebatesandIncentsPV + TaxCreditsGross / PVF
PCTCost  = GrossParticipantCostPV + RebatesandIncentsPV + LifecycleBillIncrElecGross + LifecycleBillIncrGasGross          (GrossParticipantCostPV + RebatesandIncentsPV = the measure cost before incentives)
PCTRatio = PCTBen / PCTCost
```

**Where** `PVF = Rqf ^ Qm` is the participant-cost discount factor of `RebatesandIncentsPV` (1.5): the claim quarter at the primary quarterly rate.
**Inputs:** the columns named in the formula and their inputs; no program costs.

- Example A, `PCTBen`: 38715.6133 + 11297.4525 + 5776.6364 + 500 / 1.0386667225 = **56,271.09**
- Example A, `PCTCost`: 13478.8183 + 5776.6364 + 0.00 + 0.00 = **19,255.45**
- Example A, `PCTRatio`: 56271.0886 / 19255.4547 = **2.9223**
- Example B, `PCTBen`: 0.00 + 14664.6629 + 13246.3327 + 1000 / 1.01915 = **28,892.21**
- Example B, `PCTCost`: 47421.2222 + 13246.3327 + 25956.0741 + 0.00 = **86,623.63**
- Example B, `PCTRatio`: 28892.2054 / 86623.6290 = **0.3335**

### 1.11 Emissions

Present when "Include emissions" is on. *First-year* is the measure's **first four quarters** of life (`k` = 0 ... 3, or fewer if the life is shorter), not a calendar year; `Lifecycle...` is all of its quarters. `Net` includes net-to-gross
and `MEB`; `Gross` does not. Units: CO2 metric tons; NOx and PM10 pounds.

**The pieces every emission formula uses**

```text
fe_net   = NumUnits * (NTGRkWh + MEB) * InstallationRatekWh * RealizationRatekWh
fe_gross = NumUnits * InstallationRatekWh * RealizationRatekWh
fg_net   = NumUnits * (NTGRTherm + MEB) * InstallationRateTherm * RealizationRateTherm
fg_gross = NumUnits * InstallationRateTherm * RealizationRateTherm

co2(k)   = tonnes of CO2 emitted per annual kWh delivered in quarter k:
   SUM over the hours i of quarter(k) of  shape(i) * emission_rate_tonnes_per_kwh(year(k), i)     (the ACC's own hourly rate)
   Quarters exist only for years the ACC file has: life beyond its last year adds no emissions.

AnnualkWh(k) = the per-unit annual kWh in force during quarter k, with  EQ = 4 * EUL_Yrs,  RQ = 4 * RUL_Yrs  (RQ = 0 if RUL_Yrs >= EUL_Yrs),
               kWh1 = UnitkWh1stBaseline,  kWh2 = UnitkWh2ndBaseline  (on a kWh-per-unit profile: the profile's own annual kWh per unit, 0.5):
   single baseline (RQ = 0):  0 if kWh1 = 0;  kWh1 * (EQ mod 1) if k < EQ < k + 1 (the last part-quarter);  else kWh1
   dual baseline (RQ > 0):    0 if kWh1 = kWh2 = 0;  kWh1 if RQ > k + 1;  kWh2 if RQ < k and EQ > k + 1;
                              kWh1 * (RQ mod 1) + kWh2 * (EQ - RQ)      if RQ and EQ both fall inside quarter k;
                              kWh1 * (RQ mod 1) + kWh2 * (1 - RQ mod 1) if only RQ falls inside quarter k;
                              kWh2 * (EQ - floor(EQ))                   if only EQ falls inside quarter k;   else kWh2
   counted quarters: k = 0 ... EQ - 1 (the quarters of 0.2)

nox, pm10 = the NOx / PM10 rate (lb per kWh) of the Emissions table (CET's) for the key
            (PA, AC_Elec_Target_Sector, AC_Elec_Impact_Profile, AC_Climate_Zone); 0 where the table has no row
NOxfactor = the NOx factor (lb per therm) of the CombustionType table (CET's) for CombustionType; 0 when CombustionType is blank or unknown
CO2Gas    = metric tons of CO2 per therm, the run parameters' value for PA and the ACC version (0.00531; 0.00585 on the 2021 ACC)
G_firstyear = Th1                                  if RUL >= 1
            = RUL*Th1 + (1 - RUL)*Th2              if 0 < RUL < 1 <= EUL
            = RUL*Th1 + (EUL - RUL)*Th2            if RUL < EUL < 1
            = Th1                                  if RUL = 0 and EUL > 1
            = EUL*Th1                              if RUL = 0 and EUL <= 1
G_lifecycle = RUL*Th1 + (EUL - RUL)*Th2 if RUL > 0;   EUL*Th1 otherwise
     (Th1 = UnitTherm1stBaseline, Th2 = UnitTherm2ndBaseline, EUL = EUL_Yrs, RUL = RUL_Yrs, or 0 if RUL_Yrs >= EUL_Yrs)
```

#### `FirstYearNetElecCO2`, `FirstYearGrossElecCO2`, `LifecycleNetElecCO2`, `LifecycleGrossElecCO2`

- `FirstYearNetElecCO2`: First Year Net Electric CO2 emissions avoided. Unit: Metric Tons.
- `FirstYearGrossElecCO2`: First Year Gross Electric CO2 emissions avoided. Unit: Metric Tons.
- `LifecycleNetElecCO2`: Lifecycle Net Electric CO2 emissions avoided. Unit: Metric Tons.
- `LifecycleGrossElecCO2`: Lifecycle Gross Electric CO2 emissions avoided. Unit: Metric Tons.

**Formula**

```text
FirstYearNetElecCO2   = fe_net   * SUM over k = 0 ... min(3, n-1) of  co2(k) * AnnualkWh(k)
FirstYearGrossElecCO2 = fe_gross * SUM over k = 0 ... min(3, n-1) of  co2(k) * AnnualkWh(k)
LifecycleNetElecCO2   = fe_net   * SUM over k = 0 ... n-1 of          co2(k) * AnnualkWh(k)
LifecycleGrossElecCO2 = fe_gross * SUM over k = 0 ... n-1 of          co2(k) * AnnualkWh(k)
```

**Inputs:** `NumUnits`, `NTGRkWh`, `InstallationRatekWh`, `RealizationRatekWh`, `UnitkWh1stBaseline`, `UnitkWh2ndBaseline`, `EUL_Yrs`, `RUL_Yrs`, `ClaimYearQuarter`, the electric-profile fields of `ElecBen`; the ACC's
`emission_rate_tonnes_per_kwh`; market effects. A negative value is added emissions (added load).

- Example A, `FirstYearNetElecCO2`: 68.4 * 0.0002 * 500 * (2208 + 2208 + 2160 + 2184) / 8760 = **6.8400**
- Example A, `FirstYearGrossElecCO2`: 85.50 * 0.0002 * 500 * (2208 + 2208 + 2160 + 2184) / 8760 = **8.5500**
- Example A, `LifecycleNetElecCO2`: 68.4 * 0.0002 / 8760 * (2208 * 500 + 2208 * 500 + 2160 * 500 + 2184 * 500 + 2208 * 500 + 2208 * 500 + 2160 * 500 + 2184 * 500 + 2208 * 500 + 2208 * 300) = **16.4385** (each quarter's hours times 500; the last part-quarter, k = 9, counts 0.6)
- Example B, `FirstYearNetElecCO2`: 7.2 * 0.0002 * -4000 * (2184 + 2208 + 2208 + 2160) / 8760 = **-5.7600**
- Example B, `LifecycleNetElecCO2`: 7.2 * 0.0002 / 8760 * (2184 * -4000 + 2208 * -4000 + 2208 * -4000 + 2160 * -4000 + 2184 * -3250 + 2208 * -2500 + 2208 * -2500 + 2160 * -2500 + 2184 * -2500 + 2208 * -2500) = **-11.4342** (each quarter's hours times `AnnualkWh(k)`: -4000 for k = 0-3, the blend -4000 * 0.5 + -2500 * 0.5 = -3250 for k = 4, -2500 for k = 5-9)

#### `FirstYearNetElecNOx`, `FirstYearGrossElecNOx`, `LifecycleNetElecNOx`, `LifecycleGrossElecNOx`, `FirstYearNetElecPM10`, `FirstYearGrossElecPM10`, `LifecycleNetElecPM10`, `LifecycleGrossElecPM10`

- `FirstYearNetElecNOx`: First Year Net NOx emissions avoided. Unit: Pounds.
- `FirstYearGrossElecNOx`: First Year Gross NOx emissions avoided. Unit: Pounds.
- `LifecycleNetElecNOx`: Lifecycle Net NOx emissions avoided. Unit: Pounds.
- `LifecycleGrossElecNOx`: Lifecycle Gross NOx emissions avoided. Unit: Pounds.
- `FirstYearNetElecPM10`: First Year Net Electric PM10 emissions avoided. Unit: Pounds.
- `FirstYearGrossElecPM10`: First Year Gross Electric PM10 emissions avoided. Unit: Pounds.
- `LifecycleNetElecPM10`: Lifecycle Net Electric PM10 emissions avoided. Unit: Pounds.
- `LifecycleGrossElecPM10`: Lifecycle Gross Electric PM10 emissions avoided. Unit: Pounds.

**Formula**

```text
FirstYearNetElecNOx    = fe_net   * SUM over k = 0 ... min(3, n-1) of  nox  * AnnualkWh(k) / 4
FirstYearGrossElecNOx  = fe_gross * SUM over k = 0 ... min(3, n-1) of  nox  * AnnualkWh(k) / 4
LifecycleNetElecNOx    = fe_net   * SUM over k = 0 ... n-1 of          nox  * AnnualkWh(k) / 4
LifecycleGrossElecNOx  = fe_gross * SUM over k = 0 ... n-1 of          nox  * AnnualkWh(k) / 4
FirstYearNetElecPM10   = fe_net   * SUM over k = 0 ... min(3, n-1) of  pm10 * AnnualkWh(k) / 4
FirstYearGrossElecPM10 = fe_gross * SUM over k = 0 ... min(3, n-1) of  pm10 * AnnualkWh(k) / 4
LifecycleNetElecPM10   = fe_net   * SUM over k = 0 ... n-1 of          pm10 * AnnualkWh(k) / 4
LifecycleGrossElecPM10 = fe_gross * SUM over k = 0 ... n-1 of          pm10 * AnnualkWh(k) / 4
```

The `/ 4` is because `nox` and `pm10` are per annual kWh and each quarter carries a quarter of a year. **Inputs:** those of `FirstYearNetElecCO2`, plus `AC_Elec_Target_Sector`, `AC_Elec_Impact_Profile`, `AC_Climate_Zone`, `IOU_AC_Territory` (the Emissions table key).

- Example A, `FirstYearNetElecNOx`: 0, because the Emissions table has no row for the key (PGE, Residential, `W_SHAPE`, zone 13) = **0.0000**
- Example A, `FirstYearNetElecPM10`: 0, same key = **0.0000**
- Example B, `FirstYearNetElecNOx`: the key (PGE, Commercial, `3 = Commercial HVAC`, zone 13) has `nox` = 0.00004643: 7.2 * (4 * 0.00004643 * -4000 / 4) = **-1.3372**
- Example B, `FirstYearNetElecPM10`: `pm10` = 0.00004643 for the same key: 7.2 * (4 * 0.00004643 * -4000 / 4) = **-1.3372**
- Example B, `LifecycleNetElecNOx`: 7.2 * 0.00004643 / 4 * (-4000 * 4 + -3250 + -2500 * 5) = **-2.6535**

#### `FirstYearNetGasCO2`, `FirstYearGrossGasCO2`, `LifecycleNetGasCO2`, `LifecycleGrossGasCO2`

- `FirstYearNetGasCO2`: First Year Net Gas CO2 emissions avoided. Unit: Metric Tons.
- `FirstYearGrossGasCO2`: First Year Gross Gas CO2 emissions avoided. Unit: Metric Tons.
- `LifecycleNetGasCO2`: Lifecycle Net Gas CO2 emissions avoided. Unit: Metric Tons.
- `LifecycleGrossGasCO2`: Lifecycle Gross Gas CO2 emissions avoided. Unit: Metric Tons.

**Formula**

```text
FirstYearNetGasCO2 = fg_net * CO2Gas * G_firstyear          FirstYearGrossGasCO2 = fg_gross * CO2Gas * G_firstyear
LifecycleNetGasCO2 = fg_net * CO2Gas * G_lifecycle          LifecycleGrossGasCO2 = fg_gross * CO2Gas * G_lifecycle
```

`fg_...`, `CO2Gas`, `G_firstyear`, `G_lifecycle` are defined above. **Inputs:** `NumUnits`, `NTGRTherm`, `InstallationRateTherm`, `RealizationRateTherm`, `UnitTherm1stBaseline`, `UnitTherm2ndBaseline`, `EUL_Yrs`, `RUL_Yrs`,
`IOU_AC_Territory`; ACC version (the run parameters' `CO2Gas`); market effects.

- Example A, `FirstYearNetGasCO2`: 81 * 0.00531 * 20 = **8.6022** (`fg_net` = 100 * 0.9 * 1 * 0.9; `RUL` = 0, `EUL` = 2.4 > 1, so `G_firstyear` = 20)
- Example A, `LifecycleNetGasCO2`: 81 * 0.00531 * 48 = **20.6453** (`G_lifecycle` = 2.4 * 20)
- Example B, `FirstYearNetGasCO2`: 8.55 * 0.00531 * 600 = **27.2403** (`fg_net` = 10 * (0.85 + 0.05) * 1 * 0.95; `RUL` = 1.125 >= 1, so `G_firstyear` = `Th1` = 600)
- Example B, `LifecycleNetGasCO2`: 8.55 * 0.00531 * 1225 = **55.6156** (`G_lifecycle` = 1.125 * 600 + (2.5 - 1.125) * 400)
- Example B, `FirstYearGrossGasCO2`: 9.5 * 0.00531 * 600 = **30.2670**

#### `FirstYearNetGasNOx`, `FirstYearGrossGasNOx`, `LifecycleNetGasNOx`, `LifecycleGrossGasNOx`

- `FirstYearNetGasNOx`: First Year Net Gas NOx emissions avoided. 0 when `CombustionType` is blank or unknown. Unit: Pounds.
- `FirstYearGrossGasNOx`: First Year Gross Gas NOx emissions avoided. 0 when `CombustionType` is blank or unknown. Unit: Pounds.
- `LifecycleNetGasNOx`: Lifecycle Net Gas NOx emissions avoided. 0 when `CombustionType` is blank or unknown. Unit: Pounds.
- `LifecycleGrossGasNOx`: Lifecycle Gross Gas NOx emissions avoided. 0 when `CombustionType` is blank or unknown. Unit: Pounds.

**Formula**

```text
FirstYearNetGasNOx            = fg_net   * NOxfactor * G_firstyear       FirstYearGrossGasNOx            = fg_gross * NOxfactor * G_firstyear
LifecycleNetGasNOx   = fg_net   * NOxfactor * G_lifecycle       LifecycleGrossGasNOx   = fg_gross * NOxfactor * G_lifecycle
```

**Inputs:** those of `FirstYearNetGasCO2`, plus `CombustionType` (the CombustionType table key, `<combustor type>:<controls>`).

- Example A, `FirstYearNetGasNOx`: `CombustionType` is blank, so `NOxfactor` = 0 and 81 * 0 * 20 = **0.0000**
- Example B, `FirstYearNetGasNOx`: `CombustionType` = `Small Boilers (<100 MMBtu/hr Heat Input):Uncontrolled`, `NOxfactor` = 0.0098: 8.55 * 0.0098 * 600 = **50.2740**
- Example B, `LifecycleGrossGasNOx`: 9.5 * 0.0098 * 1225 = **114.0475**

### 1.12 `CreditedPA`

#### `CreditedPA`

The PA the measure is credited to: the PA of the measure's program in the `ProgramCost.csv` input, exactly as filed. Used for the portfolio summary's "credited PA" rows. Unit: Text.

* Statewide allocation **off**: the filed `PA`, so an unallocated statewide program (filed with `PA` = `SW`) shows **`SW`**, and an IOU's own program shows that IOU.
* Statewide allocation **on**: the `ProgramCost.csv` rows are allocated like the measures (C15), so each allocated share of a statewide program (`<IOU>_SW_...`) shows
  the IOU it was allocated to (its program row's `PA` is set to that IOU); every other program keeps its filed `PA`.
* No `ProgramCost.csv`, or no row for the program: blank.

**Formula**

```text
CreditedPA = PA of the ProgramCost row with  PrgID = the measure's PrgID  and  PrgYear = the measure's claim year
            = PA of the first ProgramCost row with that PrgID      if no row has that year
            = blank                                                if there is no such row (or no ProgramCost.csv)
   (with statewide allocation: the allocated ProgramCost rows and the allocated PrgID)
```

**Inputs:** `PrgID`, `ClaimYearQuarter`; `ProgramCost.csv`: `PrgID`, `PrgYear`, `PA`. Settings: "Allocate statewide programs".

- Example A, `CreditedPA`: `PrgID` = `PRG1`, claim year 2028; the `PRG1` / 2028 program-cost row has `PA` = **PGE**
- Example B, `CreditedPA`: `PrgID` = `PRG2`, claim year 2028; the `PRG2` / 2028 program-cost row has `PA` = **PGE**

### 1.13 Component columns (only with "Calculate ACC components separately")

Placed before `CreditedPA`. Each avoided-cost component is priced alone through the same route as the total, so the components always add up. They are signed and **before** the benefit / supply-cost split.

#### `ElecValue_<component>`

Discounted lifetime dollars of one component of the electric avoided cost. One column per component present in the electric ACC: energy, generation_capacity, avoided_as_procurement, losses, methane_leakage, ghg_cap_and_trade, ghg_cap_and_invest, ghg_adder, ghg_portfolio_rebalancing, air_quality_adder, transmission, distribution. Unit: Dollars (PV, TRC rows).

**Formula** (hourly-profile route; `c` is the component column of the ACC)

```text
ElecValue_c = SUM over k = 0 ... n-1 of
                w(k) * DF(k) * Elec(k) * ( SUM over the hours i of quarter(k) of  shape(i) * c(y*(k), i) / 1000 )
SUM over c of ElecValue_c = V_elec = ElecBen - ElecSupplyCost          (checked on every run and reported in the log)
```

**Inputs:** those of `ElecBen`.

#### `GasValue_<component>`

Discounted lifetime dollars of one component of the gas avoided cost: market, td, nox, co2, upstream_methane, behind_meter_methane, air_quality_adder. Unit: Dollars (PV, TRC rows).

**Formula**

```text
GasValue_c = SUM over k = 0 ... n-1 of
               w(k) * DF(k) * Gas(k) * ( SUM over the months m of quarter(k) of  gasshape(m) * c_m(y^(k)) )
               c = the gas ACC component
SUM over c of GasValue_c = V_gas = GasBen - GasSupplyCost
```

`behind_meter_methane` is 0 for a measure that does not include it (0.6). **Inputs:** those of `GasBen`.

### 1.14 Program metadata (`Prg_*`)

The program's row from the program metadata: the CEDARS Programs list built into the package (CEDARS' export of 2026-10-03, 1,416 programs; its `ProgramManager` column,
which holds staff names, is not included), with the rows of a supplied program-metadata file replacing (same `PrgID`) or extending it. Placed after `CreditedPA`, one
column per column of the table, prefixed `Prg_`. Text, exactly as in the table. **They change no priced value**: they label the measure, give the summary its
`program segment` rows, and feed the Exclude_From_CE option.

#### `Prg_MatchedOn`

Which program-metadata row the measure's `Prg_` columns come from. Unit: Text.

**Formula**

```text
Prg_MatchedOn = PrgID                                if the table has a row with the measure's PrgID (as priced: with statewide allocation, the allocated <IOU>_SW_... PrgID)
              = PrgID + " + agreeing IOU copies"     if PrgID is a statewide SW_... row and some of its blank fields were filled from its <IOU>_SW_... rows
              = "agreeing IOU copies"                if there is no SW_... row but there are <IOU>_SW_... rows
              = "(not found)"                        otherwise (every Prg_ column is then blank)
```

The CEDARS list carries each statewide program once per funding IOU, with the IOU prepended (`PGE_SW_HVAC_Up`, `SCE_SW_HVAC_Up`, ...), plus a thin `SW_HVAC_Up` row
(`PA` = `SW`, most fields blank). With **Allocate statewide programs** on, each allocated share carries its IOU's `PrgID` and so gets that IOU's own row. With it off, the
measure keeps the `SW_...` `PrgID`; a field that the `SW_...` row leaves blank is filled from the `<IOU>_SW_...` rows **only when every one of them gives the same non-blank
value** (they often differ, for example in `ProgramName` and `ProgramImplementer`), and is left blank otherwise.

**Inputs:** `PrgID`; settings: "Allocate statewide programs", "Program metadata".

- Example A, `Prg_MatchedOn`: the supplied table has a `PRG1` row = **PRG1**
- Example B, `Prg_MatchedOn`: the supplied table has a `PRG2` row = **PRG2**

#### `Prg_<column>`

**The value of one column of the measure's program-metadata row** (see `Prg_MatchedOn`). Blank when the program is not found, or the table leaves it blank. A supplied
table's extra columns are carried the same way (`Prg_<its column name>`).

**Formula**

```text
Prg_X = column X of the program-metadata row chosen for the measure (Prg_MatchedOn)
```

**Inputs:** `PrgID`; the program metadata (built in, or supplied).

| Column | Meaning | Example A | Example B |
|---|---|---|---|
| `Prg_PA` | The program administrator the program list gives (`SW` for a statewide program's `SW_...` row, the IOU for its `<IOU>_SW_...` rows). | PGE | PGE |
| `Prg_ProgramName` | The program's name. | Example Residential Program | Example Commercial Program |
| `Prg_ProgramStatus` | `active`, `new`, `closed`, `closed_to_new_commitments` or `closure_pending`. | active | new |
| `Prg_ProgramSegment` | The portfolio segment: `resource_acquisition`, `market_support`, `equity`, `codes_and_standards` or `evaluation_measurement_verification` (blank on many older programs). Drives the summary's `program segment` rows. | resource_acquisition | market_support |
| `Prg_StatewideProgram` | For a statewide program, its statewide program code and name (for example `SW_HVAC_Up - SW HVAC Upstream Program`); blank otherwise. |  |  |
| `Prg_PrimarySector` | The program's primary sector (`Residential`, `Commercial`, `Industrial`, `Agricultural`, `Public`, `Cross-Cutting`, `Portfolio Support`). | Residential | Commercial |
| `Prg_Sector` | The program's sector in CEDARS' coded form (for example `Commercial (Com)`). | Residential (Res) | Commercial (Com) |
| `Prg_BPSector` | The business-plan sector split, as text (for example `Commercial: 0.900000, Industrial: 0.100000`). | Residential: 1.000000 | Commercial: 1.000000 |
| `Prg_ProgramImplementer` | Who implements it (`IOU Core`, `Third-Party Solicited`, `REN/CCA`, ...). | IOU Core | Third-Party Solicited |
| `Prg_ProgramCategory` | The program category (`New Construction`, `Market Transformation`, `Financing`, ...). | Other | Market Transformation |
| `Prg_ImplementationContractor` | The implementation contractor, where given. |  | Example Implementer |
| `Prg_StartYear` | The program's first year. | 2024 | 2027 |
| `Prg_EndYear` | The program's last year (blank = open-ended). |  | 2031 |
| `Prg_Resource_Flag` | `True` for a resource program. | True | False |
| `Prg_NonResource_Flag` | `True` for a non-resource program. | False | True |
| `Prg_Deemed_Flag` | `True` when the program has deemed measures. | True | False |
| `Prg_Custom_Flag` | `True` when the program has custom measures. | False | True |
| `Prg_Upstream_Flag` | `True` when the program delivers upstream. | False | False |
| `Prg_Midstream_Flag` | `True` when the program delivers midstream. | False | True |
| `Prg_Downstream_Flag` | `True` when the program delivers downstream. | True | False |
| `Prg_DirectInstall` | `Yes`, `Partial` or `No`. | No | Partial |
| `Prg_Audit_Flag` | `True` when the program includes audits. | False | True |
| `Prg_Financing` | `None`, `OBF` or `Other Financing`. | None | OBF |
| `Prg_ParentProgram` | The parent program, where given. | Residential Programs | Commercial Third Party Programs |
| `Prg_Exclude_From_Budget` | CEDARS' flag: `True` when the program is left out of budget totals. | False | False |
| `Prg_Exclude_From_CE` | CEDARS' flag: `True` when the program is left out of cost-effectiveness totals. With **Leave Exclude_From_CE programs out of the portfolio summary** the summary leaves these measures out (2.2). | False | False |
| `Prg_Exclude_From_Statewide_Flag` | CEDARS' flag: `True` when the program is left out of statewide totals. | False | False |

- Example A, `Prg_ProgramSegment`: the `PRG1` row's `ProgramSegment` = **resource_acquisition**
- Example B, `Prg_ProgramSegment`: the `PRG2` row's `ProgramSegment` = **market_support**
- Example B, `Prg_Exclude_From_CE`: the `PRG2` row's `Exclude_From_CE` = **False**

### 1.15 Savings quantities

CET's savings columns (CalcSavings): what the measure saves in its first year, over its life and on average per year, gross and net (net includes net-to-gross plus market effects), after
installation and realization rates. They are quantities, not dollars, and are not priced. `KWh` includes the IOU water energy, `KWhSite` leaves it out and `KWhWater` is the water energy
alone. Every column matches CET's own to rounding on CET jobs 121219-121222 (21,370 measures), 121842 (0% market effects) and 121841 (5%).

#### `FirstYearNetKWh`, `FirstYearNetKWhSite`, `FirstYearNetKWhWater`, `FirstYearNetKW`, `FirstYearNetThm`

First-year net electric energy (kWh: with the IOU water energy, site only, water only), demand (kW) and gas (therms) savings. Units: `FirstYearNetKWh` kWh; `FirstYearNetKWhSite` kWh; `FirstYearNetKWhWater` kWh; `FirstYearNetKW` kW; `FirstYearNetThm` Therm.

**Formula**

```text
FirstYearNetKWh      = NumUnits * (NTGRkWh + MEB) * IRkWh * RRkWh * ( w1 * (UnitkWh1stBaseline + UnitkWhIOUWater1stBaseline) + w2 * (UnitkWh2ndBaseline + UnitkWhIOUWater2ndBaseline) )
FirstYearNetKWhSite  = NumUnits * (NTGRkWh + MEB) * IRkWh * RRkWh * ( w1 * UnitkWh1stBaseline + w2 * UnitkWh2ndBaseline )
FirstYearNetKWhWater = NumUnits * (NTGRkWh + MEB) * IRkWh * RRkWh * ( w1 * UnitkWhIOUWater1stBaseline + w2 * UnitkWhIOUWater2ndBaseline )
FirstYearNetKW       = NumUnits * (NTGRkW  + MEB) * IRkW  * RRkW  * ( w1 * UnitkW1stBaseline + w2 * UnitkW2ndBaseline )
FirstYearNetThm      = NumUnits * (NTGRTherm + MEB) * IRThm * RRThm * ( w1 * UnitTherm1stBaseline + w2 * UnitTherm2ndBaseline )

w1 = 1      if RUL_Yrs > 1           w2 = EUL_Yrs - RUL_Yrs   if 0 < RUL_Yrs < 1 and EUL_Yrs < 1
   = RUL_Yrs if 0 < RUL_Yrs < 1         = 1 - RUL_Yrs       if 0 < RUL_Yrs < 1 and EUL_Yrs >= 1
   = EUL_Yrs if RUL_Yrs = 0 and EUL_Yrs < 1                     = 0 otherwise
   = 1 otherwise
```

`IR` / `RR` are the installation and realization rates of each fuel. `MEB` is 0.05 with market
effects on, else 0. `EUL_Yrs` is the value as filed, as everywhere. A measure with an RUL of exactly 1 year takes
the "otherwise" branches (w1 = 1, w2 = 0), as CET's SQL does. **Inputs:** `NumUnits`, the 1st/2nd-baseline kWh, kW, therm and IOU water fields, `NTGRkWh`, `NTGRkW`, `NTGRTherm`, the installation
and realization rates, `EUL_Yrs`, `RUL_Yrs`; the market-effects setting.

- Example A, `FirstYearNetKWh`: 100 * 0.8 * 0.95 * 0.9 * (500 + 3) = **34,405.20** (`RUL_Yrs` = 0 and `EUL_Yrs` >= 1, so w1 = 1)
- Example A, `FirstYearNetKWhSite`: 100 * 0.8 * 0.95 * 0.9 * 500 = **34,200.00**
- Example A, `FirstYearNetKW`: 100 * 0.8 * 1 * 1 * 0.2 = **16.00**
- Example A, `FirstYearNetThm`: 100 * 0.9 * 1 * 0.9 * 20 = **1,620.00**
- Example B, `FirstYearNetKWh`: 10 * (0.75 + 0.05) * 1 * 0.9 * (-4000 + -6) = **-28,843.20** (`RUL_Yrs` = 1.125 > 1, so w1 = 1 and w2 = 0)
- Example B, `FirstYearNetKWhWater`: 10 * (0.75 + 0.05) * 1 * 0.9 * -6 = **-43.20**
- Example B, `FirstYearNetKW`: no kW savings, so 0 = **0.00**
- Example B, `FirstYearNetThm`: 10 * (0.85 + 0.05) * 1 * 0.95 * 600 = **5,130.00**

#### `FirstYearGrossKWh`, `FirstYearGrossKWhSite`, `FirstYearGrossKWhWater`, `FirstYearGrossKW`, `FirstYearGrossThm`

First-year gross savings: the net columns above without the net-to-gross (and market-effects) factor. Units: `FirstYearGrossKWh` kWh; `FirstYearGrossKWhSite` kWh; `FirstYearGrossKWhWater` kWh; `FirstYearGrossKW` kW; `FirstYearGrossThm` Therm.

**Formula**

```text
FirstYearGross<x> = FirstYearNet<x> without the (NTGR... + MEB) factor, e.g.
FirstYearGrossKWh = NumUnits * IRkWh * RRkWh * ( w1 * (UnitkWh1stBaseline + UnitkWhIOUWater1stBaseline) + w2 * (UnitkWh2ndBaseline + UnitkWhIOUWater2ndBaseline) )
```

**Inputs:** those of the net columns, less the NTG fields and the market-effects setting.

- Example A, `FirstYearGrossKWh`: 100 * 0.95 * 0.9 * (500 + 3) = **43,006.50**
- Example A, `FirstYearGrossKWhWater`: 100 * 0.95 * 0.9 * 3 = **256.50**
- Example B, `FirstYearGrossKWhSite`: 10 * 1 * 0.9 * -4000 = **-36,000.00**
- Example B, `FirstYearGrossThm`: 10 * 1 * 0.95 * 600 = **5,700.00**

#### `LifecycleGrossKWh`, `LifecycleGrossKWhSite`, `LifecycleGrossKWhWater`, `LifecycleGrossThm`, `LifecycleNetKWh`, `LifecycleNetKWhSite`, `LifecycleNetKWhWater`, `LifecycleNetThm`

Savings over the measure's whole life, undiscounted: the 1st-baseline amount for the remaining useful life and the 2nd-baseline amount for the rest of the effective useful life. Units: `LifecycleGrossKWh` kWh; `LifecycleGrossKWhSite` kWh; `LifecycleGrossKWhWater` kWh; `LifecycleGrossThm` Therm; `LifecycleNetKWh` kWh; `LifecycleNetKWhSite` kWh; `LifecycleNetKWhWater` kWh; `LifecycleNetThm` Therm.

**Formula**

```text
LifecycleGross<x> = Q * IR * RR * EUL_Yrs * a1                                    if RUL_Yrs = 0
                  = Q * IR * RR * ( a1 * RUL_Yrs + a2 * (EUL_Yrs - RUL_Yrs) )      if RUL_Yrs > 0
LifecycleNet<x>   = LifecycleGross<x> * (NTGR + MEB)
```

`Q` is `NumUnits`; `a1` / `a2` are the 1st- and 2nd-baseline amounts of the fuel (for `KWh` the unit kWh plus the IOU water kWh, for `KWhSite` the unit kWh, for `KWhWater` the water kWh, for `Thm`
the unit therms); `IR`, `RR` and `NTGR` are the fuel's installation rate, realization rate and net-to-gross ratio. CET has no lifecycle kW column. **Inputs:** those of the first-year columns.

- Example A, `LifecycleGrossKWh`: 100 * 0.95 * 0.9 * 2.4 * (500 + 3) = **103,215.60**
- Example A, `LifecycleNetThm`: 100 * 0.9 * 1 * 0.9 * 2.4 * 20 = **3,888.00**
- Example B, `LifecycleGrossKWh`: 10 * 1 * 0.9 * ((-4000 + -6) * 1.125 + (-2500 + -4) * (2.5 - 1.125)) = **-71,547.75**
- Example B, `LifecycleNetThm`: 10 * (0.85 + 0.05) * 1 * 0.95 * (600 * 1.125 + 400 * (2.5 - 1.125)) = **10,473.75**

#### `AnnualGrossKWh`, `AnnualGrossKWhSite`, `AnnualGrossKWhWater`, `AnnualGrossKW`, `AnnualGrossThm`, `AnnualNetKWh`, `AnnualNetKWhSite`, `AnnualNetKWhWater`, `AnnualNetKW`, `AnnualNetThm`

Average annual savings over the measure's life: the lifecycle amount divided by the effective useful life. Units: `AnnualGrossKWh` kWh; `AnnualGrossKWhSite` kWh; `AnnualGrossKWhWater` kWh; `AnnualGrossKW` kW; `AnnualGrossThm` Therm; `AnnualNetKWh` kWh; `AnnualNetKWhSite` kWh; `AnnualNetKWhWater` kWh; `AnnualNetKW` kW; `AnnualNetThm` Therm.

**Formula**

```text
Annual<x> = Lifecycle<x> / EUL_Yrs                (0 when EUL_Yrs = 0)
AnnualGrossKW = Q * IRkW * RRkW * UnitkW1stBaseline                                                        if RUL_Yrs = 0
              = Q * IRkW * RRkW * ( UnitkW1stBaseline * RUL_Yrs + UnitkW2ndBaseline * (EUL_Yrs - RUL_Yrs) ) / EUL_Yrs   if RUL_Yrs > 0
AnnualNetKW   = AnnualGrossKW * (NTGRkW + MEB)
```

**Inputs:** those of the lifecycle columns (and the kW fields).

- Example A, `AnnualNetKWh`: 82572.4800 / 2.4 = **34,405.20**
- Example A, `AnnualGrossKW`: 100 * 1 * 1 * 0.2 = **20.00**
- Example B, `AnnualGrossThm`: 11637.5000 / 2.5 = **4,655.00**
- Example B, `AnnualNetKWhSite`: -57150.0000 / 2.5 = **-22,860.00**

### 1.16 Discounted savings, lifecycle net benefits and levelized values

CET's discounted savings and the "levelized" columns built on them (CalcCost): a benefit, or the electric (gas) share of a cost, per discounted net kWh (therm) saved. All present values are
at the primary discount rate. Each levelized column is 0 when its denominator is 0. Whether the levelized columns are still needed is RAID decision D9 in the replacement BRD.

#### `DiscountedSavingsGrosskWh`, `DiscountedSavingsNetkWh`, `DiscountedSavingsGrossThm`, `DiscountedSavingsNetThm`

Lifetime site kWh (no water) and therm savings, each year discounted at the annual rate and the whole brought back to the First Year. Units: `DiscountedSavingsGrosskWh` kWh; `DiscountedSavingsNetkWh` kWh; `DiscountedSavingsGrossThm` Therm; `DiscountedSavingsNetThm` Therm.

**Formula**

```text
DiscountedSavingsGross<f> = Q * IR * RR * a1 * A(EUL_Yrs) / PVF                                                    if RUL_Yrs = 0
                          = Q * IR * RR * ( a1 * A(RUL_Yrs) + a2 * A(EUL_Yrs - RUL_Yrs) / Raf ^ RUL_Yrs ) / PVF    if RUL_Yrs > 0
                          = 0                                                                                       if a1 = 0 or EUL_Yrs = 0
DiscountedSavingsNet<f>   = DiscountedSavingsGross<f> * (NTGR + MEB)

A(n) = 1 + (1 - Raf ^ -(n - 1)) / DiscountRateAnnual          the sum of Raf ^ -t for t = 0 ... n-1 (n may be fractional)
```

`<f>` is `kWh` (`a1` / `a2` = `UnitkWh1stBaseline` / `UnitkWh2ndBaseline`, with the kWh rates and `NTGRkWh`) or `Thm` (the therm fields, rates and `NTGRTherm`). `Raf = 1 + DiscountRateAnnual(PA)`;
`PVF` as in `RebatesandIncentsPV`. `EUL_Yrs` is as filed. The net columns match CET's to 2e-6 (relative) on every measure of CET jobs 121219-121222, 121842 and 121841. CET's *gross* columns
differ from its own net ones on early-replacement measures (its gross kWh counts one 2nd-baseline year too many and its gross therms leave the 2nd baseline undiscounted); ReCET's gross is
the net formula without NTG, so the two agree whenever NTG + MEB = 1, and it equals CET's on every single-baseline measure. **Inputs:** `NumUnits`, the kWh and therm fields, the installation
and realization rates, `NTGRkWh`, `NTGRTherm`, `EUL_Yrs`, `RUL_Yrs`, `ClaimYearQuarter`, `IOU_AC_Territory`; First Year, ACC version (discount rates), the market-effects setting.

- Example A, `DiscountedSavingsGrosskWh`: 100 * 0.95 * 0.9 * 500 * (1 + (1 - 1.0766^-1.4) / 0.0766) / 1.01915^2 = **93,907.91** (`RUL_Yrs` = 0, `EUL_Yrs` = 2.4)
- Example A, `DiscountedSavingsNetkWh`: 0.8 * 93907.9082 = **75,126.33**
- Example A, `DiscountedSavingsGrossThm`: 100 * 1 * 0.9 * 20 * (1 + (1 - 1.0766^-1.4) / 0.0766) / 1.01915^2 = **3,954.02**
- Example B, `DiscountedSavingsGrosskWh`: 10 * 1 * 0.9 * (-4000 * (1 + (1 - 1.0766^-0.125) / 0.0766) + -2500 * (1 + (1 - 1.0766^-0.375) / 0.0766) / 1.0766^1.125) / 1.01915 = **-67,117.49** (A(1.125) and A(2.5 - 1.125))
- Example B, `DiscountedSavingsNetThm`: (0.85 + 0.05) * 10917.8371 = **9,826.05**

#### `TRCLifecycleNetBen`, `PACLifecycleNetBen`

- `TRCLifecycleNetBen`: TRC Benefits minus TRC Costs. Unit: Dollars (PV).
- `PACLifecycleNetBen`: PAC Benefits minus PAC Costs. Unit: Dollars (PV).

**Formula**

```text
TRCLifecycleNetBen = ElecBen + GasBen - TRCCost
PACLifecycleNetBen = ElecBen + GasBen - PACCost
```

**Inputs:** those of `ElecBen`, `GasBen`, `TRCCost` and `PACCost`.

- Example A, `TRCLifecycleNetBen`: 5841.4973 + 3454.0256 - 20916.4229 = **-11,620.90**
- Example A, `PACLifecycleNetBen`: 5841.4973 + 3454.0256 - 8785.4865 = **510.04**
- Example B, `TRCLifecycleNetBen`: 0 + 6990.5647 - 60271.7921 = **-53,281.23**

#### `LevBenElec`, `LevBenGas`

Benefit per discounted unit saved: the electric benefit per discounted net kWh and the gas benefit per discounted net therm. Units: `LevBenElec` Dollars/kWh; `LevBenGas` Dollars/Therm.

**Formula**

```text
LevBenElec = ratio( ElecBen , DiscountedSavingsNetkWh )
LevBenGas  = ratio( GasBen , DiscountedSavingsNetThm )
```

CET's exported `LevBenElec` is instead the **SCT Base** electric benefit per discounted kWh (`ElecBen_SB / DiscountedSavingsNetkWh`, on every measure of CET jobs 121219-121222): CalcCost lists the
three `LevBenElec` columns in a different order from the values it selects, so the TRC value lands under another name. ReCET gives the TRC value its name says (CALCULATIONS.md C17).
`LevBenGas` matches CET's. **Inputs:** those of `ElecBen`, `GasBen` and the discounted savings.

- Example A, `LevBenElec`: 5841.4973 / 75126.3265 = **0.077756**
- Example A, `LevBenGas`: 3454.0256 / 3558.6155 = **0.970609**
- Example B, `LevBenElec`: 0 / -53693.9950 = **0.000000**
- Example B, `LevBenGas`: 6990.5647 / 9826.0534 = **0.711432**

#### `LevTRCCost`, `LevTRCCostNoAdmin`, `LevPACCost`, `LevPACCostNoAdmin`, `LevRIMCost`

The electric share of a test's cost per discounted net kWh. Unit: Dollars/kWh.

**Formula**

```text
Lev<T> = ratio( <T> * WeightedElecAlloc , DiscountedSavingsNetkWh )          <T> = TRCCost, TRCCostNoAdmin, PACCost, PACCostNoAdmin or RIMCost
```

Computed from CET's own cost, allocation and discounted-savings columns, each reproduces CET's on every measure of CET jobs 121219-121222. **Inputs:** those of the cost, `WeightedElecAlloc` and
`DiscountedSavingsNetkWh`.

- Example A, `LevTRCCost`: 20916.4229 * 0.615790 / 75126.3265 = **0.171446**
- Example A, `LevPACCostNoAdmin`: 5856.6364 * 0.615790 / 75126.3265 = **0.048005**
- Example A, `LevRIMCost`: 49925.6844 * 0.615790 / 75126.3265 = **0.409227**
- Example B, `LevTRCCost`: 60271.7921 * 0 / -53693.9950 = **0.000000**

#### `LevNetBenTRCElec`, `LevNetBenTRCGas`, `LevNetBenTRCElecNoAdmin`, `LevNetBenTRCGasNoAdmin`, `LevNetBenPACElec`, `LevNetBenPACGas`, `LevNetBenPACElecNoAdmin`, `LevNetBenPACGasNoAdmin`, `LevNetBenRIMElec`, `LevNetBenRIMGas`

Net benefit per discounted unit saved, by fuel: the fuel's benefit less its share of the test's cost (`WeightedElecAlloc` to electricity, the rest to gas). Units: `LevNetBenTRCElec` Dollars/kWh; `LevNetBenTRCGas` Dollars/Therm; `LevNetBenTRCElecNoAdmin` Dollars/kWh; `LevNetBenTRCGasNoAdmin` Dollars/Therm; `LevNetBenPACElec` Dollars/kWh; `LevNetBenPACGas` Dollars/Therm; `LevNetBenPACElecNoAdmin` Dollars/kWh; `LevNetBenPACGasNoAdmin` Dollars/Therm; `LevNetBenRIMElec` Dollars/kWh; `LevNetBenRIMGas` Dollars/Therm.

**Formula**

```text
LevNetBen<T>Elec<v> = ratio( ElecBen - <T>Cost<v> * WeightedElecAlloc ,       DiscountedSavingsNetkWh )
LevNetBen<T>Gas<v>  = ratio( GasBen  - <T>Cost<v> * (1 - WeightedElecAlloc) , DiscountedSavingsNetThm )
<T> = TRC, PAC (with <v> = "" or NoAdmin) or RIM (<v> = "")
```

Computed from CET's own columns, each reproduces CET's on every measure of CET jobs 121219-121222. **Inputs:** those of the benefits, costs, `WeightedElecAlloc` and the discounted savings.

- Example A, `LevNetBenTRCElec`: (5841.4973 - 20916.4229 * 0.615790) / 75126.3265 = **-0.093690**
- Example A, `LevNetBenTRCGas`: (3454.0256 - 20916.4229 * (1 - 0.615790)) / 3558.6155 = **-1.287659**
- Example A, `LevNetBenPACGasNoAdmin`: (3454.0256 - 5856.6364 * (1 - 0.615790)) / 3558.6155 = **0.338290**
- Example B, `LevNetBenTRCGas`: (6990.5647 - 60271.7921 * (1 - 0)) / 9826.0534 = **-5.422444**
- Example B, `LevNetBenRIMGas`: (6990.5647 - 32499.6332 * (1 - 0)) / 9826.0534 = **-2.596065**

### 1.17 A true-up against its parent's profile (`AC_Elec_Impact_Profile_Parent`, `AC_Elec_Impact_Profile_Parent_2nd_Baseline`)

Written only when the Measure file has the ReCET-only parent-profile columns (INPUT_FIELDS 1.3; BRD BR-05.6), after the record's own priced values; the two
input columns are echoed with the inputs. A record that names a parent is priced a second time as the same record filed on the parent's profiles: the same
ACC, claim quarter, life, rates and quantities, a blank `AC_Elec_Impact_Profile_Water` following the parent's profile, a kWh-per-unit profile carrying its
own kWh (C21). Each incremental column is the record's value as filed less that second value. Blank on a record with no parent; the `_SB`, `_SH`, SCB and
SCH ones are blank when the SCT is off. **They change no other column**: the record's own benefits, costs and ratios are as filed, no cost test or summary
uses them, and there is no incremental cost or ratio (the costs do not depend on the profile). The worked examples of section 3 have no parent; C21's test
checks every column against the job priced twice.

```text
XIncremental<s> = X<s>  -  X<s> priced with AC_Elec_Impact_Profile = AC_Elec_Impact_Profile_Parent
                                      and AC_Elec_Impact_Profile_2nd_Baseline = AC_Elec_Impact_Profile_Parent_2nd_Baseline (blank = the parent's)
ElecBenIncremental = V_elec(own shapes) - V_elec(parent's shapes)            (no fuel substitution; with it, each side is split as in C6.1 first)
<C>Incremental     = <C> - <C> on the parent's profiles                     (<C> = TotalSystemBenefit or a test's <T>Ben of the run's cost-test set)
```

`V_elec` is section 0.7's lifetime value; both sides use the record's own quantities (`AC_Net_kWh_ScaledProfile_*_Baseline`, each on its side's profiles),
claim quarter, life, ACC and discount rate.

| Field | Meaning |
|---|---|
| `ParentElecPricingStatus` | The electric pricing status of the record priced on its parent's profiles, with the values of `ElecPricingStatus` (1.3); blank on a record with no parent. `UNPRICED: no hourly profile` means the parent's profile matched nothing, so the incremental columns hold the record's whole value (preflight `no_parent_profile`). Unit: Text. |
| `ElecBenIncremental`, `ElecSupplyCostIncremental` | A true-up's change in `ElecBen` / `ElecSupplyCost` (1.4, TRC rows at the primary rate) from its parent's profile to its own. Unit: Dollars (PV). |
| `WaterEnergyBenIncremental`, `WaterEnergyCostIncremental` | A true-up's change in `WaterEnergyBen` / `WaterEnergyCost` (1.4) from its parent's profile to its own; 0 when the record's water energy has a profile of its own. Unit: Dollars (PV). |
| `ElecBenIncremental_SB`, `ElecSupplyCostIncremental_SB`, `WaterEnergyBenIncremental_SB`, `WaterEnergyCostIncremental_SB`, `ElecBenIncremental_SH`, `ElecSupplyCostIncremental_SH`, `WaterEnergyBenIncremental_SH`, `WaterEnergyCostIncremental_SH` | A true-up's change in the SCT Base (`_SB`) or SCT High (`_SH`) electric and water-energy benefits and supply costs (1.6, at the societal rate) from its parent's profile to its own. Unit: Dollars (PV). |
| `TotalSystemBenefitIncremental` | A true-up's change in `TotalSystemBenefit` (1.8) from its parent's profile to its own. Unit: Dollars (PV). |
| `TRCBenIncremental`, `PACBenIncremental`, `RIMBenIncremental`, `SCBBenIncremental`, `SCHBenIncremental`, `PCTBenIncremental` | A true-up's change in each cost test's benefits, the ratio numerators `<T>Ben` (1.10), from its parent's profile to its own. Unit: Dollars (PV). |

---------------------------------------------------------------------------------------------------------------------------------

## 2. Other files in a run folder

A run creates `<output folder>/<run name>_acc<version>/` (a timestamp `run_YYYYMMDD_HHMMSS` when no name is given). A two-version run creates `<output folder>/<run name>/` holding `run_acc<A>/`,
`run_acc<B>/` and the comparison files. Every table is written as CSV (UTF-8 with BOM) or Parquet according to "Output format".

### 2.1 `measure_level_acc<version>`

The table of section 1.

**Column names (0.1.50, 0.1.55).** Several columns were renamed from the field dictionary (0.1.50) and when the calculation code took the input and output field
names (0.1.55: the annual quantities say they are net, and the societal participant PVs take CalcCE's `_SCT` suffix), and the restated inputs (`poc_*`),
`ProgramCosts`, `WeightedSavings` and the per-record check columns (`FilingValidation`, `FilingRules`, `ACCCheck`) are no longer written. Output templates and saved
Results views that use an old name still work (the new name is used). CET's own outputs file keeps CET's names; the CET comparison (2.6) reads them as ReCET's.

**Column order (0.1.84).** The cost-test columns are written in the order of the run's cost-test set (COST_TESTS.md): `TotalSystemBenefit` and
`TotalSystemBenefitGross`, the program-cost weighting (`WeightedBenefits`, `WeightedProgramCost`, the `Wtd*` columns, `WeightedElecAlloc`,
`TotalExpenditures`), then each test's columns together (`<T>Ben`, `<T>Cost`, `<T>CostNoAdmin`, `<T>CostGross`, `<T>Ratio`, `<T>RatioNoAdmin`,
`<T>LifecycleNetBen`, its levelized columns; TRC, PAC, RIM, SCB, SCH, PCT in EE-2026), then `LevBenElec` and `LevBenGas`. Before 0.1.84 the PCT columns
came right after the gross bill impacts and the costs were written before the ratios. Every value is unchanged; a reader that takes columns by name is
not affected.

**A set's own streams (0.1.86).** A cost-test set that adds an input stream (COST_TESTS.md section 3: a non-energy benefit from a Measure column of its
own, say `HealthNEB` from a health-benefit column the set names) writes it per pricing (`HealthNEB`, `HealthNEB_SB` ...) and its gross form (`HealthNEBGross` ...) after the
pricing's own columns, before `TotalSystemBenefit`; the Measure column itself is echoed with the inputs. `field_definitions` takes their definitions
from the set. EE-2026's input streams are `OtherBen`, `OtherCost` and `TaxCredits` (C7), which stay where they were.

| ReCET writes | Before | CET's outputs file |
|---|---|---|
| `AC_Net_kWh_1st_Baseline`, `AC_Net_kWh_2nd_Baseline`, `AC_Net_Therms_1st_Baseline`, `AC_Net_Therms_2nd_Baseline` (0.1.55) | `AC_kWh_1st_Baseline`, `AC_kWh_2nd_Baseline`, `AC_Therms_1st_Baseline`, `AC_Therms_2nd_Baseline` (0.1.50-0.1.54) | (none) |
| `AC_Net_kWh_Water_1st_Baseline`, `AC_Net_kWh_Water_2nd_Baseline` (0.1.55) | `AC_kWh_Water_1st_Baseline`, `AC_kWh_Water_2nd_Baseline` (0.1.50-0.1.54) | (none) |
| `<RebatesandIncentsPV/NetParticipantCostPV/NetParticipantCostAdjustedPV/GrossParticipantCostPV>_SCT` (0.1.55) | the same with `_sct` | (none) |
| `Used_ACCVersion`, `Used_Year`, `Used_Quarter` | `POC_ACC`, `poc_claim_year`, `poc_claim_quarter` | (none) |
| `AC_Net_kWh_1st_Baseline`, `AC_Net_kWh_2nd_Baseline`, `AC_Net_Therms_1st_Baseline`, `AC_Net_Therms_2nd_Baseline` | `poc_shape_scale`, `poc_shape_scale_2nd`, `poc_annual_net_therms_1st`, `poc_annual_net_therms_2nd` | (none) |
| `AC_Net_kWh_Water_1st_Baseline`, `AC_Net_kWh_Water_2nd_Baseline`, `Fuel_Sub_Flag` | `poc_water_scale_1st`, `poc_water_scale_2nd`, `poc_is_fuel_sub` | (none) |
| `LifecycleBill<Reduc/Incr><Elec/Gas>` | `Bill<Reduc/Incr><Elec/Gas>` | the old name |
| `LifecycleBill<Reduc/Incr><Elec/Gas>Gross` | `Bill<Reduc/Incr><Elec/Gas>Gross` | (none) |
| `FirstYear<Net/Gross><Elec/Gas><CO2/NOx>`, `FirstYear<Net/Gross>ElecPM10` | `<Net/Gross><Elec/Gas><CO2/NOx>`, `<Net/Gross>PM10` | the old name |
| `Lifecycle<Net/Gross><Elec/Gas><CO2/NOx>`, `Lifecycle<Net/Gross>ElecPM10` | `<Net/Gross><Elec/Gas><CO2/NOx>Lifecycle`, `<Net/Gross>PM10Lifecycle` | the old name |
| `CreditedPA` | `CreditedIOU` | (none) |

### 2.2 `portfolio_summary_acc<version>`

One row for the whole job (`group` = `portfolio`), then one row per `IOU_AC_Territory` (`group` = `IOU territory (PA)`), then one per credited PA (`group` = `credited PA`, from `CreditedPA`; left out when there is no `ProgramCost.csv`),
then one per program segment (`group` = `program segment`, from `Prg_ProgramSegment`; `(blank)` for programs with no segment or not in the program metadata),
then one per program year (`group` = `program year`, from `Used_Year`, the claim year; BRD BR-04.1, so a multi-year filing's tests and TSB are given year by year).
Every number is computed over the measures of the row's group.

The tests are those of the run's cost-test set ([COST_TESTS.md](COST_TESTS.md); the run keeps it as `cost_tests.yaml`, 2.9c). The table below is
EE-2026's. A set with another test `<T>` adds `<T>Ben`, `<T>Cost` and `<T>_ratio` (`<T>Ben / <T>Cost`), and `<T>_NoAdmin_ratio` when `<T>` is a headline
test with a NoAdmin variant; the ratios come in the set's order, the tests with a NoAdmin variant first (`cost_test_sets.summary_ratios`).

With **Leave Exclude_From_CE programs out of the portfolio summary** (`apply_exclude_from_ce`), the measures whose `Prg_Exclude_From_CE` is `True` are left out of every one
of those rows and summed in one last row (`group` = `excluded`, `value` = `Exclude_From_CE programs (left out of the rows above)`), so nothing disappears unseen. Without
it they are counted like every other measure.

| Field | Meaning and unit | Formula |
|---|---|---|
| `group` | The kind of row. Text. | `portfolio`, `IOU territory (PA)`, `credited PA`, `program segment`, `program year` or `excluded` |
| `value` | The group's value. Text. | `all measures`, the IOU territory (`PGE`, `SCE`, `SDGE`, `SCG`) or the credited PA (any PA of `ProgramCost.csv`, such as a REN or CCA; `SW` for unallocated statewide programs), the segment (for example `resource_acquisition`, `(blank)`), the program year (for example `2028`), or the excluded row's label |
| `measures` | Number of measures in the group. Count. | `COUNT of the measure-level rows in the group` |
| `ElecBen`, `GasBen`, `WaterEnergyBen`, `OtherBen`, `ElecSupplyCost`, `GasSupplyCost`, `WaterEnergyCost`, `TotalSystemBenefit`, `TRCCost`, `PACCost`, `RIMCost`, `SCBCost`, `SCHCost`, `TRCCostNoAdmin`, `PACCostNoAdmin`, `WeightedProgramCost`, `LifecycleBillReducElec`, `LifecycleBillReducGas`, `PCTBen`, `PCTCost` | The group's total of the measure-level column of the same name (a column with no values, for example `SCBCost` when SCB/SCH are off, is omitted). $ (`WeightedProgramCost` nominal, the rest PV). | `SUM over the measures of the group of the measure-level column of the same name` |
| `FirstYearNetElecCO2`, `FirstYearNetGasCO2` | The group's First Year Net Electric CO2 emissions avoided and First Year Net Gas CO2 emissions avoided. Metric Tons. | `SUM over the group of FirstYearNetElecCO2` (and of `FirstYearNetGasCO2`) |
| `TRCBen`, `PACBen`, `RIMBen`, `SCBBen`, `SCHBen` | The numerators of the group's ratios below: the group's total of the measure-level column of the same name (`SCBBen` and `SCHBen` omitted when SCB/SCH are off), so `TRC_ratio = TRCBen / TRCCost`, `TRC_NoAdmin_ratio = TRCBen / TRCCostNoAdmin`, `PAC_ratio = PACBen / PACCost`, and so on. $ (PV). | `SUM over the group of TRCBen` (and of each of the others) |
| `TRC_ratio` | Portfolio Total Resource Cost ratio, on CET's receipt definition: the summed benefits (supply costs are inside the cost, not netted from the numerator) over the summed cost. Ratio. | `( SUM(ElecBen) + SUM(GasBen) + SUM(WaterEnergyBen) + SUM(OtherBen) + SUM(TaxCredits) ) / SUM(TRCCost)` |
| `TRC_NoAdmin_ratio` | The same over the cost without program costs. Ratio. | `( SUM(ElecBen) + SUM(GasBen) + SUM(WaterEnergyBen) + SUM(OtherBen) + SUM(TaxCredits) ) / SUM(TRCCostNoAdmin)` |
| `PAC_ratio` | Portfolio Program Administrator Cost ratio. Ratio. | `( SUM(ElecBen) + SUM(GasBen) + SUM(WaterEnergyBen) + SUM(OtherBen) ) / SUM(PACCost)` |
| `PAC_NoAdmin_ratio` | The same over the cost without program costs. Ratio. | `( SUM(ElecBen) + SUM(GasBen) + SUM(WaterEnergyBen) + SUM(OtherBen) ) / SUM(PACCostNoAdmin)` |
| `RIM_ratio` | Portfolio Ratepayer Impact Measure ratio. Ratio. | `( SUM(ElecBen) + SUM(GasBen) + SUM(WaterEnergyBen) + SUM(OtherBen) + SUM(LifecycleBillIncrElec) + SUM(LifecycleBillIncrGas) ) / SUM(RIMCost)` |
| `SCB_ratio` | Portfolio Societal Cost ratio, Base (omitted when SCB/SCH are off). Ratio. | `( SUM(ElecBen_SB) + SUM(GasBen_SB) + SUM(WaterEnergyBen_SB) + SUM(OtherBen) ) / SUM(SCBCost)` |
| `SCH_ratio` | Portfolio Societal Cost ratio, High (omitted when SCB/SCH are off). Ratio. | `( SUM(ElecBen_SH) + SUM(GasBen_SH) + SUM(WaterEnergyBen_SH) + SUM(OtherBen) ) / SUM(SCHCost)` |
| `PCT_ratio` | Portfolio Participant Cost Test ratio (gross). Ratio. | `SUM(PCTBen) / SUM(PCTCost)` |

Each `SUM` is over the group's measures. A ratio is omitted when a column it needs is blank or its denominator is 0. **Inputs:** the measure-level table only.

- Example A (a portfolio of just that measure), `measures`: `COUNT` of one row = **1**
- Example A, `TRC_ratio`: (5841.4973 + 3454.0256 + 30.6679 + 160.00 + 400.00) / 20916.4229 = **0.4727**
- Example A, `PAC_ratio`: (5841.4973 + 3454.0256 + 30.6679 + 160.00) / 8785.4865 = **1.0798**
- Example A, `TRCBen`: `SUM` of one row = **9,886.19**
- Example B, `RIM_ratio`: (0.0000 + 6990.5647 + 0.0000 + 320.00 + 20764.8592 + 0.00) / 32499.6332 = **0.8639**
- Example B, `SCB_ratio`: (0.00 + 8827.1057 + 0.00 + 320.00) / 60501.5955 = **0.1512**
- Example B, `TotalSystemBenefit`: `SUM` of one row = **3,012.77**

### 2.3 `components_summary_acc<version>` (components mode)

One row per component of each fuel, then a check row per fuel. The measure-level `ElecValue_<component>` and `GasValue_<component>` columns (1.13) are the inputs.

| Field | Meaning and unit | Formula |
|---|---|---|
| `fuel` | `Electric` or `Gas`. Text. | |
| `component` | The component's name (for example `energy`, `transmission`), or `(check) total minus sum of components`. Text. | |
| `portfolio_value_usd` | The portfolio's present value of that component. The check row is the fuel's total minus the sum of its components, which is 0 when the components add up. $ (PV). | component row: `SUM over all measures of ElecValue_<component>` (or `GasValue_<component>`); check row: `SUM(ElecBen) - SUM(ElecSupplyCost) - SUM over components c of portfolio_value_usd(c)` (gas: `SUM(GasBen) - SUM(GasSupplyCost) - ...`) |
| `share_of_fuel_total` | The component's share of its fuel's total (blank on the check row). Fraction. | `portfolio_value_usd(c) / SUM over the fuel's components of portfolio_value_usd` |

- Example A (a one-measure portfolio), `ElecValue_energy`: (the energy component is 70 of the 80 dollars per MWh of the TRC row) 70 / 80 * 5841.4973 = **5,111.31**
- Example A, `ElecValue_transmission`: 5 / 80 * 5841.4973 = **365.09**
- Example A, `ElecValue_distribution`: 5 / 80 * 5841.4973 = **365.09**
- Example A, `GasValue_market`: (the market component is 0.60 of the 1.00 dollars per therm of the TRC row) 0.60 / 1.00 * 3454.0256 = **2,072.42**
- Example A, `GasValue_co2`: 0.10 / 1.00 * 3454.0256 = **345.40**

### 2.4 `portfolio_8760_acc<version>` (combined 8760 mode)

The portfolio's net electric savings hour by hour, for each requested calendar year `Y` (the "Year(s)" setting, `profile_years`; default the First Year).

| Field | Meaning and unit | Formula |
|---|---|---|
| `hour_of_year` | The ACC hour index, 1 = 1 January 00:00. 1-8760. | `1 ... 8760` |
| `datetime` | That index laid on the ACC's calendar `A`: 2018 for the 2024/2026 ACCs, 2020 for the 2021/2022 ACCs (every forecast year of an ACC follows one calendar's weekdays, and the shapes are aligned to it; a 2020 calendar includes 29 February and ends on 30 December). Timestamp. | `A-01-01 00:00 plus (hour_of_year - 1) hours` |
| `net_kwh_<Y>` | The portfolio's net kWh in that hour of calendar year `Y`, all IOUs. Positive = savings, negative = added load. kWh. | `SUM over measures m of weight_m(Y) * shape_m,Y(i)`, `i` = `hour_of_year` |
| `net_kwh_<Y>_<PA>` | The same for the measures of one IOU territory `PA`. kWh. | the same sum over the measures with `IOU_AC_Territory` = `PA` |
| `net_kwh_<Y>_CZ<zone>` | The same for the measures of one climate zone (BRD BR-06.13): the measure's `AC_Climate_Zone` as filed, `CZ` prepended when it is a bare number (`13` -> `CZ13`; `System` -> `CZSystem`). The zones add up to `net_kwh_<Y>`. kWh. | the same sum over the measures with that `AC_Climate_Zone` |

**Where**

```text
start_m = Y0 + (q0 - 1) / 4                        the claim quarter as a fraction of a year
end_m   = start_m + EUL_Yrs
overlap(A, B) = the length in years of the overlap of the two intervals A and B

if 0 < RUL_Yrs < EUL_Yrs:
   weight_m(Y) = overlap( [Y, Y+1), [start_m, start_m + RUL_Yrs) ) * AC_Net_kWh_ScaledProfile_1st_Baseline
                 + overlap( [Y, Y+1), [start_m + RUL_Yrs, end_m) ) * AC_Net_kWh_ScaledProfile_2nd_Baseline
otherwise:
   weight_m(Y) = overlap( [Y, Y+1), [start_m, end_m) ) * AC_Net_kWh_ScaledProfile_1st_Baseline

shape_m,Y(i) = the measure's 8,760 shape for hour i, rotated onto the ACC's weekdays (0.6); the same in every year Y
```

`AC_Net_kWh_ScaledProfile_*_Baseline` is the quantity the electric pricing multiplies the profile by (0.5): `AC_Net_kWh_1st_Baseline` (`2nd`) on a share profile, the net kWh
with installation and realization rates and without market effects (market effects are an accounting adder, not physical savings, so they are
not in this profile). A measure with no 8,760 profile is left out and listed in 2.5. **Inputs:** the fields of `AC_Net_kWh_1st_Baseline` (0.5), `EUL_Yrs`, `RUL_Yrs`, `ClaimYearQuarter`, `IOU_AC_Territory`,
the electric-profile fields of `ElecBen`; the "Year(s)" setting.

- Example A, `net_kwh_2028`: overlap([2028, 2029), [2028.5, 2030.9)) = 0.5, so 0.5 * 34200 / 8760 = **1.9521** kWh in every hour (a flat shape)
- Example A, `net_kwh_2029`: overlap([2029, 2030), [2028.5, 2030.9)) = 1, so 1 * 34200 / 8760 = **3.9041** kWh in every hour
- Example A, `net_kwh_2030`: overlap([2030, 2031), [2028.5, 2030.9)) = 0.9, so 0.9 * 34200 / 8760 = **3.5137** kWh in every hour

### 2.4b `portfolio_8760_peaks_acc<version>` (combined 8760 mode)

The peaks of the combined profile (BRD BR-06.13): one row per year `Y` and group, the portfolio first, then each IOU territory, then each climate zone.

| Field | Meaning and unit | Formula |
|---|---|---|
| `year` | The calendar year `Y`. | the "Year(s)" setting |
| `group`, `value` | `portfolio` / `all measures`, `IOU territory (PA)` / the PA, or `climate zone` / the zone (`CZ13` ...). Text. | |
| `net_kwh` | The group's net kWh over the year. kWh. | `SUM over hours of the group's column of 2.4` |
| `peak_hour_of_year`, `peak_datetime`, `peak_kwh` | The hour of the group's largest net savings, on the ACC calendar, and its kWh (its own, non-coincident peak). | `argmax` and `max` of the group's column |
| `max_added_load_hour_of_year`, `max_added_load_kwh` | The hour of the group's largest added load (most negative hour, from fuel substitution) and its kWh; blank and 0 when no hour is negative. | `argmin` and `min` of the column, when below 0 |
| `kwh_at_portfolio_peak` | The group's kWh in the portfolio's peak hour (coincident with the portfolio peak). kWh. | the group's column at the portfolio's `peak_hour_of_year` |
| `share_of_portfolio_peak` | That as a share of the portfolio's peak kWh; the PAs (and the zones) add up to 1. | `kwh_at_portfolio_peak / portfolio peak_kwh` |

The peak is the hour of the largest net savings in the combined profile, not a system-peak definition such as the DEER peak period: use the hourly columns of 2.4 for any
other definition.

### 2.5 `portfolio_8760_unshaped_acc<version>` (only when some measures have no shape)

The measures left out of the combined profile because they have no 8,760 profile.

| Field | Meaning and unit | Formula |
|---|---|---|
| `CEInputID` | The measure. Text. | `CEInputID` |
| `IOU_AC_Territory`, `AC_Elec_Impact_Profile`, `AC_Elec_Target_Sector`, `AC_Climate_Zone` | The measure's key fields (`AC_Elec_Impact_Profile`: the 2nd-baseline part's own profile when it has one). Text. | the input fields |
| `kwh_<Y>` | The annual-equivalent net kWh the measure delivers in year `Y`: its share of the "unshaped" total. kWh. | `weight_m(Y)` of 2.4 |

### 2.6 `cet_comparison_measures_acc<version>` and `cet_comparison_summary_acc<version>` (with a CET outputs file)

The tool's measure-level table against a real CET run's outputs file, matched on `CEInputID` (= CET's `CET_ID`); only measures in both are compared. `c` is one of `TotalSystemBenefit`, `ElecBen`, `GasBen`, `WaterEnergyBen`,
`WaterEnergyBen_SB`, `WaterEnergyBen_SH`, `WaterEnergyBenGross`, `OtherBen`, `ElecSupplyCost`, `GasSupplyCost`, `WaterEnergyCost`, `TRCCost`, `PACCost`, `RIMCost`, `SCBCost`, `SCHCost`, `TRCRatio`, `PACRatio`, `RIMRatio`,
`FirstYearNetElecCO2`, `FirstYearNetGasCO2`, `LifecycleBillReducElec`, `LifecycleBillReducGas` (those present in both).

| Field | Meaning and unit | Formula |
|---|---|---|
| `CEInputID`, `PrgID`, `MeasDescription` | The measure (per-measure file). Text. | copied from the measure-level table |
| `<c>_poc`, `<c>_cet` | The tool's and CET's value of column `c`. As `c`. | the measure-level column `c`; CET's column `c` |
| `<c>_diff` | The tool minus CET. As `c`. | `<c>_poc - <c>_cet` |
| `<c>_ratio` | The tool over CET (blank where CET is 0). Ratio. | `<c>_poc / <c>_cet` |
| `metric`, `POC`, `CET`, `POC/CET` | The summary file: each non-ratio column's portfolio total in both and their ratio (blank where CET's total is 0). $ or metric tons; ratio. | `metric` = `c`; `POC` = `SUM(<c>_poc)`; `CET` = `SUM(<c>_cet)`; `POC/CET` = `POC / CET` |

### 2.7 `comparison_acc<A>_vs_acc<B>` and `comparison_summary_acc<A>_vs_acc<B>` (two-version run)

The same job priced under two ACC versions `A` and `B`, matched on `CEInputID` (a measure in only one run is kept, with the other side blank). `c` is one of `ElecBen`, `GasBen`, `WaterEnergyBen`, `OtherBen`,
`TotalSystemBenefit`, `ElecSupplyCost`, `WaterEnergyCost`, `TRCCost`, `PACCost`, `RIMCost`, `SCBCost`, `SCHCost`, `TRCBen`, `PACBen`, `RIMBen`, `SCBBen`, `SCHBen`, `TRCRatio`, `PACRatio`, `RIMRatio`, `SCBRatio`, `SCHRatio`, `PCTBen`, `PCTCost`, `PCTRatio`, `FirstYearNetElecCO2`, `FirstYearNetGasCO2`, `LifecycleBillReducElec`,
`LifecycleBillReducGas` (those present in both).

| Field | Meaning and unit | Formula |
|---|---|---|
| `CEInputID`, `PrgID`, `MeasDescription`, `IOU_AC_Territory` | The measure. Text. | copied from the measure-level table |
| `<c>_acc<A>`, `<c>_acc<B>` | Column `c` under each ACC version. As `c`. | the measure-level column `c` of each run |
| `<c>_delta` | The change from A to B. As `c`. | `<c>_acc<B> - <c>_acc<A>` |
| `<c>_pct` | The relative change (blank where A is 0). Fraction. | `( <c>_acc<B> - <c>_acc<A> ) / abs( <c>_acc<A> )` |
| `ElecPricingStatus_acc<A>`, `ElecPricingStatus_acc<B>`, `GasPricingStatus_acc<A>`, `GasPricingStatus_acc<B>` | The pricing status under each version (1.3). Text. | the measure-level columns of each run |
| `metric`, `acc<A>`, `acc<B>`, `delta`, `pct` | The summary file: each non-ratio column's portfolio total under both versions. $ or metric tons; fraction. | `metric` = `c`; `acc<A>` = `SUM(<c>_acc<A>)`; `acc<B>` = `SUM(<c>_acc<B>)`; `delta` = `acc<B> - acc<A>`; `pct` = `delta / abs(acc<A>)` (blank where `acc<A>` is 0) |

### 2.7b `_results/measure_level_acc<version>.parquet`

When the output format is `csv` or `csv.zip`, or the run has an output template (then in every format, with every column), the measure-level table is written as parquet first and the CSV is made from it; the parquet is kept in the run
folder's `_results` folder, and the Results tables (window and web app) read it rather than the CSV. Same columns and values as the measure-level file. The web
app does not list it for download.

The same folder holds `group_sums2_<results file>.parquet`: for each distinct (PA, `PrgID`, program name, program sector, program segment, program year), the number of
measures and the sum of every additive numeric output. The Results tables build every level above the measure from it (ratios recomputed from the sums). The
run writes it at the end (a run in parts adds it up part by part as it writes them, so the file is not read back); the Results tables rebuild it if it is missing or older than the results file. Not listed for download.

### 2.8 `preflight.txt`

The input check that ran before pricing: a header (`PREFLIGHT: no blocking problems` or the number of blocking problems), the job's statistics (measure counts, claim years, First Year, how many measures use each electric shape
source, water and gas measure counts, ACC year ranges), then every finding as `ERROR` / `WARNING` / `INFO`, its code, count, the first-year net kWh or therms its records carry (`no_electric_profile`, `loose_profile_match`,
`no_gas_profile`) and examples (for `loose_profile_match`: end use | IOU | the AC_Elec_Target_Sector given). Finding codes: `file_missing`, `no_delimiter`, `too_many_records` (the job has more records than the record cap, `max_records`, 2,000,000 by default; nothing else is checked), `not_pipe_delimited` (info: the file is comma- or tab-delimited and was read as such), `missing_columns`, `missing_descriptive_column` (warning: `MeasDescription` or `Sector` left out, read as blank),
`missing_optional_column`, `blank_id`, `duplicate_id`, `bad_claim_year_quarter`, `bad_iou`, `non_numeric`, `blank_required_value` (a numeric field CET requires is blank), `negative_life` (`EUL_Yrs` or `RUL_Yrs` below 0), `rul_above_eul` (`RUL_Yrs` greater than `EUL_Yrs`), `outside_cet_range`, `rul_not_below_eul` (warning: `RUL_Yrs` equals `EUL_Yrs`), `measures_without_program_costs` (warning: measures whose `PrgID` has no `ProgramCost.csv` row, priced with no program costs), `program_costs_without_measures` (warning: `ProgramCost.csv` rows whose `PrgID` has no measures, with the nominal dollars no cost test includes; C17 item 11), `inputs_unreadable`,
`no_program_costs`, `statewide_program_costs`, `bad_acc_version`, `acc_version_file_missing`, `no_electric_profile`, `no_electric_profile_2nd`, `no_parent_profile` (a true-up's parent profile matched nothing, C21), `parent_2nd_without_parent`, `loose_profile_match`, `kwh_per_unit_profile` (info: priced on an already-scaled kWh-per-unit profile), `zero_net_profile_water`, `no_profile_library`, `no_water_profile`, `no_gas_profile`, `bad_gas_sector`, `unknown_settings_version`, `before_acc_horizon`,
`beyond_acc_horizon`, `acc_unreadable`, `program_costs_unreadable` (a job checked in parts could not read `ProgramCost.csv`), and from the program metadata `program_not_in_metadata`, `claim_outside_program_years` (claim year before `StartYear` or after `EndYear`),
`program_closed` (status `closed` or `closure_pending`), `program_excluded_from_ce` and `program_pa_differs` (the `ProgramCost.csv` `PA` differs from the list's `PA`),
and with CEDARS filing checks `cet_reject` (warning: the records failing one rule CEDARS rejects on, the message starting with the rule's ID), `cet_warning` (the same for a
CEDARS warning rule) and `cet_rule_not_checked` (info: a rule ReCET could not check, and why). With filing checks on, `outside_cet_range` is not reported: the spec's ranges are
checked under their own IDs. The statistics then end with `CET filing validation` (the rule set, and how many measures CEDARS would reject or warn on).
Errors stop the run; warnings and info do not.

### 2.8b `preflight_findings.xlsx`

The same input check as a workbook to review and filter, written by every run (a preflight-only run and a run stopped by a blocking problem included):

| Sheet | Columns |
|---|---|
| `Summary` | `Item`, `Value`: the result (whether the run can price), the number of errors, warnings and information findings, then the statistics of `preflight.txt`. |
| `Findings` | `Severity` (`ERROR` stops the run, `WARNING`, `INFO`), `Code` (the codes of 2.8), `Records` (how many records it concerns), `Message`, `First-year net kWh` and `First-year net therms` (the energy those records carry, for `no_electric_profile` and `loose_profile_match` (kWh: NumUnits x UnitkWh1stBaseline x NTGRkWh x installation and realization rates) and `no_gas_profile` (therms, the same with the therm fields); blank for the other codes), `Examples`; errors first. |
| `Cells` | One row per flagged cell: `Severity`, `Code`, `File` (`Measure.csv` or `ProgramCost.csv`), `Line` (the line of the file; line 1 is the header), `Record` (its `CEInputID`, or `PrgID` and `PrgYear`) and `Column`. Up to 5,000 cells per finding. |

`preflight.json` holds the same report for the web app (its Validate inputs panel reads it). The web app's downloads leave `preflight.txt` and `preflight.json` out: the
input check is in the run log (`run_log.txt`, everything the run printed) and in this workbook.

### 2.9 `run_settings.json`

Everything needed to reproduce the run.

| Key | Meaning |
|---|---|
| `package_version`, `finished`, `seconds` | The `recet` version, the completion time, and the run time in seconds. |
| `config` | Every run setting (the keys of INPUT_FIELDS.md section 3), exactly as used. |
| `acc` | The resolved ACC: `key`, `label`, `avoided_cost_version` (CET's "Avoided Cost Version" number), `settings_acc_version` (the ACC whose run parameters and CET tables were used), the electric and gas file paths with their `_source` (`built-in`, or that a supplied file replaced it) and `electric_size`, and any `notes` (for example that a new label borrowed a built-in file). |
| `reference_dir` | The reference data folder used, how it was chosen, and the status (found / size) of each large file. |
| `supplied_files` | Each supplied profile / ACC file with its path and size. |
| `inputs` | The measure and program-cost paths, the number of measures priced, and the First Year used. |
| `totals` | The portfolio totals (the first row of the portfolio summary). |
| `parts` | A run in parts only: `chunk_size` and the number of parts (`count`). |
| `notes` | Notes on the run, when there are any (for example a TRC-only ACC's blank societal columns, or Excel output written as CSV). |
| `filing_validation` | With CEDARS filing checks: the rule set used (`rule_set`, `label`, `effective_date`), its number of rules, rejects and warnings, and the CEDARS files it was read from (BRD BR-03.3). |
| `cost_tests` | The cost-test definition set the run priced on ([COST_TESTS.md](COST_TESTS.md)): `id`, `workstream`, `version`, `label`, `draft`, `source` (`built-in` or the supplied file's path), `sha256` (of the set's file), `program_years` (the years it is in force for), `tests`, `used_label` (what `Used_CostTestSet` says), `chosen` (`run option`, or the program year whose set was taken by default) and any `warnings` (a draft set, a set not in force for the job's program years). |
| `acc_check` | The ACC check (2.9b, BRD BR-06.5): `purpose` and its `label` (what the run is priced for), the number of `measures`, how many were `flagged` (priced on an ACC that does not normally apply to their program year) and `not_checked`, and `by_acc_and_year`: one row per ACC version and program year with its `measures`, `status` (`ok`, `flagged` or `not checked`) and `expected_accs`. |

### 2.9b The ACC check (BRD BR-06.5)

BRD BR-06.5: whether the ACC a measure was priced on normally applies to its program year (its claim year), for what the run is priced for (the run setting
`acc_purpose`, "Priced for"). The ACC is the measure's `ACCVersion`, else the run's. Which ACCs apply comes from the table of budget filings and the ACC that
prices each (`versions.BUDGET_FILING_ACC`, from CET's "Program year assignment to CET avoided cost version" table through 2027, then the schedule of
expected filings): for *Quarterly Claims*, the ACC of the program year's final budget filing (one ACC per year, `versions.claims_acc`); for a named filing, that
filing's ACC within its program years; for *Any filing* (the default), the ACC of any filing that covers the year (2028-2029: the 2024 or the 2026 ACC). It changes no priced value: a flagged measure is still priced on its ACC.

A measure is `ok` if the ACC normally applies to its program year, flagged if it does not, and `not checked` if the table says nothing about the year (before 2020, after
the last filing, or claims after 2035) or the version is a label that names no ACC year. The flagged measures are in the input check (`acc_not_for_program_year`:
preflight.txt, and each record in the Cells sheet of `preflight_findings.xlsx`), and `run_settings.json` counts them by ACC and program year (`acc_check`, 2.9).
(Before 0.1.50 the measure-level table also carried an `ACCCheck` column.)

### 2.9c `cost_tests.yaml`

The cost-test definition set the run priced on ([COST_TESTS.md](COST_TESTS.md)), copied byte for byte (its SHA-256 is run_settings.json's
`cost_tests.sha256`), built-in or supplied. The portfolio summary, key metrics, field definitions, the Results page and the run page take their tests
from it, so a run on a custom set reads the same months later. A run written before 0.1.85 has none and is read as EE-2026, which every earlier release
priced.

### 2.10 `filing_validation_acc<version>` (with CEDARS filing checks)

With **CEDARS filing checks** set to *Quarterly Claims* or *Budget filing* (`filing_validation`), preflight applies CEDARS' own upload checks for that filing to
every record (`filing_rules.py`): the `validation_rules` and `warning_rules` files and the field constraints of the table spec that CEDARS publishes on its spec
pages, shipped unchanged in the package (`recet/data/cet_rules/<set>_<effective date>/`). A record CEDARS would reject is still priced.

One row per record that fails at least one CEDARS upload check, written by preflight, so a preflight-only run has it too. (Before 0.1.50 the measure-level table
also carried `FilingValidation` and `FilingRules`; they are now only here.)

| Column | Meaning |
|---|---|
| `rule_set` | The rule set and its effective date, e.g. `Quarterly Claims rules of 2026-03-12`. |
| `table` | `Measure` or `ProgramCost`. |
| `record` | The `CEInputID`, or for `ProgramCost` the `PrgID` and `PrgYear`. |
| `FilingValidation` | `reject` if the record fails at least one validation rule or spec constraint (CEDARS would not accept the upload with it), `warning` if it fails only warning rules (CEDARS accepts it with a warning). |
| `FilingRules` | The IDs of the rules the record fails, rejects first, comma-separated. An ID is `V` (validation rule), `W` (warning rule) or `S` (spec constraint) and six hex digits built from the rule's content, so a rule keeps its ID across the two sets and across versions. |
| `rules_failed` | Each failing rule's ID and text, separated by ` \| `. |

How the rules are read, where CEDARS' own code is not published: a rule applies to the records its `Where` conditions select (all of them, or any for `Where_connector = any`);
a blank value makes a comparison unknown, as in SQL, so a condition on it does not hold and a test on it fails; text compares case-insensitively; a value list is checked for the
record's claim year. CET's `AvoidedCostCombo` list (22 MB) is not shipped: that rule is checked against the CEDARS impact-profile library (exact end use, IOU and target sector),
and reported as not checked when the library is not installed. `UploadPA-YYYY` (budget filing `CEInputID`) is a prefix: a PA code, a dash and the filing year, as in
`PGE-2028-...` or `PGE-2028Q4-...`. Checked on the 100,000 records of PG&E's 2028-2031 consolidated filings, which CEDARS accepted: no record is rejected under either set.

### 2.11 `field_definitions_acc<version>`

One row per column of the measure-level file, in the file's order (BRD BR-06.10): what each column is, from this document (section 1) or, for an echoed
input column, from INPUT_FIELDS.md. With an output template it lists exactly the template's columns.

| Column | Meaning |
|---|---|
| `position` | The column's place in the measure-level file, from 1. |
| `column` | The column's name. |
| `definition` | The first paragraph of its entry (or its table row), as plain text. |
| `documentation` | The entry: the document and the heading id, e.g. `OUTPUT_FIELDS.md#elecben` (on the web app, `/docs/OUTPUT_FIELDS.md#elecben`). |
| `unit` | The unit its entry gives ("Dollars (PV)", "kWh", "Metric Tons"); blank when the entry names none (inputs, labels). |
| `source` | Where the value comes from: `input: Measure.csv`, the CEDARS Programs list (`Prg_` columns), or a ReCET calculation and what it works on (ACC avoided costs, emission factors, retail rates, `ProgramCost.csv` totals). |
| `version` | `as filed (CET input field)` for an input; else the ReCET version, plus the run's ACC ("2026 ACC") for a value priced on it. A record with its own `ACCVersion` is priced on that ACC. |
| `cost_tests` | How the tests of the run's cost-test set use it (section 1.9): `benefit in TRC, PAC, RIM`, `cost in RIM`, both (`benefit in PCT; cost in TRC, PAC, RIM, PCT` for `RebatesandIncentsPV`), a test's numerator, denominator or result, or `not used in a cost test`. The benefits and costs listed for a test add up to its `<test>Ben` and `<test>CostNoAdmin` (`<test>Cost` for a test without program costs, PCT; PCT's `TaxCreditsGrossPV` is priced but not written). |
| `time_basis` | First year, average year, lifecycle undiscounted, lifecycle present value to the run's First Year, nominal dollars, a ratio of present values, or levelized. |
| `aggregation` | How a group total (portfolio summary, Results levels) combines it: summed; recomputed from the summed numerator and denominator (ratios and levelized values); or not combined (inputs, labels, per-measure shares). |
| `rounding` | Always "not rounded": ReCET writes full precision; the Results pages and Excel formats only change how numbers are shown. |
| `years_in_use` | Which ACCs fill it ("2024 ACC on" for the SCT columns, blank on the TRC-only 2021 and 2022 ACCs; otherwise every ACC) and any earlier name of the column (renamed in 0.1.50 or from CET's E3 names). |

These attributes (BRD BR-08.1) follow rules on the column's name and its documented unit (`field_attributes.py`), so a column added later gets them
without an entry of its own; `tests/test_field_attributes.py` checks the cost-test treatment against the cost tests' arithmetic.

### 2.12 `key_metrics_acc<version>`

The key-metrics summary (BRD BR-06.10): the headline results and the main drivers of them. Built from every measure, whatever the output template.

| Column | Meaning |
|---|---|
| `section` | `Portfolio` (the headline metrics), `Top programs`, `Top program sectors` (the program's `PrimarySector`, 1.14) or `Top end uses` (the measure's `UseCategory`). |
| `item` | The metric's name, or the program (`PrgID` - program name), sector or end use. Up to 10 per driver section, largest share of total system benefit first (by size: a negative share counts too). |
| `measures` | The number of measures it covers. |
| `value` | Portfolio rows: the metric (total system benefit, budget, the ratio of each test of the run's cost-test set recomputed from the portfolio sums (EE-2026: TRC, PAC, RIM, SCT Base, SCT High and, since 0.1.85, PCT), the headline tests' costs (TRC and PAC), first-year net kWh / kW / therms, lifecycle CO2). A ratio whose cost is 0 is blank, as in the portfolio summary. |
| `TotalSystemBenefit`, `share_of_TSB` | Driver rows: the item's total system benefit and its share of the portfolio's. |
| `TRCCost`, `share_of_TRC_cost` | Driver rows: the item's cost in the set's first headline test (TRC in EE-2026; the columns are named for it) and its share of the portfolio's. |
| `TRC_ratio`, `PAC_ratio` | Driver rows: the item's ratios in the set's headline tests (TRC and PAC in EE-2026), recomputed from its sums. |
| `definition`, `documentation` | Portfolio rows: the metric's definition and its entry in this document, as in 2.11. |

### 2.13 `statewide_reconciliation_acc<version>` (with statewide allocation)

With **Allocate statewide programs** on (C15), the check that the allocation reconciles (BRD BR-06.7): for each statewide program and year, every field the
allocation scales, added up before allocation and over each IOU's allocated rows, from the rows the run actually priced. One row per (table, year, program, field).

| Column | Meaning |
|---|---|
| `table` | `Measure` or `ProgramCost`. |
| `Year` | The year the factors are looked up by: the claim year (`ClaimYearQuarter`), or `PrgYear` for a `ProgramCost` row without one. |
| `PrgID` | The statewide program as filed (`SW_...`). |
| `field` | The allocated field, e.g. `UnitkWh1stBaseline` or `AdminCostsOther`. |
| `factor` | Its rule: `Electric Factor`, `Gas Factor` or `Weighted Factor`. |
| `factor_sum` | That factor summed over PGE, SDGE, SCE and SCG for the program and year (1 when the table is complete). |
| `unallocated_total` | The field's total over the program's rows before allocation: `NumUnits` x the per-unit field for `Measure` (`NumUnits` is passed through), the field itself for `ProgramCost`. |
| `PGE`, `SDGE`, `SCE`, `SCG` | The same total over each IOU's allocated rows (`<IOU>_SW_...`); 0 for an IOU with no share. |
| `allocated_total` | The sum of the four IOU columns. |
| `difference` | `allocated_total - unallocated_total`. |
| `reconciles` | `True` when the difference is within 1e-9 of the unallocated total (relative; absolute below 1). |
| `note` | Why a field does not reconcile: every IOU's factor for that fuel is 0 for the program and year (the quantity is in no IOU's share; for example therms of a program whose gas factors are all 0), or the factors sum to something other than 1. |

The check is on the inputs because that is where allocation happens. Each IOU's share is then priced like any measure, and every benefit, cost and saving is
linear in the allocated quantities, so where a program's inputs reconcile its IOU shares' benefits add up to the unallocated program's (tested by pricing a
job both ways). Program costs reconcile in nominal dollars; their present values can differ because each IOU's share is discounted at that IOU's rate (C15).
A field that does not reconcile is also a note in the run log and in `run_settings.json` (the first 20). Nothing is stopped.

Also written into the run folder: `_prepared_inputs/` (a Parquet conversion of any supplied ACC that was not already Parquet) and, from the GUI, `_gui_job.json` in the output folder (the settings the GUI launched the run with).

---------------------------------------------------------------------------------------------------------------------------------

## 3. The two worked examples

Both are one-measure jobs on flat avoided costs and flat 8,760 load shapes, so every step can be checked with a calculator. Both are re-run by the test-suite (`tests/doc_examples.py`,
`tests/test_output_docs.py`), which compares every number below and in the `Example` lines of sections 1 and 2 with what the tool gives, and checks the arithmetic written on each line.

Common to both: PG&E, climate zone 13, the 2026 ACC (run parameters: `DiscountRateQtr` 0.01915, `DiscountRateAnnual` 0.0766, societal 0.012575 and 0.0503), First Year 2028, every ACC hour of a
year priced alike. Electric avoided costs, in dollars per MWh: `TRC` = energy 70 + transmission 5 + distribution 5 = 80 (so `AC` = 0.080 per kWh and `Gen` = 0.070 per kWh); `SCT Base` 88 (Gen 78); `SCT High` 96 (Gen 86);
the ACC's `emission_rate_tonnes_per_kwh` is 0.0002 in every hour. Gas avoided costs, in dollars per therm and the same in every month: `td` 0.20, `nox` 0.05, `upstream_methane` 0.03, `air_quality_adder` 0.02,
`behind_meter_methane` 0, `co2` 0.10 (`TRC`), 0.30 (`SCT` Base), 0.50 (`SCT` High), and `market` 0.60 for the Residential class and 0.50 for the Commercial class.

### 3.1 Example A: inputs

A residential efficiency measure claimed in 2028Q3: a single baseline, a fractional life, no market effects, not fuel substitution. Non-blank `Measure.csv` fields:

| Field | Value | Field | Value |
|---|---|---|---|
| `CEInputID` | `W1` | `PrgID` | `PRG1` |
| `ClaimYearQuarter` | 2028Q3 | `IOU_AC_Territory` | PGE |
| `AC_Climate_Zone` | 13 | `AC_Elec_Target_Sector` | Residential |
| `AC_Elec_Impact_Profile` | `W_SHAPE` (a flat profile, 1/8760 per hour) | `AC_Gas_Impact_Profile` | `Annual` (1/12 per month) |
| `AC_Gas_Target_Sector` | Residential | `NumUnits` | 100 |
| `EUL_Yrs` / `RUL_Yrs` | 2.4 / 0 | `UnitkWh1stBaseline` | 500 |
| `UnitTherm1stBaseline` | 20 | `UnitkWhIOUWater1stBaseline` | 3 |
| `NTGRkWh` / `NTGRkW` | 0.8 / 0.8 | `NTGRTherm` / `NTGRCost` | 0.9 / 0.9 |
| `InstallationRatekWh` / `RealizationRatekWh` | 0.95 / 0.9 | `InstallationRateTherm` / `RealizationRateTherm` | 1 / 0.9 |
| `UnitMeaCost1stBaseline` | 200 | `UnitIncentiveToOthers` / `UnitEndUserRebate` | 30 / 20 |
| `UnitDirectInstallLab` / `UnitDirectInstallMat` | 5 / 5 | `UnitRefrigCosts` / `UnitMiscBens` / `UnitTaxCredits` | 1 / 2 / 5 |

Program `PRG1` (`ProgramCost.csv`): 2028 row: `AdminCostsOverheadAndGA` 1500 and `MarketingOutreach` 500; 2029 row: `EMV` 1000.

### 3.2 Example A: quarter by quarter (TRC prices, primary discount rate)

`Elec(k)`, `Gas(k)` and `Wat(k)` are the phased annual quantities of 0.5 (no market effects, single baseline, so they are constant); each `V` column is one term of the sum in 0.7:
`w(k) * DF(k) * quantity * U(k)`, with `U_e(k)` = 0.080 * hours / 8760, `U_g(k)` = 1.00 * months / 12 and `U_w(k)` = 0.070 * hours / 8760. Quarter numbering and hours: 0.2.

<!-- oa-quarters-A:start -->
| k | Quarter | Hours | w(k) | DF(k) | Baseline | Elec(k) | V_elec term | Gas(k) | V_gas term | Wat(k) | V_water term |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 0 | 2028Q3 | 2208 | 1 | 0.962773 | 1st | 34,200.0 | 663.95 | 1,620.0 | 389.92 | 205.20 | 3.486 |
| 1 | 2028Q4 | 2208 | 1 | 0.944682 | 1st | 34,200.0 | 651.47 | 1,620.0 | 382.60 | 205.20 | 3.420 |
| 2 | 2029Q1 | 2160 | 1 | 0.926931 | 1st | 34,200.0 | 625.34 | 1,620.0 | 375.41 | 205.20 | 3.283 |
| 3 | 2029Q2 | 2184 | 1 | 0.909514 | 1st | 34,200.0 | 620.40 | 1,620.0 | 368.35 | 205.20 | 3.257 |
| 4 | 2029Q3 | 2208 | 1 | 0.892424 | 1st | 34,200.0 | 615.44 | 1,620.0 | 361.43 | 205.20 | 3.231 |
| 5 | 2029Q4 | 2208 | 1 | 0.875655 | 1st | 34,200.0 | 603.87 | 1,620.0 | 354.64 | 205.20 | 3.170 |
| 6 | 2030Q1 | 2160 | 1 | 0.859202 | 1st | 34,200.0 | 579.64 | 1,620.0 | 347.98 | 205.20 | 3.043 |
| 7 | 2030Q2 | 2184 | 1 | 0.843057 | 1st | 34,200.0 | 575.07 | 1,620.0 | 341.44 | 205.20 | 3.019 |
| 8 | 2030Q3 | 2208 | 1 | 0.827216 | 1st | 34,200.0 | 570.47 | 1,620.0 | 335.02 | 205.20 | 2.995 |
| 9 | 2030Q4 | 2208 | 0.6 | 0.811672 | 1st | 34,200.0 | 335.85 | 1,620.0 | 197.24 | 205.20 | 1.763 |
<!-- oa-quarters-A:end -->

Sums: `V_elec` = 5,841.50, `V_gas` = 3,454.03, `V_water` = 30.67. For example, row 0: 34200 x 0.080 x 2208 / 8760 x 0.962773 = 663.95.

### 3.3 Example B: inputs

A commercial gas-to-electric heat pump claimed in 2028Q2: fuel substitution (`FuelID` `FuelSub-ToElec`, so it adds electric load and water use and saves gas), a second baseline that starts part-way through a quarter,
a winter-only gas shape, 5% market effects on benefits and on costs (`MEB` = `MEC` = 0.05), and electric NOx / PM10 and gas NOx rows. Non-blank `Measure.csv` fields:

| Field | Value | Field | Value |
|---|---|---|---|
| `CEInputID` | `W1` | `PrgID` | `PRG2` |
| `ClaimYearQuarter` | 2028Q2 | `IOU_AC_Territory` | PGE |
| `AC_Climate_Zone` | 13 | `AC_Elec_Target_Sector` | Commercial |
| `AC_Elec_Impact_Profile` | `3 = Commercial HVAC` (a flat profile) | `AC_Gas_Impact_Profile` | `Winter Only` (1/6 in Jan, Feb, Mar, Oct, Nov, Dec) |
| `AC_Gas_Target_Sector` | Commercial | `NumUnits` | 10 |
| `EUL_Yrs` / `RUL_Yrs` | 2.5 / 1.125 | `FuelID` / `MeasImpactType` | `FuelSub-ToElec` / `Deem-FuelSub` |
| `UnitkWh1stBaseline` / `UnitkWh2ndBaseline` | -4000 / -2500 | `UnitTherm1stBaseline` / `UnitTherm2ndBaseline` | 600 / 400 |
| `UnitkWhIOUWater1stBaseline` / `...2ndBaseline` | -6 / -4 | `CombustionType` | `Small Boilers (<100 MMBtu/hr Heat Input):Uncontrolled` |
| `NTGRkWh` / `NTGRkW` | 0.75 / 0.75 | `NTGRTherm` / `NTGRCost` | 0.85 / 0.8 |
| `InstallationRatekWh` / `RealizationRatekWh` | 1 / 0.9 | `InstallationRateTherm` / `RealizationRateTherm` | 1 / 0.95 |
| `UnitMeaCost1stBaseline` / `UnitMeaCost2ndBaseline` | 9000 / 6000 | `MeasInflation` | 0.02 |
| `UnitIncentiveToOthers` / `UnitEndUserRebate` | 800 / 400 | `UnitDirectInstallLab` / `UnitDirectInstallMat` | 100 / 50 |
| `UnitGasInfraBens` / `UnitRefrigBens` / `UnitMiscBens` | 20 / 8 / 12 | `UnitRefrigCosts` / `UnitMiscCosts` / `UnitTaxCredits` | 10 / 5 / 100 |

Program `PRG2`: 2028 row: `EMV` 900 and `DIActivity` 300; 2029 row: `MarketingOutreach` 600. The Emissions table gives PG&E commercial zone 13 `3 = Commercial HVAC` a `NOx` and a `PM10` rate of
0.00004643 pounds per kWh; the CombustionType table gives `Small Boilers (<100 MMBtu/hr Heat Input):Uncontrolled` a NOx factor of 0.0098 pounds per therm.

### 3.4 Example B: quarter by quarter (TRC prices, primary discount rate)

Same columns as 3.2, plus `Baseline`: which baseline the quarter is on (0.4; `RUL_Yrs` 1.125 gives `RQ` = 4.5, so quarter 4 is the half-and-half blend). The quantities include the market-effects scaling
(0.5), for example `Elec(0)` = -27000 x (0.75 + 0.05) / 0.75 = -28800. The gas price `U_g(k)` is 0.90 (the Commercial `TRC` total) times the share of the year that the quarter's months carry: 0 for Q2 and Q3, and 3 x 1/6 = 0.5 for Q1 and Q4.

<!-- oa-quarters-B:start -->
| k | Quarter | Hours | w(k) | DF(k) | Baseline | Elec(k) | V_elec term | Gas(k) | V_gas term | Wat(k) | V_water term |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 0 | 2028Q2 | 2184 | 1 | 0.981210 | 1st | -28,800.0 | -563.63 | 5,130.0 | 0.00 | -43.20 | -0.740 |
| 1 | 2028Q3 | 2208 | 1 | 0.962773 | 1st | -28,800.0 | -559.12 | 5,130.0 | 0.00 | -43.20 | -0.734 |
| 2 | 2028Q4 | 2208 | 1 | 0.944682 | 1st | -28,800.0 | -548.61 | 5,130.0 | 2,180.80 | -43.20 | -0.720 |
| 3 | 2029Q1 | 2160 | 1 | 0.926931 | 1st | -28,800.0 | -526.60 | 5,130.0 | 2,139.82 | -43.20 | -0.691 |
| 4 | 2029Q2 | 2184 | 1 | 0.909514 | blend 0.500 | -23,400.0 | -424.49 | 4,275.0 | 0.00 | -36.00 | -0.571 |
| 5 | 2029Q3 | 2208 | 1 | 0.892424 | 2nd | -18,000.0 | -323.91 | 3,420.0 | 0.00 | -28.80 | -0.453 |
| 6 | 2029Q4 | 2208 | 1 | 0.875655 | 2nd | -18,000.0 | -317.83 | 3,420.0 | 1,347.63 | -28.80 | -0.445 |
| 7 | 2030Q1 | 2160 | 1 | 0.859202 | 2nd | -18,000.0 | -305.08 | 3,420.0 | 1,322.31 | -28.80 | -0.427 |
| 8 | 2030Q2 | 2184 | 1 | 0.843057 | 2nd | -18,000.0 | -302.67 | 3,420.0 | 0.00 | -28.80 | -0.424 |
| 9 | 2030Q3 | 2208 | 1 | 0.827216 | 2nd | -18,000.0 | -300.25 | 3,420.0 | 0.00 | -28.80 | -0.420 |
<!-- oa-quarters-B:end -->

Sums: `V_elec` = -4,172.17, `V_gas` = 6,990.56, `V_water` = -5.63.

### 3.5 Every output column, both examples

The value of every measure-level column that is not a copied input, for both examples (6 significant digits; `(blank)` = empty). It is the complete set of derived pricing inputs, benefits, costs, ratios and emissions the entries of section 1
refer to.

<!-- oa-all:start -->
| Field | Example A | Example B |
|---|---|---|
| `Used_ACCVersion` | 2026 ACC (Avoided Cost Version 2028) | 2026 ACC (Avoided Cost Version 2028) |
| `Used_CostTestSet` | EE-2026 | EE-2026 |
| `Used_Year` | 2028 | 2028 |
| `Used_Quarter` | 3 | 2 |
| `AC_Net_kWh_1st_Baseline` | 34200 | -27000 |
| `AC_Net_kWh_2nd_Baseline` | 0 | -16875 |
| `AC_Net_Therms_1st_Baseline` | 1620 | 4845 |
| `AC_Net_Therms_2nd_Baseline` | 0 | 3230 |
| `AC_Net_kWh_Water_1st_Baseline` | 205.2 | -40.5 |
| `AC_Net_kWh_Water_2nd_Baseline` | 0 | -27 |
| `Fuel_Sub_Flag` | false | true |
| `Scaled_Profile_Flag` | false | false |
| `ElecPricingStatus` | priced: hourly profile | priced: hourly profile |
| `ProfileSource` | user profile | user profile |
| `SystemClimateZone` | (blank) | (blank) |
| `SystemZoneSource` | (blank) | (blank) |
| `GasPricingStatus` | priced | priced |
| `GasProfileSource` | library | library |
| `WaterPricingStatus` | priced | priced |
| `ElecBen` | 5841.5 | 0 |
| `ElecSupplyCost` | 0 | 4172.17 |
| `GasBen` | 3454.03 | 6990.56 |
| `GasSupplyCost` | 0 | 0 |
| `WaterEnergyBen` | 30.6679 | 0 |
| `WaterEnergyCost` | 0 | 5.62586 |
| `OtherBen` | 160 | 320 |
| `OtherCost` | 80 | 120 |
| `TaxCredits` | 400 | 800 |
| `OtherBenGross` | 200 | 400 |
| `OtherCostGross` | 100 | 150 |
| `TaxCreditsGross` | 500 | 1000 |
| `RebatesandIncents` | 6000 | 13500 |
| `RebatesandIncentsPV` | 5776.64 | 13246.3 |
| `NetParticipantCostPV` | 12130.9 | 37937 |
| `MarkEffectPlusExcessIncPV` | 0 | 3033.38 |
| `NetParticipantCostAdjustedPV` | 12130.9 | 40970.4 |
| `GrossParticipantCostPV` | 13478.8 | 47421.2 |
| `IncentiveToOthers` | 3000 | 8000 |
| `DILaborCost` | 500 | 1000 |
| `DIMaterialCost` | 500 | 500 |
| `EndUserRebate` | 2000 | 4000 |
| `GrossMeasureCost` | 20000 | 60000 |
| `ExcessIncentives` | 0 | 0 |
| `MarkEffectPlusExcessInc` | 0 | 3000 |
| `GrossParticipantCost` | 14000 | 46500 |
| `GrossParticipantCostAdjusted` | 14000 | 49500 |
| `NetParticipantCost` | 12600 | 37200 |
| `NetParticipantCostAdjusted` | 12600 | 40200 |
| `GrossMeasCostPV` | 19255.5 | 60667.6 |
| `ExcessIncentivesPV` | 0 | 0 |
| `GrossParticipantCostAdjustedPV` | 13478.8 | 50454.6 |
| `LifecycleBillReducElec` | 30972.5 | 0 |
| `LifecycleBillIncrElec` | 0 | 20764.9 |
| `LifecycleBillReducGas` | 10167.7 | 13198.2 |
| `LifecycleBillIncrGas` | 0 | 0 |
| `LifecycleBillReducElecGross` | 38715.6 | 0 |
| `LifecycleBillIncrElecGross` | 0 | 25956.1 |
| `LifecycleBillReducGasGross` | 11297.5 | 14664.7 |
| `LifecycleBillIncrGasGross` | 0 | 0 |
| `ElecBen_SB` | 6688.34 | 0 |
| `ElecBen_SH` | 7296.37 | 0 |
| `ElecSupplyCost_SB` | 0 | 4734.01 |
| `ElecSupplyCost_SH` | 0 | 5164.37 |
| `GasBen_SB` | 4314.29 | 8827.11 |
| `GasBen_SH` | 5033.34 | 10432 |
| `GasSupplyCost_SB` | 0 | 0 |
| `GasSupplyCost_SH` | 0 | 0 |
| `WaterEnergyBen_SB` | 35.5698 | 0 |
| `WaterEnergyBen_SH` | 39.218 | 0 |
| `WaterEnergyCost_SB` | 0 | 6.46961 |
| `WaterEnergyCost_SH` | 0 | 7.13316 |
| `RebatesandIncentsPV_SCT` | 5851.9 | 13332.3 |
| `NetParticipantCostPV_SCT` | 12289 | 37525.5 |
| `NetParticipantCostAdjustedPV_SCT` | 12289 | 40537.5 |
| `GrossParticipantCostPV_SCT` | 13654.4 | 46906.9 |
| `DiscountedSavingsGrosskWh` | 93907.9 | -67117.5 |
| `DiscountedSavingsNetkWh` | 75126.3 | -53694 |
| `DiscountedSavingsGrossThm` | 3954.02 | 10917.8 |
| `DiscountedSavingsNetThm` | 3558.62 | 9826.05 |
| `ElecBenGross` | 7301.87 | 0 |
| `GasBenGross` | 3837.81 | 7767.29 |
| `ElecSupplyCostGross` | 0 | 5215.21 |
| `GasSupplyCostGross` | 0 | 0 |
| `WaterEnergyBenGross` | 38.3348 | 0 |
| `WaterEnergyCostGross` | 0 | 7.03232 |
| `ElecBenGross_SB` | 8360.43 | 0 |
| `GasBenGross_SB` | 4793.66 | 9807.9 |
| `ElecSupplyCostGross_SB` | 0 | 5917.51 |
| `GasSupplyCostGross_SB` | 0 | 0 |
| `WaterEnergyBenGross_SB` | 44.4623 | 0 |
| `WaterEnergyCostGross_SB` | 0 | 8.08701 |
| `ElecBenGross_SH` | 9120.47 | 0 |
| `GasBenGross_SH` | 5592.6 | 11591.1 |
| `ElecSupplyCostGross_SH` | 0 | 6455.46 |
| `GasSupplyCostGross_SH` | 0 | 0 |
| `WaterEnergyBenGross_SH` | 49.0225 | 0 |
| `WaterEnergyCostGross_SH` | 0 | 8.91645 |
| `TotalSystemBenefit` | 9406.19 | 3012.77 |
| `TotalSystemBenefitGross` | 11278 | 2795.05 |
| `WeightedBenefits` | 1 | 1 |
| `WeightedProgramCost` | 3000 | 1800 |
| `WtdAdminCostsOverheadAndGA` | 1500 | 0 |
| `WtdAdminCostsOther` | 0 | 0 |
| `WtdMarketingOutreach` | 500 | 600 |
| `WtdDIActivity` | 0 | 300 |
| `WtdDIInstallation` | 0 | 0 |
| `WtdDIHardwareAndMaterials` | 0 | 0 |
| `WtdDIRebateAndInspection` | 0 | 0 |
| `WtdEMV` | 1000 | 900 |
| `WtdUserInputIncentive` | 0 | 0 |
| `WtdCostsRecoveredFromOtherSources` | 0 | 0 |
| `WeightedElecAlloc` | 0.61579 | 0 |
| `TotalExpenditures` | 9000 | 15300 |
| `TRCBen` | 9886.19 | 8110.56 |
| `TRCCost` | 20916.4 | 60271.8 |
| `TRCCostNoAdmin` | 17987.6 | 58514.5 |
| `TRCCostGross` | 22284.3 | 67797.1 |
| `TRCRatio` | 0.472652 | 0.134567 |
| `TRCRatioNoAdmin` | 0.549612 | 0.138608 |
| `TRCLifecycleNetBen` | -11620.9 | -53281.2 |
| `LevTRCCost` | 0.171446 | 0 |
| `LevNetBenTRCElec` | -0.0936904 | 0 |
| `LevNetBenTRCGas` | -1.28766 | -5.42244 |
| `LevTRCCostNoAdmin` | 0.147439 | 0 |
| `LevNetBenTRCElecNoAdmin` | -0.0696835 | 0 |
| `LevNetBenTRCGasNoAdmin` | -0.971442 | -5.2436 |
| `PACBen` | 9486.19 | 7310.56 |
| `PACCost` | 8785.49 | 19301.4 |
| `PACCostNoAdmin` | 5856.64 | 17544.1 |
| `PACRatio` | 1.07976 | 0.378758 |
| `PACRatioNoAdmin` | 1.61973 | 0.416696 |
| `PACLifecycleNetBen` | 510.036 | -12310.9 |
| `LevPACCost` | 0.0720122 | 0 |
| `LevNetBenPACElec` | 0.00574348 | 0 |
| `LevNetBenPACGas` | 0.0220732 | -1.25288 |
| `LevPACCostNoAdmin` | 0.0480052 | 0 |
| `LevNetBenPACElecNoAdmin` | 0.0297504 | 0 |
| `LevNetBenPACGasNoAdmin` | 0.33829 | -1.07404 |
| `RIMBen` | 9486.19 | 28075.4 |
| `RIMCost` | 49925.7 | 32499.6 |
| `RIMCostNoAdmin` | 46996.8 | 30742.3 |
| `RIMRatio` | 0.190006 | 0.863869 |
| `RIMRatioNoAdmin` | 0.201847 | 0.91325 |
| `LevRIMCost` | 0.409227 | 0 |
| `LevNetBenRIMElec` | -0.331471 | 0 |
| `LevNetBenRIMGas` | -4.41968 | -2.59606 |
| `SCBBen` | 11198.2 | 9147.11 |
| `SCBCost` | 21173 | 60501.6 |
| `SCBCostNoAdmin` | 18220.9 | 58730.3 |
| `SCBCostGross` | 22558.4 | 68086.1 |
| `SCBRatio` | 0.528891 | 0.151188 |
| `SCBRatioNoAdmin` | 0.614581 | 0.155748 |
| `SCHBen` | 12528.9 | 10752 |
| `SCHCost` | 21173 | 60932.6 |
| `SCHCostNoAdmin` | 18220.9 | 59161.4 |
| `SCHCostGross` | 22558.4 | 68624.9 |
| `SCHRatio` | 0.591741 | 0.176458 |
| `SCHRatioNoAdmin` | 0.687614 | 0.181741 |
| `PCTBen` | 56271.1 | 28892.2 |
| `PCTCost` | 19255.5 | 86623.6 |
| `PCTRatio` | 2.92235 | 0.333537 |
| `LevBenElec` | 0.0777557 | 0 |
| `LevBenGas` | 0.970609 | 0.711432 |
| `FirstYearGrossKWh` | 43006.5 | -36054 |
| `FirstYearGrossKWhSite` | 42750 | -36000 |
| `FirstYearGrossKWhWater` | 256.5 | -54 |
| `FirstYearGrossKW` | 20 | 0 |
| `FirstYearGrossThm` | 1800 | 5700 |
| `FirstYearNetKWh` | 34405.2 | -28843.2 |
| `FirstYearNetKWhSite` | 34200 | -28800 |
| `FirstYearNetKWhWater` | 205.2 | -43.2 |
| `FirstYearNetKW` | 16 | 0 |
| `FirstYearNetThm` | 1620 | 5130 |
| `LifecycleGrossKWh` | 103216 | -71547.8 |
| `LifecycleGrossKWhSite` | 102600 | -71437.5 |
| `LifecycleGrossKWhWater` | 615.6 | -110.25 |
| `LifecycleGrossThm` | 4320 | 11637.5 |
| `LifecycleNetKWh` | 82572.5 | -57238.2 |
| `LifecycleNetKWhSite` | 82080 | -57150 |
| `LifecycleNetKWhWater` | 492.48 | -88.2 |
| `LifecycleNetThm` | 3888 | 10473.8 |
| `AnnualGrossKWh` | 43006.5 | -28619.1 |
| `AnnualGrossKWhSite` | 42750 | -28575 |
| `AnnualGrossKWhWater` | 256.5 | -44.1 |
| `AnnualGrossKW` | 20 | 0 |
| `AnnualGrossThm` | 1800 | 4655 |
| `AnnualNetKWh` | 34405.2 | -22895.3 |
| `AnnualNetKWhSite` | 34200 | -22860 |
| `AnnualNetKWhWater` | 205.2 | -35.28 |
| `AnnualNetKW` | 16 | 0 |
| `AnnualNetThm` | 1620 | 4189.5 |
| `FirstYearNetElecCO2` | 6.84 | -5.76 |
| `FirstYearNetElecNOx` | 0 | -1.33721 |
| `FirstYearNetElecPM10` | 0 | -1.33721 |
| `FirstYearNetGasCO2` | 8.6022 | 27.2403 |
| `FirstYearNetGasNOx` | 0 | 50.274 |
| `FirstYearGrossElecCO2` | 8.55 | -7.2 |
| `FirstYearGrossElecNOx` | 0 | -1.67151 |
| `FirstYearGrossElecPM10` | 0 | -1.67151 |
| `FirstYearGrossGasCO2` | 9.558 | 30.267 |
| `FirstYearGrossGasNOx` | 0 | 55.86 |
| `LifecycleNetElecCO2` | 16.4385 | -11.4342 |
| `LifecycleNetElecNOx` | 0 | -2.65352 |
| `LifecycleNetElecPM10` | 0 | -2.65352 |
| `LifecycleNetGasCO2` | 20.6453 | 55.6156 |
| `LifecycleNetGasNOx` | 0 | 102.643 |
| `LifecycleGrossElecCO2` | 20.5481 | -14.2927 |
| `LifecycleGrossElecNOx` | 0 | -3.3169 |
| `LifecycleGrossElecPM10` | 0 | -3.3169 |
| `LifecycleGrossGasCO2` | 22.9392 | 61.7951 |
| `LifecycleGrossGasNOx` | 0 | 114.047 |
| `CreditedPA` | PGE | PGE |
| `Prg_MatchedOn` | PRG1 | PRG2 |
| `Prg_PA` | PGE | PGE |
| `Prg_ProgramName` | Example Residential Program | Example Commercial Program |
| `Prg_ProgramStatus` | active | new |
| `Prg_ProgramSegment` | resource_acquisition | market_support |
| `Prg_StatewideProgram` |  |  |
| `Prg_PrimarySector` | Residential | Commercial |
| `Prg_Sector` | Residential (Res) | Commercial (Com) |
| `Prg_BPSector` | Residential: 1.000000 | Commercial: 1.000000 |
| `Prg_ProgramImplementer` | IOU Core | Third-Party Solicited |
| `Prg_ProgramCategory` | Other | Market Transformation |
| `Prg_ImplementationContractor` |  | Example Implementer |
| `Prg_StartYear` | 2024 | 2027 |
| `Prg_EndYear` |  | 2031 |
| `Prg_Resource_Flag` | True | False |
| `Prg_NonResource_Flag` | False | True |
| `Prg_Deemed_Flag` | True | False |
| `Prg_Custom_Flag` | False | True |
| `Prg_Upstream_Flag` | False | False |
| `Prg_Midstream_Flag` | False | True |
| `Prg_Downstream_Flag` | True | False |
| `Prg_DirectInstall` | No | Partial |
| `Prg_Audit_Flag` | False | True |
| `Prg_Financing` | None | OBF |
| `Prg_ParentProgram` | Residential Programs | Commercial Third Party Programs |
| `Prg_Exclude_From_Budget` | False | False |
| `Prg_Exclude_From_CE` | False | False |
| `Prg_Exclude_From_Statewide_Flag` | False | False |
<!-- oa-all:end -->

### 3.6 Example A: the societal-test terms

The same quarters priced on the `SCT Base` and `SCT High` rows and discounted at the societal rate (1.6): `DF_sct(k) = 1 / 1.012575 ^ Qac(k)`.

<!-- oa-societal-A:start -->
| k | Quarter | DF_sct(k) | V_elec_SB | V_elec_SH | V_gas_SB | V_gas_SH | V_water_SB | V_water_SH |
|---|---|---|---|---|---|---|---|---|
| 0 | 2028Q3 | 0.975317 | 739.86 | 807.12 | 474.00 | 553.00 | 3.935 | 4.338 |
| 1 | 2028Q4 | 0.963204 | 730.67 | 797.10 | 468.12 | 546.14 | 3.886 | 4.284 |
| 2 | 2029Q1 | 0.951242 | 705.91 | 770.08 | 462.30 | 539.35 | 3.754 | 4.139 |
| 3 | 2029Q2 | 0.939429 | 704.89 | 768.97 | 456.56 | 532.66 | 3.749 | 4.133 |
| 4 | 2029Q3 | 0.927762 | 703.79 | 767.77 | 450.89 | 526.04 | 3.743 | 4.127 |
| 5 | 2029Q4 | 0.916241 | 695.05 | 758.23 | 445.29 | 519.51 | 3.696 | 4.075 |
| 6 | 2030Q1 | 0.904862 | 671.49 | 732.54 | 439.76 | 513.06 | 3.571 | 3.937 |
| 7 | 2030Q2 | 0.893625 | 670.52 | 731.48 | 434.30 | 506.69 | 3.566 | 3.932 |
| 8 | 2030Q3 | 0.882527 | 669.47 | 730.33 | 428.91 | 500.39 | 3.560 | 3.926 |
| 9 | 2030Q4 | 0.871567 | 396.69 | 432.76 | 254.15 | 296.51 | 2.110 | 2.326 |
<!-- oa-societal-A:end -->

### 3.7 Example B: the societal-test terms

<!-- oa-societal-B:start -->
| k | Quarter | DF_sct(k) | V_elec_SB | V_elec_SH | V_gas_SB | V_gas_SH | V_water_SB | V_water_SH |
|---|---|---|---|---|---|---|---|---|
| 0 | 2028Q2 | 0.987581 | -624.02 | -680.75 | 0.00 | 0.00 | -0.830 | -0.915 |
| 1 | 2028Q3 | 0.975317 | -623.04 | -679.68 | 0.00 | 0.00 | -0.828 | -0.913 |
| 2 | 2028Q4 | 0.963204 | -615.30 | -671.24 | 2,717.68 | 3,211.80 | -0.818 | -0.902 |
| 3 | 2029Q1 | 0.951242 | -594.45 | -648.49 | 2,683.93 | 3,171.92 | -0.790 | -0.871 |
| 4 | 2029Q2 | 0.939429 | -482.29 | -526.14 | 0.00 | 0.00 | -0.658 | -0.725 |
| 5 | 2029Q3 | 0.927762 | -370.41 | -404.09 | 0.00 | 0.00 | -0.525 | -0.579 |
| 6 | 2029Q4 | 0.916241 | -365.81 | -399.07 | 1,723.45 | 2,036.80 | -0.519 | -0.572 |
| 7 | 2030Q1 | 0.904862 | -353.42 | -385.55 | 1,702.05 | 2,011.51 | -0.501 | -0.553 |
| 8 | 2030Q2 | 0.893625 | -352.91 | -384.99 | 0.00 | 0.00 | -0.500 | -0.552 |
| 9 | 2030Q3 | 0.882527 | -352.35 | -384.39 | 0.00 | 0.00 | -0.500 | -0.551 |
<!-- oa-societal-B:end -->
