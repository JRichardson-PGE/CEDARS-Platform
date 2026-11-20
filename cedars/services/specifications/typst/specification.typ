// The specification document (printable.py): one filing specification version as a tagged PDF/UA-1 document.
// Every word comes from sys.inputs.data as JSON strings, inserted as text, never read as markup.
#let d = json(bytes(sys.inputs.data))

#set document(title: d.title, author: "California Public Utilities Commission", keywords: ("CEDARS", "filing specification"))
#set text(lang: "en", region: "us", font: "Libertinus Serif", size: 10pt)
#set page(
  paper: "us-letter",
  flipped: true,
  margin: (x: 0.6in, top: 0.6in, bottom: 0.7in),
  footer: context [
    #set text(size: 8pt)
    #d.title #h(1fr) Page #counter(page).display() of #counter(page).final().first()
  ],
)
#set par(justify: false, leading: 0.55em)
#set heading(numbering: none)
#show heading.where(level: 1, outlined: true): it => { pagebreak(weak: true); set text(size: 16pt); block(above: 0pt, below: 10pt, it) }
#show heading.where(level: 2): set text(size: 13pt)
#show heading.where(level: 3): set text(size: 11pt)
#show link: set text(fill: rgb("#1a4f8b"))
#set table(stroke: 0.5pt + rgb("#8a8a8a"), inset: 4pt, align: left + top)
#show table.cell.where(y: 0): set text(weight: "bold")
#set table(fill: (_, y) => if y == 0 { rgb("#e8edf3") })

#let words(s) = if s == none or s == "" { [-] } else { [#s] }

#title()
#d.status

#if d.summary != "" [#d.summary]

#if d.about != "" [#d.about]

#if d.page != "" [The version's page, with the same content and its downloads: #link(d.page)[#d.page]]

This document was made on #d.made_on from the version as it then stood. Its value lists are as in force on that day.

#outline(title: [Contents], depth: 2)

= About this version
#table(
  columns: (2fr, 7fr),
  table.header([Setting], [Value]),
  ..d.settings.map(r => (words(r.at(0)), words(r.at(1)))).flatten(),
)

#if not d.historic [
  #if d.prior == "" [
    = What changed
    This is the first version of the specification, so there is nothing earlier to compare it with.
  ] else [
    = What changed from #d.prior
    #if d.changes.len() == 0 [It says the same as #d.prior.]
    #for c in d.changes [
      == #c.heading
      #for i in c.items [- #i]
    ]
  ]
]

= Tables
#if d.historic [Each file of the download, with its columns in the download's order.] else [Each table of an upload, with its fields in template order and the rules about it.]
#for t in d.tables [- #t.name (#t.fields.len() fields)]

#for t in d.tables [
  == #t.name
  #t.facts

  #if d.historic {
    table(
      columns: (3fr, 2fr, 4fr),
      table.header([Column, as CEDARS labels it], [Read as], [Loaded into the platform as]),
      ..t.fields.map(f => (words(f.name), words(f.type), words(f.notes))).flatten(),
    )
  } else {
    table(
      columns: (2.2fr, 1.1fr, 1.6fr, 1.3fr, 5fr),
      table.header([Field], [Requirement], [Type], [Value list], [Definition and notes]),
      ..t.fields.map(f => (
        words(f.name), words(f.requirement), words(f.type), words(f.value_list),
        [#words(f.definition)#if f.notes != "" [ #linebreak() #text(size: 9pt)[#f.notes]]],
      )).flatten(),
    )
  }

  #if t.rules.len() > 0 [
    === Rules about the #t.name table
    #table(
      columns: (1.5fr, 1.2fr, 1.5fr, 4fr, 3fr),
      table.header([Rule], [Severity], [Field], [Rule in words], [Suggested fix]),
      ..t.rules.map(r => (words(r.id), words(r.severity), words(r.field), words(r.message), words(r.fix))).flatten(),
    )
  ] else if not d.historic [
    No rule set rule is about the #t.name table; each field is still checked against its type and value list.
  ]
]

#if not d.historic [
  = Built-in checks
  Checks written in CEDARS' code that run beside the rules, because they need what a rule cannot see: the program registry, earlier
  confirmed quarters, the filing's other tables. A finding shows the check's ID.
  #if d.checks.len() == 0 [This version lists no built-in checks.]
  #for g in d.checks [
    == #g.label
    #table(
      columns: (1.5fr, 1.6fr, 2fr, 4fr, 3fr),
      table.header([Check], [Severity], [Tables and fields], [What it finds], [How to fix it]),
      ..g.items.map(c => (words(c.id), words(c.severity), words(c.where), words(c.finds), words(c.fix))).flatten(),
    )
  ]
]

#if d.lists.len() > 0 [
  = Value lists
  The codes each field with a value list accepts, as in force on #d.made_on. Value lists change on their own dates, not with a
  specification version, and a retired code is kept with the last year it is accepted. The value list pages always show the codes in force.
  #for l in d.lists [
    == #if l.label == l.name [#l.name] else [#l.label (list #l.name)]
    #if not l.found [
      The codes of this list could not be read where this document was made. #if l.page != "" [They are on #link(l.page)[#l.page].]
    ] else if not l.shown [
      This list has #str(l.count) codes, too many to print here. Every code is in the fields-and-rules workbook download, and on
      #if l.page != "" [#link(l.page)[#l.page].] else [the value list's page.]
    ] else if l.count == 0 [
      This list has no codes yet.
    ] else [
      #table(
        columns: (2fr, 5fr, 1.5fr),
        table.header([Code], [Description], [Accepted]),
        ..l.codes.map(c => (words(c.at(0)), words(c.at(1)), words(c.at(2)))).flatten(),
      )
    ]
  ]
]
