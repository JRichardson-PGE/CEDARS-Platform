"""Submissions and workflow: one state machine for every upload-based module.

    draft -> validated -> (priced) -> confirmed -> published
               ^   |                     |
               |   +-- a change voids the calculation; confirm needs a run on the current rows
               +------ un-confirm, before the due date (the same version: nothing superseded is kept)
    after the due date: reopen (reason; approval when the policy says) -> version n+1, draft; version n stays readable

A module instantiates it with its own period or cycle (a Deadline), rule set and approval policy (WorkflowPolicy)."""
