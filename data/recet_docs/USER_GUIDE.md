# User guide

How to run `recet` on your own computer, what every control in the desktop window does, the command line, and the common jobs. The web site is
described in [WEB_GUIDE.md](WEB_GUIDE.md). The other documents:
[INPUT_FORMATS.md](INPUT_FORMATS.md) (supplied-file formats and matching rules), [INPUT_FIELDS.md](INPUT_FIELDS.md) (every input field),
[OUTPUT_FIELDS.md](OUTPUT_FIELDS.md) (every output field) and [CALCULATIONS.md](CALCULATIONS.md) (how each output is calculated).

* [What the tool does](#what-the-tool-does)
* [Install and start](#install-and-start)
* [The GUI, control by control](#the-gui-control-by-control)
* [What a run does, step by step](#what-a-run-does-step-by-step)
* [Common jobs](#common-jobs)
* [The command line](#the-command-line)
* [Reference data, moving to another machine](#reference-data-moving-to-another-machine)
* [Speed and the cache](#speed-and-the-cache)
* [Reading the results](#reading-the-results)
* [Troubleshooting](#troubleshooting)

---------------------------------------------------------------------------------------------------------------------------------

## What the tool does

It takes the two files you would upload to CET - `Measure.csv` and (optionally) `ProgramCost.csv` - prices every measure on real **hourly** avoided-cost data, and writes one row per measure with every input column and every output the CET reports
that the tool can calculate: benefits, supply costs, participant and bill-impact values, the TRC / PAC / RIM / SCB / SCH costs and ratios, program-cost weighting, and CO2 / NOx / PM10. Four avoided-cost versions ship with it:

| Version key | Avoided-cost data | CET "Version" used for discount rates, emissions and retail rates |
|---|---|---|
| `2021` | the 2021 ACC (TRC only: no societal tests) | 2022 |
| `2022` | the 2022 ACC (TRC only: no societal tests) | 2024 |
| `2024` | the 2024 ACC | 2026 |
| `2026` | the 2026 ACC (final, v1b) | 2027 |

You can replace 8,760-hour load profiles, gas monthly profiles and the ACC files themselves with your own; a supplied item that **matches** a built-in one replaces it, one that matches nothing is added. The avoided costs and load
profiles are ReCET's own (the ACC releases and CEDARS' profile library); from CET it uses only its run parameters (discount rates, base year, gas CO2), its NOx / PM10
and combustion-type factors and its retail rate schedules, picked by ACC version.

---------------------------------------------------------------------------------------------------------------------------------

## Install and start

```text
pip install recet-<version>-py3-none-any.whl          (or, from a checkout of the repository:  pip install .)
python -m recet                                     opens the window      (also: recet  or  recet-gui)
```

Python 3.10 or newer; `numpy`, `pandas`, `pyarrow`, `openpyxl`, `sv-ttk` and `tksheet` (the Edit inputs grid) install automatically; the window needs `tkinter` (included with the python.org installers; on some Linux systems `apt install python3-tk`).
The first time, set the **reference data folder** (below) so the large files can be found.

---------------------------------------------------------------------------------------------------------------------------------

## The GUI, control by control

The window has a header, seven tabs and a button bar that is always visible at the bottom. **Every setting is remembered** between sessions (in `~/.recet/config.json`).
A file box takes a typed or pasted path, or **Browse...**.

| Part | What it holds |
|---|---|
| header | The title (**ReCET**) and version, and a **Dark mode** / **Light mode** button that switches the colour scheme (remembered). The look is the Sun Valley theme (`sv-ttk`); without it the window uses the system theme. |
| **Job** tab | The job inputs, the output folder and run name, and the ACC version (sections 1 and 2 below). |
| **Edit inputs** tab | Edit a small job's `Measure` and `ProgramCost` tables cell by cell, with dropdowns and live checks, and run them (section 2b). |
| **Supplied inputs** tab | The separate inputs (section 3). |
| **Options** tab | The modes and the comparisons (section 4). |
| **Reference data** tab | The reference data folder. |
| **Log** tab | The running job's output. **Run** switches to it. |
| **Results** tab | The finished run by PA at five levels (measure, program, program sector, program segment, portfolio), with columns from a saved view. A successful run switches to it. |
| button bar | **Run**, **Stop**, **Open results folder**, the status and a progress bar. |

**Live file checks.** On the Job and Supplied inputs tabs every chosen file is checked as soon as you pick it (or leave its box): a green ✓ with a short description
(for example *PAs: PGE, SCE* or *304 rows, 35 programs*) or a red ✗ with the reason. Click the status for the full text. The run checks everything again.

### Menus

| Menu item | What it does |
|---|---|
| **Templates > Save all templates to a folder...** | Writes a template for each kind of supplied input (8,760 profiles, gas monthly profiles, electric ACC, gas ACC, a `Measure.csv` header) and `INPUT_FORMATS.md` into a folder you choose. |
| **Templates > Export a library 8,760 profile to edit...** | Asks for an end use (`AC_Elec_Impact_Profile`, exactly as in `Measure.csv`), an IOU (`PGE`, `SCE`, `SDGE`, `SCG`) and a target sector, then saves that CEDARS library profile in the supplied-profile format so you can edit the `impact` column and give it back as a replacement. Needs the reference data folder. |
| **Settings > Save settings...** | Saves every control to a JSON file (default name `recet_job.json`). `recet run --config that.json` reproduces the run without the window. |
| **Settings > Load settings...** | Fills every control from such a file. |
| **Help > Documentation** | A submenu that opens this guide, the input-format guide, the input and output field references and the calculations document in a reading window (plain text), plus *Open the documentation folder*. |
| **Help > About** | Shows the version and where the reference data folder came from. |

### 1. Job tab: job inputs - a job workbook, or the CET files

| Control | What it does |
|---|---|
| **Input type** | **Excel job workbook (.xlsx)** or **CET files (Measure.csv, ProgramCost.csv)**. Shows only the boxes for that type, and only that type is run; the other type's boxes keep their paths (remembered between sessions), so you can switch back. Loading a settings file picks the type it names. |
| **Job workbook (.xlsx)** | One Excel file holding the job: a `Measure` sheet (required) and optional `ProgramCost`, `RunParameters`, `SWAllocation`, `Programs`, `GasProfiles` and `Profiles8760` sheets ([INPUT_FORMATS.md](INPUT_FORMATS.md#8-the-job-as-one-excel-workbook)). Shown with input type *Excel job workbook*. A file chosen on the Supplied inputs tab wins over its sheet. **Template...** saves a workbook with the run parameters and SW factors already filled in. |
| hint under the workbook box | "Sheets: Measure, ProgramCost, RunParameters, SWAllocation, Programs, GasProfiles, Profiles8760. A file chosen on the Supplied inputs tab wins over its sheet." - a reminder of the sheet names and the precedence rule. |
| **Measure.csv** | Shown with input type *CET files*. The CET measure file (pipe- or comma-delimited); required for that input type. The file is checked (the *preflight*) before anything is priced; see [INPUT_FIELDS.md](INPUT_FIELDS.md#1-measurecsv). |
| **ProgramCost.csv (optional)** | The CET program-cost file. With it, program costs are shared over each program's measures (C11) and the *NoAdmin* and regular cost tests differ; without it `WeightedProgramCost` is 0 and `NoAdmin` = regular. A measure whose `PrgID` has no `ProgramCost.csv` row is priced with no program costs, and a `ProgramCost.csv` row whose `PrgID` has no measures is charged to none; preflight warns about both (CET stops the job instead; C17 item 11). |
| **Output folder** | Where results go. Created if missing. Each run makes its own sub-folder (`<run name>_acc<version>`) inside it, so runs never overwrite each other. Default `~/recet_results`. |
| **Run name (optional)** | The sub-folder's name. Blank = a timestamp (`run_20260921_143005`). Re-using a name re-uses the folder and overwrites the files with the same names. |

### 2. Job tab: ACC version

| Control | What it does |
|---|---|
| **Run against** | Which avoided-cost version to price on. Choose **2021**, **2022**, **2024** or **2026** (the ACC of that year; the 2021 and 2022 ACCs are TRC only, so their SCT columns stay blank) from the list, or type a **new label** (one word, no spaces, e.g. `2030-draft`) and give its files in section 3. A built-in version's electric ACC comes from the reference data folder; its gas ACC ships in the package. Everything derived from the version - discount rates, emission factors, retail rates - follows it (C3.3). |
| **First Year (blank = earliest claim year)** | CET's *First Year*: the reference point of all discounting (C2.5). Blank uses the earliest `ClaimYearQuarter` year in the job. Set it to the value on the CET run you are matching - it is on the run form, not in the input files. A whole number such as `2028`. |
| **Priced for** | What the run is for: *Any filing* (the default), *Quarterly Claims*, or one budget filing. ReCET then flags every record priced on an ACC that does not normally apply to its program year (claims use the ACC of the year's final budget filing; a budget filing uses its own ACC for its program years): the `acc_not_for_program_year` finding in preflight, and counts by ACC and program year in `run_settings.json` (OUTPUT_FIELDS 2.9b). Nothing is stopped. |
| **Cost tests** | The cost-test definition set the tests are priced on ([COST_TESTS.md](COST_TESTS.md)): *In force for the first program year* (the default), a built-in set such as `EE-2026`, or the path of a set's YAML file typed in the box for an analysis run. A run with CEDARS filing checks or priced for a filing takes a published built-in set only. The set is named in `Used_CostTestSet` and `run_settings.json`. |

### 2b. Edit inputs tab: edit a small job cell by cell

For small jobs - an implementer's project of a few measures, a what-if on one program - where editing cells is quicker than going back to Excel. It holds up to
**500 measures**; a larger job is refused with a message: edit those offline and run them from the Job tab. Needs the `tksheet` package (installed with the tool; if it is
missing the tab says how to install it).

| Control | What it does |
|---|---|
| **Load from Job tab** | Reads the job the Job tab points at into the grid: the job workbook's `Measure` and `ProgramCost` sheets, or the two CET files (a file chosen separately wins over its sheet, as in a run). The workbook's other sheets (run parameters, programs ...) are not shown but still apply to a run. |
| **New job** | Starts from the workbook template's example measure and program-cost row. |
| **Save as workbook...** | Saves a job workbook. When the tables came from a workbook, its other sheets are kept. Then offers to put the saved workbook on the Job tab. |
| **Save as CET files...** | Saves `Measure.csv` and (if it has rows) `ProgramCost.csv`, pipe-delimited, ready for the CET or the Job tab. The files use CET's own field names (`E3ClimateZone`, `E3MeaElecEndUseShape`, `E3TargetSector`, `E3GasSavProfile`, `E3GasSector`) where the grid shows ReCET's (`AC_...`); ReCET reads either. |
| **Measures** / **Program costs** | Switches the grid between the two tables (the counts are in brackets). |
| **Add row** | Adds a row below the selected one. A new measure gets a unique `CEInputID` (`NEW_1` ...), the selected row's program, claim quarter, IOU, zone and sectors, 1 for units and the realization and installation rates, 0 for the other unit values, and blanks for `NTGR*` and `EUL_Yrs`, which you must fill. |
| **Duplicate selected** | Copies the selected rows (a selected cell selects its row) below them, giving copied measures new IDs (`EXAMPLE_2` ...). The usual way to build a variant of a measure. |
| **Delete selected** | Deletes the selected rows, after asking. |
| **Full check (preflight)** | Runs the real preflight on the edited tables (the same as Run with **Preflight check only**): profile coverage, ACC years and the rest of the checks a run makes. |
| **Run with the edited tables** | Ticked when you load or start a job. **Run** then uses the grid instead of the Job tab's measure and program-cost inputs (below). Untick it to run the Job tab's files unchanged. |
| grid | Every CET column, arranged for editing: **`CEInputID`, `MeasCode` and `MeasDescription` stay pinned on the left** (`PrgID` for program costs) while the rest scroll. Right after them come the columns that change a result - program and claim quarter, the price and profile keys (IOU, zone, sectors, end use, gas profile, fuel-substitution flags, combustion type), quantities, life, net-to-gross and rates, costs, other benefits and costs, and last the kW fields, which feed only the reported kW savings columns (`FirstYearNetKW` ...) - and then the ones that change nothing (carried to the output only: `Sector`, `DeliveryType`, `BldgType`, the descriptive and ID fields, the flags, `RateScheduleElec`/`RateScheduleGas`, `Comments`; for program costs `PrgYear` and `OnBillFinancing`). Five optional ReCET-only columns (not CET fields) are offered too and sit with the price and profile keys: `ACCVersion` (a dropdown of the ACC versions; blank = the run's), `AC_Elec_Impact_Profile_2nd_Baseline` and `AC_Elec_Impact_Profile_Water` (blank = `AC_Elec_Impact_Profile`), and a true-up's parent profiles `AC_Elec_Impact_Profile_Parent` and `AC_Elec_Impact_Profile_Parent_2nd_Baseline` (blank = no parent; the record's incremental columns are priced against them, CALCULATIONS C21). Any other extra columns of the input come last. **Header colours** (legend above the grid: *changes results* / *no effect on results*): **green** = the column changes results, **blue** = it changes none (including the pinned ID, code and description; the pinned `PrgID` of program costs is green). **Hover a column header** for what it affects: whether it changes results, which outputs (for example *electric benefits and supply costs, electric emissions, total system benefit and every cost-test cost and ratio*, or *gas NOx emissions only*) and how it is used. Clicking a cell also says which, in the line under the grid. **Numbers are shown without trailing zeros and with at most 6 decimals** (`0.600000` shows as `0.6`, `4.969198677` as `4.969199`) and right-aligned. That is display only: the cell keeps exactly what was typed or loaded, which is what you see when you edit the cell and what is checked, priced and saved; when a value is shown rounded, the line under the grid gives the full value. Saved files keep CET's own column order, with the ReCET-only columns after CET's and only when at least one row uses them (so a job that does not use them stays a CET-ready file). Rows are added and removed only with the buttons above (that keeps the pinned and scrolling parts in step). Edit a cell by typing or double-clicking; paste blocks from Excel (Ctrl+V); Ctrl+Z undoes cell edits (adding, duplicating or deleting rows clears the undo history). Columns with a CET value list (`Sector`, `DeliveryType`, `AC_Climate_Zone`, `MeasImpactType`, `FuelID`, ...) have a dropdown of the codes valid for the job's claim years; `AC_Elec_Impact_Profile`, `AC_Elec_Impact_Profile_2nd_Baseline` and `AC_Elec_Impact_Profile_Water` list only the end uses that have an 8,760 profile for that row's IOU and target sector (library and supplied profiles); `AC_Gas_Impact_Profile` lists the gas profiles; `IOU_AC_Territory` the four IOUs. You can still type any value: a value off the list is flagged, not refused. The row labels on the left repeat each row's `CEInputID` (or `PrgID` and quarter) so you can tell rows apart while scrolled right. |
| cell colours | Each edit re-checks the tables. **Red** = an error that would stop the run (blank or duplicate ID, bad `ClaimYearQuarter` or IOU, text in a number column, a blank numeric field CET requires (`NumUnits`, the savings and measure costs of both baselines, the `NTGR*`, `EUL_Yrs`, `RUL_Yrs`, the installation and realization rates, the other benefits and costs except `UnitTaxCredits`), a negative `EUL_Yrs` / `RUL_Yrs` or an `RUL_Yrs` above `EUL_Yrs`, a bad gas sector on a gas measure, a `PrgID` with no partner row, a pipe character in a cell). **Amber** = the run goes ahead but CET would reject the value (outside CET's range, not an active CET code) or the savings will not be priced (no profile). **Blue** = for information. Hover a coloured cell for the reason. |
| line under the grid | The selected column's meaning (from [INPUT_FIELDS.md](INPUT_FIELDS.md)) and the selected cell's problems. |
| problem list | Every flagged cell: severity, table, row, column and reason. Double-click (or Enter) goes to the cell. The count of errors and warnings is shown above the grid. |

**How a run uses the edited tables.** With **Run with the edited tables** ticked, **Run** first refuses to start if any cell is red. Otherwise it writes the tables to
`<output folder>/_edited_inputs/<date_time>/` (`Measure.csv`, `ProgramCost.csv`, and the source workbook's other sheets as files) and runs on them, so every run keeps
the exact inputs it used; `run_settings.json` points at them. Files chosen on the Supplied inputs tab still win over the workbook's sheets. With no program-cost rows the run
has no program costs. Edits are not saved anywhere else until you use a Save button; closing the window with unsaved edits asks first.

### 3. Supplied inputs tab: separate inputs (optional) - a match REPLACES the built-in data

Each box has a **Template...** button that saves a correctly laid out file to fill in. Formats and matching rules are in [INPUT_FORMATS.md](INPUT_FORMATS.md); every column is defined in [INPUT_FIELDS.md](INPUT_FIELDS.md#4-supplied-input-files).
Supplied files are validated before the run; a problem stops the run with the file, row and reason.

| Control | What it does |
|---|---|
| **8,760 electric profiles** | A CSV/Parquet of hourly load shapes, one row per (profile, climate zone, hour). A profile that matches a measure on (end use, IOU, sector) is used **instead of** the CEDARS library profile (which includes the TOU end uses as 8,760s); a profile that matches nothing is added. Each measure's `ProfileSource` output column says which was used. Shapes that do not sum to 1 are rescaled (with a note in the log). |
| **Gas monthly profiles** | A CSV/Parquet of 12-month shapes (`Profile`, `Month`, `Impact`). A `Profile` name in the library replaces it; a new name is added. |
| **Electric ACC file** | A file of hourly electric avoided costs. If the label in **Run against** is one the package ships (`2021`, `2022`, `2024`, `2026`), the file replaces that version's electric ACC for this run; if it is a new label, the file *defines* the new version. |
| **Run parameters** | Discount rates (regular and societal), base year and gas CO2 per PA. A listed PA's values replace the built-in ones (the RunParameters table) for this run; other PAs keep theirs. **Template...** saves the built-in values for the selected ACC, to edit. |
| **SW allocation factors** | Each IOU's share of each statewide program, by year. A (`Year`, `ProgramID`) row replaces the built-in one and a new row is added. Used only when **Allocate statewide programs** is ticked. **Template...** saves the whole built-in table. |
| **Gas ACC file** | The same for gas monthly avoided costs. |
| **Program metadata** | The CEDARS Programs list layout (PA, PrgID, name, status, segment, sectors, implementer, years, flags). A `PrgID` replaces the built-in row (CEDARS' list of 2026-10-03) and a new one is added. Each measure gets its program's row as `Prg_` columns, the summary gains one row per program segment, and the preflight reports unknown and closed programs. No priced value changes. **Template...** saves the whole built-in table. |
| **Run parameters of ACC version (for a NEW ACC label)** | Run parameters, emission factors and retail rates are picked by ACC version, and a brand-new ACC has none of its own. Type the built-in ACC version to borrow (for example `2026`). Blank = the newest built-in. Ignored for the built-in versions, which use their own. |

If you define a new version but supply only one fuel, the other fuel uses the newest built-in ACC, and the log says so.

### Reference data tab: Reference data folder (the large ACC and profile files)

| Control | What it does |
|---|---|
| **Folder** | Where the large files live (`acc/2024/electric_hourly.parquet`, `acc/2026/electric_hourly.parquet`, optionally `acc/2021/...` and `acc/2022/...` for the TRC-only ACCs, `impact_profiles/catalog.parquet`, `impact_profiles/hourly.parquet`). Applies to this run. |
| **Save as default** | Remembers the folder for every future run on this machine (writes `~/.recet/config.json`). |
| status line | Green: every required file was found (and from where). Red: lists what is missing. It refreshes when you browse, save, load settings or save a template. A run that uses the built-in ACC or profile library needs these files; a run entirely on supplied files does not. |

### 4. Options tab: modes, comparisons and output

The left box (**Modes**) holds the pricing switches; the right box (**Comparisons and output**) holds the comparisons, preflight-only and the output format.

| Control | What it does | Details |
|---|---|---|
| **Calculate ACC components separately** | Prices each component of the avoided cost (energy, capacity, T&D, GHG, ... and gas market, T&D, CO2, ...) on its own through the same route as the total, and adds an `ElecValue_<component>` / `GasValue_<component>` column for each. They always add up to `ElecBen - ElecSupplyCost` / `GasBen - GasSupplyCost`; the log reports the check and `components_summary_*` totals them over the portfolio. Costs about one extra pricing pass per component (10 or 11 electric depending on the ACC vintage, 7 gas). | C16.1 |
| **Produce combined 8760 electric load profile** | Writes `portfolio_8760_*`: the portfolio's net kWh in each of the 8,760 hours, in total, by IOU and by climate zone, and `portfolio_8760_peaks_*` (each one's peak savings hour, largest added load and share of the portfolio's peak hour; OUTPUT_FIELDS 2.4b). Uses each measure's own 8,760 aligned to the ACC's weekdays (the `datetime` column is on the ACC's calendar, 2018 or 2020); measures with no shape are listed in `portfolio_8760_unshaped_*`. Positive = savings, negative = added load; market effects are not included. | C16.2 |
| &nbsp;&nbsp;**Year(s)** | The calendar year(s) of the profile, separated by spaces or commas (`2028 2029 2030`). Blank = the First Year. One `net_kwh_<year>` column per year. Each year counts only the measures still in service that year, weighted by the fraction of the year (a Q3 claim delivers half of its first year). | |
| **Apply 5% market effects (benefits and costs)** | Turns on CET's *Market Effects* for benefits and costs together (CET's `MEBens` and `MECost`). Benefits: the net-to-gross of electric, gas, water-energy, other benefits and tax credits becomes `NTG + 0.05` (and the emissions "Net" columns); the Gross columns divide by `NTG + 0.05` as well. Costs: `OtherCost` uses `NTGRkWh + 0.05` and the participant cost gains `0.05 x (GrossMeasCostPV + ExcessIncentivesPV)` (inside `NetParticipantCostAdjustedPV`; `NetParticipantCostPV` does not change). The electric, gas and water supply costs follow the benefit side, as in CET. Off = 0% on both. Validated against CET runs made with 0% and 5% (C13). | C7, C8, C13 |
| **Include societal cost tests (SCB / SCH)** | On by default. Prices the societal Base and High tests: two more complete pricing passes on the `SCT` rows at the societal discount rate. Turn it off for a run about three times faster; every `_SB` / `_SH` / `SCB...` / `SCH...` column is then blank. | C5.6, C12 |
| **Include emissions (CO2 / NOx / PM10)** | On by default. Adds the twenty emission columns (net and gross, first-year and lifecycle). | C14 |
| **Allocate statewide (SW_) programs across the IOUs** | Splits each statewide program's measures and program costs across PG&E, SDG&E, SCE and SCG with CET's allocation factors *before* pricing, so each IOU's share is its own row (`<IOU>_` prepended to `CEInputID` and `PrgID`). Off (the default) prices statewide rows as filed - the way the real CET runs were done. Needs `Measure.csv` and `ProgramCost.csv` to be allocated together, which the tool does. The run writes `statewide_reconciliation` (each allocated field before allocation and by IOU, and whether it adds back up; OUTPUT_FIELDS 2.13). | C15 |
| **Leave Exclude_From_CE programs out of the portfolio summary** | Off by default. Measures whose program the program metadata flags `Exclude_From_CE` are left out of every summary row and totalled in one last `excluded` row. They are still priced and listed in the measure-level table. | |
| **Also run another ACC version and compare measure by measure** | Runs the job twice - the selected version and the one in **Compare with** - each in its own process, then writes `comparison_acc<A>_vs_acc<B>` (per-measure value, change and percent change of the key outputs) and a portfolio summary. Supplied ACC files apply to the first version only; supplied 8,760 and gas profiles apply to both, so the ACC is the only thing that differs. The second version must differ from the first. | C16.4 |
| &nbsp;&nbsp;**Compare with** | The second built-in version (`2021`, `2022`, `2024` or `2026`). Defaults to `2026` when the run is on `2024`, else `2024`. | |
| **Compare with a CET outputs file** | Matches your measures to a CET run's `*_outputs.csv` by ID and writes `cet_comparison_measures_*` (per measure: POC, CET, difference, ratio) and `cet_comparison_summary_*` (portfolio totals and POC/CET). Only meaningful when the job is run the way the CET run was: same First Year, ACC version and statewide allocation. Enter the file's path in the box below the checkbox. | C16.4 |
| **Preflight check only (do not price)** | Reads and checks the inputs and stops: writes `preflight.txt` and `preflight_findings.xlsx` (the findings and every flagged cell, to filter in Excel) and reports what would and would not be priced (measures with no profile, unknown gas profile names, blank required values, values outside CET's ranges, program-cost mismatches, claim years outside the ACC's range). Takes seconds; use it first on a new file. | |
| **CEDARS filing checks** | *none* (the default), *Quarterly Claims* or *Budget filing*: also applies CEDARS' own upload checks for that filing (its validation rules, warning rules and field constraints, as CEDARS publishes them) and flags the records it would reject or warn on: the `filing_validation` table (each failing record, with `FilingValidation` and `FilingRules`) and the findings in `preflight.txt` under each rule's ID. Nothing is stopped: every record is still priced. With **Preflight check only** it is a quick check of a file before uploading it to CEDARS. | |
| &nbsp;&nbsp;**Filing year** | The filing's year for the rules that name it (a budget filing's `CEInputID` must start with `<PA>-<year>`, and its `PrgYear` must equal it). Blank: each record's own claim year. | |
| **Output format** | `csv` (UTF-8 with a byte-order mark so Excel opens it directly), `xlsx` (one formatted Excel file per table: bold frozen header with filters, sized columns, numbers stored as numbers; a table too long for Excel falls back to `csv`) or `parquet` (smaller and faster for large jobs). Applies to every table written. | |
| **Output template** | Which columns the measure-level file holds, in what order (BRD BR-06.10): *All columns* (the default), a built-in template (*Key results*, *Participant costs*) or one you saved. **Edit templates…** opens the template editor: choose a **Template** (or type a new name; a built-in is copied under another name). The left list shows the fields not yet in the template: **Show** picks all fields, input fields, program fields, output fields or one output group, and **Search** matches names and definitions. **Add →** (or double-click, or Enter) moves the selected fields to the right, **Add shown »** moves every field listed, **← Remove** (or double-click, or Delete) takes columns out, and **Top**, **Up**, **Down**, **Bottom** (or Alt+Up / Alt+Down) reorder them; the definition of the last field clicked shows below the lists. **Save template** saves it (tick **Use for new runs** to make it the default); **Default for new runs** picks the template the window starts with (until one is set, the window keeps the last run's template). Only the measure-level file is shaped: the Results tables, the summary and the `key_metrics` file use every column. Each run writes `field_definitions` (each column of the file with its definition and documentation link) and `key_metrics` (the headline results and their main drivers by program, sector and end use); see OUTPUT_FIELDS 2.11 and 2.12. | |

The window cannot set two things the command line and settings file can: `normalize_profiles` (rescale supplied profiles that do not sum to 1; the other choice is to stop with an error) and `subset_step` (a test hook).

### Results tab

The finished run's results, filtered to one PA and shown at five levels, with the columns of a view you choose. A successful run switches here; **Load results
from a folder...** shows any earlier run folder.

| Control | What it does |
|---|---|
| **Load results from a folder...** | Shows any earlier run folder (one with a `measure_level_*` file). |
| **Open measure-level table** | Opens the shown run's `measure_level_*` file in the program your computer uses for it (Excel for CSV). |
| **Export table...** | Saves the table on the tab you are looking at, as Excel or CSV, with the view's columns and unformatted numbers. |
| **PA** | **All PAs** or one program administrator. A measure's PA is its program's administrator: the `PA` of its row in `ProgramCost.csv` (`CreditedPA`), else the program's PA in the Programs list, else the IOU territory it is priced in. So a REN or CCA program shows under the REN or CCA, and an unallocated statewide program under `SW` (with **Allocate statewide programs** on, each IOU's share under that IOU). |
| **Program years** | The menu lists the run's program years (claim years); tick the ones to total over and every table shows only their measures, so a multi-year filing's ratios and TSB can be read for any set of years (BRD BR-04.1). **All years** clears the ticks; none ticked (or all) is every year. Off for a run of one year. |
| **View** | Which columns the tables show. **Default** is built in: Budget / expenditures, Total system benefit, TRC, PAC, RIM, SCT Base, SCT High, first-year net kWh, kW and therms, and CO2 avoided over the measures' lives. Views you save appear here and are remembered. |
| **Edit views...** | Opens the view editor: **Available columns** (with a filter box) on the left, **Shown columns (in order)** on the right. **Add** / **Remove** (or double-click) move columns across, **Move up** / **Move down** order them, **Reset to default columns** starts again, **Save** keeps the view, **Save as new view...** names a new one, **Delete view** removes a saved view. **Close** shuts the editor (unsaved changes are dropped). The Default view cannot be changed; save your changes under a new name. The columns on offer are the run's additive outputs (benefits, costs, savings, emissions, bill impacts, ACC components ...), the ratios and two CO2 totals; inputs, derived pricing inputs (`Used_Year`, `AC_Net_kWh_1st_Baseline` ...), `Prg_` fields and per-measure ratios are not offered because they do not add up. |
| **Measure level** tab | One row per measure: `CEInputID`, description, `PrgID`, PA, program year and the view's columns. Turned off (its label says so) when the chosen PA has more than 5,000 measures, and for every PA when the whole run has more than 50,000; use **Open measure-level table** for those. |
| **Program level** tab | One row per `PrgID`, with the program's name, PA and number of measures. |
| **Program year level** tab | One row per program year (the claim year, `Used_Year`), with its number of measures. |
| **Program sector level** tab | One row per program sector: the program's `PrimarySector` in the Programs list (Residential, Commercial, Public, Industrial, Agricultural, Cross-Cutting, Portfolio Support). `(blank)` for programs with no sector, `(not in the program metadata)` if the run has no program metadata. |
| **Program segment level** tab | One row per program segment (`ProgramSegment` in the Programs list), `(blank)` for none. |
| **Portfolio level** tab | One row: the whole run (or the chosen PA). |
| tables | Every level above the measure adds up its measures; the sums are kept in the run folder's `_results` folder (`group_sums2_<file>.parquet`, written at the end of the run), so a 1,000,000-measure run opens in under a second without reading its measure-level file. Ratios are not averaged: they are recomputed from the summed benefits and costs, the same way as the portfolio summary file and CET's run receipt (TRC = (benefits + tax credits) / TRC cost; PAC = benefits / PAC cost; RIM = (benefits + bill increases) / RIM cost; SCB / SCH = (societal benefits + other benefits) / societal cost). Hover a column header for what it holds. Numbers are shown rounded (dollars and energy to whole numbers, ratios to two decimals); copy or export them for full precision. |

A two-version comparison folder has no measure-level table of its own: its comparison summary is shown on the Portfolio tab as it is, and the PA filter and views are
off until you load one version's run folder.

### Buttons, status and log

| Control | What it does |
|---|---|
| **Run** | Checks that a job workbook or a measure file is chosen and exists (or, with **Run with the edited tables** ticked on the Edit inputs tab, that the edited tables have no errors, and writes them out - section 2b), remembers all settings, writes them to `_gui_job.json` in the output folder and starts the job in a **separate process** (so the window stays responsive), switching to the Log tab. Disabled while a run is going. |
| **Stop** | Ends the running job. Nothing already written is deleted. |
| **Open results folder** | Opens the finished run's folder (or the output folder) in the file manager (enabled when a run ends). |
| progress bar, status | The bar moves while running. The status reads *Ready*, *Running...*, *Finished*, or *Stopped/failed (exit N)*. |
| log | The job's output live: which supplied files were checked and used, the preflight report, timings, component and CET comparison summaries, and any error. Closing the window while a run is going asks first. |

---------------------------------------------------------------------------------------------------------------------------------

## What a run does, step by step

1. **Prepare.** Set the reference data folder; check any supplied ACC files (columns, 8,760 rows per block, consecutive years, components sum to the total); resolve the ACC version and its run parameters and CET tables; load supplied profiles (checked, rescaled if needed);
   point the pricing at these files. No CET avoided-cost table is read.
2. **Preflight.** Read `Measure.csv` (and `ProgramCost.csv`), report format problems and what will be priced, write `preflight.txt` and `preflight_findings.xlsx`. A blocking problem stops the run here; *Preflight only* stops here either way.
3. **Load.** Derive the pricing fields (C1); with statewide allocation, split the statewide programs first.
4. **Price.** Electric (every measure on its 8,760 profile, aligned once to the ACC's weekdays), gas and water energy for the TRC rows, then for `SCT Base` and `SCT High` when the ACC has them; participant costs; bill impacts; other benefits and costs; program costs; the cost tests and ratios (C5-C12). Then emissions.
5. **Components** (if on), then **assemble and write** the measure-level table, the portfolio summary and any optional files; **combined 8760 profile** and **CET comparison** if requested.
6. **Record.** `run_settings.json` lists the exact settings, files and timings.

**Large jobs run in parts.** A `Measure.csv` with more than 50,000 rows (the `chunk_size` setting, `--chunk-size`) is split into parts of that many rows in the
run folder's `_parts` folder (deleted at the end), and steps 2-5 work on one part at a time, so the run's memory is about that of one part whatever the job's size.
The preflight checks each part and checks duplicate CEInputIDs and the ProgramCost alignment over the whole file. Pricing makes two passes: the first prices every
part and adds up each program's totals (the benefits its costs are shared by); the second shares each program's costs over all its measures with those totals,
finishes the cost tests and appends each part's rows to the output files. The files are the same as a whole-job run writes and the values equal to rounding
(sums are added in a different order). The run log shows one line per part.

**The record cap.** A job with more than 2,000,000 records (the `max_records` setting, `--max-records`; BRD BR-06.3) is refused at the start of preflight
with the error `too_many_records`, before the file is split: split it into files of at most that many records, or raise the cap. 0 = no cap.

---------------------------------------------------------------------------------------------------------------------------------

## Common jobs

**Price a job on one ACC.** Choose the files, pick the ACC (`2026`, `2024`, or the TRC-only `2022` / `2021`), leave the modes at their defaults, Run. Open `measure_level_acc<version>.csv`; check `ElecPricingStatus`, `GasPricingStatus` and `WaterPricingStatus` for `UNPRICED` rows first.

**Compare the 2024 and 2026 ACC.** Tick *Also run another ACC version...*, pick the other version. `comparison_acc2024_vs_acc2026.csv` holds each measure's value under both, the change and percent change.

**Replace an 8,760 profile.** *Templates > Export a library 8,760 profile to edit...*, edit the `impact` column (each zone must still sum to 1), choose the file under *8,760 electric profiles*. Measures matching that (end use, IOU, sector) now report `ProfileSource = user profile`.

**Try a new ACC.** Type a new label under *Run against*, supply the electric and/or gas ACC file (use the **Template...** buttons for the layout), and name the built-in ACC version whose run parameters and CET tables it borrows (default: the newest). Run a Preflight first: it reports the ACC's year range and how many measures run past it.

**See what drives a value.** Tick *Calculate ACC components separately*; each `ElecValue_*` / `GasValue_*` column is the dollars from one component.

**Build the portfolio load shape.** Tick *Produce combined 8760...*, list the years, open `portfolio_8760_acc<version>.csv`.

**Check against a CET run.** Enter the CET `*_outputs.csv`, set First Year and statewide allocation to match the CET run, tick *Compare with a CET outputs file*.

**Get a quick answer.** Turn off *Include societal cost tests* and *Include emissions*.

---------------------------------------------------------------------------------------------------------------------------------

## The command line

```text
recet                                       open the window (same as  recet gui)
recet run --measures M.csv [--programcost P.csv] --acc 2026 --out results [options]
recet run --config job.json                 a saved settings file (command-line options override it)
recet preflight --measures M.csv --acc 2026 check inputs, price nothing
recet template all --out templates          every template + INPUT_FORMATS.md
recet template export-profile --end-use "28 = Res. New Const. Lighting" --pa PGE --sector Residential --out shape.csv
recet check acc-electric FILE               also: acc-gas, profiles-8760, profiles-gas   (validate a supplied file)
recet data status | set-dir PATH | build DEST | verify DIR [--full] | pack DIR OUT | unpack PARTS DEST | clear-cache
```

`recet run --help` lists every option; the flag for each GUI control is in the table in [INPUT_FIELDS.md](INPUT_FIELDS.md#3-run-settings). From Python:

```python
from recet.jobs import RunConfig, run_job
result = run_job(RunConfig(measures="Measure.csv", programcost="ProgramCost.csv", out_dir="results", acc_version="2026", components=True))
result.frame        # the measure-level table (a pandas DataFrame); None for a job run in parts (more rows than chunk_size): read result.files["measure_level"]
result.files        # every file written
```

---------------------------------------------------------------------------------------------------------------------------------

## Reference data, moving to another machine

The electric ACC files (~1 GB each for the 2024 and 2026 ACCs; the 2021 and 2022 ones are smaller and optional) and the CEDARS 8,760-hour profile library (~9 GB) are too large for GitHub, so they are kept in a *reference data folder* outside the package. The folder can be anywhere and can be changed at any time:
the window's box, `recet data set-dir PATH`, the environment variable `RECET_DATA`, or `--data-dir` on a command (first match wins in that order of specificity).
`recet data status` shows what is present. To move it: `data pack` writes the folder as parts of at most 4 GB with checksums; `data unpack` rebuilds it; `data verify --full` checks sizes and SHA-256 against `manifest.json`.

---------------------------------------------------------------------------------------------------------------------------------

## Speed and the cache

A warm job of about 5,500 measures prices in roughly 5 seconds per claim year (about 30 s before the 0.1.3 speed-ups), and a 73,000-measure filing in under a minute. Two things make that possible. The pricing works out the hourly (electric) and
monthly (gas) value of each *distinct* profile, climate zone and year once and reuses it for every measure that shares it (that 73,000-measure filing has only 48 distinct combinations). And the tool keeps a **cache** of data it derives from the large files, so the next run does not rebuild it: the
ACC as memory-mapped arrays (about 0.5 GB per ACC file, plus about 0.14 GB for each column also kept divided by 1,000 into $/kWh: the total and T&D in a standard run,
every component in components mode; the pricing reads its hourly series from those files in place, so they take no private memory) and each impact profile a job has used (about 1 MB each). The first run with a given ACC or profile library builds its cache (a few extra seconds); later runs, including runs
started from the window, start in about a second. Nothing in the cache is ever the only copy of anything.

* It lives in `~/.recet/cache` (set `RECET_CACHE` to put it elsewhere). `recet data status` shows its size; `recet data clear-cache` deletes it (it is rebuilt on demand).
* An entry is keyed by the source file's path, size and modification time, so an edited or replaced ACC or profile library is rebuilt, never served stale. Old entries are not removed automatically.
* Set `RECET_NO_CACHE=1` to run without it (results are identical). If the folder cannot be written the tool falls back to memory by itself.

## Reading the results

* Start with `preflight_findings.xlsx` (or `preflight.txt`) and the `...PricingStatus` columns: a benefit of 0 can mean "no savings" or "could not price".
* `measure_level_acc<version>.csv` is the main table; [OUTPUT_FIELDS.md](OUTPUT_FIELDS.md) gives each column's complete formula in terms of the input fields, with a worked number, and [CALCULATIONS.md](CALCULATIONS.md) explains the method. Two worked examples with every intermediate are in section 3 of the output-field reference.
* `portfolio_summary_acc<version>.csv` has the portfolio, per-IOU-territory, per-credited-PA and per-program-segment totals and ratios on CET's receipt definition.
* Which results are validated against CET, where the tool deliberately differs, and what it cannot do are listed in [CALCULATIONS.md](CALCULATIONS.md#c17-validation-deliberate-differences-from-cet-and-limitations).

---------------------------------------------------------------------------------------------------------------------------------

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| "Measure.csv lacks required column(s) ..." | The file is not in CET's layout or the header is wrong; compare with `recet template measure --out header.csv`. |
| "The header has no '\|', ',' or tab" (`no_delimiter`) | The file has one column or is not a CET-format file; save it as pipe- or comma-delimited text with CET's header. |
| "blank_required_value" | A numeric field CET requires (`NumUnits`, the kWh / kW / therm savings of both baselines, the measure costs, the four NTGs, `EUL_Yrs`, `RUL_Yrs`, the installation and realization rates, the gas-infrastructure, refrigerant and miscellaneous benefits and costs) is blank on the listed measures; enter a value (0 if none). |
| "negative_life" | `EUL_Yrs` or `RUL_Yrs` is negative on the listed measures. |
| "rul_above_eul" | `RUL_Yrs` is greater than `EUL_Yrs` on the listed measures: the existing equipment's remaining life cannot exceed the measure's life. |
| Many `UNPRICED` electric measures | `AC_Elec_Impact_Profile` (or the sector / IOU) does not match a profile; the preflight lists examples. Fix the spelling, or supply a profile for that end use. |
| "electric ACC file for version ... not found" | The reference data folder is not set or lacks `acc/<version>/electric_hourly.parquet`; see the status line and `recet data status`. |
| A supplied ACC "is not usable" | The message names the problem (a missing column, a block without 8,760 rows, gaps in the years). `recet check acc-electric FILE` re-checks a file on its own. |
| Societal columns are blank | *Include societal cost tests* was off. |
| Results differ from a CET run | Compare like with like (same First Year, ACC version, statewide allocation, market effects); then see the validation table in CALCULATIONS.md C17 for what is expected to differ. |
