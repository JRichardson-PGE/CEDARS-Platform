# Old links: the PDA, the PSR, the EM&V archive and CEDARS (BR-15.10)

Commission decisions, EM&V plans and reports cite PDA and PSR addresses (D.23-04-035 cites PDA document links). After cutover each old
address leads to the same document, file or study here, for as long as CPUC Energy Division keeps the energydataweb.com domains.

The platform answers the old addresses under two prefixes, keeping the rest of the address as it was, so the old domains need only a
fixed rewrite:

| Old address | Here | Leads to |
|---|---|---|
| PDA `#!/documents/<id>/view` | `/documents/pda/documents/<id>/view` | the document migrated from PDA document `<id>` |
| PDA `#!/documents/<id>/comments/list` | `/documents/pda/documents/<id>/comments/list` | its comments |
| PDA `/api/view/<id>/<file>` | `/documents/pda/api/view/<id>/<file>` | the file, opened in the browser where it can be |
| PDA `/api/downloads/<id>/<file>` | `/documents/pda/api/downloads/<id>/<file>` | the file, downloaded |
| any other PDA address | `/documents/pda/...` | the library |
| PSR `#!/project-status-view-edit/<id>` | `/documents/psr/project-status-view-edit/<id>` | the study with that project number |
| any other PSR address | `/documents/psr/...` | the study register |
| EM&V site `/cpuc/deliverableView.aspx?did=<id>` | `/documents/edw/cpuc/deliverableView.aspx?did=<id>` | PDA document `<id>`'s main file, opened in the browser where it can be |
| EM&V site `/cpucFiles/pdaDocs/<id>/<file>` | `/documents/edw/cpucFiles/pdaDocs/<id>/<file>` | that file of PDA document `<id>` |
| EM&V site `/cpuc/search.aspx`, `deliverableDownload.aspx` or `comment.aspx` with `?did=<id>` | `/documents/edw/cpuc/...` | the document (its comments for `comment.aspx`) |
| EM&V site `/cpucFiles/pdaHomeDocs/<section>/<file>` | `/documents/edw/cpucFiles/pdaHomeDocs/...` | the home-page file, once migrated (below) |
| any other EM&V site page | `/documents/edw/...` | the library's EM&V documents |

The prefixes are three. The "EM&V site" is the 2006-2013 one at www.energydataweb.com/cpuc, the PDA's predecessor: from 2017 its search
page sent visitors to the PDA, which kept its deliverable ids and file names, so its documents are the PDA's and need no import of their
own. It ran on IIS, so its addresses are matched in any case.

Every one is a permanent (301) redirect. A file is found by its PDA name even where the library cleaned the name or kept a refused type
inside a zip (`DocumentFile.legacy_name`). An address whose document was not migrated is "not found".

## On the old domains

The PDA's and PSR's own pages are hashbang addresses: the part after `#!` never reaches a server, so the old home page has to read it
in the browser. Replace each site with two things.

1. A server rule that sends every path to the same path under the new prefix (nginx shown; any web server or a CDN redirect rule does
   the same):

   ```nginx
   # pda.energydataweb.com
   location = / { root /srv/pda-redirect; try_files /index.html =404; }
   location / { return 301 https://NEW-HOST/documents/pda$request_uri; }

   # psr.energydataweb.com
   location = / { root /srv/psr-redirect; try_files /index.html =404; }
   location / { return 301 https://NEW-HOST/documents/psr$request_uri; }

   # www.energydataweb.com (the 2006-2013 EM&V site; its CMS under /cpuc-cms/ is not migrated)
   location ~* ^/(cpuc|cpucfiles)(/|$) { return 301 https://NEW-HOST/documents/edw$request_uri; }
   ```

   The EM&V site's own addresses are not hashbangs and need only this rule. Its certificate has lapsed, so renew the domain's
   certificate before the switch, or its `https://` links will fail before reaching the rule.

2. An `index.html` at `/` that moves the hashbang address to the same place and links there for anyone without scripts (PSR: the same
   with `/documents/psr` and the study register):

   ```html
   <!doctype html>
   <html lang="en"><head><meta charset="utf-8"><title>The PDA has moved</title>
   <script>
     var h = location.hash.replace(/^#!?/, "");
     location.replace("https://NEW-HOST/documents/pda" + (h || "/"));
   </script></head>
   <body><p>The Public Document Area is now part of CEDARS: <a href="https://NEW-HOST/documents/">the document library</a>.</p></body></html>
   ```

Run the migrations first (`manage.py documents_import_pda`, then `manage.py documents_import_psr`, so a project's work order number
finds the PDA's work order), then switch the domains, then try the links cited in D.23-04-035 against the new host. A PSR project keeps
its number, so `#!/project-status-view-edit/<id>` leads to it; import the PSR before anyone adds a study here, or a new study may take a
project's number (the import names any it leaves out).

## CEDARS' own document addresses

The platform takes over CEDARS' domain, so CEDARS' old document addresses reach it with no rule elsewhere. Each address the migration
(`manage.py documents_import_cedars`) was given leads to its record or file (`LegacyLink`, kept decoded and without the host):

| Old address | Leads to |
|---|---|
| `/deer-resources/<section>/resource/<id>/history/` | the DEER resource, with every version |
| `/deer-resources/<section>/file/<id>/download/` | that version's file (a PDF opens in the browser, as CEDARS served it) |
| `/documents/guidance-document/download/<id>/` | the staff guidance file |
| `/documents/history/<id>/` | the program document (an implementation plan) |
| `/documents/download/<id>/<kind>change_summary\|main\|redline)/` | that version's main file, redline or change summary (CEDARS' route, kept exactly) |
| any other address under those prefixes (a list or a section) | the library's collection (DEER resources, staff guidance, PA documents, program files) |

A record or file address the migration was not given is "not found". CEDARS' generated downloads (a section's "Download All", the
filing and program zips under `/filings/download/`) are not documents and are not migrated; the platform builds its own.

The manifest is one CSV row per file of a version (its columns are in `documents/cedars_import.py`), written from CEDARS' database at
cutover or, for the public parts, from its pages.

## The EM&V site's home-page files

Of the 20 files the EM&V site's home page linked under `/cpucFiles/pdaHomeDocs/<section>/` (2 meetings, 3 project status, 4 program
information), only the two EM&V plan zips are in the PDA. The other 18 (10 quarterly meeting decks, 3 project status spreadsheets, 5
program information documents) come from CPUC Energy Division's own copies through the same manifest: collection `emv`, a key starting
`edw/` (`edw/pdaHomeDocs/2/EMV 9-9-14.zip`), a deliverable type (Quarterly Meeting Materials, Project Status Report, Program
Information), and the file's old address in `file_links` (with or without `http://www.energydataweb.com`). Their old addresses are kept
under `/edw`, apart from CEDARS' own, and the document page says "Migrated from: The 2006-2013 EM&V site". An EM&V record without an
`edw/` key, or an `edw/` key in another collection, is refused.
