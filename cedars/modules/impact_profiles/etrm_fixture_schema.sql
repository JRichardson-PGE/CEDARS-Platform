-- The layout DatabaseSource reads until we have the restricted eTRM database's real schema (no access yet). When the credentials
-- arrive, change PACKAGES_SQL and PERMUTATIONS_SQL in etrm.py to the real tables; the rest of the module does not change.
-- Field names follow the Load Shapes user guide's permutation fields; UECs are kWh per unit at full precision.
CREATE SCHEMA IF NOT EXISTS {schema};

CREATE TABLE IF NOT EXISTS {schema}.measure_versions (
    measure_version_id text PRIMARY KEY,      -- e.g. SWHC045-03-draft
    statewide_id text NOT NULL,               -- e.g. SWHC045
    name text NOT NULL,
    pa_lead text NOT NULL,                    -- the lead PA's code, e.g. PGE
    start_date date,
    is_draft boolean NOT NULL DEFAULT true
);

CREATE TABLE IF NOT EXISTS {schema}.permutations (
    id bigserial PRIMARY KEY,
    measure_version_id text NOT NULL REFERENCES {schema}.measure_versions,
    sector text, bldg_type text, bldg_vint text, bldg_hvac text, bldg_loc text,
    tech_group text, tech_type text,          -- the measure case's Technology Group and Type
    measure_tech_id text, standard_tech_id text, pre_existing_tech_id text,
    norm_unit text, measure_application_type text, elec_target_sector text,
    uec_kwh_meas double precision,            -- measure case
    uec_kwh_base1 double precision,           -- 1st baseline (pre-existing when there is one, else standard)
    uec_kwh_base2 double precision,           -- 2nd baseline (accelerated replacement: standard)
    electric_impact_profile_id text
);
