"""Publication snapshots: what dashboards and downloads read. When a priced submission version is
published, its measure-level results are frozen as a Parquet file in the snapshots storage area, and a new release of its dataset (claims,
forecast or monthly) names the snapshots in force. A page reads one release, so its charts, tables and downloads come from the same files,
and the release's time is the "as of" every page shows."""
