# The PDA's and PSR's old links (BR-15.10)

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
   ```

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
