# How every output is calculated

This document explains the method behind the formulas `recet` uses: the notation, which avoided-cost rows and load shape are chosen and why, each calculation stage (`C1` ... `C16`), the validation
against CET, the deliberate differences and the limitations. **The formula of each output column, written out in the input fields with every symbol defined and a worked number, is in
[OUTPUT_FIELDS.md](OUTPUT_FIELDS.md)**; this document is the place to look for the reasoning and the rules behind it. [INPUT_FIELDS.md](INPUT_FIELDS.md) defines the input fields.

**The formulas here are executable.** `tests/reference_calc.py` re-implements every one of them in plain loops (no shared pricing code) and
`tests/test_calcs.py` requires the tool to agree with it on about 300 values for a three-measure job that exercises each branch below, with and without
market effects. Section C18 is a worked example whose numbers are also checked by a test (`tests/test_docs.py`).

Contents

* [C0 Notation and conventions](#c0-notation-and-conventions)
* [C1 Derived measure fields](#c1-derived-measure-fields)
* [C2 Time: quarters, useful life, baselines, discounting](#c2-time-quarters-useful-life-baselines-discounting)
* [C3 Avoided-cost data](#c3-avoided-cost-data)
* [C4 Load shapes](#c4-load-shapes)
* [C5 Lifetime value of each fuel](#c5-lifetime-value-of-each-fuel)
* [C6 Benefit / supply-cost split](#c6-benefit--supply-cost-split)
* [C7 Other benefits, other costs and tax credits](#c7-other-benefits-other-costs-and-tax-credits)
* [C8 Participant costs and incentives](#c8-participant-costs-and-incentives)
* [C9 Bill impacts](#c9-bill-impacts)
* [C10 Gross variants](#c10-gross-variants)
* [C11 Program costs and their weighting](#c11-program-costs-and-their-weighting)
* [C12 The cost tests and ratios](#c12-the-cost-tests-and-ratios)
* [C13 Where market effects enter](#c13-where-market-effects-enter)
* [C14 Emissions](#c14-emissions)
* [C15 Statewide allocation](#c15-statewide-allocation)
* [C16 Optional and secondary outputs](#c16-optional-and-secondary-outputs)
* [C17 Validation, deliberate differences from CET, and limitations](#c17-validation-deliberate-differences-from-cet-and-limitations)
* [C18 Worked example](#c18-worked-example)
* [C19 Savings quantities, discounted savings and levelized values](#c19-savings-quantities-discounted-savings-and-levelized-values)
* [C20 Prior CET releases (replicas)](#c20-prior-cet-releases-replicas)
* [C21 A true-up priced against its parent's profile](#c21-a-true-up-priced-against-its-parents-profile)

---------------------------------------------------------------------------------------------------------------------------------

## C0 Notation and conventions

* A **measure** is one row of `Measure.csv` (one `CEInputID`). Everything below is per measure unless it says "program" or "portfolio".
* Money is nominal US dollars. **PV** means present value at the run's First Year (C2.4). Energy is kWh, demand kW, gas therms; electric avoided costs
  are $/MWh in the ACC files and $/kWh in the formulas (`/ 1000`), gas avoided costs are $/therm. Emissions are described in C14 (CO2 in metric tons; NOx and PM10 in pounds).
* `pos(x) = max(x, 0)`. `ratio(n, d) = n / d`, and `0` when `d = 0` (the way CET's ratio columns behave).
* Suffixes: `1st` / `2nd` = first / second baseline; `_SB` / `_SH` = societal test with the Base / High social-cost-of-carbon rows; `_SCT` = a value discounted at the
  societal rate; `Gross` = without net-to-gross (C10); `NoAdmin` = without the program-level costs (C11).
* `F` is the run's **First Year** (C2.5). `MEB` and `MEC` are the run's market-effect adders on benefits and on costs, both 0 or both 0.05 (C13).

Symbols used in the formulas (input field in brackets). Every numeric field CET marks NotEmpty is required: a blank one stops the run (preflight
`blank_required_value`), so the pricing uses it as filed. Only the fields CET does not require read a blank as 0 (shown).

| Symbol | Meaning | Source |
|---|---|---|
| `Q` | number of units | `NumUnits` |
| `kWh1`, `kWh2` | per-unit annual kWh savings, 1st / 2nd baseline | `UnitkWh1stBaseline`, `UnitkWh2ndBaseline` (on a kWh-per-unit profile: the profile's own, C4.5) |
| `kW1`, `kW2` | per-unit kW savings | `UnitkW1stBaseline`, `UnitkW2ndBaseline` |
| `Th1`, `Th2` | per-unit annual therm savings | `UnitTherm1stBaseline`, `UnitTherm2ndBaseline` |
| `Wt1`, `Wt2` | per-unit annual kWh embedded in the water saved (negative: used) | `UnitkWhIOUWater1stBaseline`, `UnitkWhIOUWater2ndBaseline` (blank = 0) |
| `NTGe` | kWh net-to-gross | `NTGRkWh` |
| `NTGkw` | kW net-to-gross | `NTGRkW` |
| `NTGt` | therm net-to-gross | `NTGRTherm` |
| `NTGc` | cost net-to-gross | `NTGRCost` |
| `IRe`, `RRe` | installation and realization rate, kWh | `InstallationRatekWh`, `RealizationRatekWh` |
| `IRkw`, `RRkw`, `IRt`, `RRt` | the same for kW and therms | `InstallationRatekW`, `RealizationRatekW`, `InstallationRateTherm`, `RealizationRateTherm` |
| `EUL`, `RUL` | effective and remaining useful life, years | `EUL_Yrs`, `RUL_Yrs` (neither may be negative, and `RUL_Yrs` may not exceed `EUL_Yrs`: preflight errors); `RUL` = 0 when `RUL_Yrs` equals `EUL_Yrs`, as CalcCE's InputMeasurevw does (`CASE WHEN RUL >= EUL THEN 0 ELSE RUL END`) |
| `Y0`, `q0` | claim year and claim quarter (1-4) | `ClaimYearQuarter` = `YYYYQn` |
| `PA` | avoided-cost territory | `IOU_AC_Territory` |
| `w(k)`, `DF(k)`, `phased(k; a, b)` | quarter weight, discount factor, baseline phasing | C2 |

---------------------------------------------------------------------------------------------------------------------------------

## C1 Derived measure fields

The pricing reads the `Measure.csv` fields under their own names (`recet/pipeline/data_loading.py`) and derives the few values below, each named for the output field
it is written as, or `Used_<field>` for a value derived from a field. The measure-level output repeats every raw column unchanged and writes ten derived values
(OUTPUT_FIELDS 1.2): `Used_Year`, `Used_Quarter`, `AC_Net_kWh_1st_Baseline`, `AC_Net_kWh_2nd_Baseline`, `AC_Net_Therms_1st_Baseline`, `AC_Net_Therms_2nd_Baseline`,
`AC_Net_kWh_Water_1st_Baseline`, `AC_Net_kWh_Water_2nd_Baseline`, `Fuel_Sub_Flag` and `Scaled_Profile_Flag`.

**C1.1 Time**

```text
Used_Year       = first four characters of ClaimYearQuarter        Used_Quarter = its last character (1-4)
```

The CET-optional fields `UnitkWhIOUWater1stBaseline`, `UnitkWhIOUWater2ndBaseline`, `UnitDirectInstallLab`, `UnitDirectInstallMat`, `UnitEndUserRebate`,
`UnitIncentiveToOthers`, `UnitTaxCredits` and `MeasInflation` read a blank as 0; `AC_Elec_Impact_Profile_2nd_Baseline` and `AC_Elec_Impact_Profile_Water` (ReCET-only)
read a blank as `AC_Elec_Impact_Profile`; `ACCVersion` (ReCET-only) is spelt as a version key ("2024 ACC" -> "2024"), blank = the run's ACC.

**C1.2 Electric quantities**

```text
Used_UnitkWh1stBaseline = kWh1 = UnitkWh1stBaseline       Used_UnitkWh2ndBaseline = kWh2 = UnitkWh2ndBaseline       (the profile's own on a kWh-per-unit profile, C4.5)

AC_Net_kWh_1st_Baseline = Q * kWh1 * NTGe * IRe * RRe      (S1e)      AC_Net_kWh_2nd_Baseline = Q * kWh2 * NTGe * IRe * RRe      (S2e)      (written)

AC_Net_kWh_ScaledProfile_1st_Baseline = AC_Net_kWh_1st_Baseline     AC_Net_kWh_ScaledProfile_2nd_Baseline = AC_Net_kWh_2nd_Baseline     <- what each baseline's
                                                                                                                                          profile is multiplied by
```

`AC_Net_kWh_*_Baseline` is the net annual kWh each baseline is priced on (with NTG, installation and realization rates). The profile multiplier
`AC_Net_kWh_ScaledProfile_*_Baseline` equals it on a share profile (summing to 1); on a kWh-per-unit profile (C4.5) the profile carries the kWh, so the multiplier is
`Q * NTGe * IRe * RRe` and `Scaled_Profile_Flag` is true. The combined 8,760 profile is built from the multiplier too.

**C1.3 Gas quantities**

```text
AC_Net_Therms_1st_Baseline = Q * Th1 * NTGt * IRt * RRt          (T1)      AC_Net_Therms_2nd_Baseline = Q * Th2 * NTGt * IRt * RRt   (T2)
```

**C1.4 Water-energy quantities**

```text
AC_Net_kWh_Water_1st_Baseline   = Q * Wt1 * NTGe * IRe * RRe        (W1)        AC_Net_kWh_Water_2nd_Baseline = Q * Wt2 * NTGe * IRe * RRe   (W2)
```

**C1.5 Fuel substitution**

```text
Fuel_Sub_Flag  = (FuelID is one of AllElec-New, FuelSub-ToElec, FuelSub-ToGas)  OR  ("FuelSub" appears in MeasImpactType)
```

Only fuel-substitution measures book a negative fuel value as a *supply cost* (C6.1). The `MeasImpactType` rule reproduces what CET does with rows whose `FuelID` is blank
(it matched CET's supply-cost measures in the four real runs with no misses).

---------------------------------------------------------------------------------------------------------------------------------

## C2 Time: quarters, useful life, baselines, discounting

CET prices in **calendar quarters**, not years. Every stream in C5 is a sum over the quarters of the measure's life.

**C2.1 The quarters a measure delivers savings.** Let `EQ = 4 * EUL`, `full = floor(EQ)`, `rem = EQ - full` and `n = full + 1` if `rem > 1e-9`, else `n = full`.
For `k = 0 ... n-1` (k = 0 is the *claim quarter itself*):

```text
a(k)       = (q0 - 1) + k
year(k)    = Y0 + floor(a(k) / 4)
quarter(k) = a(k) mod 4                      (0 = Q1 ... 3 = Q4)
w(k)       = rem   if k = n-1 and rem > 1e-9     (the last, partial quarter of a fractional life)
             1     otherwise
```

A claim in Q3 therefore delivers two quarters in its first calendar year, and a life of 2.4 years (9.6 quarters) is ten quarters, the last weighted 0.6.

**C2.2 Hours in a quarter.** Electric prices are hourly, 8,760 hours a year, on the ACC's own calendar (C4.2). The quarters are hours
`1-2160` (Q1), `2161-4344` (Q2), `4345-6552` (Q3), `6553-8760` (Q4): 2160, 2184, 2208 and 2208 hours.

**C2.3 Baseline phasing.** With `RQ = 4 * RUL` (RUL is counted from the claim quarter), a quantity that differs between baselines, `a` (1st) and `b` (2nd), is

```text
phased(k; a, b) = a                                     if RQ <= 0  or  k + 1 <= RQ        (single baseline, or entirely before the RUL)
                  b                                     if k >= RQ                          (entirely after the RUL)
                  (RQ - k) * a + (1 - (RQ - k)) * b     otherwise                           (the one quarter that contains the RUL)
```

`RUL = 0` (the normal case, and an `RUL_Yrs` equal to `EUL_Yrs`: C0) means a single baseline: the 1st-baseline value applies for the whole life and the 2nd-baseline fields are ignored.

**C2.4 Discounting.** The discount rates come from the run parameters for the measure's `PA` and the ACC version (C3.3), or from a supplied
run-parameters file for that PA. `DiscountRateQtr` is the nominal
annual rate divided by four (for PG&E, every built-in ACC: 0.0766 / 4 = 0.01915).

```text
Rqf  = 1 + DiscountRateQtr(PA)          Rqf_sct = 1 + DiscountRateQtr_SCT(PA)          Raf = 1 + DiscountRateAnnual(PA)          Raf_sct = 1 + DiscountRateAnnual_SCT(PA)
Qac(k) = (year(k) - F) * 4 + quarter(k)                       DF(k) = 1 / Rqf ^ Qac(k)        (DF_sct(k) with Rqf_sct)
```

`Qac` counts calendar quarters from Q1 of the First Year, not quarters since the claim: a Q3 claim in the First Year starts at `Qac = 2`. A claim year before `F` gives a negative
`Qac` and a factor above 1. Streams that CET holds annually (program costs, bill impacts) use `Raf` and whole years (C9, C11); one-time cash flows use `Qm` (C8).

**C2.5 First Year.** `F` is CET's run-level *First Year*. It is not in the input files; set it in the GUI/`--base-year`, otherwise it is the earliest claim year in the job.

---------------------------------------------------------------------------------------------------------------------------------

## C3 Avoided-cost data

**C3.1 Electric ACC.** One row per `(iou, climate_zone, cost_test, year, hour_of_year)`; dollar columns are $/MWh. `total_avoided_cost` is the sum of the component columns
(`energy`, `generation_capacity`, `avoided_as_procurement`, `losses`, `methane_leakage`, `ghg_cap_and_trade`, `ghg_portfolio_rebalancing`, `ghg_cap_and_invest`, `ghg_adder`,
`air_quality_adder`, `transmission`, `distribution` - the columns present depend on the ACC vintage).

```text
AC(y, i)   = total_avoided_cost(y, i) / 1000                                        $/kWh, hour index i = 0 ... 8759
TD(y, i)   = (transmission + distribution)(y, i) / 1000                             the transmission and distribution part
Gen(y, i)  = AC(y, i) - TD(y, i)                                                    everything else (energy, capacity, GHG, losses, ...)
```

*Which rows.* The test rows are `cost_test = TRC` for TRC, PAC and RIM, `SCT Base` for SCB and `SCT High` for SCH. The row set is chosen from the measure's `PA` and zone:

1. `PA` to an ACC utility: `PGE` = PG&E, `SCE` = SCE, `SDGE` = SDG&E. `SCG` (a gas-only utility) is priced in each zone entirely - generation, T&D and emission
   rate - by **one** utility: SCE where the ACC file has SCE's own rows for that zone, otherwise PG&E where it has PG&E's, otherwise SCE (with the fallbacks below).
   SCE's CZ5 stand-in (item 2) does not count as SCE data, so SCG CZ5 is PG&E. On the 2024 and 2026 ACCs: SCE in 6, 8, 9, 10, 13, 14, 15, 16; PG&E in 1, 2, 3A, 3B, 4, 5,
   11, 12.
2. The zone is `"CZ" + AC_Climate_Zone`. Three (utility, zone) pairs that the ACC model does not export are stood in for by a documented neighbour: SCE CZ5 uses SCE CZ9;
   SDG&E CZ6 and CZ8 use SDG&E CZ7.
3. `AC_Climate_Zone = System` is first replaced by the profile's System zone (C4.4), so a System measure is priced on a real zone's rows. A zone that is still not in the
   file for that utility (a System measure none of whose zones can be priced) uses the unweighted mean across the utility's zones in the file.
4. **Year.** A pricing year outside the file's range uses the *nearest* year in the file (a tie takes the earlier year).

**C3.2 Gas ACC.** One row per `(utility, class, cost_test, scc, year)`; dollar columns are $/therm.

```text
utility:  PGE = PG&E   SCG = Socal Gas   SDGE = SDG&E   SCE = Socal Gas (SCE has no gas utility of its own)
class:    AC_Gas_Target_Sector  Residential = Residential   Commercial = Commercial   Core = Total Core
rows:     TRC (scc blank) for TRC/PAC/RIM;   SCT with scc = Base for SCB;   SCT with scc = High for SCH
year:     clamped to the first / last year in the file

ACg(y, m) = market_m + td_m + nox + co2 + upstream_methane + air_quality_adder  [ + behind_meter_methane ]         m = 1 ... 12, $/therm
```

`market` and `td` vary by month; the other components are one annual value used in every month. `behind_meter_methane` is included **only** when the class is Residential and the
measure's `AC_Gas_Impact_Profile` contains `FuelSub` (a residential gas-to-electric fuel-substitution profile).

**C3.3 E3 tables and run parameters.** Discount rates, `CO2Gas`, NOx/PM10 factors and retail rate schedules come from CET's E3 tables and are picked by **ACC
version**. The packaged tables are CET exports, whose `Version` column uses CET's own numbering; each is mapped to the ACC it holds when the table is read:

| CET Version | 2022 | 2024 | 2026 | 2027 |
|---|---|---|---|---|
| ACC | 2021 | 2022 | 2024 | 2026 |

(from each version's release date in RunParameters `AvoidedCostVersion`; the older versions 2018-2021 map to the 2017-2020 ACCs). The discount rates, base year and
`CO2Gas` (the RunParameters table) are the **run parameters**; a supplied run-parameters file replaces them for the PAs it lists. A built-in ACC uses its own tables, and a new
ACC label borrows the tables of the ACC version you name (default: the newest built-in). The primary rate is `DiscountRateAnnual` (PG&E 7.66%, SCE 7.65%, SCG 7.38%,
SDG&E 7.36%, the same in every built-in ACC); the societal rate `DiscountRateAnnual_SCT` is 5.03% for every utility. `BaseYear`: 2021 (2021 ACC), 2022 (2022 ACC),
2024 (2024 and 2026 ACCs). The rate schedules have no SCG rows for the 2021 and 2022 ACCs, so SCG bill impacts are not priced on those versions.

**C3.4 TRC-only ACCs.** The 2021 and 2022 ACCs have no Societal Cost Test avoided costs (no SCT rows in either file, and no air quality adder). A run on them prices
TRC, PAC and RIM as usual and leaves every SCT Base / SCT High column (`*_SB`, `*_SH`, `SCB*`, `SCH*`) blank, with a note in the log and `run_settings.json`. The same
applies to any supplied ACC file without SCT rows.

**C3.5 A different ACC version per measure (BRD BR-06.5).** A measure whose optional `ACCVersion` column names a version other than the run's is priced on that
version: its electric and gas avoided costs, its run parameters (discount rates) and E3 tables (rate schedules, emissions), exactly as a run on that version would price
it. The measure-level values of each version's measures are computed under that version, then joined, and the program costs (C11) are shared over all the job's measures
and discounted at the rates of the run's own version. The societal tests are left blank for the whole job if any version in it is TRC only.

---------------------------------------------------------------------------------------------------------------------------------

## C4 Load shapes

**C4.1 Which electric shape a measure uses.** In order, first match wins; the outcome is reported per measure in `ProfileSource`:

| Order | Shape | `ProfileSource` |
|---|---|---|
| 1 | a **supplied 8,760 profile** matching (`AC_Elec_Impact_Profile`, `PA`, `AC_Elec_Target_Sector`) | `user profile` |
| 1b | the same, but the supplied profile has no rows for this measure's zone (or `System` / `ALL`): fall through to the library | `library (user profile has no shape for this zone)` |
| 2 | the **CEDARS library** profile with the same end use, `PA` and target sector | `library` |
| 3 | the library profile with the same end use and `PA`, ignoring the sector | `library (sector-loose match)` |
| - | none of the above: the measure is **not priced** (`ElecPricingStatus = UNPRICED ...`), never priced on a made-up shape | (blank) |

A shape is 8,760 non-negative-or-negative shares that sum to 1 (a *fraction of the measure's annual net kWh* in each hour). A library or supplied profile is used for the measure's own
zone (for `AC_Climate_Zone = System`, the profile's System zone, C4.4), else the profile's own `System` rows. Every electric measure is priced from an 8,760 shape (C5.1); there is no other electric route. The library includes CEDARS' TOU (H-factor) end
uses as 8,760s built from their Gen-based energy shares: each TOU period's share spread evenly over that period's hours on the TOU map of the 2026 Electric ACC v1b
as re-issued on 2026-10-20, which is on the real 2018 calendar (`represents_year` = 2018; 2018 holidays and daylight-time dates). SCE/SCG shares are rescaled to sum
to 1 and SDG&E's winter on-peak share is the remainder to 1. Their catalog rows carry `hourly_source = TOU H-factor 8,760`. (Before 0.1.18 they were drawn on the
earlier ACC map, which followed the 2025 calendar, with `represents_year` = 2025.)

**C4.2 Calendar alignment.** A profile is drawn on the weekdays of the year it represents (`represents_year`; 2009, the DEER reference year, when blank; 2018 for the TOU
8,760s). An ACC's hourly avoided costs follow **one calendar's weekdays in every forecast year**: **2018** for the 2024 and 2026 ACCs (hour 1 is a Monday; the weekly
low in distribution and transmission value falls on days 6-7 of every week of every year) and **2020** for the 2021 and 2022 ACCs (hour 1 is Wednesday 1 January
2020). The calendar year `A` is read from the ACC file's hour stamps (2018 when a supplied file has none). Every profile is rotated **once**, onto `A`, and the same
aligned shape prices every year. With Monday = 0:

```text
d0         = (weekday(1 January A) - weekday(1 January of represents_year)) mod 7
d          = d0 if d0 <= 3, else d0 - 7                         (the short way round: -3 ... +3 days)
shape_y(i) = shape( (i + 24 * d) mod 8760 )                     i = 0 ... 8759, the same for every year y
```

Example: a 2025 profile (1 January a Wednesday) has `d = -2` on the 2024/2026 ACCs, so its 1 January lands on ACC day 3, a Wednesday; on the 2021/2022 ACCs `d = 0`.
The TOU 8,760s (2018) need no rotation on the 2024/2026 ACCs; on the 2021/2022 ACCs they move `d = +2` days onto the 2020 weekdays, so their weekdays line up and
their holidays and season boundaries are two days off 2020's. Leap years: an ACC year always has 8,760 hours (the last day of a leap year is dropped, so the 2020 calendar includes 29 February and ends on
30 December); hour `i` is the same position in every year, and only weekdays are aligned. This is **not** CEDARS' rule: CEDARS aligns the profile to each
forecast year's own 1 January weekday (although the ACC data do not follow those calendars) and rotates one day too far when the offset is negative. Gas is monthly and is not shifted.

**C4.3 Gas monthly shape.** `AC_Gas_Impact_Profile` names a profile of 12 monthly shares summing to 1 (`Annual`, `Winter Only`, `Summer Only`, and the three `...-FuelSub-ToElec` versions ship with the package;
a supplied profile with the same name replaces it). A gas measure whose profile name matches nothing is not priced (`GasPricingStatus = UNPRICED ...`).

**C4.5 Already-scaled profiles and load shifting (BRD BR-06.6).** A supplied profile with `profile_basis = kwh_per_unit` holds the kWh saved per unit in each hour
(negative = added load) and is priced as given; it is not rescaled. For such a measure

```text
AC_Net_kWh_ScaledProfile_1st_Baseline = NumUnits * NTGRkWh * IR * RR       the profile carries the kWh (it is the unadjusted gross kWh per unit), so C5's
                                                                           SUM over hours of profile x price x multiplier is the profile's own value, net
AC_Net_kWh_ScaledProfile_2nd_Baseline = AC_Net_kWh_ScaledProfile_1st_Baseline * r2        r2 = UnitkWh2ndBaseline / UnitkWh1stBaseline   (1 when UnitkWh1stBaseline is 0)
Used_UnitkWh1stBaseline = S = SUM over hours of the profile      (S x r2 the 2nd)      replaces UnitkWh1stBaseline in AC_Net_kWh_*_Baseline (net), the first-year,
                                                                                       lifecycle and annual kWh (gross and net), the bill impacts and the PCT
Scaled_Profile_Flag = true
```

Emissions use the profile the same way (its hourly kWh x the hourly emission rate). Water energy is spread by the profile's shares, `profile / S`; with `S = 0` it has none and is
not priced. With statewide allocation an allocated row is multiplied by its electric factor, as its UnitkWh fields are. A **load-shifting-only** measure (`S = 0`: kWh saved in some
hours, the same kWh added in others) therefore has a real electric value (the price difference between the hours) in one record, while its annual kWh, first-year kWh and
annual-rate bill impacts are 0. (CET nets annual savings before shaping, so it values such a measure at 0.)

**C4.6 Separate shapes for the 2nd baseline and for water energy.** Two optional columns, not CET fields, name other electric end-use shapes; each defaults to
`AC_Elec_Impact_Profile` when blank and is matched the same way (C4.1, C4.4, C4.5):

* `AC_Elec_Impact_Profile_2nd_Baseline`: an early replacement (`0 < RUL_Yrs < EUL_Yrs`) is priced as two parts with the same `CEInputID`, the 1st-baseline part (every 2nd-baseline
  quantity 0) on `AC_Elec_Impact_Profile` and the 2nd-baseline part (every 1st-baseline quantity 0) on this profile, and the parts are added.
  The phasing of C2 is linear in the two quantities, so with one shape the result is unchanged; the boundary quarter's split is kept. Electric benefits and supply costs (all
  three tests), emissions and the combined 8,760 output use the parts. If only one of the two shapes is found, the measure is `PARTLY PRICED`.
* `AC_Elec_Impact_Profile_Water`: the water energy (C5.3) is spread over this shape for both baselines.

**C4.4 The System climate zone.** A measure with `AC_Climate_Zone = System` is priced exactly as if its zone were the profile's **System zone**: the real climate zone, out of
the zones the profile has, with the **lowest total electric avoided cost over the full length of the ACC data**:

```text
total(z)    = SUM over every year y in the ACC file, SUM over hours i:  shape_z,aligned(i) * AC_TRC(PA, z, y, i)        $ per annual kWh, undiscounted
System zone = the zone z with the lowest total(z); a tie goes to the first zone in the order 1, 2, 3A, 3B, 4 ... 16
```

`shape_z,aligned` is the profile's own shape for zone z aligned as in C4.2, and `AC_TRC(PA, z, ...)` is the TRC total avoided cost found by the rules of C3.1 (so SCE CZ5 and
SDG&E CZ6/CZ8 are options, priced on their stand-in zones, and SCG's options are the zones its routing reaches). A zone the ACC cannot price is not an option. The TRC cost
test decides the zone; the same zone is then used for the shape, every cost test, every component, water energy and the hourly CO2 (NOx and PM10 still use the literal
`System` key of the Emissions table, as CET does). For a supplied profile the options are the zones it gives; one given only for `ALL` (or `System`) uses that shape in every zone
the measure's `PA` has prices for, so the prices alone decide.

The choice depends only on the profile and the ACC file, so it is **cataloged**: the package ships the System zone of every library profile under each built-in ACC
(`data/system_zones/system_zones.csv.gz`, with every option's total; rebuild with `recet data build-system-zones`). A run uses the catalog for a library profile
priced on a built-in ACC file that matches the one the catalog was built from, and calculates the choice otherwise (a supplied profile or ACC file). Each measure reports
the zone in `SystemClimateZone` and where it came from in `SystemZoneSource`. If no zone can be priced the measure falls back to the profile's `System` rows and the
blended prices of C3.1 item 3.

---------------------------------------------------------------------------------------------------------------------------------

## C5 Lifetime value of each fuel

Each of C5.1-C5.5 produces one signed lifetime PV per measure: `V_elec`, `V_gas`, `V_water`. C5.6 repeats them for the societal tests. C6 then splits each into a benefit and a supply cost.

**C5.1 Electric, hourly profile (the general route).**

```text
V_elec = SUM over k of  phased(k; S1e', S2e') * w(k) * DF(k) * SUM over hours i in quarter(k) of  shape_year(k)(i) * AC(y*(k), i)

   S1e' = AC_Net_kWh_ScaledProfile_1st_Baseline * (NTGe + MEB) / NTGe        S2e' = AC_Net_kWh_ScaledProfile_2nd_Baseline * (NTGe + MEB) / NTGe
                                                                                    (the factor is 1 when MEB = 0 or NTGe = 0)
   y*(k) = the nearest ACC year to year(k)                 shape_year(k) = the calendar-aligned shape of C4.2
```

Units: annual kWh x (share of the year in each hour) x $/kWh = $. *Implementation note:* the inner sum `U` does not depend on the measure's kWh, so the tool computes it once per (shape, IOU, sector, zone) route, pricing year and quarter. The phased scale is the 1st-baseline
quantity times f(k) plus the 2nd times 1 - f(k) (f = 1 before the RUL, 0 after, the blend in the quarter that holds it), so a measure's lifetime value is
`scale_1st x A + scale_2nd x B`, where A and B are the sums over its quarters of f(k) (or 1 - f(k)) x weight x discount x U. A and B depend only on the route,
claim year, claim quarter, EUL and RUL, so they are computed once for each distinct combination; every quarter is still discounted on its own. The result equals the
per-measure loop to floating-point rounding (relative differences below 1e-14). Because the ACC's `total_avoided_cost` already contains transmission and distribution, there is no separate kW or T&D term on
this route.

**C5.2 Retired (2026-10-02): the TOU (H-factor) route.** TOU end uses are priced by C5.1 from their 8,760s in the library (C4.1). What follows is the formula the tool used
before, kept for reference: CET's two-term benefit, a **kWh (generation) term** and a **demand-scaled T&D term**.

```text
f_kWh = Q * (NTGe  + MEB) * IRe  * RRe          f_kW = Q * (NTGkw + MEB) * IRkw * RRkw
DS_i  = kW_i   if the row's ds_type = kW  (PG&E commercial / industrial / agricultural and all SCE profiles)
        kWh_i  if ds_type = kWh          (PG&E residential)

V_elec = SUM over k of  w(k) * DF(k) * [ phased(k; f_kWh*kWh1, f_kWh*kWh2) * GenQ(year(k), quarter(k))  +  phased(k; f_kW*DS_1, f_kW*DS_2) * TDQ(year(k), quarter(k)) ]
```

`GenQ` and `TDQ` are quarterly rates built once per (PA, sector, end use, zone, year) from the ACC's hourly Gen and T&D and the profile's five period factors (`e0..e4` energy shares, `kw0..kw4` demand factors):

```text
periods P = 1 summer on-peak, 2 summer partial-peak, 3 summer off-peak, 5 winter partial-peak, 6 winter off-peak       (period 4 has no hours)
period map: each ACC hour i belongs to the period of hour (i + 25) on a 1991 calendar   (a fixed +25 hour alignment)
f(P, q)   = share of period P's hours that fall in quarter q

GenQ(y, q) = SUM_P  e_P  * f(P, q) * mean over hours i in P of Gen(y, i)
TDQ(y, q)  = SUM_P  kw_P * f(P, q) * mean over hours i in P of TD(y, i)                       (kw_form = per_kwh: PG&E residential, kW per annual kWh)
           = SUM_P  kw_P * f(P, q) * 0.001 * (sum over hours i in P of TD(y, i))               (kw_form = rel_kw: relative kW factors)
```

Notes: only PG&E and SCE have TOU factors (a measure for any other territory with no hourly shape is unpriced). A TOU profile's `System` zone is the one real zone with the lowest total T&D.
Unlike C5.1, TOU quarters in a year outside the ACC's range are **skipped**, not priced at the nearest year.

**C5.3 Retired (2026-10-02): CET's SCG table.** SCG measures are priced by C5.1 on the SCG routing of C3.1; CET's table is no longer read. Formerly, with an option on (2024
ACC only), an SCG measure whose (sector, end use, zone) is in the table was priced on CET's quarterly rate, which already includes the profile:

```text
V_elec = SUM over k of  phased(k; A1, A2) * rate(year(k), quarter(k)) * w(k) * DF(k)         A_i = AC_Net_kWh_<i>_Baseline * (NTGe + MEB) / NTGe
rate = Gen + TD from CET's table for the cost test
```


**C5.4 Gas.** (Implementation note: as in C5.1, the sum over a quarter's months depends only on the profile, IOU, gas sector and year, so the tool computes it once per route, year and quarter, and the lifetime sums once per route, claim year, claim quarter, EUL and RUL; the result equals the per-measure loop to rounding.)

```text
V_gas = SUM over k of  w(k) * DF(k) * SUM over months m in quarter(k) of  gasshape(m) * phased(k; T1', T2') * ACg(y^(k), m)

   T1' = AC_Net_Therms_1st_Baseline * (NTGt + MEB) / NTGt          T2' likewise          y^(k) = year(k) clamped to the gas ACC's first / last year
   quarter months: Q1 = Jan-Mar, Q2 = Apr-Jun, Q3 = Jul-Sep, Q4 = Oct-Dec
```

A measure is gas-priced only if `T1 != 0` or `T2 != 0` and its profile name matches (C4.3).

**C5.5 Water energy.** The kWh embedded in the water a measure saves (`Wt > 0`) or uses (`Wt < 0`) is valued like electricity but on the **generation term alone** (CalcCE has no T&D term for it):

```text
V_water = the sum of C5.1 with  W1, W2 in place of S1e, S2e  and  Gen(y, i) = AC - TD  in place of AC
```

It uses the measure's own electric profile, zone and market effects. A water measure with no electric profile cannot be priced (`WaterPricingStatus = UNPRICED ...`).
Pricing water on the total avoided cost instead of Gen overstates it by about a quarter (1.23 times CET against 0.98 times CET on the real runs), which is why the Gen-only rule is written out here.

**C5.6 Societal (SB / SH) values.** `V_elec_SB`, `V_gas_SB`, `V_water_SB` (and `_SH`) repeat C5.1-C5.5 with two changes: the electric and water rows are `SCT Base` (`SCT High`) and the gas rows are `SCT` with `scc = Base` (`High`);
and discounting uses `Rqf_sct` (C2.4). When SCB/SCH are switched off these columns are blank.

---------------------------------------------------------------------------------------------------------------------------------

## C6 Benefit / supply-cost split

**C6.1 Electric and gas.**

```text
fuel-substitution measure (Fuel_Sub_Flag):   Ben = pos(V)        SupplyCost = pos(-V)
every other measure:                           Ben = V (signed)    SupplyCost = 0

ElecBen, ElecSupplyCost = split of V_elec         GasBen, GasSupplyCost = split of V_gas         (and _SB, _SH from V_elec_SB ... )
```

A fuel-substitution measure that *adds* electric load has a negative `V_elec`; the loss is booked as a positive `ElecSupplyCost`, not a negative benefit. For any other measure a negative value simply stays
in `Ben`.

**C6.2 Water energy.** The sign of the *first-baseline water kWh* decides, not the sign of the value:

```text
WaterEnergyBen  = V_water     if Wt1 > 0, else 0           WaterEnergyCost = -V_water   if Wt1 < 0, else 0           (same for _SB, _SH)
```

---------------------------------------------------------------------------------------------------------------------------------

## C7 Other benefits, other costs and tax credits

Per-unit amounts times units, **undiscounted** (they are one-time amounts in the claim):

```text
OtherBen    = Q * (NTGRkWh + MEB) * (UnitGasInfraBens + UnitRefrigBens + UnitMiscBens)
OtherCost   = Q * (NTGRkWh + MEC) * (UnitRefrigCosts + UnitMiscCosts)                    (UnitGasInfraBens is a benefit-only field)
TaxCredits  = Q * (NTGRkWh + MEB) * UnitTaxCredits

OtherBenGross = Q * (UnitGasInfraBens + UnitRefrigBens + UnitMiscBens)      OtherCostGross = Q * (UnitRefrigCosts + UnitMiscCosts)      TaxCreditsGross = Q * UnitTaxCredits
```

`TaxCredits` is added to the numerator of the TRC ratio only; it is not in `TotalSystemBenefit` or any cost.

These three are the EE-2026 cost-test set's *input streams* (`basis: one_time`), priced from the set by `pipeline/input_streams.py` (0.1.86); the
engine's `price_other_ben_cost` gives the same values bit for bit and is kept as the reference. A set may add input streams of its own (a non-energy
benefit, say), one-time or annual over the measure's life, from Measure columns CET does not have: COST_TESTS.md section 3 has their formulas.

---------------------------------------------------------------------------------------------------------------------------------

## C8 Participant costs and incentives

These are one-time cash flows at the claim quarter, discounted once. Let

```text
Qm  = (Y0 - F) * 4 + (q0 - 1)                 PVF = Rqf ^ Qm          (PVF_sct with Rqf_sct)        every "PV" below divides by PVF

IncentsDI = Q * (UnitIncentiveToOthers + UnitDirectInstallLab + UnitDirectInstallMat)              Rebates = Q * UnitEndUserRebate
```

```text
RebatesandIncentsPV = (IncentsDI + Rebates) / PVF

GrossMeasCost   = Q * UnitMeaCost1stBaseline                    if RUL = 0
                = Q * UnitMeaCost2ndBaseline                    if RUL > 0        (an early replacement counts only the normal-replacement cost)
GrossMeasCostPV = Q * (UnitMeaCost1stBaseline - PVsub) / PVF
   PVsub        = 0                                                                                    if RUL = 0
                = (UnitMeaCost1stBaseline - UnitMeaCost2ndBaseline) * ((1 + MeasInflation/4) / Rqf) ^ (4 * RUL)     if RUL > 0

ExcessIncentivesPV = pos( IncentsDI / PVF - GrossMeasCostPV )                        (end-user rebates are not part of the test)
MarkEffectPlusExcessIncPV = MEC * (GrossMeasCostPV + ExcessIncentivesPV)

RebatesandIncents            = IncentsDI + Rebates                                                    (nominal; part of TotalExpenditures, C11.3)
GrossParticipantCostPV       = GrossMeasCostPV - (IncentsDI + Rebates) / PVF
NetParticipantCostPV         = NTGc * (GrossParticipantCostPV + ExcessIncentivesPV)                 (as CET exports it: no market-effects term)
NetParticipantCostAdjustedPV = NetParticipantCostPV + MarkEffectPlusExcessIncPV                     (what the TRC / SCT costs use)
```

CET's nominal (undiscounted) participant columns, and the rest of its participant-cost columns, follow the same steps without `PVF`:

```text
IncentiveToOthers = Q * UnitIncentiveToOthers      DILaborCost = Q * UnitDirectInstallLab      DIMaterialCost = Q * UnitDirectInstallMat      EndUserRebate = Rebates
GrossMeasureCost  = GrossMeasCost                  ExcessIncentives = pos( IncentsDI - GrossMeasCost )
MarkEffectPlusExcessInc        = MEC * (GrossMeasCost + ExcessIncentivesPV)            (CET adds the PV excess incentives to the nominal cost; copied, it matches CET)
GrossParticipantCost           = GrossMeasCost - (IncentsDI + Rebates)
GrossParticipantCostAdjusted   = GrossParticipantCost + ExcessIncentives + MarkEffectPlusExcessInc
GrossParticipantCostAdjustedPV = GrossParticipantCostPV + ExcessIncentivesPV + MarkEffectPlusExcessIncPV
NetParticipantCost             = NTGc * (GrossParticipantCost + ExcessIncentives)
NetParticipantCostAdjusted     = NetParticipantCost + MarkEffectPlusExcessInc
```

`GrossMeasCostPV` and `ExcessIncentivesPV` are output columns too. For an early replacement the nominal and PV measure costs are built differently, as in CET: the nominal one is the
2nd-baseline cost, the PV one the 1st-baseline cost less the inflation-adjusted difference.

The `_SCT` columns (`RebatesandIncentsPV_SCT`, `GrossParticipantCostPV_SCT`, `NetParticipantCostPV_SCT`, `NetParticipantCostAdjustedPV_SCT`) are the same with `Rqf_sct`.
All of these match CET's own columns to the cent with 0% and 5% market effects (CET jobs 121842 / 121841, 13 measures, four of them accelerated replacements). With `MEC = 0` the market-effects term is 0.

---------------------------------------------------------------------------------------------------------------------------------

## C9 Bill impacts

The ratepayer bill change for RIM. Unlike C5 it is **annual**, not quarterly, and uses CET's retail rate schedules: `RateE` ($/kWh) for the schedule that (`PA`, `AC_Elec_Target_Sector`) maps to, and `RateG` ($/therm) for
(`PA`, `AC_Gas_Target_Sector`). Like the benefits it uses `NTG + MEB` (CalcCE prices the bill on `Qty x (NTGRkWh + MEBens)` and `(NTGRThm + MEBens)`), and the measure-level
`RateScheduleElec` / `RateScheduleGas` columns are not read.

With `yr1 = Y0 - BaseYear` (RunParameters `BaseYear` of the ACC version, C3.3: 2021, 2022, or 2024 for the 2024 and 2026 ACCs), `E = EUL`, `R = RUL`, `full = floor(E)`, `rem = E - full`, `n = full + 1 if rem > 1e-9 else full`:

```text
floor_rul = floor(R) if R > 0 else full                                     boundary share of the 1st baseline:  b = R - floor(R) if R > 0 else 1

PV(s1, s2, rates) =   SUM over k = 0 ... min(floor_rul, n) - 1 of        s1 * rate(Y0 + k) / Raf ^ k
                    + SUM over k = floor_rul ... n - 1 of                 wt * v(k) * rate(Y0 + k) / Raf ^ (k + yr1)
      where  wt = rem if k = n-1 and rem > 1e-9, else 1          v(k) = b * s1 + (1 - b) * s2  if k = floor_rul,   else s2
```

The second sum's exponent `k + yr1` (years since `BaseYear`, not since the claim) reproduces what CalcCE's SQL does for the boundary year and every 2nd-baseline year; it was confirmed against real CET output
to the cent, so it is kept rather than "corrected".

```text
elec PV = PV( fe * AC_Net_kWh_1st_Baseline, fe * AC_Net_kWh_2nd_Baseline, RateE )        gas PV = PV( ft * AC_Net_Therms_1st_Baseline, ft * AC_Net_Therms_2nd_Baseline, RateG )
   fe = (NTGRkWh + MEB) / NTGRkWh,  ft = (NTGRTherm + MEB) / NTGRTherm   (1 when MEB = 0 or the NTG is 0)

fuel-substitution measure:   BillReduc = pos(PV)     BillIncr = pos(-PV)                 every other measure:   BillReduc = PV     BillIncr = 0
LifecycleBillReducElec, LifecycleBillIncrElec from the electric PV;    LifecycleBillReducGas, LifecycleBillIncrGas from the gas PV
```

A measure whose sector has no schedule mapping (or a year outside the schedule's range) gets no bill impact for that fuel.

---------------------------------------------------------------------------------------------------------------------------------

## C10 Gross variants

A "Gross" value is the same quantity **without net-to-gross** (and without the market-effects adder). Because every net value was scaled by NTG, gross = net / NTG:

```text
ElecBenGross{,_SB,_SH}            = ratio( ElecBen{...},            NTGRkWh + MEB )        ElecSupplyCostGross{...}  = ratio( ElecSupplyCost{...},  NTGRkWh + MEB )
GasBenGross{,_SB,_SH}             = ratio( GasBen{...},             NTGRTherm + MEB )      GasSupplyCostGross{...}   = ratio( GasSupplyCost{...},   NTGRTherm + MEB )
WaterEnergyBenGross{,_SB,_SH}     = ratio( WaterEnergyBen{...},     NTGRkWh + MEB )        WaterEnergyCostGross{...} = ratio( WaterEnergyCost{...}, NTGRkWh + MEB )
```

(`ratio` returns 0 when NTG + MEB = 0.) `OtherBenGross`, `OtherCostGross`, `TaxCreditsGross` are in C7 and `GrossParticipantCostPV` is in C8.
`TotalSystemBenefitGross`, `TRCCostGross`, `SCBCostGross` and `SCHCostGross` are in C12.

---------------------------------------------------------------------------------------------------------------------------------

## C11 Program costs and their weighting

`ProgramCost.csv` costs belong to a *program*, not a measure, and are spread over the program's measures in proportion to their positive benefits.

**C11.1 Program totals.** For each `ProgramCost` row `r` (with `Year` = the first four characters of its `ClaimYearQuarter`; a `PA` of `SW` is treated as `SCE`, what CET did in the real runs):

```text
T_r = AdminCostsOverheadAndGA + AdminCostsOther + MarketingOutreach + DIActivity + DIInstallation + DIHardwareAndMaterials + DIRebateAndInspection + EMV + UserInputIncentive
      + CostsRecoveredFromOtherSources                                      (blank = 0; OnBillFinancing is not part of CET's sum)

SumCosts       (program) = SUM_r T_r
SumCostsNPV    (program) = SUM_r T_r / Raf(PA_r) ^ (Year_r - F)                     annual discounting
SumCostsNPV_SCT(program) = SUM_r T_r / Raf_sct(PA_r) ^ (Year_r - F)
```

**C11.2 A measure's share.** Within a measure's program (`PrgID`), with `B_m = pos(ElecBen) + pos(GasBen) + pos(WaterEnergyBen) + pos(OtherBen)`:

```text
share_m = B_m / SUM over the program's measures of B      if that sum is not 0          share_m = 1 / (number of measures in the program)    if it is 0
```

Five further shares use other benefit sets and are computed the same way: `share_gross` (the Gross benefits), `share_SB` (`ElecBen_SB + GasBen_SB + WaterEnergyBen_SB + OtherBen`), `share_SH`, `share_SB_gross` and `share_SH_gross`
(C12's generated block names them by the test that computes them: share_TRC, share_TRCGross, share_SCB, share_SCBGross, share_SCH, share_SCHGross).

```text
WeightedBenefits    = share_m
WeightedProgramCost = share_m * SumCosts                 nominal (undiscounted) dollars
```

**C11.3 Budget.** CET's `TotalExpenditures`, which summed over a run is the run receipt's **Budget** (checked to the cent on CET jobs 121219 and 121841):

```text
TotalExpenditures = RebatesandIncents + WeightedProgramCost          nominal dollars
```

**C11.4 Program-cost components and the electric share.** CalcCost also reports each program-cost field's share and, per measure, the electric part of its own benefits (which the
levelized columns of C19.6 use to split a cost between the fuels):

```text
Wtd<field>        = share_m * SUM_r <field>_r          for each of the ten fields of T_r (nominal)          SUM of the ten = WeightedProgramCost (CET's ProgramCosts; not written)
WeightedElecAlloc = ratio( pos(ElecBen) , pos(ElecBen) + pos(GasBen) + pos(WaterEnergyBen) + pos(OtherBen) )          the measure's own benefits, not the program's
```

---------------------------------------------------------------------------------------------------------------------------------

## C12 The cost tests and ratios

The tests are not written out in code: they are the cost-test set the run uses (`recet/data/cost_tests/<id>/definition.yaml`, COST_TESTS.md),
priced by one evaluator (`pipeline/test_evaluator.py`). The formulas below are EE-2026's, the set in force; the block between the markers is generated
from the set (`test_evaluator.formulas`) and a test fails when it no longer says what the set does. The prose after it explains the same tests.

<!-- cost-test-set EE-2026: generated by test_evaluator.formulas -->
```text
TotalSystemBenefit = ElecBen + GasBen + WaterEnergyBen + OtherBen - ElecSupplyCost - GasSupplyCost - WaterEnergyCost - OtherCost
TotalSystemBenefitGross = ElecBenGross + GasBenGross + WaterEnergyBenGross + OtherBenGross - ElecSupplyCostGross - GasSupplyCostGross - WaterEnergyCostGross - OtherCostGross

TRC  (Total Resource Cost; TRC avoided costs, primary discount rate; program costs shared as share_TRC)
  TRCBen = ElecBen + GasBen + WaterEnergyBen + OtherBen + TaxCredits
  TRCCostNoAdmin = RebatesandIncentsPV + NetParticipantCostAdjustedPV + ElecSupplyCost + GasSupplyCost + WaterEnergyCost + OtherCost
  TRCCost = share_TRC * SumCostsNPV + TRCCostNoAdmin
  TRCCostGross = share_TRCGross * SumCostsNPV + RebatesandIncentsPV + GrossParticipantCostPV + ElecSupplyCostGross + GasSupplyCostGross + WaterEnergyCostGross + OtherCostGross
  TRCRatio = ratio(TRCBen, TRCCost)    TRCRatioNoAdmin = ratio(TRCBen, TRCCostNoAdmin)
  TRCLifecycleNetBen = ElecBen + GasBen - TRCCost
  LevTRCCost = ratio(TRCCost * WeightedElecAlloc, DiscountedSavingsNetkWh)    LevNetBenTRCElec, LevNetBenTRCGas: C19.6
  LevTRCCostNoAdmin = ratio(TRCCostNoAdmin * WeightedElecAlloc, DiscountedSavingsNetkWh)    LevNetBenTRCElecNoAdmin, LevNetBenTRCGasNoAdmin: C19.6

PAC  (Program Administrator Cost; TRC avoided costs, primary discount rate; program costs shared as share_TRC)
  PACBen = ElecBen + GasBen + WaterEnergyBen + OtherBen
  PACCostNoAdmin = RebatesandIncentsPV + ElecSupplyCost + GasSupplyCost + WaterEnergyCost + OtherCost
  PACCost = share_TRC * SumCostsNPV + PACCostNoAdmin
  PACRatio = ratio(PACBen, PACCost)    PACRatioNoAdmin = ratio(PACBen, PACCostNoAdmin)
  PACLifecycleNetBen = ElecBen + GasBen - PACCost
  LevPACCost = ratio(PACCost * WeightedElecAlloc, DiscountedSavingsNetkWh)    LevNetBenPACElec, LevNetBenPACGas: C19.6
  LevPACCostNoAdmin = ratio(PACCostNoAdmin * WeightedElecAlloc, DiscountedSavingsNetkWh)    LevNetBenPACElecNoAdmin, LevNetBenPACGasNoAdmin: C19.6

RIM  (Ratepayer Impact Measure; TRC avoided costs, primary discount rate; program costs shared as share_TRC)
  RIMBen = ElecBen + GasBen + WaterEnergyBen + OtherBen + LifecycleBillIncrElec + LifecycleBillIncrGas
  RIMCostNoAdmin = LifecycleBillReducElec + LifecycleBillReducGas + RebatesandIncentsPV + ElecSupplyCost + GasSupplyCost + WaterEnergyCost + OtherCost
  RIMCost = share_TRC * SumCostsNPV + RIMCostNoAdmin
  RIMRatio = ratio(RIMBen, RIMCost)    RIMRatioNoAdmin = ratio(RIMBen, RIMCostNoAdmin)
  LevRIMCost = ratio(RIMCost * WeightedElecAlloc, DiscountedSavingsNetkWh)    LevNetBenRIMElec, LevNetBenRIMGas: C19.6

SCB  (Societal Cost Test, Base; SCT Base avoided costs, societal discount rate; program costs shared as share_SCB)
  SCBBen = ElecBen_SB + GasBen_SB + WaterEnergyBen_SB + OtherBen
  SCBCostNoAdmin = RebatesandIncentsPV_SCT + NetParticipantCostAdjustedPV_SCT + ElecSupplyCost_SB + GasSupplyCost_SB + WaterEnergyCost_SB + OtherCost
  SCBCost = share_SCB * SumCostsNPV_SCT + SCBCostNoAdmin
  SCBCostGross = share_SCBGross * SumCostsNPV_SCT + RebatesandIncentsPV_SCT + GrossParticipantCostPV_SCT + ElecSupplyCostGross_SB + GasSupplyCostGross_SB + WaterEnergyCostGross_SB + OtherCostGross
  SCBRatio = ratio(SCBBen, SCBCost)    SCBRatioNoAdmin = ratio(SCBBen, SCBCostNoAdmin)

SCH  (Societal Cost Test, High; SCT High avoided costs, societal discount rate; program costs shared as share_SCH)
  SCHBen = ElecBen_SH + GasBen_SH + WaterEnergyBen_SH + OtherBen
  SCHCostNoAdmin = RebatesandIncentsPV_SCT + NetParticipantCostAdjustedPV_SCT + ElecSupplyCost_SH + GasSupplyCost_SH + WaterEnergyCost_SH + OtherCost
  SCHCost = share_SCH * SumCostsNPV_SCT + SCHCostNoAdmin
  SCHCostGross = share_SCHGross * SumCostsNPV_SCT + RebatesandIncentsPV_SCT + GrossParticipantCostPV_SCT + ElecSupplyCostGross_SH + GasSupplyCostGross_SH + WaterEnergyCostGross_SH + OtherCostGross
  SCHRatio = ratio(SCHBen, SCHCost)    SCHRatioNoAdmin = ratio(SCHBen, SCHCostNoAdmin)

PCT  (Participant Cost Test; TRC avoided costs, primary discount rate)
  PCTBen = LifecycleBillReducElecGross + LifecycleBillReducGasGross + RebatesandIncentsPV + TaxCreditsGrossPV
  PCTCost = GrossParticipantCostPV + RebatesandIncentsPV + LifecycleBillIncrElecGross + LifecycleBillIncrGasGross
  PCTRatio = ratio(PCTBen, PCTCost)

share_TRC = C11.2 over ElecBen + GasBen + WaterEnergyBen + OtherBen
share_TRCGross = C11.2 over ElecBenGross + GasBenGross + WaterEnergyBenGross + OtherBenGross
share_SCB = C11.2 over ElecBen_SB + GasBen_SB + WaterEnergyBen_SB + OtherBen
share_SCBGross = C11.2 over ElecBenGross_SB + GasBenGross_SB + WaterEnergyBenGross_SB + OtherBenGross
share_SCH = C11.2 over ElecBen_SH + GasBen_SH + WaterEnergyBen_SH + OtherBen
share_SCHGross = C11.2 over ElecBenGross_SH + GasBenGross_SH + WaterEnergyBenGross_SH + OtherBenGross
```
<!-- end cost-test-set -->

```text
Sup       = ElecSupplyCost + GasSupplyCost + WaterEnergyCost + OtherCost
B         = ElecBen + GasBen + WaterEnergyBen + OtherBen

TotalSystemBenefit      = B - Sup
TotalSystemBenefitGross = (ElecBenGross + GasBenGross + WaterEnergyBenGross + OtherBenGross) - (ElecSupplyCostGross + GasSupplyCostGross + WaterEnergyCostGross + OtherCostGross)
```

**Costs** (`NoAdmin` leaves out the program-level term `share * SumCostsNPV`):

```text
PACCostNoAdmin  = RebatesandIncentsPV + Sup                         PACCost  = share * SumCostsNPV + PACCostNoAdmin
TRCCostNoAdmin  = RebatesandIncentsPV + NetParticipantCostAdjustedPV + Sup  TRCCost  = share * SumCostsNPV + TRCCostNoAdmin
TRCCostGross    = share_gross * SumCostsNPV + RebatesandIncentsPV + GrossParticipantCostPV + SupGross          SupGross = the four Gross supply/other costs summed
RIMCostNoAdmin  = LifecycleBillReducElec + LifecycleBillReducGas + PACCostNoAdmin     RIMCost  = LifecycleBillReducElec + LifecycleBillReducGas + PACCost

SCBCostNoAdmin  = RebatesandIncentsPV_SCT + NetParticipantCostAdjustedPV_SCT + SupSB                      SupSB = ElecSupplyCost_SB + GasSupplyCost_SB + WaterEnergyCost_SB + OtherCost
SCBCost         = share_SB * SumCostsNPV_SCT + SCBCostNoAdmin
SCBCostGross    = share_SB_gross * SumCostsNPV_SCT + RebatesandIncentsPV_SCT + GrossParticipantCostPV_SCT + SupSBGross
SCHCost..., SCHCostNoAdmin, SCHCostGross: the same with _SH
```

**Ratios** (`ratio(n, d)`, so a zero cost gives 0). Each test's numerator is written as its own column (`TRCBen` ... `SCHBen`, beside its ratios), so every ratio
is its `...Ben` column over its cost column in the same file:

```text
TRCBen = B + TaxCredits                                       TRCRatio = TRCBen / TRCCost       TRCRatioNoAdmin = TRCBen / TRCCostNoAdmin
PACBen = B                                                    PACRatio = PACBen / PACCost       PACRatioNoAdmin = PACBen / PACCostNoAdmin
RIMBen = B + LifecycleBillIncrElec + LifecycleBillIncrGas     RIMRatio = RIMBen / RIMCost       RIMRatioNoAdmin = RIMBen / RIMCostNoAdmin
SCBBen = ElecBen_SB + GasBen_SB + WaterEnergyBen_SB + OtherBen     SCBRatio = SCBBen / SCBCost       SCBRatioNoAdmin = SCBBen / SCBCostNoAdmin
SCHBen = ElecBen_SH + GasBen_SH + WaterEnergyBen_SH + OtherBen     SCHRatio = SCHBen / SCHCost       SCHRatioNoAdmin = SCHBen / SCHCostNoAdmin
```

Water-energy benefits are in every numerator and TSB; water-energy costs are in every cost. Supply costs are in the *cost*, not netted from the ratio's numerator.

**Participant Cost Test** (CA Standard Practice Manual, Chapter 2; the CET does not calculate it). From the participant's side, on gross savings and without market effects:

```text
LifecycleBillReducElecGross ... LifecycleBillIncrGasGross = C9's bill impacts with NTG = 1 and MEB = 0 (same rates, discounting and fuel-substitution split)
PCTBen   = LifecycleBillReducElecGross + LifecycleBillReducGasGross + RebatesandIncentsPV + TaxCreditsGrossPV          (TaxCreditsGross / PVF, PVF of C8; not written)
PCTCost  = GrossParticipantCostPV + RebatesandIncentsPV + LifecycleBillIncrElecGross + LifecycleBillIncrGasGross          (= the measure cost before incentives + bill increases)
PCTRatio = PCTBen / PCTCost                                                                              (0 when PCTCost = 0)
```

Not yet checked against a CPUC definition: CPUC has not published how the CET will calculate the test. The portfolio `PCT_ratio` is `SUM(PCTBen) / SUM(PCTCost)`.

---------------------------------------------------------------------------------------------------------------------------------

## C13 Where market effects enter

CET's run-level **Market Effects** adds `MEB` to the net-to-gross of benefits and `MEC` to the net-to-gross of costs. CET stores them as two settings (`MarketEffectBens`,
`MarketEffectCost`); this tool sets both from one option: "Apply 5% market effects (benefits and costs)" (`market_effects`, `--market-effects 0.05`) makes `MEB = MEC = 0.05`,
off makes both 0.

| Quantity | Effect |
|---|---|
| `ElecBen`, `ElecSupplyCost` and every `_SB`/`_SH` version | `NTGRkWh` becomes `NTGRkWh + MEB` (C5.1) |
| `GasBen`, `GasSupplyCost` | `NTGRTherm` becomes `NTGRTherm + MEB` (C5.4) |
| `WaterEnergyBen`, `WaterEnergyCost` | `NTGRkWh + MEB` (C5.5) |
| `OtherBen`, `TaxCredits` | `NTGRkWh + MEB` (C7) |
| `OtherCost` | `NTGRkWh + MEC` (C7) |
| `NetParticipantCostAdjustedPV` (and `_SCT`), so the TRC and SCT costs | adds `MEC * (GrossMeasCostPV + ExcessIncentivesPV)` = `MarkEffectPlusExcessIncPV` (C8); `NetParticipantCostPV` itself does not change |
| `...Gross` columns | divided by `NTG + MEB`, i.e. the market-effects adder is undone along with NTG (C10) |
| bill impacts, RIM's bill terms | `NTGRkWh + MEB` / `NTGRTherm + MEB` (C9) |
| net emissions | `NTGRkWh + MEB` / `NTGRTherm + MEB` (C14) |
| combined 8,760 profile | **not** included (market effects are an accounting adder, not physical savings) |

**Validated** (2026-10-08) on CET jobs 121842 (0%) and 121841 (5%), the same 13-measure PG&E job: every column the setting changes in CET changes the same way here
(benefits, net emissions, bill impacts, TRC / SCT costs, participant costs; `NetParticipantCostPV`, the Gross columns and the PAC cost do not change in either), and the
participant-cost columns match CET to the cent. CET's run used its avoided-cost Version 2024 (the 2022 ACC), which this tool did not have yet, so benefit levels were compared as ratios.

---------------------------------------------------------------------------------------------------------------------------------

## C14 Emissions

Emission columns are produced when "Include emissions" is on. "First-year" is the measure's **first four quarters** of life (not the calendar year); "Lifecycle" is all of them; "Net" includes NTG (and `MEB`), "Gross" does not.
Units: **CO2 in metric tons** (confirmed; the ACC's rate is `tonnes/kWh` and RunParameters `CO2Gas` = 0.00531 (0.00585 for the 2021 ACC) is metric tons per therm, so CET's SQL comments that say "lbs" for CO2 are wrong). NOx and PM10 are in **pounds** (confirmed).

**C14.1 Electric CO2 per annual kWh, by quarter** (`co2(qidx)`, `qidx = 4 * year + quarter`, quarter 1-4):

```text
co2(year, q) = SUM over hours i in quarter q of  shape_year(i) * rate(year, i)         rate = the ACC's emission_rate_tonnes_per_kwh (same zone / IOU routing as C3.1)
```

Quarters exist only for years present in the ACC file: life beyond its last year adds **no** emissions (unlike prices, which use the nearest year).

**C14.2 Kilowatt-hours in a quarter.** With `eul = EUL`, `rul = RUL` (0 if `RUL >= EUL`), `rulq = 4 * rul`, `eulq = 4 * eul`, quarter offset `k = qidx - install` where `install = 4 * Y0 + q0`, and per-unit `kWh1, kWh2` (`Used_UnitkWh1stBaseline`, `Used_UnitkWh2ndBaseline`; on a kWh-per-unit
profile, C4.5, the profile's hours carry the kWh):

```text
single baseline (rulq = 0):   AnnualkWh(k) = 0 if kWh1 = 0;   kWh1 * (eulq mod 1) if k < eulq < k + 1 (the last part-quarter);   else kWh1
dual baseline:                0 if kWh1 = kWh2 = 0;   kWh1 if rulq > k + 1;      kWh2 if rulq < k and eulq > k + 1;
                              kWh1*(rulq mod 1) + kWh2*(eulq - rulq)     if the RUL and the end of life fall in the same quarter;
                              kWh1*(rulq mod 1) + kWh2*(1 - rulq mod 1)  if only the RUL falls in it;      kWh2*(eulq - floor(eulq))  if only the end of life does;   else kWh2
quarters counted: install <= qidx < install + eulq
```

**C14.3 Assembly.**

```text
fe(net)  = Q * (NTGe + MEB, or 1 if gross) * IRe * RRe                      fg(net) = Q * (NTGt + MEB, or 1 if gross) * IRt * RRt

ElecCO2   = fe * SUM over counted quarters of  co2(qidx)      * AnnualkWh(k)
ElecNOx   = fe * SUM over counted quarters of  nox            * AnnualkWh(k) / 4          nox  = Emissions NOx for   (PA, target sector, end use, zone)   (0 if there is no row)
PM10      = fe * SUM over counted quarters of  pm10           * AnnualkWh(k) / 4          pm10 = Emissions PM10, same key
   first-year: the first four counted quarters;   lifecycle: all of them

GasCO2    = fg * CO2Gas(PA) * G          GasNOx = fg * NOxfactor * G          NOxfactor = CombustionType-table NOx for the measure's CombustionType (0 if blank / unknown)
   G_firstyear = Th1 if rul >= 1;  rul*Th1 + (1 - rul)*Th2 if 0 < rul < 1 <= eul;  rul*Th1 + (eul - rul)*Th2 if rul < eul < 1;  Th1 if rul = 0 and eul > 1;  eul*Th1 if rul = 0 and eul <= 1
   G_lifecycle = rul*Th1 + (eul - rul)*Th2 if rul > 0;   eul*Th1 otherwise
```

The Emissions table covers only a slice of the (PA, sector, end use, zone) keys, so NOx and PM10 are 0 for most electric measures (this is CET's own behaviour). Output columns: `FirstYearNetElecCO2`, `FirstYearNetElecNOx`, `FirstYearNetElecPM10`, `FirstYearNetGasCO2`, `FirstYearNetGasNOx`,
the five `FirstYearGross...` counterparts, and the ten `Lifecycle...` columns (`LifecycleNet...`, `LifecycleGross...`).

---------------------------------------------------------------------------------------------------------------------------------

## C15 Statewide allocation

With "Allocate statewide programs" on, each statewide program's rows are split across the four IOUs **before** any calculation, using CET's allocation factors: a table with one row per (`Year`, `ProgramID`) holding each IOU's
`GasFactor`, `ElectricFactor` and `WeightedFactor` (each set sums to 1 across PGE / SDGE / SCE / SCG), and a rule per field:

| Rule | `Measure.csv` fields | `ProgramCost.csv` fields |
|---|---|---|
| Electric factor (x `ElectricFactor`) | `UnitkW1stBaseline`, `UnitkWh1stBaseline`, `UnitkW2ndBaseline`, `UnitkWh2ndBaseline`, `UnitkWhIOUWater1stBaseline`, `UnitkWhIOUWater2ndBaseline` | - |
| Gas factor (x `GasFactor`) | `UnitTherm1stBaseline`, `UnitTherm2ndBaseline` | - |
| Weighted factor (x `WeightedFactor`) | the unit costs and benefits (`UnitMeaCost...`, `UnitDirectInstall...`, `UnitEndUserRebate`, `UnitIncentiveToOthers`, `UnitGasInfraBens`, `UnitRefrigCosts`, `UnitRefrigBens`, `UnitMiscCosts`, `UnitMiscBens`, `UnitTaxCredits`) | all ten cost fields and `OnBillFinancing` |
| Pass through (unchanged) | everything else | `PA`, `PrgID`, `PrgYear`, `ClaimYearQuarter` |

The factors are the built-in table, with the rows of a supplied SW allocation factors file replacing matching (`Year`, `ProgramID`) rows and adding new ones.
For every IOU with a non-zero factor, one row is made with `<IOU>_` prepended to `CEInputID` and `PrgID` (program-cost `PA` is set to that IOU). A measure is still priced on its own `IOU_AC_Territory`; a program's costs are shared
only among that IOU's measures and discounted at that IOU's rate. Without the option, statewide rows are priced as filed (the way the four real CET runs treated them). `CreditedPA` is the `PA` of each measure's program in `ProgramCost.csv`: with the option, the IOU each statewide share was allocated to; without it, the filed `PA` (`SW` for a statewide program).

The run writes `statewide_reconciliation_acc<version>` (OUTPUT_FIELDS 2.13): each allocated field of each statewide program and year, totalled before
allocation and over each IOU's share, with the difference. Factors that sum to 1 make every field reconcile; a fuel whose factors are all 0 for a program
(gas on an all-electric program) drops that fuel's quantities from every share, and the table and the run log say so.

---------------------------------------------------------------------------------------------------------------------------------

## C16 Optional and secondary outputs

**C16.1 "Calculate ACC components separately".** Every fuel value in C5 is a sum over ACC components, so each component can be priced alone through exactly the same route:

```text
ElecValue_<c> = lifetime PV (TRC rows) of C5.1 with the component column c in place of total_avoided_cost      for every component c present in the electric ACC
GasValue_<c>  = lifetime PV (TRC rows) of C5.4 with the component c in place of the total                             c = market, td, nox, co2, upstream_methane, behind_meter_methane, air_quality_adder

SUM_c ElecValue_<c> = ElecBen - ElecSupplyCost        SUM_c GasValue_<c> = GasBen - GasSupplyCost          (checked on every run and reported in the log)
```

`behind_meter_methane` is zero for a measure that does not include it (C3.2). The components are reported *before* the benefit / supply-cost split, so they always add up.
`components_summary_*` totals each `ElecValue_*` / `GasValue_*` over the portfolio with its share of the fuel total and a check row (total minus the sum of components).

**C16.2 "Produce combined 8760 electric load profile".** For each requested calendar year `Y`, the net kWh the portfolio delivers in each hour:

```text
kWh(i, Y) = SUM over measures m of  weight_m(Y) * shape_m,Y(i)
weight_m(Y) = overlap( [Y, Y+1), 1st-baseline period ) * S1e + overlap( [Y, Y+1), 2nd-baseline period ) * S2e
   start = Y0 + (q0 - 1)/4     end = start + EUL     1st-baseline period = [start, start + RUL) (or [start, end) if RUL = 0 or RUL >= EUL)     2nd = [start + RUL, end)     overlap in years
```

`S1e`, `S2e` are `AC_Net_kWh_ScaledProfile_*_Baseline` (net, with IR and RR, without market effects). Each measure's 8,760 profile (supplied or library) is aligned to the ACC's weekdays (C4.2), and the `datetime` column labels the hours on the ACC's calendar (2018 or 2020); a measure with no shape is left out and listed in `portfolio_8760_unshaped_*`. Positive = savings, negative = added load. `net_kwh_<Y>` sums all IOUs, `net_kwh_<Y>_<PA>` by
territory and `net_kwh_<Y>_CZ<zone>` by climate zone (BRD BR-06.13); `portfolio_8760_peaks_*` gives each one's peak savings hour, largest added load and kWh in the
portfolio's peak hour (OUTPUT_FIELDS 2.4b).

**C16.3 Portfolio summary.** `portfolio_summary_*` has one row for all measures, one per `IOU_AC_Territory`, one per `CreditedPA`, one per program segment (`Prg_ProgramSegment`, from the program metadata) and one per program year (`Used_Year`, BRD BR-04.1), with the sum of each of: `ElecBen`, `GasBen`, `WaterEnergyBen`, `OtherBen`, `ElecSupplyCost`, `GasSupplyCost`,
`WaterEnergyCost`, `TotalSystemBenefit`, `TRCBen`, `PACBen`, `RIMBen`, `SCBBen`, `SCHBen`, `TRCCost`, `PACCost`, `RIMCost`, `SCBCost`, `SCHCost`, `TRCCostNoAdmin`, `PACCostNoAdmin`, `WeightedProgramCost`, `LifecycleBillReducElec`, `LifecycleBillReducGas`, `PCTBen`, `PCTCost`, `FirstYearNetElecCO2`, `FirstYearNetGasCO2`, and portfolio ratios on CET's receipt definition (plus `PCT_ratio = sum(PCTBen) / sum(PCTCost)`):

```text
B      = sum(ElecBen) + sum(GasBen) + sum(WaterEnergyBen) + sum(OtherBen)            B_SB, B_SH: the same with the _SB / _SH benefits
TRC_ratio = (B + sum(TaxCredits)) / sum(TRCCost)         PAC_ratio = B / sum(PACCost)         RIM_ratio = (B + sum(LifecycleBillIncrElec) + sum(LifecycleBillIncrGas)) / sum(RIMCost)
SCB_ratio = B_SB / sum(SCBCost)                          SCH_ratio = B_SH / sum(SCHCost)      TRC_NoAdmin_ratio, PAC_NoAdmin_ratio: over the NoAdmin costs
```

The numerators are the sums of the measure-level `...Ben` columns (`B + sum(TaxCredits) = sum(TRCBen)`, ...), so each ratio is that row's `...Ben` over its cost column.

A ratio is omitted when its inputs are blank (for example the societal ratios when SCB/SCH are off).

**C16.4 Comparison outputs.** *Two ACC versions* (`comparison_acc<A>_vs_acc<B>`): for each key output `c`, per measure `c_<A>`, `c_<B>`, `c_delta = B - A` and `c_pct = (B - A) / |A|` (blank if `A = 0`); the summary gives each metric's portfolio total under both and the
difference. *Against a CET outputs file* (`cet_comparison_*`): per measure `c_poc`, `c_cet`, `c_diff = poc - cet` and `c_ratio = poc / cet`; the summary gives the portfolio totals and `POC/CET`.

---------------------------------------------------------------------------------------------------------------------------------

## C17 Validation, deliberate differences from CET, and limitations

**Validation** (re-run 2026-10-22 on the current engine: every measure on its 8,760, the 2018 alignment of C4.2, the TOU 8,760s on the 2026 ACC's 2018 TOU map).
The reference is four real CET runs of PG&E's consolidated 2028-2031 portfolio (runs 121219-121222; 21,370 measures, all matched; 2024 ACC = Avoided Cost Version 2026;
statewide programs unallocated; First Year = claim year; 0% market effects), priced with no CET-derived table in the calculation path. Portfolio totals, ReCET / CET, pooled over the four runs:

| Output | ReCET / CET | Note |
|---|---|---|
| `GasBen`, `GasBen_SB`, `GasBen_SH`, `OtherBen`, `RebatesandIncentsPV`, `NetParticipantCostPV`, `WeightedProgramCost`, `TotalExpenditures` | 1.0000 | `GasBen` agrees on every measure |
| every savings column (C19.1-C19.3), the nominal participant-cost columns (C8), the program-cost components (C11.4), `DiscountedSavingsNet...` | 1.0000 | every measure (discounted savings within 2e-6) |
| `LifecycleBillReducElec`, `LifecycleBillIncrElec` | 1.0000 | every measure within 1% |
| `LifecycleBillReducGas` | 0.9995 | |
| `TRCCost` / `PACCost` / `RIMCost` | 0.9992 / 0.9956 / 0.9997 | |
| `SCBCost`, `SCHCost` | 0.9995 / 0.9996 | |
| `FirstYearNetGasCO2`, `FirstYearNetElecNOx`, `FirstYearNetGasNOx`, `FirstYearNetElecPM10` | 1.0000 | |
| `ElecBen` | 1.5072 | TOU end uses 1.5297 (5,700 measures, 97% of CET's `ElecBen`), DEER end uses 0.9782 (15,670 measures). The TOU gap is deliberate difference 7: CET's TOU T&D is about 1,000 times too small |
| `ElecSupplyCost` | 0.9484 | TOU 1.5427, DEER 0.9456 |
| `ElecBen_SB`, `ElecBen_SH` | 1.3383 / 1.3383 | TOU 1.349 / 1.347 (difference 7, and CET's TOU SCT double-counts cap-and-trade carbon); DEER 1.0018 / 1.0531 (difference 4) |
| `WaterEnergyBen` | 0.9742 | `_SB` 1.0156, `_SH` 1.0293 (CET holds `_SB` = `_SH` here) |
| `TotalSystemBenefit` | 1.2632 | **1.0013 without the TOU end uses** |
| portfolio `TRC` / `PAC` / `RIM` ratio (receipt definition) | 2.0369 / 11.1496 / 0.6450 vs CET 1.6153 / 8.8097 / 0.5157 | without the TOU end uses the TRC ratio is 0.9997 of CET's |
| `FirstYearNetElecCO2` | 1.0631 | first year; TOU 1.0785, DEER 1.0032 |
| `LifecycleNetElecCO2` | 0.9945 | |

Per measure: `GasBen` agrees on all 21,370 measures and `LifecycleBillReducElec` on all to within 1%; 63% of measures are within 1% on `TRCCost` (the rest carry TOU or DEER
electric differences into their supply costs); no electric measure is unpriced. Reproduce with `python poc/recheck_recet_vs_cet.py` in the CEDARS-Research
repository, which runs each year on the 2024 ACC with its claim year as First Year against its CET output (`poc/data/consolidated_<year>`,
`poc/data/cet_output_<year>_run<id>`) and writes these totals, the TOU / DEER split and the per-measure figures.

**Deliberate differences from CET** (the tool does not copy what appear to be CET data errors; each is logged in the project notes):

1. The Gross cost columns (`TRCCostGross`, `SCBCostGross`, `SCHCostGross`) use *gross* supply costs; CET adds the net supply costs to the gross participant cost.
2. `GasSupplyCostGross` is the net value divided by NTG; CET assigns it the net value.
3. `SCBCost` / `SCHCost` discount program costs at the societal rate; CET uses the primary rate for them.
4. Electric `_SB` / `_SH` are priced on the real `SCT Base` / `SCT High` rows for every measure. For legacy DEER-named end uses CET's table holds TRC values in its SCT columns (so CET's SB = SH there).
5. (Withdrawn 2026-10-23: `WeightedElecAlloc` is produced and follows CET's formula, C11.4. CalcCE's `_SB`/`_SH` mix-up in `BenPos` touches no exported column.)
6. Unallocated statewide rows use `PA = SCE` for discounting, as CET did in the real runs; the effect is nil when every cost falls in the First Year.
7. Every electric measure is priced from an 8,760 with the 2018 calendar alignment of C4.2 (2026-10-02). CET instead prices TOU end uses from CEDARS' TOU calculators (Gen at
   full-year period means; T&D on kW with a duplicate /1000, about 1,000 times too small) and shifts other profiles to each forecast year's weekdays with a one-day error on
   negative offsets; it prices SCG from its own quarterly table.
8. **TOU societal (`_SB` / `_SH`) electric values** are priced like every other end use, on the ACC's own `SCT Base` / `SCT High` rows (which leave out the cap-and-trade carbon component).
   CET's TOU societal values keep the cap-and-trade component alongside the social cost of carbon, so carbon is counted twice (about $21/MWh, 13% of the price), and they carry its TOU
   T&D error (item 7). This is a deliberate departure from CET, confirmed 2026-10-23; it is why TOU `ElecBen_SB` / `_SH` differ from CET's in the table above.
9. `LevBenElec` is `ElecBen / DiscountedSavingsNetkWh`. CET's exported `LevBenElec` is `ElecBen_SB / DiscountedSavingsNetkWh` on every measure: CalcCost's `LevelizedCost` step names its
   three `LevBenElec` columns in a different order from the values it selects, so the TRC value lands under another name.
10. `DiscountedSavingsGross...` on early-replacement measures (`RUL_Yrs` > 0): ReCET's is the net formula without NTG (C19.5). CET's gross kWh counts one 2nd-baseline year too many
   (`Raf ^ -(EUL - RUL)` where its net version has `- 1`) and its gross therms leave the 2nd baseline undiscounted (a misplaced bracket), so CET's gross and net disagree even at NTG = 1.
   Single-baseline measures match CET.
11. **PrgIDs that do not line up between `Measure.csv` and `ProgramCost.csv`** do not stop the run (BRD BR-06.16, 0.1.59). CET stops the job on either kind. A measure whose program
   has no `ProgramCost.csv` row is priced with no program costs (C11: its share is of a program total of 0), and a `ProgramCost.csv` row whose program has no measures is charged to
   none; preflight warns about both (`measures_without_program_costs`, `program_costs_without_measures`, with the costs left out). A job whose PrgIDs all line up gives CET's results.

**Limitations.**

* **Unpriced measures.** An electric measure with no supplied profile and no library profile is reported `UNPRICED` and its benefit is 0. Gas measures with a blank or unknown profile name are unpriced the same way (CET also prices those at 0).
* Only the four IOUs are valid `IOU_AC_Territory` values.
* SCG electric is priced on the SCG routing of C3.1: one utility's whole bundle per zone, SCE unless SCE has no data for the zone (then PG&E). This matches CEDARS'
  DEER-path SCG values in zones 4, 5 (PG&E) and 13 (SCE); CEDARS' own SCG hourly files instead pair SCE generation with PG&E T&D in zones 4, 5 and 13, so the POC
  differs from them there. CET's quarterly SCG table is not used.
* Not validated against CET, because the real runs contain none: **`WaterEnergyCost`** and second-baseline water, the **2021, 2022 and 2026 ACCs** (no CET run has used them),
  statewide allocation of program costs, the Participant Cost Test (CET has none), already-scaled and load-shifting profiles (C4.5) and ACC versions per measure (C3.5).
  Market effects at 5% match CET jobs 121842 / 121841 on participant costs and bill impacts.
  These paths are covered by the hand-computed tests in `tests`.
* The gas ACCs are extracted from E3's workbooks, not taken from CET. The project notes record an unexplained 0.7-2.9% gap between the workbook's gas Commodity and CET's own gas-table export; it does not show in the real runs, where `GasBen` agrees to 4 decimals.
* Emissions beyond the last ACC year are not counted (C14.1), and NOx/PM10 come from the Emissions table (CET's), which covers only part of the keys.
* Input columns that are read but not used: `RateScheduleElec`, `RateScheduleGas` (rate schedules always come from `PA` and sector), and every descriptive column (INPUT_FIELDS.md lists them).
* Every CET measure-level output column is produced (2026-10-23) except `JobID` (CET's job number) and `PA`, which in CET's output is the measure's `IOU_AC_Territory` (echoed with the
  inputs). Whether the levelized `Lev...` columns are still needed is RAID decision D9 in the replacement BRD.

---------------------------------------------------------------------------------------------------------------------------------

## C18 Worked example

One measure, priced against constant (flat) avoided costs and a flat profile so that every step can be checked with a calculator. The job is exactly the one in `tests/test_docs.py`, which re-runs it and checks the numbers below.

**Inputs.** A PG&E residential measure, climate zone 13, claimed in **2028Q3**, First Year 2028, 2026-ACC run parameters (`DiscountRateQtr` = 0.01915, `DiscountRateAnnual` = 0.0766, societal 0.012575 / 0.0503).

| `Measure.csv` field | Value | `Measure.csv` field | Value |
|---|---|---|---|
| `NumUnits` (Q) | 100 | `UnitMeaCost1stBaseline` | 200 |
| `EUL_Yrs` / `RUL_Yrs` | 2.4 / 0 | `UnitIncentiveToOthers` / `UnitEndUserRebate` | 30 / 20 |
| `UnitkWh1stBaseline` | 500 | `UnitDirectInstallLab` / `UnitDirectInstallMat` | 5 / 5 |
| `UnitTherm1stBaseline` | 20 | `UnitRefrigCosts` / `UnitMiscBens` / `UnitTaxCredits` | 1 / 2 / 5 |
| `UnitkWhIOUWater1stBaseline` | 3 | `NTGRkWh` / `NTGRTherm` / `NTGRCost` | 0.8 / 0.9 / 0.9 |
| `AC_Gas_Impact_Profile` | `Annual` (1/12 per month) | `InstallationRatekWh` / `RealizationRatekWh` | 0.95 / 0.9 |
| shape | flat (1/8760 per hour) | `InstallationRateTherm` / `RealizationRateTherm` | 1 / 0.9 |

ACC: electric $80/MWh (TRC) = energy 70 + transmission 5 + distribution 5, so `AC` = $0.080/kWh and `Gen` = $0.070/kWh; `SCT Base` $88, `SCT High` $96 (Gen 78 / 86). Gas $1.00/therm (TRC), $1.20 (SCT Base), $1.40 (SCT High). Emission rate 0.0002 t/kWh.
Program `PRG1`: 2028 costs 1,500 (admin) + 500 (marketing); 2029 costs 1,000 (EMV). No market effects.

**C1 derived fields.** `AC_Net_kWh_1st_Baseline` = 100 x 500 x 0.8 x (0.95 x 0.9) = **34,200 kWh/yr** (also the profile multiplier); `AC_Net_Therms_1st_Baseline` = 100 x 20 x 0.9 x (1 x 0.9) = **1,620 therms/yr**;
`AC_Net_kWh_Water_1st_Baseline` = 100 x 3 x 0.8 x 0.855 = **205.2 kWh/yr**.

**C2 quarters.** EUL 2.4 years = 9.6 quarters, so ten quarters (k = 0-9), the last weighted 0.6. Claim quarter 3, so k = 0 is 2028Q3. `DF(k) = 1 / 1.01915^Qac`, `Qac = (year - 2028) x 4 + quarter - 1`.

**C5 quarter by quarter** (electric and water: annual kWh x $/kWh x hours/8760; gas: annual therms x $1.00 x months/12):

<!-- worked-example-quarters:start -->
| k | Quarter | Hours | Weight | DF | Electric $ (`V_elec`) | Gas $ (`V_gas`) | Water $ (`V_water`) |
|---|---|---|---|---|---|---|---|
| 0 | 2028Q3 | 2208 | 1.0 | 0.962773 | 663.95 | 389.92 | 3.49 |
| 1 | 2028Q4 | 2208 | 1.0 | 0.944682 | 651.47 | 382.60 | 3.42 |
| 2 | 2029Q1 | 2160 | 1.0 | 0.926931 | 625.34 | 375.41 | 3.28 |
| 3 | 2029Q2 | 2184 | 1.0 | 0.909514 | 620.40 | 368.35 | 3.26 |
| 4 | 2029Q3 | 2208 | 1.0 | 0.892424 | 615.44 | 361.43 | 3.23 |
| 5 | 2029Q4 | 2208 | 1.0 | 0.875655 | 603.87 | 354.64 | 3.17 |
| 6 | 2030Q1 | 2160 | 1.0 | 0.859202 | 579.64 | 347.98 | 3.04 |
| 7 | 2030Q2 | 2184 | 1.0 | 0.843057 | 575.07 | 341.44 | 3.02 |
| 8 | 2030Q3 | 2208 | 1.0 | 0.827216 | 570.47 | 335.02 | 2.99 |
| 9 | 2030Q4 | 2208 | 0.6 | 0.811672 | 335.85 | 197.24 | 1.76 |
<!-- worked-example-quarters:end -->

Sums: `V_elec` = 5,841.50, `V_gas` = 3,454.03, `V_water` = 30.67. (Row 0: 34,200 x 0.080 x 2208/8760 x 0.962773 = 663.95; gas row 0: 1,620 x 1.00 x 3/12 x 0.962773 = 389.92; water row 0: 205.2 x 0.070 x 2208/8760 x 0.962773 = 3.49.)

**C6 / C7 / C8 / C9.** Not a fuel-substitution measure, so `ElecBen` = `V_elec`, `GasBen` = `V_gas`, all supply costs 0; `Wt1 > 0` so `WaterEnergyBen` = `V_water`. `OtherBen` = 100 x (0.8) x (0 + 0 + 2) = 160; `OtherCost` = 100 x 0.8 x 1 = 80; `TaxCredits` = 100 x 0.8 x 5 = 400.
Qm = (2028 - 2028) x 4 + 2 = 2, `PVF` = 1.01915^2 = 1.038667. `IncentsDI` = 100 x 40 = 4,000, `Rebates` = 2,000, so `RebatesandIncentsPV` = 6,000 / 1.038667 = 5,776.64. `GrossMeasCostPV` = 100 x 200 / 1.038667 = 19,255.4;
excess = pos(4,000 / 1.038667 - 19,255.4) = 0; `GrossParticipantCostPV` = 19,255.4 - 5,776.64 = 13,478.82; `NetParticipantCostPV` = 0.9 x 13,478.82 = 12,130.94. Bill impact (C9): yr1 = 4, floor_rul = 2, boundary share 1, three billed years;
`RateE` = 0.406249518 / 0.418437003 / 0.430990113 $/kWh for 2028 / 2029 / 2030, so `LifecycleBillReducElec` = 34,200 x 0.406249518 + 34,200 x 0.418437003 / 1.0766 + 0.4 x 34,200 x 0.430990113 / 1.0766^6 = 13,893.73 + 13,292.5 + 3,786.6 = 30,972.49.

**C11 / C12.** One measure, so its share is 1: `WeightedBenefits` = 1, `WeightedProgramCost` = 2,000 + 1,000 = 3,000; `SumCostsNPV` = 2,000 + 1,000 / 1.0766 = 2,928.85.
`TotalSystemBenefit` = (5,841.50 + 3,454.03 + 30.67 + 160) - (0 + 0 + 0 + 80) = 9,406.19. `PACCostNoAdmin` = 5,776.64 + 80 = 5,856.64; `PACCost` = 2,928.85 + 5,856.64 = 8,785.49.
`TRCCostNoAdmin` = 5,776.64 + 12,130.94 + 80 = 17,987.57; `TRCCost` = 20,916.42; `TRCRatio` = (9,486.19 + 400) / 20,916.42 = 0.4727 (B = 9,486.19 is the four benefits summed).

**Results of the run:**

<!-- worked-example-outputs:start -->
| Output | Value | Output | Value |
|---|---|---|---|
| `ElecBen` | 5,841.50 | `TotalSystemBenefit` | 9,406.19 |
| `GasBen` | 3,454.03 | `WeightedProgramCost` | 3,000.00 |
| `WaterEnergyBen` | 30.67 | `PACCostNoAdmin` | 5,856.64 |
| `OtherBen` | 160.00 | `PACCost` | 8,785.49 |
| `OtherCost` | 80.00 | `TRCCostNoAdmin` | 17,987.57 |
| `TaxCredits` | 400.00 | `TRCCost` | 20,916.42 |
| `RebatesandIncentsPV` | 5,776.64 | `RIMCost` | 49,925.68 |
| `GrossParticipantCostPV` | 13,478.82 | `SCBCost` | 21,172.997 |
| `NetParticipantCostPV` | 12,130.94 | `SCHCost` | 21,172.997 |
| `LifecycleBillReducElec` | 30,972.49 | `TRCRatio` | 0.4727 |
| `LifecycleBillReducGas` | 10,167.71 | `PACRatio` | 1.0798 |
| `ElecBen_SB` | 6,688.34 | `RIMRatio` | 0.1900 |
| `ElecBen_SH` | 7,296.37 | `SCBRatio` | 0.5289 |
| `GasBen_SB` | 4,314.29 | `SCHRatio` | 0.5917 |
| `GasBen_SH` | 5,033.34 | `FirstYearNetElecCO2` | 6.84 |
| `WaterEnergyBen_SB` | 35.57 | `LifecycleNetElecCO2` | 16.438 |
| `WaterEnergyBen_SH` | 39.22 | `FirstYearNetGasCO2` | 8.6022 |
| `ElecBenGross` | 7,301.87 | `LifecycleNetGasCO2` | 20.645 |
| `GasBenGross` | 3,837.81 | `TotalSystemBenefitGross` | 11,278.01 |
| `RebatesandIncentsPV_SCT` | 5,851.90 | `TRCCostGross` | 22,284.30 |
<!-- worked-example-outputs:end -->

Emissions by hand (C14): first year = the first four quarters, hours 2208 + 2208 + 2160 + 2184 = 8,760, so `FirstYearNetElecCO2` = 68.4 x 0.0002 x 500 x 8,760/8,760 = 6.84 (68.4 = Q x NTG x IR x RR = 100 x 0.8 x 0.855); the lifecycle sum
covers 21,052.8 hours, giving 16.438. `FirstYearNetGasCO2` = 100 x 0.9 x 0.9 x 0.00531 x 20 = 8.6022 and lifecycle uses 2.4 x 20 = 48 therms: 20.645.
The societal values follow C5.6: for example `ElecBen_SB` reprices the same quarters at $0.088/kWh and `DF_sct` = 1 / 1.012575^Qac.

---------------------------------------------------------------------------------------------------------------------------------

## C19 Savings quantities, discounted savings and levelized values

CET's savings columns (CalcSavings) and the columns CalcCost builds on them. All use `EUL_Yrs` and `RUL_Yrs` as filed, as the pricing does (an `RUL_Yrs` equal to `EUL_Yrs` gives the same values as `RUL` = 0 in every one of them). For each fuel,
`a1` / `a2` are the per-unit 1st / 2nd-baseline amounts, `IR` / `RR` its installation and realization rates and `NTG` its net-to-gross ratio:

| Suffix | `a1` / `a2` | `IR`, `RR`, `NTG` |
|---|---|---|
| `KWh` | `kWh1 + Wt1` / `kWh2 + Wt2` (site plus IOU water) | `IRe`, `RRe`, `NTGRkWh` |
| `KWhSite` | `kWh1` / `kWh2` | the same |
| `KWhWater` | `Wt1` / `Wt2` | the same |
| `KW` | `kW1` / `kW2` | `IRkw`, `RRkw`, `NTGRkW` |
| `Thm` | `Th1` / `Th2` | `IRt`, `RRt`, `NTGt` |

Every `Net` column is its `Gross` column times `(NTG + MEB)`.

**C19.1 First year.**

```text
FirstYearGross<x> = Q * IR * RR * ( w1 * a1 + w2 * a2 )

w1 = 1 if RUL > 1;  RUL if 0 < RUL < 1;  EUL if RUL = 0 and EUL < 1;  1 otherwise
w2 = EUL - RUL if 0 < RUL < 1 and EUL < 1;  1 - RUL if 0 < RUL < 1 and EUL >= 1;  0 otherwise
```

An RUL of exactly 1 takes the "otherwise" branches, as CET's SQL does.

**C19.2 Lifecycle** (no `KW` column):

```text
LifecycleGross<x> = Q * IR * RR * EUL * a1                           if RUL = 0
                  = Q * IR * RR * ( a1 * RUL + a2 * (EUL - RUL) )     if RUL > 0
```

**C19.3 Annual average.** `AnnualGross<x> = LifecycleGross<x> / EUL` (0 when `EUL` = 0), the same expression for `KW`.

**C19.4 Savings share.** CET's `WeightedSavings`, a savings-based weight that no cost test uses (program costs are shared by benefits, C11.2), is not computed
(since 0.1.50).

**C19.5 Discounted savings.** Site kWh (no water) and therms, each year discounted at the annual rate, the whole brought to the First Year at the quarterly rate (`PVF` of C8):

```text
A(n) = 1 + (1 - Raf ^ -(n - 1)) / DiscountRateAnnual                 the sum of Raf ^ -t for t = 0 ... n-1 (n may be fractional)

DiscountedSavingsGross<f> = Q * IR * RR * a1 * A(EUL) / PVF                                           if RUL = 0
                          = Q * IR * RR * ( a1 * A(RUL) + a2 * A(EUL - RUL) / Raf ^ RUL ) / PVF       if RUL > 0
                          = 0                                                                          if a1 = 0 or EUL = 0
<f> = kWh (the KWhSite amounts) or Thm
```

These are the measure-level values of the measure's own ACC version (C3.5): `Raf`, `PVF` use that version's run parameters.

**C19.6 Lifecycle net benefits and levelized values.** With `W = WeightedElecAlloc` (C11.4), `DSe = DiscountedSavingsNetkWh`, `DSg = DiscountedSavingsNetThm`:

```text
TRCLifecycleNetBen = ElecBen + GasBen - TRCCost              PACLifecycleNetBen = ElecBen + GasBen - PACCost
LevBenElec = ratio(ElecBen, DSe)                             LevBenGas = ratio(GasBen, DSg)
Lev<T>     = ratio(<T> * W, DSe)                             <T> = TRCCost, TRCCostNoAdmin, PACCost, PACCostNoAdmin, RIMCost
LevNetBen<t>Elec<v> = ratio(ElecBen - <t>Cost<v> * W, DSe)   LevNetBen<t>Gas<v> = ratio(GasBen - <t>Cost<v> * (1 - W), DSg)       <t> = TRC, PAC (<v> = "" or NoAdmin), RIM
```

**Checks against CET.** C19.1-C19.3 match CET's own columns to rounding on every measure of jobs 121219-121222 (21,370 measures), 121842 (0% market effects) and 121841 (5%).
The net discounted savings match to 2e-6 (relative) on every measure. The program-cost components match CET's program totals exactly. The levelized and allocation columns, computed
from CET's own benefit, cost and discounted-savings columns, reproduce CET's on every measure except `LevBenElec` (C17, deliberate difference 9); with ReCET's own benefits they differ from
CET's as the benefits do (C17).

## C20 Prior CET releases (replicas)

ReCET's own calculation is the one described in C0-C19: hourly, on the ACC, with the corrections of C17. For evaluation and for re-running history,
`recet cet-version run` (module `recet/cet_versions`) prices a job **exactly as a prior CET release's RunCET does**, on that release's own quarterly
avoided-cost tables, **CET's errors included** (Jake, 2026-10-29: "exact replicas, labelled"). A replica's outputs are CET's four tables (`SavedCE`,
`SavedSavings`, `SavedEmissions`, `SavedCost`) with that release's columns in its order, and `run.json` names the release as "CET <release> replica".
The replica shares no code with ReCET's engine, so nothing in C0-C19 changes when a release is added.

**Releases.** One per CPUC database backup: 21.0 (2020-08-24), 22.1 (2021-12-09), 24.1 (2023-05-16), 24.2 (2023-09-20), 24.3 (2024-08-28), 25.0
(2025-05-18) and 25.1 (2026-10-07). `recet cet-version list` shows which ACCs each one has (an ACC is named by its year; the release's tables use CET's
Avoided Cost Version numbers, `versions.CET_VERSION_TO_ACC`). 25.1 lists a Version 2027 with no tables in its backup: Version 2027 was a corrected
Version 2026 (the 2024 ACC), and the 2026-10-07 backup carries only the corrected tables, under Version 2026 (Jake, 2026-10-29). So a 25.1 replica
on the 2024 ACC prices on the corrected tables. 21.0's backup lists a Version 2021 too, but the next ACC was not part of 21.0 (Jake, 2026-10-29): the
backup's tables for it are an unfinished load (Version labels `2020`, `2016`, `PG&E`). Neither Version is offered (`Release.not_offered`). Statewide programs: CEDARS does not pass statewide program data to CET (PA `SW` has no
E3Settings row), and statewide allocations began in 2022 (Jake, 2026-10-29); a replica prices the PA records it is given, as RunCET does. A CEDARS upload's Measure.csv has no PA column: a replica run gives each record its IOU_AC_Territory as its PA, whatever its program's PA (the program's PA only when the territory is blank; `cet_versions/job.cet_inputs`), and every ProgramCost row whose PA has no settings at the run's Version (no `E3Settings` row: `job.settings_pas`) PA SCE (`job.cet_programs`), so a PA given its own discount rates in a release's `E3Settings` is costed on them (Jake, 2026-10-29). On CET 25.1's Versions 2024 to 2027 the PAs with settings are the IOUs (and PAC); on Versions up to 2022 the CCAs (MCE, BAY, SCR, LCE, TCR, RCEA, SJCE, ...) have their own. CEDARS passes non-IOU program costs (statewide, CCA, REN) to CET as SCE's, SCE having historically funded most statewide programs, whatever the program's lead (Jake, 2026-10-29; CET_UI job 122738: 35 records under MCE, BAY, SCR, SDRN, SW and PGE programs reached CET with their territories as PA, and every non-IOU ProgramCost row as SCE). Checked against the published CET 25.1 (CET_UI jobs 122714 and 122716, 2024 ACC, First Year 2028, 10,000 records each, 8,927 statewide in 122714): the replica on the raw uploads equals CET's outputs in every one of the 180 columns CET exports, to 1e-9. A job already in CET's input form (a PA column) is priced as given.

**Engine.** A run's calculation engine is ReCET's own (the default) or one of these replicas (D13, BR-06.4): `RunConfig.engine` "cet-<release>",
`recet run --engine cet-24.3`, New run's **Calculation engine** (`cet_versions/job.py`). A replica run goes through the ordinary run path and writes the
release's four tables plus a measure-level file in ReCET's layout (the input columns, then the Saved* columns under ReCET's names, then the program
columns), so the Results and Compare pages read it and any two engines can be compared. `run_settings.json` records the engine (key, label, release,
backup date, CET Version). A replica refuses what only ReCET's engine does (supplied ACC or profile files, statewide allocation, components, a combined
8,760, a second ACC, the CEDARS filing checks and Check inputs only): a filing accepts only ReCET's current release. Its ACC must be one the release has.

**Acceptance.** A replica is accepted only when every output column of every measure equals that release's own RunCET run to 1e-9 (relative; NULL must
be NULL). `tests/test_cet_versions.py` checks 19 reference runs: for each release a 30-measure synthetic job built to exercise every branch (with and
without 5% market effects), and for 25.0 and 25.1 also two jobs of 41 and 50 real PG&E records. The runs were made on restored copies of the backups and
their table slices are in `tests/data/cet_versions/` (CEDARS-Research `poc/cet_version_fixtures.py`).

**What the replica copies from SQL Server.** NULL arithmetic (a NULL term makes the whole expression NULL; `SUM` skips NULLs; a `CASE` takes only a true
branch), `POWER` of a `decimal(18,6)` discount factor rounded to six places (the societal rates are floats and are not rounded), `1.000 / n` as a
14-place decimal, `UNION` dropping duplicate rows, text joins that ignore case and trailing spaces (but not a trailing line break), and column lists that
are matched by position, not by name.

**How the releases differ** (`releases.Release`; each switch names the releases it applies to):

| Area | 21.0 | 22.1 | 24.1 / 24.2 / 24.3 | 25.0 | 25.1 |
|---|---|---|---|---|---|
| CalcCE | its own procedure (`calc_ce_2021`): electric and gas benefits only | benefits and supply costs; quarter split guarded | the 22.1 layout with water energy | + tax credits, electric SCT | + gas and water SCT, bill increases |
| Fuel substitution | none | `MeasImpactType LIKE '%FuelSub'` | `'%FuelSub'` or `'%NC-AE'` | `FuelType` in a list | the list, or `FuelSub` impact type |
| Sign test for a fuel-substitution benefit | | first-baseline kWh / therms | 24.1, 24.2: the same; 24.3: the priced lifetime sum | priced sum | priced sum |
| Misc. benefits / costs in OtherBen / OtherCost | | no | 24.3 only | yes | yes |
| Savings table | 22.1 layout (goal attainment) | 22.1 layout | site / water split | the same | the same |
| Gas NOx | `E3CombustionType` lookup | lookup | constant 0.0092 | constant | lookup |

**CET errors the replicas keep** (each is marked "CET error kept" in the code):

* 21.0: the gross benefits multiply the first baseline by (1 + market effects) and leave its kW part unadjusted; `TRCCostGross` is the net TRC cost
  (a CTE column named by position). (21.0 also counts quarters from 1, not 0, and multiplies each benefit by `POWER(Rqf, Qm - 1) / POWER(Rqf, Qm)`;
  the replica does the same.)
* 24.1: the dual-baseline net lifecycle kWh are multiplied by EUL a second time.
* 24.3 and before: the gas rate-schedule mapping's `Commercial` and `Residential` sectors for PG&E, RCEA and SJCE end in a line break, so those measures
  match no gas rate and get no gas bill reduction.
* 25.0: `SaveCost` leaves `LevSCBCostNoAdmin` and `LevSCHCostNoAdmin` out of its insert list, so they are always blank.
* 25.0 and 25.1: `BenefitsSum`'s column list is out of step with its `SELECT`, so the gross program-cost weight uses the positive tax credits as the
  electric benefit; two positional lists rotate `LevBenElec`, `LevBenElec_SB` and `LevBenElec_SH`.
* Every release: the nominal `MarkEffectPlusExcessInc` adds the discounted excess incentive.
* Every release from 22.1: the net electric benefit multiplies the second-baseline kWh by a lifetime sum without `ISNULL`, so a measure whose life runs
  past the table (or whose RUL exceeds its EUL) gets no net electric benefit; `GasSupplyCostGross` is the net gas supply cost; the PAC weight adds the
  measure's own water and other benefits to the program's sums; the fractional-year bill terms are discounted from the table's first year.

**Tables for a real run.** Small tables (`E3Settings`, `CETAvoidedCostVersions`, the rate-schedule mappings, `E3CombustionType` and, before 25.1,
`E3RateScheduleGas`) ship in `recet/data/cet_versions/<release>/`. The large ones (the avoided-cost tables, `E3Emissions`, `E3RateScheduleElec`) are read
from the parquet export in the reference data folder, `<folder>/cet_versions/manifest.csv` and `tables/` (CEDARS-Research
`poc/export_cet_backup_tables.py`); only the job's PAs, end uses and climate zones are kept in memory.

**A large job.** A replica prices `replica_part_size` measures at a time (10,000), in two passes: the first prices each part's benefits and adds up the
sums CET takes over the whole job (each program's positive benefits and claim count, the savings weights' totals), the second finishes each part with the
whole job's sums. So a part's measures get the program-cost share and savings weight they get in a whole-job run, values equal to rounding (the sums are
added in a different order), and memory stays about that of one part: 1,000,000 measures on CET 25.1 peak at about 1.4 GB. Rows sharing a CEInputID are one
CET_ID to CET and are priced together, in a last part.

## C21 A true-up priced against its parent's profile

A NMEC true-up adjusts an earlier claim (its parent) to measured savings, and the measurement often changes the load shape: the parent was priced on an
estimated 8,760 and the true-up has a metered one. BRD BR-05.6 asks that the difference from the original profile be priced. Two optional ReCET Measure
columns do it (INPUT_FIELDS 1.3):

* `AC_Elec_Impact_Profile_Parent`: the parent's electric profile. Blank = the record has no parent and nothing below applies to it.
* `AC_Elec_Impact_Profile_Parent_2nd_Baseline`: the parent's profile for the 2nd-baseline years of an early replacement. Blank = `AC_Elec_Impact_Profile_Parent`.

A record that names a parent is priced twice: as filed (every output column, unchanged), and as the same record filed with the parent's profiles in
`AC_Elec_Impact_Profile` and `AC_Elec_Impact_Profile_2nd_Baseline`, the same ACC (its ACCVersion or the run's), the same claim quarter, life, NTG, rates,
quantities and First Year. A blank `AC_Elec_Impact_Profile_Water` follows the profile in force, so the water energy is priced on the parent's profile in
the second pricing too. When a profile is a kWh-per-unit profile (C4, BR-06.6) its own annual kWh is the quantity, in either pricing, exactly as a record
filed on it would be priced (`run_poc.apply_scaled_profiles`). For each column X of the list below

    XIncremental = X (as filed) - X (on the parent's profiles)                  (ElecBenIncremental, ElecBenIncremental_SB, ...)

* the priced streams that follow the electric profile, in each pricing: `ElecBen`, `ElecSupplyCost`, `WaterEnergyBen`, `WaterEnergyCost` (C5, C6) and
  their `_SB` / `_SH` forms;
* the cost-test set's aggregates and each test's benefits, which depend on the priced streams only: `TotalSystemBenefit`, `TRCBen`, `PACBen`, `RIMBen`,
  `SCBBen`, `SCHBen`, `PCTBen` on EE-2026 (`test_evaluator.benefit_totals`, the arithmetic C12 writes them with).

`ParentElecPricingStatus` is the second pricing's `ElecPricingStatus` (C16). The incremental columns are blank on a record with no parent; a job whose
Measure file has neither column writes none of them. They are reported values: no cost test, ratio, program-cost share or summary uses them, and the
record's own costs and ratios are those of the record as filed (the costs do not depend on the profile). With the SCT off the `_SB`, `_SH`, SCB and SCH
incremental columns are blank like their sources. Emissions, the combined 8,760 and the ACC components are the record's as filed.

`tests/test_true_up_parent_profile.py` holds every incremental column equal, bit for bit, to the same job priced twice (once as filed, once with the
parents' profiles in the profile columns) and subtracted, in one piece and in parts, and the incremental electric benefit to
`tests/reference_calc.incremental_elec_value` (the C5.1 loop on each shape). The difference uses the record's own quantities in both pricings, so it is
the effect of the shape alone; a quantity the true-up changes as well is in the record's own columns, and the platform sets the true-up beside its
parent's filed values. A parent profile that matches nothing is priced as no electric value (`no_parent_profile` warns, ParentElecPricingStatus is
UNPRICED), so the incremental columns then hold the record's whole value. A CET replica (C20) prices each record on its own profile only and notes it.
