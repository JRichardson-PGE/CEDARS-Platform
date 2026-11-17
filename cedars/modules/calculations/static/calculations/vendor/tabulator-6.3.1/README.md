# Tabulator 6.3.1

The Edit inputs spreadsheet grid (`calculations/edit_grid.js`), as ReCET's own Edit inputs page uses it. MIT licensed (LICENSE).
Vendored unchanged from the npm package `tabulator-tables@6.3.1`, whose tarball's integrity was checked against the registry's
(`sha512-qFW7kfadtcaISQIibKAIy0f3eeIXUVi8242Vly1iJfMD79kfEGzfczNuPBN/80hDxHzQJXYbmJ8VipI40hQtfA==`). Served from this site only
(the CSP allows same-origin scripts). `tests/test_edit_grid.py` holds the files to these SHA-256s:

| File | From the package | SHA-256 |
| --- | --- | --- |
| tabulator.min.js | dist/js/tabulator.min.js | e952272c3b2afa4ebb60cef5db8cbe9cbaabaa52b50c3cd3d22993ca5215a6ff |
| tabulator.min.js.map | dist/js/tabulator.min.js.map | 5cf6cc2cc16a120e00c888bec012cd39a750fee6b83cde161e6fa4ccf15194db |
| tabulator_simple.min.css | dist/css/tabulator_simple.min.css | 8a7431ff86fa6aa02943b70470df77fa7094e7eac9f2420b6780c6970c7345eb |
| tabulator_simple.min.css.map | dist/css/tabulator_simple.min.css.map | 1513412aa3ce621f6d7cd9bde252172a0de4fb569dc80291561b7a31c8a6263a |
| LICENSE | LICENSE | 6031e51b8450fa6047887f018951c8cf1456d98a5a08291774410c13696de18e |

The source maps are vendored because the script and stylesheet name them and the static-file manifest refuses a reference it cannot find.
